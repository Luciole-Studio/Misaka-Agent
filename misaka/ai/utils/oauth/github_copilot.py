"""GitHub Copilot OAuth helpers."""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from misaka.ai.models import get_models
from misaka.ai.utils.oauth.device_code import poll_oauth_device_code_flow
from misaka.ai.utils.oauth.types import OAuthCredentials, OAuthLoginCallbacks
from misaka.utils.values import signal_aborted

CLIENT_ID = base64.b64decode("SXYxLmI1MDdhMDhjODdlY2ZlOTg=").decode("utf-8")
COPILOT_HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.35.0",
    "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "Copilot-Integration-Id": "vscode-chat",
}


def normalize_domain(input_text: str) -> str | None:
    trimmed = input_text.strip()
    if not trimmed:
        return None
    try:
        parsed = urlparse(trimmed if "://" in trimmed else f"https://{trimmed}")
        hostname = parsed.hostname
        if not hostname or any(character.isspace() for character in hostname):
            return None
        return hostname
    except ValueError:
        return None


def _get_urls(domain: str) -> dict[str, str]:
    return {
        "deviceCodeUrl": f"https://{domain}/login/device/code",
        "accessTokenUrl": f"https://{domain}/login/oauth/access_token",
        "copilotTokenUrl": f"https://api.{domain}/copilot_internal/v2/token",
    }


def _get_base_url_from_token(token: str) -> str | None:
    import re

    match = re.search(r"proxy-ep=([^;]+)", token)
    if not match:
        return None
    api_host = re.sub(r"^proxy\.", "api.", match.group(1))
    return f"https://{api_host}"


def get_github_copilot_base_url(token: str | None = None, enterprise_domain: str | None = None) -> str:
    if token:
        url_from_token = _get_base_url_from_token(token)
        if url_from_token:
            return url_from_token
    if enterprise_domain:
        return f"https://copilot-api.{enterprise_domain}"
    return "https://api.individual.githubcopilot.com"


async def _fetch_json(url: str, **kwargs: Any) -> Any:
    async with httpx.AsyncClient(timeout=None) as client:
        response = await client.request(kwargs.pop("method", "GET"), url, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code} {response.reason_phrase}: {response.text}")
    return response.json()


async def _start_device_flow(domain: str) -> dict[str, Any]:
    urls = _get_urls(domain)
    data = await _fetch_json(
        urls["deviceCodeUrl"],
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "GitHubCopilotChat/0.35.0",
        },
        content=urlencode({"client_id": CLIENT_ID, "scope": "read:user"}).encode(),
    )
    if not isinstance(data, dict):
        raise RuntimeError("Invalid device code response")  # noqa: TRY004 - callers treat bad input as ValueError
    device_code = data.get("device_code")
    user_code = data.get("user_code")
    verification_uri = data.get("verification_uri")
    interval = data.get("interval")
    expires_in = data.get("expires_in")
    if (
        not isinstance(device_code, str)
        or not isinstance(user_code, str)
        or not isinstance(verification_uri, str)
        or (interval is not None and not isinstance(interval, (int, float)))
        or not isinstance(expires_in, (int, float))
    ):
        raise RuntimeError("Invalid device code response fields")
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "interval": interval,
        "expires_in": expires_in,
    }


async def _poll_for_github_access_token(domain: str, device: dict[str, Any], signal: Any = None) -> str:
    urls = _get_urls(domain)

    async def poll() -> dict[str, Any]:
        raw = await _fetch_json(
            urls["accessTokenUrl"],
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "GitHubCopilotChat/0.35.0",
            },
            content=urlencode(
                {
                    "client_id": CLIENT_ID,
                    "device_code": device["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }
            ).encode(),
        )
        if isinstance(raw, dict) and isinstance(raw.get("access_token"), str):
            return {"status": "complete", "accessToken": raw["access_token"]}
        if isinstance(raw, dict) and isinstance(raw.get("error"), str):
            error = raw["error"]
            description = raw.get("error_description")
            if error == "authorization_pending":
                return {"status": "pending"}
            if error == "slow_down":
                return {"status": "slow_down"}
            description_suffix = f": {description}" if description else ""
            return {"status": "failed", "message": f"Device flow failed: {error}{description_suffix}"}
        return {"status": "failed", "message": "Invalid device token response"}

    return await poll_oauth_device_code_flow(
        intervalSeconds=device.get("interval"),
        expiresInSeconds=device.get("expires_in"),
        poll=poll,
        signal=signal,
    )


async def refresh_github_copilot_token(refresh_token: str, enterprise_domain: str | None = None) -> OAuthCredentials:
    domain = enterprise_domain or "github.com"
    urls = _get_urls(domain)
    raw = await _fetch_json(
        urls["copilotTokenUrl"],
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {refresh_token}",
            **COPILOT_HEADERS,
        },
    )
    if not isinstance(raw, dict):
        raise RuntimeError("Invalid Copilot token response")  # noqa: TRY004 - callers treat bad input as ValueError
    if not isinstance(raw.get("token"), str) or not isinstance(raw.get("expires_at"), (int, float)):
        raise RuntimeError("Invalid Copilot token response fields")  # noqa: TRY004 - callers treat bad input as ValueError
    return OAuthCredentials(
        refresh=refresh_token,
        access=raw["token"],
        expires=int(raw["expires_at"] * 1000 - 5 * 60 * 1000),
        enterpriseUrl=enterprise_domain,
    )


async def _enable_github_copilot_model(token: str, model_id: str, enterprise_domain: str | None = None) -> bool:
    base_url = get_github_copilot_base_url(token, enterprise_domain)
    url = f"{base_url}/models/{model_id}/policy"
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    **COPILOT_HEADERS,
                    "openai-intent": "chat-policy",
                    "x-interaction-type": "chat-policy",
                },
                json={"state": "enabled"},
            )
        return response.is_success
    except Exception:  # noqa: BLE001 - an unreachable policy endpoint reads as 'not enabled'
        return False


_ENABLE_CONCURRENCY = 5    # one request per catalog model; do not open them all at once


async def _enable_all_github_copilot_models(
    token: str,
    enterprise_domain: str | None = None,
    on_progress: Any = None,
) -> None:
    models = get_models("github-copilot")
    limit = asyncio.Semaphore(_ENABLE_CONCURRENCY)

    async def enable(model_id: str) -> None:
        async with limit:
            await _enable_and_report(model_id, token, enterprise_domain, on_progress)

    await asyncio.gather(*[enable(model.id) for model in models])


async def _enable_and_report(model_id: str, token: str, enterprise_domain: str | None, on_progress: Any) -> None:
    success = await _enable_github_copilot_model(token, model_id, enterprise_domain)
    if on_progress is not None:
        on_progress(model_id, success)


async def login_github_copilot(options: dict[str, Any]) -> OAuthCredentials:
    input_text = await options["onPrompt"](
        {
            "message": "GitHub Enterprise URL/domain (blank for github.com)",
            "placeholder": "company.ghe.com",
            "allowEmpty": True,
        }
    )

    signal = options.get("signal")
    if signal_aborted(signal):
        raise RuntimeError("Login cancelled")

    trimmed = input_text.strip()
    enterprise_domain = normalize_domain(input_text)
    if trimmed and not enterprise_domain:
        raise RuntimeError("Invalid GitHub Enterprise URL/domain")
    domain = enterprise_domain or "github.com"

    device = await _start_device_flow(domain)
    options["onDeviceCode"](
        {
            "userCode": device["user_code"],
            "verificationUri": device["verification_uri"],
            "intervalSeconds": device.get("interval"),
            "expiresInSeconds": device["expires_in"],
        }
    )

    github_access_token = await _poll_for_github_access_token(domain, device, signal)
    credentials = await refresh_github_copilot_token(github_access_token, enterprise_domain)
    if options.get("onProgress") is not None:
        options["onProgress"]("Enabling models...")
    await _enable_all_github_copilot_models(credentials.access, enterprise_domain)
    return credentials


class _GitHubCopilotOAuthProvider:
    id = "github-copilot"
    name = "GitHub Copilot"

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials:
        return await login_github_copilot(
            {
                "onDeviceCode": callbacks.onDeviceCode,
                "onPrompt": callbacks.onPrompt,
                "onProgress": callbacks.onProgress,
                "signal": callbacks.signal,
            }
        )

    async def refreshToken(self, credentials: OAuthCredentials) -> OAuthCredentials:
        enterprise_url = getattr(credentials, "enterpriseUrl", None)
        return await refresh_github_copilot_token(credentials.refresh, enterprise_url)

    def getApiKey(self, credentials: OAuthCredentials) -> str:
        return credentials.access

    def modifyModels(self, models: list[Any], credentials: OAuthCredentials) -> list[Any]:
        enterprise_url = getattr(credentials, "enterpriseUrl", None)
        domain = normalize_domain(enterprise_url) if enterprise_url else None
        base_url = get_github_copilot_base_url(credentials.access, domain)
        return [model.model_copy(update={"baseUrl": base_url}) if model.provider == "github-copilot" else model for model in models]


github_copilot_oauth_provider = _GitHubCopilotOAuthProvider()

normalizeDomain = normalize_domain
getGitHubCopilotBaseUrl = get_github_copilot_base_url
loginGitHubCopilot = login_github_copilot
refreshGitHubCopilotToken = refresh_github_copilot_token
githubCopilotOAuthProvider = github_copilot_oauth_provider

__all__ = [
    "getGitHubCopilotBaseUrl",
    "get_github_copilot_base_url",
    "githubCopilotOAuthProvider",
    "github_copilot_oauth_provider",
    "loginGitHubCopilot",
    "login_github_copilot",
    "normalizeDomain",
    "normalize_domain",
    "refreshGitHubCopilotToken",
    "refresh_github_copilot_token",
]

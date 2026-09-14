"""GitHub Copilot OAuth helpers."""

from __future__ import annotations

import asyncio
import base64
import math
import time
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from misaka.ai.models import get_models
from misaka.ai.utils.oauth.device_code import poll_oauth_device_code_flow
from misaka.ai.utils.oauth.types import (
    OAuthCredentials,
    OAuthDeviceCodeInfo,
    OAuthLoginCallbacks,
    OAuthPrompt,
)
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


def copilot_enterprise_domain(credentials: Any) -> str | None:
    """The host an enterprise seat answers on, from whatever the credential stored.

    The field is called ``enterpriseUrl`` and is filled from what the person typed, so it
    can be a URL, a host, or a host with a trailing slash. Everything downstream builds
    ``https://{domain}/...`` out of it, and interpolating a URL there produces
    ``https://https://github.acme.com//login/...``: a refresh that cannot succeed. One
    normalisation, used by every reader, is what upstream does.
    """
    enterprise_url = getattr(credentials, "enterpriseUrl", None)
    if not isinstance(enterprise_url, str) or not enterprise_url:
        return None
    return normalize_domain(enterprise_url)


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
        raise RuntimeError("Invalid device code response")  # noqa: TRY004 - a malformed remote response is a runtime failure, not a caller type error
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
        # Upstream sets this: the endpoint rejects a poll that arrives before the
        # device code is registered (device-code.ts callers).
        waitBeforeFirstPoll=True,
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
        raise RuntimeError("Invalid Copilot token response")  # noqa: TRY004 - a malformed remote response is a runtime failure, not a caller type error
    if not isinstance(raw.get("token"), str) or not isinstance(raw.get("expires_at"), (int, float)):
        raise RuntimeError("Invalid Copilot token response fields")  # noqa: TRY004 - a malformed remote response is a runtime failure, not a caller type error
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


COPILOT_API_VERSION = "2025-05-01"
_MODEL_FETCH_MAX_RETRIES = 2
_MODEL_FETCH_MAX_ELAPSED_MS = 5000
_REQUEST_TIMEOUT_SECONDS = 5.0


def _as_record(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def parse_github_copilot_model_catalog(
    raw: Any, allow_policy_fallback: bool
) -> tuple[list[str], list[str]]:
    """Split the seat's ``/models`` answer into what it can use and what it could enable.

    Returns ``(availableModelIds, policyModelIds)``. A model whose ``tool_calls`` support
    is explicitly false is dropped outright -- this agent cannot use one.
    """
    data = (_as_record(raw) or {}).get("data")
    if not isinstance(data, list):
        # A malformed server answer is a protocol failure, not a caller type error, and
        # every other failure in this module is reported the same way.
        raise RuntimeError("Invalid Copilot models response")  # noqa: TRY004

    account_models: list[dict[str, Any]] = []
    for raw_item in data:
        item = _as_record(raw_item)
        model_id = item.get("id") if item else None
        if not item or not isinstance(model_id, str):
            continue
        supports = _as_record((_as_record(item.get("capabilities")) or {}).get("supports"))
        if supports is not None and supports.get("tool_calls") is False:
            continue
        account_models.append(
            {
                "id": model_id,
                "pickerEnabled": item.get("model_picker_enabled") is True,
                "policyState": (_as_record(item.get("policy")) or {}).get("state"),
            }
        )

    picker_model_ids = [
        model["id"]
        for model in account_models
        if model["pickerEnabled"] and model["policyState"] != "disabled"
    ]
    use_policy_fallback = allow_policy_fallback and not picker_model_ids
    if picker_model_ids or not allow_policy_fallback:
        available_model_ids = picker_model_ids
    else:
        # Some Individual accounts report every picker flag false despite explicit enabled
        # policies; only that endpoint gets the fallback.
        available_model_ids = [
            model["id"] for model in account_models if model["policyState"] == "enabled"
        ]

    catalog_ids = {model.id for model in get_models("github-copilot")}
    policy_model_ids = [
        model["id"]
        for model in account_models
        if model["policyState"] == "unconfigured"
        and model["id"] in catalog_ids
        and (model["pickerEnabled"] or use_policy_fallback)
    ]
    return available_model_ids, policy_model_ids


async def _fetch_with_rate_limit_retry(
    url: str,
    headers: dict[str, str],
    signal: Any,
    max_retries: int,
    max_elapsed_ms: int,
) -> httpx.Response:
    """GET with 429 backoff, bounded by both a retry count and a wall-clock budget."""
    deadline = (
        time.monotonic() + max_elapsed_ms / 1000 if max_retries > 0 and max_elapsed_ms > 0 else None
    )
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        for retry in range(max_retries + 1):
            response = await client.get(url, headers=headers)
            if response.status_code != 429 or retry == max_retries:
                return response

            delay_ms: float = 500 * 2**retry
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    delay_ms = float(retry_after) * 1000
                except ValueError:
                    parsed = parsedate_to_datetime(retry_after)
                    if parsed is None:
                        return response
                    delay_ms = (parsed.timestamp() - time.time()) * 1000
                if not math.isfinite(delay_ms):
                    return response
            delay_ms = max(0.0, delay_ms)
            if deadline is not None and delay_ms / 1000 >= deadline - time.monotonic():
                return response
            if signal_aborted(signal):
                raise RuntimeError("Login cancelled")
            await asyncio.sleep(delay_ms / 1000)
    raise RuntimeError("unreachable")  # pragma: no cover - the loop always returns


async def fetch_github_copilot_models(
    copilot_token: str,
    enterprise_domain: str | None = None,
    signal: Any = None,
    max_retries: int = _MODEL_FETCH_MAX_RETRIES,
    max_elapsed_ms: int = _MODEL_FETCH_MAX_ELAPSED_MS,
) -> tuple[list[str], list[str]]:
    base_url = get_github_copilot_base_url(copilot_token, enterprise_domain)
    allow_policy_fallback = base_url == "https://api.individual.githubcopilot.com"
    response = await _fetch_with_rate_limit_retry(
        f"{base_url}/models",
        {
            "Accept": "application/json",
            "Authorization": f"Bearer {copilot_token}",
            **COPILOT_HEADERS,
            "X-GitHub-Api-Version": COPILOT_API_VERSION,
        },
        signal,
        max_retries,
        max_elapsed_ms,
    )
    if not response.is_success:
        raise RuntimeError(f"{response.status_code} {response.reason_phrase}: {response.text}")
    return parse_github_copilot_model_catalog(response.json(), allow_policy_fallback)


async def _enable_github_copilot_models(
    token: str,
    model_ids: list[str],
    enterprise_domain: str | None,
    signal: Any = None,
) -> list[str]:
    """Enable each model in turn, stopping at the first failure.

    Sequential and fail-fast, as upstream is: the policy endpoint is rate limited, and a
    failure part-way usually means the whole burst would fail too.
    """
    enabled: list[str] = []
    for model_id in model_ids:
        if signal_aborted(signal):
            raise RuntimeError("Login cancelled")
        try:
            if await _enable_github_copilot_model(token, model_id, enterprise_domain):
                enabled.append(model_id)
        except Exception:  # noqa: BLE001 - one refusal ends the batch, it does not fail login
            break
    return enabled


async def login_github_copilot(options: dict[str, Any]) -> OAuthCredentials:
    # The declared payload, not a bare dict: `types.py` says `Callable[[OAuthPrompt],
    # Awaitable[str]]`, and a caller that reads it by attribute -- `ai/cli.py` prints
    # `prompt.message` -- got an AttributeError before the login had asked anything.
    input_text = await options["onPrompt"](
        OAuthPrompt(
            message="GitHub Enterprise URL/domain (blank for github.com)",
            placeholder="company.ghe.com",
            allowEmpty=True,
        )
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
    # The declared payload, not a bare dict: `types.py` says `Callable[[OAuthDeviceCodeInfo],
    # None]`, and a caller that reads it by attribute -- `ai/cli.py` prints `info.verificationUri` --
    # got an AttributeError before the login had drawn anything. `openrouter.py` and
    # `radius.py` already pass the object.
    options["onDeviceCode"](
        OAuthDeviceCodeInfo(
            userCode=device["user_code"],
            verificationUri=device["verification_uri"],
            intervalSeconds=device.get("interval"),
            expiresInSeconds=device["expires_in"],
        )
    )

    github_access_token = await _poll_for_github_access_token(domain, device, signal)
    credentials = await refresh_github_copilot_token(github_access_token, enterprise_domain)

    # Ask the seat what it actually has rather than enabling every catalog model blindly:
    # the list is what `filterModels` narrows the provider's models to, and enabling only
    # the unconfigured ones turns a burst of one request per catalog entry into a few.
    available_model_ids, policy_model_ids = await fetch_github_copilot_models(
        credentials.access, enterprise_domain, signal
    )
    enabled_model_ids: list[str] = []
    if policy_model_ids:
        if options.get("onProgress") is not None:
            options["onProgress"]("Enabling models...")
        enabled_model_ids = await _enable_github_copilot_models(
            credentials.access, policy_model_ids, enterprise_domain, signal
        )
    return credentials.model_copy(
        update={"availableModelIds": list(dict.fromkeys([*available_model_ids, *enabled_model_ids]))}
    )


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

    async def refreshToken(self, credentials: OAuthCredentials, signal: Any | None = None) -> OAuthCredentials:
        del signal  # the built-in refresh has no cancellation point; the parameter is the upstream contract
        return await refresh_github_copilot_token(credentials.refresh, copilot_enterprise_domain(credentials))

    def getApiKey(self, credentials: OAuthCredentials) -> str:
        return credentials.access

    def getBaseUrl(self, credentials: OAuthCredentials) -> str:
        """The endpoint this particular seat has to talk to.

        Copilot hands each seat its proxy in the token itself (``proxy-ep=``), and an
        enterprise install answers on its own host entirely. Upstream returns it from
        ``toAuth`` alongside the key (``auth/oauth/github-copilot.ts:501-506``); misaka's
        older flow contract has no slot for it, so this optional method is that slot --
        ``ai/auth/oauth_bridge.py`` uses it when a flow offers one.

        This covers the ``ai/auth`` path only. Without it, ``toAuth`` there reports no
        base URL and the request falls back to the provider's static
        ``api.individual.githubcopilot.com`` (``ai/provider_definitions.py:625``), which is
        right for personal accounts and silently wrong for the rest. The legacy
        ``core/model_registry`` path does not go through here: it reaches ``modifyModels``
        below, which stamps the same per-credential URL onto the models themselves.
        """
        return get_github_copilot_base_url(credentials.access, copilot_enterprise_domain(credentials))

    def modifyModels(self, models: list[Any], credentials: OAuthCredentials) -> list[Any]:
        base_url = get_github_copilot_base_url(credentials.access, copilot_enterprise_domain(credentials))
        return [model.model_copy(update={"baseUrl": base_url}) if model.provider == "github-copilot" else model for model in models]


github_copilot_oauth_provider = _GitHubCopilotOAuthProvider()

normalizeDomain = normalize_domain
getGitHubCopilotBaseUrl = get_github_copilot_base_url
loginGitHubCopilot = login_github_copilot
refreshGitHubCopilotToken = refresh_github_copilot_token
githubCopilotOAuthProvider = github_copilot_oauth_provider

__all__ = [
    "copilot_enterprise_domain",
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

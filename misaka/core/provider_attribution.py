"""Tell a gateway which client is calling it.

Translated from pi's ``src/core/provider-attribution.ts`` (v1.1.0). MISAKA fork: the values
carry MISAKA's own name (``CLIENT_NAME``/``CLIENT_URL``) where pi sends "pi"/"Pi" and
https://pi.dev. Aggregators route and meter by client: OpenRouter ranks applications
publicly, NVIDIA's header is literally called ``X-BILLING-INVOKE-ORIGIN``. Send nothing and the
traffic is anonymous to them; send this and it is attributed. No account, no content, no
machine identity travels with it -- the whole payload is the string "misaka".

Two kinds of header live here and they answer to different switches:

* **Attribution** is a courtesy, so it is gated on ``isInstallTelemetryEnabled`` -- an
  install that has opted out of being identified is not identified here either.
* **Session headers** are function, not courtesy: a gateway that threads requests into
  one conversation needs the session id to do it. Upstream keeps them outside the
  telemetry gate for that reason, and so does this: switching off attribution should not
  quietly break conversation threading.

Precedence follows pi's ``mergeProviderAttributionHeaders``: session headers first, then
attribution, then whatever the caller passed -- so an explicit header always wins over
one of ours, and an extension's ``before_provider_headers`` handler (which runs later
still, in ``sdk.py``) wins over everything.
"""

from __future__ import annotations

from typing import Any

from misaka.core.telemetry import isInstallTelemetryEnabled
from misaka.utils.whatwg_url import URL

OPENROUTER_HOST = "openrouter.ai"
NVIDIA_NIM_HOST = "integrate.api.nvidia.com"
CLOUDFLARE_API_HOST = "api.cloudflare.com"
CLOUDFLARE_AI_GATEWAY_HOST = "gateway.ai.cloudflare.com"
OPENCODE_HOST = "opencode.ai"

# What MISAKA calls itself on the wire. Upstream sends "pi" and links to pi.dev; sending
# either from MISAKA would attribute this traffic to somebody else's project.
CLIENT_NAME = "misaka"
CLIENT_URL = "https://github.com/Luciole-Studio/Misaka-Agent"


def matchesHost(baseUrl: str, expectedHost: str) -> bool:
    try:
        return URL(baseUrl).hostname == expectedHost
    except TypeError:
        return False


def isOpenRouterModel(model: Any) -> bool:
    return model.provider == "openrouter" or OPENROUTER_HOST in model.baseUrl


def isNvidiaNimModel(model: Any) -> bool:
    return model.provider == "nvidia" or matchesHost(model.baseUrl, NVIDIA_NIM_HOST)


def isCloudflareModel(model: Any) -> bool:
    return (
        model.provider == "cloudflare-workers-ai"
        or model.provider == "cloudflare-ai-gateway"
        or matchesHost(model.baseUrl, CLOUDFLARE_API_HOST)
        or matchesHost(model.baseUrl, CLOUDFLARE_AI_GATEWAY_HOST)
    )


def getDefaultAttributionHeaders(model: Any, settingsManager: Any) -> dict[str, str] | None:
    if not isInstallTelemetryEnabled(settingsManager):
        return None

    if isOpenRouterModel(model):
        return {
            "HTTP-Referer": CLIENT_URL,
            "X-OpenRouter-Title": CLIENT_NAME,
            "X-OpenRouter-Categories": "cli-agent",
        }

    if isNvidiaNimModel(model):
        return {
            "X-BILLING-INVOKE-ORIGIN": CLIENT_NAME,
        }

    if isCloudflareModel(model):
        return {
            "User-Agent": f"{CLIENT_NAME}-coding-agent",
        }

    return None


def getSessionHeaders(model: Any, sessionId: str | None) -> dict[str, str] | None:
    if not sessionId:
        return None
    if (
        model.provider != "opencode"
        and model.provider != "opencode-go"
        and not matchesHost(model.baseUrl, OPENCODE_HOST)
    ):
        return None
    return {"x-opencode-session": sessionId, "x-opencode-client": CLIENT_NAME}


def mergeProviderAttributionHeaders(
    model: Any,
    settingsManager: Any,
    sessionId: str | None,
    *headerSources: dict[str, str] | None,
) -> dict[str, str] | None:
    merged: dict[str, str] = {
        **(getSessionHeaders(model, sessionId) or {}),
        **(getDefaultAttributionHeaders(model, settingsManager) or {}),
    }

    for headers in headerSources:
        if headers:
            merged.update(headers)

    return merged if len(merged) > 0 else None


__all__ = ["CLIENT_NAME", "CLIENT_URL", "mergeProviderAttributionHeaders"]

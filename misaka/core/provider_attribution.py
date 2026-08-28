"""Tell a gateway which client is calling it.

Ported from pi's ``src/core/provider-attribution.ts``, with MISAKA's own name in the
values. Aggregators route and meter by client: OpenRouter ranks applications publicly,
NVIDIA's header is literally called ``X-BILLING-INVOKE-ORIGIN``. Send nothing and the
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
from urllib.parse import urlparse

from misaka.core.telemetry import isInstallTelemetryEnabled

OPENROUTER_HOST = "openrouter.ai"
NVIDIA_NIM_HOST = "integrate.api.nvidia.com"
CLOUDFLARE_API_HOST = "api.cloudflare.com"
CLOUDFLARE_AI_GATEWAY_HOST = "gateway.ai.cloudflare.com"
OPENCODE_HOST = "opencode.ai"

# What MISAKA calls itself on the wire. Upstream sends "pi" and links to pi.dev; sending
# either from MISAKA would attribute this traffic to somebody else's project.
CLIENT_NAME = "misaka"
CLIENT_URL = "https://github.com/nishikinokki/misaka"


def _field(model: Any, name: str, default: str = "") -> str:
    value = getattr(model, name, None)
    if value is None and isinstance(model, dict):
        value = model.get(name)
    return str(value or default)


def _matches_host(base_url: str, expected_host: str) -> bool:
    try:
        return urlparse(base_url).hostname == expected_host
    except ValueError:
        return False


def _is_openrouter(model: Any) -> bool:
    base_url = _field(model, "baseUrl")
    return _field(model, "provider") == "openrouter" or OPENROUTER_HOST in base_url


def _is_nvidia_nim(model: Any) -> bool:
    return _field(model, "provider") == "nvidia" or _matches_host(_field(model, "baseUrl"), NVIDIA_NIM_HOST)


def _is_cloudflare(model: Any) -> bool:
    base_url = _field(model, "baseUrl")
    return (
        _field(model, "provider") in ("cloudflare-workers-ai", "cloudflare-ai-gateway")
        or _matches_host(base_url, CLOUDFLARE_API_HOST)
        or _matches_host(base_url, CLOUDFLARE_AI_GATEWAY_HOST)
    )


def _is_opencode(model: Any) -> bool:
    provider = _field(model, "provider")
    return provider in ("opencode", "opencode-go") or _matches_host(
        _field(model, "baseUrl"), OPENCODE_HOST
    )


def _attribution_headers(model: Any, settings_manager: Any) -> dict[str, str]:
    if not isInstallTelemetryEnabled(settings_manager):
        return {}
    if _is_openrouter(model):
        return {
            "HTTP-Referer": CLIENT_URL,
            "X-OpenRouter-Title": CLIENT_NAME,
            "X-OpenRouter-Categories": "cli-agent",
        }
    if _is_nvidia_nim(model):
        return {"X-BILLING-INVOKE-ORIGIN": CLIENT_NAME}
    if _is_cloudflare(model):
        return {"User-Agent": f"{CLIENT_NAME}-coding-agent"}
    return {}


def _session_headers(model: Any, session_id: str | None) -> dict[str, str]:
    if not session_id or not _is_opencode(model):
        return {}
    return {"x-opencode-session": session_id, "x-opencode-client": CLIENT_NAME}


def merge_provider_attribution_headers(
    model: Any,
    settings_manager: Any,
    session_id: str | None,
    *header_sources: dict[str, str] | None,
) -> dict[str, str] | None:
    """Session and attribution headers, then the caller's, or ``None`` if empty."""
    merged: dict[str, str] = {
        **_session_headers(model, session_id),
        **_attribution_headers(model, settings_manager),
    }
    for headers in header_sources:
        if headers:
            merged.update(headers)
    return merged or None


__all__ = ["CLIENT_NAME", "CLIENT_URL", "merge_provider_attribution_headers"]

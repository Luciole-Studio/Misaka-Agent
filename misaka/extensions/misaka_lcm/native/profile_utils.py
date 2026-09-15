from typing import Optional
from urllib.parse import urlparse
from .reasoning_effort import clamp_effort as _clamp_effort
DEFAULT_ACTUAL_BASE_URL = "https://api.actual.inc/v1"

from .support import base_url_host_matches
from ..host.profiles import normalize_provider


def clamp_reasoning_effort_to_supported(
    effort: Optional[str], supported_efforts: Optional[list[str]]) -> Optional[str]:
    """Thin wrapper over :func:`agent.reasoning_effort.clamp_effort`: keep a supported level verbatim,
    else the nearest WEAKER supported level (never silently escalate cost), else the weakest; unknown
    supported-sets and bespoke level names pass through unchanged."""
    return _clamp_effort(effort, supported_efforts)


def opencode_provider_family(provider_id: Optional[str]) -> Optional[str]:
    """Resolve a provider id (canonical or prefixed) to its OpenCode family, or None.

    Returns ``"opencode-zen"`` or ``"opencode-go"`` for the built-in providers AND for custom providers
    whose name extends a family slug (e.g. ``opencode-go-bridge`` pointing at
    ``https://opencode.ai/zen/go/v1``, issue #85589). Matching is case-insensitive. Custom family providers
    need the same per-model api_mode routing and /v1 base-url normalization as the built-ins — this
    predicate is the single owner of that family-membership question; do not re-implement it inline.
    """
    raw = str(provider_id or "").strip().lower()
    if not raw:
        return None
    canonical = normalize_provider(provider_id)
    if canonical in _OPENCODE_FAMILIES:
        return canonical
    return next((f for f in _OPENCODE_FAMILIES if raw.startswith(f)), None)


_OPENCODE_FAMILIES = ("opencode-free", "opencode-go", "opencode-zen")


def is_actual_local_base_url(base_url: str) -> bool:
    """Return True for Actual's loopback local API endpoint."""
    try:
        host = (urlparse(base_url or "").hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def normalize_actual_base_url(base_url: str) -> str:
    """Return Actual's OpenAI-compatible base URL (hosted api.actual.inc or the loopback local server;
    both expose a /v1 surface for the Responses transport)."""
    url = str(base_url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_ACTUAL_BASE_URL
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path.rstrip("/")
    except Exception:
        return url
    if path in {"", "/"} and (host == "api.actual.inc" or is_actual_local_base_url(url)):
        return url + "/v1"
    return url


def _is_opencode_endpoint(base_url: str | None) -> bool:
    """OpenCode's Zen/Go relay (opencode.ai)."""
    return base_url_host_matches(base_url or "", "opencode.ai")

"""Pinned Nous JWT/endpoint/token policy. AuthStorage owns grants and refresh I/O."""
from __future__ import annotations
import base64, json, logging, os, time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, FrozenSet, Callable
logger = logging.getLogger(__name__)
from urllib.parse import urlparse
import ssl, sys

DEFAULT_NOUS_PORTAL_URL = "https://portal.nousresearch.com"


DEFAULT_NOUS_INFERENCE_URL = "https://inference-api.nousresearch.com/v1"


DEFAULT_NOUS_CLIENT_ID = "hermes-cli"


NOUS_INFERENCE_INVOKE_SCOPE = "inference:invoke"


DEFAULT_NOUS_SCOPE = NOUS_INFERENCE_INVOKE_SCOPE


ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120       # refresh 2 min before expiry


NOUS_INVOKE_JWT_MIN_TTL_SECONDS = ACCESS_TOKEN_REFRESH_SKEW_SECONDS


def _decode_jwt_claims(token: Any) -> Dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        claims = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def _parse_iso_timestamp(value: Any) -> Optional[float]:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_expiring(expires_at_iso: Any, skew_seconds: int) -> bool:
    expires_epoch = _parse_iso_timestamp(expires_at_iso)
    return expires_epoch is None or expires_epoch <= (time.time() + skew_seconds)


def _coerce_ttl_seconds(expires_in: Any) -> int:
    try:
        return max(0, int(expires_in))
    except Exception:
        return 0


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _scope_values(raw_scope: Any) -> set[str]:
    # OAuth token responses return a space-separated string; collections are kept for JWT ``scp``
    # claims and older stored fixtures.
    scopes: set[str] = set()
    if isinstance(raw_scope, str):
        scopes.update(part for part in raw_scope.replace(",", " ").split() if part.strip())
    elif isinstance(raw_scope, (list, tuple, set, frozenset)):
        scopes.update(*(_scope_values(item) for item in raw_scope if isinstance(item, str)))
    return scopes


def _nous_invoke_jwt_status(
    token: Any, *, scope: Any = None, expires_at: Any = None,
    min_ttl_seconds: int = NOUS_INVOKE_JWT_MIN_TTL_SECONDS) -> Optional[str]:
    """Return None when the token can be used for inference, else a reason."""
    claims = _decode_jwt_claims(token)
    if not claims:
        return "access_token_not_jwt"
    scopes = (_scope_values(scope) | _scope_values(claims.get("scope"))
              | _scope_values(claims.get("scp")))
    if NOUS_INFERENCE_INVOKE_SCOPE not in scopes:
        return "missing_inference_invoke_scope"
    exp = claims.get("exp")
    skew = max(0, int(min_ttl_seconds))
    if isinstance(exp, (int, float)):
        return "invoke_jwt_expiring" if float(exp) <= (time.time() + skew) else None
    return "invoke_jwt_expiry_unknown_or_expiring" if _is_expiring(expires_at, skew) else None


def _nous_invoke_jwt_is_usable(
    token: Any, *, scope: Any = None, expires_at: Any = None,
    min_ttl_seconds: int = NOUS_INVOKE_JWT_MIN_TTL_SECONDS) -> bool:
    return _nous_invoke_jwt_status(
        token, scope=scope, expires_at=expires_at, min_ttl_seconds=min_ttl_seconds) is None


def _nous_jwt_expires_at(token: Any, fallback_expires_at: Any = None) -> Optional[str]:
    claims = _decode_jwt_claims(token)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        with suppress(Exception):
            return datetime.fromtimestamp(float(exp), tz=timezone.utc).isoformat()
    return fallback_expires_at if isinstance(fallback_expires_at, str) else None


_ALLOWED_NOUS_INFERENCE_HOSTS: FrozenSet[str] = frozenset({"inference-api.nousresearch.com"})


def _validate_nous_inference_url_from_network(url: Optional[str]) -> Optional[str]:
    """Validate a Portal-returned inference URL against the host allowlist.

    Defense-in-depth: a compromised refresh response (MITM, response injection) could otherwise
    redirect every proxy request — bearing the user's inference JWT — to an attacker endpoint.
    """
    cleaned = url.strip() if isinstance(url, str) else ""
    if not cleaned:
        return None
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return None
    if parsed.scheme != "https":
        logger.warning(
            "nous: refusing non-https inference URL scheme %r from Portal response", parsed.scheme)
        return None
    if parsed.hostname not in _ALLOWED_NOUS_INFERENCE_HOSTS:
        logger.warning(
            "nous: refusing inference URL host %r from Portal response "
            "(not in allowlist); falling back to default",
            parsed.hostname)
        return None
    return cleaned.rstrip("/")


def _apply_nous_refreshed_tokens(
    state: Dict[str, Any], refreshed: Dict[str, Any], refresh_token: str, *,
    inference_base_url: Optional[str] = None) -> None:
    """Write a successful Nous token-refresh payload into *state* (tokens + expiry fields).

    *inference_base_url*, when given, is the healed network-provenance URL to persist alongside
    the rotated tokens (key order in auth.json is preserved from the original login shape).
    """
    now = datetime.now(timezone.utc)
    access_ttl = _coerce_ttl_seconds(refreshed.get("expires_in"))
    state["access_token"] = refreshed["access_token"]
    state["refresh_token"] = refreshed.get("refresh_token") or refresh_token
    state["token_type"] = refreshed.get("token_type") or state.get("token_type") or "Bearer"
    state["scope"] = refreshed.get("scope") or state.get("scope")
    if inference_base_url is not None:
        state["inference_base_url"] = inference_base_url
    state["obtained_at"] = now.isoformat()
    state["expires_in"] = access_ttl
    state["expires_at"] = _iso_after(now, access_ttl)


def _healed_nous_inference_url(refreshed: Dict[str, Any]) -> str:
    """Validated network-provenance inference URL from a refresh payload, healed to the default.

    A Portal URL rejected by the allowlist resets to the production default instead of leaving a
    previously-persisted bad host (e.g. a stale staging URL) in place — otherwise a poisoned
    auth.json re-validates to None on every refresh and silently re-uses the dead endpoint.
    """
    url = _validate_nous_inference_url_from_network(refreshed.get("inference_base_url"))
    return url or DEFAULT_NOUS_INFERENCE_URL


def _iso_after(now: datetime, ttl_seconds: int) -> str:
    """ISO timestamp *ttl_seconds* after *now* (UTC)."""
    return datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc).isoformat()


TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on"})


def is_truthy_value(value: Any, default: bool = False) -> bool:
    """Coerce bool-ish values using the project's shared truthy string set."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY_STRINGS
    return bool(value)


def _default_verify() -> bool | ssl.SSLContext:
    """Platform-aware default SSL verify for httpx clients.

    On macOS with Homebrew Python the system OpenSSL cannot find the system trust store, so pin
    certifi's bundle when importable; elsewhere defer to httpx's built-in default.
    """
    if sys.platform == "darwin":
        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
    return True


def _resolve_verify(
    *, insecure: Optional[bool] = None, ca_bundle: Optional[str] = None,
    auth_state: Optional[Dict[str, Any]] = None) -> bool | ssl.SSLContext:
    tls_state = auth_state.get("tls") if isinstance(auth_state, dict) else {}
    tls_state = tls_state if isinstance(tls_state, dict) else {}
    effective_insecure = (
        is_truthy_value(insecure, default=False) if insecure is not None
        else is_truthy_value(tls_state.get("insecure", False), default=False))
    effective_ca = (
        ca_bundle or tls_state.get("ca_bundle") or os.getenv("HERMES_CA_BUNDLE")
        or os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE"))
    if effective_insecure:
        return False
    if effective_ca:
        ca_path = str(effective_ca)
        if not os.path.isfile(ca_path):
            logger.warning(
                "CA bundle path does not exist: %s — falling back to default certificates",
                ca_path)
            return _default_verify()
        return ssl.create_default_context(cafile=ca_path)
    return _default_verify()


class AuthError(RuntimeError):
    """Structured auth error with UX mapping hints."""

    def __init__(
        self, message: str, *, provider: str = "", code: Optional[str] = None, relogin_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.relogin_required = relogin_required


def _last_auth_error_marker(
    provider: str, error: "AuthError", *, reason: str, default_code: Optional[str] = None,
) -> Dict[str, Any]:
    """The ``last_auth_error`` record persisted when dead OAuth material is quarantined."""
    return {
        "provider": provider, "message": str(error), "reason": reason, "relogin_required": True,
        "code": error.code if default_code is None else (error.code or default_code),
        "at": datetime.now(timezone.utc).isoformat()}


_OAUTH_GRANT_DEAD_CODES = frozenset({"invalid_grant", "invalid_token", "refresh_token_reused"})

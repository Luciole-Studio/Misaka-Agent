"""Pinned ACP child-process secret classification."""

_HERMES_PROVIDER_ENV_FORCE_PREFIX = "_HERMES_FORCE_"


def _is_hermes_internal_secret(key: str) -> bool:
    """True for Hermes-internal secrets injected under *dynamic* names the static
    blocklist cannot enumerate: ``AUXILIARY_<TASK>_API_KEY``/``_BASE_URL`` (per-task
    side-LLM credentials) and ``GATEWAY_RELAY_*_SECRET``/``_KEY``/``_TOKEN`` (relay
    auth; non-secret routing hints stay visible). Stripped on every spawn path
    regardless of env_passthrough registration or ``inherit_credentials``."""
    upper = key.upper()
    if upper.startswith("AUXILIARY_") and upper.endswith(("_API_KEY", "_BASE_URL")):
        return True
    return upper.startswith("GATEWAY_RELAY_") and upper.endswith(("_SECRET", "_KEY", "_TOKEN"))


_ALWAYS_STRIP_KEYS: frozenset[str] = frozenset({
    # GitHub auth
    "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_APP_INSTALLATION_ID",
    # Gateway / messaging bot tokens and access control
    "TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET", "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS",
    # Gateway relay auth triplet. _SECRET/_DELIVERY_KEY are also matched by
    # _is_hermes_internal_secret, but _ID has no secret suffix, so it must be
    # enumerated here to stay stripped on the inherit_credentials=True path.
    "GATEWAY_RELAY_ID", "GATEWAY_RELAY_SECRET", "GATEWAY_RELAY_DELIVERY_KEY",
    "HASS_TOKEN", "EMAIL_PASSWORD", "HERMES_DASHBOARD_SESSION_TOKEN",
    # Remote-compute / infrastructure secrets
    "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "DAYTONA_API_KEY",
})

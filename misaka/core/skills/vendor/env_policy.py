# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / tools/environments/local_env_policy.py; see PROVENANCE.json and LICENSE.

_AWS_SDK_CREDENTIAL_ENV_VARS = frozenset({"AWS_BEARER_TOKEN_BEDROCK"})


_STATIC_PROVIDER_ENV_BLOCKLIST = frozenset({
    "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION", "OPENROUTER_API_KEY", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "LLM_MODEL", "GOOGLE_API_KEY",
    # Path to a GCP service-account JSON, not a bare key, so OPTIONAL_ENV_VARS
    # marks it password=False and the registry loop skips it.
    "VERTEX_CREDENTIALS_PATH", "GOOGLE_APPLICATION_CREDENTIALS", "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY", "PERPLEXITY_API_KEY",
    "COHERE_API_KEY", "FIREWORKS_API_KEY", "XAI_API_KEY", "HELICONE_API_KEY",
    "PARALLEL_API_KEY", "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL",
    "TELEGRAM_HOME_CHANNEL", "TELEGRAM_HOME_CHANNEL_NAME", "DISCORD_HOME_CHANNEL",
    "DISCORD_HOME_CHANNEL_NAME", "DISCORD_REQUIRE_MENTION",
    "DISCORD_FREE_RESPONSE_CHANNELS", "DISCORD_AUTO_THREAD", "SLACK_HOME_CHANNEL",
    "SLACK_HOME_CHANNEL_NAME", "SLACK_ALLOWED_USERS", "WHATSAPP_ENABLED",
    "WHATSAPP_MODE", "WHATSAPP_ALLOWED_USERS", "SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT",
    "SIGNAL_ALLOWED_USERS", "SIGNAL_GROUP_ALLOWED_USERS", "SIGNAL_HOME_CHANNEL",
    "SIGNAL_HOME_CHANNEL_NAME", "SIGNAL_IGNORE_STORIES", "HASS_TOKEN", "HASS_URL",
    "EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST",
    "EMAIL_HOME_ADDRESS", "EMAIL_HOME_ADDRESS_NAME", "HERMES_DASHBOARD_SESSION_TOKEN",
    "GATEWAY_ALLOWED_USERS", "GH_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_APP_INSTALLATION_ID", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET",
    "DAYTONA_API_KEY", "GATEWAY_RELAY_ID", "GATEWAY_RELAY_SECRET",
    "GATEWAY_RELAY_DELIVERY_KEY", "VERCEL_OIDC_TOKEN", "VERCEL_TOKEN",
    "VERCEL_PROJECT_ID", "VERCEL_TEAM_ID",
})


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


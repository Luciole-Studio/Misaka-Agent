"""Environment-based credential discovery for provider auth."""

from __future__ import annotations

import os
from pathlib import Path

_ENV_MAP: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "azure-openai-responses": "AZURE_OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": "GEMINI_API_KEY",
    "google-vertex": "GOOGLE_CLOUD_API_KEY",
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "vercel-ai-gateway": "AI_GATEWAY_API_KEY",
    "zai": "ZAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "moonshotai": "MOONSHOT_API_KEY",
    "moonshotai-cn": "MOONSHOT_API_KEY",
    "huggingface": "HF_TOKEN",
    "fireworks": "FIREWORKS_API_KEY",
    "together": "TOGETHER_API_KEY",
    "opencode": "OPENCODE_API_KEY",
    "opencode-go": "OPENCODE_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
    "cloudflare-workers-ai": "CLOUDFLARE_API_KEY",
    "cloudflare-ai-gateway": "CLOUDFLARE_API_KEY",
    "xiaomi": "XIAOMI_API_KEY",
    "xiaomi-token-plan-cn": "XIAOMI_TOKEN_PLAN_CN_API_KEY",
    "xiaomi-token-plan-ams": "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
    "xiaomi-token-plan-sgp": "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
}

_proc_env_cache: dict[str, str] | None = None
_cached_vertex_adc_credentials_exists: bool | None = None


def _get_proc_env(key: str) -> str | None:
    global _proc_env_cache

    if os.environ:
        return None

    if _proc_env_cache is None:
        _proc_env_cache = {}
        try:
            data = Path("/proc/self/environ").read_text(encoding="utf-8")
            for entry in data.split("\0"):
                index = entry.find("=")
                if index > 0:
                    _proc_env_cache[entry[:index]] = entry[index + 1 :]
        except Exception:
            pass

    return _proc_env_cache.get(key)


def _get_env_value(key: str) -> str | None:
    return os.environ.get(key) or _get_proc_env(key)


def _has_vertex_adc_credentials() -> bool:
    global _cached_vertex_adc_credentials_exists

    if _cached_vertex_adc_credentials_exists is not None:
        return _cached_vertex_adc_credentials_exists

    credentials_path = _get_env_value("GOOGLE_APPLICATION_CREDENTIALS")
    if credentials_path:
        _cached_vertex_adc_credentials_exists = Path(credentials_path).exists()
    else:
        _cached_vertex_adc_credentials_exists = Path.home().joinpath(
            ".config", "gcloud", "application_default_credentials.json"
        ).exists()

    return _cached_vertex_adc_credentials_exists


def _clear_has_vertex_adc_credentials_cache() -> None:
    global _cached_vertex_adc_credentials_exists
    _cached_vertex_adc_credentials_exists = None


_has_vertex_adc_credentials.cache_clear = _clear_has_vertex_adc_credentials_cache  # type: ignore[attr-defined]


def _get_api_key_env_vars(provider: str) -> tuple[str, ...] | None:
    if provider == "github-copilot":
        return ("COPILOT_GITHUB_TOKEN",)

    if provider == "anthropic":
        return ("ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

    env_var = _ENV_MAP.get(provider)
    return (env_var,) if env_var else None


def find_env_keys(provider: str) -> list[str] | None:
    env_vars = _get_api_key_env_vars(provider)
    if not env_vars:
        return None

    found = [env_var for env_var in env_vars if _get_env_value(env_var)]
    return found or None


def get_env_api_key(provider: str) -> str | None:
    env_keys = find_env_keys(provider)
    if env_keys and env_keys[0]:
        return _get_env_value(env_keys[0])

    if provider == "google-vertex":
        has_project = bool(_get_env_value("GOOGLE_CLOUD_PROJECT") or _get_env_value("GCLOUD_PROJECT"))
        has_location = bool(_get_env_value("GOOGLE_CLOUD_LOCATION"))
        if _has_vertex_adc_credentials() and has_project and has_location:
            return "<authenticated>"

    if provider == "amazon-bedrock":
        if (
            _get_env_value("AWS_PROFILE")
            or (_get_env_value("AWS_ACCESS_KEY_ID") and _get_env_value("AWS_SECRET_ACCESS_KEY"))
            or _get_env_value("AWS_BEARER_TOKEN_BEDROCK")
            or _get_env_value("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
            or _get_env_value("AWS_CONTAINER_CREDENTIALS_FULL_URI")
            or _get_env_value("AWS_WEB_IDENTITY_TOKEN_FILE")
        ):
            return "<authenticated>"

    return None


findEnvKeys = find_env_keys
getEnvApiKey = get_env_api_key

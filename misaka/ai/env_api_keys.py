"""Translated from pi's ``packages/ai/src/env-api-keys.ts`` (v1.1.0).

Upstream's Node/browser loader plumbing (``_existsSync``, ``dynamicImport``, the ``node:*`` specifiers and the
"modules have not loaded yet" branch of ``hasVertexAdcCredentials``) keeps ``node:fs`` out of browser builds; Python
always has ``pathlib``.

MISAKA fork: ``envMap`` is the module-level ``_envMap``, because ``misaka/core/skills/runtime.py`` keeps every provider
key variable out of skill environments.
"""

from __future__ import annotations

from pathlib import Path

from misaka.ai.types import ProviderEnv
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.utils.paths import homedir

ANTHROPIC_AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"
ANTHROPIC_OAUTH_TOKEN_ENV = "ANTHROPIC_OAUTH_TOKEN"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_FEDERATION_RULE_ID_ENV = "ANTHROPIC_FEDERATION_RULE_ID"
ANTHROPIC_ORGANIZATION_ID_ENV = "ANTHROPIC_ORGANIZATION_ID"
ANTHROPIC_SERVICE_ACCOUNT_ID_ENV = "ANTHROPIC_SERVICE_ACCOUNT_ID"
ANTHROPIC_IDENTITY_TOKEN_FILE_ENV = "ANTHROPIC_IDENTITY_TOKEN_FILE"
ANTHROPIC_WORKSPACE_ID_ENV = "ANTHROPIC_WORKSPACE_ID"

_cachedVertexAdcCredentialsExists: bool | None = None


def _hasVertexAdcCredentials(env: ProviderEnv | None = None) -> bool:
    global _cachedVertexAdcCredentialsExists

    explicitCredentialsPath = env.get("GOOGLE_APPLICATION_CREDENTIALS") if env else None
    if explicitCredentialsPath:
        return Path(explicitCredentialsPath).exists()

    if _cachedVertexAdcCredentialsExists is None:
        # Check GOOGLE_APPLICATION_CREDENTIALS env var first (standard way)
        gacPath = getProviderEnvValue("GOOGLE_APPLICATION_CREDENTIALS", env)
        if gacPath:
            _cachedVertexAdcCredentialsExists = Path(gacPath).exists()
        else:
            # Fall back to default ADC path (lazy evaluation)
            _cachedVertexAdcCredentialsExists = Path(homedir()).joinpath(
                ".config", "gcloud", "application_default_credentials.json"
            ).exists()
    return _cachedVertexAdcCredentialsExists


_envMap: dict[str, str] = {
    "ant-ling": "ANT_LING_API_KEY",
    "qwen-token-plan": "QWEN_TOKEN_PLAN_API_KEY",
    "qwen-token-plan-cn": "QWEN_TOKEN_PLAN_CN_API_KEY",
    "qwen-token-plan-individual": "QWEN_TOKEN_PLAN_API_KEY",
    "openai": "OPENAI_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": "GEMINI_API_KEY",
    "google-vertex": "GOOGLE_CLOUD_API_KEY",
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "xai": "XAI_API_KEY",
    "typesafe": "TYPESAFE_API_KEY",
    "radius": "RADIUS_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "vercel-ai-gateway": "AI_GATEWAY_API_KEY",
    "zai": "ZAI_API_KEY",
    "zai-coding-cn": "ZAI_CODING_CN_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "moonshotai": "MOONSHOT_API_KEY",
    "moonshotai-cn": "MOONSHOT_API_KEY",
    "huggingface": "HF_TOKEN",
    "fireworks": "FIREWORKS_API_KEY",
    "together": "TOGETHER_API_KEY",
    "baseten": "BASETEN_API_KEY",
    "opencode": "OPENCODE_API_KEY",
    "opencode-go": "OPENCODE_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
    "meta": "META_API_KEY",
    "cloudflare-workers-ai": "CLOUDFLARE_API_KEY",
    "cloudflare-ai-gateway": "CLOUDFLARE_API_KEY",
    "xiaomi": "XIAOMI_API_KEY",
    "xiaomi-token-plan-cn": "XIAOMI_TOKEN_PLAN_CN_API_KEY",
    "xiaomi-token-plan-ams": "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
    "xiaomi-token-plan-sgp": "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
}


def _getApiKeyEnvVars(provider: str) -> list[str] | None:
    if provider == "github-copilot":
        return ["COPILOT_GITHUB_TOKEN"]

    # ANTHROPIC_AUTH_TOKEN participates in env discovery/status, but
    # getEnvApiKey() skips it because requests must pass it as Authorization: Bearer.
    if provider == "anthropic":
        return [ANTHROPIC_AUTH_TOKEN_ENV, ANTHROPIC_OAUTH_TOKEN_ENV, ANTHROPIC_API_KEY_ENV]

    envVar = _envMap.get(provider)
    return [envVar] if envVar else None


def findEnvKeys(provider: str, env: ProviderEnv | None = None) -> list[str] | None:
    """Find configured environment variables that can provide an API key for a provider.

    This only reports actual API key variables. It intentionally excludes ambient
    credential sources such as AWS profiles, AWS IAM credentials, and Google
    Application Default Credentials.
    """
    envVars = _getApiKeyEnvVars(provider)
    if not envVars:
        return None

    found = [envVar for envVar in envVars if getProviderEnvValue(envVar, env)]
    return found if len(found) > 0 else None


def getEnvApiKey(provider: str, env: ProviderEnv | None = None) -> str | None:
    """Get API key for provider from known environment variables, e.g. OPENAI_API_KEY.

    Will not return API keys for providers that require OAuth tokens.
    """
    envKeys = findEnvKeys(provider, env)
    if envKeys and envKeys[0]:
        apiKeyEnv = (
            next((key for key in envKeys if key != ANTHROPIC_AUTH_TOKEN_ENV), None)
            if provider == "anthropic"
            else envKeys[0]
        )
        if apiKeyEnv:
            return getProviderEnvValue(apiKeyEnv, env)

    # Vertex AI supports either an explicit API key or Application Default Credentials.
    # Auth is configured via `gcloud auth application-default login`.
    if provider == "google-vertex":
        hasCredentials = _hasVertexAdcCredentials(env)
        hasProject = bool(getProviderEnvValue("GOOGLE_CLOUD_PROJECT", env) or getProviderEnvValue("GCLOUD_PROJECT", env))
        hasLocation = bool(getProviderEnvValue("GOOGLE_CLOUD_LOCATION", env))

        if hasCredentials and hasProject and hasLocation:
            return "<authenticated>"

    if provider == "amazon-bedrock" and (
        # Amazon Bedrock supports multiple credential sources:
        # 1. AWS_PROFILE - named profile from ~/.aws/credentials
        # 2. AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY - standard IAM keys
        # 3. AWS_BEARER_TOKEN_BEDROCK - Bedrock bearer token
        # 4. AWS_CONTAINER_CREDENTIALS_RELATIVE_URI - ECS task roles
        # 5. AWS_CONTAINER_CREDENTIALS_FULL_URI - ECS task roles (full URI)
        # 6. AWS_WEB_IDENTITY_TOKEN_FILE - IRSA (IAM Roles for Service Accounts)
        getProviderEnvValue("AWS_PROFILE", env)
        or (getProviderEnvValue("AWS_ACCESS_KEY_ID", env) and getProviderEnvValue("AWS_SECRET_ACCESS_KEY", env))
        or getProviderEnvValue("AWS_BEARER_TOKEN_BEDROCK", env)
        or getProviderEnvValue("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", env)
        or getProviderEnvValue("AWS_CONTAINER_CREDENTIALS_FULL_URI", env)
        or getProviderEnvValue("AWS_WEB_IDENTITY_TOKEN_FILE", env)
    ):
        return "<authenticated>"

    return None


__all__ = [
    "ANTHROPIC_API_KEY_ENV",
    "ANTHROPIC_AUTH_TOKEN_ENV",
    "ANTHROPIC_FEDERATION_RULE_ID_ENV",
    "ANTHROPIC_IDENTITY_TOKEN_FILE_ENV",
    "ANTHROPIC_OAUTH_TOKEN_ENV",
    "ANTHROPIC_ORGANIZATION_ID_ENV",
    "ANTHROPIC_SERVICE_ACCOUNT_ID_ENV",
    "ANTHROPIC_WORKSPACE_ID_ENV",
    "findEnvKeys",
    "getEnvApiKey",
]

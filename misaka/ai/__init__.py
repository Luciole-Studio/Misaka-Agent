"""Translated from pi's ``packages/ai/src/index.ts`` (v1.3.0).

Core only, side-effect free: no generated catalogs, no provider factories,
no api-registry, no OAuth implementations, no compat. Provider factories
live under ``misaka.ai.providers.*``, API implementations under
``misaka.ai.api.*``, the old global API under ``misaka.ai.compat``.

MISAKA fork: every export is resolved on first use (PEP 562), because importing any ``misaka.ai`` submodule runs this
package first: an option type would load its vendor SDK, and the extension OAuth types would run ``misaka.ai.compat``
(TypeScript erases ``export type``). The ``typebox`` re-exports (``Static``, ``TSchema``, ``Type``) have no Python
counterpart: tool schemas are plain JSON-schema mappings.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

# `export type { ... } from "./api/<module>.ts"`.
_OPTION_TYPES: dict[str, str] = {
    "AnthropicEffort": "api.anthropic_messages",
    "AnthropicOptions": "api.anthropic_messages",
    "AnthropicThinkingDisplay": "api.anthropic_messages",
    "AzureOpenAIResponsesOptions": "api.azure_openai_responses",
    "BedrockOptions": "api.bedrock_converse_stream",
    "BedrockThinkingDisplay": "api.bedrock_converse_stream",
    "GoogleOptions": "api.google_generative_ai",
    "GoogleApiThinkingLevel": "api.google_shared",
    "ResolvedGoogleThinkingLevel": "api.google_shared",
    "GoogleVertexOptions": "api.google_vertex",
    "MistralOptions": "api.mistral_conversations",
    "OpenAICodexResponsesOptions": "api.openai_codex_responses",
    "OpenAICodexWebSocketDebugStats": "api.openai_codex_responses",
    "OpenAICompletionsOptions": "api.openai_completions",
    "OpenAIResponsesOptions": "api.openai_responses",
    "PiMessagesEvent": "api.pi_messages",
    "PiMessagesOptions": "api.pi_messages",
    "PiMessagesRewriteImpact": "api.pi_messages",
    # `export type { ... } from "./compat/extension-oauth-types.ts"`.
    "OAuthAuthInfo": "compat.extension_oauth_types",
    "OAuthDeviceCodeInfo": "compat.extension_oauth_types",
    "OAuthLoginCallbacks": "compat.extension_oauth_types",
    "OAuthPrompt": "compat.extension_oauth_types",
    "OAuthSelectOption": "compat.extension_oauth_types",
    "OAuthSelectPrompt": "compat.extension_oauth_types",
    # `export { contentText, getSystemMessageText, renderSystemMessageUpdate } from "./utils/text.ts"`.
    "contentText": "utils.text",
    "getSystemMessageText": "utils.text",
    "renderSystemMessageUpdate": "utils.text",
    # `export { uuidv7 } from "./utils/uuid.ts"`.
    "uuidv7": "utils.uuid",
}

# `export * from "./<module>.ts"`, in upstream order.
_STAR_MODULES: tuple[str, ...] = (
    "api.lazy",
    "auth.context",
    "auth.credential_store",
    "auth.helpers",
    "auth.types",
    "models",
    "models_store",
    "providers.faux",
    "session_resources",
    "types",
    "utils.diagnostics",
    "utils.event_stream",
    "utils.json_parse",
    "utils.overflow",
    "utils.retry",
    "utils.transcript",
    "utils.typebox_helpers",
    "utils.validation",
)

_exports: dict[str, str] | None = None


def _exportTable() -> dict[str, str]:
    global _exports
    if _exports is None:
        table: dict[str, str] = {}
        for module in _STAR_MODULES:
            for name in import_module(f"{__name__}.{module}").__all__:
                table.setdefault(name, module)
        table.update(_OPTION_TYPES)
        _exports = table
    return _exports


def __getattr__(name: str) -> Any:
    if name == "__all__":
        return sorted(_exportTable())
    module = _OPTION_TYPES.get(name) or _exportTable().get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_exportTable()})

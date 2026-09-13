"""Public exports for the harn AI package."""

from misaka.ai.api_registry import *
from misaka.ai.env_api_keys import *
from misaka.ai.image_models import *
from misaka.ai.images import *
from misaka.ai.images_api_registry import *
from misaka.ai.models import *
from misaka.ai.providers.images.register_builtins import *
from misaka.ai.providers.register_builtins import *
from misaka.ai.session_resources import *
from misaka.ai.stream import *
from misaka.ai.types import *
from misaka.ai.utils.diagnostics import *
from misaka.ai.utils.event_stream import *
from misaka.ai.utils.json_parse import *
from misaka.ai.utils.oauth.types import (  # noqa: F401
    OAuthAuthInfo,
    OAuthCredentials,
    OAuthDeviceCodeInfo,
    OAuthLoginCallbacks,
    OAuthPrompt,
    OAuthProvider,
    OAuthProviderId,
    OAuthProviderInfo,
    OAuthProviderInterface,
    OAuthSelectOption,
    OAuthSelectPrompt,
)
from misaka.ai.utils.overflow import *
from misaka.ai.utils.typebox_helpers import *
from misaka.ai.utils.validation import *

# Importing any AI submodule executes this package first. These option re-exports
# must not undo register_builtins' lazy loading by importing every vendor SDK.
_OPTION_MODULES = {
    "BedrockOptions": "amazon_bedrock",
    "BedrockThinkingDisplay": "amazon_bedrock",
    "AnthropicEffort": "anthropic",
    "AnthropicOptions": "anthropic",
    "AnthropicThinkingDisplay": "anthropic",
    "AzureOpenAIResponsesOptions": "azure_openai_responses",
    "GoogleOptions": "google",
    "GoogleThinkingLevel": "google_shared",
    "GoogleVertexOptions": "google_vertex",
    "MistralOptions": "mistral",
    "OpenAICodexResponsesOptions": "openai_codex_responses",
    "OpenAICompletionsOptions": "openai_completions",
    "OpenAIResponsesOptions": "openai_responses",
}


def __getattr__(name):
    module = _OPTION_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f"{__name__}.providers.{module}"), name)
    globals()[name] = value
    return value


__all__ = [name for name in globals() if not name.startswith("_")] + list(_OPTION_MODULES)

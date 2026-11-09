"""Translated from pi's ``packages/ai/src/legacy-api-aliases.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.anthropic_messages_lazy import anthropicMessagesApi
from misaka.ai.api.azure_openai_responses_lazy import azureOpenAIResponsesApi
from misaka.ai.api.google_generative_ai_lazy import googleGenerativeAIApi
from misaka.ai.api.google_vertex_lazy import googleVertexApi
from misaka.ai.api.mistral_conversations_lazy import mistralConversationsApi
from misaka.ai.api.openai_codex_responses_lazy import openAICodexResponsesApi
from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.api.openai_responses_lazy import openAIResponsesApi

_anthropicMessagesStreams = anthropicMessagesApi()
_azureOpenAIResponsesStreams = azureOpenAIResponsesApi()
_googleGenerativeAIStreams = googleGenerativeAIApi()
_googleVertexStreams = googleVertexApi()
_mistralConversationsStreams = mistralConversationsApi()
_openAICodexResponsesStreams = openAICodexResponsesApi()
_openAICompletionsStreams = openAICompletionsApi()
_openAIResponsesStreams = openAIResponsesApi()

# @deprecated Use `stream` from `@earendil-works/pi-ai/api/anthropic-messages` or `anthropicMessagesApi().stream`.
streamAnthropic = _anthropicMessagesStreams.stream
# @deprecated Use `streamSimple` from `@earendil-works/pi-ai/api/anthropic-messages` or `anthropicMessagesApi().streamSimple`.
streamSimpleAnthropic = _anthropicMessagesStreams.streamSimple

# @deprecated Use `stream` from `@earendil-works/pi-ai/api/azure-openai-responses` or `azureOpenAIResponsesApi().stream`.
streamAzureOpenAIResponses = _azureOpenAIResponsesStreams.stream
# @deprecated Use `streamSimple` from `@earendil-works/pi-ai/api/azure-openai-responses` or `azureOpenAIResponsesApi().streamSimple`.
streamSimpleAzureOpenAIResponses = _azureOpenAIResponsesStreams.streamSimple

# @deprecated Use `stream` from `@earendil-works/pi-ai/api/google-generative-ai` or `googleGenerativeAIApi().stream`.
streamGoogle = _googleGenerativeAIStreams.stream
# @deprecated Use `streamSimple` from `@earendil-works/pi-ai/api/google-generative-ai` or `googleGenerativeAIApi().streamSimple`.
streamSimpleGoogle = _googleGenerativeAIStreams.streamSimple

# @deprecated Use `stream` from `@earendil-works/pi-ai/api/google-vertex` or `googleVertexApi().stream`.
streamGoogleVertex = _googleVertexStreams.stream
# @deprecated Use `streamSimple` from `@earendil-works/pi-ai/api/google-vertex` or `googleVertexApi().streamSimple`.
streamSimpleGoogleVertex = _googleVertexStreams.streamSimple

# @deprecated Use `stream` from `@earendil-works/pi-ai/api/mistral-conversations` or `mistralConversationsApi().stream`.
streamMistral = _mistralConversationsStreams.stream
# @deprecated Use `streamSimple` from `@earendil-works/pi-ai/api/mistral-conversations` or `mistralConversationsApi().streamSimple`.
streamSimpleMistral = _mistralConversationsStreams.streamSimple

# @deprecated Use `stream` from `@earendil-works/pi-ai/api/openai-codex-responses` or `openAICodexResponsesApi().stream`.
streamOpenAICodexResponses = _openAICodexResponsesStreams.stream
# @deprecated Use `streamSimple` from `@earendil-works/pi-ai/api/openai-codex-responses` or `openAICodexResponsesApi().streamSimple`.
streamSimpleOpenAICodexResponses = _openAICodexResponsesStreams.streamSimple

# @deprecated Use `stream` from `@earendil-works/pi-ai/api/openai-completions` or `openAICompletionsApi().stream`.
streamOpenAICompletions = _openAICompletionsStreams.stream
# @deprecated Use `streamSimple` from `@earendil-works/pi-ai/api/openai-completions` or `openAICompletionsApi().streamSimple`.
streamSimpleOpenAICompletions = _openAICompletionsStreams.streamSimple

# @deprecated Use `stream` from `@earendil-works/pi-ai/api/openai-responses` or `openAIResponsesApi().stream`.
streamOpenAIResponses = _openAIResponsesStreams.stream
# @deprecated Use `streamSimple` from `@earendil-works/pi-ai/api/openai-responses` or `openAIResponsesApi().streamSimple`.
streamSimpleOpenAIResponses = _openAIResponsesStreams.streamSimple

__all__ = [
    "streamAnthropic",
    "streamAzureOpenAIResponses",
    "streamGoogle",
    "streamGoogleVertex",
    "streamMistral",
    "streamOpenAICodexResponses",
    "streamOpenAICompletions",
    "streamOpenAIResponses",
    "streamSimpleAnthropic",
    "streamSimpleAzureOpenAIResponses",
    "streamSimpleGoogle",
    "streamSimpleGoogleVertex",
    "streamSimpleMistral",
    "streamSimpleOpenAICodexResponses",
    "streamSimpleOpenAICompletions",
    "streamSimpleOpenAIResponses",
]

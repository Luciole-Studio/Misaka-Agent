"""Translated from pi's ``packages/ai/src/auth/oauth/load.ts`` (v1.1.0).

Loads an OAuth flow module through a variable specifier so bundlers cannot
follow the import into Node-only flow code (`node:http` callback servers,
`node:crypto` PKCE). The `.ts`/`.js` rewrite keeps the trick working from
both source and built output.

MISAKA fork: ``importlib.import_module`` stands in for the dynamic ``import()``; Python has no bundle, so there is no
extension to rewrite.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypedDict

from misaka.ai.auth.types import OAuthAuth

if TYPE_CHECKING:
    from misaka.ai.auth.oauth.radius import RadiusOAuthOptions


def _importOAuthModule(specifier: str) -> Any:
    return importlib.import_module(f"misaka.ai.auth.oauth.{specifier}")


class _OAuthFlowLoaders(TypedDict):
    anthropic: Callable[[], OAuthAuth | Awaitable[OAuthAuth]]
    openaiCodex: Callable[[], OAuthAuth | Awaitable[OAuthAuth]]
    openaiChatGPT: Callable[[], OAuthAuth | Awaitable[OAuthAuth]]
    githubCopilot: Callable[[], OAuthAuth | Awaitable[OAuthAuth]]
    openrouter: Callable[[], OAuthAuth | Awaitable[OAuthAuth]]
    kimiCoding: Callable[[], OAuthAuth | Awaitable[OAuthAuth]]
    meta: Callable[[], OAuthAuth | Awaitable[OAuthAuth]]
    xai: Callable[[], OAuthAuth | Awaitable[OAuthAuth]]
    radius: Callable[[RadiusOAuthOptions], OAuthAuth | Awaitable[OAuthAuth]]


_bundledLoaders: _OAuthFlowLoaders | None = None


def registerBundledOAuthFlowLoaders(loaders: _OAuthFlowLoaders) -> None:
    """Registers statically bundled OAuth flows for standalone Bun binaries."""
    global _bundledLoaders
    _bundledLoaders = loaders


async def _settle(value: OAuthAuth | Awaitable[OAuthAuth]) -> OAuthAuth:
    """`return loader()` from an async function: a promise it returns is awaited."""
    return value if isinstance(value, OAuthAuth) else await value


async def loadAnthropicOAuth() -> OAuthAuth:
    if _bundledLoaders:
        return await _settle(_bundledLoaders["anthropic"]())
    return _importOAuthModule("anthropic").anthropicOAuth


async def loadOpenAICodexOAuth() -> OAuthAuth:
    if _bundledLoaders:
        return await _settle(_bundledLoaders["openaiCodex"]())
    return _importOAuthModule("openai_codex").openaiCodexOAuth


async def loadOpenAIChatGPTOAuth() -> OAuthAuth:
    if _bundledLoaders:
        return await _settle(_bundledLoaders["openaiChatGPT"]())
    return _importOAuthModule("openai_chatgpt").openaiChatGPTOAuth


async def loadGitHubCopilotOAuth() -> OAuthAuth:
    if _bundledLoaders:
        return await _settle(_bundledLoaders["githubCopilot"]())
    return _importOAuthModule("github_copilot").githubCopilotOAuth


async def loadOpenRouterOAuth() -> OAuthAuth:
    if _bundledLoaders:
        return await _settle(_bundledLoaders["openrouter"]())
    return _importOAuthModule("openrouter").openRouterOAuth


async def loadKimiCodingOAuth() -> OAuthAuth:
    if _bundledLoaders:
        return await _settle(_bundledLoaders["kimiCoding"]())
    return _importOAuthModule("kimi_coding").kimiCodingOAuth


async def loadMetaOAuth() -> OAuthAuth:
    if _bundledLoaders:
        return await _settle(_bundledLoaders["meta"]())
    return _importOAuthModule("meta").metaOAuth


async def loadXaiOAuth() -> OAuthAuth:
    if _bundledLoaders:
        return await _settle(_bundledLoaders["xai"]())
    return _importOAuthModule("xai").xaiOAuth


async def loadRadiusOAuth(options: RadiusOAuthOptions) -> OAuthAuth:
    if _bundledLoaders:
        return await _settle(_bundledLoaders["radius"](options))
    return _importOAuthModule("radius").createRadiusOAuth(options)


__all__ = [
    "loadAnthropicOAuth",
    "loadGitHubCopilotOAuth",
    "loadKimiCodingOAuth",
    "loadMetaOAuth",
    "loadOpenAIChatGPTOAuth",
    "loadOpenAICodexOAuth",
    "loadOpenRouterOAuth",
    "loadRadiusOAuth",
    "loadXaiOAuth",
    "registerBundledOAuthFlowLoaders",
]

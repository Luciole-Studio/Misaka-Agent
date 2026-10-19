"""Hand the reading engine the request parameters MISAKA already resolved.

The Node side speaks the pi coding-agent SDK, whose ``ModelRuntime`` knows how to *use* a
provider but not which provider this profile is entitled to. The Python side does: the same
``ModelRegistry`` the TUI uses, resolved through the same ``getApiKeyAndHeaders`` call, so a
model that works in ``misaka chat`` works here too.

What crosses the process boundary is a plain dict -- provider, model id, api id, base URL,
headers, key. Never a provider object and never a callable: neither means anything to Node,
and serialising a callable loses the method rather than the object.

The key is passed in memory only. Nothing here logs it, returns it for display, or writes it
to disk; :func:`safe_summary` is the only rendering and it reports presence, not values.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

#: Explicit handover for the site proxy, when the profile has no model of its own configured.
ENV_MODEL = "MISAKA_READING_MODEL"
ENV_BASE_URL = "MISAKA_READING_BASE_URL"
ENV_API_KEY = "MISAKA_READING_API_KEY"
ENV_API = "MISAKA_READING_API"

#: The site proxy serves one text model behind an Anthropic-shaped environment. Its model id
#: is not a Claude id, so it cannot be inferred from the variable names alone.
DEFAULT_SITE_MODEL = "deepseek-flash"
DEFAULT_SITE_API = "openai-completions"
#: Room for one reading turn. The site model thinks before it writes: at the SDK's default of
#: 8k output tokens a long reconstruction can spend the entire budget on reasoning and return
#: no text at all, which is not a reading failure the reader caused. The proxy accepts this.
DEFAULT_SITE_MAX_TOKENS = 32_000


class ModelHandoverError(RuntimeError):
    """No model could be resolved, or the resolved one cannot be authenticated."""


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def with_api_version(base_url: str | None, api: str) -> str | None:
    """Return ``base_url`` as the SDK's OpenAI client expects it.

    That client appends ``/chat/completions`` to the base URL it is given; it does not add
    ``/v1``. MISAKA stores provider roots without the version segment, so supply it here.
    Already-versioned URLs are left alone, as are non-OpenAI APIs.
    """
    if not base_url:
        return base_url
    parts = urlsplit(base_url.strip())
    path = parts.path.rstrip("/")
    if api == "openai-completions" and not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _handover(
    *,
    provider: str,
    model_id: str,
    api: str,
    base_url: str | None = None,
    headers: dict[str, str] | None = None,
    api_key: str | None = None,
    context_window: int | None = None,
    max_tokens: int | None = None,
    reasoning: bool = False,
) -> dict[str, Any]:
    handover: dict[str, Any] = {
        "provider": provider,
        "id": model_id,
        "api": api,
        "reasoning": bool(reasoning),
    }
    base = with_api_version(base_url, api)
    if base:
        handover["baseUrl"] = base
    if headers:
        handover["headers"] = {str(k): str(v) for k, v in headers.items()}
    if api_key:
        handover["apiKey"] = api_key
    if context_window:
        handover["contextWindow"] = int(context_window)
    if max_tokens:
        handover["maxTokens"] = int(max_tokens)
    return handover


def _site_handover() -> dict[str, Any] | None:
    """The proxy this session is already pointed at, if the environment names one."""
    base = _env(ENV_BASE_URL, "ANTHROPIC_BASE_URL")
    key = _env(ENV_API_KEY, "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
    if not base or not key:
        return None
    model_id = _env(ENV_MODEL) or DEFAULT_SITE_MODEL
    api = _env(ENV_API) or DEFAULT_SITE_API
    return _handover(
        provider="site",
        model_id=model_id,
        api=api,
        base_url=base,
        api_key=key,
        max_tokens=DEFAULT_SITE_MAX_TOKENS,
    )


async def _registry_handover(reference: str, registry: Any) -> dict[str, Any]:
    from misaka.core.model_resolver import findExactModelReferenceMatch

    model = findExactModelReferenceMatch(reference, registry.getAll())
    if model is None:
        raise ModelHandoverError(
            f'No model matches "{reference}". Name one as provider/id, e.g. anthropic/claude-sonnet-5-5.'
        )
    resolved = await registry.getApiKeyAndHeaders(model)
    if not resolved.get("ok"):
        raise ModelHandoverError(
            f'{model.provider}/{model.id} cannot be authenticated: {resolved.get("error") or "unknown error"}'
        )
    return _handover(
        provider=model.provider,
        model_id=model.id,
        api=str(model.api),
        base_url=resolved.get("baseUrl") or model.baseUrl,
        headers={**(model.headers or {}), **(resolved.get("headers") or {})},
        api_key=resolved.get("apiKey"),
        context_window=model.contextWindow,
        max_tokens=model.maxTokens,
        reasoning=bool(model.reasoning),
    )


async def handover_for_model(model: Any, registry: Any) -> dict[str, Any]:
    """Request parameters for the model the TUI already selected.

    ``/read`` should read with the model the user is chatting to, resolved by the same
    registry call the chat's provider makes, not a second independent choice.
    """
    resolved = await registry.getApiKeyAndHeaders(model)
    if not resolved.get("ok"):
        raise ModelHandoverError(
            f'{model.provider}/{model.id} cannot be authenticated: {resolved.get("error") or "unknown error"}'
        )
    return _handover(
        provider=model.provider,
        model_id=model.id,
        api=str(model.api),
        base_url=resolved.get("baseUrl") or model.baseUrl,
        headers={**(model.headers or {}), **(resolved.get("headers") or {})},
        api_key=resolved.get("apiKey"),
        context_window=model.contextWindow,
        max_tokens=model.maxTokens,
        reasoning=bool(model.reasoning),
    )


async def resolve_handover(
    reference: str | None = None,
    *,
    profile_dir: str | None = None,
) -> dict[str, Any]:
    """Resolve the request parameters for this run.

    Order: an explicit ``reference`` (``--model``), then the profile's pinned model, then the
    site proxy named by the environment. Every failure is an error, never a silent fallback
    to some other model: a reading written by an unintended model is worse than no reading.
    """
    from misaka.config import profiles

    registry = None

    def _registry() -> Any:
        nonlocal registry
        if registry is None:
            from misaka.core.auth_storage import AuthStorage
            from misaka.core.model_registry import ModelRegistry

            registry = ModelRegistry.create(AuthStorage.create())
        return registry

    if reference:
        return await _registry_handover(reference, _registry())

    if profile_dir:
        pinned = profiles.pinned_model(profile_dir, strict=False)
        if pinned:
            try:
                return await _registry_handover(str(pinned), _registry())
            except ModelHandoverError:
                # A pin we cannot authenticate should not beat a working site proxy; the
                # caller still sees which model was actually used.
                pass

    site = _site_handover()
    if site:
        return site

    raise ModelHandoverError(
        "No text model is available for reading. Pass --model <provider>/<id>, pin one for "
        "the profile, or set MISAKA_READING_BASE_URL and MISAKA_READING_API_KEY."
    )


def safe_summary(handover: dict[str, Any]) -> str:
    """A loggable label: provider/id plus where the parameters came from. No key material."""
    auth = "handed over" if handover.get("apiKey") else "no key"
    base = handover.get("baseUrl")
    return f"{handover.get('provider')}/{handover.get('id')} ({handover.get('api')}, {auth}{', ' + base if base else ''})"

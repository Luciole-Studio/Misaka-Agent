"""Hermes auxiliary completions on MISAKA's native model/provider API.

No agent session, prompt flattening, or turn-budget shim: max_tokens limits output.
The calling session supplies its registry, including custom providers and OAuth.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import re
import sys
import time
import uuid
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

from misaka.config import current_config
from misaka.utils.async_lifecycle import run_in_thread
from misaka.utils.values import read_field

from . import config_bridge, execution

_REGISTRY = contextvars.ContextVar("lcm_model_registry", default=None)
_MODEL = contextvars.ContextVar("lcm_model", default=None)
_SESSION = contextvars.ContextVar("lcm_auxiliary_session", default=None)
_CWD = contextvars.ContextVar('lcm_auxiliary_cwd', default=None)
logger = logging.getLogger(__name__)


@contextmanager
def runtime(ctx):
    from .ingest import session_id

    registry_token = _REGISTRY.set(read_field(ctx, "modelRegistry"))
    model_token = _MODEL.set(read_field(ctx, "model"))
    session_token = _SESSION.set(session_id(ctx) or None)
    cwd_token = _CWD.set(read_field(ctx, 'cwd'))
    try:
        yield
    finally:
        _SESSION.reset(session_token)
        _CWD.reset(cwd_token)
        _MODEL.reset(model_token)
        _REGISTRY.reset(registry_token)


def usage_dict(usage) -> dict:
    """Preserve the host's separate input, output and cache counters."""
    inp, out, read, write = (int(read_field(usage, key, 0) or 0)
                             for key in ("input", "output", "cacheRead", "cacheWrite"))
    prompt = inp + read + write
    return {"prompt_tokens": prompt, "input_tokens": inp, "completion_tokens": out,
            "output_tokens": out, "cache_read_tokens": read, "cache_write_tokens": write,
            "total_tokens": int(read_field(usage, "totalTokens", 0) or 0) or prompt + out}


def get_provider(name):
    """Native provider identity for Hermes' provider-plus-endpoint rule."""
    from misaka.ai.models import get_providers

    registry = _REGISTRY.get()
    from .profiles import get_provider_profile
    profile = get_provider_profile(name)
    if profile is not None and profile.auth_type == 'external_process':
        return profile.name
    names = {item.provider for item in registry.getAll()} if registry is not None else get_providers()
    if registry is not None:
        from .profiles import native_provider
        name = native_provider(name, registry)
    return name if name in names else None


def _expand_direct_api_alias(provider, base_url):
    # Hermes models OpenAI's direct API as custom. MISAKA already registers it
    # as a first-class provider, with its own auth/transport. Keep that identity.
    return provider, base_url


def _unwrap_moa_provider(provider, model):
    # No Hermes MoA preset owner exists here. Never send its virtual endpoint or
    # preset name to the real main model as an accidental fallback.
    raise ValueError("Hermes MoA presets are not native auxiliary routes; select a registered provider/model")


def _model_route(model, registry):
    from misaka.ai.models import get_providers

    from ..vendor.model_routing import parse_lcm_model_override

    custom = {item.provider for item in registry.getAll()} - set(get_providers())
    return parse_lcm_model_override(model, provider_resolver=lambda name: name == "cerebras" or name in custom)


def _route(model: str, provider: str, registry, main_runtime, *, defer_nous=False) -> tuple[str, str]:
    # Hermes' "auto" means inherit; it is never a model ID on the wire.
    model = "" if model.lower() == "auto" else model
    provider = "" if provider.lower() == "auto" else provider
    route = _model_route(model, registry)
    selected = _MODEL.get()
    from .profiles import native_provider
    explicit_provider = native_provider(provider or route.provider, registry) if provider or route.provider else ''
    provider = explicit_provider or main_runtime.get("provider") or read_field(selected, "provider")
    model = route.model
    if not model and explicit_provider:
        if provider == 'nous' and defer_nous:
            return provider, ''  # tier-dependent: choose only after resolving the issuing account
        from ..native.auxiliary_policy import _API_KEY_PROVIDER_AUX_MODELS_FALLBACK
        from .profiles import get_provider_profile
        profile = get_provider_profile(provider)
        candidates = [item for item in registry.getAll() if item.provider == provider]
        if profile is not None and profile.auth_type == 'external_process':
            model = main_runtime.get('model') or read_field(selected, 'id')
            if not model:
                raise ValueError(f'Select an auxiliary model for {provider}')
            return provider, model
        model = ((profile.resolve_aux_model() or profile.default_aux_model) if profile is not None else '') or _API_KEY_PROVIDER_AUX_MODELS_FALLBACK.get(provider)
        if not model:
            if len(candidates) != 1:
                raise ValueError(f"Select an auxiliary model for provider {provider!r} in settings.json")
            model = candidates[0].id
    model = model or main_runtime.get("model") or read_field(selected, "id")
    if not provider or not model:
        cfg = current_config()
        provider, model = provider or cfg["provider"], model or cfg["default_model"]
    return provider, model


def resolve_model(provider, model, registry):
    from misaka.ai.types import Model
    from misaka.core.model_resolver import resolveCliModel

    from .profiles import get_provider_profile
    profile = get_provider_profile(provider)
    if profile is not None and profile.auth_type == 'external_process':
        # ACP selects the model in session/set_config_option, not in an HTTP
        # catalog. These local request defaults never replace the main model.
        return Model(id=model, name=model, provider=provider, api='openai-completions',
            baseUrl=profile.base_url, reasoning=False, input=['text'], contextWindow=128000,
            maxTokens=4096, cost={'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0})
    if not model:
        # Auth needs a native provider route, but a deferred Nous recommendation
        # must never send this temporary catalog identity to the model transport.
        candidate = next((item for item in registry.getAll() if item.provider == provider), None)
        if candidate is None:
            raise RuntimeError(f'No native model registered for {provider}')
        return candidate
    resolved = resolveCliModel({'cliProvider': provider, 'cliModel': model, 'modelRegistry': registry})
    if resolved.model is None:
        raise RuntimeError(resolved.error or f'Unknown auxiliary model: {provider}/{model}')
    return resolved.model


class _UnavailableClient(RuntimeError):
    """Native route/auth establishment failed before any model request."""


async def _complete(messages, model, provider, temperature, max_tokens, timeout, *, task,
                    main_runtime, base_url, api_key, api_mode, route_info, latency_info, started,
                    extra_body=None, reasoning_config=None, extra_headers=None, recovery=True):
    from misaka.ai.auth.resolve import AuthResolutionOverrides
    from misaka.ai.session_resources import cleanup_session_resources
    from misaka.ai.types import Context, SimpleStreamOptions, UserMessage
    from misaka.ai.utils.abort import (
        AbortController,
        combine_abort_signals,
        wait_for_abort,
    )
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_registry import ModelRegistry

    from ..native import auxiliary_options, auxiliary_policy
    from . import routing

    execution.check_cancelled()
    registry = _REGISTRY.get() or ModelRegistry.create(AuthStorage.create())
    system, turns = [], []
    for message in messages or []:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role == "system":
            if not isinstance(content, str):
                raise TypeError("Auxiliary system content must be text")
            system.append(content)
        elif role == "user":
            turns.append(UserMessage(content=content, timestamp=int(time.time() * 1000)))
        else:
            raise ValueError(f"Unexpected LCM auxiliary message role: {role}")
    context = Context(systemPrompt="\n\n".join(system), messages=turns, tools=[])
    from .auxiliary import provider_error, recover
    task_body = auxiliary_options._get_task_extra_body(task)
    task_body.update(extra_body or {})
    reasoning = reasoning_config if reasoning_config is not None else task_body.pop('reasoning', None)
    level = None
    if isinstance(reasoning, dict) and reasoning.get('enabled') is not False:
        level = reasoning.get('effort') or 'medium'

    async def call_candidate(candidate, candidate_model, label):
        deadline = candidate.timeout or auxiliary_options._fallback_entry_timeout(task, label) or timeout
        return await _complete(messages, candidate_model, candidate.provider, temperature,
            max_tokens, deadline, task=task, main_runtime=main_runtime,
            base_url=candidate.base_url, api_key=candidate.api_key, api_mode=candidate.api_mode,
            route_info=route_info, latency_info=latency_info, started=started,
            extra_body=task_body, reasoning_config=reasoning, extra_headers=extra_headers,
            recovery=False)

    selected = None

    async def unavailable(error):
        from ..native.auxiliary_fallback import _try_configured_fallback_chain
        execution.check_cancelled()
        # Original unavailable-client rule: only the explicit per-task chain.
        # Runtime 401s still follow the separate explicit-provider recovery gate.
        if recovery and task and route_provider not in {'auto', '', None}:
            with routing.scope(registry, main_runtime, provider,
                               base_url or read_field(selected, 'baseUrl', ''), {'apiKey': api_key}) as route_state:
                while True:
                    candidate, candidate_model, label = _try_configured_fallback_chain(
                        task, route_provider, reason='provider unavailable')
                    if candidate is None:
                        break
                    try:
                        return await call_candidate(candidate, candidate_model, label)
                    except _UnavailableClient:
                        # MISAKA resolves async auth at dispatch, not in the sync
                        # selector. A failed constructor is skipped for this walk.
                        route_state.unavailable.add(candidate.selection_key)
        if recovery:
            raise error
        raise _UnavailableClient(str(error)) from error

    # A plugin model prefix is part of the explicit call, not a lower-priority
    # default. Resolve it before the task config can select a different account.
    override = _model_route(model, registry)
    if recovery:
        provider, model, base_url, api_key, configured_mode = auxiliary_policy._resolve_task_provider_model(
            task, provider or override.provider, override.model or None, base_url, api_key)
    else:
        # A fallback is already a resolved destination. Its absent key/endpoint
        # means its OWN registry defaults, not the failed task's credentials.
        provider, model, configured_mode = provider or override.provider, override.model or None, api_mode
    if provider == "custom":
        config = auxiliary_policy._get_auxiliary_task_config(task)
        named = str(config.get("provider") or "")
        candidates = [item for item in registry.getAll() if (not model or item.id == model)
                      and (item.provider == named if named not in {"", "auto", "custom"}
                           else item.baseUrl.rstrip("/") == str(base_url).rstrip("/"))]
        names = {item.provider for item in candidates}
        if len(names) != 1:
            raise ValueError("Custom auxiliary endpoint must identify a registered native provider/model")
        provider = names.pop()
    route_provider = provider or "auto"
    api_mode = api_mode or configured_mode
    model = str(model or "")
    try:
        provider, model = _route(model, provider, registry, main_runtime, defer_nous=True)
        if route_provider != 'auto':
            route_provider = provider
        selected = resolve_model(provider, model, registry)
    except RuntimeError as error:
        return await unavailable(error)
    from .profiles import get_provider_profile
    profile = get_provider_profile(provider)
    external = profile is not None and profile.auth_type == 'external_process'
    # A main-model snapshot is route-scoped, not a credential pool for a pinned
    # fallback model. Registered per-model endpoint/transport metadata still wins
    # when the auxiliary model differs from the main model.
    if model and (selected.provider, selected.id) == (main_runtime.get("provider"), main_runtime.get("model")):
        base_url = base_url or main_runtime.get("base_url")
        api_mode = api_mode or main_runtime.get("api_mode")
        if not base_url or base_url == main_runtime.get("base_url"):
            api_key = api_key or main_runtime.get("api_key")
    controller = AbortController()
    signal = combine_abort_signals(controller, execution.current_signal())
    request = asyncio.current_task()

    async def abort_request():
        await wait_for_abort(execution.current_signal())
        controller.abort()
        request.cancel()

    aborting = asyncio.create_task(abort_request()) if execution.current_signal() is not None else None
    try:
        # Prepare once, outside recovery, as Hermes does. A transport retry must
        # not repeat auth lookup or borrow credentials from a concurrent /model.
        auth_started = time.monotonic()
        from .pool import Pool
        pool = None if external else Pool.configured(registry, selected.provider)
        pooled_entry = None

        def issuing_credential():
            stored = (registry.authStorage.get(pooled_entry.id if pooled_entry else selected.provider)
                      if not api_key and hasattr(registry, 'authStorage') else None)
            # The wire key is authoritative, even when a default login exists.
            return {**(stored or {}), 'access': auth.get('apiKey') or (stored or {}).get('access', '')}

        try:
            async with asyncio.timeout(timeout):
                if external:
                    auth = {'ok': True, 'apiKey': selected.provider}
                elif pool is not None and not api_key:
                    pooled_entry, auth = await pool.acquire(selected, signal=signal)
                elif (not api_key and hasattr(registry, 'isUsingOAuth') and registry.isUsingOAuth(selected)):
                    single = Pool(registry, selected.provider, [selected.provider], 'fill_first')
                    auth = await single.auth(selected, SimpleNamespace(id=selected.provider), signal=signal)
                else:
                    auth = await registry.getApiKeyAndHeaders(
                        selected, AuthResolutionOverrides(apiKey=api_key or None, signal=signal))
                execution.check_cancelled()
                if not auth["ok"]:
                    raise RuntimeError(auth["error"])
                if not model:
                    from .nous import account_scope
                    with account_scope(issuing_credential()):
                        _, recommended = await run_in_thread(_route, '', provider, registry, main_runtime)
                    selected = resolve_model(provider, recommended, registry)
        except Exception as error:  # noqa: BLE001 - native client establishment may fail in any auth resolver
            return await unavailable(error)
        if selected.provider == 'nous' and not api_mode:
            from ..native.auxiliary_nous import nous_api_mode
            api_mode = nous_api_mode(selected.id)
        if api_mode and api_mode != "auto":
            native_api = {"chat_completions": "openai-completions",
                          "anthropic_messages": "anthropic-messages",
                          "codex_responses": (selected.api if selected.api in {
                              'openai-responses', 'azure-openai-responses', 'openai-codex-responses'}
                              else 'openai-codex-responses' if provider == 'openai-codex'
                              else 'openai-responses')}.get(api_mode, api_mode)
            selected = selected.model_copy(update={"api": native_api})
        original_endpoint = selected.baseUrl
        endpoint = base_url or auth.get("baseUrl")
        if endpoint:
            selected = selected.model_copy(update={"baseUrl": endpoint})
        remaining = timeout - (time.monotonic() - auth_started)
        # Ownerless auxiliary calls still keep one cache scope across retries.
        cache_id = str(uuid.uuid4())
        if route_info is not None:
            route_info.update(provider=selected.provider, model=selected.id)

        async def primary(*, temperature, max_tokens, extra_body=None):
            nonlocal remaining
            execution.check_cancelled()
            # Auth is part of the initial deadline. Each later physical attempt
            # gets its own budget, not the already-exhausted first timer.
            budget, remaining = remaining, timeout
            if external:
                from .acp import complete
                async with asyncio.timeout(budget):
                    return await complete(messages=messages, model=selected.id, timeout=budget,
                        cwd=_CWD.get(), signal=signal, temperature=temperature, max_tokens=max_tokens)
            attempt = AbortController()
            attempt_signal = combine_abort_signals(attempt, signal)
            request_id = str(uuid.uuid4())
            failed = False
            stream = None
            fixed = auxiliary_options._fixed_temperature_for_model(selected.id, selected.baseUrl)
            if fixed is auxiliary_options.OMIT_TEMPERATURE or auxiliary_options._forbids_sampling_params(selected.id):
                temperature = None
            elif fixed is not None:
                temperature = fixed
            from . import catalog
            with catalog.route(selected, auth):
                projection = (auxiliary_options._project_provider_profile(
                    selected.provider, selected.provider, selected.id, selected.baseUrl, reasoning)
                    if selected.api == 'openai-completions' else None)
            from ..native.opencode_affinity import opencode_session_headers
            headers = {**(auth.get('headers') or {}),
                       **(projection.top_level.get('extra_headers', {}) if projection else {}),
                       **opencode_session_headers(selected.provider, selected.baseUrl, _SESSION.get()),
                       **(extra_headers or {})}
            wire_model, wire_key = selected, auth.get('apiKey')
            if selected.api == 'anthropic-messages':
                from ..native.anthropic_endpoints import _requires_bearer_auth
                wire_model = selected.model_copy(update={'baseUrl': re.sub(r'/v1/?$', '', selected.baseUrl.rstrip('/'))})
                if wire_key and (selected.provider == 'nous' or _requires_bearer_auth(selected.baseUrl)):
                    headers['Authorization'] = f'Bearer {wire_key}'
                    wire_key = None  # native header-only auth omits X-Api-Key
            try:
                streamed = latency_info is not None or selected.api in {'anthropic-messages', 'openai-codex-responses'}
                hard_budget = auxiliary_options._aux_stream_total_ceiling(budget) if streamed else budget
                async with asyncio.timeout(hard_budget):
                    def on_response(_response, _model):
                        if latency_info is not None:
                            latency_info.setdefault("time_to_first_progress_ms", _elapsed_ms(started))

                    def on_payload(payload, wire_model):
                        if max_tokens is None:
                            _omit_default_output_limit(payload, wire_model)
                        if wire_model.api in _RESPONSES_APIS:
                            _responses_cache(payload, wire_model, main_runtime, cache_id,
                                             system_prompt=context.systemPrompt)
                        body = dict(extra_body or {})
                        if selected.provider == 'nous' and selected.api == 'anthropic-messages':
                            body = {**profile.build_extra_body(), **body}
                        if projection is not None:
                            payload.update({key: value for key, value in projection.top_level.items()
                                            if key != 'extra_headers'})
                            body = auxiliary_options._merge_aux_extra_body(body, projection, reasoning, wire_model.provider)
                        payload.update(body)
                        return payload

                    options = SimpleStreamOptions(
                        maxTokens=max_tokens, temperature=temperature, reasoning=level, timeoutMs=int(timeout * 1000),
                        apiKey=wire_key, headers=headers or None, env=auth.get("env"),
                        cacheRetention="none", sessionId=request_id, signal=attempt_signal, maxRetries=0,
                        transport="sse",
                        onResponse=on_response if latency_info is not None else None,
                        onPayload=on_payload,
                    )
                    if latency_info is not None:
                        latency_info.setdefault("provider_dispatch_ms", _elapsed_ms(started))
                    stream = registry.streamSimple(wire_model, context, options)
                    if streamed:
                        # Only substantive native deltas re-arm the idle window.
                        # Metadata/keepalives count for timing, never for liveness.
                        iterator = stream.__aiter__()
                        loop = asyncio.get_running_loop()
                        idle = min(60.0, budget)
                        progress_deadline = loop.time() + idle
                        saw_content = False
                        while True:
                            try:
                                async with asyncio.timeout_at(progress_deadline):
                                    event = await anext(iterator)
                            except StopAsyncIteration:
                                break
                            except TimeoutError as error:
                                note = 'stream stalled: no new output' if saw_content else 'no-progress timeout'
                                raise TimeoutError(f'Auxiliary {note} within {idle:.1f}s') from error
                            kind = read_field(event, 'type', '')
                            substantive = (kind in {'text_delta', 'thinking_delta', 'toolcall_delta'}
                                           and bool(read_field(event, 'delta'))) or kind == 'toolcall_start'
                            if substantive:
                                saw_content = True
                                progress_deadline = loop.time() + idle
                                on_response(None, selected)
                    response = await stream.result()
                    execution.check_cancelled()
                    if response.stopReason == "aborted":
                        raise asyncio.CancelledError(response.errorMessage or "Auxiliary request aborted")
                    if response.stopReason == "error":
                        cause = getattr(stream, "error_cause", None)
                        if cause is not None:
                            error = provider_error(cause)
                            if error is cause:
                                raise cause
                            raise error from cause
                        raise RuntimeError(response.errorMessage or "Auxiliary request failed")
                    return _response(response)
            except Exception as error:
                failed = True
                normalized = provider_error(error)
                if normalized is error:
                    raise
                raise normalized from error
            except BaseException:
                failed = True
                raise
            finally:
                attempt.abort()
                try:
                    try:
                        if stream is not None and hasattr(stream, 'settled'):
                            await stream.settled()
                    finally:
                        cleanup_session_resources(request_id)
                except Exception:
                    if not failed:
                        raise
                    logger.warning("Auxiliary resource cleanup failed after request failure", exc_info=True)

        async def refresh():
            nonlocal auth, selected
            from .pool import runtime_key
            if pool is not None and pooled_entry is not None:
                if pooled_entry.auth_type != 'oauth':
                    return False
                owner, entry = pool, pooled_entry
            else:
                # An explicit key must not refresh an unrelated saved OAuth login.
                if external or api_key or not hasattr(registry, 'authStorage') or not registry.isUsingOAuth(selected):
                    return False
                if not registry.authStorage.get(selected.provider):
                    return False
                owner = Pool(registry, selected.provider, [selected.provider], 'fill_first')
                entry = SimpleNamespace(id=selected.provider)
            try:
                async with asyncio.timeout(timeout):
                    refreshed = await owner.auth(selected, entry, signal=signal, rejected_key=runtime_key(auth))
                if not refreshed['ok']:
                    return False
                auth = refreshed
                selected = selected.model_copy(update={'baseUrl': base_url or auth.get('baseUrl') or original_endpoint})
                routing.update_auth(auth, selected.baseUrl)
                return True
            except Exception:
                execution.check_cancelled()
                logger.debug('Auxiliary OAuth refresh failed', exc_info=True)
                return False

        async def rotate(error):
            nonlocal auth, pooled_entry, selected
            from .pool import runtime_key
            try:
                async with asyncio.timeout(timeout):
                    entry, rotated = await pool.acquire(selected, failed_key=runtime_key(auth), error=error, signal=signal)
                pooled_entry, auth = entry, rotated
                selected = selected.model_copy(update={'baseUrl': base_url or auth.get('baseUrl') or original_endpoint})
                routing.update_auth(auth, selected.baseUrl)
                return True
            except Exception:
                execution.check_cancelled()
                logger.debug('Auxiliary credential rotation failed', exc_info=True)
                return False

        async def heal_model():
            nonlocal selected
            from ..native.auxiliary_nous import _refresh_nous_recommended_model
            from .nous import account_scope
            with account_scope(issuing_credential()):
                healed = await run_in_thread(_refresh_nous_recommended_model,
                    vision=task == 'vision', stale_model=selected.id)
            if not healed or healed == selected.id:
                return False
            # An explicit Portal recommendation is a routable ID, not a guessed
            # native catalog row. It does not change the owner's main model/window.
            selected = selected.model_copy(update={'id': healed, 'name': healed})
            if route_info is not None:
                route_info.update(provider=selected.provider, model=selected.id)
            return True

        async def paid_access():
            from .nous import account_info, credential_state
            state = credential_state(issuing_credential())
            try:
                info = await run_in_thread(account_info, state, force_fresh=True)
                return info.fresh and info.paid_service_access is True
            except Exception:
                logger.debug('Nous entitlement check failed', exc_info=True)
                return False

        async def fallback(error):
            from ..native import auxiliary_fallback as policy
            route = SimpleNamespace(client=SimpleNamespace(model=selected), task=task, tag='',
                resolved_provider=route_provider, final_model=selected.id,
                base_info=selected.baseUrl, main_runtime=main_runtime, route_info=route_info)

            async def perform(step):
                candidate, candidate_model, label = step.args
                try:
                    return await call_candidate(candidate, candidate_model, label)
                except Exception as candidate_error:
                    if not isinstance(candidate_error, _UnavailableClient) and not policy._is_auth_error(candidate_error):
                        raise
                    routing._mark_provider_unhealthy(candidate.provider, base_url=candidate.base_url)
                    return None

            return await policy._drive_ladder_async(policy._ladder_provider_fallback(error, route), perform)

        with routing.scope(registry, main_runtime, selected.provider, selected.baseUrl, auth):
            return await recover(primary, task=task, base_url=selected.baseUrl,
                temperature=temperature, max_tokens=max_tokens, extra_body=task_body,
                refresh=refresh, rotate=rotate if pool is not None and recovery else None,
                heal_model=heal_model if selected.provider == 'nous' and recovery else None,
                paid_access=paid_access if selected.provider == 'nous' and recovery else None,
                fallback=fallback if recovery else None, retries=recovery)
    finally:
        controller.abort()
        if aborting is not None:
            aborting.cancel()
            await asyncio.gather(aborting, return_exceptions=True)


def _elapsed_ms(started):
    return max(0, int((time.monotonic() - started) * 1000))


_RESPONSES_APIS = {"openai-responses", "openai-codex-responses", "azure-openai-responses"}


def _responses_cache(payload, model, main_runtime, request_id, *, system_prompt=None):
    """Original cache policy, separate from Pi's per-request resource identity."""
    from ..native.responses_cache import (
        _cache_scope_from_session_id,
        _content_cache_key,
        _default_prompt_cache_retention_for_request,
    )
    from ..native.support import base_url_host_matches

    # Replace Pi's random request key; it owns transport resources, not cache affinity.
    payload.pop("prompt_cache_key", None)
    # Generic native Responses encodes the system prompt in input[], while
    # Codex uses instructions. Hash the same actual prefix on both transports;
    # do not duplicate an existing input[] system message into instructions.
    instructions = system_prompt or payload.get("instructions") or "You are a helpful assistant."
    if not system_prompt and not payload.get("instructions"):
        payload["instructions"] = instructions
    excluded = any(base_url_host_matches(model.baseUrl, host)
                   for host in ("x.ai", "githubcopilot.com", "models.github.ai"))
    if not excluded:
        scope = _cache_scope_from_session_id(main_runtime.get("cache_scope")
            or main_runtime.get("session_id") or _SESSION.get() or request_id)
        key = _content_cache_key(instructions, payload.get("tools"), scope)
        if key:
            payload["prompt_cache_key"] = key
    retention = _default_prompt_cache_retention_for_request(model.id, model.baseUrl)
    if payload.get("prompt_cache_retention") is None:
        # Pi inserts a None for its disabled cache policy. It is not an explicit
        # Hermes retention override and must not mask Meta/Mantle's opt-in.
        payload.pop("prompt_cache_retention", None)
        if retention:
            payload["prompt_cache_retention"] = retention


def _omit_default_output_limit(payload, model):
    """An omitted Hermes output cap stays omitted on optional-cap transports.

    Pi's simple provider adds a model default. Use its existing final-payload
    hook instead of changing ordinary chat defaults. Anthropic requires a cap
    and, like Hermes' Messages adapter, keeps the native model ceiling.
    """
    if model.api in {'openai-completions', 'openai-responses', 'azure-openai-responses',
                     'openai-codex-responses', 'mistral-conversations'}:
        for key in ('max_tokens', 'max_completion_tokens', 'max_output_tokens'):
            payload.pop(key, None)
    return payload


def _response(response):
    """Keep Hermes' response contract; each consumer owns its acceptance policy.

    In particular ContextCompressor uses bounded reasoning fallback for empty
    content and rejects finish_reason=length. The LCM plugin deliberately does not
    use that fallback. Neither policy belongs in this shared transport adapter.
    """
    text, reasoning, tool_calls = [], [], []
    for block in response.content:
        kind = read_field(block, "type")
        if kind == "text":
            text.append(read_field(block, "text", ""))
        elif kind == "thinking" and not read_field(block, "redacted", False):
            reasoning.append(read_field(block, "thinking", ""))
        elif kind == "toolCall":
            tool_calls.append(SimpleNamespace(id=read_field(block, "id"), type="function",
                function=SimpleNamespace(name=read_field(block, "name"),
                    arguments=json.dumps(read_field(block, "arguments"), ensure_ascii=False))))
    return SimpleNamespace(
        id=response.responseId, model=response.responseModel or response.model, provider=response.provider,
        choices=[SimpleNamespace(
            finish_reason={"toolUse": "tool_calls"}.get(response.stopReason, response.stopReason),
            message=SimpleNamespace(content="".join(text), reasoning_content="\n\n".join(reasoning),
                                    tool_calls=tool_calls or None))],
        usage=SimpleNamespace(**usage_dict(response.usage)))


def call_llm(*, task="", messages=None, temperature=None, max_tokens=None, timeout=None,
             model="", provider="", main_runtime=None, base_url=None, api_key=None,
             api_mode=None, route_info=None, latency_info=None, extra_body=None,
             reasoning_config=None, extra_headers=None):
    from misaka.core.platform.session import run_coro

    from ..native.auxiliary_options import _acquire_sync_aux_semaphore
    from ..native.auxiliary_policy import _effective_aux_timeout

    with config_bridge.auxiliary_config():
        deadline = float(_effective_aux_timeout(task, timeout))
        if not math.isfinite(deadline) or deadline <= 0:
            raise ValueError("Auxiliary timeout must be finite and positive")
        if max_tokens is not None and int(max_tokens) <= 0:
            raise ValueError("Auxiliary output limit must be positive")
        started = time.monotonic()
        semaphore = _acquire_sync_aux_semaphore(task)
        if semaphore is not None:
            while not semaphore.acquire(timeout=0.05):
                execution.check_cancelled()
        try:
            return run_coro(_complete(messages, str(model or ""), str(provider or ""), temperature,
                None if max_tokens is None else int(max_tokens), deadline, task=task,
                main_runtime=dict(main_runtime or {}), base_url=base_url, api_key=api_key, api_mode=api_mode,
                route_info=route_info, latency_info=latency_info, started=started,
                extra_body=extra_body, reasoning_config=reasoning_config, extra_headers=extra_headers))
        finally:
            if semaphore is not None:
                semaphore.release()
            if latency_info is not None:
                latency_info["summary_generation_ms"] = _elapsed_ms(started)


def install() -> None:
    if "agent.auxiliary_client" not in sys.modules:
        module = ModuleType("agent.auxiliary_client")
        module.call_llm = call_llm
        sys.modules["agent.auxiliary_client"] = module
        if "agent" in sys.modules:
            sys.modules["agent"].auxiliary_client = module

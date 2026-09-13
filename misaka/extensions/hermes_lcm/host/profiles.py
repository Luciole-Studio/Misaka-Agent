"""Lazy pinned wire profiles; MISAKA alone owns catalogue discovery and credentials."""
from __future__ import annotations

import importlib
import threading
from importlib.metadata import version

from . import llm

HOST_VERSION = version('misaka')
_PROFILES = {}
_LOCK = threading.RLock()
_LOADED = False
_NAMES = (
    'actual', 'ai_gateway', 'alibaba', 'alibaba_coding_plan', 'anthropic', 'arcee',
    'azure_foundry', 'bedrock', 'commandcode', 'copilot', 'copilot_acp', 'custom',
    'deepinfra', 'deepseek', 'fireworks', 'gemini', 'gmi', 'huggingface', 'kilocode',
    'kimi_coding', 'meta_ai', 'minimax', 'nebius_token_factory', 'nous', 'novita',
    'nvidia', 'ollama_cloud', 'openai_codex', 'opencode_free', 'opencode_zen',
    'openrouter', 'qwen_oauth', 'router', 'stepfun', 'upstage', 'vertex', 'xai',
    'xiaomi', 'zai',
)


def register_provider(profile):
    _PROFILES[profile.name] = profile
    for alias in profile.aliases:
        _PROFILES[alias] = profile


def get_provider_profile(name):
    global _LOADED
    with _LOCK:
        if not _LOADED:
            for module in _NAMES:
                importlib.import_module(f'.profile_{module}', package='misaka.extensions.hermes_lcm.native')
            _LOADED = True
    name = {'google': 'gemini', 'google-vertex': 'vertex', 'amazon-bedrock': 'bedrock',
            'github-copilot': 'copilot', 'openai': 'custom'}.get(name, name)
    return _PROFILES.get(name) or (_PROFILES['custom'] if name.startswith('custom:') else None)


def get_conversation_context():
    return llm._SESSION.get()


def get_affinity_scope():
    return llm._SESSION.get()


def nous_portal_tags(session_id=None):
    from ..native.portal_tags import nous_portal_tags as tags
    return tags(session_id)


def open_credentialed_url(*args, **kwargs):
    # Profiles' catalogue helpers are not used for native model discovery. Keep
    # their explicit HTTP helper redirect-safe if invoked by an operator.
    from ..native.urllib_security import open_credentialed_url as open_url
    return open_url(*args, **kwargs)


def _reasoning_caps(provider, model):
    from ..native import reasoning_caps
    return getattr(reasoning_caps, f'{provider}_model_reasoning_capabilities')(model)


def openrouter_model_reasoning_capabilities(model):
    return _reasoning_caps('openrouter', model)


def nous_model_reasoning_capabilities(model):
    return _reasoning_caps('nous', model)


def warm_nous_reasoning_caps_async():
    from ..native.reasoning_caps import warm_nous_reasoning_caps_async as warm
    return warm()


def github_model_reasoning_efforts(model, **kwargs):
    from ..native.copilot_catalog import github_model_reasoning_efforts as efforts
    return efforts(model, **kwargs)


def get_nous_recommended_aux_model(**kwargs):
    from ..native.recommended_models import (
        get_nous_recommended_aux_model as recommended,
    )
    return recommended(**kwargs)


def _fetch_deepinfra_models_by_tag(tag):
    registry = llm._REGISTRY.get()
    if registry is None or tag != 'chat':
        return []
    return [{'id': m.id, 'metadata': {'tags': ['vision'] if 'image' in m.input else []}}
            for m in registry.getAll() if m.provider == 'deepinfra']


def normalize_provider(name):
    profile = get_provider_profile(str(name or '').strip().lower())
    return profile.name if profile else str(name or '').strip().lower()


def native_provider(name, registry):
    """Native registrations win; pinned aliases map only to an installed transport."""
    names = {model.provider for model in registry.getAll()}
    if name in names:
        return name
    profile = get_provider_profile(str(name or '').lower())
    if profile is None:
        return name
    canonical = profile.name
    if canonical in names:
        return canonical
    mapped = {'copilot': 'github-copilot', 'gemini': 'google', 'vertex': 'google-vertex',
              'bedrock': 'amazon-bedrock', 'custom': 'openai'}.get(canonical)
    return mapped if mapped in names else name

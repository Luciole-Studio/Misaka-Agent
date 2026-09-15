"""MISAKA boundaries for the pinned Hermes native compressor, not its policy."""
from __future__ import annotations

import asyncio
from datetime import datetime

from misaka.core.platform.prompt_guard import untrusted
from misaka.utils.values import read_field

from ..native.support import (  # noqa: F401 - pinned compressor imports this auxiliary boundary
    AuxiliaryExplicitCancellation,
    _is_connection_error,
    aux_interrupt_protection,
    extract_content_or_reasoning,
)
from . import llm


def call_llm(**kwargs):
    # Let Hermes roll back its handoff scan before the owner receives cancellation.
    # BaseException bypasses the plugin's transport-error/deterministic fallback.
    try:
        return llm.call_llm(**kwargs)
    except asyncio.CancelledError as error:
        raise AuxiliaryExplicitCancellation() from error


def get_model_context_length(model, base_url="", api_key="", config_context_length=None,
                             provider="", custom_providers=None):
    """Use the window already resolved by the owner; never probe Hermes endpoints."""
    if config_context_length is not None:
        if type(config_context_length) is not int or config_context_length <= 0:
            raise ValueError("Native compression requires a positive context length")
        return config_context_length
    selected = llm._MODEL.get()
    if model == read_field(selected, "id") and (not provider or provider == read_field(selected, "provider")):
        return selected.contextWindow
    registry = llm._REGISTRY.get()
    if registry is not None:
        found = registry.find(provider, model)
        if found is not None:
            return found.contextWindow
    # Only direct/offline callers lack a bound model. Keep the original fallback.
    from ..native.model_metadata import DEFAULT_FALLBACK_CONTEXT
    return DEFAULT_FALLBACK_CONTEXT


def now():
    """The host's local clock, with no access to another installation's config."""
    return datetime.now().astimezone()


def get_plugin_error_classification(**context):
    # MISAKA has no Hermes provider-classifier hooks. Built-in classification runs.
    return None


def suggest_prefixed_model_id(provider, model_name):
    """The original diagnostic uses the host catalogue; never guess a model alias."""
    registry = llm._REGISTRY.get()
    if registry is None or not model_name or "/" in model_name:
        return None
    matches = [model.id for model in registry.getAll()
               if model.provider == provider and model.id.rsplit("/", 1)[-1] == model_name]
    return matches[0] if len(matches) == 1 and "/" in matches[0] else None


def trim_memory(*, reason):
    # Optional glibc allocator maintenance is not a memory provider. MISAKA does
    # not enable Hermes' allocator/config service; keep this optional hook inert.
    return None


def fence_summary_content(content, source):
    """Fence a marked native handoff, retaining the carrier's live user text.

    Ignored/stateless sessions have no LCM DAG provenance to vouch for a summary.
    Hermes appends/prepends the complete handoff as one text part; its end marker
    separates that reference from the human request in a merged carrier.
    """
    from ..native.context_compressor import (
        _MERGED_SUMMARY_DELIMITER,
        _SUMMARY_END_MARKER,
        SUMMARY_PREFIX,
    )

    def fence_body(reference):
        leading, prefix, body = reference.partition(SUMMARY_PREFIX)
        if prefix and not leading.strip():
            # Keep runtime-owned framing outside the data fence. Otherwise the
            # original parser loses its prefix, and treats our footer after its
            # end marker as a new human task on the next compaction.
            return leading + prefix + untrusted(source, body)
        return untrusted(source, reference)

    def fence_text(text):
        reference, end, live = text.partition(_SUMMARY_END_MARKER)
        if not end:
            return fence_body(text)
        prior, delimiter, summary = reference.partition(_MERGED_SUMMARY_DELIMITER)
        if not delimiter:
            return fence_body(reference) + end + live
        return prior + delimiter + fence_body(summary) + end + live

    if isinstance(content, str):
        return fence_text(content)
    parts = list(content or [])
    has_boundary = any(isinstance(part, dict) and _SUMMARY_END_MARKER in str(part.get('text', ''))
                       for part in parts)
    return [{**part, 'text': fence_text(part['text'])}
            if isinstance(part, dict) and isinstance(part.get('text'), str)
            and (not has_boundary or _SUMMARY_END_MARKER in part['text']) else part for part in parts]


def session_ids():
    from misaka.core.session_catalog import list_entries
    return {entry['id'] for entry in list_entries(None)}


def _cache_file(name):
    from pathlib import Path

    from misaka.config import get_agent_dir
    return Path(get_agent_dir()) / 'cache' / name


def _load_json_dict(path):
    # The only caller is image-token calibration. Keep the core's process-local
    # estimates, but do not reload observations from an earlier application run.
    return {}


def atomic_json_write(path, value, **options):
    # The image-cost writer's existing host seam: calibration is runtime-only.
    return None


def session_is_branch_of(session_id, parent_session_id):
    from misaka.core.session_catalog import list_entries
    from misaka.core.session_manager import read_session_header
    for entry in list_entries(None):
        if entry['id'] == session_id and entry.get('path'):
            parent = read_session_header(entry['path']).get('parentSession')
            return bool(parent and read_session_header(parent).get('id') == parent_session_id)
    return False


def estimate_native_responses_preflight_tokens(agent, messages, *, system_prompt='', tools=None):
    # Hermes' encrypted server-compaction/checkpoint side channel is not a native
    # MISAKA owner. The supplied native replay already contains the adopted full
    # checkpoint, so the original generic fallback prices that pruned replay.
    return None

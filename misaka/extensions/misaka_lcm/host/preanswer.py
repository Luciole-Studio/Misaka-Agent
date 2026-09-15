"""Adapt the original Hermes pre-LLM hook, without reimplementing its decisions."""
from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace

from misaka.ai.types import TextContent
from misaka.core.platform.prompt_guard import untrusted
from misaka.utils.values import read_field

from ..vendor import _pre_llm_context, get_recall_policy
from . import context_engine, execution, fence, ingest


def _local_index(built):
    """Bounded original indexing of loaded content; never send raw text to a cloud provider."""
    config = dataclasses.replace(built._config)
    if (not config.proactive_recall_enabled or not config.embeddings_enabled
            or config.embedding_provider.strip().lower() not in {'fastembed', 'fast-embed'}
            or getattr(built, '_misaka_index_busy', False)):
        return
    from ..vendor import command, tools
    from ..vendor.embedding_provider import resolve_provider
    from ..vendor.vector_store import VectorStore
    # Freeze the worker's config. An expired worker may finish after a settings
    # change; the upstream profile identity/lease protects its DB publication.
    worker = SimpleNamespace(_config=config, _store=built._store)
    built._misaka_index_busy = True
    def index():
        try:
            # The operator's explicit warmup permits downloads. Automatic work
            # instead uses the cached-only query API and original profile APIs.
            provider = resolve_provider(config)
            dimension = command._resolve_store_dim(config, len(provider.embed_query('MISAKA LCM local index')))
            vectors = VectorStore(worker._store.db_path, config=config)
            try:
                for task in ('summary', 'chunk'):
                    vectors.register_profile(provider.model_id, provider.provider_id, dimension,
                                             dtype=command._resolve_storage_dtype(config), task=task)
            finally:
                vectors.close()
            # ponytail: upstream scans the loaded corpus to find missing rows;
            # keep its policy/lease, cap each batch, and bound foreground wait.
            built._misaka_index_status = command._embedding_backfill_text(
                ['--corpus', 'both', '--apply', '--limit', '32'], worker)
        finally:
            built._misaka_index_busy = False
    try:
        tools._run_within_deadline(index, remaining_s=2, name='misaka-lcm-local-index')
    except TimeoutError:
        pass  # Worker ownership retains SQLite/cache until the indexer settles.
    except (RuntimeError, OSError) as error:
        built._misaka_index_busy = False
        built._misaka_index_status = str(error)


def _anchor(messages) -> tuple[str, str | None]:
    # Inspect native identities before custom/compaction messages become API user turns.
    for message in reversed(messages):
        if not ingest.is_task_message(message):
            continue
        question = ingest._text_of(read_field(message, "content"))
        stamp = read_field(message, "timestamp")
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or stamp <= 0:
            return question, None
        try:
            return question, datetime.fromtimestamp(stamp / 1000, UTC).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return question, None
    return "", None


def inject(event, ctx, *, active_tools=None) -> dict | None:
    if active_tools is not None and not any(name.startswith("lcm_") for name in active_tools):
        return None
    messages = list(read_field(event, "messages") or [])
    index = next((i for i in range(len(messages) - 1, -1, -1)
                  if ingest.is_task_message(messages[i])), None)
    if index is None:
        return None
    built = context_engine.bound_engine(ctx)
    question, question_date = _anchor(messages)
    policy = get_recall_policy()
    # The append-only ingress ID is stable across tool rounds, retries and
    # compactions, but distinct for an identical question submitted a second time.
    branch = read_field(ctx.sessionManager, "getBranch")
    key = context_engine.turn_key(context_engine.Transcript([], list(branch()))) if branch else None
    if key is not None:
        config = built._config
        key = (key, config.proactive_recall_enabled, config.embeddings_enabled,
               config.embedding_provider, config.embedding_model)
    cached = getattr(built, "_misaka_preanswer", None)
    if key is not None and cached is not None and cached[0] == key:
        context = cached[1]
    else:
        # Mark before dispatch: a failed hook is fail-open for this turn, not a
        # new paid retrieval on every subsequent provider request.
        built._misaka_preanswer = (key, "")
        result = _pre_llm_context(built, policy, {
            "user_message": question, "question_date": question_date,
            "conversation_history": ingest.upstream_messages(messages),
            "session_id": ingest.session_id(ctx), "enabled_toolsets": ["context_engine"],
        })
        context = result.get("context", "")
        # Native requests often need no compaction, so upstream's assembly-only
        # injection would never run. Reuse its exact retrieval/ranking/budget
        # builder here, once per ingress, without persisting the augmentation.
        from ..vendor.engine import LCMEngine
        with execution.worker_owner(built):
            _local_index(built)
            recalled = LCMEngine._build_proactive_recall_message(
                built, [{"role": "user", "content": question}], "user", set())
        if recalled is not None:
            context += "\n\n" + untrusted("lcm:proactive-recall", recalled["content"])
        execution.check_cancelled()
        built._misaka_preanswer = (key, context)
    if not context:
        return None
    if context.startswith(policy):
        # The policy itself is in the system prompt (tools.recall_guideline); only a retrieved
        # brief belongs on the turn, and a turn with nothing retrieved is left exactly as typed.
        context = context[len(policy):].strip()
        if not context:
            return None
        if fence.is_tainted(built, store_ids=fence.cited_rows(context)):
            context = untrusted(f"lcm:preanswer:{ingest.session_id(ctx)}", context)
    # Hermes attaches this ephemeral context to the actual user message, not the system
    # prompt. Preserve native message identity and do not persist the augmentation.
    message = copy.deepcopy(messages[index])
    content = read_field(message, "content")
    content = content + "\n\n" + context if isinstance(content, str) else [
        *(content or []), TextContent(text=context)]
    if isinstance(message, dict):
        message["content"] = content
    else:
        message.content = content
    messages[index] = message
    return {"messages": messages}

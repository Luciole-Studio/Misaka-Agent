"""Adapt the original Hermes pre-LLM hook, without reimplementing its decisions."""
from __future__ import annotations

import copy
from datetime import UTC, datetime

from misaka.ai.types import TextContent
from misaka.core.platform.prompt_guard import untrusted
from misaka.utils.values import read_field

from ..vendor import _pre_llm_context, get_recall_policy
from . import context_engine, execution, fence, ingest


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

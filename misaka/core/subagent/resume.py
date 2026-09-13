"""CCB resumeAgent message selection (77a7934e15d69da13879112ed7db695c9ee7a52a).

Port filterUnresolvedToolUses, filterOrphanedThinkingOnlyMessages, and the
whitespace filter plus mergeUserMessages from src/utils/messages.ts. Native tool
results stay separate for Pi provider conversion. Merge only the request view
after Hermes LCM has validated the original message objects against its archive.
Never rewrite the transcript: other branches and signed thinking stay intact.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from misaka.ai.types import TextContent
from misaka.core.prompt_templates import _ECMASCRIPT_WHITESPACE
from misaka.utils.values import read_field


def _blocks(message: Any) -> list[Any]:
    value = read_field(message, 'content')
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []


def _response_id(message: Any) -> Any:
    # Pi names the provider message.id responseId, not the JSONL entry ID.
    value = read_field(message, "responseId")
    return read_field(message, "id") if value is None else value


def _merge_user_messages(a: Any, b: Any) -> Any:
    # Source normalizeUserTextContent -> joinTextAtSeam -> hoistToolResults.
    # Shallow copies preserve image/signature blocks; never mutate the archive.
    left, right = read_field(a, 'content'), read_field(b, 'content')
    left = [TextContent(text=left) if hasattr(a, 'model_copy') else {'type': 'text', 'text': left}] if isinstance(left, str) else list(left)
    right = [TextContent(text=right) if hasattr(b, 'model_copy') else {'type': 'text', 'text': right}] if isinstance(right, str) else list(right)
    if left and right and read_field(left[-1], 'type') == read_field(right[0], 'type') == 'text':
        last = left[-1]
        update = {'text': read_field(last, 'text') + '\n'}
        left[-1] = last.model_copy(update=update) if hasattr(last, 'model_copy') else {**last, **update}
    joined = left + right
    content = [block for block in joined if read_field(block, 'type') == 'tool_result']
    content += [block for block in joined if read_field(block, 'type') != 'tool_result']
    update = {'content': content}
    if read_field(a, 'isMeta'):
        update['uuid'] = read_field(b, 'uuid')
    return a.model_copy(update=update) if hasattr(a, 'model_copy') else {**a, **update}


def filter_resume_messages(messages: list[Any]) -> list[Any]:
    calls, results = set(), set()
    for message in messages:
        role = read_field(message, 'role')
        if role in {'toolResult', 'tool', 'tool_result'}:
            results.add(read_field(message, 'toolCallId', read_field(message, 'tool_call_id')))
        if role not in {'user', 'assistant'}:
            continue
        for block in _blocks(message):
            kind = read_field(block, 'type')
            if kind in {'toolCall', 'tool_use'}:
                calls.add(read_field(block, 'id'))
            elif kind in {'toolResult', 'tool_result'}:
                results.add(read_field(block, 'tool_use_id', read_field(block, 'toolCallId')))
    unresolved = calls - results
    selected = []
    for message in messages:
        if read_field(message, 'role') == 'assistant':
            ids = [read_field(block, 'id') for block in _blocks(message)
                   if read_field(block, 'type') in {'toolCall', 'tool_use'}]
            # Source resume removes the MESSAGE only when every call is
            # unresolved; this is not its separate block-level fork cleaner.
            if ids and all(call in unresolved for call in ids):
                continue
        selected.append(message)
    companions = {_response_id(message) for message in selected
                  if read_field(message, 'role') == 'assistant' and _response_id(message)
                  and any(read_field(block, 'type') not in {'thinking', 'redacted_thinking'}
                          for block in _blocks(message))}
    result = []
    removed_whitespace = False
    for message in selected:
        blocks = _blocks(message)
        if read_field(message, 'role') == 'assistant' and blocks:
            if (all(read_field(block, 'type') in {'thinking', 'redacted_thinking'} for block in blocks)
                    and (not _response_id(message) or _response_id(message) not in companions)):
                continue
            if all(read_field(block, 'type') == 'text' and all(char in _ECMASCRIPT_WHITESPACE for char in str(read_field(block, 'text', '')))
                   for block in blocks):
                removed_whitespace = True
                continue
        result.append(message)
    if not removed_whitespace:
        return result
    merged = []
    for message in result:
        if merged and read_field(message, 'role') == read_field(merged[-1], 'role') == 'user':
            merged[-1] = _merge_user_messages(merged[-1], message)
        else:
            merged.append(message)
    return merged


def install_resume_filter(session: Any) -> None:
    """Select at Pi's request boundary AFTER native engine/source validation.

    Archive and active messages remain the same source of truth for Hermes LCM.
    Its existing preflight may publish a checkpoint before this view is built.
    No weakened archive validation, parallel replay cache, or duplicate JSONL IDs.
    """
    previous = session.agent.transformContext

    async def transform(messages, signal):
        if previous is not None:
            messages = await previous(messages, signal)
        return filter_resume_messages(messages)

    session.agent.transformContext = transform

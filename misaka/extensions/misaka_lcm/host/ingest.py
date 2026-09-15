"""misaka's session shapes -> the OpenAI-shaped message dicts upstream ingests.

Upstream's contract with its host is one list per turn: *the messages the model is
actually being sent*, growing as the turn appends and shrinking when compaction
rewrites it. misaka's equivalent is ``build_session_context(branch).messages`` -- and
that includes the compaction-summary message, which is why it is converted here rather
than dropped. Upstream recognises its own summary scaffold, keeps it out of the durable
store, and its ingest cursor counts it, so replaying it is what keeps the cursor and the
store in step across a compaction.

Structured content is preserved for upstream's canonical JSON storage and payload
protection. Text extraction is reserved for identifying the human question.
"""

from __future__ import annotations

import json

from misaka.agent.harness.messages import convert_to_llm
from misaka.utils.values import read_field

# Placeholder for a content block that has no useful text: the durable store keeps the
# turn's shape and its position without the payload.
_OMITTED = "[{kind} omitted]"


def is_task_message(message) -> bool:
    """Native task ingress, before logs and summaries become provider user turns."""
    role = read_field(message, 'role')
    return role == 'user' or (
        (role == 'custom' or read_field(message, 'type') == 'custom_message')
        and read_field(message, 'customType') == 'research-phase')


def _text_of(content) -> str:
    """Extract display/anchor text without using it as the durable representation."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        kind = read_field(block, "type")
        if kind == "text":
            parts.append(str(read_field(block, "text") or ""))
        elif kind == "image":
            parts.append(_OMITTED.format(kind=str(read_field(block, "mimeType") or "image")))
        elif kind in {"thinking", "toolCall"}:
            continue
        else:
            parts.append(_OMITTED.format(kind=str(kind or "content")))
    return "\n".join(part for part in parts if part)


def _tool_calls_of(content) -> list[dict] | None:
    """The assistant's tool calls, in the OpenAI shape upstream pairs against."""
    if not isinstance(content, list):
        return None
    calls = [
        {
            "id": str(read_field(block, "id") or ""),
            "type": "function",
            "function": {
                "name": str(read_field(block, "name") or ""),
                "arguments": json.dumps(read_field(block, "arguments") or {}, ensure_ascii=False),
            },
        }
        for block in content
        if read_field(block, "type") == "toolCall"
    ]
    return calls or None


def to_upstream(message) -> dict:
    """One converted LLM-shaped message.

    Optional keys are omitted rather than set to ``None``: upstream reads ``tool_calls``
    with ``msg.get("tool_calls", [])`` and iterates the result, so an explicit ``None``
    is not the same thing as an absent key.
    """
    role = str(read_field(message, "role"))
    content = read_field(message, "content")
    timestamp = read_field(message, "timestamp")
    if isinstance(content, list):
        content = [block.model_dump(mode="json", exclude_none=True)
                   if hasattr(block, "model_dump") else dict(block)
                   for block in content if read_field(block, "type") != "toolCall"]
        content = [
            {"type": "image_url", "image_url": {
                "url": f"data:{block['mimeType']};base64,{block['data']}"}}
            if block.get("type") == "image" else block for block in content
        ]
    converted = {"role": "tool" if role == "toolResult" else role, "content": content}
    if timestamp is not None:
        # Unix seconds: upstream rejects anything it cannot read as an observation time,
        # and misaka counts milliseconds.
        converted["timestamp"] = float(timestamp) / 1000
    if role == "assistant":
        converted["tool_calls"] = _tool_calls_of(read_field(message, "content")) or []
    if role == "toolResult":
        converted["tool_call_id"] = str(read_field(message, "toolCallId") or "")
        converted["tool_name"] = str(read_field(message, "toolName") or "")
    return converted


def upstream_messages(messages) -> list[dict]:
    """A misaka active context (``build_session_context(...).messages``), converted."""
    return [to_upstream(message) for message in convert_to_llm(messages)]



def source_messages(messages):
    """Compare source content without a custom entry's regenerated timestamp.

    SessionManager timestamps custom entries when persisting them, independently
    of the live wrapper's creation time. Raw user/assistant/tool timestamps remain
    part of the identity. Assistant completion state also distinguishes a failed
    attempt from a successful response with otherwise identical wire content.
    """
    projected = []
    for message in messages:
        group = upstream_messages([message])
        if read_field(message, 'role') == 'custom':
            for row in group:
                row.pop('timestamp', None)
        elif read_field(message, 'role') == 'assistant':
            for row in group:
                row.update(stopReason=read_field(message, 'stopReason'),
                           errorMessage=read_field(message, 'errorMessage'))
        projected.extend(group)
    return projected


def source_indices(messages, originals):
    """Validate the active view and retain its exact positions in the saved view.

    Native retries remove failed/truncated assistants only from live context. They
    may occur anywhere in the append-only archive after later turns arrive. Match
    in reverse so identical retry attempts retain the last occurrence, as the native
    retry loop does. No successful message, tool result or user turn may be skipped.
    """
    incoming, saved = source_messages(messages), source_messages(originals)
    indices = []
    pending = len(incoming) - 1
    for index in range(len(saved) - 1, -1, -1):
        row = saved[index]
        if pending >= 0 and incoming[pending] == row:
            indices.append(index)
            pending -= 1
        elif row['role'] != 'assistant' or row.get('stopReason') not in {'error', 'length'}:
            break
    else:
        if pending == -1:
            return indices[::-1]
    raise ValueError('LCM request messages do not belong to the transcript snapshot '
                     f'(active={len(incoming)}, snapshot={len(saved)}, '
                     f'first_mismatch={next((i for i, pair in enumerate(zip(incoming, saved)) if pair[0] != pair[1]), None)})')


def session_id(ctx) -> str:
    """The misaka session id, which is LCM's session identity."""
    try:
        return str(ctx.sessionManager.getSessionId())
    except Exception:  # noqa: BLE001 - a session without an id is addressed by nothing
        return ""


# Private transport metadata. Never inferred from message text or written onto native
# messages; it survives upstream's dict-copy cleanup so identical siblings stay distinct.
SOURCE = '_misaka_replay_source'
# Hermes' semantic flags live in checkpoint details, not provider message schemas.
# The archive bookkeeping markers (_db_persisted/_compaction_tail) are not needed:
# SessionManager already owns raw entry identity and checkpoint adoption.
NATIVE_METADATA = frozenset({'_compressed_summary', '_compressed_summary_has_user_turn',
                             '_micro_compact_marker', '_inflight_replay_merged'})


def preserve_sources(before, after):
    """Ingest preserves ordinal positions, including cached sanitized replacements."""
    if len(before) != len(after):
        raise ValueError('LCM ingest changed message cardinality')
    result = []
    for original, replay in zip(before, after):
        if original.get(SOURCE) == replay.get(SOURCE):
            result.append(replay)
        else:
            replay = dict(replay)
            replay.pop(SOURCE, None)
            if SOURCE in original:
                replay[SOURCE] = original[SOURCE]
            result.append(replay)
    return result


class Replay:
    """Lossless native join for the engine's whole-context replacement.

    Untouched rows retain native metadata; changed rows replace only the fields LCM
    changed. Newly generated scaffold/stub messages get native shapes, not the
    metadata of an arbitrary original that happens to have the same text.
    """
    def __init__(self, messages):
        import copy
        self.originals = []
        self.messages = []
        for message in messages:
            converted = convert_to_llm([message])
            if not converted:
                continue
            for converted_message in converted:
                index = len(self.originals)
                self.originals.append(copy.deepcopy(message))
                self.messages.append({**to_upstream(converted_message), SOURCE: index})

    def unchanged(self, messages):
        """Receipt-time jitter is lost on restore; all other fields must match.

        Keep source ordinals and native compression flags in this comparison:
        equal prose alone does not prove an unchanged checkpoint.
        """
        return len(messages) == len(self.messages) and all(
            {key: value for key, value in after.items() if key != 'timestamp'}
            == {key: value for key, value in before.items() if key != 'timestamp'}
            for before, after in zip(self.messages, messages))

    def restore(self, messages, model=None):
        import copy

        from misaka.ai.types import validate_message

        restored, seen = [], set()
        for replay in messages:
            source = replay.get(SOURCE)
            before = self.messages[source] if type(source) is int and 0 <= source < len(self.messages) else None
            if SOURCE in replay and (before is None or source in seen):
                raise ValueError('Invalid or duplicated LCM replay source')
            if before is not None:
                seen.add(source)
                if replay.get('role') != before['role']:
                    raise ValueError('LCM changed the role of a source message')
                if all(replay.get(key) == before.get(key) for key in
                       ('role', 'content', 'tool_calls', 'tool_call_id', 'tool_name')):
                    restored.append(copy.deepcopy(self.originals[source]))
                    continue
            role = replay.get('role')
            if role not in {'user', 'assistant', 'tool'}:
                raise ValueError(f'Unsupported LCM replay role: {role}')
            original = self.originals[source] if before is not None else None
            if read_field(original, 'role') in {'user', 'assistant', 'toolResult', 'custom'}:
                payload = (original.model_dump(mode='json') if hasattr(original, 'model_dump') else copy.deepcopy(original))
            else:
                payload = {'role': 'toolResult' if role == 'tool' else role,
                           'timestamp': int(float(replay.get('timestamp') or 0) * 1000)}
            content = replay.get('content')
            blocks = ([{'type': 'text', 'text': content}] if content else []) if isinstance(content, str) else list(content or [])
            native_blocks = []
            for block in blocks:
                block = copy.deepcopy(block)
                if block.get('type') == 'image_url':
                    url = block['image_url']['url']
                    header, sep, data = url.partition(';base64,')
                    if not sep or not header.startswith('data:'):
                        raise ValueError('LCM image replay is not a native data image')
                    block = {'type': 'image', 'mimeType': header[5:], 'data': data}
                native_blocks.append(block)
            if role == 'assistant':
                from misaka.ai.providers._common import _empty_usage
                for call in replay.get('tool_calls') or []:
                    function = call['function']
                    arguments = function.get('arguments', '{}')
                    native_blocks.append({'type': 'toolCall', 'id': call['id'], 'name': function['name'],
                                          'arguments': json.loads(arguments) if isinstance(arguments, str) else arguments})
                for key, default in {'api': 'lcm', 'provider': 'lcm', 'model': 'lcm',
                                     'usage': _empty_usage(), 'stopReason': 'toolUse' if replay.get('tool_calls') else 'stop'}.items():
                    payload.setdefault(key, read_field(model, 'id' if key == 'model' else key, default)
                                       if key in {'api', 'provider', 'model'} else default)
            elif role == 'tool':
                payload.update(toolCallId=str(replay.get('tool_call_id') or ''),
                               toolName=str(replay.get('tool_name') or payload.get('toolName', '')))
                payload.setdefault('isError', True)  # generated missing-result stub
            # UserMessage supports strings: preserve the engine's wire shape.
            # Wrapping a generated summary in a text-block list inflated request
            # pressure after checkpoint restore and could shift later budget cuts.
            payload['content'] = content if role == 'user' and isinstance(content, str) else native_blocks
            if payload['role'] == 'custom':
                # Custom-message metadata belongs to its native UI wrapper, not the wire.
                convert_to_llm([payload])
                restored.append(payload)
            else:
                restored.append(validate_message(payload))
        return restored

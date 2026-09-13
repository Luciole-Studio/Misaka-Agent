"""Pinned LocalAgentTask progress accounting, independent of MISAKA billing.

CCB uses latest input+cache tokens plus cumulative outputs for displayed progress.
Rebuild from sequence-keyed completed messages so replay/out-of-order sidecars
never double-count a call. Billing keeps the existing sum of all requests.
"""
from misaka.utils.values import read_field


def from_messages(messages):
    latest_input = outputs = calls = 0
    activities = []
    for message in messages:
        if read_field(message, 'role') != 'assistant':
            continue
        usage = read_field(message, 'usage')
        if usage is None:
            continue
        latest_input = sum(int(read_field(usage, native, read_field(usage, upstream, 0)) or 0)
                           for native, upstream in (('input', 'input_tokens'), ('cacheRead', 'cache_read_input_tokens'), ('cacheWrite', 'cache_creation_input_tokens')))
        outputs += int(read_field(usage, 'output', read_field(usage, 'output_tokens', 0)) or 0)
        for block in read_field(message, 'content', []):
            if read_field(block, 'type') not in {'toolCall', 'tool_use'}:
                continue
            calls += 1
            name = read_field(block, 'name')
            if name != 'StructuredOutput':
                activities.append({'toolName': name, 'input': read_field(block, 'arguments', read_field(block, 'input', {}))})
        activities = activities[-5:]
    result = {'toolUseCount': calls, 'tokenCount': latest_input + outputs, 'recentActivities': activities}
    if activities:
        result['lastActivity'] = activities[-1]
    return result

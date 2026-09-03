"""Inter-agent messages: SendMessage plus, when asked, the inbox pump."""
from functools import partial

from misaka.core.network import messages
from misaka.core.wiring import delegates


def activate(spec):
    can_delegate = delegates(spec)
    if spec.kind == "beast" and not can_delegate:
        return None
    route = None
    if can_delegate:
        from misaka.core.subagent import extension as subagent
        route = subagent.route_to_children
    return partial(messages.register, sender=(spec.sender or spec.role.rsplit("/", 1)[-1]),
                   route=route, receive=spec.receive_messages)

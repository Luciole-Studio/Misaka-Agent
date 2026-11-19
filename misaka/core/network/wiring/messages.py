"""Inter-agent messages: SendMessage plus, when asked, the inbox pump."""
from misaka.core.network import messages
from misaka.core.wiring import delegates, sender_address


def part(spec):
    can_delegate = delegates(spec)
    if spec.kind == "beast" and not can_delegate:
        return None
    route = None
    if can_delegate:
        from misaka.core.subagent import extension as subagent
        route = subagent.route_to_children
    return messages.MessagesPart(
        sender=sender_address(spec), route=route, receive=spec.receive_messages,
        card_task=spec.task_id,
        contact=spec.kind == "dm",
    )

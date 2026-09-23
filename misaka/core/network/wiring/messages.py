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
    sender = sender_address(spec)
    return messages.MessagesPart(
        sender=sender, route=route, receive=spec.receive_messages,
        card_task=spec.task_id,
        # A Sister's help request is answered by the Last Order working in that card's
        # project: the one session that can read the card and reply through
        # misaka_sister_message. Any other project's help waits for the contact turn, which
        # carries an allowlist for exactly those cards.
        task_help_consumer=spec.receive_messages and spec.kind == "foreground" and sender == "last-order",
        workspace=spec.workspace,
    )

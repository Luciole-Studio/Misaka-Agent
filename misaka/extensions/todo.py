"""Per-card to-do tools; only a session that owns a card has a list to keep."""
from misaka.network import todo

SESSION_KINDS = {"card", "beast"}


def activate(spec):
    return todo.tools_for(spec.task_id, spec.sender or spec.role.rsplit("/", 1)[-1]) if spec.task_id else None

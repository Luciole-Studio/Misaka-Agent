"""Per-card to-do tools; only a session that owns a card has a list to keep."""
from misaka.core.network import todo

SESSION_KINDS = {"card", "beast"}


def part(spec):
    if not spec.task_id:
        return None
    result = todo.TodoPart(spec.task_id, spec.sender or spec.role.rsplit("/", 1)[-1])
    if spec.kind == "beast":
        result.tools = []
    return result

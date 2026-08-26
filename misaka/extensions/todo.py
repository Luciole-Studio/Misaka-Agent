"""Per-card to-do tools; only a session that owns a card has a list to keep."""
from misaka.network import todo

# Not "beast": a beast session is a card under budget pressure, and it is started with
# `-t Agent,TaskOutput,SendMessage,TaskStop` (or `-nt`). Registering four todo tools and
# their reminder hooks there produces tools the ceiling then hides.
SESSION_KINDS = {"card"}


def activate(spec):
    return todo.tools_for(spec.task_id, spec.sender or spec.role.rsplit("/", 1)[-1]) if spec.task_id else None

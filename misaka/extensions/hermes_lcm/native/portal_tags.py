from typing import List
from ..host.profiles import get_conversation_context

def hermes_client_tag() -> str:
    """``client=misaka-v<MAJOR>.<MINOR>.<PATCH>`` ("unknown" if hermes_cli is unimportable)."""
    try:
        from ..host.profiles import HOST_VERSION as __version__
    except Exception:
        __version__ = "unknown"
    return f"client=misaka-v{__version__}"


def conversation_tag(session_id: str) -> str:
    """``conversation=<session_id>`` — high-cardinality, so only appended when a
    session id is actually available, never in the always-on base set."""
    return f"conversation={session_id}"


def nous_portal_tags(session_id: str | None = None) -> List[str]:
    """Fresh list of the canonical Nous Portal tags.

    The ambient conversation context (lineage ROOT id) wins over the explicit
    ``session_id``, a fallback for callers outside any agent turn.
    """
    tags = ["product=misaka", hermes_client_tag()]
    effective = get_conversation_context() or session_id
    if effective:
        tags.append(conversation_tag(effective))
    return tags

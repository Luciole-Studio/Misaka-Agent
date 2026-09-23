"""Research inherits the session's tools; the driver owns ordinary Board mutations."""

DRIVER_OWNED_TOOLS = frozenset({
    "misaka_card", "misaka_dispatch", "misaka_sister", "misaka_card_link",
    "misaka_card_request_review", "misaka_card_review", "misaka_card_unblock",
    "misaka_card_requeue", "misaka_card_delete",
})


def research_tools(names):
    """Subtract workflow entry points without granting or reordering capabilities."""
    return tuple(name for name in dict.fromkeys(names) if name not in DRIVER_OWNED_TOOLS)

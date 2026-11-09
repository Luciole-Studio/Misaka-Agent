"""Research inherits the session's tools; the driver owns ordinary Board mutations."""

from misaka.core.platform.vocabulary import DRIVER_OWNED_TOOLS


def research_tools(names):
    """Subtract workflow entry points without granting or reordering capabilities."""
    return tuple(name for name in dict.fromkeys(names) if name not in DRIVER_OWNED_TOOLS)

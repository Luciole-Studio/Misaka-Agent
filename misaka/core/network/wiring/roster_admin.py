"""``/create`` and ``/remove``: only Last Order grows or prunes the Sister roster."""
SESSION_KINDS = {"foreground", "dm"}
ROLES = {"last_order"}


def part(spec):
    from misaka.core.network import roster
    return roster.RosterPart()

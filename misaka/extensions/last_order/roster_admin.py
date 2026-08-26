"""``/create`` and ``/remove``: only Last Order grows or prunes the Sister roster."""
SESSION_KINDS = {"foreground", "dm"}


def activate(spec):
    from misaka.network import roster
    return roster.register

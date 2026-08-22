"""Startup-screen resource sections registered by extensions.

Rendered through the same path as the built-in [Skills]/[Extensions] sections.
Extensions call register("MCPs", collapsed_fn, expanded_fn); each text may be a
string or a callable. Callables are evaluated on every render, so resources that
load asynchronously (e.g. MCP servers) can show "connecting" first and switch to
the real list once connected.
"""

SECTIONS: list[dict] = []


def register(name: str, collapsed, expanded=None) -> None:
    for s in SECTIONS:
        if s["name"] == name:
            s.update(collapsed=collapsed, expanded=expanded)
            return
    SECTIONS.append({"name": name, "collapsed": collapsed, "expanded": expanded})


def unregister(name: str) -> None:
    SECTIONS[:] = [s for s in SECTIONS if s["name"] != name]


def resolve(value) -> str:
    if callable(value):
        try:
            return str(value() or "")
        except Exception:  # noqa: BLE001 - a broken section must not break the startup screen
            return ""
    return str(value or "")

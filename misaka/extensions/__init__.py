"""Bundled extensions: each package in this folder is one, found by scanning the folder.

This is what Pi's ``src/extensions/`` holds -- what ships in the package and reaches a
session through the extension API alone -- with one thing Pi does not have: roles. A module
takes part by defining ``activate(spec) -> register | None``: ``register(harn)`` installs the
extension into the harness, ``None`` skips it for this session. It qualifies itself the way
core's entries do (:mod:`misaka.core.wiring`): ``ROLES = {"last_order" | "sisters", ...}``
(the default is both) and ``SESSION_KINDS = {...}`` (the default is every kind except
``bare``). A module may override its descriptor name with ``EXTENSION_NAME`` and hide it
with ``HIDDEN``. A module may also offer a top-level command, ``misaka <COMMAND> ...``, by
defining ``COMMAND`` and ``command(argv)``; the first line of that function's docstring is
its help.

Nothing in ``misaka.core`` imports this package. The process entry hands ``discover``
to ``misaka.core.wiring.bundled`` (``misaka.cli.bootstrap``), the way Pi's ``main.ts``
composes ``builtInExtensions`` ahead of everything else, and core calls it with the
session's spec. The engine-level extension protocol stays in :mod:`misaka.core.extensions`.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from misaka.core.wiring import DEFAULT_KINDS, ROLE_KEYS, inline, role_key

# pi's `builtInExtensions`, in its order; misaka's own bundled extensions follow, by name.
_PI_BUILTINS = ("llama", "codemode", "tool_search", "mcp")


def _order(name: str) -> tuple[int, str]:
    return (_PI_BUILTINS.index(name), "") if name in _PI_BUILTINS else (len(_PI_BUILTINS), name)


def _modules():
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: _order(i.name)):
        if not info.name.startswith("_"):
            yield importlib.import_module(f"{__name__}.{info.name}")


def discover(spec) -> list[dict[str, Any]]:
    """Return the inline extensions for one session, by role and session kind."""
    role = role_key(spec)
    kind = "bare" if role == "last_order" and spec.kind == "beast" else spec.kind  # a budget-less Last Order runs tool-less
    out: list[dict[str, Any]] = []
    for mod in _modules():
        activate = getattr(mod, "activate", None)
        if activate is None or kind not in getattr(mod, "SESSION_KINDS", DEFAULT_KINDS):
            continue
        if role not in getattr(mod, "ROLES", ROLE_KEYS):
            continue
        register = activate(spec)
        if register is not None:
            out.append(
                inline(
                    getattr(mod, "EXTENSION_NAME", mod.__name__.rsplit(".", 1)[-1]),
                    register,
                    hidden=bool(getattr(mod, "HIDDEN", False)),
                    replaceable=bool(getattr(mod, "REPLACEABLE", False)),
                    builtin=bool(getattr(mod, "BUILTIN", False)),
                )
            )
    return out


def commands() -> dict[str, Any]:
    """The top-level commands bundled extensions offer: ``{COMMAND: command}``. Every bundled
    module is imported to look, so the process entry asks only for a name that is not its own."""
    found: dict[str, Any] = {}
    for mod in _modules():
        name, command = getattr(mod, "COMMAND", None), getattr(mod, "command", None)
        if name and callable(command):
            found[name] = command
    return found


__all__ = ["commands", "discover"]

"""Bundled extensions, discovered by folder.

    misaka/extensions/<module>             every role
    misaka/extensions/last_order/<module>  Last Order only
    misaka/extensions/sisters/<module>     every other role (the Sisters)

A module takes part by defining ``activate(spec) -> register | None``: ``register(harn)``
installs the extension into the harness, ``None`` skips it for this session.  A module may
restrict itself to session kinds with ``SESSION_KINDS = {...}``; the default is every kind
except ``bare``.  The engine-level extension protocol stays in :mod:`misaka.core.extensions`.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from typing import Any

from misaka.config import profiles

KINDS = frozenset({"foreground", "dm", "card", "child", "beast", "bare"})
DEFAULT_KINDS = KINDS - {"bare"}
_ROLE_DIRS = ("last_order", "sisters")


def inline(name: str, factory: Callable[[Any], Any]) -> dict[str, Any]:
    """Build the named inline-extension descriptor used by Pi's resource loader."""
    return {"name": name, "factory": factory}


def delegates(spec) -> bool:
    """Whether the session may spawn sub-agents: every role except Last Order."""
    return not profiles.is_last_order(spec.profile_dir)


def _modules(package: str):
    pkg = importlib.import_module(package)
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        if info.name.startswith("_") or (package == __name__ and info.name in _ROLE_DIRS):
            continue
        yield importlib.import_module(f"{package}.{info.name}")


def discover(spec) -> list[dict[str, Any]]:
    """Return the inline extensions for one session, by folder and session kind."""
    last_order = profiles.is_last_order(spec.profile_dir)
    kind = "bare" if last_order and spec.kind == "beast" else spec.kind  # a budget-less Last Order runs tool-less
    out: list[dict[str, Any]] = []
    for package in (__name__, f"{__name__}.{'last_order' if last_order else 'sisters'}"):
        for mod in _modules(package):
            activate = getattr(mod, "activate", None)
            if activate is None or kind not in getattr(mod, "SESSION_KINDS", DEFAULT_KINDS):
                continue
            register = activate(spec)
            if register is not None:
                out.append(inline(mod.__name__.rsplit(".", 1)[-1], register))
    return out


__all__ = ["DEFAULT_KINDS", "KINDS", "delegates", "discover", "inline"]

"""Translated from pi coding-agent's ``src/core/key-id-schema.ts`` (v1.3.0).

MISAKA fork: a TypeBox schema is its JSON schema here (a ``dict``), as in ``modes/interactive/theme/theme_schema.py``.
"""

from __future__ import annotations

import re
from typing import Any

from misaka.ui.tui.keys import Key

modifiers = ("ctrl", "shift", "alt", "super")


def _keyValues() -> list[str]:
    """``Object.values(Key)``'s strings. MISAKA fork: ``Key.return`` is set after the class body (a Python keyword
    cannot be an attribute name in source), so it is put back in its place after ``enter``."""
    values = [value for name, value in vars(Key).items() if not name.startswith("__") and isinstance(value, str)]
    values.remove("return")
    values.insert(values.index("enter") + 1, "return")
    return values


baseKeyPattern = (
    "(?:[a-z0-9]|"
    + "|".join(re.sub(r"[\\^$.*+?()[\]{}|]", lambda match: f"\\{match.group(0)}", value) for value in _keyValues())
    + ")"
)
duplicateModifierPattern = "|".join(f"{modifier}\\+.*{modifier}\\+" for modifier in modifiers)

KeyIdSchema: dict[str, Any] = {
    "type": "string",
    "pattern": (
        f"^(?!.*(?:{duplicateModifierPattern}))(?:(?:{'|'.join(modifiers)})\\+){{0,4}}{baseKeyPattern}$"
    ),
    "description": "Key identifier with optional ctrl, shift, alt, or super modifiers.",
}


def keybindingValueSchema(options: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"anyOf": [KeyIdSchema, {"type": "array", "items": KeyIdSchema}], **(options or {})}


KeybindingValueSchema = keybindingValueSchema()

__all__ = ["KeyIdSchema", "KeybindingValueSchema", "keybindingValueSchema"]

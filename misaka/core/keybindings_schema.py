"""Translated from pi coding-agent's ``src/core/keybindings-schema.ts`` (v1.3.0).

The schema of ``keybindings.json``, for ``scripts/generate_schemas.py``: one optional property per keybinding with its
description, and any other key (an extension's action) taking a keybinding value.

MISAKA fork: a TypeBox schema is its JSON schema here (a ``dict``). It lists MISAKA's ``KEYBINDINGS``, which has no
``tui.altScreen.*`` ids (D1: no fullscreen renderer).
"""

from __future__ import annotations

from typing import Any

from .key_id_schema import KeybindingValueSchema, keybindingValueSchema
from .keybindings import KEYBINDINGS

properties: dict[str, Any] = {
    "$schema": {"type": "string"},
}

for id, definition in KEYBINDINGS.items():
    # An undefined description is no key at all in the JSON TypeBox writes.
    properties[id] = keybindingValueSchema(
        {"description": definition.description} if definition.description is not None else None
    )

# `Type.Object` lists no `required` when every property is optional.
KeybindingsSchema: dict[str, Any] = {
    "type": "object",
    "properties": properties,
    "additionalProperties": KeybindingValueSchema,
}

__all__ = ["KeybindingValueSchema", "KeybindingsSchema"]

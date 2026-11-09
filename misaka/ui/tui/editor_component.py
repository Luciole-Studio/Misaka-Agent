"""Translated from pi-tui's ``editor-component.ts`` (v1.1.0).

MISAKA fork: an optional method (``addToHistory?(text)``) is an attribute that may be ``None`` or a callable, the
nearest a Protocol comes to an optional member.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from misaka.ui.tui.autocomplete import AutocompleteProvider
from misaka.ui.tui.tui import Component


class EditorComponent(Component, Protocol):
    """Interface for custom editor components.

    This allows extensions to provide their own editor implementation
    (e.g., vim mode, emacs mode, custom keybindings) while maintaining
    compatibility with the core application.
    """

    # =========================================================================
    # Core text access (required)
    # =========================================================================

    def getText(self) -> str:
        """Get the current text content"""
        ...

    def setText(self, text: str) -> None:
        """Set the text content"""
        ...

    def handleInput(self, data: str) -> None:
        """Handle raw terminal input (key presses, paste sequences, etc.)"""
        ...

    # =========================================================================
    # Callbacks (required)
    # =========================================================================

    onSubmit: Callable[[str], None] | None
    """Called when user submits (e.g., Enter key)"""

    onChange: Callable[[str], None] | None
    """Called when text changes"""

    # =========================================================================
    # History support (optional)
    # =========================================================================

    addToHistory: Callable[[str], None] | None
    """Add text to history for up/down navigation"""

    # =========================================================================
    # Advanced text manipulation (optional)
    # =========================================================================

    insertTextAtCursor: Callable[[str], None] | None
    """Insert text at current cursor position"""

    getExpandedText: Callable[[], str] | None
    """Get text with any markers expanded (e.g., paste markers).
    Falls back to getText() if not implemented.
    """

    # =========================================================================
    # Autocomplete support (optional)
    # =========================================================================

    setAutocompleteProvider: Callable[[AutocompleteProvider], None] | None
    """Set the autocomplete provider"""

    # =========================================================================
    # Appearance (optional)
    # =========================================================================

    borderColor: Callable[[str], str] | None
    """Border color function"""

    setPaddingX: Callable[[int], None] | None
    """Set horizontal padding"""

    setAutocompleteMaxVisible: Callable[[int], None] | None
    """Set max visible items in autocomplete dropdown"""


__all__ = ["EditorComponent"]

"""Translated from pi's ``modes/interactive/components/login-dialog.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Sequence
from typing import Any

from misaka.ai.auth.types import AuthInfoLink
from misaka.ai.compat.extension_oauth_types import OAuthDeviceCodeInfo
from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import Container, Input, Spacer, Text, getKeybindings
from misaka.utils.js import AbortController, AbortSignal, spawn
from misaka.utils.open_browser import openBrowser

from .auth_url import AuthUrlComponent
from .dynamic_border import DynamicBorder
from .keybinding_hints import keyHint


class LoginDialogComponent(Container):
    """Login dialog component - replaces editor during OAuth login flow"""

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.input.focused = value

    def __init__(
        self,
        tui: Any,
        providerId: str,
        onComplete: Callable[[bool, str | None], None],
        providerNameOverride: str | None = None,
        titleOverride: str | None = None,
    ) -> None:
        super().__init__()
        # The shown sign-in URL, which `app.message.copy` copies.
        self.authUrl: AuthUrlComponent | None = None
        self.abortController = AbortController()
        # MISAKA fork: the Promise resolver/rejecter pair is one asyncio Future, and `_cancelled` remembers a cancel
        # that lands before the next prompt exists (see `_set_future`).
        self._inputFuture: asyncio.Future[str] | None = None
        self._cancelled = False
        # MISAKA fork: holds the running `void this.authUrl.copy()` (asyncio holds tasks weakly).
        self._tasks: set[asyncio.Task[Any]] = set()

        # Focusable implementation - propagate to input for IME cursor positioning
        self._focused = False

        self.tui = tui
        self.onComplete = onComplete

        providerName = providerNameOverride or providerId
        title = titleOverride if titleOverride is not None else f"Login to {providerName}"

        # Top border
        self.addChild(DynamicBorder())

        # Title
        self.addChild(Text(theme.fg("accent", theme.bold(title)), 1, 0))

        # Dynamic content area
        self.contentContainer = Container()
        self.addChild(self.contentContainer)

        # Input (always present, used when needed)
        self.input = Input()

        def onSubmit(_value: str) -> None:
            if self._inputFuture is not None:
                value = self.input.getValue()
                self.replaceInputWithSubmittedText(value)
                if not self._inputFuture.done():
                    self._inputFuture.set_result(value)
                self._inputFuture = None

        self.input.onSubmit = onSubmit

        def onEscape() -> None:
            self.cancel()

        self.input.onEscape = onEscape

        # Bottom border
        self.addChild(DynamicBorder())

    @property
    def signal(self) -> AbortSignal:
        return self.abortController.signal

    def replaceInputWithSubmittedText(self, value: str) -> None:
        self.contentContainer.children = [
            Text(f"> {value}", 0, 0) if child is self.input else child for child in self.contentContainer.children
        ]

    def _set_future(self) -> asyncio.Future[str]:
        """MISAKA fork: ``new Promise((resolve, reject) => {...})`` for the prompts. A cancel that lands before this
        prompt exists (between "show the URL" and "show the paste box", while the browser opens) rejects it at once;
        otherwise the login would wait on a box the user already dismissed."""
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        if self._cancelled:
            future.set_exception(Exception("Login cancelled"))
            return future
        self._inputFuture = future
        return future

    def cancel(self) -> None:
        self._cancelled = True
        self.abortController.abort()
        if self._inputFuture is not None:
            if not self._inputFuture.done():
                self._inputFuture.set_exception(Exception("Login cancelled"))
            self._inputFuture = None
        self.onComplete(False, "Login cancelled")

    def showAuth(self, url: str, instructions: str | None = None) -> None:
        """Called by onAuth callback - show URL and optional instructions"""
        self.contentContainer.clear()
        self.contentContainer.addChild(Spacer(1))
        self.authUrl = AuthUrlComponent(self.tui, url)
        self.contentContainer.addChild(self.authUrl)

        if instructions:
            self.contentContainer.addChild(Spacer(1))
            self.contentContainer.addChild(Text(theme.fg("warning", instructions), 1, 0))

        openBrowser(url)
        self.tui.requestRender()

    def showDeviceCode(self, info: OAuthDeviceCodeInfo) -> None:
        """Called by onDeviceCode callback - show URL and user code."""
        self.authUrl = None
        self.contentContainer.clear()
        self.contentContainer.addChild(Spacer(1))
        linkedUrl = f"\x1b]8;;{info.verificationUri}\x07{info.verificationUri}\x1b]8;;\x07"
        self.contentContainer.addChild(Text(theme.fg("accent", linkedUrl), 1, 0))

        clickHint = "Cmd+click to open" if sys.platform == "darwin" else "Ctrl+click to open"
        hyperlink = f"\x1b]8;;{info.verificationUri}\x07{clickHint}\x1b]8;;\x07"
        self.contentContainer.addChild(Text(theme.fg("dim", hyperlink), 1, 0))
        self.contentContainer.addChild(Spacer(1))
        self.contentContainer.addChild(Text(theme.fg("warning", f"Enter code: {info.userCode}"), 1, 0))

        self.tui.requestRender()

    def showManualInput(self, prompt: str) -> asyncio.Future[str]:
        """Show input for manual code/URL entry (for callback server providers)"""
        self.input.setValue("")
        self.contentContainer.addChild(Spacer(1))
        self.contentContainer.addChild(Text(theme.fg("dim", prompt), 1, 0))
        self.contentContainer.addChild(self.input)
        self.contentContainer.addChild(Text(f"({keyHint('tui.select.cancel', 'to cancel')})", 1, 0))
        self.tui.requestRender()

        return self._set_future()

    def showPrompt(self, message: str, placeholder: str | None = None) -> asyncio.Future[str]:
        """Called by onPrompt callback - show prompt and wait for input
        Note: Does NOT clear content, appends to existing (preserves URL from showAuth)"""
        self.contentContainer.addChild(Spacer(1))
        self.contentContainer.addChild(Text(theme.fg("text", message), 1, 0))
        if placeholder:
            self.contentContainer.addChild(Text(theme.fg("dim", f"e.g., {placeholder}"), 1, 0))
        self.contentContainer.addChild(self.input)
        self.contentContainer.addChild(
            Text(
                f"({keyHint('tui.select.cancel', 'to cancel,')} {keyHint('tui.select.confirm', 'to submit')})",
                1,
                0,
            )
        )

        self.input.setValue("")
        self.tui.requestRender()

        return self._set_future()

    def showDetails(self, lines: list[str]) -> None:
        """Show informational text before another login step."""
        self.authUrl = None
        self.contentContainer.clear()
        self.contentContainer.addChild(Spacer(1))
        for line in lines:
            self.contentContainer.addChild(Text(line, 1, 0))
        self.tui.requestRender()

    def showInfo(self, message: str, links: Sequence[AuthInfoLink] = (), showCloseHint: bool = False) -> None:
        """Show provider-owned information and links without starting an auth callback flow."""
        self.contentContainer.addChild(Spacer(1))
        self.contentContainer.addChild(Text(theme.fg("text", message), 1, 0))
        for link in links:
            text = f"{link.label}: {link.url}" if link.label else link.url
            hyperlink = f"\x1b]8;;{link.url}\x07{text}\x1b]8;;\x07"
            self.contentContainer.addChild(Text(theme.fg("accent", hyperlink), 1, 0))
        if showCloseHint:
            self.contentContainer.addChild(Spacer(1))
            self.contentContainer.addChild(Text(f"({keyHint('tui.select.cancel', 'to close')})", 1, 0))
        self.tui.requestRender()

    def showWaiting(self, message: str) -> None:
        """Show waiting message (for polling flows like GitHub Copilot)"""
        self.contentContainer.addChild(Spacer(1))
        self.contentContainer.addChild(Text(theme.fg("dim", message), 1, 0))
        self.contentContainer.addChild(Text(f"({keyHint('tui.select.cancel', 'to cancel')})", 1, 0))
        self.tui.requestRender()

    def showProgress(self, message: str) -> None:
        """Called by onProgress callback"""
        self.contentContainer.addChild(Text(theme.fg("dim", message), 1, 0))
        self.tui.requestRender()

    def handleInput(self, data: str) -> None:
        kb = getKeybindings()

        if kb.matches(data, "tui.select.cancel"):
            self.cancel()
            return
        if self.authUrl and kb.matches(data, "app.message.copy"):
            spawn(self.authUrl.copy(), self._tasks)
            return

        # Pass to input
        self.input.handleInput(data)


__all__ = ["LoginDialogComponent"]

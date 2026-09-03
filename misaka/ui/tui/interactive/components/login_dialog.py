"""Interactive login dialog used during OAuth flows."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from misaka.ai.utils.oauth import OAuthDeviceCodeInfo, getOAuthProviders
from misaka.ui.tui import Container, Input, Spacer, Text, getKeybindings
from misaka.ui.tui.components import AbortController
from misaka.ui.tui.interactive.theme.theme import theme

from .dynamic_border import DynamicBorder
from .keybinding_hints import keyHint


def _hyperlink(url: str, text: str | None = None) -> str:
    label = text or url
    return f"\x1b]8;;{url}\x07{label}\x1b]8;;\x07"


_browser_openers: list[subprocess.Popen] = []

class LoginDialogComponent(Container):
    def __init__(
        self,
        tui: Any,
        providerId: str,
        onComplete: Callable[[bool, str | None], None],
        providerNameOverride: str | None = None,
        titleOverride: str | None = None,
    ) -> None:
        super().__init__()
        self._focused = False
        self.tui = tui
        self.onComplete = onComplete
        self.abortController = AbortController()
        self._inputFuture: asyncio.Future[str] | None = None
        self._cancelled = False

        providerInfo = next((provider for provider in getOAuthProviders() if provider.id == providerId), None)
        providerName = providerNameOverride or getattr(providerInfo, "name", None)
        if providerName is None:
            providerName = providerId
        title = titleOverride if titleOverride is not None else f"Login to {providerName}"

        self.addChild(DynamicBorder())
        self.addChild(Text(theme.fg("accent", theme.bold(title)), 1, 0))

        self.contentContainer = Container()
        self.addChild(self.contentContainer)

        self.input = Input()
        self.input.onSubmit = self._resolve_input
        self.input.onEscape = self.cancel

        self.addChild(DynamicBorder())

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.input.focused = value

    @property
    def signal(self):
        return self.abortController.signal

    def _request_render(self) -> None:
        request_render = getattr(self.tui, "requestRender", None)
        if callable(request_render):
            request_render()

    def _set_future(self) -> asyncio.Future[str]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        # A cancel that lands before this step exists used to fall on the floor: `cancel`
        # rejects whatever future is current, and between "show the URL" and "show the
        # paste box" -- one event-loop hop, but the browser opens in it -- there is none.
        # The login then waited on a box the user had already dismissed.
        if self._cancelled:
            future.set_exception(Exception("Login cancelled"))
            return future
        self._inputFuture = future
        return future

    def _replace_input_with_submitted_text(self, value: str) -> None:
        # The Input instance is reused for every step of a login, so leaving it in the
        # container means step two renders a second, value-synced copy of the same box --
        # and the code the user just pasted stays on screen and editable.
        self.contentContainer.children = [
            Text(f"> {value}", 0, 0) if child is self.input else child
            for child in self.contentContainer.children
        ]

    def _resolve_input(self, value: str) -> None:
        if self._inputFuture is not None and not self._inputFuture.done():
            self._replace_input_with_submitted_text(value)
            self._inputFuture.set_result(value)
        self._inputFuture = None

    def _reject_input(self, error: Exception) -> None:
        if self._inputFuture is not None and not self._inputFuture.done():
            self._inputFuture.set_exception(error)
        self._inputFuture = None

    def cancel(self) -> None:
        self._cancelled = True
        self.abortController.abort()
        self._reject_input(Exception("Login cancelled"))
        self.onComplete(False, "Login cancelled")

    def showAuth(self, url: str, instructions: str | None = None) -> None:
        self.contentContainer.clear()
        self.contentContainer.addChild(Spacer(1))
        self.contentContainer.addChild(Text(theme.fg("accent", _hyperlink(url)), 1, 0))
        clickHint = "Cmd+click to open" if sys.platform == "darwin" else "Ctrl+click to open"
        self.contentContainer.addChild(Text(theme.fg("dim", _hyperlink(url, clickHint)), 1, 0))
        if instructions:
            self.contentContainer.addChild(Spacer(1))
            self.contentContainer.addChild(Text(theme.fg("warning", instructions), 1, 0))
        self.openUrl(url)
        self._request_render()

    def showDeviceCode(self, info: OAuthDeviceCodeInfo) -> None:
        self.contentContainer.clear()
        self.contentContainer.addChild(Spacer(1))
        self.contentContainer.addChild(Text(theme.fg("accent", _hyperlink(info.verificationUri)), 1, 0))
        clickHint = "Cmd+click to open" if sys.platform == "darwin" else "Ctrl+click to open"
        self.contentContainer.addChild(Text(theme.fg("dim", _hyperlink(info.verificationUri, clickHint)), 1, 0))
        self.contentContainer.addChild(Spacer(1))
        self.contentContainer.addChild(Text(theme.fg("warning", f"Enter code: {info.userCode}"), 1, 0))
        self.openUrl(info.verificationUri)
        self._request_render()

    def openUrl(self, url: str) -> None:
        try:
            if sys.platform == "darwin":
                command = ["open", url]
            elif sys.platform == "win32":
                # Never `cmd /c start`: cmd.exe re-parses &, |, ^ ... before `start` runs,
                # so a provider-supplied URL like `https://host/cb?x=1&calc.exe` would
                # execute. rundll32 takes the target as a single argument, unparsed.
                command = ["rundll32", "url.dll,FileProtocolHandler", url]
            else:
                command = ["xdg-open", url]
            with open(os.devnull, "wb") as sink:
                # Fire-and-forget, but reaped: an un-waited child stays a zombie for the
                # life of the process, and login can be retried any number of times.
                _browser_openers[:] = [opener for opener in _browser_openers if opener.poll() is None]
                _browser_openers.append(subprocess.Popen(command, stdout=sink, stderr=sink, stdin=sink))
        except Exception:  # noqa: BLE001
            return

    def showManualInput(self, prompt: str):
        self.input.setValue("")
        self.contentContainer.addChild(Spacer(1))
        self.contentContainer.addChild(Text(theme.fg("dim", prompt), 1, 0))
        self.contentContainer.addChild(self.input)
        self.contentContainer.addChild(Text(f"({keyHint('tui.select.cancel', 'to cancel')})", 1, 0))
        self._request_render()
        return self._set_future()

    def showPrompt(self, message: str, placeholder: str | None = None):
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
        self._request_render()
        return self._set_future()

    def showInfo(self, lines: list[str]) -> None:
        self.contentContainer.clear()
        self.contentContainer.addChild(Spacer(1))
        for line in lines:
            self.contentContainer.addChild(Text(line, 1, 0))
        self.contentContainer.addChild(Spacer(1))
        self.contentContainer.addChild(Text(f"({keyHint('tui.select.cancel', 'to close')})", 1, 0))
        self._request_render()

    def showWaiting(self, message: str) -> None:
        self.contentContainer.addChild(Spacer(1))
        self.contentContainer.addChild(Text(theme.fg("dim", message), 1, 0))
        self.contentContainer.addChild(Text(f"({keyHint('tui.select.cancel', 'to cancel')})", 1, 0))
        self._request_render()

    def showProgress(self, message: str) -> None:
        self.contentContainer.addChild(Text(theme.fg("dim", message), 1, 0))
        self._request_render()

    def handleInput(self, data: str) -> None:
        kb = getKeybindings()
        if kb.matches(data, "tui.select.cancel"):
            self.cancel()
            return
        self.input.handleInput(data)


__all__ = ["LoginDialogComponent"]

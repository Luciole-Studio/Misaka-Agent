"""The ``/mcp`` manager view: menus that rebuild while servers connect, a read-only status screen, and
the sign-in screen that accepts a pasted redirect URL.

Translated from pi's ``coding-agent/src/extensions/mcp/ui.ts``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, NotRequired, Protocol, TypedDict

from misaka.modes.interactive.components.auth_url import AuthUrlComponent
from misaka.modes.interactive.components.dynamic_border import DynamicBorder
from misaka.modes.interactive.components.keybinding_hints import keyHint
from misaka.modes.interactive.theme.theme import getSelectListTheme
from misaka.ui.tui import (
    Container,
    Input,
    SelectItem,
    SelectList,
    Spacer,
    Text,
    truncateToWidth,
    visibleWidth,
)
from misaka.utils.js import addAbortListener, errorMessage, jsTrim, spawn, startTask
from misaka.utils.values import signal_aborted

if TYPE_CHECKING:
    from misaka.core.extensions.types import ExtensionCommandContext
    from misaka.core.keybindings import KeybindingsManager
    from misaka.modes.interactive.theme.theme import Theme
    from misaka.ui.tui.tui import TUI, Component, Focusable


class McpMenu(TypedDict):
    title: str
    # Shown below the title.
    details: NotRequired[str | None]
    # Shown below the details in the error color.
    error: NotRequired[str | None]
    items: list[SelectItem]
    # Shown when there are no items.
    empty: NotRequired[str | None]
    # Value of the item selected when the menu opens.
    selected: NotRequired[str | None]
    # What the confirm key does, for the key hint.
    confirmLabel: str
    # What the cancel key does, for the key hint.
    cancelLabel: str


class McpUi(Protocol):
    def menu(
        self, build: Callable[[], McpMenu], subscribe: Callable[[Callable[[], None]], Callable[[], None]] | None = None
    ) -> Awaitable[str | None]:
        """Show a menu and resolve to the chosen item's value, or ``None`` when cancelled. ``subscribe``
        rebuilds the menu on every change, keeping the selected item."""
        ...

    def status(self, title: str, message: str, onCancel: Callable[[], None] | None = None) -> None:
        """Show a message while an operation runs. With ``onCancel``, the cancel key calls it."""
        ...

    def redirectUrl(self, title: str, authorizationUrl: str, signal: Any) -> Awaitable[str | None]:
        """Show the authorization URL and wait for a pasted redirect URL. Resolves to ``None`` when
        cancelled or when ``signal`` aborts (the browser reached the callback)."""
        ...


def frame(theme: Theme, title: str, body: list[Component], footer: str | None = None) -> Container:
    container = Container()
    container.addChild(DynamicBorder(lambda text: theme.fg("accent", text)))
    container.addChild(Text(theme.fg("accent", theme.bold(title)), 1, 0))
    for child in body:
        container.addChild(child)
    if footer:
        container.addChild(Spacer(1))
        container.addChild(Text(theme.fg("dim", footer), 1, 0))
    container.addChild(DynamicBorder(lambda text: theme.fg("accent", text)))
    return container


MAX_VISIBLE_ITEMS = 12


class McpManagerView:
    def __init__(self, tui: TUI, theme: Theme, keybindings: KeybindingsManager) -> None:
        self.tui = tui
        self.theme = theme
        self.keybindings = keybindings
        self.content: Container = frame(theme, "MCP servers", [Text(theme.fg("muted", "Loading…"), 1, 1)])
        self.inputHandler: Callable[[str], None] | None = None
        self.inputTarget: Focusable | None = None
        self._focused = False
        # MISAKA fork: asyncio holds tasks weakly; the running `void link.copy()` calls live here.
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        if self.inputTarget is not None:
            self.inputTarget.focused = value

    def setContent(
        self, content: Container, inputHandler: Callable[[str], None] | None = None, inputTarget: Focusable | None = None
    ) -> None:
        if self.inputTarget is not None:
            self.inputTarget.focused = False
        self.content = content
        self.inputHandler = inputHandler
        self.inputTarget = inputTarget
        if self.inputTarget is not None:
            self.inputTarget.focused = self._focused
        self.tui.requestRender()

    def menu(
        self, build: Callable[[], McpMenu], subscribe: Callable[[Callable[[], None]], Callable[[], None]] | None = None
    ) -> asyncio.Future[str | None]:
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        unsubscribe: Callable[[], None] | None = None
        settled = False

        def finish(value: str | None) -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            if unsubscribe is not None:
                unsubscribe()
            if not future.done():
                future.set_result(value)

        selected: str | None = None

        def render() -> None:
            nonlocal selected
            menu = build()
            wanted = selected if selected is not None else menu.get("selected")
            body: list[Component] = []
            if menu.get("details"):
                body.append(Text(self.theme.fg("muted", menu["details"]), 1, 0))  # type: ignore[arg-type]
            if menu.get("error"):
                body.append(Text(self.theme.fg("error", menu["error"]), 1, 0))  # type: ignore[arg-type]
            body.append(Spacer(1))
            footer = (
                f"{keyHint('tui.select.confirm', menu['confirmLabel'])} • {keyHint('tui.select.cancel', menu['cancelLabel'])}"
            )
            if len(menu["items"]) == 0:
                empty = menu.get("empty")
                body.append(Text(self.theme.fg("muted", empty if empty is not None else "Nothing to show."), 1, 0))

                def onEmptyInput(data: str) -> None:
                    if self.keybindings.matches(data, "tui.select.cancel"):
                        finish(None)

                self.setContent(
                    frame(self.theme, menu["title"], body, keyHint("tui.select.cancel", menu["cancelLabel"])),
                    onEmptyInput,
                )
                return
            list = SelectList(menu["items"], min(len(menu["items"]), MAX_VISIBLE_ITEMS), getSelectListTheme())
            index = next((position for position, item in enumerate(menu["items"]) if item.value == wanted), -1)
            if index != -1:
                list.setSelectedIndex(index)
            current = list.getSelectedItem()
            selected = current.value if current is not None else None

            def onSelectionChange(item: SelectItem) -> None:
                nonlocal selected
                selected = item.value

            list.onSelectionChange = onSelectionChange
            list.onSelect = lambda item: finish(item.value)
            list.onCancel = lambda: finish(None)
            body.append(list)
            self.setContent(frame(self.theme, menu["title"], body, footer), lambda data: list.handleInput(data))

        # `new Promise(executor)`: an exception the executor throws rejects the promise.
        try:
            render()
            if subscribe is not None:
                unsubscribe = subscribe(lambda: None if settled else render())
        except Exception as error:  # noqa: BLE001 - becomes the rejection, as in a promise executor
            if not future.done():
                future.set_exception(error)
        return future

    def status(self, title: str, message: str, onCancel: Callable[[], None] | None = None) -> None:
        body: list[Component] = [Spacer(1), Text(self.theme.fg("muted", message), 1, 0)]
        if onCancel is None:
            self.setContent(frame(self.theme, title, body))
            return

        def onInput(data: str) -> None:
            if self.keybindings.matches(data, "tui.select.cancel"):
                onCancel()

        self.setContent(frame(self.theme, title, body, keyHint("tui.select.cancel", "cancel")), onInput)

    def redirectUrl(self, title: str, authorizationUrl: str, signal: Any) -> asyncio.Future[str | None]:
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        settled = False
        removeAbortListener: Callable[[], None] = lambda: None

        def finish(value: str | None) -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            removeAbortListener()
            if not future.done():
                future.set_result(value)

        def onAbort() -> None:
            finish(None)

        # `new Promise(executor)`: an exception the executor throws rejects the promise.
        try:
            if signal_aborted(signal):
                finish(None)
                return future
            removeAbortListener = addAbortListener(signal, onAbort)
            input = Input()
            link = AuthUrlComponent(self.tui, authorizationUrl)
            body: list[Component] = [
                Spacer(1),
                Text(self.theme.fg("muted", "Approve access in your browser. If it did not open, visit:"), 1, 0),
                link,
                Spacer(1),
                Text(
                    self.theme.fg("muted", "If the browser runs on another machine, paste the URL it was redirected to:"),
                    1,
                    0,
                ),
                input,
            ]

            def onInput(data: str) -> None:
                if self.keybindings.matches(data, "tui.select.confirm"):
                    value = jsTrim(input.getValue())
                    if value:
                        finish(value)
                    return
                if self.keybindings.matches(data, "tui.select.cancel"):
                    finish(None)
                    return
                if self.keybindings.matches(data, "app.message.copy"):
                    spawn(link.copy(), self._tasks)
                    return
                input.handleInput(data)

            self.setContent(
                frame(
                    self.theme,
                    title,
                    body,
                    f"{keyHint('tui.select.confirm', 'submit')} • {keyHint('tui.select.cancel', 'cancel')}",
                ),
                onInput,
                input,
            )
        except Exception as error:  # noqa: BLE001 - becomes the rejection, as in a promise executor
            if not future.done():
                future.set_exception(error)
        return future

    def handleInput(self, data: str) -> None:
        if self.inputHandler is not None:
            self.inputHandler(data)
        self.tui.requestRender()

    def render(self, width: int) -> list[str]:
        return [
            truncateToWidth(line, width, "") if visibleWidth(line) > width else line
            for line in self.content.render(width)
        ]

    def invalidate(self) -> None:
        self.content.invalidate()


async def showMcpManager(ctx: ExtensionCommandContext, manage: Callable[[McpUi], Awaitable[None]]) -> None:
    """Run ``manage`` in the manager view until it returns."""
    # MISAKA fork: asyncio holds tasks weakly; the running `manage` is held until it finishes.
    tasks: set[asyncio.Future[None]] = set()

    def factory(tui: TUI, theme: Theme, keybindings: KeybindingsManager, done: Callable[..., None]) -> McpManagerView:
        view = McpManagerView(tui, theme, keybindings)
        managed = startTask(manage(view))
        tasks.add(managed)

        def settle(finished: asyncio.Future[None]) -> None:
            tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                ctx.ui.notify(errorMessage(finished.exception()), "error")
            done(None)

        managed.add_done_callback(settle)
        return view

    await ctx.ui.custom(factory)


__all__ = ["McpManagerView", "McpMenu", "McpUi", "showMcpManager"]

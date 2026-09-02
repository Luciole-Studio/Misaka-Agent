"""Interactive llama.cpp model manager UI."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from misaka.ai.models_runtime import AbortController
from misaka.ui.tui import (
    Container,
    Input,
    SelectItem,
    SelectList,
    SelectListLayoutOptions,
    SelectListTheme,
    Spacer,
    Text,
    fuzzyFilter,
    truncateToWidth,
    visibleWidth,
)
from misaka.ui.tui.interactive.components.dynamic_border import DynamicBorder
from misaka.ui.tui.interactive.components.keybinding_hints import keyHint

from .client import LlamaModelInfo, LlamaProgress
from .huggingface import HuggingFaceModel

_DOWNLOAD_VALUE = "\0download"


@dataclass(slots=True)
class LlamaManagerAction:
    type: Literal["model", "download", "close"]
    model: LlamaModelInfo | None = None


@dataclass(slots=True)
class ProgressState(LlamaProgress):
    title: str = ""
    model: str = ""


class LlamaUi(Protocol):
    async def showModels(
        self, serverUrl: str, models: list[LlamaModelInfo]
    ) -> LlamaManagerAction: ...

    async def select(self, title: str, options: list[str]) -> str | None: ...

    async def confirm(self, title: str, message: str) -> bool: ...

    async def connectionError(
        self, serverUrl: str, message: str
    ) -> Literal["retry", "close"]: ...

    async def searchModels(
        self,
        search: Callable[[str, Any], Awaitable[list[HuggingFaceModel]]],
    ) -> str | None: ...

    def showStatus(self, title: str, message: str) -> None: ...

    async def progress(self, state: ProgressState) -> None: ...

    def updateProgress(self, state: ProgressState) -> None: ...


def _context_label(model: LlamaModelInfo) -> str | None:
    context = None
    if model.meta is not None:
        context = (
            model.meta.n_ctx if model.meta.n_ctx is not None else model.meta.n_ctx_train
        )
    if context:
        return f"{round(context / 1000)}k" if context >= 1000 else str(context)
    args = model.status.args or []
    for index in range(len(args) - 1):
        if args[index] not in {"--ctx-size", "-c", "-ctx"}:
            continue
        try:
            value = int(args[index + 1])
        except ValueError:
            continue
        if value > 0:
            return f"{round(value / 1000)}k" if value >= 1000 else str(value)
    return None


def _model_description(model: LlamaModelInfo) -> str:
    details: list[str] = []
    loaded = model.status.value in {"loaded", "sleeping"}
    if loaded:
        details.append("loaded")
    elif model.status.value != "unloaded":
        details.append(model.status.value)
    context = _context_label(model) if loaded else None
    if context:
        details.append(f"{context} context")
    return " · ".join(details)


def _select_theme(theme: Any) -> SelectListTheme:
    return SelectListTheme(
        selectedPrefix=lambda text: theme.fg("accent", text),
        selectedText=lambda text: theme.fg("accent", text),
        description=lambda text: theme.fg("muted", text),
        scrollInfo=lambda text: theme.fg("dim", text),
        noMatch=lambda text: theme.fg("warning", text),
    )


def _frame(
    theme: Any, title: str, body: list[Any], footer: str | None = None
) -> Container:
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


def _compact_count(value: int) -> str:
    if value >= 1_000_000:
        return (
            f"{value / 1_000_000:.0f}M"
            if value >= 10_000_000
            else f"{value / 1_000_000:.1f}M"
        )
    if value >= 1_000:
        return f"{value / 1_000:.0f}k" if value >= 100_000 else f"{value / 1_000:.1f}k"
    return str(value)


class _HuggingFaceSearch(Container):
    def __init__(
        self,
        tui: Any,
        theme: Any,
        keybindings: Any,
        search: Callable[[str, Any], Awaitable[list[HuggingFaceModel]]],
        cache: dict[str, list[HuggingFaceModel]],
        onSelectModel: Callable[[str | None], None],
    ) -> None:
        super().__init__()
        self.tui = tui
        self.theme = theme
        self.keybindings = keybindings
        self.search = search
        self.cache = cache
        self.onSelectModel = onSelectModel
        self.input = Input()
        self.resultsContainer = Container()
        self.results: list[HuggingFaceModel] = []
        self.filteredResults: list[HuggingFaceModel] = []
        self.selectedIndex = 0
        self.query = ""
        self.status = "Type at least 2 characters"
        self._searchTask: asyncio.Task[None] | None = None
        self._request: AbortController | None = None
        self._closed = False
        self._focused = False
        self.addChild(
            Text(theme.fg("dim", "Model name or owner/repository[:quant]"), 1, 0)
        )
        self.addChild(self.input)
        self.addChild(Spacer(1))
        self.addChild(self.resultsContainer)
        self._update_results()

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.input.focused = value

    def _update_results(self) -> None:
        self.resultsContainer.clear()
        maximum = 10
        start = max(
            0,
            min(
                self.selectedIndex - maximum // 2,
                len(self.filteredResults) - maximum,
            ),
        )
        end = min(start + maximum, len(self.filteredResults))
        for index in range(start, end):
            model = self.filteredResults[index]
            prefix = "→ " if index == self.selectedIndex else "  "
            details = f"{_compact_count(model.downloads)} downloads"
            text = (
                self.theme.fg("accent", f"{prefix}{model.id}  {details}")
                if index == self.selectedIndex
                else f"{prefix}{model.id}{self.theme.fg('muted', f'  {details}')}"
            )
            self.resultsContainer.addChild(Text(text, 0, 0))
        if start > 0 or end < len(self.filteredResults):
            self.resultsContainer.addChild(
                Text(
                    self.theme.fg(
                        "dim",
                        f"  ({self.selectedIndex + 1}/{len(self.filteredResults)})",
                    ),
                    0,
                    0,
                )
            )
        if not self.filteredResults or self.status == "Searching Hugging Face…":
            self.resultsContainer.addChild(
                Text(self.theme.fg("dim", f"  {self.status}"), 0, 0)
            )
        self.tui.requestRender()

    def _filter_results(self) -> None:
        if self.query:
            matches = {
                model.id
                for model in fuzzyFilter(self.results, self.query, lambda item: item.id)
            }
            self.filteredResults = [
                model for model in self.results if model.id in matches
            ]
        else:
            self.filteredResults = self.results
        self.selectedIndex = min(
            self.selectedIndex, max(0, len(self.filteredResults) - 1)
        )
        self._update_results()

    def _schedule_search(self) -> None:
        if self._searchTask is not None:
            self._searchTask.cancel()
        if self._request is not None:
            self._request.abort()
            self._request = None
        if len(self.query) < 2:
            self.status = "Type at least 2 characters"
            self._filter_results()
            return
        cached = self.cache.get(self.query.lower())
        if cached is not None:
            self.results = cached
            self.status = "No GGUF models found" if not cached else ""
            self._filter_results()
            return
        self.status = "Searching Hugging Face…"
        self._filter_results()
        self._searchTask = asyncio.create_task(self._run_search(self.query))

    async def _run_search(self, query: str) -> None:
        try:
            await asyncio.sleep(0.5)
            request = AbortController()
            self._request = request
            results = await self.search(query, request)
            self.cache[query.lower()] = results
            if self._closed or request.aborted or self.query != query:
                return
            self.results = results
            self.selectedIndex = 0
            self.status = "No GGUF models found" if not results else ""
            self._filter_results()
        except asyncio.CancelledError:
            return
        except Exception as error:  # noqa: BLE001 - the search view renders the error
            if self._closed or self.query != query:
                return
            self.results = []
            self.status = str(error)
            self._filter_results()
        finally:
            self._request = None

    def _close(self, model: str | None) -> None:
        if self._closed:
            return
        self._closed = True
        if self._searchTask is not None:
            self._searchTask.cancel()
        if self._request is not None:
            self._request.abort()
        self.onSelectModel(model)

    def handleInput(self, data: str) -> None:
        if self.keybindings.matches(data, "tui.select.up"):
            if self.filteredResults:
                self.selectedIndex = (
                    len(self.filteredResults) - 1
                    if self.selectedIndex == 0
                    else self.selectedIndex - 1
                )
                self._update_results()
            return
        if self.keybindings.matches(data, "tui.select.down"):
            if self.filteredResults:
                self.selectedIndex = (
                    0
                    if self.selectedIndex == len(self.filteredResults) - 1
                    else self.selectedIndex + 1
                )
                self._update_results()
            return
        if self.keybindings.matches(data, "tui.select.confirm"):
            exact = (
                self.query
                if re.fullmatch(r"[^/\s]+/[^:\s]+(?::[^\s:]+)?", self.query)
                else None
            )
            selected = exact or (
                self.filteredResults[self.selectedIndex].id
                if self.filteredResults
                else None
            )
            if selected:
                self._close(selected)
            return
        if self.keybindings.matches(data, "tui.select.cancel"):
            self._close(None)
            return
        self.input.handleInput(data)
        query = self.input.getValue().strip()
        if query != self.query:
            self.query = query
            self._schedule_search()

    def dispose(self) -> None:
        self._close(None)


class _LlamaView:
    def __init__(self, tui: Any, theme: Any, keybindings: Any) -> None:
        self.tui = tui
        self.theme = theme
        self.keybindings = keybindings
        self.searchCache: dict[str, list[HuggingFaceModel]] = {}
        self.content = _frame(
            theme,
            "llama.cpp models",
            [Text(theme.fg("muted", "Loading…"), 1, 1)],
        )
        self.inputHandler: Any = None
        self.inputTarget: Any = None
        self._progressFuture: asyncio.Future[None] | None = None
        self._showingProgress = False
        self._focused = False

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        if self.inputTarget is not None:
            self.inputTarget.focused = value

    def _set_content(
        self, content: Container, inputHandler: Any = None, inputTarget: Any = None
    ) -> None:
        if self.inputTarget is not None:
            self.inputTarget.focused = False
        self._progressFuture = None
        self._showingProgress = False
        self.content = content
        self.inputHandler = inputHandler
        self.inputTarget = inputTarget
        if self.inputTarget is not None:
            self.inputTarget.focused = self._focused
        self.tui.requestRender()

    async def showModels(
        self, serverUrl: str, models: list[LlamaModelInfo]
    ) -> LlamaManagerAction:
        sorted_models = sorted(
            models,
            key=lambda model: (
                0 if model.status.value == "loaded" else 1,
                model.id,
            ),
        )
        by_id = {model.id: model for model in sorted_models}
        items = [
            SelectItem(
                value=model.id,
                label=model.id,
                description=_model_description(model),
            )
            for model in sorted_models
        ] + [
            SelectItem(
                value=_DOWNLOAD_VALUE,
                label="Download model…",
                description="Hugging Face owner/repository[:quant]",
            )
        ]
        future: asyncio.Future[LlamaManagerAction] = (
            asyncio.get_running_loop().create_future()
        )
        select = SelectList(
            items,
            min(len(items), 12),
            _select_theme(self.theme),
            SelectListLayoutOptions(minPrimaryColumnWidth=36, maxPrimaryColumnWidth=56),
        )

        def choose(item: SelectItem) -> None:
            if future.done():
                return
            if item.value == _DOWNLOAD_VALUE:
                future.set_result(LlamaManagerAction(type="download"))
            elif item.value in by_id:
                future.set_result(
                    LlamaManagerAction(type="model", model=by_id[item.value])
                )

        select.onSelect = choose
        select.onCancel = lambda: (
            None
            if future.done()
            else future.set_result(LlamaManagerAction(type="close"))
        )
        self._set_content(
            _frame(
                self.theme,
                "llama.cpp models",
                [Text(self.theme.fg("dim", serverUrl), 1, 0), Spacer(1), select],
                f"{keyHint('tui.select.confirm', 'load/unload/download')} • {keyHint('tui.select.cancel', 'close')}",
            ),
            select,
        )
        return await future

    async def select(self, title: str, options: list[str]) -> str | None:
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        select = SelectList(
            [SelectItem(value=option, label=option) for option in options],
            min(len(options), 12),
            _select_theme(self.theme),
        )
        select.onSelect = lambda item: (
            None if future.done() else future.set_result(item.value)
        )
        select.onCancel = lambda: None if future.done() else future.set_result(None)
        self._set_content(
            _frame(
                self.theme,
                title,
                [Spacer(1), select],
                f"{keyHint('tui.select.confirm', 'select')} • {keyHint('tui.select.cancel', 'cancel')}",
            ),
            select,
        )
        return await future

    async def confirm(self, title: str, message: str) -> bool:
        return await self.select(f"{title}\n{message}", ["Yes", "No"]) == "Yes"

    async def connectionError(
        self, serverUrl: str, message: str
    ) -> Literal["retry", "close"]:
        choice = await self.select(
            f"llama.cpp unavailable\n{serverUrl}\n\n{message}", ["Retry", "Close"]
        )
        return "retry" if choice == "Retry" else "close"

    async def searchModels(
        self,
        search: Callable[[str, Any], Awaitable[list[HuggingFaceModel]]],
    ) -> str | None:
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        component = _HuggingFaceSearch(
            self.tui,
            self.theme,
            self.keybindings,
            search,
            self.searchCache,
            lambda model: None if future.done() else future.set_result(model),
        )
        self._set_content(
            _frame(
                self.theme,
                "Download model",
                [Spacer(1), component],
                f"{keyHint('tui.select.confirm', 'select')} • {keyHint('tui.select.cancel', 'back')}",
            ),
            component,
            component,
        )
        return await future

    def showStatus(self, title: str, message: str) -> None:
        self._set_content(
            _frame(
                self.theme,
                title,
                [Spacer(1), Text(self.theme.fg("muted", message), 1, 0)],
            )
        )

    async def progress(self, state: ProgressState) -> None:
        if self._progressFuture is None:
            self._progressFuture = asyncio.get_running_loop().create_future()
        self._showingProgress = True
        self.updateProgress(state)
        await self._progressFuture

    def updateProgress(self, state: ProgressState) -> None:
        if not self._showingProgress:
            return
        body: list[Any] = [
            Text(self.theme.fg("text", state.model), 1, 0),
            Spacer(1),
            Text(self.theme.fg("muted", state.message), 1, 0),
        ]
        if state.ratio is not None:
            available = 40
            ratio = max(0.0, min(1.0, state.ratio))
            filled = round(ratio * available)
            body.append(
                Text(
                    self.theme.fg(
                        "accent",
                        f"{'█' * filled}{'─' * (available - filled)} {round(ratio * 100)}%",
                    ),
                    1,
                    0,
                )
            )
        if state.detail:
            body.append(Text(self.theme.fg("dim", state.detail), 1, 0))
        self.content = _frame(
            self.theme,
            state.title,
            body,
            keyHint("tui.select.cancel", "stop"),
        )
        self.inputHandler = None
        self.tui.requestRender()

    def handleInput(self, data: str) -> None:
        if (
            self._progressFuture is not None
            and not self._progressFuture.done()
            and self.keybindings.matches(data, "tui.select.cancel")
        ):
            self._progressFuture.set_result(None)
            self._progressFuture = None
            return
        handler = getattr(self.inputHandler, "handleInput", None)
        if callable(handler):
            handler(data)
        self.tui.requestRender()

    def render(self, width: int) -> list[str]:
        return [
            truncateToWidth(line, width, "") if visibleWidth(line) > width else line
            for line in self.content.render(width)
        ]

    def invalidate(self) -> None:
        self.content.invalidate()


async def showLlamaUi(ctx: Any, run: Callable[[LlamaUi], Awaitable[None]]) -> None:
    def factory(tui: Any, theme: Any, keybindings: Any, done: Callable[[Any], None]):
        view = _LlamaView(tui, theme, keybindings)

        async def execute() -> None:
            try:
                await run(view)
            except Exception as error:  # noqa: BLE001 - command errors belong in the UI
                ctx.ui.notify(str(error), "error")
            finally:
                done(None)

        asyncio.create_task(execute())
        return view

    await ctx.ui.custom(factory)


@dataclass(slots=True)
class ProgressResult:
    cancelled: bool
    value: Any = None


async def runWithProgress(
    ui: LlamaUi,
    *,
    title: str,
    model: str,
    initialMessage: str,
    cancelTitle: str,
    cancelMessage: str,
    run: Callable[[Any, Callable[[LlamaProgress], None]], Awaitable[Any]],
    cancel: Callable[[], Awaitable[None]],
) -> ProgressResult:
    controller = AbortController()
    state = ProgressState(title=title, model=model, message=initialMessage)

    def update(progress: LlamaProgress) -> None:
        state.message = progress.message
        state.ratio = progress.ratio
        state.detail = progress.detail
        ui.updateProgress(state)

    settled = asyncio.create_task(run(controller, update))
    while not settled.done():
        progress = asyncio.create_task(ui.progress(state))
        done, _pending = await asyncio.wait(
            {settled, progress}, return_when=asyncio.FIRST_COMPLETED
        )
        if settled in done:
            progress.cancel()
            await asyncio.gather(progress, return_exceptions=True)
            break
        if not await ui.confirm(cancelTitle, cancelMessage) or settled.done():
            continue
        try:
            await cancel()
        finally:
            controller.abort()
        await asyncio.gather(settled, return_exceptions=True)
        return ProgressResult(cancelled=True)
    return ProgressResult(cancelled=False, value=await settled)


__all__ = [
    "LlamaManagerAction",
    "LlamaUi",
    "ProgressResult",
    "ProgressState",
    "runWithProgress",
    "showLlamaUi",
]

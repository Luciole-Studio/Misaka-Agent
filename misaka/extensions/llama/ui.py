"""Interactive llama.cpp model manager UI.

Translated from pi's ``coding-agent/src/extensions/llama/ui.ts``.
"""

from __future__ import annotations

import asyncio
import functools
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict

from misaka.modes.interactive.components.dynamic_border import DynamicBorder
from misaka.modes.interactive.components.keybinding_hints import keyHint
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
from misaka.utils.collation import localeCompare
from misaka.utils.js import (
    UNDEFINED,
    AbortController,
    Timeout,
    clearTimeout,
    errorMessage,
    jsNumber,
    jsRound,
    jsString,
    jsTrim,
    setTimeout,
    spawn,
    toFixed,
)

from .client import LlamaModelInfo, LlamaProgress
from .huggingface import HuggingFaceModel

DOWNLOAD_VALUE = "\0download"

# MISAKA fork: asyncio holds tasks weakly; `void this.runSearch(...)` keeps its task here until it settles.
_background: set[asyncio.Task[Any]] = set()


@dataclass(slots=True)
class LlamaManagerAction:
    """``{type: "model", model}``, ``{type: "download"}`` or ``{type: "close"}``."""

    type: Literal["model", "download", "close"]
    model: LlamaModelInfo | None = None


@dataclass(slots=True)
class ProgressState(LlamaProgress):
    title: str = ""
    model: str = ""


def contextLabel(model: LlamaModelInfo) -> str | None:
    context = None
    if model.meta is not None:
        context = model.meta.n_ctx if model.meta.n_ctx is not None else model.meta.n_ctx_train
    if context:
        return f"{jsRound(context / 1000)}k" if context >= 1000 else jsString(context)
    args = model.status.args or []
    for index in range(len(args) - 1):
        if args[index] != "--ctx-size" and args[index] != "-c" and args[index] != "-ctx":
            continue
        value = jsNumber(args[index + 1])
        if math.isfinite(value) and value > 0:
            return f"{jsRound(value / 1000)}k" if value >= 1000 else jsString(value)
    return None


def modelDescription(model: LlamaModelInfo) -> str:
    details: list[str] = []
    loaded = model.status.value == "loaded" or model.status.value == "sleeping"
    if loaded:
        details.append("loaded")
    elif model.status.value != "unloaded":
        details.append(model.status.value)
    context = contextLabel(model) if loaded else None
    if context:
        details.append(f"{context} context")
    return " · ".join(details)


def selectTheme(theme: Any) -> SelectListTheme:
    return SelectListTheme(
        selectedPrefix=lambda text: theme.fg("accent", text),
        selectedText=lambda text: theme.fg("accent", text),
        description=lambda text: theme.fg("muted", text),
        scrollInfo=lambda text: theme.fg("dim", text),
        noMatch=lambda text: theme.fg("warning", text),
    )


def frame(theme: Any, title: str, body: list[Any], footer: str | None = None) -> Container:
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


class LlamaUi(Protocol):
    def showModels(self, serverUrl: str, models: list[LlamaModelInfo]) -> Awaitable[LlamaManagerAction]: ...

    def select(self, title: str, options: list[str]) -> Awaitable[str | None]: ...

    def confirm(self, title: str, message: str) -> Awaitable[bool]: ...

    def connectionError(self, serverUrl: str, message: str) -> Awaitable[Literal["retry", "close"]]: ...

    def searchModels(self, search: Callable[[str, Any], Awaitable[list[HuggingFaceModel]]]) -> Awaitable[str | None]: ...

    def showStatus(self, title: str, message: str) -> None: ...

    def progress(self, state: ProgressState) -> Awaitable[None]: ...

    def updateProgress(self, state: ProgressState) -> None: ...


def compactCount(value: float) -> str:
    if value >= 1_000_000:
        return f"{toFixed(value / 1_000_000, 0 if value >= 10_000_000 else 1)}M"
    if value >= 1_000:
        return f"{toFixed(value / 1_000, 0 if value >= 100_000 else 1)}k"
    return jsString(value)


def _promise[T]() -> tuple[asyncio.Future[T], Callable[[T], None]]:
    """``new Promise((resolve) => ...)``: the future and its ``resolve``, which a second call leaves alone."""
    future: asyncio.Future[T] = asyncio.get_running_loop().create_future()

    def resolve(value: T) -> None:
        if not future.done():
            future.set_result(value)

    return future, resolve


class HuggingFaceSearch(Container):
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
        self.input = Input()
        self.resultsContainer = Container()
        self.results: list[HuggingFaceModel] = []
        self.filteredResults: list[HuggingFaceModel] = []
        self.selectedIndex = 0
        self.query = ""
        self.status = "Type at least 2 characters"
        self.debounce: Timeout | None = None
        self.request: AbortController | None = None
        self.closed = False
        self._focused = False
        self.tui = tui
        self.theme = theme
        self.keybindings = keybindings
        self.search = search
        self.cache = cache
        self.onSelectModel = onSelectModel
        self.addChild(Text(theme.fg("dim", "Model name or owner/repository[:quant]"), 1, 0))
        self.addChild(self.input)
        self.addChild(Spacer(1))
        self.addChild(self.resultsContainer)
        self.updateResults()

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.input.focused = value

    def updateResults(self) -> None:
        self.resultsContainer.clear()
        maxVisible = 10
        start = max(0, min(self.selectedIndex - maxVisible // 2, len(self.filteredResults) - maxVisible))
        end = min(start + maxVisible, len(self.filteredResults))
        for index in range(start, end):
            model = self.filteredResults[index]
            prefix = "→ " if index == self.selectedIndex else "  "
            details = f"{compactCount(model.downloads)} downloads"
            self.resultsContainer.addChild(
                Text(
                    self.theme.fg("accent", f"{prefix}{model.id}  {details}")
                    if index == self.selectedIndex
                    else f"{prefix}{model.id}{self.theme.fg('muted', f'  {details}')}",
                    0,
                    0,
                )
            )
        if start > 0 or end < len(self.filteredResults):
            self.resultsContainer.addChild(
                Text(self.theme.fg("dim", f"  ({self.selectedIndex + 1}/{len(self.filteredResults)})"), 0, 0)
            )
        if len(self.filteredResults) == 0:  # noqa: SIM114 - upstream's two branches
            self.resultsContainer.addChild(Text(self.theme.fg("dim", f"  {self.status}"), 0, 0))
        elif self.status == "Searching Hugging Face…":
            self.resultsContainer.addChild(Text(self.theme.fg("dim", f"  {self.status}"), 0, 0))
        self.tui.requestRender()

    def filterResults(self) -> None:
        if self.query:
            matches = {model.id for model in fuzzyFilter(self.results, self.query, lambda model: model.id)}
            self.filteredResults = [model for model in self.results if model.id in matches]
        else:
            self.filteredResults = self.results
        self.selectedIndex = min(self.selectedIndex, max(0, len(self.filteredResults) - 1))
        self.updateResults()

    def scheduleSearch(self) -> None:
        if self.debounce:
            clearTimeout(self.debounce)
        if self.request is not None:
            self.request.abort()
        self.request = None
        if len(self.query) < 2:
            self.status = "Type at least 2 characters"
            self.filterResults()
            return
        cached = self.cache.get(self.query.lower())
        if cached is not None:
            self.results = cached
            self.status = "No GGUF models found" if len(cached) == 0 else ""
            self.filterResults()
            return
        self.status = "Searching Hugging Face…"
        self.filterResults()
        self.debounce = setTimeout(lambda: spawn(self.runSearch(self.query), _background), 500)

    async def runSearch(self, query: str) -> None:
        request = AbortController()
        self.request = request
        try:
            results = await self.search(query, request.signal)
            self.cache[query.lower()] = results
            if self.closed or request.signal.aborted or self.query != query:
                return
            self.results = results
            self.selectedIndex = 0
            self.status = "No GGUF models found" if len(results) == 0 else ""
            self.filterResults()
        except Exception as error:  # noqa: BLE001 - the search view renders the error
            if self.closed or request.signal.aborted or self.query != query:
                return
            self.results = []
            self.status = errorMessage(error)
            self.filterResults()
        finally:
            if self.request is request:
                self.request = None

    def close(self, model: str | None) -> None:
        if self.closed:
            return
        self.closed = True
        if self.debounce:
            clearTimeout(self.debounce)
        if self.request is not None:
            self.request.abort()
        self.onSelectModel(model)

    def handleInput(self, data: str) -> None:
        if self.keybindings.matches(data, "tui.select.up"):
            if len(self.filteredResults) > 0:
                self.selectedIndex = (
                    len(self.filteredResults) - 1 if self.selectedIndex == 0 else self.selectedIndex - 1
                )
                self.updateResults()
            return
        if self.keybindings.matches(data, "tui.select.down"):
            if len(self.filteredResults) > 0:
                self.selectedIndex = (
                    0 if self.selectedIndex == len(self.filteredResults) - 1 else self.selectedIndex + 1
                )
                self.updateResults()
            return
        if self.keybindings.matches(data, "tui.select.confirm"):
            exact = self.query if re.fullmatch(r"[^/\s]+/[^:\s]+(?::[^\s:]+)?", self.query) else None
            selected = (
                exact
                if exact is not None
                else (
                    self.filteredResults[self.selectedIndex].id
                    if self.selectedIndex < len(self.filteredResults)
                    else None
                )
            )
            if selected:
                self.close(selected)
            return
        if self.keybindings.matches(data, "tui.select.cancel"):
            self.close(None)
            return
        self.input.handleInput(data)
        query = jsTrim(self.input.getValue())
        if query == self.query:
            return
        self.query = query
        self.scheduleSearch()


class LlamaView:
    def __init__(self, tui: Any, theme: Any, keybindings: Any) -> None:
        self.searchCache: dict[str, list[HuggingFaceModel]] = {}
        self.inputHandler: Any = None
        self.inputTarget: Any = None
        self.progressPromise: asyncio.Future[None] | None = None
        self.progressResolver: Callable[[None], None] | None = None
        self.showingProgress = False
        self._focused = False
        self.tui = tui
        self.theme = theme
        self.keybindings = keybindings
        self.content = frame(theme, "llama.cpp models", [Text(theme.fg("muted", "Loading…"), 1, 1)])

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        if self.inputTarget:
            self.inputTarget.focused = value

    def setContent(self, content: Container, inputHandler: Any = None, inputTarget: Any = None) -> None:
        if self.inputTarget:
            self.inputTarget.focused = False
        self.progressPromise = None
        self.progressResolver = None
        self.showingProgress = False
        self.content = content
        self.inputHandler = inputHandler
        self.inputTarget = inputTarget
        if self.inputTarget:
            self.inputTarget.focused = self._focused
        self.tui.requestRender()

    def showModels(self, serverUrl: str, models: list[LlamaModelInfo]) -> asyncio.Future[LlamaManagerAction]:
        def compare(left: LlamaModelInfo, right: LlamaModelInfo) -> int:
            loaded = int(right.status.value == "loaded") - int(left.status.value == "loaded")
            return loaded or localeCompare(left.id, right.id)

        sorted_ = sorted(models, key=functools.cmp_to_key(compare))
        byId = {model.id: model for model in sorted_}
        items = [
            *(SelectItem(value=model.id, label=model.id, description=modelDescription(model)) for model in sorted_),
            SelectItem(value=DOWNLOAD_VALUE, label="Download model…", description="Hugging Face owner/repository[:quant]"),
        ]
        promise: asyncio.Future[LlamaManagerAction]
        promise, resolve = _promise()
        list_ = SelectList(
            items,
            min(len(items), 12),
            selectTheme(self.theme),
            SelectListLayoutOptions(minPrimaryColumnWidth=36, maxPrimaryColumnWidth=56),
        )

        def onSelect(item: SelectItem) -> None:
            if item.value == DOWNLOAD_VALUE:
                resolve(LlamaManagerAction(type="download"))
            else:
                model = byId.get(item.value)
                if model:
                    resolve(LlamaManagerAction(type="model", model=model))

        list_.onSelect = onSelect
        list_.onCancel = lambda: resolve(LlamaManagerAction(type="close"))
        self.setContent(
            frame(
                self.theme,
                "llama.cpp models",
                [Text(self.theme.fg("dim", serverUrl), 1, 0), Spacer(1), list_],
                f"{keyHint('tui.select.confirm', 'load/unload/download')} • {keyHint('tui.select.cancel', 'close')}",
            ),
            list_,
        )
        return promise

    def select(self, title: str, options: list[str]) -> asyncio.Future[str | None]:
        promise: asyncio.Future[str | None]
        promise, resolve = _promise()
        list_ = SelectList(
            [SelectItem(value=option, label=option) for option in options],
            min(len(options), 12),
            selectTheme(self.theme),
        )
        list_.onSelect = lambda item: resolve(item.value)
        list_.onCancel = lambda: resolve(None)
        self.setContent(
            frame(
                self.theme,
                title,
                [Spacer(1), list_],
                f"{keyHint('tui.select.confirm', 'select')} • {keyHint('tui.select.cancel', 'cancel')}",
            ),
            list_,
        )
        return promise

    async def confirm(self, title: str, message: str) -> bool:
        return (await self.select(f"{title}\n{message}", ["Yes", "No"])) == "Yes"

    async def connectionError(self, serverUrl: str, message: str) -> Literal["retry", "close"]:
        choice = await self.select(f"llama.cpp unavailable\n{serverUrl}\n\n{message}", ["Retry", "Close"])
        return "retry" if choice == "Retry" else "close"

    def searchModels(self, search: Callable[[str, Any], Awaitable[list[HuggingFaceModel]]]) -> asyncio.Future[str | None]:
        promise: asyncio.Future[str | None]
        promise, resolve = _promise()
        component = HuggingFaceSearch(self.tui, self.theme, self.keybindings, search, self.searchCache, resolve)
        self.setContent(
            frame(
                self.theme,
                "Download model",
                [Spacer(1), component],
                f"{keyHint('tui.select.confirm', 'select')} • {keyHint('tui.select.cancel', 'back')}",
            ),
            component,
            component,
        )
        return promise

    def showStatus(self, title: str, message: str) -> None:
        self.setContent(frame(self.theme, title, [Spacer(1), Text(self.theme.fg("muted", message), 1, 0)]))

    def progress(self, state: ProgressState) -> asyncio.Future[None]:
        if not self.progressPromise:
            self.progressPromise, self.progressResolver = _promise()
        self.showingProgress = True
        self.updateProgress(state)
        return self.progressPromise

    def updateProgress(self, state: ProgressState) -> None:
        if not self.showingProgress:
            return
        body: list[Any] = [
            Text(self.theme.fg("text", state.model), 1, 0),
            Spacer(1),
            Text(self.theme.fg("muted", state.message), 1, 0),
        ]
        if state.ratio is not None:
            available = 40
            filled = jsRound(max(0, min(1, state.ratio)) * available)
            body.append(
                Text(
                    self.theme.fg(
                        "accent",
                        f"{'█' * filled}{'─' * (available - filled)} {jsRound(state.ratio * 100)}%",
                    ),
                    1,
                    0,
                )
            )
        if state.detail:
            body.append(Text(self.theme.fg("dim", state.detail), 1, 0))
        self.content = frame(self.theme, state.title, body, keyHint("tui.select.cancel", "stop"))
        self.inputHandler = None
        self.tui.requestRender()

    def handleInput(self, data: str) -> None:
        if self.progressResolver and self.keybindings.matches(data, "tui.select.cancel"):
            resolve = self.progressResolver
            self.progressPromise = None
            self.progressResolver = None
            resolve(None)
            return
        handleInput = getattr(self.inputHandler, "handleInput", None)
        if handleInput is not None:
            handleInput(data)
        self.tui.requestRender()

    def render(self, width: int) -> list[str]:
        return [
            truncateToWidth(line, width, "") if visibleWidth(line) > width else line
            for line in self.content.render(width)
        ]

    def invalidate(self) -> None:
        self.content.invalidate()


async def showLlamaUi(ctx: Any, run: Callable[[LlamaUi], Awaitable[None]]) -> None:
    def factory(tui: Any, theme: Any, keybindings: Any, done: Callable[[Any], None]) -> LlamaView:
        view = LlamaView(tui, theme, keybindings)

        async def settle() -> None:
            try:
                await run(view)
            except Exception as error:  # noqa: BLE001
                ctx.ui.notify(errorMessage(error), "error")
                done(None)
                return
            done(None)

        spawn(settle(), _background)
        return view

    await ctx.ui.custom(factory)


@dataclass(slots=True)
class ProgressResult:
    """``{cancelled: true}`` or ``{cancelled: false, value}``."""

    cancelled: bool
    value: Any = None


class RunWithProgressOptions(TypedDict):
    title: str
    model: str
    initialMessage: str
    cancelTitle: str
    cancelMessage: str
    run: Callable[[Any, Callable[[LlamaProgress], None]], Awaitable[Any]]
    cancel: Callable[[], Awaitable[None]]


async def runWithProgress(ui: LlamaUi, options: RunWithProgressOptions) -> ProgressResult:
    controller = AbortController()
    state = ProgressState(title=options["title"], model=options["model"], message=options["initialMessage"], ratio=None, detail=None)

    def onProgress(progress: LlamaProgress) -> None:
        # `Object.assign(state, progress)`: a field the progress leaves out keeps its value.
        for key in ("message", "ratio", "detail"):
            value = getattr(progress, key)
            if value is not UNDEFINED:
                setattr(state, key, value)
        ui.updateProgress(state)

    async def outcome() -> dict[str, Any]:
        try:
            return {"ok": True, "value": await options["run"](controller.signal, onProgress)}
        except Exception as error:  # noqa: BLE001 - rethrown below
            return {"ok": False, "error": error}

    settled = spawn(outcome(), _background)

    while not settled.done():
        progress = ui.progress(state)
        stop = asyncio.ensure_future(progress)
        await asyncio.wait({settled, stop}, return_when=asyncio.FIRST_COMPLETED)
        if settled.done():
            # MISAKA fork: a task started here for the wait is cancelled; the view's own promise stays pending, as the
            # losing promise of `Promise.race` does.
            if stop is not progress:
                stop.cancel()
            break
        if not (await ui.confirm(options["cancelTitle"], options["cancelMessage"])) or settled.done():
            continue
        try:
            await options["cancel"]()
        finally:
            controller.abort(RuntimeError("Cancelled"))
        await settled
        return ProgressResult(cancelled=True)

    result = await settled
    if not result["ok"]:
        raise result["error"]
    return ProgressResult(cancelled=False, value=result["value"])


__all__ = [
    "LlamaManagerAction",
    "LlamaUi",
    "ProgressResult",
    "ProgressState",
    "runWithProgress",
    "showLlamaUi",
]

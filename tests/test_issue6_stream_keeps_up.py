"""A streaming reply must not fall behind the provider while the TUI paints it (GitHub issue #6).

The panel's Last Order died at plan submission on DeepSeek with "Connection error." while the same
run from `misaka research` (no TUI) succeeded. Reproduced against a local DeepSeek-shaped server:
every frame re-estimated the whole context for the footer (~22 ms of a ~25 ms frame), frames ran
back to back once a frame outlasted the 16 ms interval, and the OpenAI adapters spent several loop
turns per SSE event on a fresh abort task. The reply advanced about one event per frame and was
165 s behind the server after 20k characters of thinking; with a bounded receive window the server
gave up on the client mid-reply. These tests pin each of the three repairs.
"""

import asyncio
import io
import time

import httpx
import openai
import pytest

from misaka.ai.api import _openai_sdk
from misaka.ai.utils.retry import _RETRYABLE_PROVIDER_ERROR_PATTERN
from misaka.core.session_manager import SessionManager
from misaka.modes.interactive.components.footer import FooterComponent
from misaka.ui.tui.terminal import ProcessTerminal
from misaka.ui.tui.tui_main_screen import TuiMainScreen


class Terminal(ProcessTerminal):
    def __init__(self):
        super().__init__()
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()

    @property
    def columns(self):
        return 80

    @property
    def rows(self):
        return 24

    def start(self, onInput, onResize):
        self.inputHandler = onInput
        self.resizeHandler = onResize

    def stop(self):
        return None


class SlowContent:
    """Laying out a long reply is work: 30 ms of this thread's CPU."""

    def render(self, _width):
        until = time.thread_time() + 0.03
        while time.thread_time() < until:
            pass
        return ["a long reply, laid out whole every frame"]


@pytest.mark.asyncio
async def test_a_slow_frame_leaves_the_loop_as_long_as_it_took(monkeypatch):
    ui = TuiMainScreen(Terminal())
    ui.addChild(SlowContent())
    loop = asyncio.get_running_loop()
    delays = []
    real_call_later = loop.call_later
    monkeypatch.setattr(loop, "call_later", lambda delay, *args: delays.append(delay) or real_call_later(delay, *args))

    def frame_then_schedule():
        ui.lastRenderAt = time.perf_counter() * 1000          # as the render runners stamp it
        ui.doRender()
        ui.renderRequested = True
        ui.scheduleRender()
        ui.cancelRenderTimer()

    frame_then_schedule()
    assert ui.lastRenderCostMs >= 30
    # pi's 16 ms from the frame's start would have rendered again at once.
    assert delays[-1] >= ui.lastRenderCostMs / 1000 - 0.005

    # A frame that waited beyond its CPU (threads contending for the GIL, a loaded machine) still
    # leaves the loop its CPU's worth: counted from the frame's start, the wait came to nothing.
    ui.lastRenderCostMs, ui.lastRenderWallMs = 30.0, 60.0
    ui.lastRenderAt = time.perf_counter() * 1000 - 60
    ui.scheduleRender()
    ui.cancelRenderTimer()
    assert delays[-1] >= 0.029

    ui.lastRenderCostMs = ui.lastRenderWallMs = 1.0
    ui.lastRenderAt = time.perf_counter() * 1000
    ui.scheduleRender()
    ui.cancelRenderTimer()
    assert 0.01 <= delays[-1] <= ui.MIN_RENDER_INTERVAL_MS / 1000


class HeldUpTerminal(Terminal):
    """A Windows console with text selected: the write waits, the CPU does nothing."""

    def write(self, data):
        time.sleep(0.5)
        return super().write(data)


def test_a_terminal_write_held_up_is_not_counted_as_the_frames_work():
    ui = TuiMainScreen(HeldUpTerminal())
    ui.addChild(Content())
    ui.doRender()
    assert ui.lastRenderCostMs < 100, "the next frame is not held back as long as the write was"


class Content:
    def render(self, _width):
        return ["a reply"]


class Session:
    def __init__(self, cwd):
        self.sessionManager = SessionManager.inMemory(cwd=cwd)
        self.model = object()
        self.routedModel = None
        self.estimates = 0

    def getContextUsage(self):
        self.estimates += 1
        return {"tokens": 1, "contextWindow": 10, "percent": 10.0}


def test_footer_estimates_the_context_once_per_session_change(tmp_path):
    session = Session(str(tmp_path))
    footer = FooterComponent(session, footerData=None)

    footer.getSessionStats()
    footer.getSessionStats()
    assert session.estimates == 1, "a streaming frame must not re-estimate an unchanged session"

    session.sessionManager.appendMessage({"role": "user", "content": "hi", "timestamp": 0})
    footer.getSessionStats()
    assert session.estimates == 2

    session.model = object()
    footer.getSessionStats()
    assert session.estimates == 3


class Signal:
    aborted = False

    def __init__(self):
        self.waits = 0
        self._event = asyncio.Event()

    async def wait(self):
        self.waits += 1
        await self._event.wait()


@pytest.mark.asyncio
async def test_openai_streams_wait_on_the_abort_signal_once():
    async def items():
        for index in range(50):
            yield {"index": index}

    signal = Signal()
    received = [item async for item in _openai_sdk._iterate_stream(items(), signal)]
    assert [item["index"] for item in received] == list(range(50))
    assert signal.waits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["openai_completions", "openai_responses", "azure_openai_responses"])
async def test_each_openai_adapter_reads_its_stream_through_the_one_wait_iterator(adapter, monkeypatch):
    """The adapters share `_openai_sdk._iterate_stream`, so each call site is pinned here."""
    import importlib

    from misaka.ai.compat import getModel
    from misaka.ai.types import Context, UserMessage

    module = importlib.import_module(f"misaka.ai.api.{adapter}")
    model = {
        "openai_completions": lambda: getModel("openai", "gpt-5").model_copy(update={"api": "openai-completions"}),
        "openai_responses": lambda: getModel("openai", "gpt-5"),
        "azure_openai_responses": lambda: getModel("azure", "gpt-5"),
    }[adapter]()

    async def items():
        for index in range(50):
            yield {"index": index}

    async def createWithResponse(*_args, **_kwargs):
        return _openai_sdk._WithResponse(items(), httpx.Response(200, request=httpx.Request("POST", "https://x")))

    seen: list = []
    iterate = _openai_sdk._iterate_stream

    def spy(stream_obj, signal=None):
        seen.append(stream_obj)
        return iterate(stream_obj, signal)

    monkeypatch.setattr(_openai_sdk, "_create_with_response", createWithResponse)
    monkeypatch.setattr(_openai_sdk, "_iterate_stream", spy)
    options = {"apiKey": "sk-test", "signal": Signal()}
    if adapter == "azure_openai_responses":
        options["azureBaseUrl"] = "https://example.openai.azure.com/openai/v1"
    events = [event async for event in module.stream(model, Context(messages=[UserMessage(content="hi", timestamp=0)]), options)]

    assert events
    assert len(seen) == 1


def test_connection_error_names_the_transport_failure():
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    try:
        try:
            raise httpx.ReadError("[WinError 10054] An existing connection was forcibly closed")
        except httpx.ReadError as transport_error:
            raise openai.APIConnectionError(request=request) from transport_error
    except openai.APIConnectionError as error:
        message = _openai_sdk._format_completion_error(error)

    assert message == "Connection error. (ReadError: [WinError 10054] An existing connection was forcibly closed)"
    assert _RETRYABLE_PROVIDER_ERROR_PATTERN.search(message)
    assert _openai_sdk._format_completion_error(RuntimeError("boom")) == "boom"


def test_a_request_refused_before_it_was_sent_does_not_quote_the_key():
    """h11 refuses a header with a trailing space and quotes the whole value: the API key came back
    in the error message, shown in the window and kept in the session."""
    import h11
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    try:
        try:
            raise h11.LocalProtocolError("Illegal header value b'Bearer sk-ABCSECRETKEY123 '")
        except h11.LocalProtocolError as local:
            raise openai.APIConnectionError(request=request) from local
    except openai.APIConnectionError as error:
        message = _openai_sdk._format_completion_error(error)
    assert "SECRETKEY" not in message and "API key" in message
    assert _RETRYABLE_PROVIDER_ERROR_PATTERN.search(message)


@pytest.mark.parametrize("said", [
    "peer closed connection without sending complete message body (incomplete chunked read) (RemoteProtocolError)",
    "(ConnectionResetError: [Errno 54] Connection reset by peer)",
    "Connection error. (ReadError: [WinError 10054] An existing connection was forcibly closed by the remote host)",
])
def test_a_reply_dropped_mid_stream_is_retried(said):
    """The issue #6 failure itself: the provider gave up on a slow reader mid-reply, and none of
    these words was in the pattern, so the turn ended with no retry."""
    assert _RETRYABLE_PROVIDER_ERROR_PATTERN.search(said)


def test_the_token_cap_stop_is_never_retried_whatever_its_numbers():
    from misaka.ai.api._common import _empty_usage
    from misaka.ai.types import AssistantMessage
    from misaka.ai.utils.retry import isRetryableAssistantError
    from misaka.core.platform import budget
    said = (f"{budget.EXHAUSTED_MESSAGE}: the next request needs up to 500,000 tokens and this run "
            f"has 429,000 of its 5,000,000 left.")
    message = AssistantMessage(content=[], api="a", provider="p", model="m", usage=_empty_usage(),
                               stopReason="error", errorMessage=said, timestamp=0)
    assert not isRetryableAssistantError(message)

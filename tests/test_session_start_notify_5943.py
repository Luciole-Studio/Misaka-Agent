"""Translated from pi's ``coding-agent/test/suite/regressions/5943-session-start-notify.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import Container, Text
from misaka.utils.values import read_field
from tests.suite_harness import createHarness, createTestUiContext


def createReloadCommandContext(
    *,
    editor=None,
    session: dict | None = None,
    settingsManager: dict | None = None,
    ui: dict | None = None,
    rebuildChatFromMessages=None,
):
    async def reload(options=None):
        beforeSessionStart = read_field(options, "beforeSessionStart")
        if beforeSessionStart:
            beforeSessionStart()

    return SimpleNamespace(
        hideThinkingBlock=False,
        session=SimpleNamespace(
            **{
                "isStreaming": False,
                "isCompacting": False,
                "reload": reload,
                "resourceLoader": SimpleNamespace(getThemes=lambda: {"themes": []}),
                "extensionRunner": SimpleNamespace(),
                "modelRuntime": SimpleNamespace(getError=lambda: None),
                **(session or {}),
            }
        ),
        settingsManager=SimpleNamespace(
            **{
                "getHttpIdleTimeoutMs": lambda: 0,
                "getHideThinkingBlock": lambda: False,
                "getOutputPad": lambda: 1,
                "getEditorPaddingX": lambda: 1,
                "getAutocompleteMaxVisible": lambda: 10,
                "getShowHardwareCursor": lambda: False,
                "getClearOnShrink": lambda: False,
                **(settingsManager or {}),
            }
        ),
        keybindings=SimpleNamespace(reload=lambda: None),
        editorContainer=SimpleNamespace(clear=lambda: None, addChild=lambda _component: None),
        ui=SimpleNamespace(
            **{
                "setFocus": lambda _component: None,
                "requestRender": lambda *_args: None,
                "setShowHardwareCursor": lambda _enabled: None,
                "setClearOnShrink": lambda _enabled: None,
                **(ui or {}),
            }
        ),
        editor=editor if editor is not None else SimpleNamespace(),
        defaultEditor=SimpleNamespace(setPaddingX=lambda _padding: None, setAutocompleteMaxVisible=lambda _max: None),
        themeController=SimpleNamespace(applyFromSettings=lambda: None),
        customHeader=None,
        builtInHeader=None,
        resetExtensionUI=lambda: None,
        rebuildChatFromMessages=rebuildChatFromMessages or (lambda: None),
        setupAutocompleteProvider=lambda: None,
        setupExtensionShortcuts=lambda _runner: None,
        showLoadedResources=lambda _options: None,
        maybeSaveImplicitProjectTrustAfterReload=lambda: False,
        showStatus=lambda _message: None,
        showWarning=lambda _message: None,
        showError=lambda _message: None,
    )


def getMessageText(event) -> str:
    message = read_field(event, "message")
    content = read_field(message, "content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(read_field(part, "text") for part in content if read_field(part, "type") == "text")


def createLoadedResourcesContext():
    context = SimpleNamespace(
        loadedResourcesContainer=Container(),
        chatContainer=Container(),
        options=SimpleNamespace(verbose=True),
        settingsManager=SimpleNamespace(getQuietStartup=lambda: False),
        sessionManager=SimpleNamespace(getCwd=lambda: "/repo"),
        session=SimpleNamespace(
            promptTemplates=[],
            resourceLoader=SimpleNamespace(
                getAgentsFiles=lambda: {"agentsFiles": [{"path": "/repo/AGENTS.md"}], "diagnostics": []},
                getSystemPromptSource=lambda: None,
                getAppendSystemPromptSources=list,
                getPrompts=lambda: {"prompts": [], "diagnostics": []},
                getThemes=lambda: {"themes": [], "diagnostics": []},
                getExtensions=lambda: SimpleNamespace(extensions=[], errors=[], warnings=[]),
            ),
            extensionRunner=SimpleNamespace(
                getCommandDiagnostics=list, getShortcutDiagnostics=list, getRegisteredCommands=list
            ),
        ),
        getStartupExpansionState=lambda: False,
        shouldShowStartupDetails=lambda: True,
        formatDisplayPath=lambda resourcePath: resourcePath,
        formatContextPath=lambda resourcePath: resourcePath.replace("/repo/", ""),
        getBuiltInCommandConflictDiagnostics=lambda _runner: [],
    )
    return context


def rebindContext(events: list[str], bindCurrentSessionExtensions, subscribeToAgent=None, applyRuntimeSettings=None):
    async def updateAvailableProviderCount():
        return None

    return SimpleNamespace(
        unsubscribe=None,
        session=None,
        applyRuntimeSettings=applyRuntimeSettings or (lambda: None),
        programStatus=SimpleNamespace(reset=lambda: None),
        renderCurrentSessionState=lambda: events.append("render"),
        bindCurrentSessionExtensions=bindCurrentSessionExtensions,
        subscribeToAgent=subscribeToAgent or (lambda: events.append("subscribe")),
        updateAvailableProviderCount=lambda: None,
        updateEditorBorderColor=lambda: None,
        updateTerminalTitle=lambda: None,
    )


def test_renders_loaded_resources_before_restored_messages_without_stale_entries():
    initTheme("dark", False)
    context = createLoadedResourcesContext()
    root = Container()
    root.addChild(context.loadedResourcesContainer)
    root.addChild(context.chatContainer)
    context.loadedResourcesContainer.addChild(Text("stale resources", 0, 0))
    context.chatContainer.addChild(Text("restored message", 0, 0))

    InteractiveMode.showLoadedResources(context)

    chatRendered = "\n".join(context.chatContainer.render(80))
    assert "restored message" in chatRendered
    assert "[Context]" not in chatRendered

    rendered = "\n".join(root.render(80))
    assert "stale resources" not in rendered
    assert rendered.index("[Context]") < rendered.index("restored message")


async def test_renders_replacement_session_state_before_session_start_handlers_can_notify():
    events: list[str] = []

    def factory(pi):
        pi.on("session_start", lambda _event, ctx: ctx.ui.notify("Hello Error", "error"))

    async with createHarness(extensionFactories=[factory]) as harness:

        async def bind():
            events.append("bind")
            await harness.session.bindExtensions(
                {
                    "uiContext": createTestUiContext(notify=lambda message, *_args: events.append(f"notify:{message}")),
                    "mode": "tui",
                }
            )

        context = rebindContext(events, bind, applyRuntimeSettings=lambda: events.append("apply"))

        await InteractiveMode.rebindCurrentSession(context, {"renderBeforeBind": True})

        assert events == ["apply", "render", "subscribe", "bind", "notify:Hello Error"]


async def test_subscribes_before_replacement_session_start_handlers_send_messages():
    events: list[str] = []

    def factory(pi):
        async def onStart(_event, _ctx):
            await pi.sendMessage({"customType": "session-start", "content": "custom from start", "display": True})

        pi.on("session_start", onStart)

    async with createHarness(extensionFactories=[factory]) as harness:

        async def bind():
            events.append("bind")
            await harness.session.bindExtensions({"uiContext": createTestUiContext(), "mode": "tui"})

        def subscribe():
            events.append("subscribe")

            def listener(event):
                if read_field(event, "type") not in ("message_start", "message_end"):
                    return
                events.append(
                    f"{read_field(event, 'type')}:{read_field(read_field(event, 'message'), 'role')}:{getMessageText(event)}"
                )

            harness.session.subscribe(listener)

        context = rebindContext(events, bind, subscribe)

        await InteractiveMode.rebindCurrentSession(context, {"renderBeforeBind": True})

        assert events == [
            "render",
            "subscribe",
            "bind",
            "message_start:custom:custom from start",
            "message_end:custom:custom from start",
        ]


async def test_subscribes_before_replacement_session_start_handlers_send_user_messages():
    events: list[str] = []

    def factory(pi):
        async def onStart(_event, _ctx):
            await pi.sendUserMessage("user from start")

        pi.on("session_start", onStart)

    async with createHarness(extensionFactories=[factory]) as harness:
        harness.setResponses([fauxAssistantMessage("assistant from start")])

        async def bind():
            events.append("bind")
            await harness.session.bindExtensions({"uiContext": createTestUiContext(), "mode": "tui"})

        def subscribe():
            events.append("subscribe")

            def listener(event):
                if read_field(event, "type") not in ("message_start", "message_end"):
                    return
                events.append(
                    f"{read_field(event, 'type')}:{read_field(read_field(event, 'message'), 'role')}:{getMessageText(event)}"
                )

            harness.session.subscribe(listener)

        context = rebindContext(events, bind, subscribe)

        await InteractiveMode.rebindCurrentSession(context, {"renderBeforeBind": True})
        await harness.session.agent.waitForIdle()

        assert events[:3] == ["render", "subscribe", "bind"]
        assert "message_start:user:user from start" in events
        assert "message_end:user:user from start" in events
        assert "message_end:assistant:assistant from start" in events


async def test_runs_the_reload_render_hook_before_reload_session_start_handlers_can_notify():
    events: list[str] = []
    beforeSessionStart = Mock(side_effect=lambda: events.append("render"))

    def factory(pi):
        def onStart(event, ctx):
            events.append(f"start:{read_field(event, 'reason')}")
            ctx.ui.notify(f"notify:{read_field(event, 'reason')}", "error")

        pi.on("session_start", onStart)

    async with createHarness(extensionFactories=[factory]) as harness:
        await harness.session.bindExtensions(
            {"uiContext": createTestUiContext(notify=lambda message, *_args: events.append(message)), "mode": "tui"}
        )
        assert events == ["start:startup", "notify:startup"]

        events.clear()
        await harness.session.reload({"beforeSessionStart": beforeSessionStart})

        assert beforeSessionStart.call_count == 1
        assert events == ["render", "start:reload", "notify:reload"]


async def test_refreshes_hide_thinking_block_before_rebuilding_chat_during_reload():
    initTheme("dark", False)
    events: list[str] = []
    context: SimpleNamespace

    async def reload(options=None):
        events.append("reload")
        read_field(options, "beforeSessionStart")()
        events.append(f"start:{context.hideThinkingBlock}")

    context = createReloadCommandContext(
        settingsManager={"getHideThinkingBlock": lambda: True},
        session={"reload": reload},
        rebuildChatFromMessages=lambda: events.append(f"rebuild:{context.hideThinkingBlock}"),
    )

    await InteractiveMode.handleReloadCommand(context)

    assert context.hideThinkingBlock is True
    assert events == ["reload", "rebuild:True", "start:True"]


async def test_keeps_the_reload_blocker_focused_until_async_reload_completes():
    initTheme("dark", False)
    editor = SimpleNamespace()
    state = {"focused": None, "chatRestored": False}
    loop = asyncio.get_running_loop()
    reloadWaiting: asyncio.Future[None] = loop.create_future()
    reloadFinished: asyncio.Future[None] = loop.create_future()

    async def reload(options=None):
        read_field(options, "beforeSessionStart")()
        reloadWaiting.set_result(None)
        await reloadFinished

    def setFocus(component):
        state["focused"] = component

    def rebuild():
        state["chatRestored"] = True

    context = createReloadCommandContext(
        editor=editor, session={"reload": reload}, ui={"setFocus": setFocus}, rebuildChatFromMessages=rebuild
    )

    reloadPromise = asyncio.ensure_future(InteractiveMode.handleReloadCommand(context))
    await reloadWaiting

    assert state["chatRestored"] is True
    assert state["focused"] is not editor

    reloadFinished.set_result(None)
    await reloadPromise

    assert state["focused"] is editor

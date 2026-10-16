"""Follow-up ordering, live steering and per-session settings without model calls."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path
import tempfile

from misaka.core.gui_input_queue import GuiInputQueue
from misaka.core.session_control import SessionControl
from misaka.ui.gui.chat_runner import ChatHost
from misaka.ui.gui.chats import ChatChannel, ChatManager
from misaka.ui.gui.server import Bridge


class Session:
    def __init__(self):
        self.isStreaming = False
        self.idle = asyncio.Event()
        self.idle.set()
        self.calls = []
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.model = SimpleNamespace(provider="test", id="first", name="First", reasoning=True, contextWindow=1000)
        self.thinkingLevel = "low"
        self.sessionId = "test"
        self.sessionFile = "test.jsonl"
        self.sessionName = "Test"
        self._modelRegistry = SimpleNamespace(refresh=AsyncMock(), find=Mock(return_value=self.model), getAvailable=lambda: [self.model], hasConfiguredAuth=lambda m: True, getProviderDisplayName=lambda p: p)

    async def waitForIdle(self):
        await self.idle.wait()

    async def prompt(self, text, options):
        self.calls.append((text, options))
        if self.isStreaming:
            return
        self.isStreaming = True
        self.idle.clear()
        self.started.set()
        await self.finish.wait()
        self.isStreaming = False
        self.idle.set()

    async def setModel(self, model, persist=False):
        self.model = model

    def setThinkingLevel(self, level, persist=False):
        self.thinkingLevel = level

    def getAvailableThinkingLevels(self):
        return ["off", "low", "high"]


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = Session()
        chosen = patch("misaka.ui.gui.model_preferences.preferences", return_value={"test": {"enabled": True, "models": ["first"]}})
        chosen.start()
        self.addCleanup(chosen.stop)
        self.errors, self.settled = [], []
        self.queue = GuiInputQueue(self.session, lambda p: self.session.prompt(p["text"], p),
                                   failed=self.errors.append, settled=lambda: self.settled.append(True))
        self.addCleanup(self.queue.clear)

    async def test_followups_wait_and_preserve_fifo_and_images(self):
        self.queue.submit({"text": "first", "message_id": "1"})
        await self.session.started.wait()
        image = {"type": "image", "data": "abc", "mime": "image/png"}
        self.queue.submit({"text": "second", "message_id": "2", "images": [image]})
        self.queue.submit({"text": "third", "message_id": "3"})
        await asyncio.sleep(0)
        self.assertEqual([c[0] for c in self.session.calls], ["first"])
        self.assertEqual([p["id"] for p in self.queue.snapshot()], ["2", "3"])
        self.session.finish.set()
        await self.queue.task
        self.assertEqual([c[0] for c in self.session.calls], ["first", "second", "third"])
        self.assertEqual(self.session.calls[1][1]["images"], [image])
        self.assertEqual(self.settled, [True])

    async def test_send_now_reaches_live_session_before_first_turn_finishes(self):
        self.queue.submit({"text": "first", "message_id": "1"})
        await self.session.started.wait()
        self.queue.submit({"text": "keep queued", "message_id": "2"})
        self.queue.submit({"text": "change direction", "message_id": "3"})
        await self.queue.send_now("3")
        self.assertFalse(self.session.finish.is_set())
        self.assertEqual(self.session.calls[-1][0], "change direction")
        self.assertEqual(self.session.calls[-1][1]["streamingBehavior"], "steer")
        self.assertEqual([p["id"] for p in self.queue.snapshot()], ["2"])
        with self.assertRaisesRegex(ValueError, "已开始"):
            await self.queue.send_now("3")
        self.session.finish.set()
        await self.queue.task
        self.assertEqual([c[0] for c in self.session.calls], ["first", "change direction", "keep queued"])

    async def test_stop_drops_waiting_followups(self):
        self.queue.submit({"text": "first"})
        await self.session.started.wait()
        self.queue.submit({"text": "second"})
        self.queue.clear()
        await asyncio.sleep(0)
        self.assertEqual(self.queue.snapshot(), [])
        self.assertEqual([c[0] for c in self.session.calls], ["first"])

    async def test_session_rebind_does_not_cancel_its_own_command(self):
        completed = []

        async def rebind(_params):
            self.queue.clear()
            await asyncio.sleep(0)
            completed.append(True)

        self.queue.deliver = rebind
        self.queue.submit({"text": "/fork"})
        await self.queue.task
        self.assertEqual(completed, [True])
        self.assertFalse(self.queue.task.cancelled())

    async def test_failed_promotion_remains_in_queue(self):
        self.session.isStreaming = True
        self.session.idle.clear()
        self.queue.submit({"text": "retry", "message_id": "1"})
        self.queue.deliver = AsyncMock(side_effect=ValueError("preflight failed"))
        with self.assertRaisesRegex(ValueError, "preflight"):
            await self.queue.send_now("1")
        self.assertEqual([p["id"] for p in self.queue.snapshot()], ["1"])

    async def test_withdraw_recovers_attachments_and_never_delivers(self):
        self.session.isStreaming = True
        self.session.idle.clear()
        image = {"type": "image", "data": "abc", "mime": "image/png"}
        self.queue.submit({"text": "resolved references", "display_text": "draft", "message_id": "w",
                           "images": [image], "files": ["notes.md"]})
        recovered = self.queue.withdraw("w")
        self.assertEqual((recovered["text"], recovered["images"], recovered["files"]), ("draft", [image], ["notes.md"]))
        self.assertEqual(recovered["pendingPrompts"], [])
        self.session.idle.set()
        await self.queue.task
        self.assertEqual(self.session.calls, [])
        with self.assertRaisesRegex(ValueError, "无法撤回"):
            self.queue.withdraw("w")

    async def test_started_message_cannot_be_withdrawn(self):
        self.queue.submit({"text": "started", "message_id": "w"})
        await self.session.started.wait()
        with self.assertRaisesRegex(ValueError, "已开始处理"):
            self.queue.withdraw("w")
        self.session.finish.set()
        await self.queue.task

    async def test_paused_queue_survives_idle_and_can_withdraw_or_send_now(self):
        self.queue.pause()
        self.queue.submit({"text": "withdraw", "message_id": "a"})
        self.queue.submit({"text": "send", "message_id": "b"})
        await asyncio.sleep(0)
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.queue.withdraw("a")["text"], "withdraw")
        await self.queue.send_now("b")
        await self.session.started.wait()
        self.assertFalse(self.queue.paused)
        self.assertEqual([c[0] for c in self.session.calls], ["send"])
        self.session.finish.set()
        await self.queue.task

    async def test_stop_preserves_followups_and_new_input_runs_first(self):
        host = ChatHost({"workspace": "."})
        host.session = self.session
        host._create_input_queue()
        self.addCleanup(host.input_queue.clear)
        self.session.isStreaming = True
        self.session.idle.clear()
        host.op_prompt({"text": "old draft", "message_id": "old"})

        async def abort():
            self.session.isStreaming = False
            self.session.idle.set()

        self.session.abort = abort
        host.ui = SimpleNamespace(cancel_all=Mock())
        with patch("misaka.ui.gui.chat_runner.emit"):
            await host.op_stop({})
            await asyncio.sleep(0)
            self.assertTrue(host.input_queue.paused)
            self.assertEqual([p["id"] for p in host.input_queue.snapshot()], ["old"])
            self.assertEqual(self.session.calls, [])
            host.op_prompt({"text": "new direction", "message_id": "new"})
            await self.session.started.wait()
            self.assertEqual(self.session.calls[0][0], "new direction")
            host.op_withdraw({"message_id": "old"})
            self.session.finish.set()
            await host.input_queue.task
            self.assertEqual([c[0] for c in self.session.calls], ["new direction"])

    async def test_owner_send_now_resumes_pause_but_failure_preserves_it(self):
        catalog = SimpleNamespace(spec=None, refresh=Mock())
        control = SessionControl(self.session, catalog)
        self.addCleanup(control.gui_queue.clear)
        self.session.isStreaming = True
        self.session.idle.clear()
        control._deliver_gui_input = AsyncMock()
        control.gui_queue.deliver = control._deliver_gui_input
        await control._execute({"operation": "pause"})
        control.gui_queue.submit({"text": "now", "message_id": "p"})
        await control._execute({"operation": "send_now", "message_id": "p"})
        self.assertFalse(control.paused)
        self.assertFalse(control.gui_queue.paused)
        await control._execute({"operation": "pause"})
        with self.assertRaisesRegex(ValueError, "已开始处理"):
            await control._execute({"operation": "send_now", "message_id": "missing"})
        self.assertTrue(control.paused)
        self.assertTrue(control.gui_queue.paused)

    async def test_local_message_is_promoted_atomically_while_streaming(self):
        host = ChatHost({"workspace": "."})
        host.session = self.session
        host._create_input_queue()
        self.addCleanup(host.input_queue.clear)
        self.session.isStreaming = True
        self.session.idle.clear()
        with patch("misaka.ui.gui.chat_runner.emit"):
            await host.op_send_now({"text": "direct", "message_id": "local"})
        self.assertEqual([c[0] for c in self.session.calls], ["direct"])
        self.assertEqual(self.session.calls[0][1]["streamingBehavior"], "steer")
        self.assertEqual(host.input_queue.snapshot(), [])

    async def test_host_settings_broadcast_and_survive_snapshot_while_streaming(self):
        host = ChatHost({"workspace": "."})
        host.session = self.session
        host.registry = self.session._modelRegistry
        host.cwd = "."
        host._create_input_queue()
        self.addCleanup(host.input_queue.clear)
        self.session.isStreaming = True
        with patch("misaka.ui.gui.chat_runner.emit") as emitted:
            result = await host.op_set_model({"provider": "test", "id": "second"})
            self.assertTrue(result["streaming"])
            result = await host.op_set_thinking({"level": "high"})
            self.assertEqual(result["thinkingLevel"], "high")
            self.assertEqual(emitted.call_args.args[0]["type"], "session_settings")

    async def test_model_menu_refreshes_auth_and_custom_models_without_network(self):
        host = ChatHost({"workspace": "."})
        host.session = self.session
        host.registry = self.session._modelRegistry
        host.registry.getAvailable = lambda: []
        async def refresh(_options):
            host.registry.getAvailable = lambda: [self.session.model]
        host.registry.refresh.side_effect = refresh
        result = await host.op_models({})
        self.assertEqual(result["models"][0]["id"], self.session.model.id)
        host.registry.refresh.assert_awaited_once_with({"allowNetwork": False})

    async def test_original_owner_exposes_models_and_thinking(self):
        control = SessionControl(self.session, SimpleNamespace(spec=None))
        self.addCleanup(control.gui_queue.clear)
        models = await control._execute({"operation": "models"})
        self.assertEqual(models["models"][0]["provider"], "test")
        self.session.isStreaming = True
        self.assertEqual((await control._execute({"operation": "set_thinking", "level": "high"}))["thinkingLevel"], "high")
        with self.assertRaisesRegex(ValueError, "不支持"):
            await control._execute({"operation": "set_thinking", "level": "unknown"})
        result = await control._execute({"operation": "input", "text": "next", "streamingBehavior": "followUp", "message_id": "x"})
        self.assertTrue(result["accepted"])
        self.assertEqual(control._settings()["pendingPrompts"][0]["id"], "x")
        self.assertIn("withdraw", control._settings()["capabilities"])
        result = await control._execute({"operation": "withdraw", "message_id": "x"})
        self.assertTrue(result["withdrawn"])


class ChannelTests(unittest.TestCase):
    def test_legacy_owner_snapshot_and_settings_error_are_compatible(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge = Bridge(temp)
            bridge.request = Mock(return_value={"sessions": [{"id": "old", "path": "old.jsonl"}]})
            owner = {"id": "old", "instance": "original", "control": "socket"}
            with patch("misaka.core.session_catalog.owner_record", return_value=owner), patch("misaka.core.session_control.request", new_callable=AsyncMock) as request:
                request.return_value = {"id": "old", "state": "working", "entries": []}
                result = bridge.dispatch("session_snapshot", {"session_id": "old"})
                self.assertEqual(result["capabilities"], [])
                request.side_effect = ValueError("Unknown session operation: models")
                with self.assertRaisesRegex(ValueError, "旧版进程"):
                    bridge.dispatch("session_models", {"session_id": "old"})
                self.assertEqual(request.await_count, 2, "never retry a mutation or create a second owner")

    def test_code_and_image_previews_are_read_only_and_project_confined(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bridge = Bridge(str(root))
            code = root / "example.js"
            code.write_bytes(b"const answer = 42;")
            self.assertEqual(bridge.dispatch("files", {"path": code.name})["content"], "const answer = 42;")
            image = root / "image.png"
            raw = b"\x89PNG\r\n\x1a\n"
            image.write_bytes(raw)
            self.assertTrue(bridge.dispatch("files", {"path": image.name})["image"].startswith("data:image/png;base64,"))
            self.assertEqual(image.read_bytes(), raw)
            with self.assertRaisesRegex(ValueError, "当前项目"):
                bridge.dispatch("files", {"path": "../image.png"})

    def test_settings_and_queue_events_update_metadata(self):
        channel = ChatChannel("c", {}, "log")
        channel.record({"type": "ready", "model": {"id": "first"}})
        channel.record({"type": "session_settings", "model": {"id": "second"}, "thinkingLevel": "high"})
        channel.record({"type": "prompt_queue", "pendingPrompts": [{"id": "p", "text": "next"}]})
        self.assertEqual(channel.meta["model"]["id"], "second")
        self.assertEqual(channel.meta["thinkingLevel"], "high")
        manager = ChatManager(".")
        manager.channels["c"] = channel
        self.assertEqual(manager.events({"chat_id": "c", "wait": 0})["meta"]["pendingPrompts"][0]["id"], "p")

    def test_manager_forwards_message_identity_and_promotion(self):
        manager = ChatManager(".")
        manager.request = Mock(return_value={"accepted": True})
        manager.send({"text": "next", "message_id": "p", "streamingBehavior": "followUp"})
        self.assertEqual(manager.request.call_args.args[2]["message_id"], "p")
        self.assertEqual(manager.request.call_args.args[2]["streamingBehavior"], "followUp")
        manager.send_now({"message_id": "p"})
        self.assertEqual(manager.request.call_args.args[1:], ("send_now", {"message_id": "p"}))
        manager.withdraw({"message_id": "p"})
        self.assertEqual(manager.request.call_args.args[1:], ("withdraw", {"message_id": "p"}))

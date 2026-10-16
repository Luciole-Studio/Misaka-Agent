"""Project isolation and native GUI regressions; no provider calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from misaka.ui.gui.chats import ChatChannel, ChatManager
from misaka.ui.gui.server import Bridge

STUB = str(Path(__file__).with_name("fixtures") / "stub_chat_runner.py")


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.other = self.root / "另一个 项目"
        self.other.mkdir()
        self.bridge = Bridge(str(self.root))
        self.bridge.request = Mock(return_value={"panes": [], "cards": [], "sessions": []})
        self.bridge.ensure = Mock()

    def test_fast_reply_is_registered_before_send(self):
        manager = self.bridge.chats
        channel = ChatChannel("fast", {}, "unused")
        manager.channels["fast"] = channel
        channel.send_raw = lambda data: channel.record({"type": "result", "id": data["id"], "ok": True, "data": {"ok": 1}})
        self.assertEqual(manager.request({"chat_id": "fast"}, "ping", timeout=.1), {"ok": 1})
        self.assertFalse(channel.pending)

    def test_rename_reply_updates_server_metadata(self):
        manager = self.bridge.chats
        channel = ChatChannel("fast", {"name": "old"}, "unused")
        manager.channels["fast"] = channel
        channel.send_raw = lambda data: channel.record({"type": "result", "id": data["id"], "ok": True, "data": {"name": "new"}})
        manager.rename({"chat_id": "fast", "name": "new"})
        self.assertEqual(manager.list()[0]["name"], "new")

    def test_snapshot_cursor_matches_reply_position(self):
        manager = self.bridge.chats
        channel = ChatChannel("fast", {}, "unused")
        manager.channels["fast"] = channel
        def reply(data):
            channel.record({"type": "event", "event": {"type": "agent_start"}})
            channel.record({"type": "result", "id": data["id"], "ok": True, "data": {"messages": []}})
            channel.record({"type": "event", "event": {"type": "agent_end"}})
        channel.send_raw = reply
        result = manager.request({"chat_id": "fast"}, "snapshot")
        self.assertEqual(result["cursor"], 1)
        self.assertEqual(manager.events({"chat_id": "fast", "cursor": result["cursor"], "wait": 0})["events"][0]["event"]["type"], "agent_end")

    def test_new_runner_uses_selected_project(self):
        manager = ChatManager(str(self.root), program=[sys.executable, "-u", STUB])
        self.addCleanup(manager.close_all)
        key = manager.create({"workspace": str(self.other)})["chat_id"]
        deadline = time.time() + 5
        while manager.channels[key].status == "starting" and time.time() < deadline:
            time.sleep(.01)
        meta = manager.channels[key].meta
        self.assertEqual(meta["workspace"], str(self.other.resolve()))
        self.assertEqual(meta["cwd"], str(self.other.resolve()))

    def test_state_filters_chats_by_project(self):
        self.bridge.chats.list = Mock(return_value=[
            {"id": "a", "workspace": str(self.root.resolve())},
            {"id": "b", "workspace": str(self.other.resolve())}])
        self.assertEqual([c["id"] for c in self.bridge.state({"workspace": str(self.other)})["chats"]], ["b"])

    def test_resume_reuses_live_native_owner(self):
        path = self.root / "session.jsonl"
        path.write_text(json.dumps({"type": "session", "cwd": str(self.root), "id": "s"}) + "\n", encoding="utf-8")
        c = ChatChannel("same", {"sourceSession": str(path)}, "unused")
        c.status = "starting"
        self.bridge.chats.channels[c.id] = c
        with patch("misaka.config.sessions.chat_dir", return_value=str(self.root)):
            self.assertEqual(self.bridge.native_chat({"session_path": str(path)}), {"chat_id": "same"})

    def test_resume_rejects_foreign_project(self):
        path = self.root / "session.jsonl"
        path.write_text(json.dumps({"type": "session", "cwd": str(self.other), "id": "s"}) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "其他项目"):
            self.bridge.native_chat({"session_path": str(path)})

    def test_foreign_card_cannot_be_started(self):
        with self.assertRaisesRegex(ValueError, "当前项目"):
            self.bridge.dispatch("card", {"task_id": "foreign", "operation": "run"})
        self.assertFalse(any(c.args[0] == "pane.run_card" for c in self.bridge.request.call_args_list))

    def test_folder_browser_lists_only_directories(self):
        (self.root / "note.md").write_text("text", encoding="utf-8")
        result = self.bridge.dispatch("folders", {})
        self.assertEqual(result["entries"], [{"name": self.other.name, "path": str(self.other.resolve())}])

    def test_interactive_command_is_not_run_as_output_job(self):
        self.bridge.jobs.start = Mock()
        for args in [["setup", "model"], ["research", "hello"], ["doc", 4]]:
            with self.assertRaises(ValueError):
                self.bridge.dispatch("command_output", {"args": args})
        self.bridge.jobs.start.assert_not_called()

    def test_attached_snapshot_renders_messages_without_a_pane(self):
        self.bridge.request.return_value = {"sessions": [{"id": "s", "path": "session.jsonl", "state": "active"}]}
        snapshot = {"entries": [{"type": "message", "message": {"role": "user", "content": "你好"}}],
                    "streaming": {"role": "assistant", "content": [{"type": "text", "text": "正在回答"}]}, "state": "working"}
        with patch("misaka.core.session_catalog.owner_record", return_value={"control": "socket"}), patch("misaka.core.session_control.request", new=AsyncMock(return_value=snapshot)):
            result = self.bridge.dispatch("session_snapshot", {"session_id": "s"})
        self.assertEqual(len(result["messages"]), 2)
        self.assertFalse(result["readonly"])
        self.assertEqual(self.bridge.request.call_args.args[0], "cards.list")

    def test_attached_input_rejects_foreign_session(self):
        with self.assertRaisesRegex(ValueError, "当前项目"):
            self.bridge.dispatch("session_input", {"session_id": "foreign", "text": "hello"})

    def test_native_command_preserves_project_and_literal_arguments(self):
        self.bridge.jobs.start = Mock(return_value={"job_id": "j"})
        args = ["doc", "find", "中文 & echo untouched"]
        self.bridge.dispatch("command_output", {"workspace": str(self.other), "args": args})
        self.assertEqual(self.bridge.jobs.start.call_args.args[:2], (str(self.other.resolve()), args))


if __name__ == "__main__":
    unittest.main()

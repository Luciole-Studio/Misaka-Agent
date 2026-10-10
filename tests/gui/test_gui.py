"""GUI contract tests: no user home changes and no paid model calls."""
import base64
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from misaka.ui.gui.chats import ChatManager, RING_LIMIT
from misaka.ui.gui.server import Bridge, GUIServer, research_args, workspace_path

STUB_RUNNER = str(Path(__file__).with_name("fixtures").joinpath("stub_chat_runner.py"))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = Bridge(str(self.root))
        self.bridge.ensure = Mock()
        self.bridge.request = Mock(return_value={"pane_id": "p1"})

    def test_research_goal_is_positional_and_bounds_are_enforced(self):
        argv = research_args({"goal": "--中文问题\n第二行", "depth": 0})
        self.assertEqual(argv[-2:], ["--", "--中文问题\n第二行"])
        from misaka.cli.app import _parser
        parsed = _parser().parse_args(argv)
        self.assertEqual(parsed.goal, "--中文问题\n第二行")
        for value in [True, "3", 3.2, -1, 11]:
            with self.assertRaises(ValueError):
                research_args({"goal": "问题", "depth": value})

    def test_open_terminal_uses_selected_project_and_validates_directory(self):
        folder = self.root / "另一个项目"
        folder.mkdir()
        with patch("misaka.ui.gui.server.open_terminal", return_value={"terminal": "cmd"}) as launch:
            result = self.bridge.dispatch("open_terminal", {"workspace": str(folder)})
            launch.assert_called_once_with(str(folder.resolve()))
            self.assertEqual(result["terminal"], "cmd")
            launch.reset_mock()
            with self.assertRaises(FileNotFoundError):
                self.bridge.dispatch("open_terminal", {"workspace": str(folder / "missing")})
            launch.assert_not_called()

    def test_launch_uses_argv_without_shell(self):
        self.bridge.dispatch("command", {"args": ["doc", "find", "中文 & echo test"]})
        method, params = self.bridge.request.call_args.args
        self.assertEqual(method, "pane.create")
        self.assertEqual(params["argv"][-3:], ["doc", "find", "中文 & echo test"])
        self.assertEqual(params["cwd"], str(self.root.resolve()))
        for args in [["powershell"], ["net-daemon"], ["uninstall"], ["gui"], ["chat", 5], "chat"]:
            with self.assertRaises(ValueError):
                self.bridge.dispatch("command", {"args": args})

    def test_utf8_multiline_paste_obeys_terminal_mode(self):
        for bracketed in (True, False):
            self.bridge.request.reset_mock()
            self.bridge.request.side_effect = lambda method, params: {"input": {"bracketed_paste": bracketed}} if method == "pane.screen" else {"ok": True}
            self.bridge.dispatch("send", {"id": "p1", "text": "中文\nsecond"})
            calls = self.bridge.request.call_args_list
            first = base64.b64decode(calls[1].args[1]["data"]).decode()
            self.assertEqual(first, "\x1b[200~中文\nsecond\x1b[201~" if bracketed else "中文\rsecond")
            self.assertEqual(base64.b64decode(calls[2].args[1]["data"]), b"\r")

    def test_input_without_enter_and_key(self):
        self.bridge.dispatch("input", {"id": "p1", "text": " "})
        self.assertEqual(base64.b64decode(self.bridge.request.call_args.args[1]["data"]), b" ")
        self.bridge.dispatch("key", {"id": "p1", "key": "escape"})
        self.assertEqual(base64.b64decode(self.bridge.request.call_args.args[1]["data"]), b"\x1b")
        with self.assertRaises(ValueError):
            self.bridge.dispatch("key", {"id": "p1", "key": "invalid"})

    def test_file_preview_is_read_only_and_confined(self):
        source = self.root / "中文.md"
        raw = "# 中文\r\n第二行".encode("utf-8-sig")
        source.write_bytes(raw)
        self.assertEqual(self.bridge.dispatch("files", {"path": "中文.md"})["content"], "# 中文\r\n第二行")
        self.assertEqual(source.read_bytes(), raw)
        with self.assertRaises(ValueError):
            self.bridge.dispatch("files", {"path": "../"})
        source.write_bytes("编码".encode("gbk"))
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            self.bridge.dispatch("files", {"path": "中文.md"})

    def test_live_session_reuses_its_pane(self):
        self.bridge.request.side_effect = lambda method, params=None: {"sessions": [{"id": "s1", "path": "session.jsonl"}]} if method == "cards.list" else {"panes": [{"id": "p9", "alive": True, "reported": {"session": "session.jsonl"}}]}
        result = self.bridge.dispatch("session", {"session_id": "s1", "read_only": False})
        self.assertEqual(result, {"pane_id": "p9"})
        self.assertFalse(any(c.args[0] == "pane.create" for c in self.bridge.request.call_args_list))

    def test_lost_reply_does_not_retry_mutation(self):
        self.bridge.request.side_effect = TimeoutError("结果未知")
        with self.assertRaises(TimeoutError):
            self.bridge.dispatch("chat", {})
        self.assertEqual(self.bridge.request.call_count, 1)

    def test_saved_sister_keeps_role_and_task_history_is_read_only(self):
        entry = {"id": "s1", "path": "session.jsonl", "role": "10032", "kind": "foreground", "state": "saved"}
        self.bridge.request.side_effect = lambda method, params=None: {"sessions": [entry]} if method == "cards.list" else {"panes": []}
        self.bridge.launch = Mock(return_value={"pane_id": "p7"})
        with patch("misaka.config.sisters", return_value={"10032": {}}):
            self.bridge.dispatch("session", {"session_id": "s1", "read_only": False})
            self.assertEqual(self.bridge.launch.call_args.args[1], ["chat", "--session", "session.jsonl", "--as", "10032"])
            entry["kind"] = "card"
            self.bridge.dispatch("session", {"session_id": "s1", "read_only": False})
            self.assertIn("--read-only", self.bridge.launch.call_args.args[1])

    def test_key_uses_negotiated_terminal_protocol(self):
        self.bridge.request.side_effect = lambda method, params=None: {"input": {"application_cursor": True}} if method == "pane.screen" else {"ok": True}
        self.bridge.dispatch("key", {"id": "p1", "key": "up"})
        self.assertEqual(base64.b64decode(self.bridge.request.call_args.args[1]["data"]), b"\x1bOA")

    def test_setup_sections_validated(self):
        with self.assertRaises(ValueError):
            self.bridge.dispatch("setup", {"section": "../unknown"})

    def test_missing_workspace_is_rejected(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            workspace_path(str(self.root / "not-there"))

    def test_native_chat_rejects_unknown_sister(self):
        with self.assertRaises(ValueError):
            self.bridge.dispatch("chat_native", {"role": "99999"})

    def test_native_chat_refuses_to_double_own_a_live_pane_session(self):
        session = self.root / "live.jsonl"
        session.write_text('{"type":"session","id":"x"}\n', encoding="utf-8")
        self.bridge.request.side_effect = lambda method, params=None: {
            "panes": [{"id": "p1", "alive": True, "argv": ["chat", "--session", str(session)], "reported": {"session": str(session)}}]
        } if method == "panes.list" else {}
        with self.assertRaisesRegex(ValueError, "终端"):
            self.bridge.dispatch("chat_native", {"session_path": str(session)})

    def test_chat_snapshot_validates_path(self):
        with self.assertRaises(ValueError):
            self.bridge.dispatch("chat_snapshot", {"path": str(self.root / "missing.jsonl")})
        not_session = self.root / "notes.txt"
        not_session.write_text("hello", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.bridge.dispatch("chat_snapshot", {"path": str(not_session)})

    def test_settings_ops_include_custom_services(self):
        from misaka.ui.gui.server import SETTINGS_OPS
        from misaka.ui.gui.services import SLOW_OPS
        from misaka.ui.gui.settings_worker import OPS
        for name in ("probe_custom", "ping_custom", "save_custom", "remove_custom"):
            self.assertIn(name, SETTINGS_OPS)
            self.assertIn(name, OPS)
        self.assertGreaterEqual(SLOW_OPS["probe_custom"], 45)
        self.assertGreaterEqual(SLOW_OPS["ping_custom"], 45)


class NativeChatManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        import sys
        self.manager = ChatManager(str(self.temp.name), program=[sys.executable, "-u", STUB_RUNNER])
        self.addCleanup(self.manager.close_all)

    def _start(self):
        return self.manager.create({})["chat_id"]

    def wait_for(self, chat_id, predicate, timeout=15):
        deadline = time.time() + timeout
        cursor = 0
        while time.time() < deadline:
            page = self.manager.events({"chat_id": chat_id, "cursor": cursor, "wait": 0.2})
            for event in page["events"]:
                cursor = event["seq"] + 1
                if predicate(event):
                    return page
            if page["status"] not in ("starting", "ready"):
                break
        self.fail("等待事件超时")

    def test_stub_runner_streams_events_and_answers_ops(self):
        chat_id = self._start()
        self.wait_for(chat_id, lambda ev: ev["type"] == "ready")
        self.assertEqual(self.manager.request({"chat_id": chat_id}, "ping"), {"op": "ping"})
        self.manager.request({"chat_id": chat_id}, "note", {"text": "中文消息"})
        self.wait_for(chat_id, lambda ev: ev.get("event", {}).get("text") == "中文消息")
        with self.assertRaisesRegex(ValueError, "炸了"):
            self.manager.request({"chat_id": chat_id}, "boom")
        self.assertIn(self.manager.list()[0]["status"], ("starting", "ready"))

    def test_unknown_chat_and_bad_cursor_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.events({"chat_id": "nope", "cursor": 0})
        chat_id = self._start()
        with self.assertRaises(ValueError):
            self.manager.events({"chat_id": chat_id, "cursor": -1})
        with self.assertRaises(ValueError):
            self.manager.request({"chat_id": "nope"}, "ping")

    def test_ring_trim_forces_full_replay(self):
        from misaka.ui.gui.chats import ChatChannel
        channel = ChatChannel("trim", {}, "unused.log")
        self.manager.channels["trim"] = channel
        for index in range(RING_LIMIT + 50):
            channel.record({"type": "event", "event": {"type": "tick", "index": index}})
        page = self.manager.events({"chat_id": "trim", "cursor": 5, "wait": 0})
        self.assertTrue(page["reset"])
        self.assertEqual(page["cursor"], channel.next_seq)
        self.assertEqual(page["events"][0]["seq"], channel.base_seq)
        fresh = self.manager.events({"chat_id": "trim", "cursor": 0, "wait": 0})
        self.assertTrue(fresh["reset"])   # cursor 0 is below base too; client rebuilds from the ring
        self.assertEqual(fresh["events"][0]["seq"], channel.base_seq)
        live = self.manager.events({"chat_id": "trim", "cursor": channel.next_seq, "wait": 0})
        self.assertFalse(live["reset"])
        self.assertEqual(live["events"], [])

    def test_close_terminates_stub_runner(self):
        chat_id = self._start()
        self.wait_for(chat_id, lambda ev: ev["type"] == "ready")
        self.assertTrue(self.manager.close(chat_id)["closed"])
        self.assertEqual(self.manager.list()[0]["status"], "closed")


@contextmanager
def running_server():
    bridge = Mock()
    bridge.dispatch.return_value = {"message": "中文成功"}
    server = GUIServer(("127.0.0.1", 0), bridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = running_server()
        cls.server = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def request(self, method="POST", path="/api/state", data=b"{}", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        status, body, response_headers = response.status, response.read(), dict(response.getheaders())
        conn.close()
        return status, body, response_headers

    def auth(self, **headers):
        return {"X-Misaka-Token": self.server.token, **headers}

    def test_api_requires_capability_even_for_reads(self):
        self.assertEqual(self.request()[0], 403)
        self.assertEqual(self.request(headers={"X-Misaka-Token": "wrong"})[0], 403)
        status, body, _ = self.request(headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["message"], "中文成功")

    def test_cross_origin_and_dns_rebinding_rejected(self):
        self.assertEqual(self.request(headers=self.auth(Origin="https://attacker.test"))[0], 403)
        self.assertEqual(self.request(headers=self.auth(Host="attacker.test"))[0], 403)
        self.assertEqual(self.request(headers=self.auth(Origin=self.server.origin))[0], 200)

    def test_static_files_do_not_contain_token_or_allow_traversal(self):
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(self.server.token.encode(), body)
        self.assertIn("研究工作台", body.decode("utf-8"))
        icon_status, icon, _ = self.request("GET", "/favicon.ico")
        self.assertEqual(icon_status, 200)
        self.assertEqual(icon[:4], b"\x00\x00\x01\x00")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("GET", "/../../server.py")[0], 404)

    def test_frontend_stays_paired_with_running_backend_after_source_changes(self):
        with patch("misaka.ui.gui.server.STATIC", Path("missing-new-source")):
            for path, asset in [("/", "index.html"), ("/app.js", "app.js"), ("/style.css", "style.css")]:
                status, body, _ = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(body, self.server.assets[asset])

    def test_malformed_and_oversized_requests(self):
        for data in [b"[]", b"invalid"]:
            self.assertEqual(self.request(data=data, headers=self.auth())[0], 400)
        # A declared oversized body is rejected before receiving it, including
        # clients that pause waiting for a response instead of sending the body.
        self.assertEqual(self.request(data=b"", headers=self.auth(**{"Content-Length": "262145"}))[0], 400)

    def test_backend_failures_are_visible(self):
        with patch.object(self.server.bridge, "dispatch", side_effect=RuntimeError("启动失败")):
            status, body, _ = self.request(headers=self.auth())
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"], "启动失败")

    def test_health_reports_current_gui_protocol(self):
        status, body, _ = self.request(path="/api/health", headers=self.auth())
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["native_chat"])
        self.assertEqual(payload["project_gui"], 12)
        self.assertEqual(payload["source_revision"], self.server.source_revision)
        self.assertEqual(len(payload["source_revision"]), 64)


if __name__ == "__main__":
    unittest.main()

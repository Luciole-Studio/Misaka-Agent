"""Conversation management and composer contracts, with disposable data and no model calls."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from misaka.ui.gui.composer import ComposerFiles, referenced_message, skill_catalogue
from misaka.ui.gui.server import Bridge
from misaka.ui.gui.session_library import SessionLibrary, path_key


class SessionComposerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "项目"
        self.project.mkdir()
        self.other = self.root / "其他项目"
        self.other.mkdir()
        self.bridge = Bridge(str(self.project))
        self.bridge.session_library = SessionLibrary(self.root / "preferences.json")
        self.bridge.request = Mock(return_value={"sessions": [], "panes": []})
        self.transcript = self.root / "transcript.jsonl"
        self.transcript.write_text('{"type":"session"}\n', encoding="utf-8")
        self.original = self.transcript.read_bytes()
        self.session = {"id": "s1", "path": str(self.transcript), "title": "测试对话", "role": None, "workspace": str(self.project)}
        self.bridge.saved_chat_sessions = Mock(return_value={"sessions": [self.session]})

    def update(self, op, **args):
        return self.bridge.dispatch("session_update", {"path": str(self.transcript), "op": op, **args})

    def test_metadata_survives_instances_and_preserves_renamed_title(self):
        self.update("rename", value="新名称")
        self.update("pin", value=True)
        self.update("delete", confirmed=True)
        self.update("restore")
        entry = SessionLibrary(self.bridge.session_library.path).preferences(self.project)[path_key(self.transcript)]
        self.assertEqual(entry["title"], "新名称")
        self.assertGreater(entry["pinned_at"], 0)
        self.assertFalse(entry["deleted_at"])
        self.assertEqual(self.transcript.read_bytes(), self.original)
        self.assertEqual(self.bridge.session_library.preferences(self.other), {})

    def test_delete_requires_confirmation_and_only_closes_gui_owner(self):
        self.bridge.chats.list = Mock(return_value=[{"id": "gui1", "workspace": str(self.project), "sessionFile": str(self.transcript), "status": "ready"}])
        self.bridge.chats.close = Mock()
        with self.assertRaisesRegex(ValueError, "确认"):
            self.update("delete")
        self.bridge.chats.close.assert_not_called()
        self.update("delete", confirmed=True)
        self.bridge.chats.close.assert_called_once_with("gui1")
        self.assertTrue(all(c.args[0] == "cards.list" for c in self.bridge.request.call_args_list))
        self.assertEqual(self.transcript.read_bytes(), self.original)

    def test_task_delete_retains_task_owner_and_can_restore_from_trash(self):
        self.bridge.saved_chat_sessions.return_value = {"sessions": []}
        self.bridge.request.return_value = {"sessions": [self.session]}
        self.bridge.chats.close = Mock()
        self.update("delete", confirmed=True)
        self.bridge.chats.close.assert_not_called()
        self.assertEqual(len(self.bridge.dispatch("session_trash", {})["sessions"]), 1)
        self.bridge.request.return_value = {"sessions": []}
        self.update("restore")
        self.assertEqual(self.bridge.dispatch("session_trash", {})["sessions"], [])

    def test_unknown_or_foreign_session_is_rejected(self):
        self.bridge.saved_chat_sessions.return_value = {"sessions": []}
        with self.assertRaisesRegex(ValueError, "当前项目"):
            self.update("pin", value=True)
        self.assertFalse(self.bridge.session_library.path.exists())

    def test_unknown_encoding_is_not_overwritten_and_bom_is_preserved(self):
        store = self.bridge.session_library
        store.path.write_bytes(b"\xff\xfeunknown")
        with self.assertRaises(UnicodeDecodeError):
            store.update(self.session, "pin", True)
        self.assertEqual(store.path.read_bytes(), b"\xff\xfeunknown")
        store.path.write_bytes(b"\xef\xbb\xbf{}")
        store.update(self.session, "pin", True)
        self.assertTrue(store.path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_old_saved_sessions_are_not_capped_and_deleted_entries_are_filtered(self):
        original_listing = Bridge.saved_chat_sessions.__get__(self.bridge)
        rows = [SimpleNamespace(id=str(i), path=str(self.root / f"{i}.jsonl"), name=f"对话 {i}", firstMessage="", modified=SimpleNamespace(timestamp=lambda: 1), messageCount=1) for i in range(105)]
        # A normal comparable datetime is used for sorting.
        from datetime import datetime, timezone
        for row in rows:
            row.modified = datetime.now(timezone.utc)
        deleted = {**self.session, "path": rows[0].path}
        self.bridge.session_library.update(deleted, "delete")
        with patch("misaka.config.sessions.chat_dir", return_value=str(self.root)), patch("misaka.core.session_manager.SessionManager.list", new=Mock(side_effect=lambda *_: asyncio.sleep(0, result=rows))):
            visible = original_listing({})["sessions"]
            self.assertEqual(len(visible), 104)
            self.assertNotIn(rows[0].path, [s["path"] for s in visible])

    def test_files_search_handles_unicode_spaces_and_excludes_generated_directories(self):
        (self.project / "资料").mkdir()
        (self.project / "资料" / "中文 笔记.md").write_text("正文", encoding="utf-8")
        (self.project / "node_modules").mkdir()
        (self.project / "node_modules" / "hidden.md").write_text("generated", encoding="utf-8")
        result = ComposerFiles().search(self.project, "中文")
        self.assertEqual([f["path"] for f in result["files"]], ["资料/中文 笔记.md"])
        self.assertFalse(any("node_modules" in f["path"] for f in ComposerFiles().search(self.project)["files"]))
        self.assertEqual(ComposerFiles().search(self.other)["files"], [])

    def test_file_references_cannot_escape_project_and_reach_native_prompt(self):
        note = self.project / "中文 笔记.md"
        note.write_text("正文", encoding="utf-8")
        for bad in ["../transcript.jsonl", str(self.transcript), "missing.md"]:
            with self.assertRaisesRegex(ValueError, "不属于|不存在"):
                referenced_message("请看", [bad], self.project)
        channel = SimpleNamespace(meta={"workspace": str(self.project)})
        self.bridge.chats._channel = Mock(return_value=channel)
        self.bridge.chats.send = Mock(return_value={"accepted": True})
        self.bridge.dispatch("chat_send", {"chat_id": "c", "text": '请看 @"中文 笔记.md"', "files": [note.name, note.name]})
        text = self.bridge.chats.send.call_args.args[0]["text"]
        self.assertEqual(text.count(str(note)), 1)
        self.assertIn("引用的当前项目文件", text)

    def test_skills_in_live_chat_use_existing_runner_catalogue(self):
        self.bridge.chats._channel = Mock(return_value=SimpleNamespace(meta={"workspace": str(self.project)}))
        self.bridge.chats.request = Mock(return_value={"skills": [{"name": "provider:example", "description": "技能"}]})
        result = self.bridge.dispatch("composer_skills", {"chat_id": "c"})
        self.assertEqual(result["skills"][0]["name"], "provider:example")
        self.assertEqual(self.bridge.chats.request.call_args.args[1], "skills")
        with self.assertRaisesRegex(ValueError, "其他项目"):
            self.bridge.dispatch("composer_skills", {"chat_id": "c", "workspace": str(self.other)})

    def test_file_references_also_reach_the_original_task_owner(self):
        note = self.project / "笔记.md"
        note.write_text("正文", encoding="utf-8")
        self.bridge.request.return_value = {"sessions": [self.session]}
        with patch("misaka.core.session_catalog.owner_record", return_value={"control": "socket"}), patch("misaka.core.session_control.request", new=AsyncMock(return_value={})) as owner:
            self.bridge.dispatch("session_input", {"session_id": "s1", "text": "分析笔记", "files": [note.name]})
        self.assertEqual(owner.call_args.args[1], "input")
        self.assertIn(str(note), owner.call_args.kwargs["text"])

    def test_older_live_runner_uses_its_own_role_and_project_without_restarting(self):
        self.bridge.chats._channel = Mock(return_value=SimpleNamespace(meta={"workspace": str(self.project), "role": "10086"}))
        self.bridge.chats.request = Mock(side_effect=ValueError("未知操作 'skills'"))
        self.bridge.chats.close = Mock()
        with patch("misaka.ui.gui.server.skill_catalogue", return_value={"skills": [{"name": "old-chat-skill"}]}) as catalogue:
            result = self.bridge.dispatch("composer_skills", {"chat_id": "c"})
            self.assertEqual(result["skills"][0]["name"], "old-chat-skill")
            catalogue.assert_called_once_with(str(self.project), "10086")
            with self.assertRaisesRegex(ValueError, "其他项目"):
                self.bridge.dispatch("composer_skills", {"chat_id": "c", "workspace": str(self.other)})
            catalogue.assert_called_once()
        self.bridge.chats.close.assert_not_called()

    def test_live_skill_errors_are_preserved_instead_of_hidden_by_fallback(self):
        self.bridge.chats._channel = Mock(return_value=SimpleNamespace(meta={"workspace": str(self.project)}))
        self.bridge.chats.request = Mock(side_effect=ValueError("技能目录不可读"))
        with patch("misaka.ui.gui.server.skill_catalogue") as catalogue:
            with self.assertRaisesRegex(ValueError, "技能目录不可读"):
                self.bridge.dispatch("composer_skills", {"chat_id": "c"})
            catalogue.assert_not_called()

    def test_runner_skill_operation_lists_available_skills_without_a_prompt(self):
        from misaka.ui.gui.chat_runner import ChatHost
        from misaka.core.skills.wiring.skills import SkillsPart
        part = SkillsPart.__new__(SkillsPart)
        part._entries = Mock(return_value=[{"name": "sample", "runtime_name": "provider:sample", "namespace": "provider", "description": "示例"}])
        host = ChatHost({})
        host.session = SimpleNamespace(moments=SimpleNamespace(parts=[part]), prompt=Mock())
        self.assertEqual(host.op_skills({}), {"skills": [{"name": "provider:sample", "description": "示例"}]})
        self.assertIn("skills", host.SYNC_OPS)
        host.session.prompt.assert_not_called()

    def test_home_skills_catalogue_retains_namespace_and_current_project(self):
        entries = [{"name": "example", "runtime_name": "provider:example", "namespace": "provider", "description": "技能"}]
        with patch("misaka.core.skills.index.runtime_build", return_value=entries) as build, patch("misaka.core.skills.layers.skill_roots", return_value=[str(self.project)]) as roots:
            result = skill_catalogue(str(self.project))
        self.assertEqual(result["skills"][0]["name"], "provider:example")
        self.assertEqual(roots.call_args.args[1], str(self.project))
        build.assert_called_once_with([str(self.project)])

    def test_legacy_gui_reads_skills_through_fresh_overview_worker_in_project_scope(self):
        from misaka.ui.gui.settings_worker import op_overview
        with patch("misaka.ui.gui.composer.skill_catalogue", return_value={"skills": [{"name": "sample"}]}) as catalogue, patch("os.getcwd", return_value=str(self.project)):
            self.assertEqual(op_overview({"section": "skills", "role": "10086"}), {"skills": [{"name": "sample"}]})
            catalogue.assert_called_once_with(str(self.project), "10086")

    def test_skill_catalogue_rejects_unknown_role_paths(self):
        with self.assertRaisesRegex(ValueError, "未知的 Sister"):
            skill_catalogue(str(self.project), "../../outside")

    def test_attached_session_role_aliases_use_the_correct_catalogue(self):
        from misaka.ui.gui.composer import skill_role
        self.assertIsNone(skill_role("last-order"))
        self.assertIsNone(skill_role("last_order"))
        self.assertEqual(skill_role("sister-10086"), "10086")
        with patch("misaka.ui.gui.server.skill_catalogue", return_value={"skills": []}) as catalogue, patch("misaka.ui.gui.server.sisters", return_value=["10086"]):
            self.bridge.dispatch("composer_skills", {"role": "last-order"})
            catalogue.assert_called_with(str(self.project), None)
            self.bridge.dispatch("composer_skills", {"role": "sister-10086"})
            catalogue.assert_called_with(str(self.project), "10086")


if __name__ == "__main__":
    unittest.main()

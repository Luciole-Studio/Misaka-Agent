"""Project bookmarks and OS picker contracts. Uses temporary state only."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from misaka.ui.gui.projects import Projects
from misaka.ui.gui.server import Bridge


class ProjectLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = Bridge(str(self.root))
        self.bridge.projects = Projects(self.root / "state" / "projects.json")

    def test_more_than_one_hundred_projects_survive_a_new_instance(self):
        paths = []
        for i in range(101):
            path = self.root / f"项目 {i}"
            path.mkdir()
            paths.append(str(path.resolve()))
        self.bridge.project_list({"paths": paths})
        self.bridge.project_list({"paths": [paths[0]]})
        self.assertEqual(Projects(self.bridge.projects.path).read(), paths)
        Path(paths[0]).rmdir()
        result = self.bridge.project_list({})["projects"]
        self.assertEqual(len(result), 101)
        self.assertFalse(result[0]["available"])

    def test_unknown_encoding_is_not_overwritten(self):
        store = self.bridge.projects
        store.path.parent.mkdir()
        store.path.write_bytes(b"\xff\xfeunknown")
        with self.assertRaises(UnicodeDecodeError):
            store.add([str(self.root)])
        self.assertEqual(store.path.read_bytes(), b"\xff\xfeunknown")

    def test_existing_utf8_bom_is_retained(self):
        store = self.bridge.projects
        store.path.parent.mkdir()
        store.path.write_bytes(b"\xef\xbb\xbf[]")
        store.add([str(self.root)])
        self.assertTrue(store.path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_invalid_folder_does_not_change_bookmarks(self):
        with self.assertRaises(OSError):
            self.bridge.project_list({"paths": [str(self.root / "missing")]})
        self.assertEqual(self.bridge.projects.read(), [])

    def test_picker_cancel_does_not_add_a_project(self):
        with patch("misaka.ui.gui.server.subprocess.run", return_value=Mock(stdout='{"path": null}', returncode=0)):
            self.assertEqual(self.bridge.pick_project_folder({}), {"path": None})
        self.assertEqual(self.bridge.projects.read(), [])

    def test_picker_returns_unicode_path_without_shell_interpolation(self):
        path = self.root / "中文 & 项目"
        path.mkdir()
        with patch("misaka.ui.gui.server.subprocess.run", return_value=Mock(stdout=json.dumps({"path": str(path)}), returncode=0)) as run:
            self.assertEqual(self.bridge.pick_project_folder({})["path"], str(path.resolve()))
            self.assertIsInstance(run.call_args.args[0], list)
            self.assertFalse(run.call_args.kwargs.get("shell"))

    def test_timeout_releases_picker_lock_and_parallel_picker_is_rejected(self):
        with patch("misaka.ui.gui.server.subprocess.run", side_effect=subprocess.TimeoutExpired("picker", 300)):
            with self.assertRaisesRegex(ValueError, "超时"):
                self.bridge.pick_project_folder({})
        self.assertTrue(self.bridge.picker_lock.acquire(False))
        try:
            with self.assertRaisesRegex(ValueError, "已打开"):
                self.bridge.pick_project_folder({})
        finally:
            self.bridge.picker_lock.release()

    def test_project_sessions_preserve_workspace_and_exclude_foreign_chats(self):
        path = str(self.root.resolve())
        self.bridge.project_list({"paths": [path]})
        self.bridge.chats.list = Mock(return_value=[{"workspace": path, "id": "a"}, {"workspace": path + "2", "id": "b"}])
        self.bridge.saved_chat_sessions = Mock(return_value={"sessions": [{"title": "历史"}]})
        self.bridge.request = Mock(return_value={"sessions": []})
        result = self.bridge.project_sessions({"workspace": path})
        self.assertEqual([c["id"] for c in result["chats"]], ["a"])
        self.bridge.saved_chat_sessions.assert_called_once_with({"workspace": path, "all_roles": True})
        self.bridge.request.assert_called_once_with("cards.list", {"workspace": path})


if __name__ == "__main__":
    unittest.main()

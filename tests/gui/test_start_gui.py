"""Launcher workspace selection stays independent of any one machine."""
import importlib.util
from pathlib import Path
import io
import json
from unittest.mock import Mock, patch
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "start-gui.py"
SPEC = importlib.util.spec_from_file_location("start_gui_under_test", SOURCE)
start_gui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(start_gui)


class WorkspaceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.saved_runtime = start_gui.RUNTIME
        self.saved_remembered = start_gui.REMEMBERED
        self.saved_pick = start_gui.pick_folder
        start_gui.RUNTIME = self.root / "runtime"
        start_gui.REMEMBERED = start_gui.RUNTIME / "workspace.txt"
        start_gui.pick_folder = lambda: (_ for _ in ()).throw(AssertionError("不应弹出文件夹选择"))
        self.addCleanup(self.restore)

    def restore(self):
        start_gui.RUNTIME = self.saved_runtime
        start_gui.REMEMBERED = self.saved_remembered
        start_gui.pick_folder = self.saved_pick

    def test_explicit_folder_is_remembered(self):
        project = self.root / "project"
        project.mkdir()
        resolved = start_gui.resolve_workspace(str(project))
        self.assertEqual(resolved, project.resolve())
        self.assertEqual(start_gui.remembered_workspace().resolve(), project.resolve())

    def test_next_launch_reuses_the_remembered_folder(self):
        project = self.root / "project"
        project.mkdir()
        start_gui.remember_workspace(project)
        self.assertEqual(start_gui.resolve_workspace(None), project.resolve())

    def test_missing_folder_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "文件夹"):
            start_gui.resolve_workspace(str(self.root / "missing"))

    def test_without_a_saved_folder_it_does_not_invent_one(self):
        start_gui.pick_folder = lambda: None
        with self.assertRaisesRegex(RuntimeError, "gui.cmd --workspace"):
            start_gui.resolve_workspace(None)


class ServerReuseTests(unittest.TestCase):
    def healthy(self, health):
        response = io.BytesIO(json.dumps(health).encode("utf-8"))
        opener = Mock()
        opener.open.return_value = response
        with patch.object(start_gui.urllib.request, "build_opener", return_value=opener):
            return start_gui.healthy("http://127.0.0.1:9150/#token=test", Path.cwd())

    def test_new_revision_gets_new_logs_without_overwriting_the_old_server(self):
        with patch.object(start_gui, "source_revision", side_effect=["a" * 64, "b" * 64]):
            old_log, old_error = start_gui.runtime_logs()
            new_log, new_error = start_gui.runtime_logs()
        self.assertNotEqual(old_log, new_log)
        self.assertNotEqual(old_error, new_error)
        self.assertEqual(new_log.parent, start_gui.RUNTIME)

    def test_same_protocol_without_a_source_revision_is_not_reused(self):
        self.assertFalse(self.healthy({"version": "0.18.5", "native_chat": True, "project_gui": 12}))

    def test_old_source_snapshot_is_not_reused(self):
        self.assertFalse(self.healthy({"version": "0.18.5", "native_chat": True,
                                      "project_gui": 12, "source_revision": "old"}))

    def test_current_source_snapshot_is_reused(self):
        self.assertTrue(self.healthy({"version": "0.18.5", "native_chat": True,
                                     "project_gui": 12,
                                     "source_revision": start_gui.source_revision(start_gui.SOURCE)}))

    def test_frontend_and_model_handler_edits_change_the_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            names = ["misaka/ui/gui/server.py", "misaka/ui/gui/static/index.html",
                     "misaka/ui/gui/static/app.js", "misaka/ui/gui/static/style.css",
                     "misaka/core/session_control.py", "misaka/core/network/roster.py"]
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            before = start_gui.source_revision(root)
            (root / "misaka/ui/gui/static/app.js").write_bytes(b"new frontend")
            after_frontend = start_gui.source_revision(root)
            self.assertNotEqual(before, after_frontend)
            (root / "misaka/core/network/roster.py").write_bytes(b"new model menu")
            self.assertNotEqual(after_frontend, start_gui.source_revision(root))


if __name__ == "__main__":
    unittest.main()

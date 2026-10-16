"""Side-panel routes: file metadata, local open, and Git changes inside one project."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from misaka.ui.gui import workbench
from misaka.ui.gui.server import Bridge


class FileRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "资料").mkdir()
        (self.root / "资料" / "笔记.md").write_text("# 笔记\n", encoding="utf-8", newline="\n")
        (self.root / "paper.tex").write_text("\\section{引言}\n", encoding="utf-8", newline="\n")
        (self.root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        self.bridge = Bridge(str(self.root))
        self.bridge.ensure = Mock()
        self.bridge.request = Mock(side_effect=ConnectionError("no daemon"))

    def test_listing_and_preview_carry_size_and_time(self):
        listing = self.bridge.dispatch("files", {"path": "."})
        names = [e["name"] for e in listing["entries"]]
        self.assertEqual(names, ["资料", "paper.tex"])
        folder, tex = listing["entries"]
        self.assertIsNone(folder["size"])
        self.assertEqual(tex["size"], len("\\section{引言}\n".encode("utf-8")))
        self.assertGreater(tex["modified"], 0)
        preview = self.bridge.dispatch("files", {"path": "资料/笔记.md"})
        self.assertEqual(preview["content"], "# 笔记\n")
        self.assertEqual(preview["path"], "资料/笔记.md")
        self.assertIn("size", preview)
        self.assertEqual(self.bridge.dispatch("files", {"path": "paper.tex"})["content"], "\\section{引言}\n")

    def test_secrets_and_outside_paths_are_not_previewed(self):
        with self.assertRaises(ValueError):
            self.bridge.dispatch("files", {"path": ".env"})
        with self.assertRaises(ValueError):
            self.bridge.dispatch("files", {"path": "../outside.md"})


class OpenPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "报告.md").write_text("报告", encoding="utf-8")
        (self.root / "run.bat").write_text("echo hi", encoding="utf-8")
        launch = patch.object(workbench.subprocess, "Popen")
        start = patch.object(workbench.os, "startfile", create=True)
        which = patch.object(workbench.shutil, "which", return_value="/usr/bin/xdg-open")
        self.popen, self.startfile = launch.start(), start.start()
        which.start()
        for p in (launch, start, which):
            self.addCleanup(p.stop)

    def launched(self):
        return self.popen.call_count + self.startfile.call_count

    def test_opens_project_files_and_reveals_scripts_without_running_them(self):
        self.assertIn("报告.md", workbench.open_path(str(self.root), "报告.md")["message"])
        self.assertEqual(self.launched(), 1)
        with self.assertRaises(ValueError):
            workbench.open_path(str(self.root), "run.bat")
        self.assertEqual(self.launched(), 1)
        workbench.open_path(str(self.root), "run.bat", reveal=True)
        self.assertEqual(self.launched(), 2)

    def test_paths_outside_the_project_are_refused(self):
        for value in ["../", "../../etc/passwd", "missing.md"]:
            with self.assertRaises(ValueError):
                workbench.open_path(str(self.root), value)
        self.assertEqual(self.launched(), 0)

    def test_bridge_route_passes_reveal_flag(self):
        bridge = Bridge(str(self.root))
        with patch("misaka.ui.gui.server.open_path", return_value={"message": "ok"}) as route:
            bridge.dispatch("open_path", {"path": "报告.md", "reveal": 1})
        route.assert_called_once_with(str(self.root.resolve()), "报告.md", True)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "仓库"
        (self.repo / "子项目").mkdir(parents=True)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.git("config", "core.autocrlf", "false")
        (self.repo / "子项目" / "笔记.md").write_text("第一行\n第二行\n", encoding="utf-8")
        (self.repo / "根.txt").write_text("root\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "init")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def test_status_counts_and_patches_stay_inside_the_project(self):
        (self.repo / "子项目" / "笔记.md").write_text("第一行\n改过的第二行\n第三行\n", encoding="utf-8")
        (self.repo / "子项目" / "新文件.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.repo / "根.txt").write_text("changed outside the project\n", encoding="utf-8")
        project = str(self.repo / "子项目")
        state = workbench.git_changes(project)
        self.assertTrue(state["repo"])
        files = {f["path"]: f for f in state["files"]}
        self.assertEqual(set(files), {"笔记.md", "新文件.csv"})
        self.assertEqual((files["笔记.md"]["adds"], files["笔记.md"]["dels"]), (2, 1))
        self.assertEqual(files["笔记.md"]["status"], "modified")
        self.assertEqual((files["新文件.csv"]["status"], files["新文件.csv"]["adds"]), ("untracked", 2))
        patch_text = workbench.git_diff(project, "笔记.md")["patch"]
        self.assertIn("+改过的第二行", patch_text)
        self.assertIn("-第二行", patch_text)
        untracked = workbench.git_diff(project, "新文件.csv")
        self.assertTrue(untracked["untracked"])
        self.assertIn("+1,2", untracked["patch"])
        with self.assertRaises(ValueError):
            workbench.git_diff(project, "../根.txt")

    def test_clean_and_non_repository_projects(self):
        self.assertEqual(workbench.git_changes(str(self.repo))["files"], [])
        plain = Path(self.temp.name) / "普通文件夹"
        plain.mkdir()
        with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": self.temp.name}):
            state = workbench.git_changes(str(plain))
        self.assertFalse(state["repo"])
        self.assertTrue(state["reason"])


if __name__ == "__main__":
    unittest.main()

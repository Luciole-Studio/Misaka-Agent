"""Terminal launches keep project paths literal and never run client commands."""
import base64
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from misaka.ui.gui import terminals


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "中文 项目'; & $测试"
        self.root.mkdir()
        self.platform = patch.object(terminals.sys, "platform", "win32")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.which = patch.object(terminals.shutil, "which", side_effect=lambda name: "C:/tools/" + name)
        self.which.start()
        self.addCleanup(self.which.stop)

    def process(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("terminal", 0.4)
        return process

    def test_windows_terminal_handles_unicode_and_command_separators(self):
        with patch.object(terminals, "setting", return_value="wt"), \
                patch.object(terminals.subprocess, "Popen", return_value=self.process()) as start:
            result = terminals.open_terminal(str(self.root))
        argv = start.call_args.args[0]
        self.assertEqual(argv[:6], ["C:/tools/wt.exe", "-w", "new", "new-tab", "-d", "."])
        self.assertNotIn(str(self.root), argv)
        decoded = base64.b64decode(argv[-1]).decode("utf-16-le")
        self.assertEqual(decoded, "Set-Location -LiteralPath '" + str(self.root.resolve()).replace("'", "''") + "'")
        self.assertEqual(start.call_args.kwargs["cwd"], str(self.root.resolve()))
        self.assertNotIn("shell", start.call_args.kwargs)
        self.assertEqual(result["terminal"], "wt")

    def test_auto_falls_back_when_windows_app_alias_cannot_launch(self):
        with patch.object(terminals, "setting", return_value="auto"), \
                patch.object(terminals.subprocess, "Popen", side_effect=[OSError("alias inaccessible"), self.process()]) as start:
            result = terminals.open_terminal(str(self.root))
        self.assertEqual(result["terminal"], "pwsh")
        self.assertEqual(start.call_count, 2)

    def test_explicit_type_does_not_silently_launch_a_different_shell(self):
        with patch.object(terminals, "setting", return_value="wt"), \
                patch.object(terminals.subprocess, "Popen", side_effect=OSError("alias inaccessible")) as start:
            with self.assertRaisesRegex(ValueError, "终端启动失败"):
                terminals.open_terminal(str(self.root))
        self.assertEqual(start.call_count, 1)

    def test_cmd_inherits_project_without_interpolating_path(self):
        with patch.object(terminals, "setting", return_value="cmd"), \
                patch.object(terminals.subprocess, "Popen", return_value=self.process()) as start:
            terminals.open_terminal(str(self.root))
        self.assertEqual(start.call_args.args[0], ["C:/tools/cmd.exe", "/D", "/K"])
        self.assertEqual(start.call_args.kwargs["cwd"], str(self.root.resolve()))

    def test_missing_binary_invalid_type_and_non_directory_never_spawn(self):
        with patch.object(terminals.subprocess, "Popen") as start:
            for invalid in [None, "cmd & echo unwanted", ["cmd"], "bash"]:
                with patch.object(terminals, "setting", return_value=invalid), self.assertRaises(ValueError):
                    terminals.open_terminal(str(self.root))
            with patch.object(terminals.shutil, "which", return_value=None), \
                    patch.object(terminals, "setting", return_value="pwsh"), self.assertRaisesRegex(ValueError, "未安装"):
                terminals.open_terminal(str(self.root))
            with self.assertRaises(FileNotFoundError):
                terminals.open_terminal(str(self.root / "missing"))
            start.assert_not_called()

    def test_nonzero_launcher_exit_reports_failure(self):
        process = Mock()
        process.wait.return_value = 1
        with patch.object(terminals, "setting", return_value="wt"), \
                patch.object(terminals.subprocess, "Popen", return_value=process), self.assertRaisesRegex(ValueError, "退出码 1"):
            terminals.open_terminal(str(self.root))


if __name__ == "__main__":
    unittest.main()

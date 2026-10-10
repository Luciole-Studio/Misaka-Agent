"""Launch a local interactive terminal in a project, using a fixed executable list."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys

from misaka.config.product import setting


def terminal_choices() -> list[dict]:
    if sys.platform == "win32":
        specs = [("wt", "Windows Terminal", "wt.exe"),
                 ("pwsh", "PowerShell 7", "pwsh.exe"),
                 ("powershell", "Windows PowerShell", "powershell.exe"),
                 ("cmd", "命令提示符（CMD）", "cmd.exe")]
    elif sys.platform == "darwin":
        specs = [("terminal", "Terminal", "open")]
    else:
        specs = [("gnome", "GNOME Terminal", "gnome-terminal"),
                 ("konsole", "Konsole", "konsole"), ("xterm", "XTerm", "xterm")]
    choices = [{"id": key, "label": label, "available": bool(shutil.which(exe))}
               for key, label, exe in specs]
    return [{"id": "auto", "label": "自动选择", "available": any(c["available"] for c in choices)}, *choices]


def validate_terminal(value) -> str:
    if not isinstance(value, str) or value not in {c["id"] for c in terminal_choices()}:
        raise ValueError("未知的终端类型")
    return value


def terminal_settings() -> dict:
    return {"selected": setting("gui", "terminal", "auto", str), "choices": terminal_choices()}


def _powershell(executable: str, cwd: str) -> list[str]:
    # EncodedCommand protects Unicode, apostrophes and WT's semicolon parser.
    command = "Set-Location -LiteralPath '" + cwd.replace("'", "''") + "'"
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    return [executable, "-NoLogo", "-NoProfile", "-NoExit", "-EncodedCommand", encoded]


def _command(kind: str, cwd: str) -> list[str]:
    if sys.platform == "win32":
        if kind == "wt":
            shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
            if not shell:
                raise ValueError("Windows Terminal 需要 PowerShell，请先安装或选择 CMD")
            # Keep raw paths out of WT's command parser. The shell sets the exact
            # directory; -d . also inherits the Popen cwd for the initial tab.
            return [shutil.which("wt.exe"), "-w", "new", "new-tab", "-d", ".", *_powershell(shell, cwd)]
        executable = shutil.which(kind + ".exe")
        return [executable, "/D", "/K"] if kind == "cmd" else _powershell(executable, cwd)
    if sys.platform == "darwin":
        return [shutil.which("open"), "-a", "Terminal", cwd]
    if kind == "gnome":
        return [shutil.which("gnome-terminal"), "--working-directory=" + cwd]
    if kind == "konsole":
        return [shutil.which("konsole"), "--workdir", cwd]
    return [shutil.which("xterm")]


def open_terminal(workspace: str) -> dict:
    path = Path(workspace).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("项目路径必须是已有文件夹")
    selected = validate_terminal(setting("gui", "terminal", "auto", str))
    choices = terminal_choices()[1:]
    candidates = [c for c in choices if c["available"] and (selected == "auto" or c["id"] == selected)]
    if not candidates:
        raise ValueError("所选终端未安装或不在 PATH 中，请在设置 → 终端中切换")
    last_error = None
    for choice in candidates:
        try:
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW if choice["id"] == "wt" else subprocess.CREATE_NEW_CONSOLE
            process = subprocess.Popen(_command(choice["id"], str(path)), cwd=str(path),
                                       close_fds=True, start_new_session=True, creationflags=flags)
            try:
                code = process.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                code = None
            if code not in (None, 0):
                raise OSError(f"启动程序退出码 {code}")
            return {"terminal": choice["id"], "label": choice["label"], "workspace": str(path),
                    "message": f"已打开 {choice['label']}：{path}"}
        except (OSError, ValueError) as error:
            last_error = error
            if selected != "auto":
                break
    raise ValueError(f"终端启动失败：{last_error}。请在设置 → 终端中切换类型")

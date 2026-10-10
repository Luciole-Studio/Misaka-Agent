"""Double-click launcher using the installed MISAKA Python environment.

The web server runs without a console window on Windows. Repeated launches
reuse a healthy authenticated local server. Logs stay next to the checkout.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

SOURCE = Path(__file__).resolve().parents[1]
RUNTIME = SOURCE / ".gui-runtime"
REMEMBERED = RUNTIME / "workspace.txt"
URL_PATTERN = re.compile(r"http://127\.0\.0\.1:\d+/#token=[A-Za-z0-9_-]+")
# This launcher may run in plain Python, without the provider dependencies installed.
source_revision = runpy.run_path(str(SOURCE / "misaka" / "ui" / "gui" / "revision.py"))["source_revision"]


def url_in_log(path):
    if not path.exists():
        return None
    text = path.read_bytes().decode("utf-8", errors="strict")
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def healthy(url, workspace):
    origin, fragment = url.split("/#token=", 1)
    request = urllib.request.Request(origin + "/api/health",
        data=json.dumps({"workspace": str(workspace)}).encode(),
        headers={"Content-Type": "application/json", "X-Misaka-Token": fragment})
    try:
        # Loopback must never follow a user's HTTP proxy to a third party.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            health = json.load(response)
        # native_chat: a server from before the native chat refactor answers the
        # version check but cannot serve the current page; restart it instead.
        # project_gui must match server.py's /api/health. Bump both when the
        # server protocol changes, or the launcher will keep an old process.
        # Protocol compatibility alone cannot detect an old process serving its
        # startup snapshot after a local edit. Match the actual GUI sources too.
        return (health.get("version") is not None and health.get("native_chat") is True
                and health.get("project_gui") == 12
                and health.get("source_revision") == source_revision(SOURCE))
    except (OSError, ValueError, urllib.error.URLError):
        return False


def interpreter(env):
    suffix = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    candidates = [SOURCE / ".venv" / suffix]
    if uv := shutil.which("uv"):
        result = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True,
                                encoding="utf-8", timeout=15, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            candidates.append(Path(result.stdout.strip()) / "misaka" / suffix)
    candidates.append(Path(sys.executable))
    for candidate in candidates:
        if candidate.is_file():
            result = subprocess.run([str(candidate), "-c", "import misaka, pyte, filelock, pydantic"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=20, env=env,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0:
                return str(candidate)
    raise RuntimeError("找不到 MISAKA 的 Python 环境。请先在本仓库运行 uv sync，或按 README 安装后再双击 gui.cmd。")


def remembered_workspace():
    try:
        text = REMEMBERED.read_text(encoding="utf-8").strip().strip('"')
    except OSError:
        return None
    path = Path(text).expanduser() if text else None
    return path if path is not None and path.is_dir() else None


def remember_workspace(path):
    RUNTIME.mkdir(exist_ok=True)
    REMEMBERED.write_text(str(path), encoding="utf-8")


def pick_folder():
    """Windows folder dialog. Other systems pass --workspace."""
    if os.name != "nt":
        return None
    command = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$d.Description = 'Select the research project folder'; "
        "$d.ShowNewFolderButton = $true; "
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", command],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    chosen = (result.stdout or "").strip().strip('"')
    path = Path(chosen) if chosen else None
    return path if path is not None and path.is_dir() else None


def resolve_workspace(explicit):
    if explicit:
        path = Path(explicit).expanduser().resolve()
    else:
        path = remembered_workspace()
        if path is None:
            path = pick_folder()
        if path is None:
            raise RuntimeError("请指定项目文件夹：gui.cmd --workspace 文件夹路径")
        path = path.resolve()
    if not path.is_dir():
        raise ValueError("项目路径必须是文件夹")
    remember_workspace(path)
    return path


def runtime_logs():
    # A source update can leave the old process alive, with its log handles open.
    # Give each source revision its own logs instead of truncating those files.
    revision = source_revision(SOURCE)[:12]
    return RUNTIME / f"gui-{revision}.log", RUNTIME / f"gui-error-{revision}.log"


def main():
    parser = argparse.ArgumentParser(description="启动 MISAKA 中文界面")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    workspace = resolve_workspace(args.workspace)
    RUNTIME.mkdir(exist_ok=True)
    log, errors = runtime_logs()
    # These logs are UTF-8. Refuse unknown bytes before any truncating write.
    for path in (log, errors):
        if path.exists():
            path.read_bytes().decode("utf-8", errors="strict")
    url = url_in_log(log) or url_in_log(RUNTIME / "gui.log")
    if not url or not healthy(url, workspace):
        # Keep the browser origin stable across restarts so recent projects survive.
        port = int(url.split(":")[2].split("/")[0]) if url else 9155
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                port = 0  # An older server may still own active conversations.
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(SOURCE)}
        python = interpreter(env)
        with log.open("wb") as output, errors.open("wb") as error:
            process = subprocess.Popen([python, "-u", "-X", "utf8", "-m", "misaka.ui.gui.server", "--no-browser",
                                        "--port", str(port), "--workspace", str(workspace)], cwd=str(SOURCE), env=env,
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                                       close_fds=True, start_new_session=True,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(80):
            if process.poll() is not None:
                raise RuntimeError(errors.read_text(encoding="utf-8") or "GUI 服务未能启动")
            url = url_in_log(log)
            if url and healthy(url, workspace):
                break
            time.sleep(0.25)
        else:
            raise RuntimeError(f"启动超时，请检查 {errors}")
    if not args.no_browser:
        webbrowser.open(url)
    print("MISAKA 中文界面已打开，可以关闭此启动窗口。")
    print(url)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        raise SystemExit(1)

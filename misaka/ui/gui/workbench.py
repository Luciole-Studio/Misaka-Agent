"""Side-panel helpers: version-control changes and opening project files locally.

Every path is resolved inside the project first. Git runs with a fixed argument
list and a timeout; its output is size-capped before it reaches the browser.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

MAX_FILES = 500
MAX_PATCH = 400_000
MAX_UNTRACKED = 200_000
# A click in the side panel must never run a program the agent just wrote.
RUNNABLE = {".exe", ".bat", ".cmd", ".com", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
            ".msi", ".msp", ".scr", ".lnk", ".url", ".hta", ".cpl", ".jar", ".reg", ".sh", ".command", ".app", ".py", ".pyw"}
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
STATUS = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied", "U": "conflict", "T": "modified"}


def project_target(workspace, relative) -> tuple[Path, Path]:
    root = Path(workspace).expanduser().resolve(strict=True)
    target = (root / str(relative or ".")).resolve()
    if not target.is_relative_to(root):
        raise ValueError("只能访问当前项目内的文件")
    if not target.exists():
        raise ValueError("文件不存在或已被移动")
    return root, target


def open_path(workspace, relative, reveal=False) -> dict:
    root, target = project_target(workspace, relative)
    if not reveal and target.is_file() and target.suffix.lower() in RUNNABLE:
        raise ValueError("为安全起见，可执行文件和脚本不会从这里直接打开；可以在文件夹中显示后自行处理")
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        if reveal:
            # Windows paths cannot contain quotes, so the resolved path cannot escape them.
            subprocess.Popen(f'explorer.exe /select,"{target}"', **quiet)
        else:
            os.startfile(str(target))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", *(["-R"] if reveal else []), str(target)], start_new_session=True, **quiet)
    else:
        opener = shutil.which("xdg-open")
        if not opener:
            raise ValueError("没有找到 xdg-open，无法用系统应用打开")
        shown = target.parent if reveal and target.is_file() else target
        subprocess.Popen([opener, str(shown)], start_new_session=True, **quiet)
    name = target.name or root.name
    return {"message": ("已在文件夹中显示 " if reveal else "已用系统应用打开 ") + name}


def _git(root: Path, *args, timeout=15) -> subprocess.CompletedProcess:
    git = shutil.which("git")
    if not git:
        raise FileNotFoundError("git")
    return subprocess.run([git, "-C", str(root), "-c", "core.quotepath=off", *args],
                          capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _repository(workspace):
    root = Path(workspace).expanduser().resolve(strict=True)
    try:
        top = _git(root, "rev-parse", "--show-toplevel")
    except FileNotFoundError:
        return root, None, "没有找到 git，无法读取版本变更"
    except subprocess.TimeoutExpired:
        return root, None, "git 响应超时"
    if top.returncode != 0:
        return root, None, "当前项目不是 Git 仓库"
    top_path = Path(_text(top.stdout).strip()).resolve()
    prefix = root.relative_to(top_path).as_posix() if root != top_path and root.is_relative_to(top_path) else ""
    return root, (prefix + "/" if prefix else ""), None


def _base(root: Path) -> str:
    return "HEAD" if _git(root, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0 else EMPTY_TREE


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(8000)
    except OSError:
        return False


def _line_count(path: Path):
    try:
        if path.stat().st_size > MAX_UNTRACKED:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8000]:
        return None
    return raw.count(b"\n") + (0 if raw.endswith(b"\n") or not raw else 1)


def git_changes(workspace) -> dict:
    root, prefix, reason = _repository(workspace)
    if prefix is None:
        return {"repo": False, "reason": reason, "files": []}
    try:
        branch = _text(_git(root, "branch", "--show-current").stdout).strip()
        status = _git(root, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all", "--", ".", timeout=30)
        if status.returncode != 0:
            raise ValueError(_text(status.stderr).strip() or "git status 失败")
        numstat = _git(root, "diff", _base(root), "--numstat", "-z", "--no-renames", "--", ".", timeout=30)
    except subprocess.TimeoutExpired:
        raise ValueError("git 响应超时，项目可能过大") from None
    counts = {}
    for record in _text(numstat.stdout).split("\0"):
        parts = record.split("\t", 2)
        if len(parts) == 3:
            adds, dels, path = parts
            counts[path] = (int(adds) if adds.isdigit() else None, int(dels) if dels.isdigit() else None)
    files = []
    for record in _text(status.stdout).split("\0"):
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        if not path.startswith(prefix) or path.endswith("/"):
            continue
        relative = path[len(prefix):]
        untracked = code == "??"
        letter = "U" if "U" in code else (code[0] if code[0] not in " ?" else code[1])
        adds, dels = counts.get(path, (None, None))
        binary = adds is None and path in counts
        if untracked:
            adds, dels, binary = _line_count(root / relative), 0, _is_binary(root / relative)
        files.append({"path": relative, "code": code.strip() or code, "status": "untracked" if untracked else STATUS.get(letter, "modified"),
                      "staged": code[0] not in " ?", "adds": adds, "dels": dels, "binary": binary})
        if len(files) >= MAX_FILES:
            break
    files.sort(key=lambda f: f["path"].casefold())
    return {"repo": True, "branch": branch, "files": files, "truncated": len(files) >= MAX_FILES}


def git_diff(workspace, relative) -> dict:
    root, prefix, reason = _repository(workspace)
    if prefix is None:
        raise ValueError(reason)
    target = (root / str(relative)).resolve()
    if not target.is_relative_to(root):
        raise ValueError("只能查看当前项目内的文件")
    path = target.relative_to(root).as_posix()
    tracked = _git(root, "ls-files", "--error-unmatch", "--", path).returncode == 0
    if not tracked:
        if not target.is_file():
            raise ValueError("文件不存在或已被移动")
        if _line_count(target) is None:
            return {"path": path, "patch": "", "binary": True, "untracked": True}
        lines = target.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        patch = f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n" + "".join("+" + line + "\n" for line in lines)
        return {"path": path, "patch": patch, "untracked": True}
    try:
        result = _git(root, "diff", _base(root), "--no-renames", "--no-color", "--no-ext-diff", "--", path, timeout=30)
    except subprocess.TimeoutExpired:
        raise ValueError("git 响应超时") from None
    raw = result.stdout
    truncated = len(raw) > MAX_PATCH
    patch = _text(raw[:MAX_PATCH])
    return {"path": path, "patch": patch, "truncated": truncated, "binary": "Binary files" in patch[:2000] and "@@" not in patch}

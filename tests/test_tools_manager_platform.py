"""A missing program is named with the line that installs it on this OS, and a program its Windows
installer left off PATH is found where the installer said it put it.

Windows 11, 2026-11-10: the grep tool told a Sister to `brew install ripgrep`; and after the
`winget install UB-Mannheim.TesseractOCR` the wizard names, tesseract sat in
``C:\\Program Files\\Tesseract-OCR`` with no PATH entry, and MISAKA went on saying it had none."""
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.cli import setup_wizard
from misaka.utils import tools_manager

KEY = r"SOFTWARE\Tesseract-OCR"


def _on(monkeypatch, platform):
    """Only this module's view of the OS: ``shutil.which`` itself must go on working here."""
    monkeypatch.setattr(tools_manager, "sys", SimpleNamespace(platform=platform))


@pytest.mark.parametrize(("platform", "rg", "fd"), [
    ("darwin", "brew install ripgrep", "brew install fd"),
    ("linux", "sudo apt install ripgrep", "sudo apt install fd-find"),
    ("win32", "winget install BurntSushi.ripgrep.MSVC", "winget install sharkdp.fd"),
])
def test_a_missing_search_tool_names_this_systems_install_line(monkeypatch, platform, rg, fd):
    _on(monkeypatch, platform)
    for tool, line in (("rg", rg), ("fd", fd)):
        message = tools_manager.missingToolMessage(tool)
        assert f"(`{line}`)" in message and tools_manager.TOOLS[tool].installHint in message
        others = {"brew install", "apt install", "winget install"} - {" ".join(line.removeprefix("sudo ").split()[:2])}
        assert not any(other in message for other in others)


async def test_the_startup_notice_names_it_too(monkeypatch):
    _on(monkeypatch, "win32")
    monkeypatch.setattr(tools_manager, "getToolPath", lambda tool: None)
    said = []
    assert await tools_manager.ensureTool("rg", said.append) is None
    assert said[0].type == "warning" and "winget install BurntSushi.ripgrep.MSVC" in said[0].message


def test_setup_and_the_tools_read_one_table():
    assert setup_wizard._install_command is tools_manager.install_command
    assert tools_manager.install_command("mineru-kit") == "uv tool install mineru"
    assert tools_manager.install_command("nonesuch") == "install nonesuch"


def _registry(monkeypatch, entries):
    """A ``winreg`` holding ``{(hive, view): folder}`` under the Tesseract key and nothing else."""
    asked = []
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE, fake.HKEY_CURRENT_USER = "HKLM", "HKCU"
    fake.KEY_READ, fake.KEY_WOW64_64KEY, fake.KEY_WOW64_32KEY = 1, 256, 512

    class _Key:
        def __init__(self, folder):
            self.folder = folder

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(hive, key, reserved, access):
        asked.append((hive, key, access))
        view = "64" if access & fake.KEY_WOW64_64KEY else "32"
        if key != KEY or (hive, view) not in entries:
            raise FileNotFoundError(key)
        return _Key(entries[(hive, view)])

    def query(opened, name):
        if name != "InstallDir":
            raise FileNotFoundError(name)
        return opened.folder, 1

    fake.OpenKey, fake.QueryValueEx = open_key, query
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return asked


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """tesseract in a folder of its own, PATH leading nowhere near it."""
    folder = tmp_path / "Tesseract-OCR"
    folder.mkdir()
    program = folder / ("tesseract.exe" if os.name == "nt" else "tesseract")   # Windows runs no bare name
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    program.chmod(0o755)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return folder, program


def _found(tool):
    """As a path, which on Windows is the same for the `tesseract.EXE` it answers."""
    found = tools_manager.find_tool(tool)
    return found and Path(found)


def test_tesseract_off_the_path_is_found_where_its_installer_put_it(installed, monkeypatch):
    folder, program = installed
    _on(monkeypatch, "win32")
    _registry(monkeypatch, {("HKLM", "64"): str(folder)})
    assert _found("tesseract") == program


@pytest.mark.parametrize("where", [("HKLM", "32"), ("HKCU", "64"), ("HKCU", "32")])
def test_an_install_for_one_user_or_of_the_32_bit_build_is_found_too(installed, monkeypatch, where):
    folder, program = installed
    _on(monkeypatch, "win32")
    _registry(monkeypatch, {where: str(folder)})
    assert _found("tesseract") == program


def test_a_key_left_by_an_uninstall_finds_nothing(installed, monkeypatch, tmp_path):
    _folder, program = installed
    program.unlink()
    _on(monkeypatch, "win32")
    _registry(monkeypatch, {("HKLM", "64"): str(tmp_path / "Tesseract-OCR")})
    assert tools_manager.find_tool("tesseract") is None


def test_the_path_is_asked_first_and_the_registry_only_on_windows(installed, monkeypatch):
    folder, program = installed
    asked = _registry(monkeypatch, {("HKLM", "64"): str(folder)})
    _on(monkeypatch, "linux")
    assert tools_manager.find_tool("tesseract") is None and asked == []
    _on(monkeypatch, "win32")
    monkeypatch.setenv("PATH", str(folder))
    assert _found("tesseract") == program and asked == []
    monkeypatch.setenv("PATH", str(folder.parent / "empty"))
    assert tools_manager.find_tool("pdftotext") is None and asked == []     # no installer key is known for it


@pytest.mark.parametrize(("platform", "line", "absent"), [
    ("win32", '$env:ANTHROPIC_API_KEY = "..."', "export "),
    ("darwin", "export ANTHROPIC_API_KEY=...", "$env:"),
    ("linux", "export ANTHROPIC_API_KEY=...", "$env:"),
])
def test_setup_without_a_terminal_names_the_line_this_systems_shell_takes(monkeypatch, capsys, platform, line, absent):
    monkeypatch.setattr(sys, "platform", platform)
    assert setup_wizard.run() == 1
    said = capsys.readouterr().err
    assert "needs a terminal" in said and line in said and absent not in said

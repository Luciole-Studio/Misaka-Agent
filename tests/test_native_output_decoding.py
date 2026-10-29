"""Output of native programs is decoded leniently, as pi's Node children are (issue #13, B2).

On Chinese Windows `where` prints its "not found" notice in the console code page: GBK
"信息: 用提供的模式无法找到文件。" starts with 0xD0 0xC5, which a strict UTF-8 decode rejects with
"can't decode byte 0xd0 in position 0" -- the reporter's traceback. On Windows the decode runs
in subprocess's reader thread, which died and left the output None; here, on POSIX, the same
strict decode raises in the caller, so the old code fails these tests loudly.
"""
import os
import subprocess
import sys
from types import SimpleNamespace

from misaka.core import resolve_config_value
from misaka.utils import shell

NOT_FOUND_GBK = "信息: 用提供的模式无法找到文件。".encode("gbk")


def _fake_where(monkeypatch, *, stdout=b"", stderr=b"", code=1):
    """Run ``where`` as a Python child that writes these bytes, through the caller's own kwargs."""
    real = subprocess.run
    script = (f"import sys; sys.stdout.buffer.write({stdout!r}); sys.stderr.buffer.write({stderr!r}); "
              f"sys.exit({code})")

    def run(argv, **kwargs):
        assert argv[0] == "where"
        return real([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(shell.subprocess, "run", run)
    monkeypatch.setattr(shell, "os", SimpleNamespace(name="nt", path=os.path))


def test_where_not_finding_a_program_in_the_console_code_page_is_a_miss(monkeypatch):
    _fake_where(monkeypatch, stderr=NOT_FOUND_GBK, code=1)
    assert shell.findExecutableOnPath("pwsh.exe") is None


def test_where_finding_a_program_returns_its_path(monkeypatch, tmp_path):
    program = tmp_path / "bash.exe"
    program.write_bytes(b"")
    _fake_where(monkeypatch, stdout=f"{program}\r\nC:\\other\\bash.exe\r\n".encode(), code=0)
    assert shell.findExecutableOnPath("bash.exe") == str(program)


def test_a_config_command_printing_non_utf8_still_yields_its_value():
    """pi's ``execSync(command, {encoding: "utf-8"})`` never throws on bad bytes."""
    value = resolve_config_value.executeWithDefaultShell(
        f"\"{sys.executable}\" -c \"import sys; sys.stdout.buffer.write(b'\\xd0\\xc5key')\"")
    assert value == "��key"

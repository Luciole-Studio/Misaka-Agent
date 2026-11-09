"""Chinese typed into a setup prompt, and into a new Sister's ID.

The line prompt used a bare ``input()``, so the tty driver edited the line and Backspace
erased one byte of a three-byte character: two presses over 「红队」 left half a character
behind and ``input()`` died with UnicodeDecodeError, ending the wizard. And ``\\w`` matches
Chinese, so a specialty pasted into the ID prompt could become a Sister's folder name.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time

import pytest

from misaka.core.network import roster


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX pty; the Windows console edits the line itself")
def test_backspace_erases_a_whole_chinese_character():
    import pty

    # A child on a real terminal answers the prompt; pytest's capture would swallow a fork's.
    fd, tty = pty.openpty()
    child = subprocess.Popen(
        [sys.executable, "-c", ("from misaka.cli.setup_ui import prompt; "
                                "print('RESULT=' + repr(prompt('Sister ID', '10033')))")],
        stdin=tty, stdout=tty, stderr=tty, close_fds=True)
    os.close(tty)
    out = b""

    def read_until(marker: bytes, timeout: float = 20) -> None:
        nonlocal out
        end = time.monotonic() + timeout
        while marker not in out and time.monotonic() < end:
            if not select.select([fd], [], [], 0.1)[0]:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:  # the child closed the terminal
                break
            if not chunk:
                break
            out += chunk

    try:
        read_until(b"Sister ID")
        for keys in ("红队".encode(), b"\x7f\x7f", b"10034\r"):
            os.write(fd, keys)
            time.sleep(0.2)
        read_until(b"RESULT=")
        read_until(b"\n")
    finally:
        child.kill()
        child.wait()
        os.close(fd)
    assert "RESULT='10034'" in out.decode("utf-8", "replace"), out


def test_a_new_sister_id_must_be_ascii(tmp_path):
    ok, message = roster.create_sister("红队", root=str(tmp_path))
    assert not ok and "Invalid Sister ID" in message
    assert not os.listdir(tmp_path)


def test_an_existing_non_ascii_sister_still_resolves():
    # Only creation is stricter: a Sister made before keeps working.
    assert roster._valid("红队") and not roster._valid_new("红队")
    assert roster._valid_new("10033")

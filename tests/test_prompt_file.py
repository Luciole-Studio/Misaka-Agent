"""A prompt handed to a child as a file (pi's subagent ``writePromptToTempFile``), issue #13."""
import os
import stat
import sys

import pytest

from misaka.utils.prompt_file import readPromptFile, removePromptFile, writePromptToTempFile


def test_the_file_holds_the_prompt_exactly_and_is_private():
    prompt = "第一行\r\n第二行\nthird\rfourth " + "x" * 40000
    directory, path = writePromptToTempFile("t_1a2b3c", prompt)
    try:
        assert os.path.dirname(path) == directory and os.path.basename(path) == "prompt-t_1a2b3c.md"
        assert readPromptFile(path) == prompt
        with open(path, "rb") as handle:
            assert handle.read() == prompt.encode("utf-8"), "no newline translation, no BOM"
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        removePromptFile((directory, path))
    assert not os.path.exists(path) and not os.path.exists(directory)


def test_a_name_is_made_safe_for_a_file_name():
    directory, path = writePromptToTempFile("Sister 10032/../x", "hi")
    try:
        assert os.path.basename(path) == "prompt-Sister_10032_.._x.md"
        assert os.path.dirname(path) == directory
    finally:
        removePromptFile((directory, path))


def test_unpaired_surrogates_become_replacement_characters_as_node_writes_them():
    directory, path = writePromptToTempFile("t_1", "a\ud800b")
    try:
        assert readPromptFile(path) == "a�b"
    finally:
        removePromptFile((directory, path))


def test_a_leading_bom_is_dropped_on_read(tmp_path):
    path = tmp_path / "prompt.md"
    path.write_bytes(b"\xef\xbb\xbfhello")
    assert readPromptFile(str(path)) == "hello"


def test_removing_what_is_already_gone_is_quiet(tmp_path):
    removePromptFile(None)
    removePromptFile((str(tmp_path / "gone"), str(tmp_path / "gone" / "prompt.md")))


def test_a_failed_write_leaves_nothing_behind(monkeypatch):
    made = []
    real_mkdtemp = __import__("tempfile").mkdtemp

    def mkdtemp(**kwargs):
        made.append(real_mkdtemp(**kwargs))
        return made[-1]

    monkeypatch.setattr("misaka.utils.prompt_file.tempfile.mkdtemp", mkdtemp)
    monkeypatch.setattr("misaka.utils.prompt_file.os.open", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space")))
    with pytest.raises(OSError):
        writePromptToTempFile("t_1", "hi")
    assert made and not os.path.exists(made[0])

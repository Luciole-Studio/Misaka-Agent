"""Hermes 1a24b851e5 and 85564321be (``tests/agent/test_skill_inline_bash_windows.py``,
``tests/agent/test_skill_inline_shell_output.py``): a skill's inline shell runs the terminal's bash -- on Windows
Git Bash, not the WSL launcher a bare "bash" finds in System32 first -- and a silent failure is not an empty result."""
import sys

import pytest

from misaka.core.skills import host
from misaka.core.skills.vendor.preprocessing import (
    preprocess_skill_content,
    run_inline_shell,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_silent_nonzero_exit_returns_marker(tmp_path):
    """rc!=0 with no stdout and no stderr is the interpreter-never-ran signature (#116818)."""
    assert run_inline_shell("exit 3", tmp_path, 5) == "[inline-shell exit 3 with no output: exit 3]"


def test_nonzero_exit_with_stderr_keeps_the_diagnostic(tmp_path):
    assert run_inline_shell('printf "diag" >&2; exit 3', tmp_path, 5) == "diag"


@windows_only
@pytest.mark.parametrize(("command", "expected"), [
    ('printf "hello"', "hello"),
    (":", ""),
    ('printf "diagnostic" >&2; exit 3', "diagnostic"),
    ('printf "preferred"; printf "ignored" >&2', "preferred"),
])
def test_inline_shell_keeps_output_contract(tmp_path, command, expected):
    assert run_inline_shell(command, tmp_path, 5) == expected


@windows_only
def test_inline_shell_resolver_failure_is_a_marker(tmp_path, monkeypatch):
    def unavailable():
        raise RuntimeError("Git Bash unavailable")

    monkeypatch.setattr(host, "_find_bash", unavailable)
    assert run_inline_shell(":", tmp_path, 5) == "[inline-shell error: Git Bash unavailable]"


@windows_only
def test_disabled_inline_shell_does_not_resolve_bash(tmp_path, monkeypatch):
    def unexpected():
        pytest.fail("disabled inline shell must not resolve an interpreter")

    monkeypatch.setattr(host, "_find_bash", unexpected)
    assert preprocess_skill_content("!`printf hello`", tmp_path, skills_cfg={}) == "!`printf hello`"


@windows_only
def test_inline_shell_executes_in_native_skill_directory(tmp_path):
    (tmp_path / "marker.txt").write_text("native-skill", encoding="utf-8")
    result = preprocess_skill_content(
        'value=!`read -r value < marker.txt; printf "%s" "$value"`',
        tmp_path,
        skills_cfg={"inline_shell": True, "inline_shell_timeout": 5},
    )
    assert result == "value=native-skill"

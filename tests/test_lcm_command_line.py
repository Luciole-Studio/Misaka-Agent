"""`misaka lcm` at a terminal: upstream's dispatcher answers, in the words a terminal takes."""
import re
from pathlib import Path

import pytest

from misaka.extensions.misaka_lcm import vendor
from misaka.extensions.misaka_lcm.host import context_engine as ce
from misaka.extensions.misaka_lcm.host import operations, operators, repair, storage


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    ce.close_all()
    yield
    ce.close_all()


@pytest.mark.parametrize("argv", [["--help"], ["-h"], ["help"], []])
def test_the_help_flag_is_the_help(argv, capsys):
    # It was "Unknown subcommand: --help" over lines such as "- /lcm doctor: ...".
    assert operators.main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("LCM command help\n") and "Unknown subcommand" not in out
    assert "/lcm" not in out
    # Bare `/lcm` is the status; bare `misaka lcm` is this help.
    assert "- misaka lcm status: " in out and "misaka lcm or" not in out
    assert "- misaka lcm doctor clean apply: " in out
    assert "- misaka lcm clear: " in out and "- misaka lcm repair [--apply]: " in out


def test_a_help_flag_after_a_subcommand_runs_nothing(monkeypatch, capsys):
    # `misaka lcm clear --help` removed the cache.
    ran = []
    monkeypatch.setattr(operations, "command", lambda raw, ctx=None: ran.append(raw) or "LCM command help")
    monkeypatch.setattr(storage, "clear", lambda workspace: ran.append("clear") or True)
    monkeypatch.setattr(repair, "run", lambda apply=False: ran.append("repair") or 0)
    for argv in (["clear", "--help"], ["repair", "--apply", "-h"], ["embed", "backfill", "--apply", "--help"],
                 ["doctor", "clean", "apply", "-h"]):
        assert operators.main(argv) == 0
    assert ran == ["help"] * 4
    for argv in (["clear"], ["repair"], ["doctor", "clean"]):
        operators.main(argv)
    assert ran[4:] == ["clear", "repair", "doctor clean"]


def test_an_operator_script_answers_its_own_help(capsys):
    with pytest.raises(SystemExit) as stopped:
        operators.main(["import", "--help"])
    assert stopped.value.code == 0 and "--source-jsonl" in capsys.readouterr().out


def test_an_unknown_subcommand_is_still_named(capsys):
    assert operators.main(["bogus"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Unknown subcommand: bogus\n") and "- misaka lcm status: " in out and "/lcm" not in out


def test_a_path_is_not_the_command():
    said = ("run `/lcm doctor repair` first, then /lcm backup\n"
            "project: /work/a (old)/lcm\n"
            "project: /lcm\n"
            "database_path: /work/a (old)/lcm/.misaka/lcm/lcm.db\n"
            "/lcm embed backfill --apply")
    assert operators._for_terminal(said) == (
        "run `misaka lcm doctor repair` first, then misaka lcm backup\n"
        "project: /work/a (old)/lcm\n"
        "project: /lcm\n"
        "database_path: /work/a (old)/lcm/.misaka/lcm/lcm.db\n"
        "misaka lcm embed backfill --apply")


def test_every_slash_command_upstream_writes_is_reached():
    # The two it leaves are log lines about registering the slash command, which no reply carries.
    left = []
    for path in sorted(Path(vendor.__file__).parent.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        left += [path.name for found in re.finditer(r"/lcm\b(?![-.])", source)   # not `/lcm.db` or `</lcm-...>`
                 if not operators._SLASH.match(source, found.start())]
    assert left == ["__init__.py", "__init__.py"]

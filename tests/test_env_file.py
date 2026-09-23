"""The home's ``.env``: environment for code that is not MISAKA (config.env)."""

from __future__ import annotations

import os
import stat

import pytest

from misaka.config import env as env_file
from misaka.config import home


@pytest.fixture(autouse=True)
def fresh_bookkeeping():
    env_file._loaded_from_home.clear()
    yield
    env_file._loaded_from_home.clear()


def test_parse_takes_dotenv_syntax_and_the_last_line_wins():
    text = """
    # a comment
    export EXA_API_KEY=exa-plain   # trailing comment
    DQ="a \\"quoted\\" value\\twith\\nescapes # not a comment"
    SQ='single $literal \\n'
    EMPTY=
    AGAIN=first
    AGAIN=second
    """
    assert env_file.parse(text) == {
        "EXA_API_KEY": "exa-plain",
        "DQ": 'a "quoted" value\twith\nescapes # not a comment',
        "SQ": "single $literal \\n",
        "EMPTY": "",
        "AGAIN": "second",
    }


@pytest.mark.parametrize("line", ["NOT A LINE", "1BAD=x", 'Q="open', "Q='open", 'Q="a" trailing', "=value"])
def test_a_line_that_does_not_parse_refuses_the_whole_file(line):
    with pytest.raises(env_file.EnvFileError):
        env_file.parse("GOOD=1\n" + line + "\n")


def test_load_sets_what_the_shell_did_not_and_skips_misaka_names(monkeypatch, caplog):
    monkeypatch.setenv("FROM_SHELL", "shell")
    monkeypatch.delenv("FROM_FILE", raising=False)
    monkeypatch.delenv("MISAKA_WHO", raising=False)
    home.home().mkdir(parents=True, exist_ok=True)
    home.path("env").write_text("FROM_SHELL=file\nFROM_FILE=file\nMISAKA_WHO=10032\nMISAKA_HOME=/elsewhere\n")
    with caplog.at_level("WARNING"):
        assert env_file.load() == ["FROM_FILE"]
    assert os.environ["FROM_SHELL"] == "shell" and os.environ["FROM_FILE"] == "file"
    assert "MISAKA_WHO" not in os.environ
    assert "MISAKA_HOME, MISAKA_WHO" in caplog.text
    monkeypatch.delenv("FROM_FILE")


def test_a_role_overlays_the_homes_file_but_never_the_shell(monkeypatch, tmp_path):
    monkeypatch.setenv("FROM_SHELL", "shell")
    monkeypatch.delenv("SHARED_KEY", raising=False)
    monkeypatch.delenv("ROLE_ONLY", raising=False)
    home.home().mkdir(parents=True, exist_ok=True)
    home.path("env").write_text("SHARED_KEY=home\nFROM_SHELL=home\n")
    env_file.load()
    role = tmp_path / "role"
    role.mkdir()
    env_file.write({"SHARED_KEY": "role", "ROLE_ONLY": "yes", "FROM_SHELL": "role"}, role)
    overlay = env_file.role_overlay(role)
    assert overlay == {"SHARED_KEY": "role", "ROLE_ONLY": "yes"}
    assert env_file.values(role)["FROM_SHELL"] == "shell"
    assert env_file.values()["SHARED_KEY"] == "home"          # the process itself is unchanged
    monkeypatch.delenv("SHARED_KEY")


def test_write_keeps_other_lines_creates_owner_only_and_refuses_misaka_names():
    home.home().mkdir(parents=True, exist_ok=True)
    target = home.path("env")
    target.write_text("# keys for the skills\nOLD=1\nGONE=2\n")
    env_file.write({"OLD": "with space", "NEW": "n"}, remove=("GONE",))
    assert target.read_text() == '# keys for the skills\nOLD="with space"\nNEW=n\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert env_file.read() == {"OLD": "with space", "NEW": "n"}
    with pytest.raises(ValueError):
        env_file.write({"MISAKA_MODEL": "x"})
    with pytest.raises(ValueError):
        env_file.write({"bad name": "x"})


def test_a_symlinked_or_malformed_file_is_refused_not_half_read(tmp_path):
    home.home().mkdir(parents=True, exist_ok=True)
    real = tmp_path / "elsewhere.env"
    real.write_text("A=1\n")
    home.path("env").symlink_to(real)
    with pytest.raises(env_file.EnvFileError):
        env_file.read()
    assert env_file.load() == []                               # reported, nothing applied
    home.path("env").unlink()
    home.path("env").write_text("A=1\nnot a line\n")
    assert env_file.load() == [] and "A" not in os.environ


def test_ensure_narrows_a_readable_env_file():
    home.home().mkdir(parents=True, exist_ok=True)
    home.path("env").write_text("A=1\n")
    home.path("env").chmod(0o644)
    home.ensure()
    assert stat.S_IMODE(home.path("env").stat().st_mode) == 0o600
    assert "env" not in home.strays() and ".env" not in home.strays()


def test_a_role_session_gets_its_env_overlay_and_a_skill_reads_the_roles_secrets(tmp_path, monkeypatch):
    from misaka.core.skills.runtime import SkillRuntime
    from misaka.core.wiring import role_session_setup

    monkeypatch.delenv("VENDOR_KEY", raising=False)
    role = home.path("profiles_root") / "10099"
    role.mkdir(parents=True)
    runtime = SkillRuntime(role, env={"PATH": "/bin"})
    runtime.store_secret("VENDOR_KEY", "from-role")
    assert env_file.read(role) == {"VENDOR_KEY": "from-role"}
    assert runtime.load_env()["VENDOR_KEY"] == "from-role"
    _flags, _assembly, env = role_session_setup(str(role), str(tmp_path))
    assert env["VENDOR_KEY"] == "from-role" and env["MISAKA_PROFILE_DIR"] == str(role)

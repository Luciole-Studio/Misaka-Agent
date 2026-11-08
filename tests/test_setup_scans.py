"""The setup wizard's Documents section installs MinerU when asked and sets what reads a scan.

MinerU lives in a uv tool environment of its own, so installing it does not touch the one setup
runs in, and the wizard can run the install. The two settings, ``documents.ocr_engine`` and
``documents.mineru_tier``, go to settings.json; Enter keeps each, so running setup again is a
review, not a reset."""
import json
import subprocess

import pytest

from misaka.cli import setup_wizard as setup
from misaka.config import home, product
from misaka.core.documents import mineru


@pytest.fixture
def machine(monkeypatch):
    """Which programs are installed, and every command the wizard runs."""
    present = {"uv": "/opt/bin/uv"}
    ran = []

    def which(name, *args, **kwargs):
        return present.get(name)

    def run(command, *args, **kwargs):
        ran.append(list(command))
        if command[1:3] == ["tool", "install"] and present.get("on_path_after_install", True):
            present[mineru.BINARY] = "/opt/bin/mineru-kit"
        if command[1:] == ["tool", "dir", "--bin"]:
            return subprocess.CompletedProcess(command, 0, "/home/me/.local/bin\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(setup.shutil, "which", which)
    monkeypatch.setattr(setup, "find_tool", which)
    monkeypatch.setattr(setup.subprocess, "run", run)
    return present, ran


def answers(monkeypatch, *, choices=(), yes_no=()):
    """Answer the menus in order; a menu with no answer left is Enter, the current value."""
    asked = {"choice": [], "yes_no": []}
    choices, yes_no = list(choices), list(yes_no)

    def choice(question, options, default=0, description=None):
        asked["choice"].append((question, options, default))
        return choices.pop(0) if choices else default

    def yes(question, default=True):
        asked["yes_no"].append(question)
        return yes_no.pop(0) if yes_no else default

    monkeypatch.setattr(setup, "prompt_choice", choice)
    monkeypatch.setattr(setup, "prompt_yes_no", yes)
    return asked


def documents_section():
    path = home.path("settings")
    return json.loads(path.read_text(encoding="utf-8")).get("documents", {}) if path.exists() else {}


def run_scans():
    wizard = setup.Wizard()
    wizard._scans()
    return wizard


def test_the_choices_go_to_settings_json_and_are_read_back(machine, monkeypatch):
    present, _ran = machine
    present[mineru.BINARY] = "/opt/bin/mineru-kit"
    answers(monkeypatch, choices=[1, 2])        # mineru, basic

    run_scans()

    assert documents_section() == {"ocr_engine": "mineru", "mineru_tier": "basic"}
    assert product.setting("documents", "ocr_engine", "auto", str) == "mineru"
    assert product.setting("documents", "mineru_tier", "advanced", str) == "basic"


def test_enter_keeps_what_is_set_and_writes_nothing_new(machine, monkeypatch):
    present, _ran = machine
    present[mineru.BINARY] = "/opt/bin/mineru-kit"
    answers(monkeypatch)

    run_scans()

    assert documents_section() == {}


def test_each_menu_opens_on_the_current_value(machine, monkeypatch):
    present, _ran = machine
    present[mineru.BINARY] = "/opt/bin/mineru-kit"
    answers(monkeypatch, choices=[1, 1])
    run_scans()
    asked = answers(monkeypatch)

    run_scans()

    assert [default for _question, _options, default in asked["choice"]] == [1, 1]


def test_how_hard_mineru_reads_is_not_asked_when_ocrmypdf_reads(machine, monkeypatch):
    asked = answers(monkeypatch, choices=[2], yes_no=[False])

    run_scans()

    assert len(asked["choice"]) == 1 and documents_section() == {"ocr_engine": "ocrmypdf"}


def test_a_missing_mineru_is_installed_with_uv_when_wanted(machine, monkeypatch):
    _present, ran = machine
    answers(monkeypatch, yes_no=[True, False])

    wizard = run_scans()

    assert ["/opt/bin/uv", "tool", "install", "mineru"] in ran
    assert wizard.state["scans"].startswith("MinerU")


def test_declining_installs_nothing(machine, monkeypatch):
    _present, ran = machine
    answers(monkeypatch, yes_no=[False])

    wizard = run_scans()

    assert not any(command[1:3] == ["tool", "install"] for command in ran)
    assert "MinerU" not in wizard.state["scans"]


def test_without_uv_the_command_is_shown_and_nothing_runs(machine, monkeypatch, capsys):
    present, ran = machine
    del present["uv"]
    asked = answers(monkeypatch)

    run_scans()

    assert ran == [] and not any("Install MinerU" in question for question in asked["yes_no"])
    assert "uv tool install mineru" in capsys.readouterr().out


def test_an_install_off_path_says_how_to_put_it_on(machine, monkeypatch, capsys):
    present, _ran = machine
    present["on_path_after_install"] = False
    answers(monkeypatch, yes_no=[True])

    run_scans()

    out = capsys.readouterr().out
    assert "/home/me/.local/bin" in out and "uv tool update-shell" in out
    assert "not installed" not in out


def test_an_install_off_path_is_what_the_summary_says(machine, monkeypatch):
    present, _ran = machine
    present["on_path_after_install"] = False
    answers(monkeypatch, yes_no=[True])

    wizard = run_scans()

    assert wizard.state["scans_ok"] is False and "update-shell" in wizard.state["scans"]


def test_the_models_are_offered_right_after_the_install_for_the_tier_chosen(machine, monkeypatch):
    _present, ran = machine
    answers(monkeypatch, choices=[0, 2], yes_no=[True, True])     # install; basic; fetch

    run_scans()

    assert ran[-1] == ["/opt/bin/mineru-kit", "models", "download", "--tier", "basic"]


def test_advanced_fetches_the_models_standard_uses(machine, monkeypatch):
    _present, ran = machine
    answers(monkeypatch, yes_no=[True, True])

    run_scans()

    assert ran[-1] == ["/opt/bin/mineru-kit", "models", "download", "--tier", "standard"]


def test_the_models_are_not_offered_again_on_a_later_review(machine, monkeypatch):
    present, ran = machine
    present[mineru.BINARY] = "/opt/bin/mineru-kit"
    asked = answers(monkeypatch)

    run_scans()

    assert asked["yes_no"] == [] and ran == []


def test_a_missing_tesseract_is_named_with_its_install_command(machine, monkeypatch, capsys):
    present, _ran = machine
    present[mineru.BINARY] = "/opt/bin/mineru-kit"
    answers(monkeypatch)

    run_scans()

    assert setup._install_command("tesseract") in capsys.readouterr().out


def test_the_summary_says_what_reads_scans(machine, monkeypatch, capsys):
    present, _ran = machine
    present[mineru.BINARY] = "/opt/bin/mineru-kit"
    answers(monkeypatch)
    wizard = run_scans()

    wizard.summary()

    assert "MinerU, advanced" in capsys.readouterr().out


def test_mineru_chosen_but_missing_is_said_plainly(machine, monkeypatch):
    answers(monkeypatch, choices=[1], yes_no=[False])

    wizard = run_scans()

    assert wizard.state["scans_ok"] is False and "not installed" in wizard.state["scans"]

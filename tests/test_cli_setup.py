"""``setupCli`` (pi's ``cli/setup.ts``): what every MISAKA process sets up before its command runs."""

from __future__ import annotations

import os
import sys
import warnings

from misaka.cli.setup import setupCli


def test_names_the_agent_for_every_process_it_starts(monkeypatch):
    monkeypatch.delenv("MISAKA_CODING_AGENT", raising=False)
    monkeypatch.delenv("AI_AGENT", raising=False)
    monkeypatch.setattr(sys, "warnoptions", [])
    with warnings.catch_warnings():
        setupCli()
        assert warnings.filters[0][0] == "ignore"
    assert os.environ["MISAKA_CODING_AGENT"] == "true"
    assert os.environ["AI_AGENT"] == "misaka"


def test_warnings_asked_for_on_the_command_line_still_show(monkeypatch):
    monkeypatch.setenv("AI_AGENT", "x")
    monkeypatch.setenv("MISAKA_CODING_AGENT", "x")
    monkeypatch.setattr(sys, "warnoptions", ["default"])
    with warnings.catch_warnings():
        before = list(warnings.filters)
        setupCli()
        assert warnings.filters == before

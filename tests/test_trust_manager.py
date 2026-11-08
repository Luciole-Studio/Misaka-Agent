"""pi v1.5.0 ``test/trust-manager.test.ts``: the two regression tests for
https://github.com/earendil-works/pi/issues/10488 (Windows paths compared case-sensitively).

MISAKA fork: ``hasTrustRequiringProjectResources`` walks for Sisters agent definitions and stops at $HOME, where
upstream walks for ``.agents/skills`` and skips the user's own; the first test checks that stop instead.
"""

from __future__ import annotations

import json
import sys

from misaka.config import home
from misaka.core.trust_manager import (
    ProjectTrustStore,
    hasTrustRequiringProjectResources,
)


def test_stops_at_home_when_home_differs_only_by_case_on_windows(tmp_path, monkeypatch):
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "misaka-home"))
    # Sisters definitions above $HOME: a walk that misses $HOME goes on and finds them.
    (tmp_path / home.DIR_NAME / home.SUBAGENTS_DIR).mkdir(parents=True)
    mixedCaseHome = tmp_path / "Home"
    projectDir = mixedCaseHome / "project"
    projectDir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(mixedCaseHome).lower())
    monkeypatch.setattr(sys, "platform", "win32")

    assert hasTrustRequiringProjectResources(str(projectDir)) is False


def test_matches_saved_decisions_regardless_of_casing_on_windows(tmp_path, monkeypatch):
    agentDir = tmp_path / "agent"
    agentDir.mkdir()
    projectDir = tmp_path / "Project-Mixed"
    projectDir.mkdir()
    store = ProjectTrustStore(str(agentDir))
    store.set(str(projectDir), True)
    monkeypatch.setattr(sys, "platform", "win32")

    assert store.get(str(projectDir).lower()) is True
    store.set(str(projectDir).lower(), False)
    saved = json.loads((agentDir / "trust.json").read_text(encoding="utf-8"))
    assert list(saved.values()) == [False]
    assert store.get(str(projectDir)) is False

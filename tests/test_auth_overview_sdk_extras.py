"""``misaka auth check`` and the setup wizard name the extra a provider's wire needs (``missing_sdk_extras``).

pi 1.3.0 moved kimi-coding from anthropic-messages to openai-completions: an install with only ``misaka[anthropic]``
can no longer reach Kimi Code, and the hint must say ``openai``. ``providers`` (README, release archives) has both.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

from misaka.ai.compat import getModels
from misaka.cli import auth_overview


def test_kimi_code_needs_the_openai_extra(monkeypatch):
    runtime = SimpleNamespace(getModels=lambda: getModels("kimi-coding"))
    monkeypatch.setattr(auth_overview, "sdk_installed", lambda module: module == "anthropic")

    assert auth_overview.missing_sdk_extras(runtime, "kimi-coding") == ["openai"]


def test_the_providers_extra_brings_the_openai_sdk():
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    extras = project["project"]["optional-dependencies"]

    assert "openai" in extras["providers"][0]
    assert any(requirement.startswith("openai") for requirement in extras["openai"])

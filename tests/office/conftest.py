"""Office fixtures never populate the developer's HOME, caches or intent archives."""
import importlib

import pytest


@pytest.fixture(autouse=True)
def office_state(tmp_path, monkeypatch):
    from misaka.config.product import CFG
    from misaka.core.documents.office import soffice
    # Required production dependencies: a missing package is a failure, not a green skip.
    for module in ("docx", "openpyxl", "pptx", "PIL"):
        importlib.import_module(module)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MISAKA_PAGEINDEX", str(tmp_path / "corpus"))
    monkeypatch.setenv("MISAKA_RUNS_HOME", str(tmp_path / "runs"))
    monkeypatch.setitem(CFG, "office_intent", str(tmp_path / "intent"))
    monkeypatch.setitem(CFG, "office_cache", str(tmp_path / "cache"))
    # Unit tests do not launch external applications. Dedicated integration smoke
    # checks exercise the real installed LibreOffice separately.
    monkeypatch.setattr(soffice, "_MACOS_PATH", "/nonexistent/soffice")
    monkeypatch.setattr(soffice.shutil, "which", lambda name: None)

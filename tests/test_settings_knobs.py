"""A bad settings.json value stops a process at its entry, never in the middle of its work.

``setting()`` used to raise ``SystemExit`` wherever it was read -- inside the panel daemon, a
research driver or a live TUI -- the first time the code path ran after the user's edit.
"""
import ast
import json
import logging
from pathlib import Path

import pytest

from misaka.cli import bootstrap
from misaka.config import home, product

ROOT = Path(product.__file__).resolve().parents[1]


def _write(document):
    path = home.path("settings")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_every_knob_read_in_the_code_is_registered_with_its_type():
    seen = set()
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "setting"):
                continue
            if len(node.args) < 3 or not all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args[:2]):
                continue
            section, key = node.args[0].value, node.args[1].value
            cast = node.args[3] if len(node.args) > 3 else next(
                (k.value for k in node.keywords if k.arg == "cast"), None)
            cast_name = getattr(cast, "id", None) if cast is not None else None
            assert (section, key) in product.KNOBS, f"{path}:{node.lineno} reads unregistered knob {section}.{key}"
            registered = product.KNOBS[section, key]
            assert cast_name == (registered.__name__ if registered else None), f"{path}:{node.lineno} {section}.{key}"
            seen.add((section, key))
    for section, key, _default, _cast in product._KNOBS.values():
        seen.add((section, key))
    assert seen == set(product.KNOBS), f"registered but never read: {set(product.KNOBS) - seen}"


def test_a_bad_value_read_at_runtime_warns_once_and_uses_the_default(caplog):
    _write({"mcp": {"required_wait": "soon"}})
    with caplog.at_level(logging.WARNING, logger="misaka.config.product"):
        assert product.setting("mcp", "required_wait", 120.0, float) == 120.0
        assert product.setting("mcp", "required_wait", 120.0, float) == 120.0
    assert [r.getMessage() for r in caplog.records].count(
        "settings.json: mcp.required_wait='soon' is not a number; using the default 120.0") == 1


def test_good_values_are_still_read():
    _write({"mcp": {"required_wait": "45"}, "subagents": {"simple": "yes"}})
    assert product.setting("mcp", "required_wait", 120.0, float) == 45.0
    assert product.setting("subagents", "simple", False, bool) is True


def test_the_entry_refuses_to_start_and_names_every_bad_value():
    _write({"mcp": {"required_wait": "soon"}, "subagents": {"max_concurrent": True, "simple": "maybe"},
            "research": {"token_cap": 5}})
    assert sorted(product.validate_settings()) == [
        "mcp.required_wait='soon' is not a number",
        "subagents.max_concurrent=True is not an integer",
        "subagents.simple='maybe' is not a boolean",
    ]
    with pytest.raises(SystemExit) as refused:
        bootstrap.install()
    assert "mcp.required_wait" in str(refused.value) and "subagents.simple" in str(refused.value)
    bootstrap.install(check_settings=False)


def test_a_negative_token_cap_is_refused_and_read_as_none():
    """A negative cap stopped every research run at its start."""
    from misaka.core.platform import budget
    _write({"research": {"token_cap": -1}})
    assert product.validate_settings() == ["research.token_cap=-1 is negative (0 is none)"]
    assert budget.default_cap() == 0

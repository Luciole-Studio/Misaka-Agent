"""The config entry point: `misaka web`, so credentials and backend choice are not hand-edited.

The document can hold vendor API keys, so the writer forces 0600 and `status` never prints a
value -- only whether one is set and where it came from.
"""

from __future__ import annotations

import json
import stat

import pytest
from webconf import read_web

from misaka.config import home
from misaka.core.web import config


class _Layer:
    """What the product stored: the ``web`` section of settings.json with the credentials folded in."""

    def read_text(self):
        return json.dumps(read_web())

    def stat(self):
        return home.path("env").stat()


@pytest.fixture
def web_home():
    return _Layer()


def test_a_scalar_key_is_written(web_home):
    config.set_config("backend", "tavily")
    assert json.loads(web_home.read_text())["backend"] == "tavily"


def test_a_flag_key_is_coerced_to_a_bool_not_a_string(web_home):
    config.set_config("keyless_rescue", "off")
    assert json.loads(web_home.read_text())["keyless_rescue"] is False


def test_the_cache_keys_are_settable(web_home):
    """Both are read by the memo; before this they were rejected as unknown keys."""
    config.set_config("cache_enabled", "off")
    config.set_config("cache_ttl_minutes", "45")
    doc = json.loads(web_home.read_text())
    assert doc["cache_enabled"] is False
    assert doc["cache_ttl_minutes"] == "45"

    from misaka.core.web import cache

    assert cache.cache_enabled() is False
    config.set_config("cache_enabled", "on")
    assert cache.ttl_seconds() == 45 * 60.0


def test_choosing_a_backend_clears_that_backend_s_stale_tier_pin(web_home):
    """A leftover free pin silently decides where the keyless ring starts its walk."""
    config.set_config("provider_tier.exa", "free")
    config.set_config("backend", "exa")
    assert "provider_tier" not in json.loads(web_home.read_text())

    config.set_config("provider_tier.keenable", "paid")
    config.set_config("backend", "exa")
    # Another vendor's pin is somebody's deliberate choice and is left alone.
    assert json.loads(web_home.read_text())["provider_tier"] == {"keenable": "paid"}


def test_a_list_key_is_split_on_commas(web_home):
    config.set_config("cache_exempt_hosts", "staging.example, *.tunnel.test")
    assert json.loads(web_home.read_text())["cache_exempt_hosts"] == [
        "staging.example",
        "*.tunnel.test",
    ]


def test_a_nested_list_and_flag_are_coerced(web_home):
    config.set_config("website_blocklist.enabled", "on")
    config.set_config("website_blocklist.domains", "ads.example,tracker.test")
    doc = json.loads(web_home.read_text())["website_blocklist"]
    assert doc == {"enabled": True, "domains": ["ads.example", "tracker.test"]}


def test_the_extract_keys_are_settable(web_home):
    config.set_config("extract_backend", "firecrawl")
    config.set_config("extract_char_limit", "30000")
    doc = json.loads(web_home.read_text())
    assert doc["extract_backend"] == "firecrawl"

    from misaka.core.web import extract

    assert extract.extract_char_limit() == 30000


def test_a_bad_flag_value_is_refused(web_home):
    with pytest.raises(ValueError, match="true/false"):
        config.set_config("keyless_fallback", "maybe")


def test_a_credential_lands_in_the_env_section(web_home):
    config.set_config("env.TAVILY_API_KEY", "tvly-abc")
    assert json.loads(web_home.read_text())["env"]["TAVILY_API_KEY"] == "tvly-abc"


def test_the_file_holding_credentials_is_not_world_readable(web_home):
    config.set_config("env.TAVILY_API_KEY", "tvly-abc")
    assert stat.S_IMODE(web_home.stat().st_mode) == 0o600


def test_a_tier_must_be_valid(web_home):
    with pytest.raises(ValueError, match="tier must be"):
        config.set_config("provider_tier.exa", "cheap")
    config.set_config("provider_tier.exa", "free")
    assert json.loads(web_home.read_text())["provider_tier"]["exa"] == "free"


def test_a_section_cannot_be_set_as_a_scalar(web_home):
    with pytest.raises(ValueError, match="section"):
        config.set_config("env", "x")


def test_keys_nest_at_most_one_level(web_home):
    with pytest.raises(ValueError, match="one level"):
        config.set_config("env.a.b", "x")


def test_unset_removes_a_key_and_prunes_an_empty_section(web_home):
    config.set_config("env.TAVILY_API_KEY", "tvly-abc")
    config.unset_config("env.TAVILY_API_KEY")
    assert "env" not in json.loads(web_home.read_text())


def test_unset_is_a_noop_on_an_absent_key(web_home):
    config.set_config("backend", "tavily")
    config.unset_config("search_backend")
    assert json.loads(web_home.read_text()) == {"backend": "tavily"}


def test_credential_status_never_reveals_the_value(web_home):
    config.set_config("env.TAVILY_API_KEY", "tvly-secret-value")
    rows = {name: (is_set, src) for name, is_set, src in config.credential_status()}
    assert rows["TAVILY_API_KEY"] == (True, home.display(home.path("env")))
    assert rows["EXA_API_KEY"] == (False, "")


def test_an_exported_credential_wins_over_the_file(web_home, monkeypatch):
    config.set_config("env.TAVILY_API_KEY", "from-file")
    monkeypatch.setenv("TAVILY_API_KEY", "from-env")
    rows = {name: src for name, is_set, src in config.credential_status() if is_set}
    assert rows["TAVILY_API_KEY"] == "env"


def test_the_web_command_status_runs_and_hides_secrets(web_home, capsys):
    config.set_config("env.TAVILY_API_KEY", "tvly-secret-value")
    from misaka.cli import app

    app.main(["web", "status"])
    out = capsys.readouterr().out
    assert "TAVILY_API_KEY" in out
    assert "tvly-secret-value" not in out


def test_a_misspelt_top_level_key_is_refused(web_home):
    with pytest.raises(ValueError, match="unknown key"):
        config.set_config("backned", "tavily")

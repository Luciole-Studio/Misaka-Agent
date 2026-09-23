"""Hermes #113017 / 010a45097e49: capability keys do not pin the shared backend.

The upstream regression is adapted to MISAKA's WebScope, not Hermes' global YAML
loader. Provider calls are stubbed; no credentials or requests leave this fixture.
"""

from unittest.mock import AsyncMock

import pytest

from misaka.core.web import dispatch, registry
from misaka.core.web.scope import WebScope


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "ddgs_package_importable", lambda: False)
    monkeypatch.setattr("misaka.core.web.gateway.available", lambda: False)
    with WebScope(str(tmp_path), config={}, environment={}).activate() as current:
        yield current


def test_upstream_extract_only_config_does_not_reroute_search(scope, monkeypatch):
    scope.config = {"extract_backend": "keenable"}
    monkeypatch.setattr(registry, "ddgs_package_importable", lambda: True)
    assert registry.search_backend_name() == "ddgs"
    assert registry.extract_backend_name() == "keenable"


@pytest.mark.parametrize("selection", ["search_backend", "extract_backend"])
@pytest.mark.parametrize("value", ["exa", "firecrawl", "searxng", "typo"])
def test_other_capability_uses_the_keyed_ladder(scope, selection, value):
    scope.config = {selection: value, "env": {"EXA_API_KEY": "fixture-key"},
                    "provider_tier": {"exa": "paid"}}
    other = "extract" if selection == "search_backend" else "search"
    resolve = dispatch.resolve_extractor if other == "extract" else dispatch.resolve_provider
    provider, name, error = resolve()
    assert error == ""
    assert provider.name == name == "exa"
    assert not dispatch.serves_keyless(provider)


@pytest.mark.parametrize("backend", ["exa", "tavily", "nous", "typo"])
def test_shared_selection_still_bypasses_autodetect(scope, monkeypatch, backend):
    scope.config = {"backend": backend, "extract_backend": "keenable"}
    monkeypatch.setattr(registry, "ddgs_package_importable", lambda: True)
    assert registry.backend_name() == backend
    assert registry.search_backend_name() == backend
    assert registry.extract_backend_name() == "keenable"


def test_keyed_priority_is_unchanged(scope):
    scope.config = {"extract_backend": "firecrawl", "env": {
        "TAVILY_API_KEY": "fixture-tavily", "EXA_API_KEY": "fixture-exa",
        "FIRECRAWL_API_KEY": "fixture-firecrawl"}}
    assert registry.search_backend_name() == "tavily"


def test_upstream_search_only_autodetect_still_reports_capability_error(scope):
    scope.config = {"search_backend": "searxng", "env": {"SEARXNG_URL": "https://index.test"}}
    provider, name, error = dispatch.resolve_extractor()
    assert provider is None
    assert name == "searxng"
    assert "search-only" in error


async def test_real_dispatcher_calls_keyed_exa_without_rescue(scope, monkeypatch):
    scope.config = {"search_backend": "exa", "provider_tier": {"exa": "paid"},
                    "env": {"EXA_API_KEY": "fixture-key"}}
    registry.ensure_backends_registered()
    exa = registry.get_provider("exa")
    call = AsyncMock(return_value=[{"url": "https://example.test/", "content": "fixture"}])
    monkeypatch.setattr(exa, "extract", call)

    async def unexpected(*args, **kwargs):
        pytest.fail("the extract must not enter Firecrawl or keyless rescue")

    monkeypatch.setattr(registry.get_provider("firecrawl"), "extract", unexpected)
    monkeypatch.setattr(dispatch, "rescue_extract", unexpected)
    provider, name, error = dispatch.resolve_extractor()
    results, rescued = await dispatch.web_extract(provider, ["https://example.test/"])
    assert provider is exa and name == "exa" and not error
    call.assert_awaited_once_with(["https://example.test/"], format=None)
    assert results[0]["content"] == "fixture" and not rescued


def test_status_uses_the_actual_exa_route(scope, monkeypatch, capsys):
    from misaka.cli import web

    scope.config = {"search_backend": "exa", "provider_tier": {"exa": "paid"},
                    "env": {"EXA_API_KEY": "fixture-key"}}
    monkeypatch.setattr("misaka.cli.web_browser.status", lambda: None)
    web._status()
    output = capsys.readouterr().out
    assert "Search backend: exa  (ready; provider direct)" in output
    assert "Extract backend: exa  (ready; provider direct)" in output
    assert "fixture-key" not in output

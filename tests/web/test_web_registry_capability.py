"""Search and extract are two questions the registry answers separately.

Every resolution step in ``registry._resolve`` filters by capability, so these tests pin
one step each: the explicitly-configured name, the single-eligible shortcut, the legacy
preference walk and the keyless ring. A backend that only searches must never surface as
the extract provider -- it would be handed a batch of URLs it has no renderer for and
answer with nothing, where a None lets the caller say "that backend is search-only".

Nothing here touches the network or the bundled backends: the fakes below control their
own capability flags, which is the whole subject.
"""

from __future__ import annotations

import json

import pytest

from misaka.config.product import CFG
from misaka.core.web import keyless, registry
from misaka.core.web.provider import WebSearchProvider

_VENDOR_ENV = (
    "BRAVE_SEARCH_API_KEY",
    "SEARXNG_URL",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "PARALLEL_API_KEY",
    "KEENABLE_API_KEY",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
)


@pytest.fixture(autouse=True)
def web_home(monkeypatch, tmp_path):
    """A throwaway web config and an empty vendor environment for every test."""
    for name in _VENDOR_ENV:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "web.json"
    monkeypatch.setitem(CFG, "web_config", str(path))
    registry.reset_for_tests()
    # The ring cursor is random per process; pin it so the keyless walk order is assertable.
    monkeypatch.setattr(keyless.current_scope(), "cursor", [0])
    yield path
    registry.reset_for_tests()


def write_config(path, **keys) -> None:
    path.write_text(json.dumps(keys), encoding="utf-8")


class _Fake(WebSearchProvider):
    """The fake from ``tests/test_web_providers.py``, with the capability flags exposed."""

    def __init__(self, name, *, available=False, keyless_ok=False, search=True, extract=False):
        self._name = name
        self._available = available
        self._keyless = keyless_ok
        self._search = search
        self._extract = extract

    @property
    def name(self):
        return self._name

    def is_available(self):
        return bool(self._available)

    def is_keyless_available(self):
        return bool(self._keyless)

    def supports_search(self):
        return self._search

    def supports_extract(self):
        return self._extract


def test_capability_flags_split_the_two_resolutions():
    registry.register_provider(_Fake("tavily", available=True))
    registry.register_provider(_Fake("render-only", available=True, search=False, extract=True))
    assert registry.resolve_search_provider(None).name == "tavily"
    assert registry.resolve_extract_provider(None).name == "render-only"


def test_the_single_eligible_shortcut_counts_only_capable_providers():
    """Neither name is in the legacy order or the ring, so only step 2 can answer."""
    registry.register_provider(_Fake("house-index", available=True))
    registry.register_provider(_Fake("house-render", available=True, extract=True))
    assert registry.resolve_extract_provider(None).name == "house-render"


def test_the_legacy_walk_steps_over_a_higher_priority_search_only_backend():
    registry.register_provider(_Fake("firecrawl", available=True))
    registry.register_provider(_Fake("parallel", available=True, extract=True))
    registry.register_provider(_Fake("exa", available=True, extract=True))
    # firecrawl leads the preference order and wins search; extract skips it for the
    # next capable name rather than stopping at the first available one.
    assert registry.resolve_search_provider(None).name == "firecrawl"
    assert registry.resolve_extract_provider(None).name == "parallel"


def test_the_keyless_walk_skips_a_ring_vendor_that_cannot_extract():
    registry.register_provider(_Fake("exa", keyless_ok=True))
    registry.register_provider(_Fake("parallel", keyless_ok=True, extract=True))
    assert registry.resolve_search_provider(None).name == "exa"
    assert registry.resolve_extract_provider(None).name == "parallel"


def test_the_extract_key_overrides_the_shared_backend_and_leaves_search_alone(web_home):
    write_config(web_home, backend="searxng", extract_backend="firecrawl")
    registry.register_provider(_Fake("searxng", available=True))
    registry.register_provider(_Fake("firecrawl", available=True, extract=True))
    assert registry.search_backend_name() == "searxng"
    assert registry.extract_backend_name() == "firecrawl"
    assert registry.active_search_provider().name == "searxng"
    assert registry.active_extract_provider().name == "firecrawl"


def test_the_extract_key_alone_counts_as_a_stored_selection(web_home):
    assert registry.selection_stored() is False
    write_config(web_home, extract_backend="firecrawl")
    assert registry.selection_stored() is True
    assert registry.extract_backend_name() == "firecrawl"


def test_a_configured_search_only_backend_resolves_to_nothing_for_extract(web_home):
    """Falling through to None is the point: the caller says "search-only", not "empty"."""
    write_config(web_home, extract_backend="searxng")
    registry.register_provider(_Fake("searxng", available=True))
    assert registry.extract_backend_name() == "searxng"
    assert registry.active_extract_provider() is None
    assert registry.active_search_provider().name == "searxng"


def test_a_half_split_config_leaves_the_other_capability_on_the_default(web_home, monkeypatch):
    """Setting one capability's key configures the section; the other one stops laddering.

    The user pointed search at their own index and never named an extract backend. The
    credential ladder would answer "searxng" here -- ``SEARXNG_URL`` is set, that is what
    makes the index reachable -- and web_extract would then refuse every call as
    search-only, for want of exactly the renderer the default names. So a configured
    section takes the default instead of reading the environment.
    """
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
    write_config(web_home, search_backend="searxng")
    assert registry.search_backend_name() == "searxng"
    assert registry.extract_backend_name() == "firecrawl"


def test_the_extract_half_alone_stops_the_search_side_laddering_too(web_home, monkeypatch):
    """The same branch, from the other direction: the gate is the section, not the key."""
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    write_config(web_home, extract_backend="firecrawl")
    assert registry.extract_backend_name() == "firecrawl"
    assert registry.search_backend_name() == "firecrawl"


def test_a_never_configured_install_still_runs_the_credential_ladder(web_home, monkeypatch):
    """The bound on the branch above: nothing stored means the environment still decides."""
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
    assert registry.selection_stored() is False
    assert registry.search_backend_name() == "searxng"
    assert registry.extract_backend_name() == "searxng"


def test_a_failed_registration_does_not_latch_the_registry_empty(monkeypatch):
    """Hermes' plugin load is non-fatal and retryable; so is this one.

    Latching the flag before the work means one broken import leaves the session with no
    backends at all and a "no provider configured" error that names the wrong cause.
    """
    from misaka.core.web import registry as reg

    reg.reset_for_tests()
    calls = {"n": 0}

    def explode():
        calls["n"] += 1
        raise RuntimeError("a backend module is broken")

    monkeypatch.setattr(
        "misaka.core.web.backends.register_builtin_providers", explode
    )
    reg.ensure_backends_registered()
    assert reg.list_providers() == []

    # The flag stayed down, so a later call tries again rather than being stuck.
    reg.ensure_backends_registered()
    assert calls["n"] == 2

    monkeypatch.undo()
    reg.reset_for_tests()
    reg.ensure_backends_registered()
    assert [p.name for p in reg.list_providers()]


async def test_a_configured_extract_only_backend_falls_back_to_search_capability(web_home, monkeypatch):
    """Hermes falls back by capability without ever calling extract-only search."""
    from misaka.core.web import dispatch
    from misaka.core.web import registry as reg

    class _ExtractOnly(WebSearchProvider):
        @property
        def name(self):
            return "renderonly"

        def is_available(self):
            return True

        def supports_search(self):
            return False

        def supports_extract(self):
            return True

    class _Search(WebSearchProvider):
        @property
        def name(self):
            return "searching"

        def is_available(self):
            return True

        async def search(self, query, limit=5):
            return {"success": True, "data": {"web": [], "used": self.name}}

    reg.ensure_backends_registered()
    reg.register_provider(_ExtractOnly())
    reg.register_provider(_Search())
    write_config(web_home, search_backend="renderonly")
    provider, backend, error = dispatch.resolve_provider()
    assert provider.name == backend == "searching"
    assert error == ""
    result = await dispatch.web_search("q")
    assert result["data"]["used"] == "searching"

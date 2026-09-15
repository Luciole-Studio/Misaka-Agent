"""All three web tools reach a session, and search is the only one that needs credentials.

`web_fetch` and `download_file` need no key: reading a page the model already has a URL
for does not depend on anyone's search subscription. Gating them behind search would have
reproduced the MISAKA_SERPER_KEY era, where the tool existed in the code and in no session.
"""

from __future__ import annotations

import pytest

from misaka.core import web
from misaka.core.wiring import SessionSpec, parts_for


def _spec(kind="card", workspace="/tmp/ws"):
    return SessionSpec(profile_dir="/tmp/profiles/sisters/x", role="x",
                       workspace=workspace, kind=kind)


def _names(monkeypatch, *, searchable):
    monkeypatch.setattr("misaka.core.web.registry.web_search_available",
                        lambda: searchable)
    return [t.name for t in web.part(_spec()).tools]


def test_every_web_tool_is_registered_when_a_backend_can_serve(monkeypatch):
    assert _names(monkeypatch, searchable=True) == [
        "web_search",
        "web_extract",
        "web_fetch",
        "download_file",
    ]


def test_fetch_and_download_survive_a_machine_with_no_search_backend(monkeypatch):
    """Both vendor-backed tools share one gate, as in Hermes; the direct ones need none."""
    assert _names(monkeypatch, searchable=False) == ["web_fetch", "download_file"]


def test_the_extension_activates_for_every_kind_it_declares():
    for kind in web.SESSION_KINDS:
        assert web.part(_spec(kind)) is not None, kind


@pytest.mark.parametrize("kind", sorted(web.SESSION_KINDS))
def test_assembly_reaches_the_tools_in_each_declared_kind(kind):
    # web is core: its tools take Pi's customTools door rather than the extension list.
    assert "web_fetch" in [tool.name for part in parts_for(_spec(kind)) for tool in part.tools]


def test_no_web_tool_is_marked_sequential(monkeypatch):
    """A sequential tool forces the whole batch serial (agent_loop.execute_tool_calls)."""
    monkeypatch.setattr("misaka.core.web.registry.web_search_available", lambda: True)
    assert [t.executionMode for t in web.part(_spec()).tools] == [None, None, None, None]

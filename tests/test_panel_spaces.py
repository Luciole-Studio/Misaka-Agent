"""A panel space has a durable id, and a pane's program knows which space it is in.

2026-09-29: space ids were a per-daemon counter (`w1`, `w2`), so a session could not say which
space it belonged to once the daemon was gone, and the sidebar told spaces apart by folder -- two
spaces on one folder listed and opened each other's sessions. Now the daemon mints `w` + 8 hex
digits, remembers every space some session still belongs to in state/spaces.json, reopens one
by id, and stamps MISAKA_NET_SPACE on every program it starts."""
import asyncio
import json
import os
import re
import sys

import pytest

from misaka.config import home
from misaka.core import session_catalog
from misaka.ui.panel import daemon as d


def _record(space, session_id="s-fixture"):
    """A saved session that belongs to ``space``, as the catalog keeps it."""
    index = session_catalog._index_dir()
    index.mkdir(parents=True, exist_ok=True)
    (index / f"{session_id}.json").write_text(json.dumps(
        {"id": session_id, "path": None, "space": space, "state": "saved"}), encoding="utf-8")


def _registry():
    return json.loads(home.path("spaces").read_text(encoding="utf-8"))


def _write_registry(value):
    path = home.path("spaces")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def daemon(tmp_path):
    return d.Daemon(str(tmp_path / "net.sock"), str(tmp_path / "net.json"))


def test_the_protocol_moved_on():
    assert d.PROTOCOL >= 52


def test_the_registry_is_pruned_to_the_spaces_sessions_belong_to(daemon, tmp_path):
    _write_registry({"wkeep0001": {"folder": str(tmp_path), "name": "kept"},
                     "wdrop0002": {"folder": "/", "name": None}})
    _record("wkeep0001")
    daemon._load_spaces()
    assert _registry() == {"wkeep0001": {"folder": str(tmp_path), "name": "kept"}}
    assert session_catalog.spaces_in_use() == {"wkeep0001"}
    layout = daemon._api("layout.get", {})
    assert layout["spaces"] == []
    assert layout["dormant"] == [{"id": "wkeep0001", "folder": str(tmp_path), "name": "kept"}]


def test_a_remembered_space_reopens_with_its_folder_and_name(daemon, tmp_path):
    folder = tmp_path / "research"
    folder.mkdir()
    _write_registry({"wabc12345": {"folder": str(folder), "name": "Research"}})
    _record("wabc12345")
    daemon._load_spaces()
    daemon._seat(d.Pane("p1", "Last Order", [], str(tmp_path)), {"space": "wabc12345"})
    daemon._seat(d.Pane("p2", "10036", [], "/"), {"space": "wabc12345", "name": "a Sister"})
    [space] = daemon.spaces
    assert (space["id"], space["folder"], space["name"]) == ("wabc12345", str(folder), "Research")
    assert [tab["name"] for tab in space["tabs"]] == ["Last Order", "a Sister"]
    assert daemon._api("layout.get", {})["dormant"] == []


def test_new_spaces_get_ids_that_outlive_the_daemon(daemon, tmp_path):
    daemon._seat(d.Pane("p1", "shell", [], str(tmp_path)), {})
    daemon._seat(d.Pane("p2", "shell", [], str(tmp_path)), {"space": True})
    first, second = daemon.spaces
    assert re.fullmatch(r"w[0-9a-f]{8}", first["id"]) and first["id"] != second["id"]
    assert _registry()[first["id"]] == {"folder": os.path.realpath(tmp_path), "name": None}
    renamed = [{**space, "name": "renamed"} if space is first else space for space in daemon.spaces]
    daemon.apply_layout(renamed, daemon.layout_revision)
    _record(first["id"])
    successor = d.Daemon(str(tmp_path / "net2.sock"), str(tmp_path / "net2.json"))
    successor._load_spaces()
    assert successor.known_spaces == {first["id"]: {"folder": os.path.realpath(tmp_path), "name": "renamed"}}


async def _read_when_written(path, tries=200):
    for _ in range(tries):
        if path.exists() and path.stat().st_size:
            return path.read_text(encoding="utf-8")
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} was never written")


async def test_a_pane_program_is_told_its_space(daemon, tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_NET_SPACE", "wstale000")
    out = tmp_path / "space.txt"
    probe = [sys.executable, "-c",
             f"import os; open({str(out)!r}, 'w').write(os.environ.get('MISAKA_NET_SPACE', '-'))"]
    panes = []
    try:
        panes.append(daemon.create(probe, str(tmp_path), title="probe"))
        own = daemon._tab_holding(panes[0].id)[0]["id"]
        assert await _read_when_written(out) == own
        out.unlink()
        panes.append(daemon.create(probe, str(tmp_path), title="probe", place={"space": "wabc12345"}))
        assert daemon._tab_holding(panes[1].id)[0]["id"] == "wabc12345"
        assert await _read_when_written(out) == "wabc12345"
        seats = [space["id"] for space in daemon.spaces]
        with pytest.raises(OSError):
            daemon.create([str(tmp_path / "no-such-program")], str(tmp_path), title="broken", place={"space": True})
        assert [space["id"] for space in daemon.spaces] == seats
    finally:
        for pane in panes:
            daemon.close(pane.id)
        await asyncio.gather(*list(daemon._reapers), return_exceptions=True)

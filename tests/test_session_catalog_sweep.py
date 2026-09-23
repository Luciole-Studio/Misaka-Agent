"""The session index does not fill up with pointers to processes that are gone.

2026-09-18 (B3): 18 of 19 catalog records named a dead pid, and eight control-socket
directories were still in /tmp. A pane the panel closes is sent SIGTERM and killed two
seconds later, which is less time than a runtime needs to finish `session_shutdown`, so it
never unlinks its own record. The daemon kills those processes, so the daemon sweeps up
after them -- but only records whose pid is *gone*, never one whose identity merely
disagrees, because that verdict has been wrong before (B20)."""
import json
import os
from pathlib import Path

import pytest

from misaka.core import session_catalog
from misaka.core.platform import processes


@pytest.fixture(autouse=True)
def owned_control_parent(tmp_path, monkeypatch):
    """Never let a test sweep the real /tmp: `sweep_dead` removes the control directories of
    dead sessions, and the developer's machine is full of them."""
    controls = tmp_path / "controls"
    controls.mkdir()
    monkeypatch.setattr(session_catalog, "_CONTROL_PARENTS", {str(controls)})
    return controls


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    index = tmp_path / "sessions" / ".catalog"
    index.mkdir(parents=True)
    monkeypatch.setattr(session_catalog, "_index_dir", lambda: index)
    return index


def _record(index, name, **fields):
    value = {"id": name, "pid": 424242, "identity": "host:424242:1.0", "state": "working",
             "cwd": "/tmp", "role": "last-order", "kind": "card"}
    value.update(fields)
    (index / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    return index / f"{name}.json"


def _control(tmp_path, name):
    directory = tmp_path / name
    directory.mkdir()
    (directory / "control.sock").write_text("", encoding="utf-8")
    return str(directory / "control.sock")


def _gone(pid, expected):
    return (False, "pid gone")


def test_a_record_whose_transcript_exists_is_marked_saved(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "explain_liveness", _gone)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    file = _record(catalog, "a", path=str(transcript), paused=True,
                   control=str(tmp_path / "controls" / "misaka-session-x" / "control.sock"))
    assert session_catalog.sweep_dead() == 1
    value = json.loads(file.read_text())
    assert value["state"] == "saved"
    assert "control" not in value and "paused" not in value


def test_a_record_with_no_transcript_is_removed(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "explain_liveness", _gone)
    file = _record(catalog, "b", path=str(tmp_path / "never-written.jsonl"), pending=True)
    assert session_catalog.sweep_dead() == 1
    assert not file.exists()


def test_an_in_memory_record_is_removed(catalog, monkeypatch):
    monkeypatch.setattr(processes, "explain_liveness", _gone)
    file = _record(catalog, "c", path=None)
    assert session_catalog.sweep_dead() == 1
    assert not file.exists()


def test_a_live_session_is_left_alone(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "explain_liveness", lambda pid, expected: (True, "alive"))
    transcript = tmp_path / "live.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    file = _record(catalog, "d", path=str(transcript))
    assert session_catalog.sweep_dead() == 0
    assert json.loads(file.read_text())["state"] == "working"


def test_an_identity_that_only_disagrees_is_left_alone(catalog, tmp_path, monkeypatch):
    """B20's verdict has been wrong inside a long-lived process; deleting on it would lose a
    live session's pointer. Only "the pid is gone" is acted on."""
    monkeypatch.setattr(processes, "explain_liveness",
                        lambda pid, expected: (False, "identity mismatch: recorded x, now y"))
    file = _record(catalog, "e", path=str(tmp_path / "missing.jsonl"))
    assert session_catalog.sweep_dead() == 0
    assert file.exists()
    assert json.loads(file.read_text())["state"] == "working"


def test_an_already_retired_record_is_not_rewritten(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "explain_liveness", _gone)
    transcript = tmp_path / "old.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    file = _record(catalog, "f", path=str(transcript), state="saved")
    before = file.stat().st_mtime_ns
    assert session_catalog.sweep_dead() == 0
    assert file.stat().st_mtime_ns == before


def test_the_control_socket_directory_goes_with_the_record(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "explain_liveness", _gone)
    monkeypatch.setattr(session_catalog, "_CONTROL_PARENTS", {str(tmp_path)})
    control = _control(tmp_path, "misaka-session-abc")
    _record(catalog, "g", path=None, control=control)
    session_catalog.sweep_dead()
    assert not os.path.exists(os.path.dirname(control))


def test_only_a_session_control_directory_is_ever_removed(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "explain_liveness", _gone)
    monkeypatch.setattr(session_catalog, "_CONTROL_PARENTS", {str(tmp_path)})
    stranger = _control(tmp_path, "someone-elses-work")
    _record(catalog, "h", path=None, control=stranger)
    session_catalog.sweep_dead()
    assert os.path.isdir(os.path.dirname(stranger))


def test_a_directory_outside_the_control_parents_is_never_removed(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "explain_liveness", _gone)
    monkeypatch.setattr(session_catalog, "_CONTROL_PARENTS", {str(tmp_path / "somewhere-else")})
    control = _control(tmp_path, "misaka-session-elsewhere")
    _record(catalog, "i", path=None, control=control)
    session_catalog.sweep_dead()
    assert os.path.isdir(os.path.dirname(control))


def test_the_sweep_survives_an_unreadable_record(catalog, monkeypatch):
    monkeypatch.setattr(processes, "explain_liveness", _gone)
    (catalog / "broken.json").write_text("{not json", encoding="utf-8")
    _record(catalog, "j", path=None)
    assert session_catalog.sweep_dead() == 1
    assert (catalog / "broken.json").exists()


async def test_the_daemon_sweeps_after_it_reaps_a_process(monkeypatch):
    """The daemon is what kills these processes, so it is what clears up after them."""
    import inspect
    from types import SimpleNamespace

    from misaka.ui.panel import daemon

    swept = []
    monkeypatch.setattr(session_catalog, "sweep_dead", lambda: swept.append(True) or 0)
    panel = daemon.Daemon.__new__(daemon.Daemon)
    await panel._reap(SimpleNamespace(poll=lambda: 0, pid=-1))
    assert swept == [True]
    # The startup pass is a loop that never returns; its wiring is read rather than run.
    assert "self._sweep_catalog()" in inspect.getsource(daemon.Daemon._watch_cards)


def test_control_parents_cover_the_directory_session_control_uses(monkeypatch):
    """SessionControl hardcodes /tmp for the socket's short path; the sweep must look there."""
    import inspect

    from misaka.core import session_control

    monkeypatch.undo()          # read the real value, not this file's isolated one
    assert 'dir="/tmp"' in inspect.getsource(session_control.SessionControl.start)
    assert Path("/tmp").resolve().as_posix() in {Path(p).as_posix()
                                                 for p in session_catalog._CONTROL_PARENTS}


# ── control directories nothing is listening in ────────────────────────────────────────

@pytest.fixture
def control_parent(monkeypatch):
    """A short parent of this test's own under /tmp: AF_UNIX paths are capped at 104 bytes on
    macOS, which is why SessionControl puts its socket there rather than in the default temp
    directory. Only this directory is ever swept, never /tmp itself."""
    import shutil
    import tempfile
    parent = tempfile.mkdtemp(prefix="misaka-sweep-", dir="/tmp")
    monkeypatch.setattr(session_catalog, "_CONTROL_PARENTS", {parent})
    try:
        yield Path(parent)
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def _socket_dir(parent, name, *, listening):
    import socket as socket_module
    directory = parent / name
    directory.mkdir()
    path = str(directory / "control.sock")
    server = None
    if listening:
        server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
    else:
        Path(path).write_text("", encoding="utf-8")
    os.utime(directory, (0, 0))          # older than the start-up grace period
    return directory, server


def test_a_directory_no_record_names_and_nothing_listens_in_is_removed(control_parent):
    directory, _ = _socket_dir(control_parent, "misaka-session-dead", listening=False)
    assert session_catalog._sweep_orphan_controls(set()) == 1
    assert not directory.exists()


def test_a_directory_with_a_listener_is_left_alone(control_parent):
    directory, server = _socket_dir(control_parent, "misaka-session-live", listening=True)
    try:
        assert session_catalog._sweep_orphan_controls(set()) == 0
        assert directory.exists()
    finally:
        server.close()


def test_a_directory_a_record_still_names_is_left_alone(control_parent):
    directory, _ = _socket_dir(control_parent, "misaka-session-owned", listening=False)
    assert session_catalog._sweep_orphan_controls({str(directory.resolve())}) == 0
    assert directory.exists()


def test_a_directory_that_has_just_appeared_is_left_alone(control_parent):
    """A session binds its socket just after making the directory; that gap is not an orphan."""
    directory = control_parent / "misaka-session-starting"
    directory.mkdir()
    assert session_catalog._sweep_orphan_controls(set()) == 0
    assert directory.exists()


def test_nothing_else_in_the_parent_is_touched(control_parent):
    stranger = control_parent / "someone-elses-work"
    stranger.mkdir()
    os.utime(stranger, (0, 0))
    plain = control_parent / "misaka-session-file"
    plain.write_text("not a directory", encoding="utf-8")
    assert session_catalog._sweep_orphan_controls(set()) == 0
    assert stranger.is_dir() and plain.is_file()


def test_the_record_pass_protects_the_directories_of_sessions_it_leaves_alone(catalog, control_parent,
                                                                              monkeypatch):
    monkeypatch.setattr(processes, "explain_liveness", lambda pid, expected: (True, "alive"))
    directory, _ = _socket_dir(control_parent, "misaka-session-busy", listening=False)
    _record(catalog, "k", path=None, control=str(directory / "control.sock"))
    assert session_catalog.sweep_dead() == 0
    assert directory.exists(), "a live session's socket directory survives a dead socket file"


def test_a_record_republished_while_we_looked_is_left_to_its_new_owner(catalog, tmp_path, monkeypatch):
    """`_retire` guards on an instance token before it writes; the sweep needs the same guard,
    or a session that starts on this transcript mid-sweep has its pointer marked saved."""
    file = _record(catalog, "l", path=str(tmp_path / "gone.jsonl"))

    def republish(pid, expected):
        _record(catalog, "l", path=str(tmp_path / "gone.jsonl"), pid=999, identity="host:999:2.0")
        return (False, "pid gone")

    monkeypatch.setattr(processes, "explain_liveness", republish)
    assert session_catalog.sweep_dead() == 0
    assert file.exists() and json.loads(file.read_text())["pid"] == 999


def test_a_retired_record_costs_no_liveness_probe(catalog, tmp_path, monkeypatch):
    """The sweep runs on every pane close over an index that never shrinks, and a liveness
    probe can cost a `ps` fork; the records it can rule out by reading are ruled out first."""
    probes = []
    monkeypatch.setattr(processes, "explain_liveness",
                        lambda pid, expected: (probes.append(pid), (False, "pid gone"))[1])
    transcript = tmp_path / "retired.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _record(catalog, "m", path=str(transcript), state="saved")
    assert session_catalog.sweep_dead() == 0
    assert probes == []


def test_a_symlinked_control_parent_is_not_followed(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "explain_liveness", _gone)
    monkeypatch.setattr(session_catalog, "_CONTROL_PARENTS", {str(tmp_path)})
    real = tmp_path / "misaka-session-real"
    real.mkdir()
    (real / "control.sock").write_text("", encoding="utf-8")
    link = tmp_path / "misaka-session-link"
    link.symlink_to(real, target_is_directory=True)
    _record(catalog, "n", path=None, control=str(link / "control.sock"))
    session_catalog.sweep_dead()
    assert real.is_dir(), "a symlinked parent must not redirect the removal"

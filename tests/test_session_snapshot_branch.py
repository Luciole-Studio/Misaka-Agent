"""Snapshots follow the selected branch, not the last appended entry."""
import json
from types import SimpleNamespace

import pytest

from misaka.core.session_control import SessionControl, request
from misaka.core.session_manager import SessionManager


async def test_snapshot_tracks_selected_and_empty_leaf():
    manager = SessionManager.inMemory(cwd="/tmp")
    root = manager.appendMessage({"role": "user", "content": "root", "timestamp": 0})
    left = manager.appendMessage({"role": "user", "content": "left", "timestamp": 1})
    manager.branch(root)
    right = manager.appendMessage({"role": "user", "content": "right", "timestamp": 2})
    session = SimpleNamespace(sessionManager=manager, sessionId=manager.sessionId, isIdle=True,
                              getSteeringMessages=list, getFollowUpMessages=list)
    control = SessionControl(session, None)
    for leaf, expected in ((right, [root, right]), (left, [root, left]), (None, [])):
        manager.resetLeaf() if leaf is None else manager.branch(leaf)
        snapshot = await control._execute({"operation": "snapshot"})
        assert snapshot["cursor"][0] == leaf
        assert [entry["id"] for entry in snapshot["entries"]] == expected
        unchanged = await control._execute({"operation": "snapshot", "cursor": snapshot["cursor"]})
        assert unchanged["entries"] is None


async def test_snapshot_does_not_read_another_branches_checkpoint():
    manager = SessionManager.inMemory(cwd="/tmp")
    root = manager.appendMessage({"role": "user", "content": "root", "timestamp": 0})
    left = manager.appendCompaction("left", "", 1, contextMessages=[
        {"role": "user", "content": "left summary", "timestamp": 1}])
    manager.branch(root)
    manager.appendCompaction("right", "", 1, contextMessages=[
        {"role": "user", "content": "right summary", "timestamp": 2}])
    manager.branch(left)
    session = SimpleNamespace(sessionManager=manager, sessionId=manager.sessionId, isIdle=True,
                              getSteeringMessages=list, getFollowUpMessages=list)
    snapshot = await SessionControl(session, None)._execute({"operation": "snapshot"})
    assert snapshot["entries"] == manager.buildContextEntries()


async def test_snapshot_socket_still_fences_session_owner(tmp_path):
    manager = SessionManager.inMemory(cwd=str(tmp_path))
    manager.appendMessage({"role": "user", "content": "fixture", "timestamp": 0})
    record = {"id": manager.sessionId, "instance": "original"}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(record))
    session = SimpleNamespace(sessionManager=manager, sessionId=manager.sessionId, isIdle=True,
                              getSteeringMessages=list, getFollowUpMessages=list)
    catalog = SimpleNamespace(record=str(path), instance="original", spec=None)
    control = SessionControl(session, catalog)
    await control.start()
    record["control"] = control.path
    try:
        snapshot = await request(record, "snapshot")
        assert snapshot["entries"] == manager.buildContextEntries()
        path.write_text(json.dumps({**record, "instance": "replacement"}))
        with pytest.raises(ValueError, match="changed owners"):
            await request(record, "snapshot")
    finally:
        control.close()

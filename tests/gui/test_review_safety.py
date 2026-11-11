"""Regressions for review findings at GUI trust and state boundaries."""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import httpx
import pytest

from misaka.ui.gui import settings_worker, workbench
from misaka.ui.gui.chat_runner import ChatHost
from misaka.ui.gui.chats import CLOSED_GRACE, ChatChannel, ChatManager
from misaka.ui.gui.composer import ComposerFiles
from misaka.ui.gui.jobs import OUTPUT_LIMIT, Jobs
from misaka.ui.gui.server import Bridge
from misaka.ui.gui.session_library import SessionLibrary, path_key


@pytest.mark.parametrize("origin,target", [
    ("http://catalog.test:8000/models", "http://catalog.test:8001/models"),
    ("https://catalog.test/models", "http://catalog.test/models"),
    ("http://catalog.test/models", "https://catalog.test/models"),
])
def test_redirect_never_sends_credentials_to_a_different_origin(origin, target):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": target})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    with patch("httpx.Client", return_value=client), pytest.raises(ValueError, match="重定向"):
        settings_worker._fetch_one(origin, {"Authorization": "Bearer fixture-key"}, "fixture-key")
    assert len(requests) == 1


def test_redirect_accepts_an_equivalent_default_port():
    """Verify redirect accepts an equivalent default port."""
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": "http://catalog.test:80/v1/models"})
        return httpx.Response(200, json={"data": []})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    with patch("httpx.Client", return_value=client):
        assert settings_worker._fetch_one("http://catalog.test/models", {}, "") == (200, {"data": []})
    assert len(requests) == 2


@pytest.mark.parametrize("role", [None, "", "   "])
def test_empty_role_is_rejected_before_resolving_a_profile(role):
    with patch.object(settings_worker, "_profile_for") as profile, pytest.raises(ValueError, match="角色"):
        settings_worker.op_read_role_file({"file": "SOUL.md", "role": role})
    profile.assert_not_called()


@pytest.mark.parametrize("platform,name,directory", [("darwin", "project.app", True), ("linux", "project.desktop", False)])
def test_project_launchers_cannot_be_opened(tmp_path, platform, name, directory):
    """Verify project launchers cannot be opened."""
    target = tmp_path / name
    target.mkdir() if directory else target.write_text("fixture", encoding="utf-8")
    with patch.object(workbench.sys, "platform", platform), patch.object(workbench.subprocess, "Popen") as launch:
        with pytest.raises(ValueError, match="可执行"):
            workbench.open_path(tmp_path, name)
    launch.assert_not_called()


def test_application_reveal_and_ordinary_directories_remain_available(tmp_path):
    (tmp_path / "project.app").mkdir()
    (tmp_path / "reports").mkdir()
    with patch.object(workbench.sys, "platform", "darwin"), patch.object(workbench.subprocess, "Popen") as launch:
        workbench.open_path(tmp_path, "project.app", reveal=True)
        assert launch.call_args.args[0][1] == "-R"
        workbench.open_path(tmp_path, "reports")
        assert launch.call_args.args[0] == ["open", str(tmp_path / "reports")]


def test_pin_does_not_override_a_later_derived_title_and_trash_keeps_a_label(tmp_path):
    store = SessionLibrary(tmp_path / "preferences.json")
    session = {"path": str(tmp_path / "chat.jsonl"), "workspace": str(tmp_path), "title": "Initial title"}
    store.update(session, "pin", True)
    assert "title" not in store.preferences(tmp_path)[path_key(session["path"])]
    store.update({**session, "title": "Updated title"}, "delete")
    bridge = Bridge(str(tmp_path))
    bridge.session_library = store
    assert bridge.dispatch("session_trash", {})["sessions"][0]["title"] == "Updated title"


async def test_disposal_cancels_tracked_operation_tasks():
    host = ChatHost({})
    started, stopped = asyncio.Event(), asyncio.Event()

    async def wait(_host, _params):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    host.ASYNC_OPS = {"wait": wait}
    with patch("misaka.ui.gui.chat_runner.emit"):
        host.dispatch({"id": 1, "op": "wait"})
        await asyncio.wait_for(started.wait(), 2)
        await host._dispose()
    assert stopped.is_set()
    assert not host._operations


def test_runtime_supported_thinking_levels_are_forwarded(tmp_path):
    manager = ChatManager(str(tmp_path))
    manager.request = Mock(return_value={"level": "max"})
    assert manager.set_thinking({"chat_id": "fixture", "level": "max", "persist": True}) == {"level": "max"}
    assert manager.request.call_args.args[2] == {"level": "max", "persist": True}


def test_ended_channel_keeps_final_events_then_expires(tmp_path):
    """Verify ended channel keeps final events then expires."""
    manager = ChatManager(str(tmp_path))
    channel = ChatChannel("fixture", {"workspace": str(tmp_path)}, "unused.log")
    channel.proc = Mock(stdout=io.StringIO(json.dumps({"type": "bye"}) + "\n"), stdin=None)
    channel.proc.poll.return_value = 0
    manager.channels[channel.id] = channel
    manager._pump(channel)
    assert any(event["type"] == "closed" for event in manager.events({"chat_id": channel.id, "wait": 0})["events"])
    with patch("misaka.ui.gui.chats.time.monotonic", return_value=channel.finished_at + CLOSED_GRACE + 1):
        assert manager.list() == []


def test_restoring_a_live_chat_uses_a_locked_channel_snapshot(tmp_path):
    """Restoration cannot iterate the registry while another thread removes entries."""
    bridge = Bridge(str(tmp_path))
    manager = bridge.chats

    class CheckedChannels(dict):
        def values(self):
            assert manager.lock.locked(), "Reading live channels requires the manager lock"
            return super().values()

    session = str(tmp_path / "fixture.jsonl")
    channel = ChatChannel("fixture", {"workspace": str(tmp_path), "sourceSession": session}, "unused.log")
    channel.status = "ready"
    manager.channels = CheckedChannels({channel.id: channel})
    bridge._saved_session = Mock(return_value=session)
    assert bridge.native_chat({"session_path": session}) == {"chat_id": channel.id}
    snapshot = manager.channels_snapshot()
    with manager.lock:
        manager.channels.clear()
    assert snapshot == [channel]


def test_cached_workspace_search_does_not_wait_for_another_walk(tmp_path):
    """Verify cached workspace search does not wait for another walk."""
    slow, cached = tmp_path / "slow", tmp_path / "cached"
    slow.mkdir()
    cached.mkdir()
    search = ComposerFiles()
    search.cache[os.path.normcase(str(cached.resolve()))] = (time.monotonic(), [{"path": "report.md", "name": "report.md"}], False)
    entered, release = threading.Event(), threading.Event()
    original = os.walk

    def walk(root, **kwargs):
        if root == slow:
            entered.set()
            assert release.wait(5)
        yield from original(root, **kwargs)

    with patch("misaka.ui.gui.composer.os.walk", side_effect=walk), ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(search.search, slow)
        try:
            assert entered.wait(2)
            assert pool.submit(search.search, cached).result(timeout=1)["total"] == 1
        finally:
            release.set()
        pending.result(timeout=2)


def wait_for_job(jobs, key, workspace):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        item = jobs.get(key, workspace)
        if item["status"] in {"done", "failed"} and item["exit_code"] is not None:
            return item
        time.sleep(0.01)
    raise AssertionError("Job did not terminate")


def test_cli_job_times_out_and_reaps_its_owned_process(tmp_path):
    jobs = Jobs(timeout=0.3, program=[sys.executable, "-u", "-c", "import time; time.sleep(60)"])
    key = jobs.start(str(tmp_path), [], "fixture")["job_id"]
    try:
        item = wait_for_job(jobs, key, str(tmp_path))
        assert item["status"] == "failed"
        assert "超过时限" in item["error"]
        assert item["exit_code"] != 0
    finally:
        jobs.close_all()


def test_cli_output_is_bounded_and_old_finished_jobs_are_evicted(tmp_path):
    """Verify cli output is bounded and old finished jobs are evicted."""
    script = "import sys; sys.stdout.buffer.write(('测'*100000+'END').encode()); sys.stdout.flush()"
    jobs = Jobs(finished_limit=1, program=[sys.executable, "-u", "-c", script])
    first = jobs.start(str(tmp_path), [], "first")["job_id"]
    item = wait_for_job(jobs, first, str(tmp_path))
    assert item["status"] == "done" and item["output"].endswith("END")
    assert len(item["output"].encode("utf-8")) <= OUTPUT_LIMIT
    assert "\ufffd" not in item["output"]
    second = jobs.start(str(tmp_path), [], "second")["job_id"]
    wait_for_job(jobs, second, str(tmp_path))
    with pytest.raises(ValueError, match="没有这项操作"):
        jobs.get(first, str(tmp_path))


def test_gui_shutdown_reaps_a_running_cli_job(tmp_path):
    """Closing the server must release its command process before the timeout."""
    jobs = Jobs(program=[sys.executable, "-u", "-c", "import time; time.sleep(60)"])
    key = jobs.start(str(tmp_path), [], "fixture")["job_id"]
    try:
        jobs.close_all()
        item = wait_for_job(jobs, key, str(tmp_path))
        assert item["status"] == "failed" and "网页服务已关闭" in item["error"]
        assert item["exit_code"] != 0
    finally:
        jobs.close_all()


def test_research_start_reports_an_unavailable_session_service(tmp_path):
    """Verify research start reports an unavailable session service."""
    bridge = Bridge(str(tmp_path))
    bridge.ensure = Mock(side_effect=ConnectionError("fixture connection refused"))
    bridge.research_runs.start = Mock(return_value={"job_id": "fixture"})
    result = bridge._research({}, ["research", "fixture"], "fixture")
    assert result["job_id"] == "fixture" and "fixture connection refused" in result["warning"]
    bridge.research_runs.start.assert_called_once()

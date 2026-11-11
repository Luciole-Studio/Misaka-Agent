"""Recovery after rejected delivery and a GUI process restart."""
import asyncio
import json
import subprocess
import sys
import time
from unittest.mock import Mock, patch

import pytest

from misaka.core.gui_input_queue import GuiInputQueue, InputNotAccepted
from misaka.ui.gui.chat_runner import ChatHost
from misaka.ui.gui.services import ResearchProcesses
from misaka.ui.gui.server import Bridge


@pytest.mark.parametrize("accepted", [False, True])
async def test_delivery_failure_only_retains_confirmed_rejection(accepted):
    """Preserve attachments before acceptance, but never replay an accepted turn."""
    class Session:
        async def waitForIdle(self):
            pass

        async def prompt(self, text, options):
            if accepted:
                options["preflightResult"]("started")
            raise RuntimeError("fixture failure")

    host = ChatHost({})
    host.session = Session()
    failed = asyncio.Event()
    queue = GuiInputQueue(host.session, host._deliver_prompt, failed=lambda _: failed.set())
    images = [{"type": "image", "data": "fixture"}]
    queue.submit({"message_id": "fixture", "text": "recover me", "images": images, "files": ["fixture.md"]})
    try:
        await asyncio.wait_for(failed.wait(), 2)
        if accepted:
            assert not queue.pending
        else:
            assert queue.paused
            restored = queue.withdraw("fixture")
            assert restored["images"] == images and restored["files"] == ["fixture.md"]
            assert restored["text"] == "recover me"
    finally:
        queue.clear()
        await asyncio.gather(queue.task, return_exceptions=True)


def test_research_receipt_survives_restart_and_rejects_reused_pid(tmp_path):
    """A new GUI recovers the live job and log, without launching a second owner."""
    manager = ResearchProcesses(tmp_path / "receipts")
    popen = subprocess.Popen
    children = []

    def launch(_args, **kwargs):
        proc = popen([sys.executable, "-u", "-c", "import time; print('Research run fixture-run:', flush=True); print('x'*8000, flush=True); time.sleep(60)"], **kwargs)
        children.append(proc)
        return proc

    try:
        with patch("misaka.ui.gui.services.subprocess.Popen", side_effect=launch) as spawn:
            first = manager.start(str(tmp_path), ["research", "fixture"], "fixture")
            deadline = time.monotonic() + 5
            while (tmp_path / "receipts" / (first["job_id"] + ".log")).stat().st_size < 8000:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            restored = ResearchProcesses(tmp_path / "receipts")
            row = restored.listing(str(tmp_path))[0]
            assert row["running"] and row["run_id"] == "fixture-run"
            assert restored.log(str(tmp_path), key=first["job_id"])["running"]
            assert restored.start(str(tmp_path), ["research", "--resume", "fixture-run"], "fixture") == first
            assert spawn.call_count == 1
        receipt = tmp_path / "receipts" / (first["job_id"] + ".json")
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["identity"] -= 10
        receipt.write_text(json.dumps(data), encoding="utf-8")
        assert not ResearchProcesses(tmp_path / "receipts").listing(str(tmp_path))[0]["running"]
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=5)


def test_resume_refuses_an_existing_driver_lease(tmp_path, monkeypatch):
    """Even a research launched outside this GUI must not get a second driver."""
    from misaka.config import CFG
    from misaka.core.research import runs

    database = tmp_path / "fixture.db"
    database.touch()
    monkeypatch.setitem(CFG, "db", str(database))
    bridge = Bridge(str(tmp_path))
    bridge._research = Mock()
    with patch("misaka.core.platform.tasks.connect", return_value=Mock()), \
         patch.object(runs, "listing", return_value=[{"id": "fixture"}]), \
         patch.object(runs, "driverless", return_value=False):
        with pytest.raises(ValueError, match="原进程"):
            bridge.dispatch("research_resume", {"run_id": "fixture"})
    bridge._research.assert_not_called()


@pytest.mark.parametrize("accepted", [False, True])
async def test_send_now_never_replays_an_accepted_turn(accepted):
    """Only a confirmed rejection restores an immediately steered message."""
    async def deliver(_item):
        raise RuntimeError("accepted failure") if accepted else InputNotAccepted("rejected")

    changed = Mock()
    queue = GuiInputQueue(Mock(isStreaming=True), deliver, changed=changed)
    item = {"message_id": "fixture", "text": "fixture", "images": ["image"]}
    queue.pending.append(item)
    with pytest.raises((RuntimeError, InputNotAccepted)):
        await queue.send_now("fixture")
    assert queue.pending == ([] if accepted else [item])
    changed.assert_called_once()


def test_corrupt_receipts_do_not_hide_healthy_jobs(tmp_path):
    """Keep damaged files intact, report their names, and load the valid receipt."""
    bad = tmp_path / "broken.json"
    bad.write_bytes(b"not json")
    good = {"id": "healthy", "workspace": str(tmp_path), "title": "fixture", "log": "fixture.log",
            "pid": 123, "identity": 0, "started": 0, "run_id": "fixture-run"}
    (tmp_path / "healthy.json").write_text(json.dumps(good), encoding="utf-8")
    manager = ResearchProcesses(tmp_path)
    with patch.object(manager, "_status", return_value=(False, 0)):
        assert manager.listing(str(tmp_path))[0]["id"] == "healthy"
    assert "broken.json" in manager.warning()
    assert bad.read_bytes() == b"not json"
    bad.unlink()
    manager._load()
    assert manager.warning() == ""


def test_receipt_mutation_and_save_share_a_lock(tmp_path):
    """Concurrent exit and run-banner updates leave a complete JSON receipt."""
    from concurrent.futures import ThreadPoolExecutor

    manager = ResearchProcesses(tmp_path)
    log = tmp_path / "fixture.log"
    log.write_text("Research run fixture-run:\n", encoding="utf-8")
    item = {"id": "fixture", "workspace": str(tmp_path), "title": "fixture", "log": str(log),
            "pid": 123, "identity": 0, "started": 0, "run_id": None, "proc": Mock()}
    item["proc"].poll.return_value = 0
    manager.items["fixture"] = item
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: manager.listing(str(tmp_path)) if n % 2 else manager._status(item), range(40)))
    saved = json.loads((tmp_path / "fixture.json").read_text(encoding="utf-8"))
    assert saved["run_id"] == "fixture-run" and saved["exit_code"] == 0
    assert "proc" not in saved


async def test_side_task_finishes_before_runtime_disposal():
    """Shutdown cancels a side task even after its panel has been closed."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    finished = asyncio.Event()
    async def prompt(_text):
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    host = ChatHost({})
    agent = Mock(state=SimpleNamespace(messages=[], isStreaming=False))
    agent.prompt = prompt
    host._side_agent = Mock(return_value=agent)
    async def dispose():
        assert finished.is_set()
    host.runtime = Mock(dispose=AsyncMock(side_effect=dispose))
    with patch("misaka.ui.gui.chat_runner.emit"):
        side = host.op_side({"text": "fixture"})
        await asyncio.sleep(0)
        task = host.sides[side["side_id"]]["task"]
        host.op_side_close(side)
        await host._dispose()
    assert task.cancelled()


@pytest.mark.parametrize("line", ["not json\n", "[]\n"])
async def test_malformed_ipc_shuts_down_with_visible_error(line):
    """Uncorrelatable invalid requests end the channel instead of being ignored."""
    import io
    host = ChatHost({})
    with patch("misaka.ui.gui.chat_runner.sys.stdin", io.StringIO(line)), \
         patch("misaka.ui.gui.chat_runner.emit") as emit:
        await asyncio.wait_for(host.serve(), 2)
    assert any(call.args[0]["type"] == "fatal" for call in emit.call_args_list)
    assert host.shutting_down


def test_close_handles_absent_process_and_only_kills_on_timeout():
    """Absent runners close normally; unrelated wait errors are not treated as hangs."""
    from misaka.ui.gui.chats import ChatChannel, ChatManager
    manager = ChatManager(".")
    channel = ChatChannel("fixture", {}, "fixture.log")
    manager.channels[channel.id] = channel
    assert manager.close(channel.id) == {"closed": True}
    manager.channels[channel.id] = channel
    channel.proc = Mock()
    channel.proc.wait.side_effect = OSError("fixture wait failure")
    with pytest.raises(OSError):
        manager.close(channel.id)
    channel.proc.kill.assert_not_called()
    channel.proc.wait.side_effect = [subprocess.TimeoutExpired("fixture", 1), 0]
    assert manager.close(channel.id) == {"closed": True}
    channel.proc.kill.assert_called_once()

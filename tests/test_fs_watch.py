"""MISAKA tests for ``misaka/utils/fs_watch.py``, the polling stand-in for Node's ``fs.watch``/``fs.watchFile``
(pi tests these through Node itself, so there is no upstream file). Each case pins one Node behaviour the footer and
theme watchers rely on."""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from misaka.core.footer_data_provider import FooterDataProvider
from misaka.utils.fs_watch import unwatchFile, watch, watchFile


async def waitFor(condition: Any, timeoutMs: int = 3000) -> None:
    deadline = time.monotonic() + timeoutMs / 1000
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("Timed out waiting for condition")
        await asyncio.sleep(0.02)


async def test_a_directory_watch_reports_the_changed_entry_on_the_loop(tmp_path: Path):
    (tmp_path / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    events: list[tuple[str, str | None, bool]] = []
    loopThread = threading.current_thread()
    watcher = watch(str(tmp_path), lambda eventType, filename: events.append(
        (eventType, filename, threading.current_thread() is loopThread)
    ))
    try:
        # Git replaces HEAD through a lock file and a rename.
        (tmp_path / "HEAD.lock").write_text("ref: refs/heads/feature\n", encoding="utf-8")
        os.replace(tmp_path / "HEAD.lock", tmp_path / "HEAD")
        await waitFor(lambda: any(filename == "HEAD" for _, filename, _ in events))

        assert ("rename", "HEAD", True) in events
    finally:
        watcher.close()


async def test_removing_the_watched_directory_reports_each_entry_and_then_the_directory(tmp_path: Path):
    watched = tmp_path / "wt"
    watched.mkdir()
    (watched / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    events: list[tuple[str, str | None]] = []
    watcher = watch(str(watched), lambda eventType, filename: events.append((eventType, filename)))
    try:
        shutil.rmtree(watched)
        await waitFor(lambda: ("rename", "wt") in events)

        assert ("rename", "HEAD") in events
        assert events.index(("rename", "HEAD")) < events.index(("rename", "wt"))
    finally:
        watcher.close()


async def test_closing_a_watcher_does_not_wait_and_drops_late_events(tmp_path: Path):
    events: list[Any] = []
    watcher = watch(str(tmp_path), lambda *args: events.append(args))
    started = time.monotonic()
    watcher.close()
    assert time.monotonic() - started < 0.05

    (tmp_path / "late").write_text("x", encoding="utf-8")
    await asyncio.sleep(0.3)
    assert events == []


def test_without_a_loop_a_raising_listener_does_not_stop_the_watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reported: list[type[BaseException] | None] = []
    monkeypatch.setattr("sys.excepthook", lambda kind, *_: reported.append(kind))
    calls: list[str | None] = []

    def listener(_eventType: str, filename: str | None) -> None:
        calls.append(filename)
        raise RuntimeError("listener failed")

    watcher = watch(str(tmp_path), listener)
    try:
        (tmp_path / "first").write_text("1", encoding="utf-8")
        deadline = time.monotonic() + 3
        while "first" not in calls and time.monotonic() < deadline:
            time.sleep(0.02)
        (tmp_path / "second").write_text("2", encoding="utf-8")
        while "second" not in calls and time.monotonic() < deadline:
            time.sleep(0.02)

        assert calls[:2] == ["first", "second"]
        assert reported[:2] == [RuntimeError, RuntimeError]
    finally:
        watcher.close()


async def test_watch_file_stops_calling_a_listener_once_it_is_unwatched(tmp_path: Path):
    target = tmp_path / "tables.list"
    target.write_bytes(b"0\n")
    kept: list[Any] = []
    removed: list[Any] = []

    def keptListener(current: Any, previous: Any) -> None:
        kept.append((current, previous))

    def removedListener(current: Any, previous: Any) -> None:
        removed.append((current, previous))

    watchFile(str(target), {"interval": 20}, keptListener)
    watchFile(str(target), removedListener)
    try:
        unwatchFile(str(target), removedListener)
        target.write_bytes(b"10\n")
        await waitFor(lambda: len(kept) > 0)

        assert removed == []
        current, previous = kept[0]
        assert (current.size, previous.size) == (3, 2)
    finally:
        unwatchFile(str(target))


def test_a_footer_provider_made_outside_an_event_loop_reads_the_branch_without_watchers(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    provider = FooterDataProvider(str(repo))
    try:
        assert provider.getGitBranch() == "main"
        assert provider._headWatcher is None
    finally:
        provider.dispose()

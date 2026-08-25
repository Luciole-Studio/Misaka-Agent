"""Cancelable process boundary for the card verification gate."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Mapping

from misaka.platform import processes as process_tree
from misaka.platform import tasks as db


@dataclass(frozen=True, slots=True)
class JudgeProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


async def _terminate(process: asyncio.subprocess.Process) -> None:
    captured = await asyncio.to_thread(process_tree.snapshot, process.pid)
    await asyncio.to_thread(process_tree.terminate, process.pid, captured)
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


def _identity(pid: int) -> str | None:
    host = socket.gethostname()
    try:
        fields = open(f"/proc/{pid}/stat", encoding="utf-8").read().rsplit(")", 1)[1].split()
        return f"{host}:{pid}:{fields[19]}"
    except (OSError, IndexError):
        pass
    try:
        started = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{host}:{pid}:{started}" if started else None


async def terminate_owned(pid: int | None, identity: str | None) -> bool:
    """Terminate a recorded verifier group without ever trusting a reused PID."""
    if not pid or not identity or _identity(int(pid)) != identity:
        return False
    captured = await asyncio.to_thread(process_tree.snapshot, int(pid))
    if _identity(int(pid)) != identity:
        return False
    await asyncio.to_thread(process_tree.terminate, int(pid), captured)
    return True


async def run(
    cfg: Mapping[str, Any],
    task_id: str,
    verify_token: str,
    timeout: float,
    generation: int | None = None,
) -> JudgeProcessResult:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "misaka.network.judge_child",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
        env={**os.environ, "MISAKA_INHERIT_PROCESS_GROUP": "1"},
    )
    payload = json.dumps(
        {
            "version": 1,
            "task_id": task_id,
            "verify_token": verify_token,
            "generation": generation,
            "cfg": dict(cfg),
        },
        ensure_ascii=False,
    ).encode()
    identity = _identity(process.pid)
    try:
        con = db.connect(str(cfg["db"]))
        try:
            attached = db.set_verifier_process(
                con,
                task_id,
                verify_token,
                process.pid,
                identity,
                generation=generation,
            )
        finally:
            con.close()
    except Exception:  # stdin may already be closed during process teardown
        await _terminate(process)
        raise
    if not attached:
        await _terminate(process)
        return JudgeProcessResult(
            process.returncode if process.returncode is not None else -1,
            "",
            "judge lease was revoked before process attachment",
        )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(payload), timeout=timeout)
    except asyncio.CancelledError:
        await _terminate(process)
        raise
    except asyncio.TimeoutError:
        await _terminate(process)
        return JudgeProcessResult(
            process.returncode if process.returncode is not None else -1,
            "",
            "judge process timed out",
            timed_out=True,
        )
    return JudgeProcessResult(
        process.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


__all__ = ["JudgeProcessResult", "run", "terminate_owned"]

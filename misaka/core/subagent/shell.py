"""CCB LocalShellTask/TaskOutput/stopTask through native BashOperations.

Pin 77a7934e15d69da13879112ed7db695c9ee7a52a. Keep one process owner,
source terminal/notification semantics, and native bounded output/process cleanup.
The native command/SDK entry point shares Agent's TaskOutput and TaskStop tools.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape

from misaka.ai.utils.abort import AbortController
from misaka.core.prompt_templates import _ECMASCRIPT_WHITESPACE
from misaka.core.tools.bash import BashOperations, BashSpawnContext
from misaka.core.tools.output_accumulator import (
    OutputAccumulator,
    OutputAccumulatorOptions,
)
from misaka.utils.async_lifecycle import settle

# CCB LocalShellTask.tsx: looksLikePrompt / startStallWatchdog.
STALL_CHECK_SECONDS = 5.0
STALL_THRESHOLD_SECONDS = 45.0
STALL_TAIL_BYTES = 1024
_PROMPT_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE | re.ASCII) for pattern in (
    r'\(y/n\)', r'\[y/n\]', r'\(yes/no\)',
    r'\b(?:Do you|Would you|Shall I|Are you sure|Ready to)\b.*\? *$',
    r'Press (any key|Enter)', r'Continue\?', r'Overwrite\?',
))


def looks_like_prompt(tail: str) -> bool:
    last_line = tail.rstrip(''.join(_ECMASCRIPT_WHITESPACE)).split('\n')[-1]
    return any(pattern.search(last_line) for pattern in _PROMPT_PATTERNS)


@dataclass
class ShellTask:
    manager: Any
    command: str
    cwd: str
    spawn_context: BashSpawnContext
    operations: BashOperations
    timeout: float | None = None
    description: str | None = None
    prepare_remote: Any = None
    background: bool = True
    on_data: Any = None
    id: str = field(default_factory=lambda: 'b' + secrets.token_hex(8))
    status: str = 'running'
    exit_code: int | None = None
    error: str | None = None
    notified: bool = False
    notification_id: str = field(default_factory=lambda: secrets.token_hex(16))
    controller: AbortController = field(default_factory=AbortController)
    output: OutputAccumulator = field(default_factory=lambda: OutputAccumulator(OutputAccumulatorOptions(tempFilePrefix='misaka-background')))
    runner: asyncio.Task | None = None
    _done: asyncio.Event = field(default_factory=asyncio.Event)
    _backgrounded: asyncio.Event = field(default_factory=asyncio.Event)
    _stall_job: asyncio.Task | None = None
    backgrounded_by_user: bool = True

    def output_data(self) -> dict:
        snapshot = self.output.snapshot(persistIfTruncated=True)
        return {'task_id': self.id, 'task_type': 'local_bash', 'status': self.status,
                'description': self.description if self.description is not None else self.command, 'output': snapshot.content,
                'exitCode': self.exit_code, 'error': self.error, 'outputFile': snapshot.fullOutputPath}

    def request_background(self) -> bool:
        # CCB ShellCommand.background / backgroundExistingForegroundTask:
        # one registration, one execution, no process restart.
        if self.status != 'running' or self.controller.aborted or self.background:
            return False
        self.background = True
        self.on_data = None
        self._backgrounded.set()
        self._start_stall_watchdog()
        return True

    def _handle_data(self, data):
        if self.on_data is not None:
            self.on_data(data)
        else:
            self.output.append(data)

    async def wait_foreground(self, signal, timeout, *, auto_background=False):
        """Source result/background race; native owner keeps process + output.

        Parent abort and timeout belong to this foreground wait, not the detached
        process. Completion wins a simultaneous background request; suppress its
        redundant notification as runShellCommand/markTaskNotified do upstream.
        """
        from misaka.ai.utils.abort import wait_for_abort
        from misaka.core.subagent._shell_parser import is_autobackgrounding_allowed
        from misaka.core.subagent.background import default_bash_timeout_seconds
        from misaka.utils.values import signal_aborted

        if auto_background and timeout is None:
            timeout = default_bash_timeout_seconds()
        auto_allowed = auto_background and is_autobackgrounding_allowed(self.command)
        detached = False
        backgrounded = asyncio.create_task(self._backgrounded.wait())
        aborted = asyncio.create_task(wait_for_abort(signal)) if signal is not None else None
        timer = asyncio.create_task(asyncio.sleep(timeout)) if timeout is not None else None
        watchers = [job for job in (backgrounded, aborted, timer) if job is not None]
        try:
            if signal_aborted(signal):
                raise RuntimeError('aborted')
            await asyncio.wait([self.runner, *watchers], return_when=asyncio.FIRST_COMPLETED)
            if self.status != 'running' or self.runner.done():
                await asyncio.shield(self.runner)
                if self.error is not None:
                    raise RuntimeError(self.error)
                return {'exitCode': self.exit_code}
            if self.background and not self.controller.aborted:
                detached = True
                return None
            if self.controller.aborted or signal_aborted(signal):
                raise RuntimeError('aborted')
            # CCB onTimeout/startBackgrounding: reuse the registered owner. Native
            # live scope can revoke the output/stop tools during the wait.
            active = getattr(self.manager.session, 'getActiveToolNames', None)
            manageable = active is None or {'TaskOutput', 'TaskStop'}.issubset(active())
            if (auto_allowed and manageable and timer is not None and timer.done()
                    and await self.manager.request_background(self)):
                self.backgrounded_by_user = False
                detached = True
                return None
            raise RuntimeError(f'timeout:{timeout}')
        finally:
            # A committed user handoff survives cancellation of the old caller.
            detached = detached or (self.background and self.status == 'running' and not self.controller.aborted)
            self.on_data = None
            for job in watchers:
                job.cancel()
            if not detached and not self.runner.done():
                self.controller.abort()
                self.runner.cancel()
            jobs = watchers if detached else [*watchers, self.runner]
            _, cancelled = await settle(asyncio.gather(*jobs, return_exceptions=True))
            if detached:
                if not self.controller.aborted:
                    self.notified = False
                    if self._done.is_set():
                        self.notify()
            else:
                # Source unregisterForeground: ordinary completions aren't tasks.
                self.notified = True
                if not self.background and self.manager._shell_tasks.get(self.id) is self:
                    self.manager._shell_tasks.pop(self.id)
            if cancelled is not None:
                raise cancelled

    def _start_stall_watchdog(self):
        if self._stall_job is None:
            self._stall_job = asyncio.create_task(self._watch_stall())

    async def _watch_stall(self):
        # Native pipe output already has bounded tail + raw-byte accounting;
        # reuse it instead of polling the same file through a second I/O path.
        loop = asyncio.get_running_loop()
        last_size, last_growth = 0, loop.time()
        while True:
            await asyncio.sleep(STALL_CHECK_SECONDS)
            if self.status != 'running' or self.manager._closed:
                return
            size = self.output.totalRawBytes
            if size > last_size:
                last_size, last_growth = size, loop.time()
                continue
            if loop.time() - last_growth < STALL_THRESHOLD_SECONDS:
                continue
            content = self.output.tailText.encode('utf-8')[-STALL_TAIL_BYTES:].decode('utf-8', errors='replace')
            if not looks_like_prompt(content):
                last_growth = loop.time()
                continue
            # One-shot latch is the return: no await between the check/enqueue.
            session = self.manager.session
            if session is not None:
                description = self.description if self.description is not None else self.command
                summary = f'Background command "{description}" appears to be waiting for interactive input'
                text = '<task-notification>\n' + '\n'.join(
                    f'<{key}>{escape(str(value))}</{key}>' for key, value in (
                        ('task-id', self.id), ('output-file', self.output.tempFilePath or ''),
                        ('summary', summary))) + '\n</task-notification>\nLast output:\n'
                text += content.rstrip(''.join(_ECMASCRIPT_WHITESPACE))
                text += "\n\nThe command is likely blocked on an interactive prompt. Kill this task and re-run with piped input (e.g., `echo y | command`) or a non-interactive flag if one exists."
                # Source intentionally omits status: this is NOT task completion.
                session.moments.send_message(
                    {'customType': 'task-notification', 'content': text, 'display': True,
                     'details': {'task_id': self.id, 'task_type': 'local_bash', 'progress': True}},
                    {'deliverAs': 'followUp', 'triggerTurn': True, '_deliveryId': 'shell-stall:' + self.notification_id})
            return

    async def run(self) -> None:
        try:
            if self.background:
                self._start_stall_watchdog()
            self.output.ensure_temp_file()
            if self.prepare_remote is not None:
                from misaka.utils.async_lifecycle import run_in_thread
                await run_in_thread(self.prepare_remote)
            result = await self.operations.exec(
                self.spawn_context.command, self.spawn_context.cwd,
                {'onData': self._handle_data, 'signal': self.controller,
                 'timeout': self.timeout, 'env': self.spawn_context.env})
            self.exit_code = result.get('exitCode')
            self.status = 'completed' if self.exit_code == 0 else 'failed'
        except asyncio.CancelledError:
            self.status = 'killed'
            self.notified = True
            raise
        except Exception as error:  # noqa: BLE001 - publish native command failures as task state
            self.error = str(error)
            self.status = 'killed' if self.controller.aborted else 'failed'
        finally:
            try:
                stall_cancelled = None
                if self._stall_job is not None:
                    self._stall_job.cancel()
                    _, stall_cancelled = await settle(asyncio.gather(self._stall_job, return_exceptions=True))
                self.output.finish()
                _, cancelled = await settle(asyncio.create_task(self.output.close_temp_file()))
                if cancelled is not None or stall_cancelled is not None:
                    self.status = 'killed'
                    self.notified = True
            finally:
                self._done.set()
            try:
                self.notify()
            except Exception:  # A closed parent must not invalidate command output.
                logging.getLogger(__name__).warning('Background shell notification was not queued', exc_info=True)

    def notify(self) -> None:
        session = self.manager.session
        if not self.background or self.notified or self.manager._closed or session is None:
            return
        summary = f'Background command "{self.command}" '
        summary += {'completed': f'completed (exit code {self.exit_code})',
                    'failed': f'failed with exit code {self.exit_code}', 'killed': 'was stopped'}[self.status]
        data = self.output_data()
        content = '<task-notification>\n' + '\n'.join(
            f'<{key}>{escape(str(value))}</{key}>' for key, value in (
                ('task-id', self.id), ('output-file', data['outputFile'] or ''),
                ('status', self.status), ('summary', summary), ('trust', 'untrusted-data'))) + '\n</task-notification>'
        def ack(): self.notified = True
        session.moments.send_message(
            {'customType': 'task-notification', 'content': content, 'display': True, 'details': data},
            {'deliverAs': 'followUp', 'triggerTurn': True, '_deliveryId': 'shell:' + self.notification_id,
             '_onPersist': ack})

    async def stop(self) -> dict:
        if self.status != 'running':
            raise ValueError(f'Task {self.id} is not running (status: {self.status})')
        # Source stopTask suppresses shell-kill noise, not agent partial results.
        self.notified = True
        self.controller.abort()
        if self.runner is not None:
            _, cancelled = await settle(asyncio.gather(self.runner, return_exceptions=True))
            if cancelled is not None:
                raise cancelled
        return {'message': f'Successfully stopped task: {self.id} ({self.command})',
                'task_id': self.id, 'task_type': 'local_bash', 'command': self.command}


def background_result(task, *, by_user=False):
    """Source BashTool background result, shared by explicit and handed-off jobs."""
    from misaka.agent.types import AgentToolResult
    from misaka.ai.types import TextContent

    path = task.output.tempFilePath
    text = f"Command running in background with ID: {task.id}. Output is being written to: {path}"
    return AgentToolResult(content=[TextContent(text=text)], details={
        'backgroundTaskId': task.id, 'task_id': task.id, 'task_type': 'local_bash',
        'status': task.status, 'fullOutputPath': path,
        **({'backgroundedByUser': True} if by_user else {}),
    })

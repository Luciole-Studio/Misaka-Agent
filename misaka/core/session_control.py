"""Attach a terminal to the owning AgentSession, never a second session writer.

The catalog locates this local socket. Commands are fenced by the session and
runtime instance; disconnecting a client does not cancel an accepted input.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from misaka.utils.values import read_field


def for_session(session):
    return next((part.control for part in session.moments.parts
                 if getattr(part, "control", None) is not None), None)


async def wait_for_session(session):
    control = for_session(session) if session is not None else None
    if control is not None:
        await control.wait()


async def request(record, operation, **arguments):
    """One command/ack, without retries: an uncertain write must not be sent twice."""
    async with asyncio.timeout(10):
        reader, writer = await asyncio.open_unix_connection(record["control"], limit=1024 * 1024)
        try:
            writer.write((json.dumps({"session": record["id"], "instance": record["instance"],
                                      "operation": operation, **arguments}) + "\n").encode())
            await writer.drain()
            line = await reader.read()  # One reply then EOF; large saved conversations are not single-line input frames.
            if not line:
                raise ConnectionError("The original session disconnected; delivery is unconfirmed. Do not resend automatically.")
            reply = json.loads(line)
            if not reply["ok"]:
                raise ValueError(reply["error"])
            return reply["result"]
        finally:
            writer.close()
            await writer.wait_closed()


class SessionControl:
    def __init__(self, session, catalog):
        self.session, self.catalog = session, catalog
        self.server = self.directory = None
        self.path = None
        self.accepting = True
        self.paused = False
        self.error = ""
        self.inputs = set()
        self.connections = set()
        self.ingress = asyncio.Lock()
        self.on_input = None
        self.describe = dict
        self.check_active = lambda: None
        self._stream_revision = 0
        self._unsubscribe_stream = None

    def _stream_event(self, event):
        if read_field(event, "type") in {"message_start", "message_update", "message_end", "agent_end"}:
            self._stream_revision += 1

    async def start(self):
        if self.server is not None:
            return
        self.accepting, self.paused = True, False
        # macOS sockaddr_un is only 104 bytes; its default temp directory can
        # consume most of that. A private, short directory also protects the socket.
        self.directory = tempfile.TemporaryDirectory(prefix="misaka-session-", dir="/tmp")
        self.path = str(Path(self.directory.name) / "control.sock")
        self.server = await asyncio.start_unix_server(self._serve, self.path, limit=1024 * 1024)
        os.chmod(self.path, 0o600)
        self._unsubscribe_stream = self.session.subscribe(self._stream_event)

    def close(self):
        self.accepting = False
        if self._unsubscribe_stream is not None:
            self._unsubscribe_stream()
            self._unsubscribe_stream = None
        if self.server is not None:
            self.server.close()
            self.server = None
        for task in (*self.connections, *self.inputs):
            task.cancel()
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None

    async def wait(self):
        """Pause at a request/tool/workflow boundary, not halfway through a write.

        An in-flight request/tool may finish. Abort and shutdown still release
        the boundary; pausing never holds a database transaction or freezes a PID.
        """
        while self.paused and self.accepting:
            self.check_active()
            signal = self.session.agent.signal
            if signal is not None and signal.aborted:
                return
            await asyncio.sleep(.1)

    def _owns(self, command):
        from misaka.core.session_catalog import _object

        record = _object(self.catalog.record or "")
        return (command.get("session") == self.session.sessionId
                and command.get("instance") == self.catalog.instance
                and record.get("instance") == self.catalog.instance
                and record.get("id") == self.session.sessionId)

    def _check_input_owner(self):
        self.check_active()
        if self.catalog.spec is not None and self.catalog.spec.task_id:
            from misaka.core.network.todo import TodoPart

            card = next((part for part in self.session.moments.parts if isinstance(part, TodoPart)
                         and part.task_id == self.catalog.spec.task_id), None)
            if card is None or card._owned_row() is None:
                raise ValueError("This card execution no longer holds its original claim; open the current owner from Sessions.")

    async def _serve(self, reader, writer):
        from misaka.core.session_manager import _dump_json

        task = asyncio.current_task()
        self.connections.add(task)
        try:
            try:
                async with asyncio.timeout(10):
                    command = json.loads(await reader.readline())
                    if not isinstance(command, dict) or not self._owns(command):
                        raise ValueError("The original session changed owners or ended; reopen it from Sessions.")
                    self.check_active()
                    result = await self._execute(command)
                reply = {"ok": True, "result": result}
            except Exception as error:  # noqa: BLE001 - send a command error, not a silent dropped input
                reply = {"ok": False, "error": str(error) or "Delivery unconfirmed: owner timed out; do not resend automatically."}
            writer.write((_dump_json(reply) + "\n").encode())
            await writer.drain()
        except (ConnectionError, OSError):
            pass  # The client detached. Accepted input belongs to the owner, not this connection.
        finally:
            self.connections.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _execute(self, command):
        operation = command.get("operation")
        if operation == "snapshot":
            manager = self.session.sessionManager
            cursor = [manager.getLeafId(), len(manager.getEntries())]
            streaming = self.session.state.streamingMessage
            if read_field(streaming, "role") != "assistant":
                streaming = None
            # Keep native in-flight content separate from committed history. A history
            # change also resends the preview after the view rebuilds its branch.
            stream_cursor = [self.session.sessionId, *cursor, self._stream_revision, streaming is not None]
            return {"id": self.session.sessionId, "cwd": manager.getCwd(),
                    "state": "idle" if self.session.isIdle else "working", "paused": self.paused,
                    "error": self.error, "workflow": self.describe(), "cursor": cursor,
                    "steering": self.session.getSteeringMessages(), "follow_up": self.session.getFollowUpMessages(),
                    "stream_cursor": stream_cursor,
                    "streaming": streaming if command.get("stream_cursor") != stream_cursor else None,
                    "entries": manager.buildContextEntries() if command.get("cursor") != cursor else None}
        if not self.accepting:
            raise ValueError("The original owner is finishing; no further input is accepted.")
        self._check_input_owner()
        if operation in {"pause", "resume"}:
            self.paused = operation == "pause"
            self.catalog.refresh()
            return "Pause requested: current request/tool may finish; new work waits." if self.paused else "Resumed."
        if operation != "input":
            raise ValueError(f"Unknown session operation: {operation}")
        text = command.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Enter a message for this session.")
        # Serialize only ingress, not turns. A second message can steer the first
        # while it is running, using AgentSession's normal input/queue contract.
        async with self.ingress:
            if not self.accepting or not self._owns(command):
                raise ValueError("The original session ended before accepting this input.")
            self._check_input_owner()
            accepted = asyncio.get_running_loop().create_future()
            accepted.add_done_callback(lambda result: result.exception() if not result.cancelled() else None)

            def preflight(ok):
                if not accepted.done():
                    accepted.set_result(ok)

            async def deliver():
                try:
                    if self.on_input is not None:
                        await self.on_input(text, preflight)
                    else:
                        await self.session.prompt(text, {"streamingBehavior": "steer", "preflightResult": preflight})
                except Exception as error:  # noqa: BLE001 - failed background turns must remain visible
                    self.error = str(error)
                    if not accepted.done():
                        accepted.set_exception(error)
                finally:
                    preflight(False)

            self.error = ""
            task = asyncio.create_task(deliver())
            self.inputs.add(task)
            task.add_done_callback(self.inputs.discard)
            if not await asyncio.shield(accepted):
                raise ValueError(self.error or "The session did not accept the input.")
        return "Delivered to the original session; its reply appears below once its turn runs."

"""Local metadata about execution. Session files and the Board still own their state."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from hashlib import sha256
from uuid import uuid4

from misaka.ai.runtime_observation import observation_context, observation_sink
from misaka.config import home
from misaka.core.session_manager import assertValidSessionId
from misaka.utils.values import read_field

_LOG = logging.getLogger(__name__)
_FILE_ID = uuid4().hex
_LEGACY_DIRECTORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_WINDOWS_DEVICE = re.compile(r"con|prn|aux|nul|(?:com|lpt)[0-9]", re.IGNORECASE)
_LABEL = re.compile(r"[A-Za-z0-9_.:/|-]{1,160}\Z")
_INCOMPLETE: set[tuple[str, str]] = set()
_GROUPED_FIELDS = {
    "usage", "raw", "normalized", "issues", "present", "counts",
}
BUDGET_TOKEN_FIELDS = (
    "budget_input_bound", "budget_minimum_output", "budget_thinking_allowance", "budget_need", "budget_desired",
    "budget_granted", "budget_cap", "budget_used", "budget_remaining", "budget_held",
)
_FIELDS = _GROUPED_FIELDS | set(BUDGET_TOKEN_FIELDS) | {
    "execution_id", "session_id", "turn", "request_id", "attempt", "status_code", "status",
    "stop_reason", "reason", "provider", "model", "duration_ms", "call_id", "entry_id", "tool",
    "read_only", "nested", "count", "retry_number", "delay_ms", "success", "card_id", "node_id",
    "generation", "card_attempt", "budget_scope", "budget_leased", "http_attempts", "auxiliary",
    "retry_source", "failure_count",
}


def _valid_session_id(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        assertValidSessionId(value)
    except ValueError:
        return False
    return True


def valid_id(value: str | None) -> bool:
    return isinstance(value, str) and (_LEGACY_DIRECTORY.fullmatch(value) is not None or _valid_session_id(value))


def trace_folders(run_id: str) -> tuple[str, ...]:
    """Preferred portable directory, then any spelling used by the first trace writer."""
    legacy = bool(_LEGACY_DIRECTORY.fullmatch(run_id))
    reserved = _WINDOWS_DEVICE.fullmatch(run_id) is not None
    if legacy and not reserved and run_id == run_id.lower():
        return (run_id,)
    # Prefix is outside the session-ID namespace. Lowercase direct names and hashed
    # alternatives also distinguish IDs that differ only by case on Windows.
    mapped = "_" + sha256(run_id.encode("ascii")).hexdigest()
    return (mapped, run_id) if legacy and not reserved else (mapped,)


_STRING_FIELDS = {"session_id", "execution_id", "request_id", "call_id", "entry_id", "card_id", "node_id",
                  "mode", "segment", "tool", "status", "stop_reason", "reason", "provider", "model", "retry_source",
                  "budget_scope"}
_INTEGER_FIELDS = {"turn", "attempt", "status_code", "count", "retry_number", "failure_count", "generation",
                   "card_attempt", "http_attempts", *BUDGET_TOKEN_FIELDS}
_BOOLEAN_FIELDS = {"success", "read_only", "nested", "auxiliary", "budget_leased"}
_MAPPING_FIELDS = {"raw", "normalized", "usage", "counts"}
_LIST_FIELDS = {"issues", "present"}

def within_trace_dir(path, parent):
    try:
        return not path.is_symlink() and path.resolve().parent == parent.resolve()
    except (OSError, RuntimeError):
        return False


def _finite_number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _valid_row(row):
    if (not isinstance(row, dict) or type(row.get("version")) is not int or row["version"] != 1
            or not valid_id(row.get("run_id")) or type(row.get("at_ns")) is not int or row["at_ns"] < 0
            or not isinstance(row.get("event"), str)):
        return False
    for field, value in row.items():
        if value is None:
            continue
        if field in _STRING_FIELDS and not isinstance(value, str):
            return False
        if field in _INTEGER_FIELDS and (type(value) is not int or value < 0):
            return False
        if field in _BOOLEAN_FIELDS and type(value) is not bool:
            return False
        if field in _MAPPING_FIELDS and (not isinstance(value, dict) or not all(_finite_number(v) for v in value.values())):
            return False
        if field in _LIST_FIELDS and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
            return False
        if field in {"duration_ms", "delay_ms"} and (not _finite_number(value) or value < 0):
            return False
    return True


def trace_row(line):
    if not line.endswith(b"\n"):
        return None
    try:
        row = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return row if _valid_row(row) else None


def trace_children(folder):
    """Read a trace directory and retain any names found before a read error."""
    children = []
    try:
        with os.scandir(folder) as entries:
            children.extend(folder / entry.name for entry in entries)
    except FileNotFoundError:
        return children, bool(children)
    except OSError:
        return children, True
    return children, False


def read_trace(run_id: str, *, kinds: frozenset[str] | None = None):
    """Return validated trace rows and whether any part of the recording is incomplete."""
    if not valid_id(run_id):
        raise ValueError("Invalid run or session ID")
    root = home.path("runtime_traces")
    events = []
    incomplete = False
    for name in trace_folders(run_id):
        folder = root / name
        if not within_trace_dir(folder, root):
            incomplete = True
            continue
        children, scan_incomplete = trace_children(folder)
        incomplete |= scan_incomplete
        for segment in sorted(path for path in children if path.match("*.jsonl")):
            try:
                if not within_trace_dir(segment, folder):
                    incomplete = True
                    continue
                ordered_at = 0
                with segment.open("rb") as stream:
                    for line in stream:
                        row = trace_row(line)
                        if row is None:
                            incomplete = True
                            continue
                        if row["run_id"] != run_id:
                            # Old Windows directory names could collide for case-variant IDs.
                            incomplete |= row["run_id"].lower() != run_id.lower()
                            continue
                        ordered_at = max(ordered_at, row["at_ns"])
                        if kinds is None or row["event"] in kinds:
                            events.append((ordered_at, row))
                        incomplete |= row["event"] == "recording_gap"
            except OSError:
                incomplete = True
    events.sort(key=lambda item: item[0])
    return [row for _, row in events], incomplete



def _label(value):
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else None


@dataclass(frozen=True, slots=True)
class Scope:
    run_id: str | None
    diagnostics: bool
    card_id: str | None = None
    generation: int | None = None
    attempt: int | None = None


_scope: ContextVar[Scope | None] = ContextVar("misaka_trace_scope", default=None)


def current_scope(session_id: str | None = None) -> Scope | None:
    if active := _scope.get():
        if active.run_id is not None:
            return active
        return replace(active, run_id=session_id) if session_id else None
    run_id = os.getenv("MISAKA_TRACE_RUN_ID") or session_id
    if run_id and valid_id(run_id):
        generation = os.getenv("MISAKA_TRACE_GENERATION", "")
        attempt = os.getenv("MISAKA_TRACE_CARD_ATTEMPT", "")
        return Scope(run_id, os.getenv("MISAKA_TRACE_DIAGNOSTICS") == "1", os.getenv("MISAKA_TRACE_CARD_ID"),
                     int(generation) if generation.isdecimal() else None, int(attempt) if attempt.isdecimal() else None)
    return None


def trace_environment() -> dict[str, str]:
    if not (scope := current_scope()):
        return {}
    env = {"MISAKA_TRACE_RUN_ID": scope.run_id, "MISAKA_TRACE_DIAGNOSTICS": "1" if scope.diagnostics else "0"}
    for key, value in (("MISAKA_TRACE_CARD_ID", scope.card_id), ("MISAKA_TRACE_GENERATION", scope.generation),
                       ("MISAKA_TRACE_CARD_ATTEMPT", scope.attempt)):
        if value is not None:
            env[key] = str(value)
    return env


@contextmanager
def trace_scope(run_id: str | None, diagnostics: bool, *, card_id=None, generation=None, attempt=None):
    scope = Scope(run_id, diagnostics, card_id, generation, attempt)
    token = _scope.set(scope)
    try:
        if run_id is None:
            yield
        else:
            recorder = SessionTrace(None, scope=scope)
            with observation_sink(recorder.observe), observation_context(request_id=None):
                yield
    finally:
        _scope.reset(token)


def _clean(value):
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str):
        return _label(value)
    if isinstance(value, (list, tuple)):
        return [item for member in value if (item := _label(member)) is not None]
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if _label(key) and type(item) in (bool, int, float)}
    return None


def _call_id(value):
    if value is None or _label(value) is not None:
        return value
    if isinstance(value, str):
        return "~" + sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()
    return None


def _append(scope: Scope, kind: str, fields: dict, *, sync: bool = False) -> None:
    if not valid_id(scope.run_id):
        return
    root = home.path("runtime_traces")
    key = str(root), scope.run_id
    segment = f"{os.getpid()}-{_FILE_ID}"
    row = {"version": 1, "run_id": scope.run_id, "mode": "diagnostic" if scope.diagnostics else "default",
           "at_ns": time.time_ns(), "segment": segment, "event": kind}
    for name in ("card_id", "generation", "attempt"):
        if (value := getattr(scope, name)) is not None:
            row["card_attempt" if name == "attempt" else name] = _clean(value)
    for name, value in fields.items():
        if name not in _FIELDS or (name not in _GROUPED_FIELDS and isinstance(value, (dict, list, tuple))):
            continue
        if name == "session_id":
            row[name] = value if _valid_session_id(value) else None
        elif name == "call_id":
            row[name] = _call_id(value)
        else:
            row[name] = _clean(value)
    try:
        folder = root / trace_folders(scope.run_id)[0]
        if folder.is_symlink() or folder.resolve().parent != root.resolve():
            raise OSError("trace directory is outside the trace root")
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = folder / f"{segment}.jsonl"
        if path.is_symlink():
            raise OSError("trace segment is a symbolic link")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as stream:
            if key in _INCOMPLETE:
                stream.write(json.dumps({**row, "event": "recording_gap"}) + "\n")
            stream.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n")
            if sync:
                stream.flush()
                os.fsync(stream.fileno())
        _INCOMPLETE.discard(key)
    except OSError as error:
        if key not in _INCOMPLETE:
            _LOG.warning("Runtime trace for %s is incomplete (%s)", scope.run_id, type(error).__name__)
        _INCOMPLETE.add(key)


def scope_event(kind: str, **metadata) -> None:
    if scope := current_scope():
        _append(scope, kind, metadata)


class SessionTrace:
    def __init__(self, session_id: str | None, *, scope: Scope | None = None):
        self.scope = scope or current_scope(session_id) or Scope(session_id, False)
        self.session_id = session_id
        self.execution_id = uuid4().hex
        self.turn = 0
        self.request_id = None
        self.http_attempts: dict[str, int] = {}
        self.outcome = "unknown"
        self.failure_reason: str | None = None
        self._agent_failure: str | None = None
        self.reads: dict[str, tuple[int, int]] = {}
        self.tools: dict[str, dict] = {}
        if session_id is not None:
            self.write("run_started")

    def write(self, kind: str, *, sync=False, **fields):
        _append(self.scope, kind, {"session_id": self.session_id, "execution_id": self.execution_id,
                                  "turn": self.turn, **fields}, sync=sync)

    def observe(self, kind: str, fields: dict) -> None:
        if kind == "model_started":
            if not fields.get("auxiliary"):
                self.request_id = fields.get("request_id")
                self._agent_failure = None
        elif kind == "model_finished":
            if not fields.get("auxiliary"):
                self.outcome = fields.get("status", "unknown")
            count = self.http_attempts.pop(fields["request_id"], None)
            fields = {**fields, "http_attempts": count}
        elif kind == "http_observable":
            if request_id := fields.get("request_id"):
                self.http_attempts.setdefault(request_id, 0)
            if not self.scope.diagnostics:
                return
        elif kind == "http_attempt":
            request_id = fields.get("request_id")
            if request_id:
                self.http_attempts[request_id] = self.http_attempts.get(request_id, 0) + 1
            if not self.scope.diagnostics:
                return
        elif kind == "http_response" and not self.scope.diagnostics:
            if (fields.get("status_code") or 0) < 400:
                return
        elif kind == "tool_entered":
            call_id = fields["call_id"]
            self.tools[call_id] = {**fields, "request_id": self.request_id}
            if self.scope.diagnostics or not fields["read_only"]:
                self.write(kind, sync=not fields["read_only"], **self.tools[call_id])
            return
        elif kind == "tool_returned":
            pending = self.tools.get(fields["call_id"])
            if pending is None:
                return
            fields = {**fields, "request_id": pending.get("request_id")}
            if not pending["read_only"] or self.scope.diagnostics:
                self.write(kind, **{**pending, **fields})
            pending.update(fields)
            return
        elif kind == "tool_finished":
            self.finish_tool(fields["call_id"], bool(fields.get("is_error")))
            return
        elif kind not in {"provider_usage", "http_response", "budget_refused", "budget_admitted",
                         "budget_wait", "budget_aborted", "budget_unavailable", "tool_rejected",
                         "tool_timeout", "model_timeout", "provider_retry", "provider_retry_stopped", "circuit_opened",
                         "circuit_rejected", "circuit_probe", "circuit_probe_finished"}:
            return
        if kind == "tool_rejected":
            fields = {**fields, "request_id": fields.get("request_id") or self.request_id}
            if not fields.get("nested"):
                self.tools[fields["call_id"]] = fields
        if kind == "tool_timeout" and (pending := self.tools.get(fields.get("call_id"))):
            fields = {**pending, **fields, "request_id": pending.get("request_id")}
        self.write(kind, **fields)

    def finish_tool(self, call_id: str, failed: bool):
        pending = self.tools.get(call_id)
        if pending is None:
            return
        if pending.get("read_only") and not self.scope.diagnostics:
            if failed or pending.get("status") != "ok":
                status = "error" if failed and pending.get("status") == "ok" else pending.get("status")
                self.write("tool_failed", **{**pending, "status": status})
            else:
                tool = pending["tool"]
                count, duration = self.reads.get(tool, (0, 0))
                self.reads[tool] = (count + 1, duration + pending.get("duration_ms", 0))
                self.tools.pop(call_id)
        if pending.get("nested"):
            self.tools.pop(call_id, None)

    def receipt(self, message, entry_id: str | None):
        if read_field(message, "role") == "assistant":
            if request_id := read_field(message, "requestId"):
                self.write("model_receipt_saved", request_id=request_id, entry_id=entry_id)
        elif read_field(message, "role") == "toolResult":
            call_id = read_field(message, "toolCallId")
            pending = self.tools.pop(call_id, None)
            if pending is not None or self.scope.diagnostics:
                self.write("tool_receipt_saved", call_id=call_id, entry_id=entry_id,
                           status="error" if read_field(message, "isError") else "ok",
                           request_id=(pending or {}).get("request_id", self.request_id))

    def event(self, event):
        kind = read_field(event, "type")
        if kind == "turn_start":
            self.turn += 1
        elif kind == "tool_execution_end" and read_field(event, "parentToolCallId") is None:
            self.finish_tool(read_field(event, "toolCallId"), read_field(event, "isError"))
        elif kind == "turn_end" and self.reads:
            for tool, (count, duration) in self.reads.items():
                self.write("tool_summary", tool=tool, count=count, duration_ms=duration, request_id=self.request_id)
            self.reads.clear()
        elif kind in {"auto_retry_start", "auto_retry_end"}:
            self.write(kind, request_id=self.request_id, retry_number=read_field(event, "attempt"),
                       delay_ms=read_field(event, "delayMs"), retry_source=read_field(event, "retrySource"),
                       success=read_field(event, "success"))
        elif kind == "agent_end":
            last = next((message for message in reversed(read_field(event, "messages", []) or [])
                         if read_field(message, "role") == "assistant"), None)
            stop = read_field(last, "stopReason")
            # Agent emits its own failure message when the run fails outside a model response.
            if stop in {"error", "aborted"} and not read_field(last, "requestId"):
                self._agent_failure = stop
        elif kind == "agent_settled":
            status, reason = self.outcome, self.failure_reason
            if read_field(event, "aborted") or self._agent_failure == "aborted":
                status = "aborted"
            elif self._agent_failure == "error" and reason is None and status not in {"aborted", "error", "timeout"}:
                status, reason = "error", "run_error"
            self.write("run_finished", status=status, reason=reason)

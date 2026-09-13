"""Opt-in, bounded operational records; never another copy of research material."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import stat
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

from misaka.config import get_agent_dir
from misaka.core.web import config
from misaka.core.web.scope import current_scope
from misaka.utils.async_lifecycle import settle_thread_call
from misaka.utils.atomic import write_bytes
from misaka.utils.values import read_field

logger = logging.getLogger(__name__)
MAX_EVENTS = 128
MAX_BYTES = 65_536
MAX_FILES = 128
RETENTION_SECONDS = 7 * 24 * 60 * 60
_FILENAME = re.compile(r"web-debug-[0-9a-f]{32}\.json")
_current: ContextVar[dict | None] = ContextVar("web_debug_call", default=None)


def enabled():
    environment = current_scope().environment
    raw = (os.environ if environment is None else environment).get("WEB_TOOLS_DEBUG")
    if raw is not None:
        return config._as_bool("WEB_TOOLS_DEBUG", raw)
    value = config.web_config(strict=True).get("debug_enabled", False)
    if not isinstance(value, bool):
        raise ValueError("debug_enabled must be true or false")  # noqa: TRY004 - invalid config document
    return value


def directory():
    profile = current_scope().profile_dir
    return Path(get_agent_dir() if profile is None else profile) / "logs" / "web"


def active():
    return _current.get() is not None


def trace_id():
    trace = _current.get()
    return trace["trace_id"] if trace is not None else None


def _fingerprint(text):
    # Same digest/encoding as the existing external-call ledger. No query/URL copy.
    return {"chars": len(text), "sha256": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]}


def _parameter(value, depth=0):
    if isinstance(value, str):
        return _fingerprint(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and -1e12 <= value <= 1e12 and math.isfinite(value):
        return value
    if isinstance(value, list):
        return {"count": len(value), "items": [_parameter(item, depth + 1) for item in value[:5]] if depth == 0 else []}
    return {"type": type(value).__name__[:80]}


def _label(value):
    try:
        return config.redact_secrets(str(value))[:128] if value is not None else None
    except Exception:  # noqa: BLE001 - broken plugin metadata must not break diagnostics or reveal secrets
        return "[unavailable]"


def event(kind, *, subject=None, **facts):
    trace = _current.get()
    if trace is None:
        return
    if len(trace["events"]) >= MAX_EVENTS:
        trace["events_dropped"] += 1
        return
    try:
        row = {"kind": kind, **{key: _label(value) if isinstance(value, str) else value for key, value in facts.items()}}
        if subject is not None:
            row["subject"] = _fingerprint(subject)
        trace["events"].append(row)
    except Exception:  # noqa: BLE001 - diagnostics do not change the attempted operation
        trace["events_dropped"] += 1


def metrics(**facts):
    trace = _current.get()
    if trace is not None:
        trace["metrics"].update(facts)


def original_json(value):
    if not active():
        return
    try:
        metrics(original_response_chars=len(json.dumps(value)))
    except Exception:  # noqa: BLE001 - provider metadata is not a diagnostic admission gate
        # A provider's unused metadata need not be JSON-serializable for the actual
        # tool renderer to succeed. Do not introduce a new content admission gate.
        metrics(original_json_serializable=False)


def result(value):
    if not active():
        return
    try:
        content = read_field(value, "content", [])
        if isinstance(content, list):
            metrics(result_content_chars=sum(len(text) for block in content
                                            if isinstance(text := read_field(block, "text"), str)))
        details = read_field(value, "details", {})
        for key in ("refused", "skipped", "truncated", "status", "bytes"):
            fact = read_field(details, key)
            if isinstance(fact, (bool, int)):
                metrics(**{key: fact})
    except Exception:  # noqa: BLE001 - diagnostics must preserve even unconventional plugin results
        metrics(result_inspection_failed=True)


def result_json(value):
    """Read only the tool's JSON envelope, not the meaning of returned prose."""
    if not active():
        return
    metrics(final_response_chars=len(value))
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        metrics(json_envelope=False)
        return
    if not isinstance(result, dict):
        return
    metrics(reported_errors=int(bool(result.get("error")) or result.get("success") is False))
    data = result.get("data")
    if isinstance(data, dict) and isinstance(data.get("web"), list):
        metrics(results_count=len(data["web"]))
    pages = result.get("results")
    if isinstance(pages, list):
        metrics(pages_extracted=len(pages), reported_errors=sum(
            bool(page.get("error")) for page in pages if isinstance(page, dict)))


@contextmanager
def attempt(service, backend, subject, unit):
    trace = _current.get()
    if trace is None:
        yield None
        return
    trace["attempt_count"] += 1
    number, start = trace["attempt_count"], time.monotonic()
    outcome, error_type = "completed", None
    try:
        yield {"web_trace_id": trace["trace_id"], "web_attempt": number}
    except BaseException as error:
        outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
        error_type = type(error).__name__
        raise
    finally:
        event("attempt", subject=subject, attempt=number, backend=backend, service=service,
              unit=unit, outcome=outcome, error_type=error_type,
              duration_ms=round((time.monotonic() - start) * 1000, 3))


def _save(root, trace):
    """One owned write; diagnostics never change the tool's result or retry policy."""
    phase = "write"
    try:
        if root.is_symlink():
            raise OSError("Debug directory is a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        payload = json.dumps(trace, ensure_ascii=True, allow_nan=False, indent=2).encode()
        while len(payload) > MAX_BYTES and trace["events"]:
            dropped = max(1, len(trace["events"]) // 2)
            del trace["events"][-dropped:]
            trace["events_dropped"] += dropped
            payload = json.dumps(trace, ensure_ascii=True, allow_nan=False, indent=2).encode()
        if len(payload) > MAX_BYTES:
            raise ValueError("Debug metadata exceeds its file limit")
        with FileLock(root / ".lock", timeout=1, mode=0o600):
            target = root / f"web-debug-{trace['trace_id']}.json"
            write_bytes(target, payload, mode=0o600)
            phase = "retention"
            # Dedicated bounded directory. Never prune unrelated files or follow links.
            previous = []
            cutoff = time.time() - RETENTION_SECONDS
            for path in root.glob("web-debug-*.json"):
                if path == target:
                    continue
                info = path.lstat()
                if not _FILENAME.fullmatch(path.name) or not stat.S_ISREG(info.st_mode):
                    continue
                if info.st_mtime < cutoff:
                    path.unlink()
                else:
                    previous.append((info.st_mtime, path))
            for _mtime, path in sorted(previous, reverse=True)[MAX_FILES - 1:]:
                path.unlink()
    except Exception as error:  # noqa: BLE001 - observability must not turn success into a paid retry
        logger.warning("Web debug %s failed (%s)", phase, type(error).__name__)


def _start(owner, name, execute, args, kwargs, tool_call):
    try:
        if not enabled():
            return None
        call_id, parameters, session_id, metadata_error = None, {}, None, None
        if tool_call:
            try:
                bound = inspect.signature(execute).bind(*args, **kwargs)
                bound.apply_defaults()
                values = list(bound.arguments.values())
                call_id, raw = values[:2]
                for key in ("query", "url", "urls", "filename", "limit", "char_limit", "format"):
                    value = read_field(raw, key)
                    if value is not None or isinstance(raw, dict) and key in raw:
                        parameters[key] = _parameter(value)
                context = values[4] if len(values) > 4 else None
                manager = read_field(context, "sessionManager")
                if manager is not None:
                    session_id = manager.getSessionId()
            except Exception as error:  # noqa: BLE001 - retain the trace even when optional metadata fails
                metadata_error = type(error).__name__
        if owner.debug_id is None:
            owner.debug_id = uuid.uuid4().hex
        return directory(), {
            "version": 1, "trace_id": uuid.uuid4().hex, "owner_id": owner.debug_id,
            "session_id": _label(session_id), "tool_call_id": _label(call_id), "tool": _label(name),
            "started_at": datetime.now(UTC).isoformat(), "parameters": parameters,
            "metadata_error_type": _label(metadata_error),
            "execution_outcome": "returned", "attempt_count": 0, "events_dropped": 0,
            "events": [], "metrics": {},
        }
    except Exception as error:  # noqa: BLE001 - malformed diagnostics must not prevent execution
        logger.warning("Web debug record was not started (%s)", type(error).__name__)
        return None


@asynccontextmanager
async def call(owner, name, execute, args, kwargs, *, tool_call=False):
    started = _start(owner, name, execute, args, kwargs, tool_call)
    trace = None if started is None else started[1]
    token = _current.set(trace)  # An untraced nested owner must not charge its parent trace.
    begin, primary = time.monotonic(), None
    try:
        yield
    except BaseException as error:
        primary = error
        if trace is not None:
            trace["execution_outcome"] = (
                "cancelled" if isinstance(error, asyncio.CancelledError) else
                "timeout" if isinstance(error, TimeoutError) else "error")
            trace["error_type"] = _label(type(error).__name__)
        raise
    finally:
        _current.reset(token)
        if trace is not None:
            trace["duration_ms"] = round((time.monotonic() - begin) * 1000, 3)
            # Execution outcome is not a delivery receipt: cancellation during this
            # awaited flush can still prevent the caller from receiving the result.
            try:
                _, cancelled = await settle_thread_call(_save, started[0], trace)
            except Exception as error:  # noqa: BLE001 - preserve outcome if the executor is unavailable
                logger.warning("Web debug flush failed (%s)", type(error).__name__)
            else:
                if cancelled is not None and primary is None:
                    raise cancelled

"""Log messages MCP servers send with ``notifications/message``, appended to ``mcp.log``. Several misaka
processes may write to the same file, so every message is one synchronous append. The file is rotated to
``mcp.log.1`` once it grows past ``MAX_LOG_BYTES``.

MISAKA fork: the home keeps ``mcp.log`` with its other logs, where pi keeps it in the agent directory.

Translated from pi's ``coding-agent/src/extensions/mcp/log.ts``.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

from misaka.utils.js import UNDEFINED, jsonStringify, jsString, toWellFormed

MAX_LOG_BYTES = 5 * 1024 * 1024


def isRecord(value: Any) -> bool:
    return isinstance(value, dict)


def formatData(data: Any) -> str:
    if isinstance(data, str):
        return data
    try:
        # `JSON.stringify(undefined)` is undefined, which falls back to `String(data)`.
        return jsonStringify(data) if data is not UNDEFINED else jsString(data)
    except Exception:  # noqa: BLE001 - upstream `catch`: unserialisable data is shown as `String(data)`
        return jsString(data)


def _toISOString(now: datetime) -> str:
    # MISAKA fork: `Date.prototype.toISOString`.
    utc = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{utc.microsecond // 1000:03d}Z"


def formatMcpLogMessage(server: str, params: Any, now: datetime | None = None) -> str:
    """Format one ``notifications/message`` from ``server`` as a log line; continuation lines are indented."""
    if now is None:
        now = datetime.now(UTC)
    message = params if isRecord(params) else {"data": params}
    level = message["level"] if isinstance(message.get("level"), str) else "info"
    logger = f" {message['logger']}:" if isinstance(message.get("logger"), str) and message["logger"] else ""
    text = re.sub(r"\r?\n", "\n    ", formatData(message.get("data", UNDEFINED)))
    return f"{_toISOString(now)} [{server}] {level}{logger} {text}\n"


class McpServerLog:
    """Appends server log messages to one file. Write errors are ignored: logging must not break tools."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._size: int | None = None

    def write(self, server: str, params: Any) -> None:
        line = formatMcpLogMessage(server, params)
        try:
            if self._size is None:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                self._size = self._currentSize()
            if self._size > MAX_LOG_BYTES:
                # Another process may have rotated it already; check before renaming.
                if self._currentSize() > MAX_LOG_BYTES:
                    os.replace(self.path, f"{self.path}.1")
                self._size = self._currentSize()
            # MISAKA fork: `appendFileSync` writes a string as UTF-8, a lone surrogate as U+FFFD.
            data = toWellFormed(line).encode("utf-8")
            if sys.platform == "win32":
                from misaka.utils import libuv_windows

                # libuv opens an append on Windows with append-only access, shared for reading, writing and
                # deleting: appends from several processes land whole, and another one can rotate the file.
                libuv_windows.appendFile(self.path, data)
            else:
                with open(self.path, "ab") as appended:
                    appended.write(data)
            self._size += len(data)
        except Exception:  # noqa: BLE001, S110 - upstream `catch {}`
            # Ignore: the log is best effort.
            pass

    def _currentSize(self) -> int:
        try:
            return os.stat(self.path).st_size
        except Exception:  # noqa: BLE001 - upstream `catch`
            return 0


__all__ = ["McpServerLog", "formatMcpLogMessage"]

"""``listFiles`` through a pi-env daemon, for remote environments and for ``LocalExecutionEnv``.

Translated from pi's ``packages/env/src/list-files.ts`` (v1.3.0). Results are the ``Result`` dicts of
``misaka.durable.env``.
"""

from __future__ import annotations

from typing import Any

from misaka.chord.context import Context
from misaka.durable.env import FileError, err, ok
from misaka.durable.env.node import NodeExecutionEnv
from misaka.utils.js import jsIsSafeInteger

from .connection import Connection
from .errors import abortResult, toFileError

KINDS = {102: "file", 100: "directory", 108: "symlink"}
"""Kind bytes of ``listFiles`` records: ``f``, ``d``, ``l``."""


async def listFilesOver(connection: Connection, path: str, options: dict[str, Any] | None, context: Context) -> dict[str, Any]:
    """``listFiles`` of an absolute path through the daemon: fails with ``not_supported`` if the daemon does not list
    files (a release before ``listFiles``), so callers fall back to their own walk."""
    options = options or {}
    for key in ("maxEntries", "maxBytes", "timeoutMs"):
        value = options.get(key)
        if value is not None and (not jsIsSafeInteger(value) or value < 0):
            return err(FileError("invalid", f"{key} must be a non-negative safe integer", path))
    aborted = abortResult(context.abortSignal, path)
    if aborted:
        return aborted
    try:
        if "listFiles" not in ((await connection.info()).get("features") or []):
            return err(FileError("not_supported", "The pi-env daemon does not list files; update it", path))
        # Fields left undefined are not sent; the daemon applies the defaults.
        request = {
            "path": path,
            **{
                key: options[key]
                for key in ("hidden", "gitignore", "exclude", "maxEntries", "maxBytes", "timeoutMs")
                if options.get(key) is not None
            },
        }
        reply = await connection.request(
            "listFiles", request, {} if context.abortSignal is None else {"signal": context.abortSignal}
        )
        afterAbort = abortResult(context.abortSignal, path)
        if afterAbort:
            return afterAbort
        # Records of a kind byte, the UTF-8 path, and a NUL.
        entries: list[dict[str, Any]] = []
        data = reply["payload"]
        start = 0
        while start < len(data):
            end = data.find(0, start)
            if end == -1:
                break
            kind = KINDS.get(data[start])
            if kind is not None:
                entries.append({"path": data[start + 1 : end].decode("utf-8", "replace"), "kind": kind})
            start = end + 1
        return ok({"entries": entries, "truncated": reply["json"].get("truncated") is True})
    except Exception as error:  # noqa: BLE001 - failures are returned, not raised
        return err(toFileError(error, path))


class LocalExecutionEnv(NodeExecutionEnv):
    """``NodeExecutionEnv`` with ``listFiles`` through a pi-env daemon on the same machine, so local and remote
    environments list files with the same code. Everything else is ``NodeExecutionEnv``'s. The connection belongs to the
    caller, who closes it; several environments may share it. ``options`` (``LocalExecutionEnvOptions``) are
    ``NodeExecutionEnv``'s and ``connection``: a connection to this machine's daemon, ``Connection({"command":
    [sys.executable, localDaemon()]})``, for ``listFiles``."""

    def __init__(self, options: dict[str, Any]) -> None:
        node = {key: value for key, value in options.items() if key != "connection"}
        super().__init__(node)
        self._connection: Connection = options["connection"]

    async def listFiles(self, path: str, options: dict[str, Any] | None, context: Context) -> dict[str, Any]:
        resolved = await self.absolutePath(path, context)
        if not resolved["ok"]:
            return resolved  # type: ignore[no-any-return]
        return await listFilesOver(self._connection, resolved["value"], options, context)


LocalExecutionEnvOptions = dict[str, Any]
"""``NodeExecutionEnv``'s options and ``connection``."""

__all__ = ["KINDS", "LocalExecutionEnv", "LocalExecutionEnvOptions", "listFilesOver"]

"""JSONL-backed session repository implementation."""

from __future__ import annotations

import re
from typing import Any

from misaka.agent.harness.session.jsonl_storage import JsonlSessionStorage, load_jsonl_session_metadata
from misaka.agent.harness.session.repo_utils import (
    create_session_id,
    create_timestamp,
    get_entries_to_fork,
    get_file_system_result_or_throw,
    to_session,
)
from misaka.agent.harness.types import SessionError, to_error


def _encode_cwd(cwd: str) -> str:
    normalized = re.sub(r"^[/\\]+", "", cwd)
    return f"--{re.sub(r'[/\\\\:]', '-', normalized)}--"


class JsonlSessionRepo:
    def __init__(self, options: dict[str, Any]) -> None:
        self._fs = options["fs"]
        self._sessions_root_input = options["sessionsRoot"]
        self._sessions_root: str | None = None

    async def _get_sessions_root(self) -> str:
        if self._sessions_root is None:
            self._sessions_root = get_file_system_result_or_throw(
                await self._fs.absolutePath(self._sessions_root_input),
                f"Failed to resolve sessions root {self._sessions_root_input}",
            )
        return self._sessions_root

    async def _get_session_dir(self, cwd: str) -> str:
        return get_file_system_result_or_throw(
            await self._fs.joinPath([await self._get_sessions_root(), _encode_cwd(cwd)]),
            f"Failed to resolve session directory for {cwd}",
        )

    async def _create_session_file_path(self, cwd: str, session_id: str, timestamp: str) -> str:
        file_name = f"{re.sub(r'[:.]', '-', timestamp)}_{session_id}.jsonl"
        return get_file_system_result_or_throw(
            await self._fs.joinPath([await self._get_session_dir(cwd), file_name]),
            f"Failed to resolve session file path for {session_id}",
        )

    async def create(self, options: dict[str, Any]):
        session_id = options["id"] if "id" in options else create_session_id()
        created_at = create_timestamp()
        session_dir = await self._get_session_dir(options["cwd"])
        get_file_system_result_or_throw(
            await self._fs.createDir(session_dir, {"recursive": True}),
            f"Failed to create session directory {session_dir}",
        )
        file_path = await self._create_session_file_path(options["cwd"], session_id, created_at)
        storage = await JsonlSessionStorage.create(
            self._fs,
            file_path,
            {
                "cwd": options["cwd"],
                "sessionId": session_id,
                "parentSessionPath": options.get("parentSessionPath"),
            },
        )
        return to_session(storage)

    async def open(self, metadata: dict[str, Any]):
        exists = get_file_system_result_or_throw(
            await self._fs.exists(metadata["path"]),
            f"Failed to check session {metadata['path']}",
        )
        if not exists:
            raise SessionError("not_found", f"Session not found: {metadata['path']}")
        storage = await JsonlSessionStorage.open(self._fs, metadata["path"])
        return to_session(storage)

    async def list(self, options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        opts = dict(options or {})
        if "cwd" in opts and opts["cwd"]:
            dirs = [await self._get_session_dir(opts["cwd"])]
        else:
            dirs = await self._list_session_dirs()
        sessions: list[dict[str, Any]] = []
        for dir_path in dirs:
            exists = get_file_system_result_or_throw(
                await self._fs.exists(dir_path),
                f"Failed to check session directory {dir_path}",
            )
            if not exists:
                continue
            files = get_file_system_result_or_throw(
                await self._fs.listDir(dir_path),
                f"Failed to list sessions in {dir_path}",
            )
            session_files = [file for file in files if file.kind != "directory" and file.name.endswith(".jsonl")]
            for file in session_files:
                try:
                    sessions.append(await load_jsonl_session_metadata(self._fs, file.path))
                except Exception as error:
                    cause = to_error(error)
                    if not isinstance(cause, SessionError) or cause.code != "invalid_session":
                        raise cause from error
        sessions.sort(key=lambda metadata: metadata["createdAt"], reverse=True)
        return sessions

    async def delete(self, metadata: dict[str, Any]) -> None:
        get_file_system_result_or_throw(
            await self._fs.remove(metadata["path"], {"force": True}),
            f"Failed to delete session {metadata['path']}",
        )

    async def fork(self, sourceMetadata: dict[str, Any], options: dict[str, Any]):
        source = await self.open(sourceMetadata)
        forked_entries = await get_entries_to_fork(source.getStorage(), options)
        session_id = options["id"] if "id" in options else create_session_id()
        created_at = create_timestamp()
        session_dir = await self._get_session_dir(options["cwd"])
        get_file_system_result_or_throw(
            await self._fs.createDir(session_dir, {"recursive": True}),
            f"Failed to create session directory {session_dir}",
        )
        storage = await JsonlSessionStorage.create(
            self._fs,
            await self._create_session_file_path(options["cwd"], session_id, created_at),
            {
                "cwd": options["cwd"],
                "sessionId": session_id,
                "parentSessionPath": (
                    options["parentSessionPath"] if "parentSessionPath" in options else sourceMetadata["path"]
                ),
            },
        )
        for entry in forked_entries:
            await storage.appendEntry(entry)
        return to_session(storage)

    async def _list_session_dirs(self) -> list[str]:
        sessions_root = await self._get_sessions_root()
        exists = get_file_system_result_or_throw(
            await self._fs.exists(sessions_root),
            f"Failed to check sessions root {sessions_root}",
        )
        if not exists:
            return []
        entries = get_file_system_result_or_throw(
            await self._fs.listDir(sessions_root),
            f"Failed to list sessions root {sessions_root}",
        )
        return [entry.path for entry in entries if entry.kind == "directory"]


__all__ = ["JsonlSessionRepo"]

#!/usr/bin/env python3
"""Pre-create Hermes-native sidecars for historical textual tool results.

The SQLite database is always opened read-only. Dry-run is the default; pass
``--apply`` to create sidecars. Raw message rows and summary nodes are never
rewritten. Rollback also defaults to dry-run and deletes only manifest-owned,
digest-matching sidecars that no message or summary references.
"""

from __future__ import annotations

import argparse
from contextlib import closing  # misaka: U04 SQLite connection context commits, it does not close
import copy
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "hermes_lcm"
BACKFILL_OPERATION = "historical_tool_output_externalization"
BACKFILL_PROVENANCE_KEY = "historical_backfill_provenance"


from misaka.extensions.hermes_lcm.host.config_bridge import load_config  # misaka: native host configuration


from misaka.extensions.hermes_lcm.vendor.config import LCMConfig  # noqa: E402
from misaka.extensions.hermes_lcm.vendor.db_bootstrap import refuse_schema_version_too_new  # noqa: E402
from misaka.extensions.hermes_lcm.vendor.externalize import (  # noqa: E402
    _replace_externalized_payload,
    find_externalized_payload_for_message,
    get_large_output_storage_dir,
    is_externalized_placeholder,
)
from misaka.extensions.hermes_lcm.vendor.ingest_protection import (  # noqa: E402
    _contains_media_payload,
    extract_all_externalized_payload_refs,
    redact_sensitive_value,
    sensitive_pattern_status,
)
from misaka.extensions.hermes_lcm.vendor.tokens import count_tokens  # noqa: E402


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _path_digest(path: Path) -> str:
    return _sha256_bytes(os.fsencode(str(path)))


def _ownership_proof(*, manifest_id: str, ref: str, content_sha256: str) -> str:
    identity = json.dumps(
        {
            "operation": BACKFILL_OPERATION,
            "manifest_id": manifest_id,
            "ref": ref,
            "sha256": content_sha256,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return _sha256(identity)


def _is_hex_digest(value: Any, *, length: int) -> bool:
    text = str(value or "")
    return len(text) == length and all(character in "0123456789abcdef" for character in text)


def _is_safe_ref(ref: str) -> bool:
    return bool(ref) and Path(ref).name == ref and "/" not in ref and "\\" not in ref


def _validate_rollback_manifest(source: Any) -> str:
    if not isinstance(source, dict):
        raise ValueError("rollback requires a JSON object manifest")
    if type(source.get("schema_version")) is not int or source.get("schema_version") != 1:
        raise ValueError("rollback requires an applied schema-v1 backfill manifest")
    if source.get("applied") is not True:
        raise ValueError("rollback requires an applied schema-v1 backfill manifest")
    if source.get("operation") != BACKFILL_OPERATION:
        raise ValueError(f"rollback requires operation {BACKFILL_OPERATION!r}")
    manifest_id = str(source.get("manifest_id") or "")
    if not _is_hex_digest(manifest_id, length=32):
        raise ValueError("rollback manifest lacks valid backfill provenance")
    if source.get("state") != "complete" or source.get("pending_items"):
        raise ValueError("rollback requires a complete backfill ownership journal")
    if not isinstance(source.get("target"), dict):
        raise ValueError("rollback manifest is not bound to a database and storage root")
    items = source.get("items")
    if not isinstance(items, list):
        raise ValueError("rollback manifest items must be a list")

    seen_refs: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or item.get("created") is not True:
            raise ValueError("rollback manifest contains an entry without backfill provenance")
        ref = str(item.get("ref") or "")
        if not _is_safe_ref(ref):
            continue
        content_sha256 = str(item.get("sha256") or "")
        proof = str(item.get("ownership_proof") or "")
        if not _is_hex_digest(content_sha256, length=64) or proof != _ownership_proof(
            manifest_id=manifest_id,
            ref=ref,
            content_sha256=content_sha256,
        ):
            raise ValueError("rollback manifest contains an entry without valid backfill provenance")
        if ref in seen_refs:
            raise ValueError("rollback manifest contains duplicate sidecar references")
        seen_refs.add(ref)
    return manifest_id


def _sidecar_matches_provenance(
    payload: dict[str, Any],
    *,
    manifest_id: str,
    ownership_proof: str,
) -> bool:
    return payload.get(BACKFILL_PROVENANCE_KEY) == {
        "operation": BACKFILL_OPERATION,
        "manifest_id": manifest_id,
        "ownership_proof": ownership_proof,
    }


def _redaction_binding(config: LCMConfig) -> dict[str, Any]:
    """Record the sensitive-pattern policy applied to persisted sidecar content."""
    status = sensitive_pattern_status(config)
    return {
        "enabled": bool(status.get("enabled")),
        "active_patterns": sorted(str(name) for name in (status.get("active_patterns") or [])),
    }


def _redact_backfill_content(content: str, config: LCMConfig) -> str:
    """Apply the currently-enabled sensitive-pattern policy exactly as live ingest
    does before a tool result is externalized, so no un-redacted secret reaches the
    new sidecar retention surface."""
    redacted = redact_sensitive_value(content, config, parse_json_strings=False)
    return redacted if isinstance(redacted, str) else content


def _contains_serialized_media(content: str) -> bool:
    if _contains_media_payload(content):
        return True
    stripped = content.lstrip()
    if not stripped.startswith(("[", "{")):
        return False
    try:
        return _contains_media_payload(json.loads(content))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _read_only_connection(database_path: Path) -> sqlite3.Connection:
    uri = f"{database_path.expanduser().resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        refuse_schema_version_too_new(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _absolute_without_following(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _entry_identity(result: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(result.st_dev),
        int(result.st_ino),
        int(result.st_size),
        int(result.st_mtime_ns),
        int(result.st_ctime_ns),
    )


def _same_inode(result: os.stat_result, identity: tuple[int, int, int, int, int]) -> bool:
    return (int(result.st_dev), int(result.st_ino)) == identity[:2]


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def _lock_storage_directory(directory_fd: int) -> None:
    """Require an integrity-protected storage root and serialize this script's writers."""
    before = os.fstat(directory_fd)
    if not stat.S_ISDIR(before.st_mode):
        raise NotADirectoryError("externalized-payload storage root is not a directory")
    if int(before.st_uid) != os.geteuid():
        raise PermissionError("externalized-payload storage root must be owned by the current user")
    if before.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError(
            "externalized-payload storage root must not be writable by group or other users"
        )
    fcntl.flock(directory_fd, fcntl.LOCK_EX)
    after = os.fstat(directory_fd)
    if (
        (int(after.st_dev), int(after.st_ino)) != (int(before.st_dev), int(before.st_ino))
        or int(after.st_uid) != os.geteuid()
        or after.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise PermissionError("externalized-payload storage root changed while it was locked")


def _open_locked_storage_directory(path: Path) -> int:
    directory_fd = _open_directory(path)
    try:
        _lock_storage_directory(directory_fd)
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


@contextmanager
def _manifest_guard(path: Path) -> Iterator[tuple[Path, int]]:
    """Lock and bind manifest operations to one opened parent directory."""
    path = _absolute_without_following(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_fd = _open_directory(path.parent)
    lock_fd: int | None = None
    try:
        lock_flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        lock_fd = os.open(f".{path.name}.lock", lock_flags, 0o600, dir_fd=directory_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield path, directory_fd
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory_fd)


def _stat_entry(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _rename_exchange(directory_fd: int, first: str, second: str) -> None:
    """Atomically exchange two names without replacing either inode."""
    if sys.platform.startswith("linux"):
        function_name = "renameat2"
        exchange_flag = 2  # RENAME_EXCHANGE
    elif sys.platform == "darwin":
        function_name = "renameatx_np"
        exchange_flag = 0x00000002  # RENAME_SWAP
    else:
        raise OSError(errno.ENOTSUP, "atomic manifest exchange is not supported on this platform")

    libc = ctypes.CDLL(None, use_errno=True)
    rename_exchange = getattr(libc, function_name, None)
    if rename_exchange is None:
        raise OSError(errno.ENOTSUP, "atomic manifest exchange is not available on this platform")
    rename_exchange.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename_exchange.restype = ctypes.c_int
    result = rename_exchange(
        directory_fd,
        os.fsencode(first),
        directory_fd,
        os.fsencode(second),
        exchange_flag,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), second)


def _unlink_if_identity(
    directory_fd: int,
    name: str,
    expected_identity: tuple[int, int, int, int, int],
) -> None:
    current = _stat_entry(directory_fd, name)
    if current is not None and _entry_identity(current) == expected_identity:
        os.unlink(name, dir_fd=directory_fd)


def _read_regular_entry(directory_fd: int, name: str, *, label: str) -> tuple[bytes, tuple[int, int, int, int, int]]:
    before = _stat_entry(directory_fd, name)
    if before is None:
        raise FileNotFoundError(name)
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"{label} path must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} path must be a regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _entry_identity(opened) != _entry_identity(before):
            raise RuntimeError(f"{label} changed while it was being opened")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read()
            after = os.fstat(handle.fileno())
        if _entry_identity(after) != _entry_identity(opened):
            raise RuntimeError(f"{label} changed while it was being read")
        return data, _entry_identity(after)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_manifest(directory_fd: int, name: str) -> tuple[dict[str, Any], tuple[int, int, int, int, int]] | None:
    try:
        data, identity = _read_regular_entry(directory_fd, name, label="manifest")
    except FileNotFoundError:
        return None
    try:
        source = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest is not valid UTF-8 JSON") from exc
    if not isinstance(source, dict):
        raise ValueError("manifest must be a JSON object")
    return source, identity


def _write_manifest(
    directory_fd: int,
    name: str,
    manifest: dict[str, Any],
    *,
    expected_identity: tuple[int, int, int, int, int] | None,
) -> tuple[int, int, int, int, int]:
    """Atomically publish JSON without clobbering a raced target."""
    current = _stat_entry(directory_fd, name)
    if current is not None and stat.S_ISLNK(current.st_mode):
        raise ValueError("manifest path must not be a symlink")
    if expected_identity is None:
        if current is not None:
            raise FileExistsError("manifest path appeared during the operation")
    elif current is None or _entry_identity(current) != expected_identity:
        raise RuntimeError("manifest changed during the operation")

    data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
    temporary_identity: tuple[int, int, int, int, int] | None = None
    cleanup_identity: tuple[int, int, int, int, int] | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_identity = _entry_identity(os.fstat(handle.fileno()))
        cleanup_identity = temporary_identity
        current = _stat_entry(directory_fd, name)
        if current is not None and stat.S_ISLNK(current.st_mode):
            raise ValueError("manifest path must not be a symlink")
        if expected_identity is None:
            if current is not None:
                raise FileExistsError("manifest path appeared during the operation")
        elif current is None or _entry_identity(current) != expected_identity:
            raise RuntimeError("manifest changed during the operation")

        if expected_identity is None:
            try:
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise FileExistsError("manifest path appeared during the operation") from exc
            linked_temporary = _stat_entry(directory_fd, temporary_name)
            if linked_temporary is None:
                raise RuntimeError("manifest temporary file disappeared during publication")
            cleanup_identity = _entry_identity(linked_temporary)
        else:
            try:
                _rename_exchange(directory_fd, temporary_name, name)
            except FileNotFoundError as exc:
                raise RuntimeError("manifest changed during publication") from exc
            displaced = _stat_entry(directory_fd, temporary_name)
            published = _stat_entry(directory_fd, name)
            if (
                displaced is None
                or not _same_inode(displaced, expected_identity)
                or published is None
                or temporary_identity is None
                or not _same_inode(published, temporary_identity)
            ):
                try:
                    _rename_exchange(directory_fd, temporary_name, name)
                    os.fsync(directory_fd)
                except OSError as exc:
                    raise RuntimeError(
                        "manifest changed during publication and could not be restored"
                    ) from exc
                restored_temporary = _stat_entry(directory_fd, temporary_name)
                cleanup_identity = (
                    _entry_identity(restored_temporary)
                    if restored_temporary is not None
                    and temporary_identity is not None
                    and _same_inode(restored_temporary, temporary_identity)
                    else None
                )
                raise RuntimeError("manifest changed during publication")
            cleanup_identity = _entry_identity(displaced)

        if cleanup_identity is not None:
            _unlink_if_identity(directory_fd, temporary_name, cleanup_identity)
            cleanup_identity = None
        os.fsync(directory_fd)
        published = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(published.st_mode)
            or temporary_identity is None
            or not _same_inode(published, temporary_identity)
        ):
            raise RuntimeError("published manifest is not a regular file")
        return _entry_identity(published)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if cleanup_identity is not None:
            _unlink_if_identity(directory_fd, temporary_name, cleanup_identity)
            os.fsync(directory_fd)


def _target_binding(database_path: Path, storage_dir: Path) -> dict[str, Any]:
    database_path = database_path.expanduser().resolve(strict=True)
    storage_dir = storage_dir.expanduser().resolve(strict=False)
    database_stat = database_path.stat()
    storage_stat = storage_dir.stat() if storage_dir.exists() else None
    return {
        "database": {
            "path_sha256": _path_digest(database_path),
            "device": int(database_stat.st_dev),
            "inode": int(database_stat.st_ino),
        },
        "storage_root": {
            "path_sha256": _path_digest(storage_dir),
            "device": int(storage_stat.st_dev) if storage_stat is not None else None,
            "inode": int(storage_stat.st_ino) if storage_stat is not None else None,
        },
    }


def _validate_target_binding(source: dict[str, Any], current: dict[str, Any]) -> None:
    recorded = source.get("target")
    if not isinstance(recorded, dict):
        raise ValueError("manifest is not bound to a database and storage root")
    recorded_database = recorded.get("database")
    recorded_storage = recorded.get("storage_root")
    current_database = current["database"]
    current_storage = current["storage_root"]
    if not isinstance(recorded_database, dict) or not isinstance(recorded_storage, dict):
        raise ValueError("manifest is not bound to a database and storage root")
    database_matches = recorded_database == current_database
    storage_matches = recorded_storage.get("path_sha256") == current_storage.get("path_sha256")
    if recorded_storage.get("device") is not None or recorded_storage.get("inode") is not None:
        storage_matches = storage_matches and recorded_storage == current_storage
    if not database_matches or not storage_matches:
        raise ValueError("manifest belongs to a different database or storage root")


def _validate_redaction_binding(source: dict[str, Any], current: dict[str, Any]) -> None:
    """Refuse to resume a journal written under a different sensitive-pattern policy.

    The persisted sidecar content is redacted with the active policy, so reusing a
    journal after the policy changed would key pending entries by a different digest
    and could strand or duplicate owned sidecars. Fail closed instead.
    """
    recorded = source.get("redaction")
    if recorded is None:
        recorded = {"enabled": False, "active_patterns": []}
    if not isinstance(recorded, dict):
        raise ValueError("manifest is not bound to a sensitive-pattern redaction policy")
    recorded_patterns = recorded.get("active_patterns") or []
    if bool(recorded.get("enabled")) != bool(current.get("enabled")) or sorted(
        str(name) for name in recorded_patterns
    ) != list(current.get("active_patterns") or []):
        raise ValueError("manifest was written under a different sensitive-pattern redaction policy")


def _validate_existing_journal(source: dict[str, Any]) -> str:
    schema_version = source.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise ValueError(f"unsupported manifest schema: {schema_version!r}")
    if source.get("operation") != BACKFILL_OPERATION:
        raise ValueError("existing manifest belongs to a different operation")
    manifest_id = str(source.get("manifest_id") or "")
    if not _is_hex_digest(manifest_id, length=32):
        raise ValueError("existing manifest lacks valid backfill provenance")
    if source.get("applied") not in (True, False):
        raise ValueError("existing manifest has an invalid applied state")
    if not isinstance(source.get("items"), list) or not isinstance(source.get("pending_items", []), list):
        raise ValueError("existing manifest has invalid journal items")
    if source.get("applied") is True:
        for item in source["items"]:
            if not isinstance(item, dict) or item.get("created") is not True:
                raise ValueError("existing manifest contains an invalid owned item")
    return manifest_id


def _row_key(*, content: str, session_id: str, tool_call_id: str) -> tuple[str, str, str]:
    return (_sha256(content), _sha256(session_id), _sha256(tool_call_id))


def _pending_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("sha256") or ""),
        str(item.get("session_sha256") or ""),
        str(item.get("tool_call_sha256") or ""),
    )


def _backfill_ref(content: str) -> str:
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    return f"{timestamp}_historical-backfill_{_sha256(content)[:12]}_{secrets.token_hex(12)}.json"


def _backfill_payload(
    *,
    content: str,
    session_id: str,
    tool_call_id: str,
    manifest_id: str,
    ref: str,
    ownership_proof: str,
) -> dict[str, Any]:
    return {
        "kind": "tool_result",
        "tool_call_id": tool_call_id,
        "role": "tool",
        "session_id": session_id,
        "content": content,
        "content_chars": len(content),
        "content_bytes": len(content.encode("utf-8")),
        "created_at": time.time(),
        BACKFILL_PROVENANCE_KEY: {
            "operation": BACKFILL_OPERATION,
            "manifest_id": manifest_id,
            "ownership_proof": ownership_proof,
        },
    }


def _read_sidecar(directory_fd: int, ref: str) -> tuple[str, dict[str, Any] | None, tuple[int, int, int, int, int] | None]:
    entry = _stat_entry(directory_fd, ref)
    if entry is None:
        return "missing", None, None
    if stat.S_ISLNK(entry.st_mode):
        return "symlink", None, None
    if not stat.S_ISREG(entry.st_mode):
        return "invalid", None, None
    try:
        data, identity = _read_regular_entry(directory_fd, ref, label="sidecar")
        payload = json.loads(data)
    except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return "invalid", None, None
    return "ok", payload if isinstance(payload, dict) else None, identity


def _sidecar_matches_item(payload: dict[str, Any] | None, item: dict[str, Any], manifest_id: str) -> bool:
    if not isinstance(payload, dict):
        return False
    content = payload.get("content")
    return (
        isinstance(content, str)
        and _sha256(content) == str(item.get("sha256") or "")
        and _sidecar_matches_provenance(
            payload,
            manifest_id=manifest_id,
            ownership_proof=str(item.get("ownership_proof") or ""),
        )
    )


def _manifest_item(*, content: str, ref: str = "", created: bool = False) -> dict[str, Any]:
    return {
        "ref": ref,
        "sha256": _sha256(content),
        "content_chars": len(content),
        "token_estimate": count_tokens(content),
        "created": created,
    }


def run_backfill(
    *,
    database_path: Path,
    hermes_home: Path,
    manifest_path: Path,
    threshold_chars: int,
    apply: bool,
    max_rows: int = 0,
    config: LCMConfig | None = None,
) -> dict[str, Any]:
    """Scan historical rows and maintain a crash-recoverable ownership journal."""
    runtime_config = copy.copy(config or load_config())
    runtime_config.large_output_externalization_enabled = True
    runtime_config.large_output_externalization_threshold_chars = max(1, threshold_chars)
    threshold_chars = max(1, threshold_chars)
    with closing(_read_only_connection(database_path)) as connection:
        storage_dir = get_large_output_storage_dir(
            runtime_config,
            hermes_home=str(hermes_home),
            create=False,
        )
        current_target = _target_binding(database_path, storage_dir)
        redaction = _redaction_binding(runtime_config)

        with _manifest_guard(manifest_path) as (guarded_manifest_path, manifest_directory_fd):
            existing_manifest = _read_manifest(manifest_directory_fd, guarded_manifest_path.name)
            manifest_identity: tuple[int, int, int, int, int] | None = None
            if existing_manifest is None:
                manifest_id = secrets.token_hex(16)
                owned_items: list[dict[str, Any]] = []
                pending_items: list[dict[str, Any]] = []
            else:
                source, manifest_identity = existing_manifest
                manifest_id = _validate_existing_journal(source)
                _validate_target_binding(source, current_target)
                _validate_redaction_binding(source, redaction)
                if source["applied"] is True and not apply:
                    raise ValueError("an applied ownership journal cannot be replaced by a dry run")
                if source["applied"] is False:
                    owned_items = []
                    pending_items = []
                else:
                    owned_items = copy.deepcopy(source["items"])
                    pending_items = copy.deepcopy(source.get("pending_items", []))

            if apply:
                storage_dir = get_large_output_storage_dir(
                    runtime_config,
                    hermes_home=str(hermes_home),
                    create=True,
                )
                current_target = _target_binding(database_path, storage_dir)

            counts = {
                "scanned": 0,
                "eligible": 0,
                "created": 0,
                "existing": 0,
                "skipped_below_threshold": 0,
                "skipped_externalized": 0,
                "skipped_media": 0,
                "failed": 0,
            }
            failed_paths: list[str] = []
            token_estimate_total = 0
            manifest = {
                "schema_version": 1,
                "operation": BACKFILL_OPERATION,
                "manifest_id": manifest_id,
                "applied": apply,
                "state": "applying" if apply else "dry_run",
                "target": current_target,
                "redaction": redaction,
                "threshold_chars": threshold_chars,
                "counts": counts,
                "token_estimate_total": 0,
                "items": owned_items,
                "pending_items": pending_items,
                "failed_paths": failed_paths,
            }

            def publish_manifest() -> None:
                nonlocal manifest_identity
                manifest_identity = _write_manifest(
                    manifest_directory_fd,
                    guarded_manifest_path.name,
                    manifest,
                    expected_identity=manifest_identity,
                )

            storage_directory_fd: int | None = None
            if apply:
                storage_directory_fd = _open_locked_storage_directory(storage_dir)
                publish_manifest()
            try:
                pending_by_key = {_pending_key(item): item for item in pending_items}
                owned_refs = {str(item.get("ref") or "") for item in owned_items}
                cursor = connection.execute(
                    """
                    SELECT session_id, content, tool_call_id
                    FROM messages
                    WHERE role = 'tool'
                    ORDER BY store_id
                    """
                )
                for row in cursor:
                    if max_rows > 0 and counts["scanned"] >= max_rows:
                        break
                    counts["scanned"] += 1
                    content = row["content"]
                    if not isinstance(content, str) or len(content) <= threshold_chars:
                        counts["skipped_below_threshold"] += 1
                        continue
                    if is_externalized_placeholder(content):
                        counts["skipped_externalized"] += 1
                        continue
                    if _contains_serialized_media(content):
                        counts["skipped_media"] += 1
                        continue

                    counts["eligible"] += 1
                    # Redact secrets exactly as live ingest does before externalizing,
                    # so the persisted sidecar (and every digest, ref, and provenance
                    # proof derived from it) never carries an un-redacted secret.
                    payload_content = _redact_backfill_content(content, runtime_config)
                    item = _manifest_item(content=payload_content)
                    token_estimate_total += int(item["token_estimate"])
                    manifest["token_estimate_total"] = token_estimate_total
                    session_id = str(row["session_id"] or "")
                    tool_call_id = str(row["tool_call_id"] or "")
                    key = _row_key(content=payload_content, session_id=session_id, tool_call_id=tool_call_id)
                    pending = pending_by_key.get(key)

                    if pending is None:
                        existing = find_externalized_payload_for_message(
                            payload_content,
                            tool_call_id=tool_call_id,
                            session_id=session_id,
                            kind="tool_result",
                            role="tool",
                            config=runtime_config,
                            hermes_home=str(hermes_home),
                        )
                        if existing is not None:
                            counts["existing"] += 1
                            continue
                    if not apply:
                        manifest["items"].append(item)
                        continue

                    assert storage_directory_fd is not None
                    if pending is None:
                        ref = _backfill_ref(payload_content)
                        ownership_proof = _ownership_proof(
                            manifest_id=manifest_id,
                            ref=ref,
                            content_sha256=item["sha256"],
                        )
                        pending = {
                            "ref": ref,
                            "sha256": item["sha256"],
                            "session_sha256": key[1],
                            "tool_call_sha256": key[2],
                            "ownership_proof": ownership_proof,
                        }
                        pending_items.append(pending)
                        pending_by_key[key] = pending
                        publish_manifest()
                    else:
                        ref = str(pending.get("ref") or "")
                        ownership_proof = str(pending.get("ownership_proof") or "")

                    status, persisted_payload, _persisted_identity = _read_sidecar(storage_directory_fd, ref)
                    if status == "ok" and _sidecar_matches_item(persisted_payload, pending, manifest_id):
                        counts["existing"] += 1
                    elif status == "missing":
                        payload = _backfill_payload(
                            content=payload_content,
                            session_id=session_id,
                            tool_call_id=tool_call_id,
                            manifest_id=manifest_id,
                            ref=ref,
                            ownership_proof=ownership_proof,
                        )
                        try:
                            _replace_externalized_payload(storage_dir / ref, payload)
                        except OSError:
                            counts["failed"] += 1
                            failed_paths.append(str(storage_dir / ref))
                            publish_manifest()
                            continue
                        status, persisted_payload, _persisted_identity = _read_sidecar(storage_directory_fd, ref)
                        if status != "ok" or not _sidecar_matches_item(persisted_payload, pending, manifest_id):
                            counts["failed"] += 1
                            failed_paths.append(str(storage_dir / ref))
                            publish_manifest()
                            continue
                        counts["created"] += 1
                    else:
                        counts["failed"] += 1
                        failed_paths.append(str(storage_dir / ref))
                        publish_manifest()
                        continue

                    pending_items.remove(pending)
                    pending_by_key.pop(key, None)
                    if ref not in owned_refs:
                        owned_item = _manifest_item(content=payload_content, ref=ref, created=True)
                        owned_item["ownership_proof"] = ownership_proof
                        owned_items.append(owned_item)
                        owned_refs.add(ref)
                    publish_manifest()

                manifest["state"] = "complete" if not pending_items else "incomplete"
                publish_manifest()
                return manifest
            finally:
                if storage_directory_fd is not None:
                    os.close(storage_directory_fd)


def _extract_referenced_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str):
        refs.update(extract_all_externalized_payload_refs(value))
        refs.update(
            ref
            for ref in re.findall(r"(?:externalized_)?ref\s*[=:]\s*[\"']?([A-Za-z0-9_.-]+)", value)
            if _is_safe_ref(ref)
        )
        stripped = value.lstrip()
        if stripped.startswith(("[", "{")):
            try:
                refs.update(_extract_referenced_refs(json.loads(value)))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return refs
    if isinstance(value, list):
        for item in value:
            refs.update(_extract_referenced_refs(item))
        return refs
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"ref", "externalized_ref"} and isinstance(item, str) and _is_safe_ref(item):
                refs.add(item)
            refs.update(_extract_referenced_refs(item))
    return refs


def _collect_referenced_refs(connection: sqlite3.Connection) -> set[str]:
    refs: set[str] = set()
    for row in connection.execute("SELECT content, tool_calls FROM messages"):
        refs.update(_extract_referenced_refs(row["content"]))
        refs.update(_extract_referenced_refs(row["tool_calls"]))
    has_summary_nodes = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'summary_nodes'"
    ).fetchone()
    if has_summary_nodes:
        for row in connection.execute("SELECT summary FROM summary_nodes"):
            refs.update(_extract_referenced_refs(row["summary"]))
    return refs


def _restore_quarantine(
    directory_fd: int,
    quarantine: str,
    ref: str,
    expected_identity: tuple[int, int, int, int, int],
) -> None:
    quarantined = _stat_entry(directory_fd, quarantine)
    if (
        quarantined is None
        or (int(quarantined.st_dev), int(quarantined.st_ino)) != expected_identity[:2]
        or _stat_entry(directory_fd, ref) is not None
    ):
        return
    os.rename(quarantine, ref, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)


def _unlink_verified_quarantine(
    directory_fd: int,
    quarantine: str,
    expected_identity: tuple[int, int, int, int, int],
) -> bool:
    """Perform the final identity check under the storage-directory integrity lock."""
    current = _stat_entry(directory_fd, quarantine)
    if current is None or (int(current.st_dev), int(current.st_ino)) != expected_identity[:2]:
        return False
    os.remove(quarantine, dir_fd=directory_fd)
    return True


def _quarantine_unlink(
    directory_fd: int,
    ref: str,
    expected_identity: tuple[int, int, int, int, int],
) -> bool:
    """Move the checked inode to a private name before unlinking it."""
    current = _stat_entry(directory_fd, ref)
    if current is None or _entry_identity(current) != expected_identity:
        return False
    quarantine = f".rollback-{secrets.token_hex(16)}.tmp"
    os.rename(ref, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    try:
        moved = _stat_entry(directory_fd, quarantine)
        if moved is None or (int(moved.st_dev), int(moved.st_ino)) != expected_identity[:2]:
            _restore_quarantine(directory_fd, quarantine, ref, expected_identity)
            return False
        if not _unlink_verified_quarantine(directory_fd, quarantine, expected_identity):
            _restore_quarantine(directory_fd, quarantine, ref, expected_identity)
            return False
        os.fsync(directory_fd)
        return True
    except OSError:
        _restore_quarantine(directory_fd, quarantine, ref, expected_identity)
        raise


def _assert_manifest_identity(
    directory_fd: int,
    name: str,
    expected_identity: tuple[int, int, int, int, int],
) -> None:
    current = _stat_entry(directory_fd, name)
    if current is None or _entry_identity(current) != expected_identity:
        raise RuntimeError("rollback manifest changed during the operation")


def run_rollback(
    *,
    database_path: Path,
    hermes_home: Path,
    source_manifest_path: Path,
    apply: bool,
    config: LCMConfig | None = None,
) -> dict[str, Any]:
    """Delete only safe, unreferenced sidecars owned by an apply manifest."""
    runtime_config = copy.copy(config or load_config())
    with closing(_read_only_connection(database_path)) as connection:
        storage_dir = get_large_output_storage_dir(
            runtime_config,
            hermes_home=str(hermes_home),
            create=False,
        )
        current_target = _target_binding(database_path, storage_dir)
        referenced_refs = _collect_referenced_refs(connection)

        with _manifest_guard(source_manifest_path) as (guarded_manifest_path, manifest_directory_fd):
            loaded = _read_manifest(manifest_directory_fd, guarded_manifest_path.name)
            if loaded is None:
                raise FileNotFoundError(source_manifest_path)
            source, source_identity = loaded
            manifest_id = _validate_rollback_manifest(source)
            _validate_target_binding(source, current_target)

            counts = {
                "manifest_items": 0,
                "eligible": 0,
                "deleted": 0,
                "succeeded": 0,
                "failed": 0,
                "skipped": 0,
                "skipped_invalid_ref": 0,
                "skipped_missing": 0,
                "skipped_symlink": 0,
                "skipped_provenance_mismatch": 0,
                "skipped_digest_mismatch": 0,
                "skipped_referenced": 0,
            }
            failed_paths: list[str] = []
            eligible_entries: list[tuple[str, tuple[int, int, int, int, int]]] = []
            try:
                storage_directory_fd = _open_locked_storage_directory(storage_dir)
            except FileNotFoundError:
                storage_directory_fd = None
            try:
                for item in source["items"]:
                    counts["manifest_items"] += 1
                    ref = str(item.get("ref") or "")
                    if not _is_safe_ref(ref):
                        counts["skipped_invalid_ref"] += 1
                        continue
                    if storage_directory_fd is None:
                        counts["skipped_missing"] += 1
                        continue
                    status, payload, identity = _read_sidecar(storage_directory_fd, ref)
                    if status == "missing":
                        counts["skipped_missing"] += 1
                        continue
                    if status == "symlink":
                        counts["skipped_symlink"] += 1
                        continue
                    if status != "ok" or not isinstance(payload, dict) or identity is None:
                        counts["skipped_provenance_mismatch"] += 1
                        continue
                    if not _sidecar_matches_provenance(
                        payload,
                        manifest_id=manifest_id,
                        ownership_proof=str(item["ownership_proof"]),
                    ):
                        counts["skipped_provenance_mismatch"] += 1
                        continue
                    content = payload.get("content")
                    if not isinstance(content, str) or _sha256(content) != str(item.get("sha256") or ""):
                        counts["skipped_digest_mismatch"] += 1
                        continue
                    if ref in referenced_refs:
                        counts["skipped_referenced"] += 1
                        continue
                    counts["eligible"] += 1
                    eligible_entries.append((ref, identity))

                if apply and storage_directory_fd is not None:
                    for ref, identity in eligible_entries:
                        _assert_manifest_identity(
                            manifest_directory_fd,
                            guarded_manifest_path.name,
                            source_identity,
                        )
                        try:
                            deleted = _quarantine_unlink(storage_directory_fd, ref, identity)
                        except OSError:
                            deleted = False
                        if not deleted:
                            counts["failed"] += 1
                            failed_paths.append(str(storage_dir / ref))
                            continue
                        counts["deleted"] += 1
                        counts["succeeded"] += 1
            finally:
                if storage_directory_fd is not None:
                    os.close(storage_directory_fd)

            counts["skipped"] = sum(
                counts[key]
                for key in (
                    "skipped_invalid_ref",
                    "skipped_missing",
                    "skipped_symlink",
                    "skipped_provenance_mismatch",
                    "skipped_digest_mismatch",
                    "skipped_referenced",
                )
            )

            return {
                "schema_version": 1,
                "operation": "historical_tool_output_externalization_rollback",
                "applied": apply,
                "counts": counts,
                "failed_paths": failed_paths,
            }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="LCM SQLite path; defaults to LCM_DATABASE_PATH or HERMES_HOME/lcm.db")
    parser.add_argument("--hermes-home", help="Hermes profile home; defaults to HERMES_HOME or ~/.hermes")
    parser.add_argument("--manifest", default="lcm-externalization-backfill-manifest.json")
    parser.add_argument("--threshold-chars", type=int)
    parser.add_argument("--max-rows", type=int, default=0, help="0 scans all historical tool rows")
    parser.add_argument("--rollback", help="Applied manifest to roll back safely")
    parser.add_argument("--apply", action="store_true", help="Create or delete sidecars; otherwise dry-run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    config = load_config(database=args.database, home=args.hermes_home)
    hermes_home = Path(args.hermes_home or Path(config.database_path).parent).expanduser().resolve()
    database_path = Path(args.database or config.database_path or hermes_home / "lcm.db").expanduser().resolve()
    if not database_path.is_file():
        raise SystemExit("LCM database does not exist")

    try:
        if args.rollback:
            result = run_rollback(
                database_path=database_path,
                hermes_home=hermes_home,
                source_manifest_path=_absolute_without_following(Path(args.rollback)),
                apply=args.apply,
                config=config,
            )
        else:
            threshold = args.threshold_chars
            if threshold is None:
                threshold = max(1, int(config.large_output_externalization_threshold_chars or 12_000))
            result = run_backfill(
                database_path=database_path,
                hermes_home=hermes_home,
                manifest_path=_absolute_without_following(Path(args.manifest)),
                threshold_chars=threshold,
                apply=args.apply,
                max_rows=max(0, args.max_rows),
                config=config,
            )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        result = {
            "schema_version": 1,
            "operation": (
                "historical_tool_output_externalization_rollback" if args.rollback else BACKFILL_OPERATION
            ),
            "applied": args.apply,
            "counts": {"failed": 1},
            "failed_paths": [str(Path(args.rollback or args.manifest).expanduser())],
            "error": str(exc),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.apply and result["counts"]["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

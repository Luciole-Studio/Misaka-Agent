# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / tools/credential_files.py; see PROVENANCE.json and LICENSE.
import logging
import os
from pathlib import Path
from typing import Optional, Dict, List, Iterator, Tuple
from ..layers import EXCLUDED_SKILL_DIRS
from ..runtime import active_runtime, credential_read_error, validate_within_dir
logger = logging.getLogger(__name__)
def get_hermes_home():
    return active_runtime().profile
def _get_registered():
    return active_runtime().files
get_read_block_error = credential_read_error

def _mount(host_path: Path | str, container_path: str) -> Dict[str, str]:
    return {"host_path": str(host_path), "container_path": container_path}


def _contained_host_path(rel: str, hermes_home: Path, abs_msg: str, traversal_msg: str) -> Optional[Path]:
    """Resolve *rel* under HERMES_HOME, refusing absolute paths and escapes."""
    if os.path.isabs(rel):
        logger.warning(abs_msg, rel)
        return None
    host_path = hermes_home / rel

    if containment_error := validate_within_dir(host_path, hermes_home):
        logger.warning(traversal_msg, rel, containment_error)
        return None
    return host_path.resolve()


def register_credential_file(relative_path: str, container_base: str = "/root/.hermes") -> bool:
    """Register a HERMES_HOME-relative credential file for mounting; True if it exists and was registered.

    Rejects absolute paths and traversal out of HERMES_HOME. Containment alone is not
    enough: HERMES_HOME holds the MASTER stores (``.env``, ``auth.json``, ``mcp-tokens/``),
    which are refused via the canonical read deny-list so the mount surface cannot hand a
    skill what the read surface denies. Fails CLOSED (logged) if the guard is unavailable or raises.
    """
    resolved = _contained_host_path(
        relative_path, get_hermes_home(),
        "credential_files: rejected absolute path %r (must be relative to HERMES_HOME)",
        "credential_files: rejected path traversal %r (%s)")
    if resolved is None:
        return False
    if not resolved.is_file():
        logger.debug("credential_files: skipping %s (not found)", resolved)
        return False
    # Master credential stores are never mountable, even though they sit inside HERMES_HOME and therefore
    # pass the containment check above. Fails CLOSED: if the canonical guard can't be consulted we refuse
    # the mount rather than risk bind-mounting auth.json into a sandbox. The import lives at module top (no
    # circular-import concern — file_safety is stdlib-only); the sentinel + logger.exception keep guard
    # failures debuggable instead of silently swallowed (#67665).
    if get_read_block_error is None:
        logger.error("credential_files: refusing %r — agent.file_safety could not be "
                     "imported, so the master-store deny-list cannot be consulted", relative_path)
        return False
    try:
        denied = get_read_block_error(str(resolved))
    except Exception:
        logger.exception("credential_files: refusing %r — read guard raised", relative_path)
        return False
    if denied:
        logger.warning("credential_files: refused %r — it is a credential store the agent "
                       "is denied from reading; a skill may mount its own service token, "
                       "not the master key files", relative_path)
        return False

    container_path = f"{container_base.rstrip('/')}/{relative_path}"
    _get_registered()[container_path] = str(resolved)
    logger.debug("credential_files: registered %s -> %s", resolved, container_path)
    return True


def register_credential_files(entries: list, container_base: str = "/root/.hermes") -> List[str]:
    """Register skill-frontmatter entries (str or dict with ``path``); return missing paths."""
    missing = []
    for entry in entries:
        if isinstance(entry, dict):
            entry = entry.get("path") or entry.get("name") or ""
        elif not isinstance(entry, str):
            continue
        rel_path = entry.strip() if isinstance(entry, str) else ""
        if rel_path and not register_credential_file(rel_path, container_base):
            missing.append(rel_path)
    return missing


def _walk_skill_tree(root: Path) -> Iterator[Tuple[Path, List[Path]]]:
    """Yield ``(dir, regular_non_symlink_files)`` for every directory a sandbox should receive.

    Prunes ``EXCLUDED_SKILL_DIRS`` *before* descending so bookkeeping/dependency trees (``.hub``,
    ``.archive``, ``.curator_backups``, ``node_modules``, ``.git``, ...) the remote agent never reads
    are never even walked; sync thus agrees with discovery on what is skill content. Deliberately
    not ``is_excluded_skill_path()``: that also prunes ``references/``, ``templates/``, ``assets/``,
    ``scripts/`` — progressive-disclosure files and bundled scripts the sandbox does execute.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_SKILL_DIRS)
        base = Path(dirpath)
        yield base, [f for f in (base / n for n in filenames) if not f.is_symlink() and f.is_file()]


"""Save extracted pages with source provenance and content-addressed names.

Model-facing copies redact configured credentials. If that changes the material,
the original is retained privately in the profile, outside the workspace corpus.
Different versions never overwrite the material another researcher cited.
Digests identify files, not the truth or adequacy of their content.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from misaka.core.documents.index import under
from misaka.core.tools.path_utils import DOWNLOAD_DIR_NAME, resolve_to_cwd

# Where the complete extracted text of a fetched page is left, under the workspace.
# Beside download_file's own output rather than in a directory of its own: both are
# "a thing this session pulled off the internet and can be asked to cite".
_PAGE_DIR = f"{DOWNLOAD_DIR_NAME}/pages"

# Characters of the page digest used as the filename: a page fetched
# twice writes the same file rather than accumulating copies. 12 hex digits is 48 bits,
# which is not a collision risk across one workspace's worth of pages and is short
# enough that the model can carry the name into a finding.
_PAGE_STEM_CHARS = 12


def citable_url(url: str) -> str:
    """*url* with its query dropped: the address a page may be reported and stored under.

    Only ever applied to a *final* URL -- the one the server chose at the end of a
    redirect chain, which commonly ends at a CDN and commonly ends presigned.
    ``?X-Amz-Signature=...`` is a live credential the caller never saw and must not be
    handed, still less written into a provenance header that outlives the session and gets
    registered as an artifact. The path is what identifies the object and the digest
    beside it is what locks the evidence.

    The *requested* URL is deliberately not passed through this: the caller wrote it,
    already has it, and its query is often the only thing that names the document.
    """
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _frontmatter(provenance: dict[str, Any]) -> str:
    """The provenance block a saved page opens with, delimiters and trailing blank line.

    The one place the format exists. :func:`save_page` writes what this returns and
    :func:`frontmatter_line_count` counts what this returns, so the writer and the count
    cannot drift apart the way two implementations that merely agree today would.

    JSON scalars are valid YAML scalars, which is what keeps a page-written ``<title>`` or
    a query-laden URL from deciding how the frontmatter parses -- and, incidentally, what
    makes the line count exact: ``json.dumps`` escapes a newline inside a value rather
    than emitting one, so every key is one line whatever the page put in it.
    """
    front = "\n".join(f"{key}: {json.dumps(value)}" for key, value in provenance.items())
    return f"---\n{front}\n---\n\n"


def read_provenance(path: str) -> dict[str, Any]:
    """The provenance block a saved page opens with, as written by ``_frontmatter``: ``key: <json>``
    lines between two ``---`` delimiters. Empty for any other file or an unreadable one."""
    try:
        with open(path, encoding="utf-8") as handle:
            if handle.readline().strip() != "---":
                return {}
            out: dict[str, Any] = {}
            for _ in range(64):
                line = handle.readline()
                if not line or line.strip() == "---":
                    break
                key, _sep, value = line.partition(":")
                try:
                    out[key.strip()] = json.loads(value.strip())
                except ValueError:
                    out[key.strip()] = value.strip()
            return out
    except OSError:
        return {}


def frontmatter_line_count(provenance: dict[str, Any]) -> int:
    """Lines the provenance block occupies: both ``---`` delimiters and the blank line.

    ``web_extract`` truncates a long page and tells the model which line of the saved file
    the omitted middle starts at, so its next ``read`` lands in the gap instead of
    guessing. Hermes computes that offset against the text alone (``tools/web_tools.py``
    ``_truncate_with_footer``, ``head.count("\\n") + 2``), which is right for a Hermes
    cache file because that file *is* the text. Here the file opens with this block and
    ``read`` is 1-indexed over the whole file, so the same arithmetic points the model at
    the frontmatter and it reads yaml where it expected the page.

    Nobody may hand-count this: the writer owns the format, so the writer owns the count.
    """
    return _frontmatter(provenance).count("\n")


def check_material_read(path: str) -> None:
    """Keep vault key/ciphertext and restricted originals out of ordinary readers.

    Resolved paths also cover symlinks. This is a tool/corpus boundary, not an OS
    sandbox for an independently approved terminal command.
    """
    resolved = Path(path).resolve()
    for directory in (resolved, *resolved.parents):
        if (directory.name == "originals" and directory.parent.name == "web-evidence") or (
                directory.name == "vault" and any((directory / name).exists()
                    for name in ("vault.key", "vault.json.enc"))):
            raise ValueError("Private Web material is not available through file readers; use the vault tools or the redacted saved_path.")


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass  # nothing to undo if the staging file is already gone


def save_page(cwd: str | None, provenance: dict[str, Any], text: str) -> str | None:
    """Write the complete extracted text into the workspace; return its relative path.

    The filename hashes the exact serialized bytes, including every provenance field.
    A new title, provider or extraction is a new artifact, never an overwrite of an
    already cited version. Returns None on storage failure; the fetch still succeeds.
    """
    if not cwd:
        return None
    from misaka.config import home
    from misaka.core.web.config import redact_secrets, redact_values
    from misaka.core.web.scope import current_scope

    original = f"{_frontmatter(provenance)}{text}\n"
    public = redact_values(provenance)
    clean = redact_secrets(text)
    if clean != text or public != provenance:
        try:
            digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
            root = home.path("web_evidence", current_scope().profile_dir).resolve()
            directory = root / "originals"
            if not directory.resolve().is_relative_to(root):
                return None
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
            if not _write_document(str(directory / f"{digest}.md"), original, mode=0o600):
                return None  # Keep the original before publishing a changed copy.
        except (OSError, UnicodeError):
            return None
        public = {**public, "redacted": True, "original_sha256": digest,
                  "text_sha256": hashlib.sha256(clean.encode("utf-8")).hexdigest()}
    document = f"{_frontmatter(public)}{clean}\n"
    try:
        stem = hashlib.sha256(document.encode("utf-8")).hexdigest()[:_PAGE_STEM_CHARS]
    except UnicodeError:
        return None
    relative = f"{_PAGE_DIR}/{stem}.md"
    path = resolve_to_cwd(relative, cwd)
    if not under(path, cwd):
        return None
    return relative if _write_document(path, document, mode=0o644) else None


def _write_document(path: str, document: str, *, mode: int) -> bool:
    """Stage privately, then publish atomically; never expose a partial original."""
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        handle, staging = tempfile.mkstemp(dir=directory, suffix=".part")
    except OSError:
        return False
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(document)
        os.chmod(staging, mode)
        # Renamed into place rather than written in place: the name is a digest of the
        # bytes being written, so two fetches can legitimately target it at once, and a
        # reader must never meet a half-written provenance header.
        os.replace(staging, path)
    except (OSError, UnicodeError):
        # UnicodeError as well as OSError: a vendor's JSON can legitimately carry a lone
        # surrogate (json.loads accepts an unpaired \\ud83d), and writing it raises
        # UnicodeEncodeError, which is not an OSError. Losing the evidence file is the
        # documented cost of a failed write; losing the whole extraction is not.
        _discard(staging)
        return False
    return True


__all__ = ["citable_url", "frontmatter_line_count", "read_provenance", "save_page"]

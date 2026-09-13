"""Save complete extracted pages with source provenance and content-addressed names.

Tool previews are bounded; the saved original remains available for readers and red
teams. Different versions never overwrite the material another researcher cited.
Digests identify files, not the truth or adequacy of their content.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
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
    document = f"{_frontmatter(provenance)}{text}\n"
    try:
        stem = hashlib.sha256(document.encode("utf-8")).hexdigest()[:_PAGE_STEM_CHARS]
    except UnicodeError:
        return None
    relative = f"{_PAGE_DIR}/{stem}.md"
    path = resolve_to_cwd(relative, cwd)
    if not under(path, cwd):
        return None
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        handle, staging = tempfile.mkstemp(dir=directory, suffix=".part")
    except OSError:
        return None
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(document)
        # mkstemp creates 0600; an evidence file is an ordinary workspace file, and the
        # mode came from the staging file rather than from any decision about it.
        os.chmod(staging, 0o644)
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
        return None
    return relative


__all__ = ["citable_url", "frontmatter_line_count", "read_provenance", "save_page"]

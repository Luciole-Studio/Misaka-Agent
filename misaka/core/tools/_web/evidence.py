"""Where a page this session read is left on disk, and what its file is called.

``web_fetch`` and ``web_extract`` both come back holding the clean text of a page, and
both owe that text a home: what enters the context is a truncated rendering only this
process ever saw, while ``research/ledger.py`` verifies a quotation against a *registered
artifact*. Without a file, a Sister has to retype the page into her own markdown and the
"verbatim" check then compares her transcription with itself.

Hermes gives ``web_extract`` a home of its own -- ``~/.hermes/cache/web``, named for the
URL alone (``tools/web_tools.py:693`` ``_store_full_text``) -- and that is the thing not
ported. A directory outside the workspace is invisible to the run's evidence machinery:
``branch_finish`` never sweeps it into the leftover commit, ``_register_task_artifacts``
never registers it, and a name keyed on the URL alone means the second fetch of a page
that now serves different bytes silently rewrites the copy a card has already cited.

So MISAKA has one writer for both tools, and this is it: a workspace file under
``downloads/pages/`` whose *name* is a digest of the addresses and the bytes together,
opened by a provenance block. A page reached through a vendor's renderer is then evidence
on exactly the same terms as a page dialled directly, and the ledger cannot tell them
apart -- which is the point.

Lifted out of ``web_fetch`` behaviour-for-behaviour rather than rewritten: these files are
content-addressed and already cited, so one changed byte here changes every filename and
every ``research_artifacts.sha256`` ever recorded against them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from misaka.core.tools.path_utils import DOWNLOAD_DIR_NAME, resolve_to_cwd

logger = logging.getLogger(__name__)

# Where the complete extracted text of a fetched page is left, under the workspace.
# Beside download_file's own output rather than in a directory of its own: both are
# "a thing this session pulled off the internet and can be asked to cite".
_PAGE_DIR = f"{DOWNLOAD_DIR_NAME}/pages"

# Characters of the page digest used as the filename (see page_stem): a page fetched
# twice writes the same file rather than accumulating copies. 12 hex digits is 48 bits,
# which is not a collision risk across one workspace's worth of pages and is short
# enough that the model can carry the name back in a report.json entry.
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


def page_stem(requested: str, final_url: str, body: bytes) -> str:
    """The evidence file's name: a digest over the addresses *and* the bytes it documents.

    Not the body's digest alone, for two reasons that are the same reason.

    Two different URLs routinely serve byte-identical bodies -- the academic table sends
    ``/pdf/`` and ``/abs/`` to one address, and a utm-tagged link returns the page the
    plain link does -- and one file for both means the second fetch rewrites the
    ``source_url`` a card has already cited: the delivered report then attributes a quote
    to a URL it did not come from.

    And in the other direction, everything written into the file is derived from these
    three values, so one name can only ever hold one set of bytes. That is what lets two
    research nodes fetch the same primary source in their own worktrees: ``branch_finish``
    sweeps ``downloads/`` into a leftover commit and merges, and same-path-different-bytes
    is an add/add conflict that parks the whole run. It is also why ``fetched_at`` is kept
    out of the file entirely -- a clock reading is not a property of the page, and
    ``runs.artifact_text`` re-checks the sha it registered.
    """
    digest = hashlib.sha256()
    for address in (requested, final_url):
        # NUL-separated: a URL cannot contain one, so no two field splits collide.
        digest.update(address.encode() + b"\0")
    digest.update(body)
    return digest.hexdigest()[:_PAGE_STEM_CHARS]


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


def save_page(cwd: str | None, stem: str, provenance: dict[str, Any], text: str) -> str | None:
    """Write the complete extracted text into the workspace; return its relative path.

    This is the half of a fetch that makes a page quotable. What enters the context is a
    truncated rendering only this process ever saw, while ``research/ledger.py`` verifies
    a quote against a *registered artifact* -- so without a file on disk a Sister has to
    retype the page into her own markdown, and the "verbatim" check then compares her
    transcription with itself. The file is plain UTF-8 under the workspace, which is
    exactly what ``research/workflow.py``'s ``_register_task_artifacts`` accepts as-is
    once the card lists the path in its ``report.json``.

    *provenance* is written key for key in the order given, with no whitelist: ``web_fetch``
    stamps the five fields ``ledger`` reads and ``web_extract`` adds the vendor that
    rendered the page, and a writer that decided which keys were allowed would make adding
    the sixth a change to this module rather than to its caller.

    Returns None when there is nowhere to write (a session with no workspace) or when the
    write fails: a page that was fetched successfully is still reported successfully, so
    a full disk costs the evidence file, never the fetch.
    """
    if not cwd:
        return None
    relative = f"{_PAGE_DIR}/{stem}.md"
    path = resolve_to_cwd(relative, cwd)
    directory = os.path.dirname(path)
    document = f"{_frontmatter(provenance)}{text}\n"
    try:
        os.makedirs(directory, exist_ok=True)
        handle, staging = tempfile.mkstemp(dir=directory, suffix=".part")
    except OSError as error:
        logger.debug("No evidence file for %s: %s", relative, error)
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
    except (OSError, UnicodeError) as error:
        # UnicodeError as well as OSError: a vendor's JSON can legitimately carry a lone
        # surrogate (json.loads accepts an unpaired \\ud83d), and writing it raises
        # UnicodeEncodeError, which is not an OSError. Losing the evidence file is the
        # documented cost of a failed write; losing the whole extraction is not.
        logger.debug("No evidence file for %s: %s", relative, error)
        _discard(staging)
        return None
    return relative


__all__ = ["citable_url", "frontmatter_line_count", "page_stem", "save_page"]

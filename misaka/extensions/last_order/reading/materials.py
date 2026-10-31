"""The materials a reading opens. A file in the project is indexed where it is. A file outside the
project, or a URL, is copied in first -- into the reading's ``sources/``, or the project's
``downloads/`` when the reading's folder is outside the project -- and indexed there, with the
original path or URL kept as its origin. Every material is then read through the project's index,
by the same document tools Last Order and the Sisters use."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import shutil
import time
import unicodedata
from urllib.parse import unquote, urlparse

from .store import ReadingError

MAX_BYTES = 64 * 1024 * 1024
DOWNLOAD_SECONDS = 300          # a whole download, so a server that drips bytes cannot hang /read open
MAX_NAME_BYTES = 180            # download_file's cap: a long percent-encoded title is no file name
HEADERS = {"Accept": "*/*", "Accept-Encoding": "identity",
           "User-Agent": "misaka-reading/1.0 (+https://github.com/Luciole-Studio/Misaka-Agent)"}


def is_url(text):
    return bool(re.match(r"https?://", text, re.IGNORECASE))


def safe_name(name):
    """A file name that keeps its letters (any script), drops control characters and leading or
    trailing dots and spaces, and fits ``MAX_NAME_BYTES`` without losing its suffix."""
    kept = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C" and ch not in '\\/:*?"<>|').strip(" .")
    if len(kept.encode()) <= MAX_NAME_BYTES:
        return kept
    stem, suffix = os.path.splitext(kept)
    if len(suffix.encode()) > 16:     # a dotted name, not a suffix
        stem, suffix = kept, ""
    room = max(MAX_NAME_BYTES - len(suffix.encode()), 1)
    return stem.encode()[:room].decode(errors="ignore").rstrip(" .") + suffix


def unique_path(path):
    """``path``, or ``name-2.ext``, ``name-3.ext`` ... when it is taken: a copy never overwrites."""
    stem, suffix = os.path.splitext(path)
    candidate, n = path, 1
    while os.path.exists(candidate):
        n += 1
        candidate = f"{stem}-{n}{suffix}"
    return candidate


def inbox(workspace, directory):
    from misaka.core.documents import index as corpus
    from misaka.core.tools.path_utils import DOWNLOAD_DIR_NAME

    return (os.path.join(directory, "sources") if corpus.under(directory, workspace)
            else os.path.join(workspace, DOWNLOAD_DIR_NAME))


async def add(source, workspace, directory):
    """Index one material for a reading. Returns its record -- ``doc_id``, ``title``, ``pages``,
    ``outline`` and ``origin``, where it came from (the URL, the file outside the project, or the
    file itself) -- and the copy's path (None for a file indexed where it is)."""
    from misaka.core.documents import index as corpus

    origin = None
    if is_url(source):
        path, origin = await _download(source, inbox(workspace, directory)), source
    else:
        path = os.path.abspath(os.path.expanduser(source))
        if not os.path.isfile(path):
            raise ReadingError(f"No file at {source}.")
        if not corpus.under(path, workspace):
            target = inbox(workspace, directory)
            os.makedirs(target, exist_ok=True)
            copy = unique_path(os.path.join(target, safe_name(os.path.basename(path)) or "material"))
            await asyncio.to_thread(shutil.copyfile, path, copy)   # the content, not a read-only flag
            path, origin = copy, path
    try:
        doc_id, pages = await asyncio.to_thread(corpus.ingest, path, None, True, None, workspace)
    except ValueError as error:
        raise ReadingError(f"{source}: {error}") from error
    row = corpus.doc_row(doc_id, workspace=workspace) or {}
    record = {"doc_id": doc_id, "title": row.get("title") or os.path.basename(path), "pages": int(pages),
              "outline": bool(row.get("has_tree")), "origin": origin or path}
    return record, (path if origin else None)


async def _download(url, target):
    from misaka.core.documents import index as corpus
    from misaka.core.web.bounded import open_checked_stream, read_bounded

    deadline = time.monotonic() + DOWNLOAD_SECONDS
    try:
        async with asyncio.timeout(DOWNLOAD_SECONDS), open_checked_stream(
                url, headers=HEADERS, service="reading_material") as response:
            if response.status_code >= 400:
                raise ReadingError(f"{url} answered HTTP {response.status_code}.")
            body, truncated = await read_bounded(response, MAX_BYTES, deadline=deadline)
            final, kind = str(response.url), response.headers.get("content-type", "").split(";")[0].strip()
    except ReadingError:
        raise
    except TimeoutError as error:
        raise ReadingError(f"{url} took longer than {DOWNLOAD_SECONDS} s; download it yourself and open the "
                           "file instead.") from error
    except Exception as error:
        raise ReadingError(f"Could not download {url}: {error}") from error
    if truncated:
        raise ReadingError(f"{url} is larger than 64 MB; download it yourself and open the file instead.")
    name = safe_name(unquote(os.path.basename(urlparse(final).path))) or "material"
    if os.path.splitext(name)[1].lower() not in corpus.READABLE_SUFFIXES:
        guessed = mimetypes.guess_extension(kind) if kind else None
        if guessed not in corpus.READABLE_SUFFIXES:
            raise ReadingError(f"{url} does not name a format the index can read ({kind or 'no content type'}).")
        name += guessed
    os.makedirs(target, exist_ok=True)
    path = unique_path(os.path.join(target, name))
    await asyncio.to_thread(_write, path, body)
    return path


def _write(path, body):
    with open(path, "wb") as handle:
        handle.write(body)

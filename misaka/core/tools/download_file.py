"""Bounded, type-checked download of one remote file into the workspace.

Everything about a download is chosen by the far end: how many bytes arrive, what the
bytes are, and what the file wants to be called. Each of those is an attack, so each
gets an answer here -- a byte ceiling enforced twice (declared, then counted), a magic
check that the bytes are the type the name claims, and a filename rebuilt from
scratch rather than trusted. A download that fails any of them leaves no file behind:
a half-written or disguised artifact on disk is worse than no artifact, because the
next tool to read it has no way to tell.

The network side is not reimplemented: :func:`open_checked_stream` is the one place
in MISAKA that vets an outbound URL, and it stays that way.

A download that lands in a format the corpus can read is also indexed on arrival, so
the file and the document it becomes are one step rather than two. Ingestion is the
only part of this module that may fail without failing the download: the bytes are
already on disk and verified by then, and a scanned PDF is worth keeping so it can be
OCR'd -- the refusal is reported and the file stays.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import mimetypes
import os
import re
import tempfile
import time
import unicodedata
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.documents import index as corpus
from misaka.core.documents.prompt import (
    MATERIAL_REUSE_GUIDELINE,
    QUOTATION_LOCATOR_GUIDELINE,
)
from misaka.core.extensions.types import ToolDefinition
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.tools._common import run_with_abort
from misaka.core.tools.path_utils import DOWNLOAD_DIR_NAME, resolveToCwd
from misaka.core.web.bounded import UnsafeUrlError, open_checked_stream
from misaka.core.web.evidence import citable_url
from misaka.core.web.screening import screen_url
from misaka.utils.async_lifecycle import run_in_thread
from misaka.utils.values import signal_aborted

MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024

_STREAM_TIMEOUT = 60.0

_MAGIC_BYTES = 512

_MAX_NAME_BYTES = 180

_MAX_COLLISIONS = 1000

_REQUEST_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "User-Agent": "misaka-download/1.0",
}

_DISPOSITION_NAME = re.compile(r"""filename\*?=(?:UTF-8''|["']?)([^;"']+)""", re.IGNORECASE)

_TYPE_JUNK = re.compile(r"[^A-Za-z0-9._+/-]")

_ZIP_HEADS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OLE_HEADS = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)

_SIGNATURES: dict[str, tuple[int, tuple[bytes, ...]]] = {
    ".pdf": (0, (b"%PDF-",)),
    ".djvu": (0, (b"AT&TFORM",)),
    ".gz": (0, (b"\x1f\x8b",)),
    ".png": (0, (b"\x89PNG\r\n\x1a\n",)),
    ".jpg": (0, (b"\xff\xd8\xff",)),
    ".jpeg": (0, (b"\xff\xd8\xff",)),
    ".gif": (0, (b"GIF87a", b"GIF89a")),
    ".webp": (8, (b"WEBP",)),
    ".tar": (257, (b"ustar",)),
    **dict.fromkeys([".zip", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub"], (0, _ZIP_HEADS)),
    **dict.fromkeys([".doc", ".xls", ".ppt"], (0, _OLE_HEADS)),
}

_TEXT_SUFFIXES = frozenset({
    ".bib", ".csv", ".htm", ".html", ".json", ".jsonl", ".log", ".md", ".ndjson",
    ".rst", ".svg", ".tex", ".tsv", ".txt", ".xml", ".yaml", ".yml",
})

_ACCEPTED_SUFFIXES = frozenset(_SIGNATURES) | _TEXT_SUFFIXES

_DECLARED_SUFFIXES = {"image/vnd.djvu": ".djvu", "image/x-djvu": ".djvu"}


class DownloadFileToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = Field(description="Public http(s) URL of the file to download")
    path: str = Field(
        default="",
        description=(
            "Optional filename to save as. Directory components are ignored — downloads "
            f"always land in the workspace {DOWNLOAD_DIR_NAME}/ directory."
        ),
    )


class _Refused(Exception):
    """A download stopped on purpose; the message is what the model is told."""


def _result(text: str, details: dict[str, Any] | None = None, *, is_error: bool = True) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=text)], details={**(details or {}), "isError": is_error})


def _sanitize_name(candidate: str) -> str:
    """Strip a caller- or server-supplied name down to a harmless basename.

    Traversal (``../../etc/passwd``), absolute paths, Windows separators, and the bare
    ``.``/``..`` names all collapse to their final component, which for the dot names
    is empty. Unicode category ``C`` covers NUL and the rest of the control range plus
    the invisible formatting characters -- the bidi overrides are the interesting ones,
    since one of those makes a ``.exe`` render as a ``.pdf`` in every log line and UI
    that shows the name.
    Non-ASCII letters survive: a Chinese paper title is a legitimate filename, and
    nothing downstream shells out to a path.
    """
    name = PurePosixPath(candidate.replace("\\", "/")).name
    name = "".join(char for char in name if unicodedata.category(char)[0] != "C")
    return name.strip().strip(".").strip()


def _fit_name(name: str) -> str:
    """Cap *name* at the byte limit without losing its suffix.

    The suffix decides both the accepted-type check and which signature is required,
    so truncating it would silently turn a checked type into an unchecked one.
    """
    if not name:
        return "download"
    if len(name.encode()) <= _MAX_NAME_BYTES:
        return name
    stem, suffix = os.path.splitext(name)
    if len(suffix.encode()) > 16:
        stem, suffix = name, ""
    room = max(_MAX_NAME_BYTES - len(suffix.encode()), 1)
    stem = stem.encode()[:room].decode("utf-8", errors="ignore")
    return (stem or "download") + suffix


def _disposition_name(headers: httpx.Headers) -> str:
    match = _DISPOSITION_NAME.search(headers.get("content-disposition", ""))
    return unquote(match.group(1)) if match else ""


def _target_name(requested: str, final_url: str, headers: httpx.Headers, content_type: str) -> str:
    """The basename to save under: caller's choice, then the server's, then the URL's."""
    candidate = _sanitize_name(requested)
    if not candidate:
        candidate = _sanitize_name(_disposition_name(headers))
    if not candidate:
        candidate = _sanitize_name(unquote(PurePosixPath(urlsplit(final_url).path).name))
    if not candidate:
        candidate = "download"
    if os.path.splitext(candidate)[1].lower() not in _ACCEPTED_SUFFIXES:
        guessed = (_DECLARED_SUFFIXES.get(content_type) or mimetypes.guess_extension(content_type)
                   or "").lower() if content_type else ""
        if guessed in _ACCEPTED_SUFFIXES:
            candidate += guessed
    return _fit_name(candidate)


def _magic_mismatch(suffix: str, head: bytes) -> bool:
    """Whether the leading bytes contradict what *suffix* claims the file is."""
    entry = _SIGNATURES.get(suffix)
    if entry is None:
        import codecs
        encoding = ("utf-32" if head.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)) else
                    "utf-16" if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else None)
        if encoding:
            try:
                return "\x00" in codecs.getincrementaldecoder(encoding)().decode(head, final=False)
            except UnicodeError:
                return True
        return b"\x00" in head
    offset, prefixes = entry
    return not any(head[offset : offset + len(prefix)] == prefix for prefix in prefixes)


def _declared_length(headers: httpx.Headers) -> int | None:
    raw = headers.get("content-length", "").strip()
    if not (raw.isascii() and raw.isdigit()):
        return None
    return int(raw)


def _publish(part: str, directory: str, name: str) -> str:
    """Publish a complete file atomically without replacing an existing download."""
    os.chmod(part, 0o644)
    stem, suffix = os.path.splitext(name)
    for index in range(_MAX_COLLISIONS):
        path = os.path.join(directory, name if index == 0 else f"{stem}-{index}{suffix}")
        try:
            os.link(part, path)
        except FileExistsError:
            continue
        except OSError as error:
            if (error.errno not in {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}
                    and getattr(error, "winerror", None) not in (1, 50)):
                raise
            try:
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
            except FileExistsError:
                continue
            os.replace(part, path)
            return path
        _discard(part)
        return path
    raise _Refused(f"Could not find a free filename for {name} after {_MAX_COLLISIONS} tries.")


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


async def _stream_to_disk(
    response: httpx.Response, handle: Any, deadline: float, signal: Any
) -> tuple[int, str, bytes]:
    """Write the body to *handle*, returning ``(bytes, sha256, head)``.

    The counted total is the authority on size: a Content-Length is a claim, and a
    chunked response makes no claim at all. The caller owns staging-file cleanup
    until publication, including cancellation during connection teardown.
    """
    digest = hashlib.sha256()
    total = 0
    head = b""
    with handle:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise _Refused(
                    f"Download aborted: the file is larger than the {MAX_DOWNLOAD_BYTES} byte limit "
                    "(the server understated or did not declare its size). The partial file was "
                    "deleted. Find a smaller source, or fetch only the part you need."
                )
            if signal_aborted(signal):
                raise RuntimeError("Operation aborted")
            if time.monotonic() > deadline:
                raise _Refused(
                    f"Download exceeded its transfer deadline with {total} bytes received. "
                    "The partial file was deleted. Try a faster mirror."
                )
            handle.write(chunk)
            digest.update(chunk)
            if len(head) < _MAGIC_BYTES:
                head += chunk[: _MAGIC_BYTES - len(head)]
    return total, digest.hexdigest(), head


async def _index_in_corpus(path: str, workspace: str) -> tuple[str, str | None]:
    """Index a downloaded document into the corpus; return ``(line for the model, doc_id)``.

    Without this the download has no usable next step: the read tool understands text and
    images only, so a downloaded PDF read as text is bytes of noise, and ``doc_add`` -- the
    step that actually works -- is something the model has to already know to look for.
    Indexing here makes the download and the corpus entry one motion.

    The project-local store deduplicates identical documents without moving the download.
    """
    try:
        doc_id, pages = await run_in_thread(corpus.ingest, path, workspace=workspace)
    except Exception as error:  # noqa: BLE001 - the file is downloaded, verified and kept; indexing is the step after, and its failures may damage nothing but themselves
        reason = str(error) or type(error).__name__
        return f"  not indexed: {reason}", None
    note = (f"  indexed as doc {doc_id} ({pages} pages) — navigate it with doc_outline / doc_read, "
            "and use doc_verify when you need help locating a passage")
    return note, doc_id


async def _download(url: str, requested: str, directory: str, signal: Any, workspace: str) -> AgentToolResult:
    from misaka.core.web.timeouts import operation_seconds

    if not corpus.under(directory, workspace):
        raise _Refused("Download directory resolves outside the workspace; nothing was written.")
    part = None
    try:
        seconds = operation_seconds("download_transfer")
        if seconds == 0:
            raise _Refused("Download transfer deadline is 0s; no request started.")
        deadline = time.monotonic() + seconds
        async with (
            asyncio.timeout(seconds),
            open_checked_stream(url, headers=_REQUEST_HEADERS, timeout=_STREAM_TIMEOUT, service="download_file") as response,
        ):
            if response.status_code >= 400:
                return _result(
                    f"Download failed: the server answered HTTP {response.status_code} for {url}. "
                    "Check the URL, or find the file at another source."
                )
            content_type = _TYPE_JUNK.sub("", response.headers.get("content-type", "").split(";", 1)[0].lower())[:80]

            declared = _declared_length(response.headers)
            if declared is not None and declared > MAX_DOWNLOAD_BYTES:
                return _result(
                    f"Refused to download {url}: the server declares {declared} bytes, over the "
                    f"{MAX_DOWNLOAD_BYTES} byte limit. Nothing was transferred. Find a smaller source."
                )

            name = _target_name(requested, str(response.url), response.headers, content_type)
            suffix = os.path.splitext(name)[1].lower()
            if suffix not in _ACCEPTED_SUFFIXES:
                return _result(
                    f"Refused to download {url}: {suffix[:20] or '(no extension)'} is not a downloadable type "
                    f"(declared type {content_type or 'unknown'}). Documents, text and data files, images, "
                    "and archives are allowed. Use web_fetch to read a web page instead."
                )

            os.makedirs(directory, exist_ok=True)
            descriptor, part = tempfile.mkstemp(dir=directory, prefix=".partial-")
            total, sha256, head = await _stream_to_disk(
                response, os.fdopen(descriptor, "wb"), deadline, signal
            )

        if _magic_mismatch(suffix, head):
            return _result(
                f"Refused to keep the download from {url}: its first bytes are not {suffix} content "
                f"(the server sent {content_type or 'an unknown type'} — usually a paywall, login, or "
                "error page). The file was deleted. Open the URL's landing page with web_fetch to find "
                "the real file link."
            )

        path = _publish(part, directory, name)
        part = None
    finally:
        if part is not None:
            _discard(part)

    final_url = citable_url(str(response.url))
    details = {
        "url": url,
        "final_url": final_url,
        "path": path,
        "sha256": sha256,
        "bytes": total,
        "content_type": content_type,
    }
    lines = [
        f"Downloaded {url}",
        f"  saved to: {path}",
        f"  {total} bytes, type {content_type or 'unknown'}",
        f"  sha256: {sha256}",
    ]
    if final_url != citable_url(url):
        lines.insert(1, f"  redirected to: {final_url}")

    ingestable = suffix in corpus.SCAN_SUFFIXES
    doc_id = None
    if ingestable:
        note, doc_id = await _index_in_corpus(path, workspace)
        lines.append(note)
        if doc_id:
            details["doc_id"] = doc_id
    if doc_id:
        lines.append("The file is on disk, not in this result — work with it through the doc tools above.")
    elif ingestable:
        lines.append("The file is on disk and was kept. Fix what the line above reports, then index it with doc_add.")
    else:
        lines.append("The file is on disk, not in this result — use the read tool to open it.")
    return _result(untrusted("download", "\n".join(lines)), details, is_error=False)


def create_download_file_tool_definition(
    cwd: str,
) -> ToolDefinition[DownloadFileToolInput | dict[str, Any], dict[str, Any] | None]:
    """Build the download_file tool rooted at the workspace *cwd*."""

    async def execute(
        _tool_call_id: str,
        params: DownloadFileToolInput | dict[str, Any],
        signal: Any | None = None,
        _on_update: Callable[[AgentToolResult], None] | None = None,
        _ctx: Any = None,
    ) -> AgentToolResult:
        parsed = (
            params
            if isinstance(params, DownloadFileToolInput)
            else DownloadFileToolInput.model_validate(params or {})
        )
        url = parsed.url.strip()
        if not url:
            return _result("download_file needs a URL. Call it again with the http(s) address of the file.")
        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")

        screened = screen_url(url)
        if not screened.allowed:
            return _result(screened.refusal)
        url = screened.url

        directory = resolveToCwd(DOWNLOAD_DIR_NAME, cwd)
        try:
            result, aborted = await run_with_abort(
                _download(url, parsed.path.strip(), directory, signal, cwd), signal
            )
            if aborted:
                raise RuntimeError("Operation aborted")
            return result
        except _Refused as refusal:
            return _result(str(refusal))
        except UnsafeUrlError as error:
            return _result(
                f"Refused to download {url}: {error}. Downloads may only target public internet "
                "addresses. Do not retry this URL."
            )
        except httpx.TooManyRedirects:
            return _result(
                f"Refused to download {url}: it redirects too many times. The link is probably "
                "broken or a redirect loop; find the file at another source."
            )
        except (TimeoutError, httpx.TimeoutException):
            return _result(
                f"Download of {url} timed out. Retry once, or find a faster mirror."
            )
        except httpx.HTTPError as error:
            return _result(
                f"Download of {url} failed ({type(error).__name__}). The network may be down; "
                "retry once, then continue without the file."
            )
        except OSError as error:
            return _result(
                f"Download of {url} could not be written to {directory} ({error.strerror or error}). "
                "Check the workspace has space and is writable."
            )

    return ToolDefinition(
        name="download_file",
        label="download file",
        description=(
            "Download one file (PDF, dataset, document, image, or archive) from a public URL into "
            f"the workspace {DOWNLOAD_DIR_NAME}/ directory, and report where it landed with its "
            "sha256. A PDF, Markdown, or text file is submitted for indexing on arrival; the "
            "receipt reports its document ID or an indexing failure without discarding the download. "
            "Size-capped, type-checked, and refused for private addresses. Prefer it for retained "
            "files; use an available page reader for web pages."
        ),
        promptSnippet="Download a paper, dataset, or document to the workspace.",
        promptGuidelines=[
            ("Use download_file for files worth keeping (papers, datasets); it saves them without "
             "putting the content in your context. A downloaded PDF, Markdown, or text file is "
             "submitted for indexing on arrival; check the receipt for a document ID or indexing failure. "
             "Read indexed documents with doc_outline/doc_read when available. The read tool handles text, "
             "images and Office files, not PDFs; use an available PDF reader for an unindexed PDF."),
            MATERIAL_REUSE_GUIDELINE,
            QUOTATION_LOCATOR_GUIDELINE,
        ],
        parameters=DownloadFileToolInput,
        execute=execute,
    )


__all__ = [
    "DOWNLOAD_DIR_NAME",
    "MAX_DOWNLOAD_BYTES",
    "DownloadFileToolInput",
    "create_download_file_tool_definition",
]

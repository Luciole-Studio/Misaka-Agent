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
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._web.bounded import UnsafeUrlError, open_checked_stream
from misaka.core.tools.path_utils import resolve_to_cwd
from misaka.documents import index as corpus
from misaka.platform import budget
from misaka.platform.prompt_guard import untrusted
from misaka.utils.values import signal_aborted

#: Ceiling on one downloaded file. Sized for what research actually pulls -- a paper
#: PDF is single-digit MB, a supplementary dataset tens -- while staying small enough
#: that a runaway transfer cannot fill a laptop's disk.
#: ponytail: a constant, not configuration. Nobody has needed a second value; the
#: upgrade path when someone does is one env read here, not a settings key.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024

#: Downloads land here, under the workspace, and nowhere else.
DOWNLOAD_DIR_NAME = "downloads"

# Wall-clock ceiling for the whole transfer. The per-operation timeout below only
# bounds a *stall*; a server that dribbles bytes forever passes it indefinitely.
_TOTAL_TIMEOUT = 300.0
_STREAM_TIMEOUT = 60.0

# Bytes kept for the signature check. 512 covers the deepest signature we look at
# (tar's, at offset 257).
_MAGIC_BYTES = 512

# Filename length cap, in *bytes*: the common filesystem limit is 255 bytes, not
# characters, and a CJK name costs three bytes each.
_MAX_NAME_BYTES = 180

# Collision suffixes tried before giving up. A workspace with a thousand copies of one
# filename has a different problem than this tool can solve.
_MAX_COLLISIONS = 1000

_REQUEST_HEADERS = {
    "Accept": "*/*",
    # Identity encoding keeps the declared Content-Length comparable with the bytes we
    # count: under gzip the two measure different things, and the check that the server
    # told the truth about size would compare a compressed number with a raw one.
    "Accept-Encoding": "identity",
    "User-Agent": "misaka-download/1.0",
}

_DISPOSITION_NAME = re.compile(r"""filename\*?=(?:UTF-8''|["']?)([^;"']+)""", re.IGNORECASE)

# A Content-Type is free text from the far end and it ends up in messages the model
# reads, so it is reduced to the characters a media type is actually made of.
_TYPE_JUNK = re.compile(r"[^A-Za-z0-9._+/-]")

_ZIP_HEADS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OLE_HEADS = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)

# suffix -> (offset, acceptable byte sequences at that offset). Membership is also the
# "we can verify this type" rule: a binary type with no entry cannot be checked, so it
# is not accepted at all.
_SIGNATURES: dict[str, tuple[int, tuple[bytes, ...]]] = {
    ".pdf": (0, (b"%PDF-",)),
    ".gz": (0, (b"\x1f\x8b",)),
    ".png": (0, (b"\x89PNG\r\n\x1a\n",)),
    ".jpg": (0, (b"\xff\xd8\xff",)),
    ".jpeg": (0, (b"\xff\xd8\xff",)),
    ".gif": (0, (b"GIF87a", b"GIF89a")),
    # RIFF alone is also AVI and WAV; the format tag at offset 8 is what makes it WebP.
    ".webp": (8, (b"WEBP",)),
    # ustar lives at offset 257, so a file too short to hold one is not a tar.
    ".tar": (257, (b"ustar",)),
    **dict.fromkeys([".zip", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub"], (0, _ZIP_HEADS)),
    **dict.fromkeys([".doc", ".xls", ".ppt"], (0, _OLE_HEADS)),
}

# Text-shaped payloads, which no signature identifies. They get the one check that
# still means something: text does not contain NUL, so a binary blob wearing a .csv
# name is caught even though a genuine .csv has nothing to match against.
_TEXT_SUFFIXES = frozenset({
    ".bib", ".csv", ".htm", ".html", ".json", ".jsonl", ".log", ".md", ".ndjson",
    ".rst", ".svg", ".tex", ".tsv", ".txt", ".xml", ".yaml", ".yml",
})

_ACCEPTED_SUFFIXES = frozenset(_SIGNATURES) | _TEXT_SUFFIXES


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


def _result(text: str, details: dict[str, Any] | None = None) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=text)], details=details)


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
    # Leading dots would make the download invisible to ls and to the model.
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
    if len(suffix.encode()) > 16:  # a dotted filename, not an extension
        stem, suffix = name, ""
    room = max(_MAX_NAME_BYTES - len(suffix.encode()), 1)
    # errors="ignore" drops a codepoint cut in half by the byte slice.
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
        # No usable extension: take one from the declared type, so a URL like
        # /article/12345 served as application/pdf still lands as a .pdf and still
        # gets signature-checked. A wrong declaration only renames the file -- the
        # signature check below is what decides whether it is kept.
        guessed = (mimetypes.guess_extension(content_type) or "").lower() if content_type else ""
        if guessed in _ACCEPTED_SUFFIXES:
            candidate += guessed
    return _fit_name(candidate)


def _magic_mismatch(suffix: str, head: bytes) -> bool:
    """Whether the leading bytes contradict what *suffix* claims the file is."""
    entry = _SIGNATURES.get(suffix)
    if entry is None:
        return b"\x00" in head
    offset, prefixes = entry
    return not any(head[offset : offset + len(prefix)] == prefix for prefix in prefixes)


def _declared_length(headers: httpx.Headers) -> int | None:
    raw = headers.get("content-length", "").strip()
    # ``isascii`` before ``isdigit``: the latter is true of superscripts and other
    # non-decimal Unicode digits that ``int`` then refuses, which would turn a header
    # the far end chose into an exception escaping this tool.
    if not (raw.isascii() and raw.isdigit()):
        return None
    return int(raw)


def _publish(part: str, directory: str, name: str) -> str:
    """Move the finished, verified file from *part* to a free name, and return it.

    Nothing is visible under a real download name until this runs: a name in
    ``downloads/`` is a contract that the file is complete and is the type it claims,
    and a reader has no way to tell a still-streaming or still-unchecked file from a
    finished one. ``O_EXCL`` makes "is this name free?" and the claim on the answer one
    syscall, so two downloads racing on one name get two files rather than one
    clobbered by the other, and ``os.replace`` onto the name this call just created is
    atomic -- no lock involved.
    """
    stem, suffix = os.path.splitext(name)
    for index in range(_MAX_COLLISIONS):
        path = os.path.join(directory, name if index == 0 else f"{stem}-{index}{suffix}")
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError:
            continue
        os.replace(part, path)
        # mkstemp creates 0600; a download is an ordinary workspace file, and the
        # mode came from the staging file rather than from any decision about it.
        os.chmod(path, 0o644)
        return path
    raise _Refused(f"Could not find a free filename for {name} after {_MAX_COLLISIONS} tries.")


def _provenance(url: str) -> str:
    """A URL fit to put in front of the model and into the ledger.

    The query is dropped because the *final* URL of a redirect chain is chosen by the
    server, not by the caller, and the redirect that ends at a CDN commonly ends at a
    presigned one -- ``?X-Amz-Signature=...`` is a live credential the model never saw
    and must not be handed, still less written to a ledger that gets exported. The
    path is what identifies the object, and the sha256 beside it is what locks the
    evidence. The *requested* URL is left alone: the caller wrote it, already has it,
    and its query is often the only thing that names the document.
    """
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass  # nothing to undo if the partial file is already gone


async def _stream_to_disk(
    response: httpx.Response, path: str, handle: Any, deadline: float, signal: Any
) -> tuple[int, str, bytes]:
    """Write the body to *handle*, returning ``(bytes, sha256, head)``.

    The counted total is the authority on size: a Content-Length is a claim, and a
    chunked response makes no claim at all. Any exit other than a completed body
    removes the file, cancellation included -- the caller must never find a partial.
    """
    digest = hashlib.sha256()
    total = 0
    head = b""
    try:
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
                    # Checked per chunk, not just before the request: this is the one tool
                    # that can legitimately run for minutes, and an abort the caller has
                    # already issued must not keep a transfer alive behind their back.
                    raise RuntimeError("Operation aborted")
                if time.monotonic() > deadline:
                    raise _Refused(
                        f"Download aborted after {_TOTAL_TIMEOUT:g}s with {total} bytes received. "
                        "The partial file was deleted. Try a faster mirror."
                    )
                handle.write(chunk)
                digest.update(chunk)
                if len(head) < _MAGIC_BYTES:
                    head += chunk[: _MAGIC_BYTES - len(head)]
    except BaseException:  # every exit that is not a completed body, cancellation included
        _discard(path)
        raise
    return total, digest.hexdigest(), head


async def _index_in_corpus(path: str) -> tuple[str, str | None]:
    """Index a downloaded document into the corpus; return ``(line for the model, doc_id)``.

    Without this the download has no usable next step: the read tool understands text and
    images only, so a downloaded PDF read as text is bytes of noise, and ``doc_add`` -- the
    step that actually works -- is something the model has to already know to look for.
    Indexing here makes the download and the corpus entry one motion.

    The ingest is content-addressed under the corpus root, outside the workspace, and a
    second download of the same bytes links the existing document rather than writing a
    second one -- so this adds no file to the workspace whose name is not already a
    function of what is in it.
    """
    try:
        # pdftotext runs under a 300s timeout inside ingest, and page files are written one
        # by one: on the event loop that is the whole session held still for minutes.
        doc_id, pages = await asyncio.to_thread(corpus.ingest, path)
    except Exception as error:  # noqa: BLE001 - the file is downloaded, verified and kept; indexing is the step after, and its failures may damage nothing but themselves
        # ValueError is ingest's documented refusal (a scanned PDF with no text layer), and
        # its text is exactly the instruction the model needs -- OCR it, then doc_add. Any
        # other failure is reported the same way rather than swallowed: the file is on disk
        # either way, and a download that succeeded must not be reported as one that failed.
        reason = str(error) or type(error).__name__  # some OSErrors carry no message at all
        return f"  not indexed: {reason}", None
    note = (f"  indexed as doc {doc_id} ({pages} pages) — navigate it with doc_outline / doc_read, "
            "and doc_verify every quotation before you cite it")
    return note, doc_id


async def _download(url: str, requested: str, directory: str, signal: Any) -> AgentToolResult:
    deadline = time.monotonic() + _TOTAL_TIMEOUT
    async with open_checked_stream(url, headers=_REQUEST_HEADERS, timeout=_STREAM_TIMEOUT) as response:
        if response.status_code >= 400:
            return _result(
                f"Download failed: the server answered HTTP {response.status_code} for {url}. "
                "Check the URL, or find the file at another source."
            )
        content_type = _TYPE_JUNK.sub("", response.headers.get("content-type", "").split(";", 1)[0].lower())[:80]

        declared = _declared_length(response.headers)
        if declared is not None and declared > MAX_DOWNLOAD_BYTES:
            # Refused before a single body byte is read: the stream is still open, and
            # leaving this context closes the connection.
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
        # Streamed into a dotted staging name, never straight to the destination: until
        # the byte count and the signature have both passed there is nothing here worth
        # a real filename, and a process killed mid-transfer must not leave a truncated
        # file sitting under one.
        descriptor, part = tempfile.mkstemp(dir=directory, prefix=".partial-")
        total, sha256, head = await _stream_to_disk(
            response, part, os.fdopen(descriptor, "wb"), deadline, signal
        )

    # Accounted on the transfer, not on the keep: the bytes below may yet be thrown away
    # for wearing the wrong signature, and they cost the same either way.
    budget.record_external_call("download_file", subject=url, bytes=total)

    if _magic_mismatch(suffix, head):
        # The overwhelmingly common case is a paywall or login page served with a 200
        # in place of the PDF. Keeping the file would put an HTML page into the ledger
        # under a paper's name, so it is deleted and the failure is reported as one.
        _discard(part)
        # The name is deliberately not echoed here: it is the server's text, and this
        # message is one of the few that reach the model outside the untrusted fence.
        return _result(
            f"Refused to keep the download from {url}: its first bytes are not {suffix} content "
            f"(the server sent {content_type or 'an unknown type'} — usually a paywall, login, or "
            "error page). The file was deleted. Open the URL's landing page with web_fetch to find "
            "the real file link."
        )

    try:
        path = _publish(part, directory, name)
    except BaseException:  # a name that cannot be claimed leaves no staging file behind
        _discard(part)
        raise

    final_url = _provenance(str(response.url))
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
    if final_url != _provenance(url):
        # Compared query-free on both sides: a request URL that carries its own query
        # is not a redirect, and reporting one would be a lie about where this came from.
        lines.insert(1, f"  redirected to: {final_url}")

    # What the corpus can extract text from is defined there, not here: a format added to
    # SCAN_SUFFIXES starts being indexed on arrival without a second edit in this file.
    ingestable = suffix in corpus.SCAN_SUFFIXES
    doc_id = None
    if ingestable:
        note, doc_id = await _index_in_corpus(path)
        lines.append(note)
        if doc_id:
            details["doc_id"] = doc_id
    if doc_id:
        lines.append("The file is on disk, not in this result — work with it through the doc tools above.")
    elif ingestable:
        # Telling the model to "read" an unindexed PDF is what this tool used to do, and it
        # is the one instruction that cannot work; the reason above is the actionable one.
        lines.append("The file is on disk and was kept. Fix what the line above reports, then index it with doc_add.")
    else:
        lines.append("The file is on disk, not in this result — use the read tool to open it.")
    # The URL, the server's filename, and the declared type are all written by the far
    # end, so the block goes to the model fenced as data like every other tool's.
    return _result(untrusted("download", "\n".join(lines)), details)


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

        directory = resolve_to_cwd(DOWNLOAD_DIR_NAME, cwd)
        try:
            return await _download(url, parsed.path.strip(), directory, signal)
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
        except httpx.TimeoutException:
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
            "sha256. A PDF, Markdown, or text file is indexed into the document store on arrival "
            "and comes back with its document ID. Size-capped, type-checked, and refused for "
            "private addresses. Use it instead of curl; use web_fetch for web pages you want to read."
        ),
        promptSnippet="Download a paper, dataset, or document to the workspace.",
        promptGuidelines=[
            ("Use download_file for files worth keeping (papers, datasets); it saves them without "
             "putting the content in your context. A downloaded PDF, Markdown, or text file is "
             "indexed on arrival: work with it through doc_outline / doc_read / doc_verify, which "
             "is also what makes it citable. The read tool understands only text and images, so it "
             "cannot open a PDF; use it for the other downloaded types."),
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

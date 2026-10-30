"""The reading engine's only door to source material.

The Node engine never opens a material file. It asks for ``source_add/read/find/outline``
over the line protocol, and this module answers with the repository's own corpus functions
(``misaka.core.documents.index``): one extractor, one index, one set of page markers.

A file already inside the workspace is indexed where it sits. A single file outside it, or
a URL, is first copied/downloaded into the run's ``sources/`` folder -- the original is
never moved or rewritten, and its path or URL is remembered as the source's origin. When the
runs tree itself is outside the workspace, those copies join a second corpus at the runs
root (the project's corpus only holds paths under the workspace); both stay readable here.

Nothing here writes credentials or model output. Downloaded bytes are bounded, vetted by
the same SSRF-checked stream every other outbound fetch uses, and never decoded by this
module: the corpus does that, once, the way it does for ``misaka doc add``.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shutil
import unicodedata
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from misaka.core.documents import index as corpus
from misaka.core.web.bounded import open_checked_stream, read_bounded

#: One material's ceiling. A book-sized PDF is single-digit MB; this leaves ample room
#: for a scanned monographic chapter while keeping a runaway transfer off the disk.
MAX_MATERIAL_BYTES = 64 * 1024 * 1024

#: ``read_pages`` appends this note when the window ends before the selected pages do.
#: The offset is reported structurally instead (``truncated``/``nextOffset``), so the note
#: itself is cut from the text rather than asked for twice.
_CONTINUE = re.compile(r"\n… \(more; call again with offset=(\d+) to continue\)\s*\Z")

_URL_SCHEMES = ("http://", "https://")

#: Sent with every material download. Wikimedia (and others) answer 403 to clients that
#: give no contact address, so the project URL is part of the agent string rather than an
#: optional nicety; identity encoding keeps a declared Content-Length comparable with the
#: bytes counted here.
_REQUEST_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "User-Agent": "misaka-reading/1.0 (+https://github.com/Luciole-Studio/Misaka-Agent)",
}


class MaterialError(ValueError):
    """A material request this host refuses, with a sentence the reader can act on."""


def is_url(value: str) -> bool:
    return value.lower().startswith(_URL_SCHEMES)


def _safe_name(candidate: str) -> str:
    """A basename safe to join under the run's ``sources/``, or "" when nothing is left.

    Path components, control/invisible characters and leading dots are dropped; non-ASCII
    letters survive because a Chinese title is a legitimate filename.
    """
    name = PurePosixPath(candidate.replace("\\", "/")).name
    name = "".join(char for char in name if unicodedata.category(char)[0] != "C")
    return name.strip().strip(".").strip()


def _suffix(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _unique_path(directory: Path, name: str) -> Path:
    """``directory/name``, or ``name-2.ext`` and so on rather than overwriting a material."""
    path, n = directory / name, 2
    stem, suffix = os.path.splitext(name)
    while path.exists():
        path = directory / f"{stem}-{n}{suffix}"
        n += 1
    return path


def _read_meta(doc_id: str, workspace: str) -> dict:
    """The document's own ``meta.json``: title, page count, original path."""
    directory = corpus.resolve_doc(doc_id, workspace=workspace)
    if not directory:
        raise MaterialError(f"No indexed material {doc_id!r} in this workspace.")
    path = os.path.join(directory, "meta.json")
    try:
        with open(path, encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError) as error:
        raise MaterialError(f"The index for {doc_id!r} is unreadable: {error}") from error
    return meta if isinstance(meta, dict) else {}


def _flatten(tree: list[dict], depth: int = 0) -> list[dict]:
    """PageIndex's nested tree as the flat ``{nodeId,title,pageStart,pageEnd,depth}`` list."""
    out: list[dict] = []
    for node in tree:
        title = str(node.get("title") or "").strip()
        start, end = node.get("start_index"), node.get("end_index")
        out.append(
            {
                "nodeId": str(node.get("node_id") or ""),
                "title": title,
                **({"pageStart": int(start)} if isinstance(start, int) and start > 0 else {}),
                **({"pageEnd": int(end)} if isinstance(end, int) and end > 0 else {}),
                "depth": depth,
            }
        )
        children = node.get("nodes") or []
        if isinstance(children, list):
            out.extend(_flatten(children, depth + 1))
    return out


def _guess_suffix(content_type: str | None, url: str) -> str:
    """The suffix a download should carry: from the declared type, never invented."""
    bare = (content_type or "").split(";", 1)[0].strip().lower()
    guessed = mimetypes.guess_extension(bare) if bare else ""
    if guessed and guessed in corpus.SCAN_SUFFIXES:
        return guessed
    path_suffix = _suffix(PurePosixPath(unquote(urlsplit(url).path)).name)
    return path_suffix if path_suffix in corpus.SCAN_SUFFIXES else ""


class Materials:
    """Corpus access for one reading run, in one workspace."""

    def __init__(
        self,
        workspace: str,
        run_directory: str | None = None,
        *,
        runs_root: str | None = None,
    ) -> None:
        self.workspace = os.path.realpath(os.path.abspath(os.path.expanduser(workspace)))
        self.run_directory = run_directory
        # Where runs are stored. The corpus only ingests a file under the workspace it is
        # given, so a runs tree outside the project needs its own: see ``_import_workspace``.
        self.runs_root = (
            os.path.realpath(os.path.abspath(os.path.expanduser(runs_root)))
            if runs_root
            else None
        )

    def _import_workspace(self) -> str:
        """The workspace a copy under the runs tree is indexed in.

        A runs directory inside the project is covered by the project workspace, so copies
        join the same ``.pageindex`` every ``misaka doc`` uses. A runs directory elsewhere
        gets one corpus at ``<runs-root>/.pageindex``: a copy in ``<run>/sources`` must stay
        under the workspace its corpus is given, and the project's own store cannot hold it.
        """
        for candidate in (self.runs_root, self.run_directory):
            if candidate and not corpus.under(candidate, self.workspace):
                return candidate
        return self.workspace

    def _workspace_for(self, doc_id: str) -> str:
        """The workspace whose corpus owns ``doc_id``; the project's when neither does."""
        if corpus.resolve_doc(doc_id, workspace=self.workspace):
            return self.workspace
        imported = self._import_workspace()
        if imported != self.workspace and corpus.resolve_doc(doc_id, workspace=imported):
            return imported
        return self.workspace

    # -- dispatch ---------------------------------------------------------------

    async def call(self, method: str, params: dict) -> object:
        """Answer one ``source_call``; blocking corpus work stays off the event loop."""
        if method == "source_add":
            return await self.add(params)
        handler = {
            "source_list": self.list,
            "source_outline": self.outline,
            "source_read": self.read,
            "source_find": self.find,
        }.get(method)
        if handler is None:
            raise MaterialError(f"The host has no material operation {method!r}.")
        return await asyncio.to_thread(handler, params)

    # -- operations -------------------------------------------------------------

    async def add(self, params: dict) -> dict:
        """Index one material and return ``{docId, pages, title, origin}``.

        A path inside the workspace is indexed where it is. Anything else is copied into
        this run's ``sources/`` first, so the corpus always owns a path under the
        workspace and the reader's original is untouched.
        """
        path = str(params.get("path") or "").strip()
        if not path:
            raise MaterialError("source_add needs a path or URL.")
        title = str(params.get("title") or "").strip() or None
        run_directory = str(params.get("runDirectory") or self.run_directory or "").strip() or None
        workspace = self.workspace

        if is_url(path):
            local, origin = await self._download(path, run_directory)
            workspace = self._import_workspace()
        else:
            local = os.path.abspath(os.path.expanduser(path))
            if not os.path.isfile(local):
                raise MaterialError(f"No file at {local}.")
            origin = local
            if not corpus.under(local, self.workspace):
                local = await asyncio.to_thread(self._import, local, run_directory)
                workspace = self._import_workspace()

        doc_id, pages = await asyncio.to_thread(
            corpus.ingest, local, title=title, workspace=workspace
        )
        meta = await asyncio.to_thread(_read_meta, doc_id, workspace)
        return {
            "docId": doc_id,
            "pages": int(pages),
            "title": str(meta.get("title") or title or os.path.basename(local)),
            "origin": origin,
        }

    def list(self, _params: dict) -> list[dict]:
        """Every indexed document this run can read, oldest first."""
        out = []
        seen: set[str] = set()
        workspaces = [self.workspace]
        imported = self._import_workspace()
        if imported != self.workspace:
            workspaces.append(imported)
        for workspace in workspaces:
            for meta in corpus.docs(workspace=workspace):
                doc_id = str(meta.get("doc_id") or "")
                if not doc_id or doc_id in seen:
                    continue
                seen.add(doc_id)
                out.append(
                    {
                        "sourceId": doc_id,
                        "docId": doc_id,
                        "title": str(meta.get("title") or doc_id),
                        "pages": int(meta.get("pages") or 0),
                        **({"origin": str(meta["orig_path"])} if meta.get("orig_path") else {}),
                    }
                )
        return out

    def outline(self, params: dict) -> dict:
        """The document's outline when the format has one, and the honest mode when not."""
        doc_id = self._doc_id(params)
        workspace = self._workspace_for(doc_id)
        structure = corpus.structure(doc_id, workspace=workspace)
        if not structure:
            raise MaterialError(f"No indexed material {doc_id!r} in this workspace.")
        title = structure.get("title")
        tree = structure.get("tree")
        if isinstance(tree, list) and tree:
            return {
                "mode": "tree",
                "title": title,
                "nodes": _flatten(tree),
            }
        heads = structure.get("pages") or []
        # `mode` is the corpus's own sentence about why there is no tree; repeating it as a
        # `note` would hand the same text over twice.
        return {
            "mode": str(structure.get("mode") or "pages only"),
            "title": title,
            "pages": len(heads) if isinstance(heads, list) else 0,
        }

    def read(self, params: dict) -> dict:
        """A bounded text window with the corpus's own page markers.

        With ``outlineNode`` the page span comes from the structure tree; ``offset``
        continues a window that stopped at ``maxChars``.
        """
        doc_id = self._doc_id(params)
        workspace = self._workspace_for(doc_id)
        start = params.get("start")
        end = params.get("end")
        outline_node = str(params.get("outlineNode") or "").strip()
        if outline_node:
            span = corpus.node_pages(doc_id, outline_node, workspace=workspace)
            if not span:
                raise MaterialError(
                    f"No section {outline_node!r} in {doc_id}; ask for its outline first."
                )
            start, end = span
        start = max(1, int(start or 1))
        end = int(end or start)
        if end < start:
            start, end = end, start
        offset = max(0, int(params.get("offset") or 0))
        max_chars = min(max(1000, int(params.get("maxChars") or 12000)), 60000)
        text = corpus.read_pages(
            doc_id, start, end, max_chars=max_chars, offset=offset, workspace=workspace
        )
        match = _CONTINUE.search(text)
        next_offset = int(match.group(1)) if match else None
        if match:
            text = text[: match.start()].rstrip()
        return {
            "docId": doc_id,
            "text": text,
            "pageStart": start,
            "pageEnd": end,
            "truncated": next_offset is not None,
            **({"nextOffset": next_offset} if next_offset is not None else {}),
        }

    def find(self, params: dict) -> list[dict]:
        """Literal positions ``{doc_id, page, printed, s}`` for a distinctive phrase."""
        query = str(params.get("query") or params.get("question") or "").strip()
        if not query:
            raise MaterialError("source_find needs a query.")
        doc_id = str(params.get("docId") or "").strip() or None
        limit = min(max(1, int(params.get("limit") or 10)), 50)
        if doc_id:
            return corpus.search_literal(
                query, limit=limit, doc_id=doc_id, workspace=self._workspace_for(doc_id)
            )
        # No docId means the whole corpus, and one corpus cannot see another's documents:
        # a runs tree with its own ``.pageindex`` is searched too, so the materials this
        # run imported are not silently left out.
        hits = corpus.search_literal(query, limit=limit, workspace=self.workspace)
        imported = self._import_workspace()
        if imported != self.workspace and len(hits) < limit:
            seen = {hit["doc_id"] for hit in hits}
            more = corpus.search_literal(query, limit=limit, workspace=imported)
            hits.extend(hit for hit in more if hit["doc_id"] not in seen)
        return hits[:limit]

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    def _doc_id(params: dict) -> str:
        doc_id = str(params.get("docId") or "").strip()
        if not doc_id:
            raise MaterialError("This request needs a docId.")
        return doc_id

    def _sources_directory(self, run_directory: str | None) -> Path:
        if not run_directory:
            raise MaterialError(
                "Material outside the workspace (or a URL) needs a run directory to be "
                "copied into; this run did not name one."
            )
        directory = Path(run_directory) / "sources"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _import(self, source: str, run_directory: str | None) -> str:
        """Copy one outside file into the run's ``sources/``; the original stays put."""
        name = _safe_name(os.path.basename(source)) or "material"
        if _suffix(name) not in corpus.SCAN_SUFFIXES:
            raise MaterialError(
                f"{name}: the corpus has no extractor for {_suffix(name) or 'a file without a suffix'}."
            )
        target = _unique_path(self._sources_directory(run_directory), name)
        shutil.copyfile(source, target)
        return str(target)

    async def _download(self, url: str, run_directory: str | None) -> tuple[str, str]:
        """Fetch one bounded, vetted URL into the run's ``sources/``; returns (path, url)."""
        async with open_checked_stream(
            url, headers=_REQUEST_HEADERS, service="reading_material"
        ) as response:
            if response.status_code >= 400:
                raise MaterialError(f"{url} answered HTTP {response.status_code}.")
            body, truncated = await read_bounded(response, MAX_MATERIAL_BYTES)
            if truncated:
                raise MaterialError(
                    f"{url} is larger than {MAX_MATERIAL_BYTES // (1024 * 1024)} MB; "
                    "download it yourself and open the file instead."
                )
            content_type = response.headers.get("content-type")
            name = _safe_name(
                unquote(PurePosixPath(urlsplit(str(response.url)).path).name)
            ) or "material"
            if _suffix(name) not in corpus.SCAN_SUFFIXES:
                suffix = _guess_suffix(content_type, str(response.url))
                if not suffix:
                    raise MaterialError(
                        f"{url} does not name a format the corpus can read "
                        f"({content_type or 'no content type'})."
                    )
                name = f"{os.path.splitext(name)[0] or 'material'}{suffix}"
        target = _unique_path(self._sources_directory(run_directory), name)
        target.write_bytes(body)
        return str(target), url

"""What a research product rests on, gathered beside it.

Each research card's folder, each node's folder and the run's ``final/`` get two things once
their products are settled: a ``sources/`` folder holding the files those products cite, and a
``SOURCES.md`` saying what is there and why. A cited file is hard-linked from where it already
lives (``downloads/``, another node's folder) and copied only where a hard link is impossible;
the original never moves. Citations come from the ledger (each card's declared findings and
their locators) and from the ``## Sources`` lines and inline locators of the product text, and
they close over references: a conclusion that cites a card's output also gets that card's
sources; a final report that cites a node's conclusion gets that node's.

The bundle is derived state -- rebuilt from scratch on every call, never registered as an
artifact, never indexed, never committed -- so ``sources/`` belongs to this module and anything
put there by hand is gone at the next rebuild. Runs on the older project-flat layout are left
alone.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from misaka.core.documents import index as corpus
from misaka.core.research import ledger, runs
from misaka.core.tools.path_utils import DOWNLOAD_DIR_NAME
from misaka.core.web.evidence import check_material_read, read_provenance

MANIFEST = "SOURCES.md"
SOURCES_DIR = "sources"
_SCAN_LIMIT = 4 * 1024 * 1024          # a product bigger than this is listed, not read for locators
_SCAN_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
_NODE_SEEDS = ("synthesis",)            # the node product whose citations define the node's sources
_RUN_SEEDS = ("final", "partial")       # the run's delivered document (partial when it stopped early)
_STOP = r'''[^\s`'"<>\[\]()]'''         # a locator runs until whitespace or Markdown/quote punctuation
_SEG = r'''[^\s`'"<>\[\]()/]'''
_LOCATOR = re.compile(
    rf"doc:[0-9a-f]{{8,64}}(?:#p\d+)?"
    rf"|https?://{_STOP}+"
    rf"|(?<![\w/.\-])(?:{DOWNLOAD_DIR_NAME}|nodes|final|cards)/{_STOP}+"
    rf"|(?<![\w:.])/(?:{_SEG}+/)+{_SEG}+"
)
_NOTE = ("Written by misaka when the products here were settled and rebuilt from scratch each time, so edit "
         "nothing in this file or under the sources folder. Each file there is a hard link to where it already "
         "lives in the project (a copy only where a hard link was not possible); the originals never move. "
         "Cite original project files, not this bundle's disposable sources paths.")


def locators_in(text):
    """Every source locator a text mentions, in order, once each: ``doc:<id>#p<n>``, URLs, paths
    under downloads/, nodes/, final/ or cards/, and absolute paths (which count only when they
    turn out to be inside the project)."""
    out = []
    for match in _LOCATOR.finditer(text or ""):
        token = match.group(0).rstrip(".,;:!?*")
        if token and token not in out:
            out.append(token)
    return out


def place(src, dst):
    """Put ``src`` at ``dst`` without moving it: a hard link, or a copy when the link is refused
    (another device, a filesystem without links). Returns True when it had to copy."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
        return False
    except OSError as error:
        if error.errno not in {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
    # Never open an existing destination for writing: it may link to another original.
    out = open(dst, "xb")  # noqa: SIM115 - an existing target must stay outside failure cleanup
    try:
        with out, open(src, "rb") as source:
            shutil.copyfileobj(source, out)
        shutil.copystat(src, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dst)
        raise
    return True


def _url_key(url, *, query=True):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", parts.query if query else "", ""))


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _Index:
    """What the project holds that a locator can point at: saved pages by URL, corpus documents
    by id, the title and URL of any file we know the provenance of, and which files each card's
    corpus links say it consulted."""

    def __init__(self, workspace):
        self.workspace = os.path.realpath(workspace)
        self.pages = {}            # citable URL -> real path of the saved page
        self.docs = {}             # doc id -> real path of the original file
        self.titles = {}           # real path -> (title, url)
        self.digests = {}          # real path -> sha256 already on record (corpus, registered artifacts)
        self.consulted = {}        # task id -> real paths its corpus documents point at
        pages = os.path.join(self.workspace, DOWNLOAD_DIR_NAME, "pages")
        for name in (sorted(os.listdir(pages)) if os.path.isdir(pages) else []):
            real = self.file_inside(os.path.join(pages, name))
            if not real or not name.endswith(".md"):
                continue
            meta = read_provenance(real)
            for key in ("source_url", "final_url", "url", "requested_url"):
                value = meta.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    key_ = _url_key(value)
                    self.pages[key_] = real if key_ not in self.pages or self.pages[key_] == real else None
            url = meta.get("source_url") or meta.get("final_url") or ""
            self.titles[real] = (str(meta.get("title") or ""), str(url))
        try:
            docs = corpus.docs(workspace=self.workspace)
        except Exception:  # noqa: BLE001 - a corpus fault costs doc: resolution, not the bundle
            docs = []
        for doc in docs:
            candidates = [doc.get("orig_path"), *(doc.get("paths") or [])]
            real = next((r for p in candidates if p and (r := self.file_inside(p))), None)
            if not real:
                continue
            self.docs[str(doc.get("doc_id"))] = real
            self.titles.setdefault(real, (str(doc.get("title") or ""), ""))
            if doc.get("sha256"):
                self.digests[real] = str(doc["sha256"])
            for task_id in doc.get("task_ids") or []:
                self.consulted.setdefault(str(task_id), set()).add(real)

    def file_inside(self, path):
        """The real path of ``path`` when it is a regular file, not a symlink, inside the project
        and outside ``.git``; else None."""
        try:
            st = os.lstat(path)
        except (OSError, ValueError):
            return None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return None
        real = os.path.realpath(path)
        if not corpus.under(real, self.workspace) or os.path.relpath(real, self.workspace).split(os.sep)[0] == ".git":
            return None
        try:
            check_material_read(real)
        except ValueError:
            return None
        return real

    def checked_digest(self, real):
        if self.file_inside(real) != real:
            raise ValueError("source is missing or outside the readable project material")
        actual = corpus.sha256_file(real)
        expected = self.digests.get(real)
        if expected and actual != expected:
            raise ValueError("source changed since its registered sha256; original evidence is unresolved")
        return actual

    def dir_inside(self, path):
        real = os.path.realpath(path)
        return real if os.path.isdir(real) and corpus.under(real, self.workspace) else None


@dataclass
class Source:
    real: str
    relative: str                                   # to the project
    cited: list[str] = field(default_factory=list)  # how and by whom
    placed: str | None = None                       # where its link went, relative to the bundle folder
    copied: bool = False
    digest: str | None = None


class _Collector:
    """Walks citations from a card, a node or a document, closing over references."""

    def __init__(self, con, run, index):
        self.con, self.run, self.index = con, run, index
        self.sources = {}          # real path -> Source
        self.unresolved = []       # (locator, reason, by)
        self.consulted = set()
        self.seen = set()
        self.cards = {}            # real output dir -> task row
        self.nodes = {}            # real node dir -> node row
        for task in runs.tasks(con, run["id"]):
            if task["output_dir"]:
                self.cards[os.path.realpath(task["output_dir"])] = task
        for row in runs.artifacts(con, run["id"]):
            index.digests.setdefault(os.path.realpath(row["path"]), row["sha256"])
        for node in runs.nodes(con, run["id"]):
            self.nodes[os.path.realpath(os.path.join(index.workspace, _node_dir(run, node)))] = node

    def cite(self, locator, by, bases, *, origin=None):
        real, reason = self._resolve(locator, bases)
        if real is None:
            if reason and (locator, reason, by) not in self.unresolved:
                self.unresolved.append((locator, reason, by))
            return
        if real == origin:                          # a document naming its own path is not a citation
            return
        source = self.sources.get(real)
        if source is None:
            source = self.sources[real] = Source(real, os.path.relpath(real, self.index.workspace))
        line = (f"as `{locator}` " if locator.startswith(("doc:", "http://", "https://")) else "") + by
        if line not in source.cited:
            source.cited.append(line)
        self.follow(real)

    def _resolve(self, locator, bases):
        if locator.startswith("doc:"):
            real = self.index.docs.get(locator[4:].split("#", 1)[0])
            return real, "" if real else "not in this project's corpus"
        if locator.startswith(("http://", "https://")):
            real = self.index.pages.get(_url_key(locator))
            return real, "" if real else f"no saved copy under {DOWNLOAD_DIR_NAME}/"
        for base in ([""] if os.path.isabs(locator) else bases):
            candidate = os.path.join(base, locator)
            real = self.index.file_inside(candidate)
            if real:
                return real, ""
            if self.index.dir_inside(candidate):        # a folder named in passing is not a citation
                return None, ""
        # An absolute path that is not one of ours is ordinary prose, not a broken citation.
        return None, "" if os.path.isabs(locator) else "no such file in this project"

    def follow(self, real):
        """A cited card output brings that card's declared sources; a cited node product
        brings that node's."""
        for folder, task in self.cards.items():
            if real.startswith(folder + os.sep):
                self.card(task)
                return
        for folder, node in self.nodes.items():
            if real.startswith(folder + os.sep):
                self.node(node)
                return

    def scan(self, path, by, bases):
        real = self.index.file_inside(path)
        if not real or not real.lower().endswith(_SCAN_SUFFIXES):
            return
        try:
            self.index.checked_digest(real)
            if os.path.getsize(real) > _SCAN_LIMIT:
                return
            with open(real, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except (OSError, ValueError) as error:
            self.unresolved.append((os.path.relpath(real, self.index.workspace), str(error), by))
            return
        for locator in locators_in(text):
            self.cite(locator, by, [*bases, os.path.dirname(real)], origin=real)

    def card(self, task):
        if ("card", task["id"]) in self.seen:
            return
        self.seen.add(("card", task["id"]))
        bases = [self.index.workspace, *([task["output_dir"]] if task["output_dir"] else [])]
        for finding in ledger.findings(self.con, self.run["id"], task_id=task["id"]):
            by = f'by [{task["id"]}] "{_clip(finding["text"], 100)}" ({finding["claim_type"]})'
            for claim in ledger.claims(self.con, finding["id"]):
                if claim["source_file"]:
                    quote = f' — "{_clip(claim["quote"], 100)}"' if claim["quote"] else ""
                    self.cite(claim["source_file"], by + quote, bases)
        for row in runs.artifacts(self.con, self.run["id"], task_id=task["id"]):
            self.scan(row["path"], f"in `{os.path.relpath(row['path'], self.index.workspace)}`", bases)
        self.consulted |= self.consulted_by(task)

    def consulted_by(self, task):
        """Files a card looked at, whether or not it cited them: what its sessions fetched,
        downloaded, read or opened through the document tools, the corpus documents linked to
        it, and the downloads it declared as written."""
        out = set(self.index.consulted.get(task["id"], ()))
        for event in self.con.execute(
            "SELECT payload FROM events WHERE task_id=? AND kind='artifact_written' ORDER BY id", (task["id"],),
        ):
            try:
                rel = json.loads(event["payload"] or "{}").get("path")
            except (TypeError, ValueError):
                continue
            if isinstance(rel, str) and rel.split("/")[0] == DOWNLOAD_DIR_NAME:
                real = self.index.file_inside(os.path.join(self.index.workspace, rel))
                if real:
                    out.add(real)
        sessions = [row["session_file"] for row in self.con.execute(
            "SELECT session_file FROM task_runs WHERE task_id=? AND session_file IS NOT NULL ORDER BY started_at",
            (task["id"],))]
        current = self.con.execute("SELECT session_file FROM tasks WHERE id=?", (task["id"],)).fetchone()
        if current and current["session_file"]:
            sessions.append(current["session_file"])
        for session_file in dict.fromkeys(sessions):
            out |= consulted_in_session(session_file, self.index)
        return out

    def node(self, node):
        if ("node", node["id"]) in self.seen:
            return
        self.seen.add(("node", node["id"]))
        for row in _node_products(self.con, self.run, node, self.index):
            if row["kind"] in _NODE_SEEDS:
                self.scan(row["path"], f"in `{os.path.relpath(row['path'], self.index.workspace)}`",
                          [self.index.workspace])
        for task in runs.tasks(self.con, self.run["id"], node_id=node["id"]):
            self.card(task)


_READ_TOOLS = ("read",)
_DOC_TOOLS = ("doc_outline", "doc_read", "doc_find", "doc_verify", "doc_page_image")
_SAVING_TOOLS = ("web_fetch", "web_extract", "download_file")


def consulted_in_session(session_file, index):
    """Files a session actually opened: paths read with ``read``, documents opened through the
    document tools (by id, via the corpus), and pages or files the web tools saved. Read from the
    session's tool calls and tool results; nothing else in the transcript is looked at."""
    out = set()

    def keep(value):
        if not isinstance(value, str) or not value:
            return
        real = index.file_inside(value if os.path.isabs(value) else os.path.join(index.workspace, value))
        if real:
            out.add(real)

    try:
        lines = Path(session_file).read_text(encoding="utf-8").split("\n")
    except (OSError, TypeError, ValueError):
        return out
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" and isinstance(message.get("content"), list):
            for block in message["content"]:
                if not isinstance(block, dict) or block.get("type") != "toolCall":
                    continue
                arguments = block.get("arguments") if isinstance(block.get("arguments"), dict) else {}
                if block.get("name") in _READ_TOOLS:
                    keep(arguments.get("path"))
                elif block.get("name") in _DOC_TOOLS:
                    real = index.docs.get(str(arguments.get("doc_id") or ""))
                    if real:
                        out.add(real)
        elif message.get("role") == "toolResult" and message.get("toolName") in _SAVING_TOOLS:
            details = message.get("details") if isinstance(message.get("details"), dict) else {}
            keep(details.get("saved_path"))
            keep(details.get("path"))
            for value in (details.get("saved_paths") or []):
                keep(value)
    return out


def _node_dir(run, node):
    return runs.node_dir(run, node["id"] if node["parent_id"] else None)


def _node_products(con, run, node, index):
    """The node's own registered files: under its folder, not under one of its cards."""
    folder = os.path.join(index.workspace, _node_dir(run, node))
    cards = os.path.join(folder, "cards") + os.sep
    scope = {"branch_id": node["id"]} if node["parent_id"] else {"root_only": True}
    return [row for row in runs.artifacts(con, run["id"], **scope)
            if (real := os.path.realpath(row["path"])).startswith(folder + os.sep) and not real.startswith(cards)]


def _placed_name(relative, taken):
    """Where a source goes under sources/: the last two levels of its project path, minus the
    downloads/ or nodes/ prefix (``downloads/pages/0c4b.md`` -> ``pages/0c4b.md``,
    ``nodes/b_1/cards/t_1/out.md`` -> ``t_1/out.md``), disambiguated when two collide."""
    parts = relative.split("/")
    if parts[0] in (DOWNLOAD_DIR_NAME, "nodes") and len(parts) > 1:
        parts = parts[1:]
    name = "/".join(parts[-2:])
    if name in taken:
        stem, ext = os.path.splitext(name)
        stem = f"{stem}-{hashlib.sha256(relative.encode()).hexdigest()[:8]}"
        name = f"{stem}{ext}"
        suffix = 1
        while name in taken:
            name = f"{stem}-{suffix}{ext}"
            suffix += 1
    return name


def _reset(path, *, keep):
    """Empty the sources folder we own (and only that: a symlink or a file there is somebody
    else's), recreating it when there is something to put in it."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ValueError(f"{path} is not a folder this bundle owns; refusing to rebuild it")
        shutil.rmtree(path)
    if keep:
        os.makedirs(path)


def _build(collector, *, folder, manifest, sources_dir, title, products, cards=()):
    index = collector.index
    folder = os.path.realpath(folder)
    if not corpus.under(folder, index.workspace) or not sources_dir.startswith(folder + os.sep):
        raise ValueError("A bundle is written only inside the research project.")
    inside = lambda real: real.startswith(folder + os.sep)
    valid = []
    for source in collector.sources.values():
        try:
            if corpus.under(source.real, sources_dir):
                raise ValueError("disposable bundle source; cite the original project file instead")
            source.digest = index.checked_digest(source.real)
            valid.append(source)
        except (OSError, ValueError) as error:
            collector.unresolved.append((source.relative, str(error), ""))
    to_place = sorted((s for s in valid if not inside(s.real)), key=lambda s: s.relative)
    in_folder = sorted((s for s in valid if inside(s.real)), key=lambda s: s.relative)
    _reset(sources_dir, keep=bool(to_place))
    taken = set()
    for source in to_place:
        name = _placed_name(source.relative, taken)
        taken.add(name)
        dest = os.path.join(sources_dir, name)
        try:
            source.copied = place(source.real, dest)
            if corpus.sha256_file(dest) != source.digest:
                os.unlink(dest)
                raise ValueError("source changed during placement; original evidence is unresolved")
            source.placed = os.path.relpath(dest, folder)
        except (OSError, ValueError) as error:
            collector.unresolved.append((source.relative, f"could not be placed: {error}", ""))

    def rel(path):
        return os.path.relpath(path, index.workspace)

    def provenance(real):
        title_, url = index.titles.get(real, ("", ""))
        return " — ".join(part for part in (f'"{_clip(title_, 120)}"' if title_ else "", url) if part)

    lines = [f"# Sources — {title}", "", _NOTE, "", "## Products", ""]
    lines += [f"- `{os.path.relpath(row['path'], folder)}` — {row['title']} ({row['kind']})" for row in products] \
        or ["- (none registered)"]
    if cards:
        lines += ["", "## Cards", ""]
        lines += [f"- `{os.path.relpath(t['output_dir'], folder) if t['output_dir'] else '-'}` — "
                  f"[{t['id']}] {t['title']} ({t['research_kind']}, {t['status']})" for t in cards]
    lines += ["", f"## Cited sources (under `{os.path.relpath(sources_dir, folder)}/`)", ""]
    for source in to_place:
        if source.placed is None:
            continue
        lines.append(f"- `{source.placed}` ← `{source.relative}`"
                     + (" (copied: a hard link was not possible)" if source.copied else ""))
        if provenance(source.real):
            lines.append(f"  - {provenance(source.real)}")
        lines.append(f"  - sha256 {source.digest}")
        lines += [f"  - cited {how}" for how in source.cited]
    if not any(s.placed for s in to_place):
        lines.append("- (none)")
    if in_folder:
        lines += ["", "## Cited, already in this folder", ""]
        for source in in_folder:
            lines.append(f"- `{os.path.relpath(source.real, folder)}`")
            lines += [f"  - cited {how}" for how in source.cited]
    # Material means downloads/: a card's own outputs and the node's files are products, not sources.
    consulted = sorted(r for real in collector.consulted - set(collector.sources)
                       if (r := rel(real)).split(os.sep)[0] == DOWNLOAD_DIR_NAME)
    lines += ["", "## Consulted but not cited (left where they are)", ""]
    lines += [f"- `{r}`" + (f" — {provenance(os.path.join(index.workspace, r))}"
                            if provenance(os.path.join(index.workspace, r)) else "") for r in consulted] or ["- (none)"]
    lines += ["", "## Unresolved locators", ""]
    lines += [f"- `{loc}` — {reason}" + (f" ({by})" if by else "") for loc, reason, by in collector.unresolved] or ["- (none)"]
    text = "\n".join(lines) + "\n"
    handle, staging = tempfile.mkstemp(dir=os.path.dirname(manifest), prefix=".SOURCES-", suffix=".part")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.chmod(staging, 0o644)
        os.replace(staging, manifest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staging)
        raise
    return manifest


def card_bundle(con, run, task):
    """Beside one card's outputs: the sources its declared findings rest on and what its files cite."""
    if not runs._by_node(run) or not task["output_dir"]:
        return None
    index = _Index(run["workspace"])
    folder = index.dir_inside(task["output_dir"])
    if folder is None:
        return None
    collector = _Collector(con, run, index)
    collector.card(task)
    return _build(collector, folder=folder, manifest=os.path.join(folder, MANIFEST),
                  sources_dir=os.path.join(folder, SOURCES_DIR),
                  title=f"{os.path.relpath(folder, index.workspace)} — [{task['id']}] {task['title']}",
                  products=runs.artifacts(con, run["id"], task_id=task["id"]))


def node_bundle(con, run, node):
    """Beside a node's conclusion: what it cites, closed over its cards' declared sources."""
    if not runs._by_node(run):
        return None
    index = _Index(run["workspace"])
    folder = index.dir_inside(os.path.join(index.workspace, _node_dir(run, node)))
    if folder is None:                                # a node that never wrote a file has no folder to bundle in
        return None
    collector = _Collector(con, run, index)
    collector.node(node)
    return _build(collector, folder=folder, manifest=os.path.join(folder, MANIFEST),
                  sources_dir=os.path.join(folder, SOURCES_DIR),
                  title=f"{os.path.relpath(folder, index.workspace)} — {_clip(node['trigger_text'], 100)}",
                  products=_node_products(con, run, node, index),
                  cards=runs.tasks(con, run["id"], node_id=node["id"]))


def final_bundle(con, run):
    """Beside the run's delivered document (final.md, or partial.md when the run stopped early):
    what it cites, closed over the nodes and cards it names. Files keep the run's prefix so two
    runs of one project never overwrite each other's bundle."""
    if not runs._by_node(run):
        return None
    index = _Index(run["workspace"])
    folder = index.dir_inside(os.path.join(index.workspace, "final"))
    if folder is None:
        return None
    prefix = os.path.join(folder, f"{run['id']}-")
    products = [row for row in runs.artifacts(con, run["id"], root_only=True)
                if os.path.realpath(row["path"]).startswith(prefix)]
    collector = _Collector(con, run, index)
    for kind in _RUN_SEEDS:
        seeds = [row for row in products if row["kind"] == kind]
        if seeds:
            collector.scan(seeds[-1]["path"], f"in `{os.path.relpath(seeds[-1]['path'], index.workspace)}`",
                           [index.workspace])
            break
    for task in runs.tasks(con, run["id"]):
        collector.consulted |= collector.consulted_by(task)
    return _build(collector, folder=folder, manifest=prefix + MANIFEST, sources_dir=prefix + SOURCES_DIR,
                  title=f"final/ — run {run['id']}", products=products)

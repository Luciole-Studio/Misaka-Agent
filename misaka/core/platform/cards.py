"""Task-card persistence.

``cards/<id>.md`` owns identity, contract fields, dependencies, references, log,
generation, and at-rest status. SQLite caches those fields and exclusively owns
runtime leases, PIDs, attempts, cooldowns, and the live ``running`` state. Git is
history: a failed commit leaves the authoritative file dirty for a later retry; it
does not roll a lifecycle transition backward. Create cards through :func:`create`,
never through ``tasks.create_task`` directly -- a row without its file is refused
at dispatch and may be removed after inspection.
"""
import io
import os
import stat
import subprocess
import time
from pathlib import Path

from ruamel.yaml import YAML

from misaka.config import home
from misaka.core.platform import owners, repo, tasks
from misaka.utils import atomic
from misaka.utils.frontmatter import parseFrontmatter

LOG_HEADING = "## log"
_FIELD_ORDER = ("id", "title", "status", "generation", "assignee", "reviewer", "model",
                "priority", "origin_session", "needs", "urls", "created_at")
_AT_REST_STATUSES = frozenset({
    "ready", "todo", "review", "done", "failed", "stopped", "blocked", "triage", "archived", "held",
})


def _now_iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def card_path(workspace, task_id):
    return _card_path(tasks.canonical_workspace(workspace), task_id)


def _card_path(workspace, task_id):
    """The card's path under an already-canonical workspace. ``canonical_workspace`` lstats
    every component of the path; the reconcile loop pays that once per pass, not per card."""
    return os.path.join(workspace, "cards", f"{task_id}.md")


def _dump(fields, body):
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    out = io.StringIO()
    out.write("---\n")
    ordered = {k: fields[k] for k in _FIELD_ORDER if fields.get(k) is not None}
    ordered.update((key, value) for key, value in fields.items() if key not in _FIELD_ORDER)
    yaml.dump(ordered, out)
    out.write("---\n\n")
    out.write(body.strip() + "\n")
    return out.getvalue()


_PARSED: dict[str, tuple] = {}
_PARSED_CAP = 4096


_INVALID: dict[str, tuple[str, str]] = {}


def _forget(path):
    _PARSED.pop(str(path), None)
    _INVALID.pop(str(path), None)


def _note_invalid(path, error, kind):
    if kind == "unreadable" and isinstance(error, (FileNotFoundError, NotADirectoryError)):
        kind = "missing"
    if len(_INVALID) >= _PARSED_CAP:
        _INVALID.clear()
    _INVALID[str(path)] = (kind, f"{type(error).__name__}: {error}")


def unreadable_reason(path):
    """Why this card file's content is unusable, or ``None``.

    ``None`` also covers "there is no such file": a caller that must refuse to guess at a card's
    contract (see ``tasks.parent_ids(strict=True)``) has nothing to refuse over when the file is
    absent -- an absent card declares nothing, hides nothing, and cannot be repaired either.
    """
    entry = _INVALID.get(str(path))
    return entry[1] if entry is not None and entry[0] != "missing" else None


def invalid_cards():
    """``{card path: why it could not be used}`` for every card skipped since process start.

    Cards with no file at all are not in here; ``board`` lists their index rows as
    ``missing_file`` instead: the project folder has no file for that row.
    """
    return {path: reason for path, (kind, reason) in _INVALID.items() if kind != "missing"}


def _write_file(path, text):
    atomic.write_text(path, text)
    _forget(path)


def read(path):
    """``{"fields": frontmatter, "body": contract text}`` for one card file."""
    parsed = parseFrontmatter(Path(path).read_text(encoding="utf-8"))
    return {"fields": parsed.frontmatter or {}, "body": (parsed.body or "").strip()}


def try_read(path):
    """:func:`read`, or ``None`` with the failure recorded in :func:`invalid_cards`.

    Broken frontmatter raises ``FrontmatterError`` and a non-UTF-8 file raises
    ``UnicodeDecodeError`` -- both ``ValueError``, neither an ``OSError``. For a caller that
    walks every card in the project, one such file is one card's problem, not the project's.
    A missing file is recorded apart from those, as ``missing``: nothing about it parsed badly.

    Success clears an ``unreadable`` or ``missing`` note -- the file this reader just read is
    proof of neither. It says nothing about whether the file is a *usable* card, so it may not
    clear the ``invalid`` diagnosis :func:`reconcile_one` recorded.
    """
    try:
        card = read(path)
    except (OSError, ValueError) as error:
        _note_invalid(path, error, "unreadable")
        return None
    note = _INVALID.get(str(path))
    if note is not None and note[0] != "invalid":
        del _INVALID[str(path)]
    return card


def iter_cards(workspace):
    """Every card file in the workspace, sorted by id: ``(task_id, path)``."""
    root = Path(tasks.canonical_workspace(workspace)) / "cards"
    if not root.is_dir():
        return
    for path in sorted(root.glob("t_*.md")):
        yield path.stem, str(path)


def create(con, workspace, title, body, assignee, *, reviewer=None, model=None,
           priority=0, origin_session=None, after_row=None, needs=()):
    """The one front door for new cards: index row first (it mints the id), then ``after_row(tid)``
    for whatever else the row must be tied to (a workflow's link), then the file. Any failure before
    the file exists rolls the row back -- no card exists without its truth, and no file without its row."""
    workspace = tasks.canonical_workspace(workspace)
    needs = list(dict.fromkeys(needs))
    for parent_id in needs:
        parent = tasks.get(con, parent_id)
        if parent is None or parent["workspace"] != workspace:
            raise ValueError("A dependency must name an existing task in the same project.")
    tid = tasks.create_task(con, title, body=body, assignee=assignee, model=model,
                            priority=priority,
                            reviewer=reviewer, workspace=workspace,
                            origin_session=origin_session)
    fields = {"id": tid, "title": title, "status": "ready", "generation": 1, "assignee": assignee,
              "reviewer": reviewer, "model": model,
              "priority": priority,
              "origin_session": origin_session, "created_at": _now_iso()}
    text = body.strip() + f"\n\n{LOG_HEADING}\n- {_now_iso()} last-order: created\n"
    try:
        if after_row is not None:
            after_row(tid)
        if needs:
            fields.update(needs=needs, status="todo")
            con.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
        _write_file(card_path(workspace, tid), _dump(fields, text))
    except BaseException:
        tasks.delete_task(con, tid, allow_active=True)
        raise
    return tid


def _rewrite(workspace, task_id, mutate):
    """Read-modify-write under a per-card lock: two processes (a pane and Last Order) editing
    one card never lose each other's change."""
    from filelock import FileLock
    path = card_path(workspace, task_id)
    with FileLock(os.path.join(tasks.task_state_dir(task_id), "card.lock")):
        card = read(path)
        fields, body = mutate(card["fields"], card["body"])
        _write_file(path, _dump(fields, body))


def append_log(workspace, task_id, author, text):
    """Append one line to the card's ``## log`` section (created if missing).

    The log goes into git with the card, so credentials a tool echoed into the note are
    masked here."""
    from misaka.utils.redact import redact
    line = f"- {_now_iso()} {author}: {' '.join(redact(str(text)).split())}"

    def mutate(fields, body):
        if LOG_HEADING not in body:
            body = body.rstrip() + f"\n\n{LOG_HEADING}"
        return fields, body.rstrip() + "\n" + line

    _rewrite(workspace, task_id, mutate)


def set_fields(workspace, task_id, **updates):
    """Update at-rest frontmatter fields (reviewer, status, needs, ...). ``None`` removes."""

    def mutate(fields, body):
        for key, value in updates.items():
            if value is None:
                fields.pop(key, None)
            else:
                fields[key] = value
        return fields, body

    _rewrite(workspace, task_id, mutate)


MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


def attachment_dir(base_dir, task_id):
    return os.path.join(base_dir, "cards", str(task_id))


def attach(workspace, task_id, source):
    """Copy a local regular file into the card's attachment directory
    (``cards/<id>/<name>``, part of the repository) and log it. Returns the repo-relative
    path. Symlinks and files over 50 MiB are rejected."""
    src = Path(source)
    if src.is_symlink() or not src.is_file():
        raise ValueError(f"Not a regular file: {source}")
    if src.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"Attachment exceeds 50 MiB: {source}")
    workspace = tasks.canonical_workspace(workspace)
    target_dir = attachment_dir(workspace, task_id)
    os.makedirs(target_dir, exist_ok=True)
    name, target = src.name, os.path.join(target_dir, src.name)
    stem, dot, ext = src.name.rpartition(".")
    counter = 0
    while os.path.exists(target):
        counter += 1
        suffix = f"{int(time.time())}" + (f"-{counter}" if counter > 1 else "")
        name = f"{stem or ext}-{suffix}{dot}{ext if stem else ''}"
        target = os.path.join(target_dir, name)
    import shutil
    shutil.copyfile(src, target)
    append_log(workspace, task_id, "last-order", f"[attach] cards/{task_id}/{name}")
    return os.path.join("cards", str(task_id), name)


def attach_url(workspace, task_id, url):
    """Record an http(s) reference on the card (frontmatter ``urls`` list, plus the log)."""
    if not str(url).startswith(("http://", "https://")):
        raise ValueError("Only http(s) URLs can be attached.")
    line = f"- {_now_iso()} last-order: [url] {url}"

    def mutate(fields, body):
        urls = list(fields.get("urls") or [])
        if url not in urls:
            urls.append(url)
            fields["urls"] = urls
        if LOG_HEADING not in body:
            body = body.rstrip() + f"\n\n{LOG_HEADING}"
        return fields, body.rstrip() + "\n" + line

    _rewrite(workspace, task_id, mutate)


def attachment_list(base_dir, task_id, workspace=None):
    """The card's inputs as the prompt expects them: the files under ``cards/<id>/`` in the
    directory the card runs in (worktree or project), plus the URL references from the card
    file. Same shape the old staging produced: {"kind", "path"/"source", "name"}."""
    out = []
    root = attachment_dir(base_dir, task_id)
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if os.path.isfile(full) and not os.path.islink(full):
                out.append({"kind": "file", "path": full, "name": name})
    card = try_read(card_path(workspace or base_dir, task_id))
    fields = card["fields"] if card is not None else {}
    out.extend({"kind": "url", "source": u, "name": u} for u in fields.get("urls") or [])
    return out


def read_log(workspace, task_id):
    """The card's ``## log`` lines (comments, attachments, lifecycle notes), oldest first."""
    body = read(card_path(workspace, task_id))["body"]
    if LOG_HEADING not in body:
        return []
    section = body.split(LOG_HEADING, 1)[1]
    return [line[2:] for line in section.splitlines() if line.startswith("- ")]


STUCK_SECONDS = 24 * 3600
FAILING_AFTER = 2


def card_signal(row, now=None):
    """What a reader should notice about a card beyond its status: ``failing`` (attempts are
    running out) or ``stuck`` (blocked for a day); None otherwise."""
    if row is None:
        return None
    if int(row["consecutive_failures"] or 0) >= FAILING_AFTER:
        return "failing"
    now = int(time.time()) if now is None else int(now)
    if (row["status"] in ("blocked", "triage") and row["blocked_at"]
            and now - int(row["blocked_at"]) >= STUCK_SECONDS):
        return "stuck"
    return None


def board(con, workspace):
    """The board, file-first: every card file joined with its live index status; index rows
    without a file (pre-migration strays) listed last so nothing hides."""
    workspace = tasks.canonical_workspace(workspace)
    rebuild(con, workspace)
    out, seen = [], set()
    now = int(time.time())
    for tid, path in iter_cards(workspace):
        card = try_read(path)
        note = _INVALID.get(str(path))
        row = tasks.get(con, tid)
        seen.add(tid)
        if card is None or note is not None:
            kind, reason = note if note is not None else ("unreadable", "unreadable card file")
            fields = card["fields"] if card is not None else {}
            title = str(fields.get("title") or (row["title"] if row is not None else "") or "")
            out.append({"id": tid, "title": f"{title} (card file {kind}: {reason})".strip(),
                        "assignee": str(fields.get("assignee")
                                        or (row["assignee"] if row is not None else "") or ""),
                        "status": "invalid", "invalid": reason,
                        "origin_session": row["origin_session"] if row is not None else None})
            continue
        fields = card["fields"]
        signal = card_signal(row, now)
        out.append({"id": tid, "title": str(fields.get("title") or ""),
                    "assignee": str(fields.get("assignee") or ""),
                    "status": str(row["status"] if row is not None else fields.get("status") or "?"),
                    "origin_session": (row["origin_session"] if row is not None
                                       else fields.get("origin_session")),
                    **({"signal": signal} if signal else {})})
    for row in con.execute(
            "SELECT id,title,assignee,status,origin_session FROM tasks "
            "WHERE workspace=? ORDER BY created_at", (workspace,)):
        if row["id"] not in seen:
            out.append({"id": row["id"], "title": row["title"], "assignee": row["assignee"],
                        "status": row["status"], "origin_session": row["origin_session"],
                        "missing_file": True})
    return out


def remove(con, workspace, task_id):
    """Delete a card: index row (refusing active ones) and its file. The one entry point for
    deletion: a file left behind would resurrect the card on the next rebuild, so that is
    reported as a failure, never as success."""
    git_paths = [os.path.join("cards", f"{task_id}.md"), os.path.join("cards", str(task_id))]
    tracked = repo.enabled(workspace) and bool(repo._git(workspace, "ls-files", "--", *git_paths).stdout.strip())
    ok, msg = tasks.delete_task(con, task_id)
    if not ok:
        return ok, msg
    path = card_path(workspace, task_id)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        return False, (f"Card {task_id} left the board but its file could not be deleted ({error}); "
                       f"remove {path} by hand or the card comes back on the next rebuild.")
    _forget(path)
    attachments = attachment_dir(workspace, task_id)
    if os.path.isdir(attachments):
        import shutil
        try:
            shutil.rmtree(attachments)
        except OSError as error:
            return False, f"Card {task_id} was deleted but its attachments remain ({error}): {attachments}"
    msg += (" Its committed copy stays in the project's git history; the deletion itself is left "
            "uncommitted for you to commit."
            if tracked else
            " Its file, log, and attachments were deleted from disk; they were never committed "
            "to git, so there is no copy left.")
    if os.path.isdir(tasks.card_output_dir(workspace, task_id)):
        msg += f" What it produced stays in outputs/{task_id}."
    return ok, msg


def _card_values(fields, body):
    status = str(fields.get("status") or "ready")
    if status not in _AT_REST_STATUSES:
        raise ValueError(f"Invalid at-rest card status: {status}")
    generation = int(fields.get("generation") or 1)
    if generation < 1:
        raise ValueError("Card generation must be positive.")
    return {
        "title": str(fields.get("title") or ""), "body": body,
        "assignee": str(fields.get("assignee") or ""), "reviewer": fields.get("reviewer"),
        "model": fields.get("model"), "priority": int(fields.get("priority") or 0),
        "origin_session": fields.get("origin_session"), "status": status, "generation": generation,
    }


_MIRRORED = ("title", "body", "assignee", "reviewer", "model", "priority",
             "origin_session", "status", "generation")


def reconcile_one(con, workspace, task_id):
    """Refresh one idle index row from its card; return the parsed card, or None if unusable.

    Returning the card lets the caller answer further questions about it -- dependencies,
    say -- without reading the file a second time.

    Neither half of the work is repeated for nothing: an unchanged file is not re-read or
    re-parsed (the stat gate above), and a row that already mirrors its file is not rewritten
    -- an identical UPDATE still costs a WAL frame per idle card on every tick.
    """
    workspace = tasks.canonical_workspace(workspace)
    path = _card_path(workspace, task_id)
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    key = (info.st_mtime_ns, info.st_size, info.st_ino)
    cached = _PARSED.get(path)
    if cached is not None and cached[0] == key:
        _, fields, body, values = cached
    else:
        try:
            card = read(path)
        except (OSError, ValueError) as error:
            _note_invalid(path, error, "unreadable")
            return None
        fields, body = card["fields"], card["body"]
        try:
            if str(fields.get("id") or "") != str(task_id):
                raise ValueError(f"Card names id {fields.get('id')!r}, but its file is {task_id}.md")
            values = _card_values(fields, body)
        except (TypeError, ValueError) as error:
            _note_invalid(path, error, "invalid")
            return None
        _INVALID.pop(path, None)
        if len(_PARSED) >= _PARSED_CAP:
            _PARSED.clear()
        _PARSED[path] = (key, fields, body, values)
    card = {"fields": dict(fields), "body": body}
    row = con.execute(
        "SELECT workspace,output_dir," + ",".join(_MIRRORED) + " FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        return card if tasks.insert_index_row(con, fields, body, workspace=workspace) else None
    if row["workspace"] != workspace:
        return None
    if row["status"] in {"running", "review"}:
        return card
    if not row["output_dir"]:
        con.execute("UPDATE tasks SET output_dir=? WHERE id=?", (tasks.card_output_dir(workspace, task_id), task_id))
    if all(row[column] == values[column] for column in _MIRRORED):
        return card
    changed = con.execute(
        "UPDATE tasks SET title=?,body=?,assignee=?,reviewer=?,model=?,priority=?,"
        "origin_session=?,status=?,generation=? WHERE id=? AND status=?",
        (*(values[column] for column in _MIRRORED), task_id, row["status"]),
    ).rowcount
    if changed and row["status"] != values["status"]:
        tasks.add_event(con, task_id, "reconciled",
                        {"from": row["status"], "to": values["status"], "source": "card file"},
                        generation=values.get("generation"))
    return card


def _needs(card):
    """The card's declared dependencies, or None when the edge list is malformed."""
    raw = card["fields"].get("needs") or []
    if not isinstance(raw, list) or any(not isinstance(parent, str) or not parent for parent in raw):
        return None
    return list(dict.fromkeys(raw))


def dispatchable(con, workspace, task_id):
    """Whether this one card may be claimed -- without walking the whole project.

    A parent releases its child only by reaching ``done``, and a card reaches ``done``
    either by passing this same check on its own way out or by a person writing it into
    the file (which is what "the file is the contract" means). So the project-wide
    ``needs`` fixed point that :func:`reconcile` computes is index-rebuild work, not
    per-claim work: doing it inside every claim made one dispatch tick read every card
    file once per claim.
    """
    workspace = tasks.canonical_workspace(workspace)
    card = reconcile_one(con, workspace, task_id)
    if card is None:
        return False
    needs = _needs(card)
    if needs is None or task_id in needs:
        return False
    for parent in needs:
        row = con.execute("SELECT status,workspace FROM tasks WHERE id=?", (parent,)).fetchone()
        if row is None or row["status"] != "done":
            return False
        if tasks.canonical_workspace(row["workspace"]) != workspace:
            return False
    return True


def reconcile(con, workspace):
    """Derive one project's idle index, then return cards safe to dispatch.

    The second pass validates the complete ``needs`` graph only after every card
    row has been restored.  A missing/cross-project parent, malformed edge, or
    cycle therefore holds the affected chain instead of being mistaken for a
    satisfied dependency.
    """
    workspace = tasks.canonical_workspace(workspace)
    contracts = {}
    for task_id, _path in iter_cards(workspace):
        card = reconcile_one(con, workspace, task_id)
        if card is None:
            continue
        needs = _needs(card)
        if needs is None:
            continue
        contracts[task_id] = needs

    valid = set()
    while True:
        resolved = {
            task_id for task_id, parents in contracts.items()
            if task_id not in parents and all(parent in valid for parent in parents)
        }
        if resolved <= valid:
            break
        valid.update(resolved)

    statuses = {
        row["id"]: row["status"] for row in con.execute(
            "SELECT id,status FROM tasks WHERE workspace=?", (workspace,)
        )
    }
    return {
        task_id for task_id in valid
        if all(statuses.get(parent) == "done" for parent in contracts[task_id])
    }


def rebuild(con, workspace):
    """Restore missing rows and reconcile idle rows from card files; return the number restored."""
    missing = {tid for tid, _path in iter_cards(workspace) if tasks.get(con, tid) is None}
    reconcile(con, workspace)
    return sum(tasks.get(con, tid) is not None for tid in missing)


GITIGNORE_TEMPLATE = """# MISAKA: derived and cache material. The originals these point at are what git records.
{config_dir}/
.pageindex/
.office-cache/
.office-intent/
downloads/
.DS_Store
__pycache__/
*.pyc
"""

PROJECT_TEMPLATE = """# {name}

> 项目简报（PROJECT.md）——所有 agent 开工前读这份；计划、范围、已知缺口变了就改这里。

## 一句话目标

（待填）

## 行文规范

- 一句一行（semantic line breaks）：每个句子独占一行，改动对照（diff）即逐句校记。
- 语料（PDF/EPUB 等）不入库：用 `misaka doc add` 登记，正文里引用 doc id。

## 范围与边界

（待填）

## 已知缺口

（待填）
"""


def _git(folder, *args):
    """Run one git command for :func:`init_project`, reporting a missing or failing git as
    a readable RuntimeError. ``misaka init`` is usually the first command a new user runs,
    and git is a hard prerequisite the installer cannot supply: a subprocess traceback here
    tells them nothing about what to install."""
    try:
        subprocess.run(["git", *args], cwd=folder, check=True)
    except FileNotFoundError:
        raise RuntimeError(
            "misaka init needs git, but no git was found on PATH. Install git first "
            "(macOS: xcode-select --install; Debian/Ubuntu: apt install git), "
            "then run misaka init again."
        ) from None
    except subprocess.CalledProcessError as err:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {folder} (exit {err.returncode})."
        ) from None


def init_project(folder, *, draft_brief=True, git=True):
    """Make a folder a MISAKA project: the skeleton (PROJECT.md, cards/, .gitignore) with its
    cards indexed, and -- with ``git``, as ``misaka init`` asks -- a git repository. Only a
    repository this call itself created gets the initial commit; an existing repository is never
    committed to. A workflow starting in a folder passes ``git=False``: git is the user's, never a side effect."""
    folder = tasks.canonical_workspace(folder)
    _refuse_whole_home(folder)
    actions = []
    fresh = git and not repo.enabled(folder)
    if fresh:
        _git(folder, "init", "-q")
        actions.append("git repository created")
    os.makedirs(os.path.join(folder, "cards"), exist_ok=True)
    project_md = os.path.join(folder, "PROJECT.md")
    if draft_brief and not os.path.exists(project_md):
        _write_file(project_md, PROJECT_TEMPLATE.format(name=os.path.basename(folder) or folder))
        actions.append("PROJECT.md written")
    ignore = os.path.join(folder, ".gitignore")
    config_dir = home.project_dir(folder)
    if not os.path.exists(ignore) and config_dir is not None:
        atomic.write_text(ignore, GITIGNORE_TEMPLATE.format(config_dir=config_dir.name)
                          + "".join(owner.gitignore() for owner in owners.registered()))
        actions.append(".gitignore written")
    con = tasks.connect(os.path.expanduser(_cfg_db()))
    try:
        restored = rebuild(con, folder)
    finally:
        con.close()
    if restored:
        actions.append(f"{restored} card(s) indexed from cards/")
    if fresh:
        skeleton = [name for name in ("PROJECT.md", "cards", ".gitignore")
                    if os.path.exists(os.path.join(folder, name))]
        if skeleton:
            _git(folder, "add", "--", *skeleton)
        _git(folder, "-c", "user.name=misaka", "-c", "user.email=misaka@local",
             "commit", "--allow-empty", "-q", "-m", "misaka init: project skeleton")
        actions.append("initial commit")
    return actions or ["already initialized"]


def _refuse_whole_home(folder):
    """A project is a folder of its own. Turning a home directory or a filesystem root into
    one is never what was meant -- it is what pressing Enter at the wizard's folder prompt
    would do from a fresh shell -- and everything downstream (indexing, git, the board's
    workspace identity) then treats every file on the machine as project material."""
    if folder == os.path.dirname(folder):
        raise RuntimeError(
            f"{folder} is a filesystem root; a project has to be a folder of its own.")
    try:
        home = str(Path.home().resolve())
    except (OSError, RuntimeError):
        return
    if folder == home:
        raise RuntimeError(
            f"{folder} is your home directory; a project has to be a folder of its own. "
            "Make one and initialize that instead, for example:\n"
            "  mkdir ~/project && cd ~/project && misaka init")


def _cfg_db():
    from misaka.config import CFG
    return CFG["db"]

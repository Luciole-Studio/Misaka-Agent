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
import json
import os
import subprocess
import time
from pathlib import Path

from ruamel.yaml import YAML

from misaka.platform import repo, tasks
from misaka.utils import atomic
from misaka.utils.frontmatter import parse_frontmatter

LOG_HEADING = "## log"
_FIELD_ORDER = ("id", "title", "status", "generation", "assignee", "reviewer", "executor", "model",
                "priority", "timeout_seconds", "origin_session", "needs", "urls", "created_at")
_AT_REST_STATUSES = frozenset({
    "ready", "todo", "review", "done", "failed", "stopped", "blocked", "triage", "archived", "held",
})


def _now_iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def card_path(workspace, task_id):
    return os.path.join(tasks.canonical_workspace(workspace), "cards", f"{task_id}.md")


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


def _write_file(path, text):
    atomic.write_text(path, text)


def read(path):
    """``{"fields": frontmatter, "body": contract text}`` for one card file."""
    parsed = parse_frontmatter(Path(path).read_text(encoding="utf-8"))
    return {"fields": parsed.frontmatter or {}, "body": (parsed.body or "").strip()}


def iter_cards(workspace):
    """Every card file in the workspace, sorted by id: ``(task_id, path)``."""
    root = Path(tasks.canonical_workspace(workspace)) / "cards"
    if not root.is_dir():
        return
    for path in sorted(root.glob("t_*.md")):
        yield path.stem, str(path)


def create(con, workspace, title, body, assignee, *, reviewer=None, model=None,
           priority=0, timeout_seconds=900, executor=None, origin_session=None, after_row=None):
    """The one front door for new cards: index row first (it mints the id), then ``after_row(tid)``
    for whatever else the row must be tied to (a research link), then the file. Any failure before
    the file exists rolls the row back -- no card exists without its truth, and no file without its row."""
    workspace = tasks.canonical_workspace(workspace)
    tid = tasks.create_task(con, title, body=body, assignee=assignee, model=model,
                            priority=priority, timeout_seconds=timeout_seconds,
                            executor=executor, reviewer=reviewer, workspace=workspace,
                            origin_session=origin_session)
    fields = {"id": tid, "title": title, "status": "ready", "generation": 1, "assignee": assignee,
              "reviewer": reviewer, "executor": executor, "model": model,
              "priority": priority, "timeout_seconds": timeout_seconds,
              "origin_session": origin_session, "created_at": _now_iso()}
    text = body.strip() + f"\n\n{LOG_HEADING}\n- {_now_iso()} last-order: created\n"
    try:
        if after_row is not None:
            after_row(tid)
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
    with FileLock(os.path.join(tasks.task_state_dir(task_id), "card.lock")):   # outside the tracked tree
        card = read(path)
        fields, body = mutate(card["fields"], card["body"])
        _write_file(path, _dump(fields, body))


def append_log(workspace, task_id, author, text):
    """Append one line to the card's ``## log`` section (created if missing)."""
    line = f"- {_now_iso()} {author}: {' '.join(str(text).split())}"

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
    while os.path.exists(target):                  # same name, same second: keep counting, never overwrite
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
    try:
        fields = read(card_path(workspace or base_dir, task_id))["fields"]
    except OSError:
        fields = {}
    out.extend({"kind": "url", "source": u, "name": u} for u in fields.get("urls") or [])
    return out


def read_log(workspace, task_id):
    """The card's ``## log`` lines (comments, attachments, lifecycle notes), oldest first."""
    body = read(card_path(workspace, task_id))["body"]
    if LOG_HEADING not in body:
        return []
    section = body.split(LOG_HEADING, 1)[1]
    return [line[2:] for line in section.splitlines() if line.startswith("- ")]


def board(con, workspace):
    """The board, file-first: every card file joined with its live index status; index rows
    without a file (pre-migration strays) listed last so nothing hides."""
    workspace = tasks.canonical_workspace(workspace)
    rebuild(con, workspace)
    out, seen = [], set()
    for tid, path in iter_cards(workspace):
        fields = read(path)["fields"]
        row = tasks.get(con, tid)
        seen.add(tid)
        out.append({"id": tid, "title": str(fields.get("title") or ""),
                    "assignee": str(fields.get("assignee") or ""),
                    "status": str(row["status"] if row is not None else fields.get("status") or "?"),
                    "origin_session": (row["origin_session"] if row is not None
                                       else fields.get("origin_session"))})
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
    attachments = attachment_dir(workspace, task_id)
    if os.path.isdir(attachments):
        import shutil
        try:
            shutil.rmtree(attachments)
        except OSError as error:
            return False, f"Card {task_id} was deleted but its attachments remain ({error}): {attachments}"
    if tracked and not repo.commit(workspace, git_paths, f"card {task_id}: delete"):
        return False, f"Card {task_id} was deleted but the deletion could not be committed to git; commit it by hand."
    return ok, msg


def migrate(con):
    """One-shot: write a card file for every index row that lacks one (pre-file era).
    Returns ``(written, existed, no_folder)``; run again, it writes nothing."""
    written = existed = no_folder = 0
    for row in con.execute("SELECT * FROM tasks ORDER BY created_at"):
        workspace = row["workspace"]
        if not workspace or not os.path.isdir(workspace):   # pre-NOT-NULL era rows may be bare
            no_folder += 1
            continue
        path = card_path(workspace, row["id"])
        if os.path.exists(path):
            existed += 1
            continue
        fields = {"id": row["id"], "title": row["title"], "status": row["status"],
                  "generation": max(1, int(row["generation"])),
                  "assignee": row["assignee"], "reviewer": row["reviewer"],
                  "executor": json.loads(row["executor"]) if row["executor"] else None,
                  "model": row["model"],
                  "priority": row["priority"], "timeout_seconds": row["timeout_seconds"],
                  "origin_session": row["origin_session"],
                  "created_at": _now_iso(row["created_at"])}
        body = ((row["body"] or "").strip()
                + f"\n\n{LOG_HEADING}\n- {_now_iso()} misaka: migrated from board.db\n")
        _write_file(path, _dump(fields, body))
        written += 1
    return written, existed, no_folder


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
        "executor": json.dumps(fields["executor"]) if fields.get("executor") else None,
        "model": fields.get("model"), "priority": int(fields.get("priority") or 0),
        "timeout_seconds": int(fields.get("timeout_seconds") or 900),
        "origin_session": fields.get("origin_session"), "status": status, "generation": generation,
    }


def reconcile_one(con, workspace, task_id):
    """Refresh one idle index row from its card; false means it is unsafe to dispatch."""
    workspace = tasks.canonical_workspace(workspace)
    path = card_path(workspace, task_id)
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    try:
        card = read(path)
        fields = card["fields"]
        if str(fields.get("id") or "") != str(task_id):
            return False
        values = _card_values(fields, card["body"])
    except (OSError, TypeError, ValueError):
        return False
    row = con.execute("SELECT status,workspace FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return bool(tasks.insert_index_row(con, fields, card["body"], workspace=workspace))
    if tasks.canonical_workspace(row["workspace"]) != workspace:
        return False
    if row["status"] in {"running", "review"}:
        return True                         # an in-flight contract is immutable until it rests again
    con.execute(
        "UPDATE tasks SET title=?,body=?,assignee=?,reviewer=?,executor=?,model=?,priority=?,"
        "timeout_seconds=?,origin_session=?,status=?,generation=? WHERE id=?",
        (*values.values(), task_id),
    )
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
    for task_id, path in iter_cards(workspace):
        if not reconcile_one(con, workspace, task_id):
            continue
        try:
            raw = read(path)["fields"].get("needs") or []
        except (OSError, TypeError, ValueError):
            continue
        if not isinstance(raw, list) or any(not isinstance(parent, str) or not parent for parent in raw):
            continue
        contracts[task_id] = list(dict.fromkeys(raw))

    # ponytail: O(cards^2) fixed-point validation; use a graph library only if
    # projects grow large enough for reconciliation to show up in profiles.
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


def init_project(folder):
    """Make a folder a MISAKA project: a git repository with the skeleton (PROJECT.md,
    cards/) and its cards indexed. Only a repository this call itself created gets the
    initial commit; an existing repository is never committed to."""
    folder = tasks.canonical_workspace(folder)
    actions = []
    fresh = not repo.enabled(folder)  # a worktree's .git is a file, not a directory
    if fresh:
        subprocess.run(["git", "init", "-q"], cwd=folder, check=True)
        actions.append("git repository created")
    os.makedirs(os.path.join(folder, "cards"), exist_ok=True)
    project_md = os.path.join(folder, "PROJECT.md")
    if not os.path.exists(project_md):
        _write_file(project_md, PROJECT_TEMPLATE.format(name=os.path.basename(folder) or folder))
        actions.append("PROJECT.md written")
    con = tasks.connect(os.path.expanduser(_cfg_db()))
    try:
        restored = rebuild(con, folder)
    finally:
        con.close()
    if restored:
        actions.append(f"{restored} card(s) indexed from cards/")
    if fresh:
        subprocess.run(["git", "add", "-A"], cwd=folder, check=True)
        subprocess.run(["git", "-c", "user.name=misaka", "-c", "user.email=misaka@local",
                        "commit", "-q", "-m", "misaka init: project skeleton"],
                       cwd=folder, check=True)
        actions.append("initial commit")
    return actions or ["already initialized"]


def _cfg_db():
    from misaka.config import CFG
    return CFG["db"]

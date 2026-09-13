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
import stat
import subprocess
import time
from pathlib import Path

from ruamel.yaml import YAML

from misaka.core.platform import repo, tasks
from misaka.utils import atomic
from misaka.utils.frontmatter import parse_frontmatter

LOG_HEADING = "## log"
_FIELD_ORDER = ("id", "title", "status", "generation", "assignee", "reviewer", "executor", "model",
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


# The mtime gate for :func:`reconcile_one`, which is run over every card in the project on
# every dispatch and every research tick. Nothing durable lives here: losing the dict to
# process death costs one re-read per card, never a lost fact, so it stays on the allowed
# side of "process-local caches may not hold anything a crash must not lose".
#
# The key is the stat triple a rewrite moves. Our own writer replaces the inode (atomic.write
# renames a fresh temp file over the target) *and* calls _forget below, so an in-process edit
# can never be missed. An outside writer -- a card shell in its own process, a person's editor,
# a git checkout -- moves st_mtime_ns, and usually st_ino with it. What remains possible is an
# outside in-place rewrite landing the exact same byte count within one mtime tick of our last
# stat; on the filesystems this runs on that tick is sub-microsecond, and the cost is one stale
# dispatch of a card that is re-read on its next write -- the same window reconcile already has
# between reading a card and the claim it feeds.
_PARSED: dict[str, tuple] = {}
_PARSED_CAP = 4096          # cards seen in one long-lived process; a cache, so overflow just drops it


# Card files this process could not use: path -> (kind, reason). A card is a hand-edited file,
# so a broken one is a normal accident; what it may not do is take every *other* card down with
# it, since the traversals below walk the whole project on behalf of a single card's transition.
# They skip a card they cannot use -- and record it here, because a skip nobody can see is the
# same lost work by a quieter route. :func:`board` shows such a card as ``invalid`` so the person
# who broke the file learns which file to fix.
#
# The three kinds are cleared by different readers, and confusing them loses the diagnosis:
#   "unreadable" -- the bytes are there and do not parse (broken frontmatter, not UTF-8, a
#                   directory or an unreadable mode in the file's place). Every reader hits it,
#                   so any successful :func:`try_read` is proof the file was repaired and clears it.
#   "missing"    -- there is no file at all. That is not a broken card: an index row whose file
#                   never got written (pre-migration) is what ``board`` already shows as
#                   ``missing_file``, and a card that does not exist declares no dependencies and
#                   can hide no cycle. So it is kept apart from "unreadable" -- it is not listed
#                   as a card that failed to parse, and it may not make a caller fail closed --
#                   and, like "unreadable", any successful read clears it.
#   "invalid"    -- it parses, but is not a usable card (an at-rest status no lifecycle can
#                   produce, generation < 1, a field of the wrong type). ``try_read`` succeeds on
#                   such a file, so only :func:`reconcile_one` -- the one reader that validates --
#                   may clear it. Letting try_read clear it too meant the diagnosis died on the
#                   next board() or dependency walk, and the board went on printing the stale
#                   index status of a card that could never be dispatched.
_INVALID: dict[str, tuple[str, str]] = {}


def _forget(path):
    _PARSED.pop(str(path), None)
    _INVALID.pop(str(path), None)


def _note_invalid(path, error, kind):
    # "no file there" is its own answer, never a parse failure: only a caller holding bytes it
    # cannot make sense of has something to fail closed on.
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
    ``missing_file`` instead, which says the repairable thing (``misaka init --migrate``).
    """
    return {path: reason for path, (kind, reason) in _INVALID.items() if kind != "missing"}


def _write_file(path, text):
    atomic.write_text(path, text)
    _forget(path)


def read(path):
    """``{"fields": frontmatter, "body": contract text}`` for one card file."""
    parsed = parse_frontmatter(Path(path).read_text(encoding="utf-8"))
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
           priority=0, executor=None, origin_session=None, after_row=None, needs=()):
    """The one front door for new cards: index row first (it mints the id), then ``after_row(tid)``
    for whatever else the row must be tied to (a research link), then the file. Any failure before
    the file exists rolls the row back -- no card exists without its truth, and no file without its row."""
    workspace = tasks.canonical_workspace(workspace)
    needs = list(dict.fromkeys(needs))
    for parent_id in needs:
        parent = tasks.get(con, parent_id)
        if parent is None or parent["workspace"] != workspace:
            raise ValueError("A dependency must name an existing task in the same project.")
    tid = tasks.create_task(con, title, body=body, assignee=assignee, model=model,
                            priority=priority,
                            executor=executor, reviewer=reviewer, workspace=workspace,
                            origin_session=origin_session)
    fields = {"id": tid, "title": title, "status": "ready", "generation": 1, "assignee": assignee,
              "reviewer": reviewer, "executor": executor, "model": model,
              "priority": priority,
              "origin_session": origin_session, "created_at": _now_iso()}
    text = body.strip() + f"\n\n{LOG_HEADING}\n- {_now_iso()} last-order: created\n"
    try:
        if after_row is not None:
            after_row(tid)
        if needs:
            # Publish the entire contract at once, never a ready file with half its edges.
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
    with FileLock(os.path.join(tasks.task_state_dir(task_id), "card.lock")):   # outside the tracked tree
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


STUCK_SECONDS = 24 * 3600       # a card blocked this long is waiting on nobody in particular
FAILING_AFTER = 2               # consecutive failed attempts before the board says so


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
        # Unparsable, or parsed and not a usable card (``rebuild`` above just decided that):
        # either way ``reconcile_one`` refuses it, so it can never be dispatched. Printing the
        # index row's status -- a mirror last written when the file was still good -- would tell
        # the reader the card is ``ready`` while nothing will ever pick it up. Name the file.
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
    # Deleting the file is the last repair a broken card can get: nothing will ever read it again,
    # so nothing would ever clear its diagnosis, and it would go on being reported for the life of
    # the process (and, through board(), to the person who just removed it).
    _forget(path)
    attachments = attachment_dir(workspace, task_id)
    if os.path.isdir(attachments):
        import shutil
        try:
            shutil.rmtree(attachments)
        except OSError as error:
            return False, f"Card {task_id} was deleted but its attachments remain ({error}): {attachments}"
    if tracked and not repo.commit(workspace, git_paths, f"card {task_id}: delete"):
        return False, f"Card {task_id} was deleted but the deletion could not be committed to git; commit it by hand."
    # Only say the file survives in git when git actually had it. `create()` never commits
    # cards/, so unless something committed the card later the file is simply gone -- say so
    # rather than send the user to a history that holds nothing.
    msg += (" Its file, log, and attachments stay in the project repository's history."
            if tracked else
            " Its file, log, and attachments were deleted from disk; they were never committed "
            "to git, so there is no copy left.")
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
                  "priority": row["priority"],
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
        "origin_session": fields.get("origin_session"), "status": status, "generation": generation,
    }


_MIRRORED = ("title", "body", "assignee", "reviewer", "executor", "model", "priority",
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
        info = os.stat(path, follow_symlinks=False)     # lstat: a symlinked card is not a card
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
            return None                     # a card we cannot use is re-read next pass, never cached
        fields, body = card["fields"], card["body"]
        try:
            if str(fields.get("id") or "") != str(task_id):
                raise ValueError(f"Card names id {fields.get('id')!r}, but its file is {task_id}.md")
            values = _card_values(fields, body)
        except (TypeError, ValueError) as error:
            # Parses, but is not a card any lifecycle could have written. This is the only reader
            # that can tell, so it is also the only one allowed to clear the note below.
            _note_invalid(path, error, "invalid")
            return None
        _INVALID.pop(path, None)
        if len(_PARSED) >= _PARSED_CAP:
            _PARSED.clear()
        _PARSED[path] = (key, fields, body, values)
    # The caller gets its own mapping: an edit to the returned fields must not rewrite the
    # parse every later reconcile will answer with.
    card = {"fields": dict(fields), "body": body}
    row = con.execute(
        "SELECT workspace," + ",".join(_MIRRORED) + " FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        return card if tasks.insert_index_row(con, fields, body, workspace=workspace) else None
    # The stored workspace is written canonical; re-resolving it is for rows an older build
    # left behind, so only a string that differs is worth another walk of the filesystem.
    if row["workspace"] != workspace and tasks.canonical_workspace(row["workspace"]) != workspace:
        return None
    if row["status"] in {"running", "review"}:
        return card                         # an in-flight contract is immutable until it rests again
    if all(row[column] == values[column] for column in _MIRRORED):
        return card
    # The status the SELECT above saw is part of the WHERE: between it and here another process
    # can have claimed this card (``claim`` sets status='running' and the lease in one CAS), and
    # this mirror is then holding a snapshot that is simply out of date. Writing it anyway would
    # put the row back to 'ready' with the claimer's lock still on it -- every fenced write that
    # worker makes afterwards fails, nobody can re-claim it, and the round's work is lost. Zero
    # rows affected is that case, and it is not an error: the file's at-rest status is only ever
    # a mirror of the live state, and the next pass reads both again.
    con.execute(
        "UPDATE tasks SET title=?,body=?,assignee=?,reviewer=?,executor=?,model=?,priority=?,"
        "origin_session=?,status=?,generation=? WHERE id=? AND status=?",
        (*(values[column] for column in _MIRRORED), task_id, row["status"]),
    )
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
        needs = _needs(card)          # one read per card: reconcile_one already parsed it
        if needs is None:
            continue
        contracts[task_id] = needs

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


def init_project(folder, *, draft_brief=True):
    """Make a folder a MISAKA project: a git repository with the skeleton (PROJECT.md,
    cards/) and its cards indexed. Only a repository this call itself created gets the
    initial commit; an existing repository is never committed to."""
    folder = tasks.canonical_workspace(folder)
    _refuse_whole_home(folder)
    actions = []
    fresh = not repo.enabled(folder)  # a worktree's .git is a file, not a directory
    if fresh:
        _git(folder, "init", "-q")
        actions.append("git repository created")
    os.makedirs(os.path.join(folder, "cards"), exist_ok=True)
    project_md = os.path.join(folder, "PROJECT.md")
    if draft_brief and not os.path.exists(project_md):
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
        # Only the skeleton this call wrote, never `git add -A`. A folder that is not
        # already a repository may be full of things that are not project material, and
        # the first commit is the one place nobody reviews: run in a home directory,
        # `add -A` quietly commits ssh keys, .env files and whatever is in Downloads.
        # Existing repositories are not committed to at all, so this is the only path.
        skeleton = [name for name in ("PROJECT.md", "cards")
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
            "  mkdir ~/research && cd ~/research && misaka init")


def _cfg_db():
    from misaka.config import CFG
    return CFG["db"]

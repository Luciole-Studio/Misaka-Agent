"""Execute durable Sister task cards."""
import contextlib
import json
import logging
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from misaka.config import profiles
from misaka.core.network.card_contract import Finding
from misaka.core.platform import owners
from misaka.core.platform import tasks as task_store
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.platform.session import run_coro, run_session
from misaka.core.platform.vocabulary import MANAGEMENT_TOOLS
from misaka.utils.paths import posix_relpath

logger = logging.getLogger(__name__)

MAX_ARTIFACT_PATH_CHARS = 1_024


def over_cap(usage_db, task_id, cap):
    """Whether the scope ``task_id`` belongs to has spent ``cap``: a card is not started then."""
    from misaka.core.platform import budget
    cap = budget.default_cap() if cap is None else int(cap or 0)
    if not usage_db or not task_id or not cap:
        return False
    return budget.status_path(str(usage_db), cap, task_id=str(task_id))["mode"] == "stop"


def budget_cap_reason():
    from misaka.core.platform import budget
    return f"{budget.EXHAUSTED_MESSAGE}; the card stays ready"


def stopped_at_cap(error):
    """Whether a run ended because a request did not fit under the token cap."""
    from misaka.core.platform import budget
    return budget.EXHAUSTED_MESSAGE in str(error or "")


COMPLETION_INSTRUCTIONS = """

---
## Completion
- When the work is complete, call `misaka_card_complete` with a concise summary, then end the turn. The system
  submits the card when that turn ends; do not write a submission file.
- A turn that ends without that call leaves the card running: that is how to answer a message from Last Order or
  report progress. A card whose contract names a deliverable cannot be completed until that file exists there.
- Put deliverables in the requested location: files written into the card's output directory are collected
  automatically.
- When the card asks for findings, record final evidence-backed findings and concrete uncertainties with
  `misaka_card_note`.
"""


class IncompleteSubmission(ValueError):
    """The turn ended without what the card's kind requires; the model can supply it."""


_DELIVERABLE_CLAUSE = re.compile(r"^##[ \t]+deliverable[ \t]*\r?$", re.MULTILINE | re.IGNORECASE)


def _card_field(task, key):
    """A card row (``sqlite3.Row``) or a plain dict: rows have no ``get``."""
    try:
        return task[key]
    except (KeyError, IndexError, TypeError):
        return None


def contract_deliverable(task):
    """The file the card's contract names under ``## deliverable``, or None for a card without one."""
    from misaka.core.network.card_contract import split_deliverable

    body = str(_card_field(task, "body") or "")
    matches = list(_DELIVERABLE_CLAUSE.finditer(body))
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Invalid deliverable: the card has multiple deliverable sections.")
    section = re.split(r"^##[ \t]+", body[matches[0].end():], maxsplit=1, flags=re.MULTILINE)[0]
    return split_deliverable(section)[0]


COMPLETION_EVENT = "completion_declared"


def claim_env(task_id):
    """``(generation, claim_lock)`` this process holds on ``task_id``, or ``(None, None)``.

    Whoever claimed the card -- the daemon for a pane, the dispatcher, a Last Order's Sister
    runtime -- hands the claim to the process doing the work through these variables, and to
    that process's own helpers (an ally's MCP bridge) the same way."""
    owner = os.environ.get("MISAKA_SISTER_OWNER_TASK_ID") or os.environ.get("MISAKA_USAGE_TASK_ID")
    generation = (os.environ.get("MISAKA_SISTER_OWNER_GENERATION")
                  or os.environ.get("MISAKA_USAGE_GENERATION"))
    claim_lock = (os.environ.get("MISAKA_SISTER_OWNER_CLAIM_LOCK")
                  or os.environ.get("MISAKA_USAGE_CLAIM_LOCK"))
    if owner != task_id or not generation or not claim_lock:
        return None, None
    try:
        return int(generation), claim_lock
    except ValueError:
        return None, None


CONSULT_RUN_ENV, CONSULT_LOCK_ENV = "MISAKA_CONSULT_RUN", "MISAKA_CONSULT_LOCK"


def consult_env(task_id):
    """``(generation, run_id, lock)`` of the consultation this process was started to answer
    (``tasks.start_consultation``), or ``(None, None, None)``. The daemon hands it over as it
    hands a claim; the card's generation is the one being consulted, and stays."""
    owner = os.environ.get("MISAKA_USAGE_TASK_ID")
    generation = os.environ.get("MISAKA_USAGE_GENERATION")
    run_id, lock = os.environ.get(CONSULT_RUN_ENV), os.environ.get(CONSULT_LOCK_ENV)
    if owner != task_id or not generation or not run_id or not lock:
        return None, None, None
    try:
        return int(generation), run_id, lock
    except ValueError:
        return None, None, None


def attempt_env(task_id):
    """``(generation, claim_lock)`` this process works ``task_id`` under: its claim, or for a
    consultation the consulted generation with no claim (``claim_lock`` None)."""
    generation, claim_lock = claim_env(task_id)
    if generation is not None:
        return generation, claim_lock
    return consult_env(task_id)[0], None


def owned_card(con, task_id, generation, claim_lock):
    """The running card this attempt still owns, its lease renewed; None once it is not."""
    row = task_store.get(con, task_id)
    if (generation is None or row is None or row["status"] != "running"
            or int(row["generation"]) != generation or row["claim_lock"] != claim_lock):
        return None
    if not task_store.heartbeat(con, task_id, claim_lock, generation=generation, ttl_seconds=1800):
        return None
    return row


def declare_completion(con, row, summary):
    """Record that this attempt's work is done. The host submits it when the turn ends: a
    Sister's session at ``agent_settled``, an ally's runner after its ACP turn. A board event,
    so a declaration made in another process (an ally's MCP bridge) reaches the host too."""
    if not task_store.add_event(con, row["id"], COMPLETION_EVENT, {"summary": summary, "claim_lock": row["claim_lock"]},
                                generation=row["generation"], claim_lock=row["claim_lock"]):
        raise ValueError("Card ownership changed before its completion was recorded.")


def attempt_payload(con, row, kind):
    """What this attempt of the card recorded under ``kind``, or None. A card sent back to ready --
    a retry, a dead worker reclaimed, a reviewer asking for changes -- keeps its generation, so an
    event of the generation can be an earlier attempt's: only one stamped with this attempt's claim
    lock counts. Before, the next attempt submitted with the last one's summary (0.18.10 sweep)."""
    raw = task_store.latest_payload(con, row["id"], kind, generation=row["generation"])
    try:
        value = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    # A settled card has no claim left: its redeclaration (a red team revising its critique after a
    # note) reads what its last attempt recorded, as before.
    return value if row["claim_lock"] is None or value.get("claim_lock") == row["claim_lock"] else None


def declared_completion(con, row):
    """The summary this attempt declared with ``misaka_card_complete``, or None."""
    value = attempt_payload(con, row, COMPLETION_EVENT)
    return str(value.get("summary") or "") if value is not None else None


def missing_deliverable(task):
    """The contract deliverable that is not yet a non-empty file under the card's output
    directory, or None when the card has no contract deliverable or it is in place.

    A turn that ends in plain text used to complete the card whatever it had produced
    (2026-09-18, B27): every note Last Order sent a running Sister -- "continue", "not
    approved, rework", a side errand -- ended a turn, and the turn ended the card, deliverable
    or not, even when the Sister had just written "not submitting yet". The contract names
    the file; until it exists the card is not done, whatever the turn said."""
    try:
        name = contract_deliverable(task)
    except ValueError as error:
        return str(error)  # malformed contracts block completion, including the idle-turn notice
    return None if name is None or deliverable_path(task, name) else name


def deliverable_path(task, name):
    """The delivered file for contract name ``name`` under the card's output directory, or None.
    The Sister's file may differ from the contract name in a character or two: Last Order's
    plans misspell names (2026-09-28: 莱茅邦联 for 莱茵邦联, 狂太 for 犹太), and holding her to the
    misspelling only made her copy the file under it."""
    output_dir = _card_field(task, "output_dir")
    if not output_dir:
        return None
    try:
        root = Path(output_dir).resolve(strict=True)
        workspace = _card_field(task, "workspace")
        if workspace:
            root.relative_to(Path(workspace).resolve(strict=True))
        candidates = [name] + sorted(
            entry for entry in os.listdir(root)
            if entry != name and len(entry) == len(name) and os.path.splitext(entry)[1] == os.path.splitext(name)[1]
            and sum(a != b for a, b in zip(entry, name, strict=True)) <= 2)
        for candidate in candidates:
            path = (root / candidate).resolve(strict=True)
            path.relative_to(root)  # a symlink outside this card is not its deliverable
            if path.is_file() and path.stat().st_size > 0:
                return path
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def card_handoffs(con, task):
    """What the card's finished dependencies delivered: id, title, summary, artifacts.

    A card that ``needs`` others starts only once they are done, and their results are the
    reason it exists; without this the Sister had to know to go and look. Only the latest
    generation's submission counts, the one the parent's ``done`` stands on.
    """
    out = []
    for parent_id in task_store.parent_ids(con, task["id"]):
        parent = task_store.get(con, parent_id)
        if parent is None:
            continue
        try:
            submitted = json.loads(task_store.latest_payload(
                con, parent_id, "submitted", generation=parent["generation"]) or "{}")
        except (TypeError, ValueError):
            submitted = {}
        if not isinstance(submitted, dict) or not submitted.get("summary"):
            continue
        out.append({"id": parent_id, "title": parent["title"],
                    "summary": str(submitted["summary"]),
                    "artifacts": [str(a) for a in submitted.get("artifacts") or []]})
    return out


_MATERIAL_TITLE_CHARS = 200    # an HTML <title> is cut there already; a downloaded .md's front matter is not
_MATERIALS_LIMIT = 40   # lines of the "already downloaded" list; the rest is counted, not listed


def _download_dir_name():
    # The one folder name the web tools save under; imported lazily to keep worker importable alone.
    from misaka.core.tools.path_utils import DOWNLOAD_DIR_NAME
    return DOWNLOAD_DIR_NAME


def materials_on_hand(workspace):
    """What is already downloaded into the workspace, as a prompt section -- so a Sister reads
    it instead of fetching it again: every file under downloads/, a saved page with the URL and
    title it came from, and the corpus document ID when a download was indexed on arrival.
    Returns "" when there is nothing to show."""
    if not workspace:
        return ""
    from misaka.core.web.evidence import read_provenance
    root = os.path.join(workspace, _download_dir_name())
    if not os.path.isdir(root):
        return ""
    doc_ids = {}
    with contextlib.suppress(Exception):   # the list is a convenience; a corpus fault must not stop the card
        from misaka.core.documents import index as corpus
        for doc in corpus.docs(workspace=workspace):
            for source in [doc.get("orig_path"), *(doc.get("paths") or [])]:
                if source:
                    doc_ids[os.path.realpath(source)] = doc["doc_id"]
    files = []
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            if name.startswith(".") or name.endswith(".part"):
                continue
            files.append(os.path.join(base, name))
    if not files:
        return ""
    lines = []
    for path in files[:_MATERIALS_LIMIT]:
        relative = posix_relpath(path, workspace)
        line = f"- `{relative}`"
        if path.endswith(".md"):
            try:
                provenance = read_provenance(path)
            except (OSError, UnicodeError):
                # Optional metadata must not hide the path or prevent card startup.
                provenance = {}
            url = (provenance.get("source_url") or provenance.get("requested_url")
                   or provenance.get("final_url") or provenance.get("url"))
            if url:
                line += f" ← {url}"
            if provenance.get("title"):
                title = " ".join(str(provenance["title"]).split())
                line += f' "{title[:_MATERIAL_TITLE_CHARS]}"'
        doc_id = doc_ids.get(os.path.realpath(path))
        if doc_id:
            line += f" (doc {doc_id})"
        lines.append(line)
    if len(files) > _MATERIALS_LIMIT:
        lines.append(f"- … and {len(files) - _MATERIALS_LIMIT} more under `{_download_dir_name()}/`")
    # URLs and titles come from the pages themselves: web_fetch fences a title, and reading it
    # back out of the saved file's front matter must not unfence it (issue #10 audit, M5).
    lines = [untrusted("materials", "\n".join(lines))]
    lines.append("This material list is a snapshot. Reuse relevant items; check the current workspace "
                 "or an available document index when you need to discover additional or newer material.")
    return "\n".join(lines)


def card_extras(con, task, cfg=None, *, include_materials=True):
    """A workflow's context and the materials on hand, shared by every card entry point.

    Who else is on the board reaches a session through its system prompt's routing catalog,
    never through the task body. Restore paths can skip materials they will not publish,
    avoiding unrelated filesystem reads.
    """
    extras = {"_owner_context": None, "_owner": None, "_materials": "", "_session_overrides": {}}
    with contextlib.suppress(Exception):   # a board a workflow has never touched is an ordinary board
        owner, context = owners.card(con, task["id"])
        if owner is not None:
            extras["_owner_context"], extras["_owner"] = context, owner.name
            extras["_session_overrides"] = owner.session_overrides(con, context)
    if include_materials:
        extras["_materials"] = materials_on_hand(task["workspace"])
    return extras


def card_prompt(task):
    """Build the durable card contract shared by foreground and Sister runtimes."""
    body = task.get("body") or task.get("title") or ""
    if review_feedback := task.get("review_feedback"):
        body = (
            "⚠️ Independent reviewer requested changes. Address each item and resubmit:\n"
            + str(review_feedback) + "\n\n---\n\n" + body
        )
    if handoffs := task.get("_handoffs"):
        sections = []
        for item in handoffs:
            section = f"### {item['id']} {item['title']}\n{item['summary']}"
            if item.get("artifacts"):
                section += "\nArtifacts: " + ", ".join(f"`{a}`" for a in item["artifacts"])
            sections.append(section)
        body += ("\n\n## Handoff from parent cards\n"
                 "This card waited for these cards; their results are its starting point.\n\n"
                 + "\n\n".join(sections))
    if attachments := task.get("_attachments"):
        lines = []
        for item in attachments:
            target = item.get("path") or item.get("source") or item.get("name")
            lines.append(f"- Attachment: `{target}`")
        body += "\n\n## Task attachments\n" + "\n".join(lines)
    if materials := task.get("_materials"):
        body += "\n\n## Materials already in this workspace\n" + materials
    if output_dir := task.get("output_dir"):
        body += (
            "\n\n## Deliverable location\n"
            f"Write every new deliverable under `{output_dir}`. Files there are recorded automatically."
        )
    if failure := task.get("last_failure_error"):
        body += (
            "\n\n## Previous attempt\n"
            f"The previous attempt on this card failed: {failure}\n"
            f"Consecutive failures so far: {int(task.get('consecutive_failures') or 0)} of "
            f"{task_store.FAILURE_LIMIT} allowed. If the work is already complete, verify it, call "
            "`misaka_card_complete`, and end this turn with the plain-text summary. A session that ends without that call "
            "counts as another failure regardless of what it did."
        )
    prompt = body + COMPLETION_INSTRUCTIONS
    if task.get("beast"):
        from misaka.core.platform import budget as _b

        prompt += _b.BEAST_SUFFIX
    return prompt


def _contract_artifact(task, root):
    """The contract deliverable, as the card's product whether or not this attempt rewrote it. A
    card reopened to declare again left an unchanged deliverable out of its submission -- only
    files changed since the attempt's baseline were listed -- and the run lost the file's
    registration (2026-09-27, a divergence review after a resume)."""
    try:
        name = contract_deliverable(task)
    except ValueError:
        return None
    path = deliverable_path(task, name) if name else None
    return _artifact(root, posix_relpath(path, root)) if path else None


def _artifact(root, value):
    """Return one existing workspace-relative regular file, else ``None``."""
    if not isinstance(value, str) or not value or "\0" in value or len(value) > MAX_ARTIFACT_PATH_CHARS:
        return None
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if posix.is_absolute() or windows.drive or windows.root or ".." in posix.parts or ".." in windows.parts:
        return None
    try:
        path = (root / value).resolve(strict=True)
        path.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return posix_relpath(path, root) if path.is_file() else None


def _output_files(root, workspace):
    """Workspace-relative files under ``root``, nested ones included; hidden entries and the
    board's own folders are not deliverables."""
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("cards", "node_modules"))
        out.extend(posix_relpath(os.path.join(base, name), workspace)
                   for name in sorted(files) if not name.startswith("."))
    return out


def _output_snapshot(task, root):
    """Current deliverables keyed by a revision stronger than mtime alone."""
    output_dir = task.get("output_dir")
    if not output_dir:
        return {}
    from misaka.utils.paths import getFileRevision

    output = Path(output_dir).resolve()
    output.relative_to(root)
    if not output.is_dir():
        return {}
    snapshot = {}
    for value in _output_files(str(output), str(root)):
        relative = _artifact(root, value)
        revision = getFileRevision(str(root / relative)) if relative else None
        if revision is not None:
            snapshot[relative] = list(revision)
    return snapshot


def _source_artifact(root, output_dir, value, artifacts):
    """Resolve evidence aliases only to files already collected for this card."""
    if path := _artifact(root, value):
        return path
    if not isinstance(value, str) or not value or len(value) > MAX_ARTIFACT_PATH_CHARS:
        return None
    if ".." in PurePosixPath(value).parts or ".." in PureWindowsPath(value).parts:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        if not output_dir:
            return None
        candidate = Path(output_dir) / candidate
    try:
        relative = candidate.resolve(strict=True).relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None
    return relative if relative in artifacts else None


def record_output_baseline(con, task):
    """Freeze output_dir once per generation, before its first model turn."""
    task = dict(task)
    if not task.get("output_dir") or con.execute(
        "SELECT 1 FROM events WHERE task_id=? AND kind='artifact_baseline' "
        "AND generation=? LIMIT 1",
        (task["id"], task["generation"]),
    ).fetchone():
        return
    root = Path(task["workspace"]).resolve(strict=True)
    task_store.add_event(
        con,
        task["id"],
        "artifact_baseline",
        {"files": _output_snapshot(task, root)},
        generation=task["generation"],
        claim_lock=task["claim_lock"],
    )


def build_submission(con, task, summary):
    """Build the system-owned submission for one completed Sister run."""
    task = dict(task)
    root = Path(task["workspace"]).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("card workspace is not a directory")

    artifacts = []
    output_dir = task.get("output_dir")
    if output_dir:
        current = _output_snapshot(task, root)
        baseline_row = con.execute(
            "SELECT payload FROM events WHERE task_id=? AND kind='artifact_baseline' "
            "AND generation=? ORDER BY id LIMIT 1",
            (task["id"], task["generation"]),
        ).fetchone()
        try:
            baseline = json.loads(baseline_row["payload"] or "{}").get("files")
        except (AttributeError, TypeError, ValueError):
            baseline = None
        if not isinstance(baseline, dict):
            # Every generation freezes one before its first model turn (record_output_baseline).
            raise ValueError("output directory baseline is missing for this generation")
        artifacts.extend(path for path, revision in current.items() if baseline.get(path) != revision)

    for event in con.execute(
        "SELECT payload FROM events WHERE task_id=? AND kind='artifact_written' "
        "AND generation=? ORDER BY id",
        (task["id"], task["generation"]),
    ):
        try:
            value = json.loads(event["payload"] or "{}").get("path")
        except (AttributeError, TypeError, ValueError):
            continue
        if path := _artifact(root, value):
            artifacts.append(path)
    if path := _contract_artifact(task, root):
        artifacts.append(path)

    # Notes append material. Only critique issues are a complete snapshot (below).
    # Never let an uncertainty-only note erase the findings recorded earlier.
    findings, uncertain = [], []
    for event in con.execute(
        "SELECT payload FROM events WHERE task_id=? AND kind='card_evidence' "
        "AND generation=? ORDER BY id", (task["id"], task["generation"]),
    ):
        evidence = json.loads(event["payload"])
        findings.extend(Finding.model_validate(item).model_dump(exclude_none=True)
                        for item in evidence.get("findings", []))
        uncertain.extend(evidence.get("uncertain", []))
    for finding in findings:
        if path := _source_artifact(root, output_dir, finding.get("source_file"), artifacts):
            finding["source_file"] = path
            artifacts.append(path)

    submission = {
        "summary": str(summary or task.get("title") or "Task completed.").strip(),
        "artifacts": list(dict.fromkeys(artifacts)),
        "notes": "",
        "uncertain": uncertain,
        "findings": findings,
    }
    owner, context = owners.card(con, task["id"])
    if owner is not None:
        owner.submission(con, task, context, submission, lambda kind: attempt_payload(con, task, kind))
    return submission


def card_session_setup(task, workspace, profile_dir, provider, default_model):
    """Build the shared session configuration for headless and interactive cards."""
    model = profiles.explicit_model_override(profile_dir, task["model"])
    os.makedirs(workspace, exist_ok=True)
    state_dir = task_store.task_state_dir(task["id"])
    os.makedirs(state_dir, exist_ok=True)
    beast = isinstance(task, dict) and task.get("beast")
    prompt = card_prompt(task)

    role = profiles.role_of(profile_dir)
    from misaka.config import sessions as session_roots

    # New cards follow saved thinking defaults; resumed cards keep their native setting.
    flags = ["--session-dir", session_roots.card_session_dir(task)]
    if model:
        flags += ["--model", model]
    sender = role.rsplit("/", 1)[-1]
    from misaka.core.wiring import SessionSpec, assemble, spec_overrides

    kind = "beast" if beast else "card"
    delegates = not profiles.is_last_order(profile_dir)
    if beast:
        # Beast mode gets no builtin tools: with DELEGATE it keeps only the child-management
        # tools, otherwise none (Last Order has no DELEGATE, so it runs tool-less).
        if delegates:
            flags += ["-t", ",".join(MANAGEMENT_TOOLS)]
        else:
            flags += ["-nt"]
        # A `-t` list without `mcp__` entries keeps MCP tools registered (pi 1.0.4); a wrap-up turn
        # starts no MCP servers at all.
        flags.append("--no-mcp")
    assembly = assemble(SessionSpec(
        profile_dir=profile_dir,
        role=role,
        workspace=workspace,
        kind=kind,
        sender=sender,
        receive_messages=True,          # a card hears Last Order (and her Sisters) at its next tool boundary
        task_id=task.get("id"),
        tool_ceiling=MANAGEMENT_TOOLS if beast and delegates else None,
        workflow_context=bool(task.get("_owner_context")),
        overrides=spec_overrides(task.get("_session_overrides")),
    ))
    from misaka.config import identity
    for section in identity.base_prompt_sources(profile_dir, role):
        flags += ["--append-system-prompt", section]
    return flags, assembly, prompt, role


def continue_flags(session_file, session_dir):
    """Engine flags that go on in a card's own conversation, or None when it has none: the newest
    transcript in its session folder -- after an in-pane fork the recorded path is the pre-fork
    line and the branched file is newer -- else the recorded one. One rule for a pane
    (``card_shell``) and a headless continuation (``run_card``)."""
    try:
        files = [os.path.join(session_dir, n) for n in os.listdir(session_dir) if n.endswith(".jsonl")]
    except OSError:
        files = []
    try:
        newest = max(files, key=os.path.getmtime) if files else None
    except OSError:
        newest = None
    if newest:
        return ["--session", newest]
    if session_file and os.path.isfile(session_file):
        return ["--session", session_file]
    return None


def run_card(
    task,
    workspace,
    profile_dir,
    provider,
    default_model,
    on_event,
    *,
    usage_db=None,
    usage_generation=None,
    usage_claim_lock=None,
    usage_token_cap=None,
    con=None,
):
    """Run one task card; its session lifecycle finalizes the board row."""
    task_id = task.get("id") if isinstance(task, dict) else None
    flags, assembly, prompt, role = card_session_setup(
        task, workspace, profile_dir, provider, default_model
    )
    if task.get("_say") is not None:
        # A settled card continued with a message (``dispatch.run_task(say=...)``): the turn goes on
        # in her own conversation and is the message alone, as ``card-shell --resume --say`` is in
        # a pane -- the contract she already worked from is not sent again.
        from misaka.config import sessions as session_roots
        flags += continue_flags(task.get("session_file"), session_roots.card_session_dir(task)) or []
        prompt = task["_say"]
    from misaka.config import env as env_file

    env = {**env_file.role_overlay(profile_dir),      # the Sister's own .env, then the card's hand-off names
           "MISAKA_PROFILE_DIR": profile_dir,
           "MISAKA_WHO": role,
           "MISAKA_MCP_ROLE": role,
           "MISAKA_WORKSPACE": workspace,
           "MISAKA_TASK_OUTPUT_DIR": str(task.get("output_dir") or workspace)}
    if usage_db and task_id and usage_generation is not None:
        env.update({
            "MISAKA_USAGE_DB": str(usage_db),
            "MISAKA_USAGE_TASK_ID": str(task_id),
            "MISAKA_USAGE_GENERATION": str(usage_generation),
            "MISAKA_USAGE_TOKEN_CAP": str(int(usage_token_cap or 0)),
        })
        if usage_claim_lock:
            env["MISAKA_USAGE_CLAIM_LOCK"] = str(usage_claim_lock)
    if over_cap(usage_db, task_id, usage_token_cap):
        return {"ok": False, "reason": budget_cap_reason(), "exit_code": 0, "budget_stop": True}
    r = run_coro(run_session(flags, prompt, workspace, on_event=on_event, timeout=None,
                             assembly=assembly, env=env))

    row = task_store.get(con, task_id) if con is not None and task_id else None
    if row is not None and row["status"] != "running":
        return {"ok": True, "settled": True, "exit_code": 0}
    reason = r["error"] or "session exited before the card was finalized"
    if stopped_at_cap(r["error"]):
        return {"ok": False, "reason": reason, "exit_code": 0, "budget_stop": True}
    return {
        "ok": False,
        "reason": reason,
        "exit_code": 1 if r["error"] else 0,
        "stderr_tail": r["error"] or "",
    }

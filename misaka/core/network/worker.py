"""Execute durable Sister task cards."""
import asyncio
import contextlib
import json
import logging
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from misaka.config import profiles
from misaka.core.platform import tasks as task_store
from misaka.core.platform.session import run_coro, run_session
from misaka.core.platform.vocabulary import MANAGEMENT_TOOLS
from misaka.core.skills import sandbox as skill_sandbox

logger = logging.getLogger(__name__)

MAX_ARTIFACT_PATH_CHARS = 1_024
RESERVATION_HEARTBEAT_SECONDS = 300


def _reserve_usage(usage_db, usage_task_id, usage_generation, usage_token_cap, timeout):
    if not usage_db or not usage_task_id or usage_generation is None:
        return {"allowed": True, "token": None}
    from misaka.core.platform import budget

    return budget.reserve_agent_path(
        str(usage_db),
        usage_token_cap,
        str(usage_task_id),
        int(usage_generation),
        ttl_seconds=600 if timeout is None else max(60, int(timeout) + 60),
    )


def _release_usage(usage_db, reading):
    token = reading.get("token") if isinstance(reading, dict) else None
    if token and usage_db:
        from misaka.core.platform import budget

        budget.release_agent_path(str(usage_db), token)


async def _run_with_reservation(coro, usage_db, reservation):
    """Keep an unbounded card run's finite budget lease alive."""
    token = reservation.get("token") if isinstance(reservation, dict) else None
    if not token or not usage_db:
        return await coro
    task = asyncio.create_task(coro)
    try:
        while True:
            done, _pending = await asyncio.wait(
                {task}, timeout=RESERVATION_HEARTBEAT_SECONDS
            )
            if done:
                return await task
            from misaka.core.platform import budget

            alive = await asyncio.to_thread(
                budget.touch_agent_path, str(usage_db), token, 600
            )
            if alive:
                continue
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return {
                "text": None,
                "timed_out": False,
                "error": "shared token budget lease lost",
                "budget_usage": None,
            }

    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class _UsageRecorder:
    """Account a root worker turn before releasing its parallel reservation."""

    def __init__(self, callback, usage_db, task_id, generation, reservation):
        self.callback = callback
        self.usage_db = usage_db
        self.task_id = task_id
        self.generation = generation
        self.reservation = reservation
        self.fallback_tokens = 0
        self.observed_tokens = 0
        self.delivered_tokens = 0

    @staticmethod
    def _tokens(line):
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            return 0
        if event.get("type") != "agent_end":
            return 0
        total = 0
        for message in event.get("messages") or []:
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue
            if isinstance(usage.get("totalTokens"), int):
                total += usage["totalTokens"]
            else:
                total += sum(
                    int(usage.get(key) or 0)
                    for key in (
                        "input",
                        "output",
                        "cacheRead",
                        "cacheWrite",
                        "input_tokens",
                        "output_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
        return total

    def __call__(self, line):
        total = self._tokens(line)
        self.observed_tokens += total
        delivered = False
        if self.callback:
            try:
                delivered = self.callback(line) is True
            except Exception:  # noqa: BLE001 - accounting fallback must survive observer failure
                delivered = False
        if total and not delivered:
            self.fallback_tokens += total
        elif total:
            self.delivered_tokens += total

    def settle(self, accounted_tokens=None):
        if not self.usage_db or not self.task_id or self.generation is None:
            return
        from misaka.core.platform import budget

        token = self.reservation.get("token") if self.reservation else None
        target = max(
            self.observed_tokens,
            max(0, int(accounted_tokens)) if accounted_tokens is not None else 0,
        )
        # Successfully delivered harn_event usage is already in the same
        # ledger.  Commit only the missing compaction/fallback delta while
        # atomically releasing the reservation.
        additional = max(0, target - self.delivered_tokens)
        if additional:
            budget.commit_agent_usage_path(
                str(self.usage_db),
                token,
                str(self.task_id),
                int(self.generation),
                additional,
            )
        else:
            _release_usage(self.usage_db, self.reservation)


def install_card_usage(session, *, usage_db, task_id, generation):
    """Record a pane's completed requests, not historical messages or its exit.

    Only card-shell opts in: headless workers and managed children already meter
    their calls. Observe session events without wrapping the provider's stream.
    """
    from misaka.core.platform.session import event_line
    from misaka.utils.values import read_field

    recorder = _UsageRecorder(None, usage_db, task_id, generation, None)

    def flush():
        nonlocal recorder
        if not recorder.observed_tokens:
            return
        try:
            recorder.settle()
        except Exception:  # a receipt failure must not discard the model's response
            logger.warning("Card %s generation %s: %s tokens remain unrecorded",
                           task_id, generation, recorder.observed_tokens, exc_info=True)
        else:
            recorder = _UsageRecorder(None, usage_db, task_id, generation, None)

    def record(event):
        kind = read_field(event, "type")
        if kind == "message_end":
            message = read_field(event, "message")
            if read_field(message, "usage") is None:
                return
        elif kind == "compaction_end":
            usage = read_field(read_field(event, "result"), "usage")
            if usage is None:
                return
            message = {"usage": usage}
        else:
            return
        recorder(event_line({"type": "agent_end", "messages": [message]}))
        flush()

    unsubscribe = session.subscribe(record)

    def close():
        unsubscribe()
        flush()

    return close


COMPLETION_INSTRUCTIONS = """

---
## Completion
- When the work is complete, call `misaka_card_complete` with a concise summary, then end the turn. The system
  submits the card when that turn ends; do not write a submission file.
- A turn that ends without that call leaves the card running: that is how to answer a message from Last Order or
  report progress. A card whose contract names a deliverable cannot be completed until that file exists there.
- Put deliverables in the requested location. Successful `write`, `edit`, and `office` operations are recorded
  automatically.
- On a research card, record final evidence-backed findings and concrete uncertainties with `misaka_card_note`.
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
    if name is None:
        return None
    output_dir = _card_field(task, "output_dir")
    if not output_dir:
        return name
    try:
        root = Path(output_dir).resolve(strict=True)
        workspace = _card_field(task, "workspace")
        if workspace:
            root.relative_to(Path(workspace).resolve(strict=True))
        path = (root / name).resolve(strict=True)
        path.relative_to(root)  # a symlink outside this card is not its deliverable
        if path.is_file() and path.stat().st_size > 0:
            return None
    except (OSError, RuntimeError, ValueError):
        pass
    return name


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
        relative = os.path.relpath(path, workspace)
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
                line += f' "{provenance["title"]}"'
        doc_id = doc_ids.get(os.path.realpath(path))
        if doc_id:
            line += f" (doc {doc_id})"
        lines.append(line)
    if len(files) > _MATERIALS_LIMIT:
        lines.append(f"- … and {len(files) - _MATERIALS_LIMIT} more under `{_download_dir_name()}/`")
    lines.append("This material list is a snapshot. Reuse relevant items; check the current workspace "
                 "or an available document index when you need to discover additional or newer material.")
    return "\n".join(lines)


def card_extras(con, task, cfg=None, *, include_materials=True):
    """Research/material context shared by every card entry point.

    Who else is on the board reaches a session through its system prompt's routing catalog,
    never through the task body. Restore paths can skip materials they will not publish,
    avoiding unrelated filesystem reads.
    """
    extras = {"_research": None, "_materials": ""}
    with contextlib.suppress(Exception):   # a board without the research schema is an ordinary board
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_run_tasks'").fetchone():
            row = con.execute("SELECT run_id, branch_id, kind FROM research_run_tasks WHERE task_id=?",
                              (task["id"],)).fetchone()
            if row is not None:
                extras["_research"] = dict(row)
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
    return os.path.relpath(path, root) if path.is_file() else None


def _output_snapshot(task, root):
    """Current deliverables keyed by a revision stronger than mtime alone."""
    output_dir = task.get("output_dir")
    if not output_dir:
        return {}
    from misaka.core.network.ally.runner import _artifacts
    from misaka.utils.paths import get_file_revision

    output = Path(output_dir).resolve()
    output.relative_to(root)
    if not output.is_dir():
        return {}
    snapshot = {}
    for value in _artifacts(str(output), str(root), None):
        relative = _artifact(root, value)
        revision = get_file_revision(str(root / relative)) if relative else None
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
        relative = str(candidate.resolve(strict=True).relative_to(root))
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

    # Notes append material. Only critique issues are a complete snapshot (below).
    # Never let an uncertainty-only note erase the findings recorded earlier.
    from misaka.core.research.ledger import Finding
    findings, uncertain = [], []
    for event in con.execute(
        "SELECT payload FROM events WHERE task_id=? AND kind='research_evidence' "
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
    # Typed tool data, frozen with the card's completion. Never reopen a model-written JSON file.
    critique = task_store.latest_payload(con, task["id"], "research_critique", generation=task["generation"])
    if critique is not None:
        submission["issues"] = json.loads(critique)["issues"]
    elif con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_run_tasks'").fetchone():
        link = con.execute("SELECT kind FROM research_run_tasks WHERE task_id=?", (task["id"],)).fetchone()
        from misaka.core.research import runs
        if link and link["kind"] in runs.REVIEW_KINDS:
            raise IncompleteSubmission(
                "Record the red-team issues with misaka_card_note before completing the card "
                "(issues=[] if none)."
            )
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
    # The engine has no skill loading of its own; the skills extension reads the
    # card's sandbox (SessionSpec.skill_roots) and nothing else.
    from misaka.config import sessions as session_roots

    # New cards follow saved thinking defaults; resumed cards keep their native setting.
    flags = ["--session-dir", session_roots.card_session_dir(task)]
    if model:
        flags += ["--model", model]
    ro_root = os.path.join(state_dir, ".skills-ro")
    sender = role.rsplit("/", 1)[-1]
    from misaka.core.wiring import SessionSpec, assemble

    kind = "beast" if beast else "card"
    delegates = not profiles.is_last_order(profile_dir)
    skill_roots = None
    if beast:
        # Beast mode gets no builtin tools: with DELEGATE it keeps only the child-management
        # tools, otherwise none (Last Order has no DELEGATE, so it runs tool-less).
        if delegates:
            flags += ["-t", ",".join(MANAGEMENT_TOOLS)]
        else:
            flags += ["-nt"]
    else:
        # Regular cards run against read-only copies of the role's skill stack: skills are
        # read-only at run time (constitution), and a card never sees the live tree.
        skill_sandbox.snapshot_stack(profile_dir, workspace, ro_root)
        skill_roots = (("sandbox", ro_root),)
    assembly = assemble(SessionSpec(
        profile_dir=profile_dir,
        role=role,
        workspace=workspace,
        kind=kind,
        sender=sender,
        receive_messages=True,          # a card hears Last Order (and her Sisters) at its next tool boundary
        task_id=task.get("id"),
        tool_ceiling=MANAGEMENT_TOOLS if beast and delegates else None,
        skill_roots=skill_roots,
        research_context=bool(task.get("_research")),
    ))
    from misaka.config import identity
    for section in identity.base_prompt_sources(profile_dir, role):
        flags += ["--append-system-prompt", section]
    return flags, assembly, prompt, ro_root, role


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
    flags, assembly, prompt, ro_root, role = card_session_setup(
        task, workspace, profile_dir, provider, default_model
    )
    from misaka.config import env as env_file

    env = {**env_file.role_overlay(profile_dir),      # the Sister's own .env, then the card's hand-off names
           "MISAKA_PROFILE_DIR": profile_dir,
           "MISAKA_WHO": role,
           "MISAKA_MCP_ROLE": role,
           "MISAKA_WORKSPACE": workspace,
           "MISAKA_TASK_OUTPUT_DIR": str(task.get("output_dir") or workspace)}
    if os.path.isdir(ro_root):
        env["MISAKA_SKILL_SANDBOX"] = ro_root    # nested agents pin the same read-only skill snapshot
    if usage_db and task_id and usage_generation is not None:
        env.update({
            "MISAKA_USAGE_DB": str(usage_db),
            "MISAKA_USAGE_TASK_ID": str(task_id),
            "MISAKA_USAGE_GENERATION": str(usage_generation),
            "MISAKA_USAGE_TOKEN_CAP": str(int(usage_token_cap or 0)),
        })
        if usage_claim_lock:
            env["MISAKA_USAGE_CLAIM_LOCK"] = str(usage_claim_lock)
    reservation = _reserve_usage(
        usage_db,
        task_id,
        usage_generation,
        usage_token_cap,
        1800,
    )
    if not reservation.get("allowed"):
        skill_sandbox.cleanup(ro_root)
        return {
            "ok": False,
            "reason": "shared token budget exhausted",
            "exit_code": 0,
            "budget_stop": True,
        }
    if reservation.get("tokens"):
        env["MISAKA_TURN_TOKEN_LIMIT"] = str(reservation["tokens"])
    recorder = _UsageRecorder(
        on_event, usage_db, task_id, usage_generation, reservation
    )
    r = None
    try:
        r = run_coro(
            _run_with_reservation(
                run_session(
                    flags,
                    prompt,
                    workspace,
                    on_event=recorder,
                    timeout=None,
                    assembly=assembly,
                    env=env,
                ),
                usage_db,
                reservation,
            )
        )
    finally:
        recorder.settle(
            r.get("budget_usage")
            if isinstance(r, dict)
            else int(reservation.get("tokens") or 0)
        )
        skill_sandbox.cleanup(ro_root)          # the read-only copies go with the run, however it ended

    row = task_store.get(con, task_id) if con is not None and task_id else None
    if row is not None and row["status"] != "running":
        return {"ok": True, "settled": True, "exit_code": 0}
    reason = r["error"] or "session exited before the card was finalized"
    return {
        "ok": False,
        "reason": reason,
        "exit_code": 1 if r["error"] else 0,
        "stderr_tail": r["error"] or "",
    }

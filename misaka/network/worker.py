"""Execute durable Sister task cards and validate structured submissions."""
import json
import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath

from misaka.config import profiles
from misaka.platform import tasks as task_store
from misaka.platform.session import run_coro, run_session
from misaka.skills import sandbox as skill_sandbox

SUBAGENT_TOOLS = ("Agent", "TaskOutput", "SendMessage", "TaskStop")
MAX_REPORT_BYTES = 256 * 1024
MAX_SUMMARY_CHARS = 2_000
MAX_NOTES_CHARS = 32_000
MAX_ARTIFACTS = 256
MAX_ARTIFACT_PATH_CHARS = 1_024
MAX_UNCERTAIN = 32
MAX_UNCERTAIN_ITEM_CHARS = 2_000


def _reserve_usage(usage_db, usage_task_id, usage_generation, usage_token_cap, timeout):
    if not usage_db or not usage_task_id or usage_generation is None:
        return {"allowed": True, "token": None}
    from misaka.platform import budget

    return budget.reserve_agent_path(
        str(usage_db),
        usage_token_cap,
        str(usage_task_id),
        int(usage_generation),
        ttl_seconds=max(60, int(timeout) + 60),
    )


def _release_usage(usage_db, reading):
    token = reading.get("token") if isinstance(reading, dict) else None
    if token and usage_db:
        from misaka.platform import budget

        budget.release_agent_path(str(usage_db), token)


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
        from misaka.platform import budget

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


REPORT_INSTRUCTIONS = """

---
## Submission contract
When the task is complete, write UTF-8 JSON to `$MISAKA_TASK_DIR/report.json`:
{"schema_version": 1, "generation": __GENERATION__, "status": "done", "summary": "Concise description of the work",
 "artifacts": ["path relative to the workspace"],
 "uncertain": ["One to three specific weak points, such as a second-hand date or fragile estimate"],
 "notes": ""}
- `generation` must be exactly __GENERATION__ (this attempt); a report for another attempt is rejected.
- `status` must be `done` or `blocked`; explain missing input in `notes` when blocked.
- List every deliverable in `artifacts`. Unlisted files are not accepted as deliverables.
- Use `done` only when every listed artifact exists.
- `uncertain` is required. Name concrete doubts, not generic caveats. Honest uncertainty does not count against you;
  hiding it delays review.

## Report blockers immediately
If a premise collapses, external input is indispensable, or a decision is needed, use `SendMessage` to notify
`last-order` immediately. The message is informational: it does not modify the card or count as submission.
Continue any work that remains valid, then submit through `report.json`.
"""


def report_instructions(generation):
    """The submission contract for one attempt: the generation stamp is what keeps a stale
    report.json from passing as this attempt's proof."""
    return REPORT_INSTRUCTIONS.replace("__GENERATION__", str(int(generation or 1)))


def set_aside_report(task_id):
    """Set aside the previous report.json: a continuation must submit fresh proof, never reuse stale."""
    from pathlib import Path

    from misaka.platform import tasks as db
    root = Path(db.task_state_dir(task_id))
    current, previous = root / "report.json", root / ".previous-report.json"
    if not current.is_file():
        return
    try:
        previous.unlink(missing_ok=True)
        current.replace(previous)
    except OSError:
        current.unlink(missing_ok=True)


def card_prompt(task):
    """Build the durable card contract shared by foreground and Sister runtimes."""
    body = task.get("body") or task.get("title") or ""
    if review_feedback := task.get("review_feedback"):
        body = (
            "⚠️ Independent reviewer requested changes. Address each item and resubmit:\n"
            + str(review_feedback) + "\n\n---\n\n" + body
        )
    if feedback := task.get("feedback"):
        body = feedback + "\n\n---\n\n" + body
    if attachments := task.get("_attachments"):
        lines = []
        for item in attachments:
            target = item.get("path") or item.get("source") or item.get("name")
            lines.append(f"- Attachment: `{target}`")
        body += "\n\n## Task attachments\n" + "\n".join(lines)
    if output_dir := task.get("output_dir"):
        body += (
            "\n\n## Deliverable location\n"
            f"Write every new deliverable under `{output_dir}`. In `report.json`, list each artifact "
            "using its path relative to the current workspace."
        )
    prompt = body + report_instructions(task.get("generation"))
    if task.get("beast"):
        from misaka.platform import budget as _b

        prompt += _b.BEAST_SUFFIX
    return prompt


def _load_profile(profile_dir):
    """Return ``(SOUL.md path or None, config.json dict)`` for a profile directory."""
    soul = os.path.join(profile_dir, "SOUL.md")
    cfg_path = os.path.join(profile_dir, "config.json")
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    return (soul if os.path.exists(soul) else None), cfg


def check_report(workspace, con=None, task_id=None, generation=None):
    """Validate a card's report.json.

    Returns ``(True, report)`` or ``(False, reason)``; a blocked report yields
    ``(False, "blocked: <notes>")``. With ``generation`` the report must name that attempt:
    a report left behind by an earlier generation is never accepted for a later one.
    """
    try:
        root = Path(workspace).resolve(strict=True)
    except (OSError, RuntimeError):
        return False, "workspace invalid"
    if not root.is_dir():
        return False, "workspace invalid"
    path = (Path(task_store.task_state_dir(task_id)) if task_id else root) / "report.json"
    if path.is_symlink() or not path.is_file():
        return False, "no report.json"
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            return False, "report.json too large"
        raw = path.read_bytes()
        if len(raw) > MAX_REPORT_BYTES:
            return False, "report.json too large"
        report = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return False, f"report.json unparsable: {e}"
    if not isinstance(report, dict):
        return False, "report.json must be an object"
    if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        return False, "report.json bad schema"
    if report.get("status") not in ("done", "blocked"):
        return False, "report.json bad schema"
    if generation is not None and report.get("generation") != int(generation):
        return False, "report.json is from another generation of this card"
    summary = report.get("summary")
    notes = report.get("notes", "")
    artifacts = report.get("artifacts")
    uncertain = report.get("uncertain")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY_CHARS:
        return False, "report.json bad summary"
    if not isinstance(notes, str) or len(notes) > MAX_NOTES_CHARS:
        return False, "report.json bad notes"
    empty_ok = report.get("status") == "blocked"
    if not isinstance(artifacts, list) or (not artifacts and not empty_ok) or len(artifacts) > MAX_ARTIFACTS:
        return False, "report.json bad artifacts"
    if not isinstance(uncertain, list) or len(uncertain) > MAX_UNCERTAIN:
        return False, "report.json must contain an `uncertain` array, which may be empty"
    seen: set[Path] = set()
    for index, value in enumerate(artifacts):
        if not isinstance(value, str) or not value or "\0" in value:
            return False, f"artifact[{index}] invalid"
        if len(value) > MAX_ARTIFACT_PATH_CHARS:
            return False, f"artifact[{index}] too long"
        posix, windows = PurePosixPath(value), PureWindowsPath(value)
        if (
            posix.is_absolute()
            or bool(windows.drive or windows.root)
            or ".." in posix.parts
            or ".." in windows.parts
        ):
            return False, f"artifact[{index}] unsafe path"
        try:
            candidate = (root / value).resolve(strict=True)
            candidate.relative_to(root)
            mode = candidate.stat().st_mode
        except (OSError, RuntimeError, ValueError):
            return False, f"artifact[{index}] missing or outside workspace"
        if not stat.S_ISREG(mode):
            return False, f"artifact[{index}] is not a regular file"
        if candidate in seen:
            return False, f"artifact[{index}] duplicates another artifact"
        seen.add(candidate)
    for index, value in enumerate(uncertain):
        if not isinstance(value, str) or not value.strip():
            return False, f"uncertain[{index}] invalid"
        if len(value) > MAX_UNCERTAIN_ITEM_CHARS:
            return False, f"uncertain[{index}] too long"
    report["notes"] = notes
    if report["status"] == "blocked":
        if not notes.strip():
            return False, "blocked report missing notes"
        return False, f"blocked: {notes[:500]}"
    if con is not None and task_id:
        from misaka.network import todo
        doing = todo.stats(con, task_id)["doing"]
        if doing:
            return False, (
                "to-do list still has items marked doing: " + "; ".join(doing[:5]) +
                ". Mark completed items done, or move unfinished items to open or blocked with a note."
            )
    return True, report


def run_llm_json(profile_dir, prompt, provider, default_model,
                 cwd=None, tools=None, timeout=600, model=None,
                 usage_db=None, usage_task_id=None, usage_generation=None,
                 usage_token_cap=None, on_event=None, raw=False,
                 soul=True, session_dir=None, continue_session=False,
                 thinking="low"):
    """Run a bare session under ``profile_dir`` (only the tools listed, no delegation) and extract
    the first JSON object from its output. Returns ``(obj, raw_text, error)``."""
    from misaka.network import validate

    _soul_path, cfg = _load_profile(profile_dir)
    model = os.environ.get("MISAKA_FORCE_MODEL") or model or cfg.get("model") or default_model
    flags = ["--provider", provider, "--model", model, "--thinking", thinking]
    if session_dir:
        flags += ["--session-dir", os.path.abspath(session_dir)]
        if continue_session:
            flags.append("--continue")
    else:
        flags.append("--no-session")
    workdir = cwd or os.getcwd()
    role = profiles.role_of(profile_dir)
    from misaka.app.composition import SessionSpec, build_extensions

    allowed = list(tools or ())
    factories = build_extensions(SessionSpec(
        profile_dir=profile_dir,
        role=role,
        workspace=workdir,
        kind="bare",
        sender=role.rsplit("/", 1)[-1],
        tool_ceiling=None,
    )) or None
    flags += ["-t", ",".join(dict.fromkeys(allowed))] if allowed else ["-nt"]
    if soul:
        from misaka.config import identity
        for section in identity.prompt_sections(profile_dir, role):
            flags += ["--append-system-prompt", section]
    env = {"MISAKA_PROFILE_DIR": profile_dir,
           "MISAKA_WHO": role,
           "MISAKA_MCP_ROLE": role,
           "MISAKA_WORKSPACE": workdir}
    if usage_db and usage_task_id and usage_generation is not None:
        env.update({
            "MISAKA_USAGE_DB": str(usage_db),
            "MISAKA_USAGE_TASK_ID": str(usage_task_id),
            "MISAKA_USAGE_GENERATION": str(usage_generation),
            "MISAKA_USAGE_TOKEN_CAP": str(int(usage_token_cap or 0)),
        })
    reservation = _reserve_usage(
        usage_db, usage_task_id, usage_generation, usage_token_cap, timeout
    )
    if not reservation.get("allowed"):
        return None, "", "shared token budget exhausted"
    if reservation.get("tokens"):
        env["MISAKA_TURN_TOKEN_LIMIT"] = str(reservation["tokens"])
    recorder = _UsageRecorder(
        on_event, usage_db, usage_task_id, usage_generation, reservation
    )
    r = None
    try:
        r = run_coro(run_session(
            flags, prompt, workdir, timeout=timeout, extension_factories=factories,
            env=env, on_event=recorder))
    finally:
        recorder.settle(
            r.get("budget_usage")
            if isinstance(r, dict)
            else int(reservation.get("tokens") or 0)
        )
    if r["timed_out"]:
        return None, r["text"] or "", "timeout"
    if r["error"]:
        return None, r["text"] or "", r["error"]
    if raw:
        return None, r["text"] or "", None
    obj = validate.extract_json(r["text"] or "")
    if obj is None:
        return None, r["text"] or "", "no json in output"
    return obj, r["text"] or "", None


def card_session_setup(task, workspace, profile_dir, provider, default_model):
    """Build the shared session configuration for headless and interactive cards."""
    _soul, cfg = _load_profile(profile_dir)
    model = os.environ.get("MISAKA_FORCE_MODEL") or task["model"] or cfg.get("model") or default_model
    os.makedirs(workspace, exist_ok=True)
    state_dir = task_store.task_state_dir(task["id"])
    os.makedirs(state_dir, exist_ok=True)
    beast = isinstance(task, dict) and task.get("beast")
    prompt = card_prompt(task)

    role = profiles.role_of(profile_dir)
    # The engine has no skill loading of its own; the skills extension reads the
    # card's sandbox (SessionSpec.skill_roots) and nothing else.
    flags = ["--provider", provider, "--model", model, "--thinking", "low",
             "--session-dir", os.path.join(state_dir, "session")]
    ro_root = os.path.join(state_dir, ".skills-ro")
    sender = role.rsplit("/", 1)[-1]
    from misaka.app.composition import SessionSpec, build_extensions

    kind = "beast" if beast else "card"
    delegates = not profiles.is_last_order(profile_dir)
    skill_roots = None
    if beast:
        # Beast mode gets no builtin tools: with DELEGATE it keeps only the subagent
        # tools, otherwise none (Last Order has no DELEGATE, so it runs tool-less).
        if delegates:
            flags += ["-t", ",".join(SUBAGENT_TOOLS)]
        else:
            flags += ["-nt"]
    else:
        # Regular cards run against read-only copies of the role's skill stack: skills are
        # read-only at run time (constitution), and a card never sees the live tree.
        from misaka.skills import layers as skill_layers
        skill_sandbox.readonly_copies(skill_layers.skills_stack(profile_dir, cwd=workspace), ro_root)
        skill_roots = (("sandbox", ro_root),)
    factories = build_extensions(SessionSpec(
        profile_dir=profile_dir,
        role=role,
        workspace=workspace,
        kind=kind,
        sender=sender,
        receive_messages=True,          # a card hears Last Order (and her Sisters) at its next tool boundary
        task_id=task.get("id"),
        tool_ceiling=SUBAGENT_TOOLS if beast and delegates else None,
        skill_roots=skill_roots,
    )) or None
    flags += ["--append-system-prompt", profiles.shared_soul()]
    from misaka.config import identity
    for section in identity.prompt_sections(profile_dir, role):
        flags += ["--append-system-prompt", section]
    return flags, factories, prompt, ro_root, role


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
    """Run one task card in a headless session and validate its report; return a verdict dict."""
    task_id = task.get("id") if isinstance(task, dict) else None
    flags, factories, prompt, ro_root, role = card_session_setup(
        task, workspace, profile_dir, provider, default_model
    )
    env = {"MISAKA_PROFILE_DIR": profile_dir,
           "MISAKA_WHO": role,
           "MISAKA_MCP_ROLE": role,
           "MISAKA_WORKSPACE": workspace,
           "MISAKA_TASK_DIR": task_store.task_state_dir(task_id),
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
    reservation = _reserve_usage(
        usage_db,
        task_id,
        usage_generation,
        usage_token_cap,
        task["timeout_seconds"],
    )
    if not reservation.get("allowed"):
        skill_sandbox.cleanup(ro_root)
        return {
            "ok": False,
            "reason": "shared token budget exhausted",
            "exit_code": 0,
            "timed_out": False,
            "budget_stop": True,
        }
    if reservation.get("tokens"):
        env["MISAKA_TURN_TOKEN_LIMIT"] = str(reservation["tokens"])
    recorder = _UsageRecorder(
        on_event, usage_db, task_id, usage_generation, reservation
    )
    r = None
    try:
        r = run_coro(run_session(
            flags, prompt, workspace, on_event=recorder,
            timeout=task["timeout_seconds"], extension_factories=factories,
            env=env))
    finally:
        recorder.settle(
            r.get("budget_usage")
            if isinstance(r, dict)
            else int(reservation.get("tokens") or 0)
        )
        skill_sandbox.cleanup(ro_root)          # the read-only copies go with the run, however it ended

    ok, result = check_report(workspace, con=con, task_id=task_id, generation=task.get("generation"))
    if ok:
        return {"ok": True, "report": result, "exit_code": 0, "timed_out": False}
    reason = result if not r["error"] else f"{result} (session error: {r['error']})"
    return {"ok": False, "reason": reason, "exit_code": 1 if r["error"] else 0,
            "timed_out": r["timed_out"], "stderr_tail": r["error"] or ""}

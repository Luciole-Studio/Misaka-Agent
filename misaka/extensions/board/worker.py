"""Sister worker：跑一张卡（进程内会话），收结构化交卷并实物核验。

会话装配在 misaka/engine_session.py（适配层）；本文件只管卡的业务：
人格装配、交卷合同、keystone 核验。

交卷 keystone（agent-smith MasterVerification 裁剪版）：report.json 必在、必合法、
artifacts 必须逐个真实在盘——"卡说完成"不算数。
"""
import json
import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath

from misaka.config import profiles

from misaka.orchestration import skill_sandbox
from misaka.orchestration.session import run_coro, run_session

# ``-t`` 是整张工具表的白名单，不只筛 builtin；因此需要把子代理四工具显式并入，
# 才能在判官仅有 read、Beast 没有 builtin 的同时保留分身能力。
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
    from misaka.orchestration import budget

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
        from misaka.orchestration import budget

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
            except Exception:  # accounting fallback must survive observer failure
                delivered = False
        if total and not delivered:
            self.fallback_tokens += total
        elif total:
            self.delivered_tokens += total

    def settle(self, accounted_tokens=None):
        if not self.usage_db or not self.task_id or self.generation is None:
            return
        from misaka.orchestration import budget

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


def _subagent_factory(profile_dir, role, workspace, tool_ceiling=None):
    """Bind after ``run_session`` installs this worker's target environment."""

    def bound(harn):
        from misaka.extensions.subagent import extension as subagent

        subagent.bind(
            profile_dir,
            role,
            workspace,
            mcp_role=role,
            tool_ceiling=tool_ceiling,
        )(harn)

    return bound

REPORT_INSTRUCTIONS = """

---
## 交卷规矩（必须遵守）
干完后在当前工作目录写 `report.json`（UTF-8），格式：
{"schema_version": 1, "status": "done", "summary": "一句话干了什么",
 "artifacts": ["产物相对路径", "..."],
 "uncertain": ["你自己最没底的 1-3 个具体点（哪条结论出处最弱/哪个数字最可能错）"],
 "notes": ""}
- status 只许 "done" 或 "blocked"（卡住时用 blocked 并在 notes 说明缺什么）。
- artifacts 列出你产出的每个文件（相对当前目录）；没写进 artifacts 的产物不算数。
- 不许在没有真产物时报 done。
- **uncertain 必填**：写你真正心虚的具体点（"第二节的年份出处只有二手转引"），
  不是客套（"可能还有改进空间"）。验收方会集中火力查这些——诚实标出来不会扣分，藏起来被抓才扣。

## 中途上报（别闷头烧时间）
发现卡片前提被推翻、没有外部输入就走不下去、或撞上需要人裁决的岔路时，
**当场 `SendMessage` 给 last-order**，不要等交卷才说。发消息只是送信：不改卡状态、
不代替交卷、也不算验收通过；发完继续干你还能干的部分。编排官在场会立刻看到，
不在场则等她开会话时收到。
"""


def card_prompt(task):
    """Build the durable card contract shared by foreground and Sister runtimes."""
    body = task.get("body") or task.get("title") or ""
    if feedback := task.get("feedback"):
        body = feedback + "\n\n---\n\n" + body
    prompt = body + REPORT_INSTRUCTIONS
    if task.get("beast"):
        from misaka.orchestration import budget as _b

        prompt += _b.BEAST_SUFFIX
    return prompt


def _load_profile(profile_dir):
    soul = os.path.join(profile_dir, "SOUL.md")
    cfg_path = os.path.join(profile_dir, "config.json")
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    # 技能＝角色目录下的 skills/（人格与技能同居 ~/.misaka/profiles/<角色>/，照 pi）
    skills = profiles.skills(profile_dir)
    return (soul if os.path.exists(soul) else None), cfg, skills


def check_report(workspace, con=None, task_id=None):
    """keystone：报告+实物才算完成。返回 (ok, report_or_reason)。
    给了 con+task_id 再加一道微观代办闸：doing 清零才许交卷（终态诚实，
    末尾补不了历史）；没给就只验报告（golden/e2e 的纯 keystone 用法不受累）。"""
    try:
        root = Path(workspace).resolve(strict=True)
    except (OSError, RuntimeError):
        return False, "workspace invalid"
    if not root.is_dir():
        return False, "workspace invalid"
    path = root / "report.json"
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
    summary = report.get("summary")
    notes = report.get("notes", "")
    artifacts = report.get("artifacts")
    uncertain = report.get("uncertain")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY_CHARS:
        return False, "report.json bad summary"
    if not isinstance(notes, str) or len(notes) > MAX_NOTES_CHARS:
        return False, "report.json bad notes"
    # blocked（卡住等输入）通常没有产物——不强制非空；done 仍必须有产物
    empty_ok = report.get("status") == "blocked"
    if not isinstance(artifacts, list) or (not artifacts and not empty_ok) or len(artifacts) > MAX_ARTIFACTS:
        return False, "report.json bad artifacts"
    if not isinstance(uncertain, list) or len(uncertain) > MAX_UNCERTAIN:
        return False, "report.json 缺 uncertain（心虚点必填，可为空数组但键必须在）"
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
        return False, f"blocked: {notes[:500]}"  # ponytail: blocked 态 M1 加，先记 failed(blocked)
    if con is not None and task_id:
        from misaka.extensions.board import todo
        doing = todo.stats(con, task_id)["doing"]
        if doing:
            return False, ("todo 未收口：还挂着 doing——" + "；".join(doing[:5])
                           + "。做完的标 done；没做完的退 open 或标 blocked＋note，再交卷")
    return True, report


def run_llm_json(profile_dir, prompt, provider, default_model,
                 cwd=None, tools=None, timeout=600, model=None,
                 usage_db=None, usage_task_id=None, usage_generation=None,
                 usage_token_cap=None, on_event=None, raw=False, bare=False):
    """一次性进程内调用，从最终回答捞 JSON。

    ``tools`` 只描述 builtin 白名单；除 Last Order 外，角色始终另有子代理四工具。
    ``raw=True``＝要纯文本不捞 JSON（没有 JSON 不算失败——MoA 参谋用）；
    ``bare=True``＝裸调用：零内置工具零分身（hermes 参谋纪律：参谋不行动）。

    返回 (obj, raw_text, err)。err 为 None 即成功拿到可解析 JSON（raw 时＝拿到文本）。
    """
    from misaka.extensions.board import validate  # 延迟导入避免包初始化环

    soul, cfg, _skills = _load_profile(profile_dir)
    model = os.environ.get("MISAKA_FORCE_MODEL") or model or cfg.get("model") or default_model  # 停摆时全线切换
    flags = ["--provider", provider, "--model", model, "--no-session", "--thinking", "low"]
    workdir = cwd or os.getcwd()
    role = profiles.role_of(profile_dir)
    can_delegate = not bare and not profiles.is_last_order(profile_dir)
    factories = None
    allowed = list(tools or ())
    if can_delegate:
        allowed.extend(SUBAGENT_TOOLS)
        from functools import partial

        from misaka.extensions import inline, messages
        from misaka.extensions.subagent import extension as subagent

        factories = [
            inline(
                "subagent",
                _subagent_factory(profile_dir, role, workdir, tuple(allowed)),
            ),
            # SendMessage 在白名单里，得有人注册它：续聊分身＋上报都走这一件
            inline("messages", partial(
                messages.register, sender=role.rsplit("/", 1)[-1],
                route=subagent.route_to_children,
            )),
        ]
    flags += ["-t", ",".join(dict.fromkeys(allowed))] if allowed else ["-nt"]
    if soul:
        flags += ["--append-system-prompt", soul]
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
    """跑卡会话的装配（无头路径与格子交互路径共用）。

    返回 (flags, factories, prompt, ro_root, role)：引擎旗标、扩展工厂、
    开场合同（含验收反馈与交卷规矩）、技能只读副本根、角色名。
    """
    soul, cfg, skills = _load_profile(profile_dir)
    model = os.environ.get("MISAKA_FORCE_MODEL") or task["model"] or cfg.get("model") or default_model
    os.makedirs(workspace, exist_ok=True)
    beast = isinstance(task, dict) and task.get("beast")
    prompt = card_prompt(task)

    role = profiles.role_of(profile_dir)
    can_delegate = not profiles.is_last_order(profile_dir)
    flags = ["--provider", provider, "--model", model, "--thinking", "low",
             "--session-dir", os.path.join(workspace, "session")]
    factories = None
    ro_root = os.path.join(workspace, ".skills-ro")
    sender = role.rsplit("/", 1)[-1]   # 信箱地址用编号（"sisters/10032"→"10032"）
    if beast:
        if not can_delegate:
            flags += ["-nt"]
        else:
            from functools import partial

            from misaka.extensions import inline, messages
            from misaka.extensions.board import todo
            from misaka.extensions.subagent import extension as subagent

            factories = [
                inline(
                    "subagent",
                    _subagent_factory(profile_dir, role, workspace, SUBAGENT_TOOLS),
                ),
                inline("messages", partial(
                    messages.register, sender=sender,
                    route=subagent.route_to_children,
                )),
            ]
            if task.get("id"):   # 合成任务（无卡号）没有微观代办
                factories.append(inline("todo", todo.tools_for(task["id"])))
            # 禁 builtin，但仍可派分身与上报；SendMessage 在 SUBAGENT_TOOLS 白名单里
            flags += ["-t", ",".join(SUBAGENT_TOOLS)]
    else:
        from functools import partial

        from misaka.extensions import docs, inline, mcp, messages
        from misaka.extensions.board import todo
        from misaka.extensions.subagent import extension as subagent

        factories = [
            inline("doc-tools", docs.register),
            inline("messages", partial(
                messages.register, sender=sender,
                route=subagent.route_to_children if can_delegate else None,
            )),
        ]
        if task.get("id"):   # 合成任务（无卡号）没有微观代办
            factories.append(inline("todo", todo.tools_for(task["id"])))
        if can_delegate:
            factories.append(inline(
                "subagent",
                _subagent_factory(profile_dir, role, workspace, None),
            ))
        factories.append(inline("mcp", mcp.bind(profile_dir, role)))  # 文献+（非 Last Order）分身+MCP，直接进工具表
        # 宪法④：技能只读——Sister 拿到的是去写权副本，够不到真技能树
        for d in skill_sandbox.readonly_copies(skills, ro_root):
            flags += ["--skill", d]
    flags += ["--append-system-prompt", profiles.shared_soul()]   # 共同魂在前
    if soul:
        flags += ["--append-system-prompt", soul]                 # 角色个性在后
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
    """同步跑一张卡。返回 verdict dict：{ok, report|reason, exit_code, timed_out}。
    con 给了就在交卷时连微观代办闸一起验（dispatch 传板连接进来）。"""
    task_id = task.get("id") if isinstance(task, dict) else None
    flags, factories, prompt, ro_root, role = card_session_setup(
        task, workspace, profile_dir, provider, default_model
    )
    env = {"MISAKA_PROFILE_DIR": profile_dir,
           "MISAKA_WHO": role,
           "MISAKA_MCP_ROLE": role,
           "MISAKA_WORKSPACE": workspace}
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
            env=env))  # 分身子进程复用同一人格、角色与预算账本
    finally:
        recorder.settle(
            r.get("budget_usage")
            if isinstance(r, dict)
            else int(reservation.get("tokens") or 0)
        )

    skill_sandbox.cleanup(ro_root)  # 副本用完即删，不占工作区也不进产物核验
    ok, result = check_report(workspace, con=con, task_id=task_id)
    if ok:
        return {"ok": True, "report": result, "exit_code": 0, "timed_out": False}
    reason = result if not r["error"] else f"{result}（会话错误：{r['error']}）"
    return {"ok": False, "reason": reason, "exit_code": 1 if r["error"] else 0,
            "timed_out": r["timed_out"], "stderr_tail": r["error"] or ""}

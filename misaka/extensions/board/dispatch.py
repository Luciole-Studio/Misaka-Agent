"""执行内核（库，无 CLI 面）：对账/跑卡/红队判卷。人执行的 dispatch 已删（2026-08-08 用户裁定：编排只归 Last Order）；dispatch_once 保留给 e2e 干跑与将来复用。

投影纪律：worker 只产事件；tasks 行只由本进程写（单写者免锁）。
崩溃对账（MiroFlow 判据）：产物是真相，状态列只是投影。
打回环（Mavis 三态）：验收不过自动回 ready 带 must_fix 反馈；两轮不过 gave_up 判 failed。
"""
import json
import os
import secrets
import socket
import subprocess

from misaka.extensions.board import db, validate
from misaka.orchestration import budget
from misaka.research.indexer import workspace as ws_index
from misaka.research.kernel import canon, cdcl, guard, precedent, store
from misaka.extensions.board import worker

MAX_VERIFY_ROUNDS = 2
_skipped_logged = set()  # ponytail: 进程内记一次 skipped，防重复刷事件；重启会再记一条，无害


def _profile_dir(cfg, assignee):
    for root in (cfg["profiles_root"], cfg["roles_root"]):
        d = os.path.join(root, assignee)
        if os.path.isdir(d):
            return d
    return None


def run_hooks(cfg, task, workspace):
    """跑 hooks/ 下的可执行钩子。非零退出＝否决（宪法⑥的机器闸）。返回否决理由列表。"""
    hook_dir = cfg.get("hooks_dir") or ""
    if not os.path.isdir(hook_dir):
        return []
    vetoes = []
    for name in sorted(os.listdir(hook_dir)):
        path = os.path.join(hook_dir, name)
        if not os.access(path, os.X_OK) or name.startswith("."):
            continue
        try:
            p = subprocess.run([path, workspace], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            vetoes.append(f"钩子 {name} 跑不起来: {e}")  # 闸坏了按否决处理，不静默放行
            continue
        if p.returncode != 0:
            vetoes.append(f"[{name}] {(p.stdout + p.stderr).strip()[:200]}")
    return vetoes


def _event_summary(d, nbytes):
    """超长事件的合法 JSON 摘要:留 type＋tail 要显示的字段,全文在 session jsonl。

    2026-08-08(爱酱审计④③):此前非 agent_end 分支 `line[:cap]` 裸切,把序列化 JSON
    切成非法 JSON——board.db 攒了 1446 条 4000 字节垃圾/13MB。裸切任何 JSON 都产生非法
    JSON,只有 agent_end 因带 token 账被发现修了;其余照样切。根治=一律不裸切,存合法摘要。
    message_update 连 tail 都不显示(_fmt return None),存它全文纯浪费。"""
    keep = {"type": d.get("type") or "event", "truncated_bytes": nbytes}
    for k in ("toolName", "name"):
        if isinstance(d.get(k), str):
            keep[k] = d[k]
    for k in ("text", "delta", "thinking", "error", "errorMessage"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            keep[k] = v.strip().splitlines()[0][:300]
            break
    return json.dumps(keep, ensure_ascii=False)


def _compact_event(line, cap=4000):
    """事件入库前压到 4k 内（全文在 workspace/session 的 jsonl 里）。
    agent_end 不许盲截——它带着预算的账本（usage.totalTokens），截断＝漏记。
    其余超长事件存合法 JSON 摘要,不裸切（爱酱审计④③:裸切=非法 JSON 垃圾）。"""
    if len(line) <= cap:
        return line
    try:
        d = json.loads(line)
    except ValueError:
        return json.dumps({"type": "raw", "truncated_bytes": len(line),
                           "preview": line[:200]}, ensure_ascii=False)
    if d.get("type") == "agent_end":
        total = 0
        for message in d.get("messages") or []:
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
        # Always valid JSON and bounded independently of the number/size of
        # messages.  Slicing a serialized ledger silently turned it invalid.
        return json.dumps(
            {
                "type": "agent_end",
                "messages": [{"role": "assistant", "usage": {"totalTokens": total}}],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return _event_summary(d, len(line))


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reconcile(con):
    import time as _time

    from misaka.extensions.board.sister_runtime import _claimer_alive
    from misaka.extensions.subagent.child import PROCESS_GROUP_IDENTITY
    from misaka.orchestration import processes as process_tree

    now = int(_time.time())
    for t in db.by_status(con, "running"):
        expires = t["claim_expires"]
        if expires is not None and int(expires) >= now and _claimer_alive(t["claim_lock"]):
            # sister_runtime 同款守卫（审查 2026-08-20 补齐）：租约有效且认领者
            # 活着＝回合边界常态窗口，抢收会白丢一轮产出还误记崩溃
            continue
        stored = str(t["worker_identity"] or "")
        if stored.startswith(PROCESS_GROUP_IDENTITY):
            # 与 sister_runtime 同款纪律：带组身份的孤儿要先证明组空才许回收
            leader = stored[len(PROCESS_GROUP_IDENTITY):]
            if process_tree.identity_is_alive(t["worker_pid"], leader):
                continue
            if not process_tree.terminate_orphaned_group(int(t["worker_pid"]), leader):
                continue
        elif _pid_alive(t["worker_pid"]):
            continue
        ok, result = worker.check_report(t["workspace"] or "", con=con, task_id=t["id"])
        if not db.reclaim_abandoned(
            con,
            t["id"],
            generation=t["generation"],
            claim_lock=t["claim_lock"],
            worker_pid=t["worker_pid"],
            worker_identity=t["worker_identity"],
            claim_expires=t["claim_expires"],
            submitted=bool(ok),
        ):
            continue
        if ok:
            db.add_event(
                con,
                t["id"],
                "submitted",
                {
                    "summary": result["summary"],
                    "artifacts": result.get("artifacts", []),
                    "notes": result.get("notes", ""),
                    "uncertain": result.get("uncertain", []),
                    "reconciled": True,
                },
                generation=t["generation"],
            )
        else:
            db.add_event(
                con,
                t["id"],
                "reclaimed",
                {"reason": str(result)},
                generation=t["generation"],
            )


def run_task(con, t, cfg):
    profile_dir = _profile_dir(cfg, t["assignee"])
    if not profile_dir:
        # 无档案的卡永远派不出去：判失败让人看见，别静默漏派一辈子
        if t["id"] not in _skipped_logged:
            _skipped_logged.add(t["id"])
            lock = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
            generation = int(t["generation"])
            if db.claim(con, t["id"], lock, ttl_seconds=60,
                        generation=generation, pid=os.getpid()):
                db.add_event(con, t["id"], "failed",
                             {"reason": f"assignee 无档案：{t['assignee']}"},
                             generation=generation, claim_lock=lock)
                db.mark_failed(con, t["id"], generation=generation, claim_lock=lock)
        return False

    lock = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
    generation = int(t["generation"])
    if not db.claim(
        con,
        t["id"],
        lock,
        ttl_seconds=max(1800, int(t["timeout_seconds"]) + 60),
        generation=generation,
        pid=os.getpid(),
    ):
        return False
    workspace = os.path.join(cfg["workspaces_root"], t["id"])
    os.makedirs(workspace, exist_ok=True)
    if not db.set_workspace(
        con, t["id"], workspace, generation=generation, claim_lock=lock
    ):
        return False
    db.add_event(
        con,
        t["id"],
        "claimed",
        {"lock": lock, "workspace": workspace},
        generation=generation,
    )

    task = dict(t)
    bud = budget.status(con, cfg.get("token_cap"))
    if bud["mode"] == "stop":
        if db.back_to_ready(
            con, t["id"], generation=generation, claim_lock=lock
        ):
            db.add_event(
                con, t["id"], "budget_stop", bud, generation=generation
            )
        return False
    hit = cdcl.check(con, f"{t['title']} {(t['body'] or '')[:300]}", canon)
    if hit:
        db.add_event(con, t["id"], "clause_hit", {"clause_id": hit[0], "text": hit[1], "sim": hit[2]})
        task["feedback"] = (f"⚠️ 过往同类尝试的教训（避免重蹈）：{hit[1]}\n"
                            + (task.get("feedback") or ""))
    if bud["mode"] == "beast":
        db.add_event(con, t["id"], "beast_mode", bud)
        task["beast"] = True
    fb = db.latest_payload(
        con, t["id"], "verify_fail", generation=generation
    )
    if fb:
        fixes = json.loads(fb).get("must_fix", [])
        task["feedback"] = (task.get("feedback") or "") + \
                           "⚠️ 上一轮验收未过，必须先修复以下问题（产物按最新要求重写）：\n" + \
                           "\n".join(f"- {x}" for x in fixes)

    usage_db = cfg.get("db")
    if not usage_db:
        try:
            usage_db = con.execute("PRAGMA database_list").fetchone()[2] or None
        except (AttributeError, IndexError, TypeError):
            usage_db = None
    try:
        verdict = worker.run_card(
            task, workspace, profile_dir, cfg["provider"], cfg["default_model"],
            on_event=lambda line: db.add_event(
                con,
                t["id"],
                "harn_event",
                _compact_event(line),
                generation=generation,
                claim_lock=lock,
            ),
            usage_db=usage_db,
            usage_generation=generation,
            usage_claim_lock=lock,
            usage_token_cap=cfg.get("token_cap"),
            con=con,
        )
    except BaseException:  # 跑挂了也要落终态：卡别悬在 running 等进程退出
        if db.back_to_ready(con, t["id"], generation=generation, claim_lock=lock):
            db.add_event(con, t["id"], "dispatch_error",
                         {"reason": "run_card crashed"}, generation=generation)
        raise
    if verdict.get("budget_stop"):
        if db.back_to_ready(
            con, t["id"], generation=generation, claim_lock=lock
        ):
            db.add_event(
                con,
                t["id"],
                "budget_stop",
                budget.status(con, cfg.get("token_cap")),
                generation=generation,
            )
    elif verdict["ok"]:
        if db.mark_verifying(
            con, t["id"], generation=generation, claim_lock=lock
        ):
            db.add_event(
                con,
                t["id"],
                "submitted",
                {
                    "summary": verdict["report"]["summary"],
                    "artifacts": verdict["report"]["artifacts"],
                    "notes": verdict["report"].get("notes", ""),
                    "uncertain": verdict["report"].get("uncertain", []),
                },
                generation=generation,
            )
    else:
        failure = {
            "reason": str(verdict["reason"]),
            "exit_code": verdict["exit_code"],
            "timed_out": verdict["timed_out"],
            "stderr_tail": verdict.get("stderr_tail", "")[-500:],
        }
        # blocked ≠ 失败（2026-08-08）：妹妹诚实交卷"卡住等输入"，此前被记 failed
        # 还触发尸检立禁令——诚实上报污染禁令库。现在事件如实记 blocked、不立禁令；
        # 状态列仍走 failed（状态机不动，真 blocked 列并入将来 board 手术），
        # 通知与看板按 blocked 事件如实显示，LO 用 misaka_sister_message 回她即可续跑。
        blocked = str(verdict["reason"]).startswith("blocked:")
        if db.add_event(
            con,
            t["id"],
            "blocked" if blocked else "failed",
            failure,
            generation=generation,
            claim_lock=lock,
        ) and db.mark_failed(
            con, t["id"], generation=generation, claim_lock=lock
        ) and not blocked:
            cid = cdcl.learn(con, t, verdict["reason"], cfg, worker, canon)  # 尸检立禁令
            if cid:
                db.add_event(
                    con,
                    t["id"],
                    "clause_learned",
                    {"clause_id": cid},
                    generation=generation,
                )
    return True


def _publish_finalizing(con, t, verify_token, generation, reasons, artifacts=None):
    """CAS done, then publish the verified artifact list into the index.

    入库发生在 ``finish_finalize`` 之后（实现如此，注释曾说反）；因此产物清单
    必须用**验收时校验过的那份**（artifacts 参数）而不是重读盘上的 report.json，
    入库端还会再过一遍路径栅栏。文件树无事务,靠 corpus.ingest 幂等——同 sha
    已在即跳过,重试不重复入库。
    """
    if verify_token is None or not db.owns_verification(
        con, t["id"], verify_token, generation=generation
    ):
        return False
    # 先认领 finalize,再入库：认领失败就不入库（替代原 SQLite 事务的"部分发布禁止"）。
    if not db.owns_verification(
        con, t["id"], verify_token, generation=generation
    ) or not db.finish_finalize(
        con, t["id"], verify_token, generation=generation
    ):
        return False

    got = []
    index_error = None
    try:
        got = ws_index.ingest_artifacts(con, t, artifacts=artifacts)
    except Exception as error:  # 入库是派生索引,建不上不回滚卡的 finalize（可后续 ws reindex 补）
        index_error = error

    if index_error is not None:
        db.add_event(
            con,
            t["id"],
            "index_error",
            {"error": str(index_error)[:200]},
            generation=generation,
        )
    elif got:
        db.add_event(
            con,
            t["id"],
            "indexed",
            {"docs": [item[0] for item in got]},
            generation=generation,
        )
    db.add_event(
        con,
        t["id"],
        "verify_pass",
        {"reasons": reasons},
        generation=generation,
    )
    return True


def judge_task(con, t, cfg, verify_token=None, generation=None):
    """Run the mandatory red-team gate for one verifying card."""
    generation = int(t["generation"] if generation is None else generation)
    if verify_token is not None and not db.owns_verification(
        con, t["id"], verify_token, generation=generation
    ):
        return
    ws = t["workspace"]
    # 红队闸前 keystone：直调，缺了就该炸（曾 getattr 防御＝静默跳闸，审查 2026-08-20）
    valid, report_or_reason = worker.check_report(ws or "")
    if not valid:
        if db.back_to_ready(
            con, t["id"], verify_token, generation=generation
        ):
            # verify_reclaimed ≠ reclaimed：验收侧打回不算崩溃，不进白死计数
            db.add_event(
                con,
                t["id"],
                "verify_reclaimed",
                {"reason": f"verifying report invalid: {report_or_reason}"[:500]},
                generation=generation,
            )
        return
    verified_artifacts = list(report_or_reason.get("artifacts", []))
    report_limit = worker.MAX_REPORT_BYTES
    try:
        with open(os.path.join(ws, "report.json"), "rb") as f:
            report_bytes = f.read(report_limit + 1)
        if len(report_bytes) > report_limit:
            raise ValueError("report.json too large")
        report_full = report_bytes.decode("utf-8")
        report_raw = report_full[:4000]
    except (OSError, UnicodeDecodeError, ValueError):
        if db.back_to_ready(
            con, t["id"], verify_token, generation=generation
        ):
            db.add_event(
                con,
                t["id"],
                "verify_reclaimed",
                {"reason": "verifying 但 report.json 消失"},
                generation=generation,
            )
        return
    if t["status"] == "finalizing":
        decision = db.latest_payload(
            con, t["id"], "verify_decision_pass", generation=generation
        )
        try:
            reasons = json.loads(decision or "{}").get("reasons") or []
        except (TypeError, ValueError, AttributeError):
            reasons = []
        _publish_finalizing(con, t, verify_token, generation, reasons,
                            artifacts=verified_artifacts)
        return
    try:
        uncertain = json.loads(report_full).get("uncertain") or []
    except (ValueError, AttributeError):
        uncertain = []
    focus = ("\n# 作者点名的心虚点（集中火力先查这些）\n"
             + "\n".join(f"- {x}" for x in uncertain[:3]) + "\n") if uncertain else ""
    cases = precedent.as_prompt(precedent.relevant(con, f"{t['title']} {(t['body'] or '')[:300]}", canon))
    prompt = (
        f"# 待验收卡片合同\n标题：{t['title']}\n\n{t['body']}\n\n{cases}"
        f"# 交卷 report.json\n{guard.untrusted('report.json', report_raw)}{focus}\n"
        "工作目录就是该卡工作区。按你的验收流程办：先从合同（尤其「## 验收」节）列判据清单，"
        "再用 read 工具逐条核对产物实物，最后只输出 verdict JSON："
        '{"pass": true|false, "reasons": ["…"], "must_fix": ["…"]}'
    )
    for attempt in (1, 2):  # 判官偶发挂起/吐非 JSON：就地重试一次
        if verify_token is not None and not db.owns_verification(
            con, t["id"], verify_token, generation=generation
        ):
            return
        obj, _raw, err = worker.run_llm_json(
            os.path.join(cfg["roles_root"], "redteam"), prompt,
            cfg["provider"], cfg["default_model"],
            cwd=ws, tools=["read"], timeout=cfg.get("judge_timeout", 600),
            usage_db=cfg["db"],
            usage_task_id=t["id"],
            usage_generation=generation,
            usage_token_cap=cfg.get("token_cap"),
            on_event=lambda line: db.add_event(
                con,
                t["id"],
                "harn_event",
                _compact_event(line),
                generation=generation,
            ),
        )
        errors = validate.validate_verdict(obj) if err is None else [err]
        if not errors:
            break
        if verify_token is None or db.owns_verification(
            con, t["id"], verify_token, generation=generation
        ):
            db.add_event(
                con,
                t["id"],
                "verify_error",
                {"attempt": attempt, "errors": errors},
                generation=generation,
            )
        else:
            return
    if errors:
        # ponytail: 判官两次都跪＝基础设施故障，不是 Sister 的错——留 verifying 等下一趟，
        # 绝不误判 failed（agent-smith 教训：设施故障要重试，只有真判据不过才丢单）
        return
    if obj["pass"]:
        vetoes = run_hooks(cfg, t, t["workspace"] or "")
        if vetoes:  # 钩子一票否决：判官说过，闸不认
            rounds = db.bump_verify(
                con, t["id"], verify_token, generation=generation
            )
            if rounds is None:
                return
            db.add_event(
                con,
                t["id"],
                "hook_veto",
                {"round": rounds, "vetoes": vetoes},
                generation=generation,
            )
            if rounds >= MAX_VERIFY_ROUNDS:
                failure = {
                    "reason": "verification hooks failed repeatedly",
                    "must_fix": vetoes,
                }
                if db.add_event(
                    con,
                    t["id"],
                    "failed",
                    failure,
                    generation=generation,
                    verify_lock=verify_token,
                ):
                    db.mark_failed(
                        con, t["id"], verify_token, generation=generation
                    )
            else:
                if db.back_to_ready(
                    con, t["id"], verify_token, generation=generation
                ):
                    db.add_event(
                        con,
                        t["id"],
                        "verify_fail",
                        {"round": rounds, "must_fix": vetoes},
                        generation=generation,
                    )
            return
        db.add_event(
            con,
            t["id"],
            "verify_decision_pass",
            {"reasons": obj["reasons"]},
            generation=generation,
        )
        if verify_token is None or not db.begin_finalize(
            con, t["id"], verify_token, generation=generation
        ):
            return
        finalizing = dict(t)
        finalizing["status"] = "finalizing"
        _publish_finalizing(
            con,
            finalizing,
            verify_token,
            generation,
            obj["reasons"],
            artifacts=verified_artifacts,
        )
    else:
        rounds = db.bump_verify(
            con, t["id"], verify_token, generation=generation
        )
        if rounds is None:
            return
        if rounds >= MAX_VERIFY_ROUNDS:
            failure = {
                "reason": "; ".join(obj["must_fix"]) or "verification failed repeatedly",
                "round": rounds,
                "must_fix": obj["must_fix"],
            }
            if db.add_event(
                con,
                t["id"],
                "failed",
                failure,
                generation=generation,
                verify_lock=verify_token,
            ) and db.mark_failed(
                con, t["id"], verify_token, generation=generation
            ):
                db.add_event(
                    con,
                    t["id"],
                    "verify_gave_up",
                    {"round": rounds, "must_fix": obj["must_fix"]},
                    generation=generation,
                )
        else:
            if db.back_to_ready(
                con, t["id"], verify_token, generation=generation
            ):
                db.add_event(
                    con,
                    t["id"],
                    "verify_fail",
                    {"round": rounds, "must_fix": obj["must_fix"]},
                    generation=generation,
                )


def dispatch_once(con, cfg):
    store.init_all(con)  # 入口自保：被当库调用时也有全套表
    reconcile(con)
    n = 0
    for t in db.by_status(con, "ready"):
        n += run_task(con, db.get(con, t["id"]), cfg) or 0
    for t in [*db.by_status(con, "verifying"), *db.by_status(con, "finalizing")]:
        token = f"judge:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
        ttl = max(1800, int(cfg.get("judge_timeout", 600)) * 7 + 300)
        generation = int(t["generation"])
        if db.claim_verification(
            con, t["id"], token, ttl, generation=generation
        ):
            try:
                # 认领后重读：verifying→finalizing 的窗口里旧快照会导致重跑整场判卷
                judge_task(con, db.get(con, t["id"]), cfg, token, generation=generation)
            finally:
                db.release_verification(
                    con, t["id"], token, generation=generation
                )
            n += 1
    return n

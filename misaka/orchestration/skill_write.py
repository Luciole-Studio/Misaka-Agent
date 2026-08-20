"""技能写权闸＋变更总账＋写入来源（宪法 D2 的实现）。

宪法原文：
  铁律④「技能运行时只读；**沉淀走提案制人审**；记忆编辑权只给史官」
  D2「记忆外置；**skill 写权三档默认 forbid**；沉淀走提案制＋留出法验收」

在此之前这两条在代码里没有任何着落点：`/learn` 让 agent 拿通用 write 直接写进活的
角色技能树，下一个会话立刻加载——没有闸、没有 diff、没有记账、没有回滚。

严格对齐 hermes 三件（tools/write_approval.py｜skill_ledger.py｜skill_provenance.py），
按 misaka 的角色维度落地：

  ① 写权三档（config `skill_write_mode`）：
       forbid（缺省）＝暂存待人审｜ask＝同 forbid（misaka 无内联审批通道）｜allow＝直写
     hermes 的 skills 分支永远 stage（SKILL.md 太大不能内联审）——misaka 同此结论。
  ② 变更总账：append-only JSONL ＋ 内容寻址 blob（sha256）。对上铁律①事件溯源、
     ⑧证据键。总账是遥测不是闸门——记账失败绝不阻断它描述的那次变更；
     唯独 rollback fail-closed（缺任一 blob 就整体中止，且先记 pre-rollback 快照）。
  ③ 写入来源：ContextVar 记「谁发起的这次写入」。hermes 只有前台/后台自省两态，
     misaka 有编排官/妹妹/红队/分身——这个维度信息量更大。
"""
import contextvars
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

# off＝彻底关闭（agent 连暂存都不行，硬拒）｜forbid（缺省）＝暂存待人审｜
# ask＝同 forbid｜allow＝直写。off 是 misaka 自加的一档（hermes 的闸有意只延迟
# 不拒绝）——用户主权开关：关的是 **agent** 的写入能力；人经 CLI approve 重放
# 走 bypass，不受 off 影响
WRITE_MODES = ("off", "forbid", "ask", "allow")
DEFAULT_WRITE_MODE = "forbid"          # 宪法 D2：默认 forbid

_ORIGIN = contextvars.ContextVar("misaka_skill_write_origin", default="user")


def _root():
    return Path(os.path.expanduser("~/.misaka"))


def _config():
    try:
        with open(_root() / "skills.json", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_mode():
    """当前写权档。无法识别的值按 forbid（安全侧）——坏配置不该 fail-open。"""
    mode = str(_config().get("skill_write_mode") or DEFAULT_WRITE_MODE).strip().lower()
    return mode if mode in WRITE_MODES else DEFAULT_WRITE_MODE


# ── ① 写入来源（hermes skill_provenance.py）──────────────────────────

def set_origin(origin):
    """设置本上下文的写入来源；返回 Token，调用方必须在 finally 里 reset_origin。"""
    return _ORIGIN.set(str(origin or "user"))


def reset_origin(token):
    _ORIGIN.reset(token)


def current_origin():
    """谁在写：user／last-order／10032／redteam／agent:<分身>…（默认 user）。"""
    return _ORIGIN.get()


# ── ② 总账（hermes skill_ledger.py）──────────────────────────────────

def _ledger_path():
    return _root() / "skills" / ".ledger.jsonl"


def _blob_dir():
    return _root() / "cache" / "skill_blobs"


def _store_blob(path):
    """文件 → sha256 内容寻址 blob（已存在就不重写）。返回 sha256。"""
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    blob = _blob_dir() / digest
    if not blob.exists():
        blob.parent.mkdir(parents=True, exist_ok=True)
        tmp = blob.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, blob)
    return digest


def snapshot(root):
    """技能目录 → [{path(相对), sha256}]，目录不存在＝空清单。"""
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for f in sorted(root.rglob("*")):
        if f.is_file() and not f.is_symlink():
            out.append({"path": str(f.relative_to(root)), "sha256": _store_blob(f)})
    return out


def record(action, skill, *, before=None, after_root=None, evidence=None):
    """追加一条总账。**永不抛、永不阻断它描述的那次变更**（遥测不是闸门）。"""
    try:
        entry = {
            "id": uuid.uuid4().hex[:12],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "actor": current_origin(),
            "action": action,
            "skill": str(skill),
            "evidence": evidence or {},
            "before": before if before is not None else [],
            "after": snapshot(after_root) if after_root else [],
        }
        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry["id"]
    except Exception:  # noqa: BLE001 - 记账失败不拖累变更本身
        return None


def entries(limit=None):
    """读总账（旧→新）。坏行跳过，不让一行毁掉整本账。"""
    out = []
    try:
        with open(_ledger_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out[-limit:] if limit else out


def rollback(entry_id, skill_root):
    """回滚一条总账（**fail-closed**）：任一 blob 缺失就整体中止、什么都不动；
    动手前先记一条 pre-rollback 快照——让回滚本身也可回滚。返回 (ok, 说明)。"""
    target = next((e for e in entries() if e["id"] == entry_id), None)
    if target is None:
        return False, f"总账里没有这条：{entry_id}"
    root = Path(skill_root)
    # 预检：先确认每个要用的 blob 都在
    for item in target["before"]:
        if not (_blob_dir() / item["sha256"]).exists():
            return False, f"blob 缺失，整体中止：{item['path']} ({item['sha256'][:12]})"
    if root.exists() and not str(root.resolve()).startswith(str(_root().resolve())):
        return False, "拒绝回滚到 ~/.misaka 之外的路径"
    record("pre-rollback", target["skill"], after_root=root,
           evidence={"rollback_of": entry_id})
    root.mkdir(parents=True, exist_ok=True)
    keep = set()
    for item in target["before"]:
        dest = root / item["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((_blob_dir() / item["sha256"]).read_bytes())
        keep.add(dest.resolve())
    for item in target["after"]:      # 该次变更新建的文件：删掉
        dest = root / item["path"]
        if dest.is_file() and dest.resolve() not in keep:
            dest.unlink()
    record("rollback", target["skill"], after_root=root,
           evidence={"rollback_of": entry_id})
    return True, f"已回滚 {target['skill']}（{len(target['before'])} 个文件）"


# ── ③ 写权闸（hermes write_approval.py）─────────────────────────────

def _pending_dir():
    return _root() / "pending" / "skills"


def evaluate_gate():
    """本次技能写入该怎么办。返回 (decision, 给 agent 看的话)。
    decision ∈ {allow, stage, off}。forbid/ask 只延迟不拒绝（hermes 同款）；
    off 是用户主权硬关——agent 连暂存都不行。skills 永远 stage 而非内联审。"""
    mode = write_mode()
    if mode == "allow":
        return "allow", ""
    if mode == "off":
        # 不教命令：这条文案是给 agent 看的，agent 有 bash——写出切换命令等于教它绕。
        # 用户自己知道开关在哪（/skill-mode、misaka skills mode）。
        return "off", ("技能写入已被用户全局关闭。不要再试，也不要用 bash/write 绕过"
                       "（改配置或直写技能目录都算绕）——开不开回来是用户的决定，"
                       "需要时把这个情况告诉用户即可。")
    return "stage", (
        f"技能写入已暂存待人审（skill_write_mode={mode}，宪法 D2）。"
        "**尚未落盘**——用 `misaka skills pending` 看待审、`misaka skills approve <id>` 批准。")


def stage(payload, *, summary):
    """暂存一次待审写入。落盘失败也返回记录——对审批闸来说，丢掉写入是安全的失败。"""
    record_id = uuid.uuid4().hex[:8]
    item = {
        "id": record_id,
        "summary": (summary or "").strip(),
        "origin": current_origin(),
        "created_at": time.time(),
        "payload": payload,
    }
    try:
        d = _pending_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{record_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return item


def list_pending():
    try:
        files = sorted(_pending_dir().glob("*.json"))
    except OSError:
        return []
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: r.get("created_at", 0))


def get_pending(pending_id):
    return next((r for r in list_pending() if r["id"] == pending_id), None)


def discard_pending(pending_id):
    try:
        (_pending_dir() / f"{pending_id}.json").unlink()
        return True
    except OSError:
        return False


def pending_diff(item):
    """待审写入 vs 盘上现状的 diff（人审要看的东西）。"""
    import difflib
    payload = item.get("payload") or {}
    target = Path(payload.get("path") or "")
    new = str(payload.get("content") or "")
    old = ""
    if target.is_file():
        try:
            old = target.read_text(encoding="utf-8-sig")
        except OSError:
            old = ""
    return "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"现状/{target.name}", tofile=f"待审/{target.name}", lineterm=""))


if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    os.environ["HOME"] = str(tmp)
    (tmp / ".misaka").mkdir()

    # 写权档：缺省 forbid；坏值也回 forbid（不 fail-open）
    assert write_mode() == "forbid"
    (tmp / ".misaka" / "skills.json").write_text('{"skill_write_mode":"nonsense"}', encoding="utf-8")
    assert write_mode() == "forbid", "无法识别的档位必须回落安全侧"
    (tmp / ".misaka" / "skills.json").write_text('{"skill_write_mode":"allow"}', encoding="utf-8")
    assert write_mode() == "allow" and evaluate_gate()[0] == "allow"
    (tmp / ".misaka" / "skills.json").write_text('{"skill_write_mode":"forbid"}', encoding="utf-8")
    decision, msg = evaluate_gate()
    assert decision == "stage" and "尚未落盘" in msg

    # 来源：ContextVar，进出成对
    token = set_origin("10032")
    assert current_origin() == "10032"
    reset_origin(token)
    assert current_origin() == "user"

    # 暂存＋列举＋diff
    skill_dir = tmp / ".misaka" / "profiles" / "sisters" / "10032" / "skills" / "档案整理"
    skill_dir.mkdir(parents=True)
    target = skill_dir / "SKILL.md"
    target.write_text("---\nname: 档案整理\ndescription: 旧版\n---\n旧正文\n", encoding="utf-8")
    tok = set_origin("last-order")
    item = stage({"path": str(target), "content": "---\nname: 档案整理\ndescription: 新版\n---\n新正文\n"},
                 summary="更新档案整理技能")
    reset_origin(tok)
    assert item["origin"] == "last-order", "待审记录带发起者"
    assert len(list_pending()) == 1 and get_pending(item["id"])["summary"] == "更新档案整理技能"
    diff = pending_diff(item)
    assert "旧正文" in diff and "新正文" in diff, "人审要看得见 diff"

    # 总账＋回滚（fail-closed 预检、pre-rollback 快照）
    before = snapshot(skill_dir)
    assert before and before[0]["sha256"], "快照带证据键"
    target.write_text("---\nname: 档案整理\ndescription: 新版\n---\n新正文\n", encoding="utf-8")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "新附件.md").write_text("附件", encoding="utf-8")
    eid = record("update", "档案整理", before=before, after_root=skill_dir)
    assert eid and entries()[-1]["actor"] == "user"
    ok, why = rollback(eid, skill_dir)
    assert ok, why
    assert "旧正文" in target.read_text(encoding="utf-8"), "before 文件已还原"
    assert not (skill_dir / "references" / "新附件.md").exists(), "该次新建的文件已删"
    kinds = [e["action"] for e in entries()]
    assert kinds[-2:] == ["pre-rollback", "rollback"], f"回滚本身也进账: {kinds}"

    ok2, why2 = rollback("不存在的id", skill_dir)
    assert not ok2 and "没有这条" in why2
    assert discard_pending(item["id"]) and not list_pending()
    print("skill_write selfcheck ok — 写权档/来源/暂存 diff/总账证据键/回滚 fail-closed")

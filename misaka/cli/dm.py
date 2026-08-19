"""agent 互信：hermes Bot Mode DM 模型严格移植（MIT）——canonical 联络会话＋直投唤醒。

hermes 机制逐段对应：每个 bot 一个永生「Bot Chat」会话；发信＝把带署名前缀
`Message from 🤖 <名> (@<handle>): ` 的消息提交进对方会话并跑一轮，收件人立即
处理；回信走对称通道（她自己发 DM），协议纪律禁止原地等回复。
misaka 落地差异（结构性，非语义）：
- 会话定位不靠 title 检索——引擎会话按目录组织，每角色固定
  ~/.misaka/sessions/<角色>/dm/ 即 canonical（比 hermes 按标题三级回退更稳）；
  标题串 "Bot Chat" 保留进 MISAKA_APP_TITLE＝协议注入闸（阶段2）＋显示锚。
- misaka 无 gateway 常驻排队层：并发投递按收件人 flock 串行（锁即队列）；
  超时时消息已落对方会话文件——晚到不丢（stranded-harvest 同语义）。
- 审计：送达后在 messages.db 补一行即时 delivered（hermes 没有的总账，白拿）。
- cwd=~（hermes CLI `--in ~` 同款）。
"""
import fcntl
import os
import sys
from contextlib import contextmanager

from misaka.config import CFG

DM_TITLE = "Bot Chat"   # hermes BOT_CHAT_TITLE 逐字（注入闸/清扫白名单键在这个串上）


def dm_prefix(sender):
    """hermes 署名前缀逐字（`Message from 🤖 ${senderName} (@${senderHandle}): `）。"""
    return f"Message from 🤖 {sender} (@{sender}): "


def dm_session_dir(to):
    return os.path.expanduser(f"~/.misaka/sessions/{to}/dm")


@contextmanager
def _serial(to):
    """按收件人排队（hermes gateway 按会话串行的等价物）：flock 阻塞即队列。"""
    path = os.path.expanduser(f"~/.misaka/locks/dm-{to}.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)   # close 即释放

def deliver(to, message, sender=None, model=None, timeout=600):
    """直投一条消息进 to 的联络会话并跑一轮。返回退出码：0 拿到回复，
    1 会话出错（消息未入场），2 超时（消息已入场，回复晚到不丢）。"""
    from misaka.cli import chat
    from misaka.config import profiles, sisters
    from misaka.extensions import messages
    from misaka.orchestration.session import run_coro, run_session

    to = (to or "").strip().replace("_", "-")
    sender = (sender or "").strip().replace("_", "-") or None
    known = {"last-order"} | sisters()
    if to not in known:
        sys.exit(f"没有这个收件人：{to}。在册地址：{', '.join(sorted(known))}")
    if sender == to:
        sys.exit("自己给自己发 DM 没有意义")
    body = (message or "").strip()
    if not body:
        sys.exit("消息不能为空")
    text = dm_prefix(sender) + body if sender else body   # 用户直发＝普通用户轮，无前缀

    prof, soul, model_default, skill_flags = chat.assembly(
        None if to == "last-order" else to)
    home = os.path.expanduser("~")
    sess_dir = dm_session_dir(to)
    os.makedirs(sess_dir, exist_ok=True)
    flags = ["--provider", CFG["provider"], "--model", model or model_default,
             "--append-system-prompt", profiles.shared_soul(),
             "--append-system-prompt", soul,
             "--session-dir", sess_dir] + skill_flags
    try:
        if any(n.endswith(".jsonl") for n in os.listdir(sess_dir)):
            flags.append("-c")   # canonical＝接续唯一现场；空目录＝首次开箱
    except OSError:
        pass
    role = profiles.role_of(prof)
    env = {"MISAKA_APP_TITLE": DM_TITLE, "MISAKA_DM_SESSION": "1",
           "MISAKA_WHO": to, "MISAKA_MCP_ROLE": role,
           "MISAKA_PROFILE_DIR": prof, "MISAKA_WORKSPACE": home}
    # 同包复用 chat 的装配（收件人带全副能力处理消息；receive=True 顺带清她的信箱）
    factories = chat._extension_factories(prof, role, home, to,
                                          sister=(to != "last-order"))
    with _serial(to):
        r = run_coro(run_session(flags, text, home, timeout=timeout,
                                 extension_factories=factories, env=env))
        if not r["error"]:
            con = messages.connect()   # 审计行：已直投即已送达（收信循环绝不能再送）
            try:
                messages.claim(con, [messages.send(
                    con, to, text, summary=body[:80], sender=sender or "user")])
            finally:
                con.close()
    if r["error"]:
        print(f"DM 会话出错：{r['error']}", file=sys.stderr)
        return 1
    if r["timed_out"]:
        print(f"等回复超时（{timeout}s）：消息已入 {to} 的联络会话，回复晚到不丢",
              file=sys.stderr)
        return 2
    if r["text"]:
        print(r["text"])
    return 0


if __name__ == "__main__":
    import asyncio
    import sqlite3
    import tempfile
    import threading
    import time

    tmp = tempfile.mkdtemp()
    os.environ["HOME"] = tmp   # 会话/锁/信箱全进沙箱
    CFG["messages_db"] = os.path.join(tmp, "messages.db")
    CFG["profiles_root"] = os.path.join(tmp, "sisters")
    CFG["roles_root"] = tmp
    os.makedirs(os.path.join(tmp, "sisters", "10032"))
    os.makedirs(os.path.join(tmp, "last_order"))

    assert dm_prefix("10033") == "Message from 🤖 10033 (@10033): ", "hermes 署名逐字"

    from misaka.orchestration import session as sess_mod
    calls = []

    async def fake_run(flags, prompt, cwd, on_event=None, timeout=600, env=None,
                       extension_factories=None):
        calls.append({"flags": list(flags), "prompt": prompt, "cwd": cwd,
                      "env": dict(env or {})})
        return {"text": "收到", "timed_out": False, "error": None, "budget_usage": None}

    real_run = sess_mod.run_session
    sess_mod.run_session = fake_run
    try:
        code = deliver("10032", "档案在哪", sender="last_order")
        assert code == 0
        c = calls[-1]
        assert c["prompt"] == "Message from 🤖 last-order (@last-order): 档案在哪", \
            "发件人归一＋前缀逐字"
        assert c["env"]["MISAKA_APP_TITLE"] == "Bot Chat" \
            and c["env"]["MISAKA_DM_SESSION"] == "1", "注入闸锚"
        assert c["cwd"] == tmp, "cwd=~（hermes --in ~ 同款）"
        i = c["flags"].index("--session-dir")
        assert c["flags"][i + 1].endswith("/sessions/10032/dm"), "canonical 目录"
        assert "-c" not in c["flags"], "空目录＝首次开箱不接续"

        with open(os.path.join(tmp, ".misaka/sessions/10032/dm/x.jsonl"), "w") as f:
            f.write("{}")
        deliver("10032", "又来", sender=None)
        assert "-c" in calls[-1]["flags"], "有现场＝接续"
        assert calls[-1]["prompt"] == "又来", "用户直发无前缀"

        rows = sqlite3.connect(CFG["messages_db"]).execute(
            "SELECT sender, delivered_at FROM messages ORDER BY id").fetchall()
        assert len(rows) == 2 and all(r[1] for r in rows), "审计行即时 delivered"
        assert rows[0][0] == "last-order" and rows[1][0] == "user"

        for bad, why in ((("10099", "在吗"), "不在册"),
                         (("10032", "  "), "空消息")):
            try:
                deliver(*bad)
                raise AssertionError(why + "该被拒")
            except SystemExit as e:
                assert e.code != 0
        try:
            deliver("10032", "自语", sender="10032")
            raise AssertionError("自发自收该被拒")
        except SystemExit:
            pass

        async def slow_run(flags, prompt, cwd, **kw):
            await asyncio.sleep(0.3)
            return {"text": "慢", "timed_out": False, "error": None, "budget_usage": None}

        sess_mod.run_session = slow_run
        t0 = time.monotonic()
        t = threading.Thread(target=deliver, args=("10032", "先到"))
        t.start()
        time.sleep(0.05)   # 让线程先拿到锁
        deliver("10032", "后到")
        t.join()
        assert time.monotonic() - t0 >= 0.55, "同收件人串行（flock 即队列）"

        async def err_run(flags, prompt, cwd, **kw):
            return {"text": None, "timed_out": False, "error": "boom", "budget_usage": None}

        sess_mod.run_session = err_run
        n = len(sqlite3.connect(CFG["messages_db"]).execute(
            "SELECT * FROM messages").fetchall())
        assert deliver("10032", "会炸") == 1
        assert len(sqlite3.connect(CFG["messages_db"]).execute(
            "SELECT * FROM messages").fetchall()) == n, "出错＝没入场，不记假账"
    finally:
        sess_mod.run_session = real_run
    print("dm selfcheck ok — 前缀/归一/闸锚/-c/审计/拒收/串行/错误不记账")

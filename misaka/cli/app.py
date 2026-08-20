"""misaka — 御坂网络 CLI。薄入口：解析参数、调各包、打印结果；业务逻辑住在各自的包里。

入口经 pyproject [project.scripts] 生成 `misaka` 命令（照 engine/harn 同款做法，仓根无入口脚本）。
"""
import argparse
import os
import signal
import sys

from misaka.extensions.board import db
from misaka.research import basemap
from misaka.orchestration import budget
from misaka.research.indexer import index as corpus
from misaka.research.indexer import workspace as ws_index
from misaka.research.kernel import audit, calibration, canon, cdcl, evidence, frontier, harvest, precedent, saturation, store, synthesize, tms, verdict
from misaka.config import CFG, sisters
from misaka.extensions.board import tail


def _parser():
    p = argparse.ArgumentParser(prog="misaka")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="建卡")
    a.add_argument("title")
    a.add_argument("--body", default="")
    a.add_argument("--body-file")
    a.add_argument("--assignee", required=True)
    a.add_argument("--model")
    a.add_argument("--priority", type=int, default=0)
    a.add_argument("--timeout", type=int, default=900)
    a.add_argument("--project", help="归属课题(须先 misaka project 建目录);不填=未分类")

    pr = sub.add_parser("project", help="课题:建/列/归档/置顶/删除")
    pr.add_argument("name", nargs="?", help="课题名(=目录名);省略则列出全部")
    pr_act = pr.add_mutually_exclusive_group()
    pr_act.add_argument("--archive", action="store_true", help="归档（列表沉底标灰）")
    pr_act.add_argument("--unarchive", action="store_true", help="恢复进行中")
    pr_act.add_argument("--pin", action="store_true", help="置顶")
    pr_act.add_argument("--unpin", action="store_true", help="取消置顶")
    pr_act.add_argument("--delete", action="store_true",
                        help="删课题（有卡挂着会拒；目录软删进 .trash-*）")
    pr.add_argument("--with-cards", action="store_true",
                    help="随 --delete：连课题下的卡一起删（卡是硬删）")

    tk = sub.add_parser("task", help="卡：删除（破坏性，连事件与预算一并抹）")
    tk.add_argument("task_id")
    tk.add_argument("--delete", action="store_true", required=True,
                    help="硬删这张卡（在跑的先 misaka net stop）")

    sub.add_parser("board", help="看板")
    t = sub.add_parser("tail", help="跟事件流")
    t.add_argument("--since", type=int)
    t.add_argument("--no-follow", action="store_true")
    tl = sub.add_parser("tell", help="协力者给编排官/御坂送信（在卡的工作区里跑）")
    tl.add_argument("message", help="要说的话")
    tl.add_argument("--to", default="last-order", help="收件人（默认编排官）")
    tl.add_argument("--summary", help="一句话摘要")

    dmp = sub.add_parser("dm", help="hermes 式互信：把消息直投某角色的联络会话并唤醒她跑一轮")
    dmp.add_argument("to", help="收件角色：last-order 或妹妹编号")
    dmp.add_argument("message", help="消息正文")
    dmp.add_argument("--from", dest="sender", help="发件角色（agent 互发署名；省略＝用户直发）")
    dmp.add_argument("--model", help="覆盖模型")
    dmp.add_argument("--timeout", type=int, default=600, help="等回复上限秒（超时不丢信）")
    dmp.add_argument("--summary", help="一句话摘要（审计行用）")
    dmp.add_argument("--task-id", dest="dm_task", help=argparse.SUPPRESS)      # 发件卡上下文
    dmp.add_argument("--generation", dest="dm_gen", type=int, help=argparse.SUPPRESS)

    sub.add_parser("init", help="建库")

    nt = sub.add_parser("net", help="御坂网络守护进程：格子里跑卡（断线保活）")
    nt_sub = nt.add_subparsers(dest="net_cmd", required=True)
    nt_sub.add_parser("status", help="守护进程与格子概况（不在会拉起）")
    nt_sub.add_parser("panes", help="列出所有格子")
    nr = nt_sub.add_parser("run-card", help="把一张 ready 卡放进格子里跑")
    nr.add_argument("task_id")
    nd = nt_sub.add_parser("read", help="看某格子的输出尾巴")
    nd.add_argument("pane_id")
    nd.add_argument("--lines", type=int, default=40)
    ns = nt_sub.add_parser("send", help="往格子里打字（默认带回车）")
    ns.add_argument("pane_id")
    ns.add_argument("text")
    ns.add_argument("--no-enter", action="store_true")
    nc = nt_sub.add_parser("close", help="关一个格子（终止其进程组）")
    nc.add_argument("pane_id")
    nx = nt_sub.add_parser("explain", help="为什么这个格子判成在跑/闲着")
    nx.add_argument("pane_id", nargs="?")
    nt_sub.add_parser("stop", help="停守护进程（所有格子一并关闭）")
    sub.add_parser("net-daemon", help=argparse.SUPPRESS)     # 内部：守护进程本体
    cs = sub.add_parser("card-shell", help=argparse.SUPPRESS)  # 内部：格子内跑卡会话
    cs.add_argument("task_id")
    cs.add_argument("--resume", action="store_true",
                    help="只接续会话现场看/手聊，不重发合同（面板点卡用）")

    sub.add_parser("panel", help="★ 多格子面板（光敲 misaka 就是它）：编排官+妹妹格子+状态灯")
    ch = sub.add_parser("chat", help="直连对话（不经守护进程的逃生舱）；--as <sister> 直接找某位妹妹")
    ch.add_argument("--model", help="覆盖模型")
    ch.add_argument("--as", dest="as_agent", metavar="SISTER",
                    help="改为与某个 Sister 对话（名册见 misaka board）")
    ch.add_argument("-c", "--continue", dest="cont", action="store_true",
                    help="接续上次会话（默认开新会话，与 claude 一致）")
    ch.add_argument("--pick", action="store_true", help="从历史会话里挑一个恢复")
    ch.add_argument("--session", help="切入指定会话（文件路径或部分 UUID，在该角色的会话目录里找）")

    pl = sub.add_parser("plan", help="Last Order：目标 → 卡片上板")
    pl.add_argument("goal")
    pl.add_argument("--dry", action="store_true", help="只打印卡片不上板")

    rs = sub.add_parser("research", help="深研模式（无头）：立论→找刺→束宽展开多轮循环（花模型额度）")
    rs.add_argument("goal", help="研究目标（首跑建立论卡；课题已有立论卡则只续跑）")
    rs.add_argument("--project", required=True, help="归属课题（不存在会自动建）")
    rs.add_argument("--assignee", default="10032", help="立论与展开卡的 Sister")
    rs.add_argument("--rounds", type=int, help="授权轮数；缺省=不限（只剩自然闸）")
    rs.add_argument("--beam", type=int, default=4, help="束宽：每轮最多展开几个刺/缺口")
    rs.add_argument("--cap", type=int, help="本次预算顶（对全局账本比对）；缺省=沿用全局")
    rs.add_argument("--no-synth", action="store_true", help="终止后不建综合卡")

    hv = sub.add_parser("harvest", help="收割：已验收卡的产物 → 图节点")
    hv.add_argument("ids", nargs="*", help="卡 id；缺省=全部未收割的 done 卡")

    ex = sub.add_parser("expand", help="前沿：挑 top-k 缺口生成新卡上板")
    ex.add_argument("-k", type=int, default=2)
    ex.add_argument("--assignee", default="10032")
    ex.add_argument("--dry", action="store_true")

    sub.add_parser("graph", help="看研究图")

    tr = sub.add_parser("trace", help="执行迹：无参=全局树＋每卡脉搏；<卡号|角色|路径>=单会话迷宫（←→选步 f过滤 /搜索 +-缩放）")
    tr.add_argument("targets", nargs="*", help="0 个=全局；1 个=单会话迷宫；2 个=同轴对比")
    tr.add_argument("--project", help="全局档：只看某课题")
    tr.add_argument("--watch", action="store_true", help="实时刷新（全局档每 2s；迷宫档盯现场文件生长）")
    tr.add_argument("--plain", action="store_true", help="迷宫档：不进交互，打一帧就走（管道/嵌入用）")

    lc = sub.add_parser("lcm", help="无损上下文运维：status 存量 / doctor 只读体检 / backup 热备快照")
    lc.add_argument("op", nargs="?", default="status", choices=["status", "doctor", "backup"])

    sk = sub.add_parser("skills", help="技能：trust 信任仓 / list 装配栈 / scan 扫描 / "
                                       "pending 待审 / approve 批准 / reject 弃审 / "
                                       "ledger 变更账 / rollback 回滚（宪法 D2）")
    sk.add_argument("op", nargs="?", default="list",
                    choices=["trust", "list", "scan", "pending", "approve", "reject",
                             "ledger", "rollback"])
    sk.add_argument("name", nargs="?", help="approve/reject：技能名；rollback：总账条目 id")
    sk.add_argument("--dir", help="项目根（缺省＝当前目录向上找 .git）")
    sk.add_argument("--as", dest="role", default="sisters/10032", help="以哪个角色的视角看栈")

    ac = sub.add_parser("auth", help="凭据预检：auth check <provider> 看某家认证配没配好")
    ac.add_argument("op", nargs="?", default="check", choices=["check"])
    ac.add_argument("provider", nargs="?", help="供应商 id（省略＝列出全部已配置的）")
    ac.add_argument("--show", action="store_true", help="连解析到的凭据一起打（小心屏幕共享）")

    au = sub.add_parser("selftest", help="免疫系统：安慰剂抽验判官（破坏产物看抓不抓得住）")
    au.add_argument("-k", type=int, default=1, help="抽几张已通过的卡")
    au.add_argument("--seed", type=int)

    sub.add_parser("immune", help="免疫状态：预算/禁令/判例一览")

    bm = sub.add_parser("basemap", help="底图：看格 / 灌种子")
    bm.add_argument("--load", action="store_true", help="灌入首期三套分类法种子")
    bm.add_argument("--scheme", nargs="*", help="只看某几套（OCM CAP JEL）")

    wsp = sub.add_parser("ws", help="工作区导航树：卡片/材料/产物同树（PageIndex 形状）")
    wsp.add_argument("action", choices=["outline", "read", "reindex"])
    wsp.add_argument("arg", nargs="?", help="read:node_id  outline:限定卡 id")

    dc = sub.add_parser("doc", help="文献层：入库/检索/复核引文（FTS5 页级正典＋PageIndex 树＋语义）")
    dc.add_argument("action", choices=["add", "list", "find", "verify", "tree"])
    dc.add_argument("arg", nargs="?", help="add:文件路径 find:查询词 verify:引文 tree:doc_id")
    dc.add_argument("--doc", help="限定某份文献（doc_id）")
    dc.add_argument("--semantic", action="store_true", help="find 用语义检索")
    dc.add_argument("--no-tree", action="store_true", help="add 时跳过 PageIndex 建树（默认 ≥20 页的 PDF 自动建）")
    dc.add_argument("--project", help="doc add: 归属课题（落到该课题目录的 corpus/）")

    sv = sub.add_parser("survey", help="网格扫描：逐格问「这格与命题通不通」→ 生成触达判定卡")
    sv.add_argument("proposition", help="研究命题")
    sv.add_argument("--scheme", nargs="*", default=["OCM"], help="用哪几套底图（默认 OCM）")
    sv.add_argument("--assignee", default="10032")
    sv.add_argument("--dry", action="store_true")

    cl = sub.add_parser("claims", help="证据台账：覆盖率与全量复核（宪法⑧）")
    cl.add_argument("--audit", action="store_true", help="逐条复核 blob 与引文")
    sub.add_parser("saturation", help="饱和仪表：挖够了没有")

    ca = sub.add_parser("calibration", help="校准账：harvester 预估 vs 实际回报（分桶+收缩修正）")
    ca.add_argument("--project", help="只看某课题")
    ca.add_argument("--drift", action="store_true", help="把系统性高/低估立成判例喂回 harvester")

    hy = sub.add_parser("hypothesize", help="假说综合：同课题高权重发现→溯因假说→检验卡（花模型额度）")
    hy.add_argument("--project", help="综合哪个课题（不填=未分类池）")
    hy.add_argument("--assignee", default="10032")

    fa = sub.add_parser("falsify", help="裁定假说被检验证伪：标 dropped + 立禁令")
    fa.add_argument("node_id", help="hypothesis 节点号")
    fa.add_argument("--reason", default="", help="证伪依据（写进禁令）")

    vd = sub.add_parser("verdict", help="裁决：矛盾预筛+送审+标注谁站得住")
    vd.add_argument("--limit", type=int, default=8, help="本轮最多送审几对")
    vd.add_argument("--dry", action="store_true", help="只列候选不送审")

    rt = sub.add_parser("retract", help="撤回一张卡的证据基础（连坐+复核卡）")
    rt.add_argument("task_id")
    rt.add_argument("--reason", default="")
    rt.add_argument("--assignee", default="10032")
    rt.add_argument("--no-card", action="store_true")

    sy = sub.add_parser("synth", help="建综合卡（把已 done 的卡汇成 REPORT.md）")
    sy.add_argument("ids", nargs="*", help="卡 id；缺省=全部 done")
    sy.add_argument("--title", default="综合报告")

    cr = sub.add_parser("create", help="新建御坂（配置向导；--desc/--model 直接给值免问）")
    cr.add_argument("sid", nargs="?", help="编号，如 10033")
    cr.add_argument("--desc", help="一句话人格/专长（写进 SOUL）")
    cr.add_argument("--model", help="钉模型，如 claude-opus-5")
    rm = sub.add_parser("remove", help="御坂除名（历史卡与工作区保留；活卡在跑拒绝）")
    rm.add_argument("sid", help="编号")
    rm.add_argument("--yes", action="store_true", help="跳过确认（非交互环境必须）")
    return p


def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # tail | head 不炸
    argv = sys.argv[1:]
    if not argv:
        # 光敲 misaka ＝ 多格子面板（有终端才行；管道/脚本里退回直连对话）
        argv = ["panel"] if sys.stdin.isatty() and sys.stdout.isatty() else ["chat"]
    args = _parser().parse_args(argv)
    con = db.connect(CFG["db"])
    store.init_all(con)

    if args.cmd == "chat":
        from misaka.cli import chat
        chat.launch(args.as_agent, model=args.model, cont=args.cont, pick=args.pick,
                    session=args.session)
    elif args.cmd == "panel":
        from misaka.cli import panel
        panel.launch()
    elif args.cmd == "net-daemon":
        from misaka.net import daemon
        daemon.main()
    elif args.cmd == "card-shell":
        from misaka.cli import card_shell
        card_shell.launch(args.task_id, resume_only=args.resume)
    elif args.cmd == "net":
        from misaka.net import client as net
        if args.net_cmd == "stop":
            try:
                net.request("server.stop")
                print("已通知守护进程停机")
            except (ConnectionError, FileNotFoundError, OSError):
                print("守护进程本来就不在")
            sys.exit(0)
        info = net.ensure()
        if args.net_cmd == "status":
            print(f"守护进程 pid={info['pid']}，格子 {info['panes']} 个")
        elif args.net_cmd == "panes":
            for p in net.request("panes.list")["panes"]:
                state = "跑" if p["alive"] else f"退({p['exit_code']})"
                card = f" 卡:{p['card']}" if p["card"] else ""
                print(f"{p['id']}  [{state}]{card}  {p['title']}  {p['cwd']}")
        elif args.net_cmd == "run-card":
            out = net.request("pane.run_card", {"task_id": args.task_id})
            print(f"卡 {args.task_id} 已进格子 {out['pane_id']}（pid {out['pid']}）")
        elif args.net_cmd == "read":
            out = net.request("pane.read",
                              {"id": args.pane_id, "lines": args.lines, "strip": True})
            print(out["text"])
        elif args.net_cmd == "send":
            net.request("pane.send", {"id": args.pane_id, "text": args.text,
                                      "enter": not args.no_enter})
        elif args.net_cmd == "close":
            net.request("pane.close", {"id": args.pane_id})
        elif args.net_cmd == "explain":
            ids = ([args.pane_id] if args.pane_id
                   else [p["id"] for p in net.request("panes.list")["panes"]])
            for pane_id in ids:
                out = net.request("pane.explain", {"id": pane_id})
                mark = "●在跑" if out["busy"] else "○闲着"
                print(f"{out['id']}  {mark}  {out['title']}\n    {out['why']}")
    elif args.cmd == "init":
        print("board:", os.path.expanduser(CFG["db"]))
    elif args.cmd == "create":
        from misaka.extensions import roster
        sys.exit(roster.cli_create(args.sid, desc=args.desc, model=args.model))
    elif args.cmd == "remove":
        from misaka.extensions import roster
        sys.exit(roster.cli_remove(args.sid, yes=args.yes))
    elif args.cmd == "add":
        from misaka.extensions.board import project
        body = args.body
        if args.body_file:
            with open(args.body_file, encoding="utf-8") as f:
                body = f.read()
        proj = project.require(args.project)   # 拼错/未注册当场报错
        tid = db.create_task(con, args.title, body=body, assignee=args.assignee,
                             model=args.model, priority=args.priority, timeout_seconds=args.timeout,
                             project=proj)
        print(tid)
    elif args.cmd == "project":
        from misaka.extensions.board import project
        if not args.name:
            state = project.states(con)
            names = project.listing()
            if not names:
                print("(还没有课题)")
            for n in sorted(names, key=lambda x: (
                    state.get(x, {}).get("archived", False),
                    -(state.get(x, {}).get("pinned_at") or 0), x)):
                meta = state.get(n, {})
                marks = ("★" if meta.get("pinned_at") else "") + \
                        ("（已归档）" if meta.get("archived") else "")
                print(f"{n} {marks}".rstrip())
        elif args.delete:
            ok, msg = project.delete(con, args.name, with_cards=args.with_cards)
            print(msg)
            sys.exit(0 if ok else 1)
        elif args.archive or args.unarchive or args.pin or args.unpin:
            ok, msg = project.set_state(
                con, args.name,
                archived=True if args.archive else False if args.unarchive else None,
                pinned=True if args.pin else False if args.unpin else None)
            print(msg)
            sys.exit(0 if ok else 1)
        else:
            ok, msg = project.create(args.name)
            print(msg)
            sys.exit(0 if ok else 1)
    elif args.cmd == "tell":
        from misaka.extensions.ally import tell as ally_tell
        ok, msg = ally_tell.tell(args.message, to_addr=args.to, summary=args.summary)
        print(msg)
        sys.exit(0 if ok else 1)
    elif args.cmd == "dm":
        from misaka.cli import dm as dm_cli
        sys.exit(dm_cli.deliver(args.to, args.message, sender=args.sender,
                                model=args.model, timeout=args.timeout,
                                task_id=args.dm_task, generation=args.dm_gen,
                                summary=args.summary))
    elif args.cmd == "task":
        ok, msg = db.delete_task(con, args.task_id)
        print(msg)
        sys.exit(0 if ok else 1)
    elif args.cmd == "board":
        tail.board_view(con)
    elif args.cmd == "tail":
        tail.follow(con, since=args.since, once=args.no_follow)
    elif args.cmd == "plan":
        from misaka.extensions.board import plan
        bet, cards, errors, raw = plan.make(CFG, args.goal, sisters())
        if errors:
            sys.exit("计划书不过:\n" + "\n".join(f"  {e}" for e in errors) + f"\n---原文---\n{(raw or '')[-1000:]}")
        print(f"赌注：{bet}\n")
        for c in cards:
            print(("[dry] " if args.dry else "") + c["title"], "→", c["assignee"])
        if not args.dry:
            for tid in plan.submit(con, bet, cards):
                print(" ", tid)
    elif args.cmd == "research":
        import asyncio as _asyncio

        from misaka.extensions.board import project as project_mod
        from misaka.extensions.board import research_loop
        from misaka.extensions.board import worker as worker_mod
        if not project_mod.exists(args.project):
            print(project_mod.create(args.project)[1])
        boot = research_loop.bootstrap(con, goal=args.goal, project=args.project,
                                       assignee=args.assignee)
        if boot["argument_task"]:
            print(f"立论卡 {boot['argument_task']} 已上板")
        cfg = dict(CFG)
        if args.cap:
            cfg["token_cap"] = args.cap
        out = _asyncio.run(research_loop.run_loop(
            con, cfg, research_loop.DispatchRunner(con, cfg), worker_mod,
            project=args.project, assignee=args.assignee, beam=args.beam,
            max_rounds=args.rounds, poll_seconds=1.0, synthesize=not args.no_synth))
        print(f"深研收场：{out['reason']}｜共 {out['rounds']} 轮"
              + (f"｜综合 {out['synthesis']}" if out.get("synthesis") else ""))
        for e in out["errors"]:
            print(f"  ⚠️ {e}")
    elif args.cmd == "harvest":
        from misaka.extensions.board import worker
        rows = [db.get(con, i) for i in args.ids] if args.ids else db.by_status(con, "done")
        harvested = {r["task_id"] for r in con.execute(
            "SELECT DISTINCT src AS task_id FROM edges WHERE kind='from_task'")}
        rows = [r for r in rows if r and r["status"] == "done" and (args.ids or r["id"] not in harvested)]
        if not rows:
            print("没有待收割的卡")
        for r in rows:
            fids, gids, err = harvest.harvest_task(con, store, r, CFG, worker, evidence=evidence)
            if err and not fids and not gids:
                print(f"{r['id']}  收割失败: {err}")
                continue
            merged = canon.dedup(con, store, fids + gids, project=r["project"])
            print(f"{r['id']}  发现 {len(fids)} 缺口 {len(gids)}" +
                  (f"  判重合流 {len(merged)}" if merged else "") + (f"  ⚠️ {err}" if err else ""))
            for dup, keep, sim in merged:
                print(f"    {dup} → {keep} (相似 {sim})")
    elif args.cmd == "expand":
        picks = frontier.pick(con, store, k=args.k)
        if not picks:
            print("前沿无可挖缺口（跑 harvest 先收割）")
        for node, sc in picks:
            c = frontier.card_for(node, args.assignee)
            print(f"[{sc:.2f}] {c['title']}")
            if not args.dry:
                tid = db.create_task(con, c["title"], body=c["body"], assignee=c["assignee"])
                store.set_status(con, node["id"], "expanded")
                store.add_edge(con, node["id"], tid, "expanded_to")
                print(" ", tid)
    elif args.cmd == "trace":
        if len(args.targets) > 2:
            sys.exit("最多两个目标（单会话或同轴对比）")
        if args.targets:
            from misaka.cli import trace_view
            paths = []
            for t in args.targets:
                p, err = trace_view.locate_session(con, t)
                if err:
                    sys.exit(err)
                paths.append(p)
            sys.exit(trace_view.run(paths, watch=args.watch, plain=args.plain))
        import time as _time

        from misaka.extensions.board import observe
        if not args.watch:
            print(observe.render(con, args.project))
        else:
            try:
                while True:
                    print("\x1b[2J\x1b[H" + observe.render(con, args.project), flush=True)
                    _time.sleep(2)
            except KeyboardInterrupt:
                pass
    elif args.cmd == "lcm":
        import os as _os

        from misaka.orchestration.lcm import maintenance as lcm_maint
        lcm_db = _os.path.expanduser(CFG.get("lcm_db") or "~/.misaka/lcm.db")
        if args.op == "status":
            st = lcm_maint.status(lcm_db)
            print(f"库 {st['db']}｜{st['size_bytes']:,} 字节｜"
                  f"{st['sessions']} 会话｜{st['messages']} 条原文｜{st['nodes']} 个摘要节点")
            for sid_, v in st["per_session"].items():
                print(f"  {sid_}: 原文 {v['messages']} 摘要 {v['nodes']}")
        elif args.op == "doctor":
            for c in lcm_maint.doctor(lcm_db):
                mark = {"pass": "✅", "warn": "⚠️", "fail": "❌"}[c["status"]]
                suffix = f"  → {c['action']}" if c["status"] != "pass" else ""
                print(f"{mark} {c['check']}: {c['detail']}{suffix}")
            print("note: 只读体检，未改任何行")
        else:
            dest, err = lcm_maint.backup(lcm_db)
            print(err if err else f"已备份：{dest}")
    elif args.cmd == "auth":
        # 凭据预检（pi #7152/a261366b）：跑卡前先确认认证配好了，别烧到一半才发现
        import asyncio as _asyncio

        from misaka.core.auth_storage import AuthStorage
        storage = AuthStorage.create()
        targets = [args.provider] if args.provider else sorted(storage.getAll())
        if not targets:
            print("没有任何已存凭据（misaka 用的是 provider 配置：见 ~/.misaka/auth.json）")
            sys.exit(1)
        bad = 0
        for provider in targets:
            status = storage.getAuthStatus(provider)
            mark = "✓" if status.configured or status.source else "✗"
            detail = status.source or "未配置"
            if status.label:
                detail += f"（{status.label}）"
            line = f"{mark} {provider}  {detail}"
            if args.show and (status.configured or status.source):
                key = _asyncio.run(storage.getApiKey(provider))
                line += f"  {key}" if key else "  （解析不出凭据）"
            print(line)
            if not (status.configured or status.source):
                bad += 1
        sys.exit(1 if bad else 0)
    elif args.cmd == "skills":
        import os as _os

        from misaka.orchestration import skill_layers
        if args.op in ("pending", "approve", "reject", "ledger", "rollback"):
            import shutil as _shutil

            from misaka.orchestration import skill_write
            staging = _os.path.expanduser("~/.misaka/pending/skills/workspace")
            live = _os.path.join(CFG["roles_root"], args.role, "skills")

            def _staged():
                if not _os.path.isdir(staging):
                    return []
                return sorted(d for d in _os.listdir(staging)
                              if _os.path.isdir(_os.path.join(staging, d)))

            if args.op == "pending":
                names = _staged()
                print(f"写权档：{skill_write.write_mode()}（宪法 D2 缺省 forbid）")
                if not names:
                    print("暂存区没有待审技能")
                from misaka.orchestration.skill_linter import format_findings, lint_skill
                for n in names:
                    md = _os.path.join(staging, n, "SKILL.md")
                    desc = ""
                    if _os.path.isfile(md):
                        from misaka.utils.frontmatter import parse_frontmatter
                        desc = str((parse_frontmatter(
                            open(md, encoding="utf-8-sig").read()).frontmatter or {}
                        ).get("description") or "")
                    print(f"  {n}  {desc}")
                    # 人审要看见检查结果（顾问层，不阻断批准）
                    print(format_findings(lint_skill(_os.path.join(staging, n))))
                    print(f"    批准：misaka skills approve {n} --as {args.role}")
            elif args.op == "approve":
                if not args.name:
                    sys.exit("要批准哪个：misaka skills approve <技能名>")
                src = _os.path.join(staging, args.name)
                if not _os.path.isdir(src):
                    sys.exit(f"暂存区没有「{args.name}」")
                dst = _os.path.join(live, args.name)
                before = skill_write.snapshot(dst)
                _os.makedirs(live, exist_ok=True)
                if _os.path.isdir(dst):
                    _shutil.rmtree(dst)
                _shutil.copytree(src, dst)
                _shutil.rmtree(src)
                eid = skill_write.record("approve", args.name, before=before, after_root=dst,
                                         evidence={"from": "pending", "role": args.role})
                print(f"已批准并落位：{dst}（总账 {eid}）")
            elif args.op == "reject":
                if not args.name:
                    sys.exit("要弃审哪个：misaka skills reject <技能名>")
                src = _os.path.join(staging, args.name)
                if not _os.path.isdir(src):
                    sys.exit(f"暂存区没有「{args.name}」")
                _shutil.rmtree(src)
                skill_write.record("reject", args.name, evidence={"role": args.role})
                print(f"已弃审并删除暂存：{args.name}")
            elif args.op == "ledger":
                rows = skill_write.entries(limit=30)
                if not rows:
                    print("总账还是空的")
                for e in rows:
                    print(f"{e['ts']}  {e['id']}  {e['actor']:<12} {e['action']:<12} "
                          f"{e['skill']}  (前{len(e['before'])}/后{len(e['after'])})")
            else:   # rollback
                if not args.name:
                    sys.exit("要回滚哪条：misaka skills rollback <总账id>（先看 ledger）")
                target = next((e for e in skill_write.entries() if e["id"] == args.name), None)
                if target is None:
                    sys.exit(f"总账里没有这条：{args.name}")
                ok, why = skill_write.rollback(
                    args.name, _os.path.join(live, target["skill"]))
                print(why)
                sys.exit(0 if ok else 1)
        elif args.op == "trust":
            target = args.dir or skill_layers.find_project_root()
            if not target:
                print("当前目录不在 git 仓里；用 --dir 指定项目根")
            else:
                print(skill_layers.trust_project_root(target)[1])
        elif args.op == "scan":
            rt = args.dir or skill_layers.find_project_root()
            if not rt:
                print("当前目录不在 git 仓里")
            else:
                from misaka.orchestration.skills_guard import format_scan_report, scan_skill
                found = False
                for d in skill_layers._candidate_project_skills_dirs(rt):
                    for md in __import__("pathlib").Path(d).rglob("SKILL.md"):
                        found = True
                        print(format_scan_report(scan_skill(md.parent, source="project-local")))
                if not found:
                    print("该仓没有项目技能（.misaka/skills 或 .agents/skills）")
        else:
            prof = _os.path.join(_os.path.expanduser(CFG["roles_root"]), args.role)
            for d in skill_layers.skills_stack(prof, cwd=_os.getcwd()):
                print(d)
            hint = skill_layers.get_untrusted_project_skills_root(cwd=_os.getcwd())
            if hint:
                print(f"（{hint[0]} 有 {hint[1]} 个技能未信任——misaka skills trust 解锁）")
    elif args.cmd == "graph":
        rows, nedges = store.stats(con)
        print("边:", nedges)
        for r in rows:
            print(f"  {r['kind']:<9} {r['status']:<9} {r['n']}")
        for n in store.nodes(con, status="open")[:15]:
            print(f"  {n['id']}  {n['kind']:<8} w={n['weight']:.2f}  {n['text'][:70]}")
    elif args.cmd == "selftest":
        import random as _r
        from misaka.extensions.board import validate
        from misaka.extensions.board import worker
        done = [t for t in db.by_status(con, "done") if t["workspace"]]
        picks = audit.reservoir(done, args.k, _r.Random(args.seed))
        if not picks:
            sys.exit("没有可抽验的已通过卡")
        for t in picks:
            r = audit.run_placebo(con, db, t, CFG, worker)
            mark = {"caught": "✅ 判官抓住了", "leaked": "🚨 判官放水（已记 placebo_failed）"}.get(r, "⏭ " + r)
            print(f"{t['id']}  {mark}")
            if r == "leaked":
                precedent.add(con, f"卡「{t['title']}」的产物被删掉一行验收要求的内容",
                              "缺任一硬性判据即判不通过，不许因整体看着不错就放行",
                              "placebo_leak", t["id"], canon)
                print("   → 已立判例，未来判官会带着它掌握尺度")
    elif args.cmd == "basemap":
        bcon = basemap.connect()
        if args.load:
            print("灌入", basemap.load_seeds(bcon), "格")
        st = basemap.stats(bcon)
        if not st:
            sys.exit("底图为空——先跑 misaka basemap --load")
        print("  ".join(f"{r['scheme']}={r['n']}" for r in st))
        for c in basemap.cells(bcon, args.scheme)[:80]:
            print(f"  {c['id']:<8} {c['label']}")
    elif args.cmd == "ws":
        if args.action == "outline":
            print(ws_index.render(ws_index.outline(con, task_id=args.arg)))
        elif args.action == "read":
            txt = ws_index.read(con, args.arg or "")
            print(txt if txt else "无此节点（用 misaka ws outline 看 node_id）")
        elif args.action == "reindex":
            n = 0
            for t in db.by_status(con, "done"):
                n += len(ws_index.ingest_artifacts(con, t))
            print(f"补索引 {n} 件产物入正典")
    elif args.cmd == "doc":
        if args.action == "add":
            proj = args.project if getattr(args, "project", None) else None
            did, n = corpus.ingest(args.arg, with_tree=not args.no_tree, project=proj)
            has = corpus.doc_dir(did) and os.path.exists(os.path.join(corpus.doc_dir(did), "tree.json"))
            print(f"{did}  {n} 页入正典  {'＋PageIndex 结构树' if has else '（无结构树，按页导航）'}  {os.path.basename(args.arg)}")
        elif args.action == "list":
            for d_ in corpus.docs():
                print(f"  {d_['doc_id']}  {d_['pages']:>4} 页  {d_['title']}")
        elif args.action == "find":
            if args.semantic:
                hits = corpus.search_semantic(args.arg, canon, doc_id=args.doc)
                if hits is None:
                    sys.exit("嵌入服务不可用——改用词面检索（去掉 --semantic）")
            else:
                hits = corpus.search_literal(args.arg, doc_id=args.doc)
            for h in hits:
                sc = f" {h['score']}" if "score" in h else ""
                print(f"  {h['doc_id']} p{h['page']}{sc}  {h['s'][:90]}")
            print(f"({len(hits)} 命中；进引用前请用 doc verify 逐字复核)")
        elif args.action == "verify":
            if not args.doc:
                sys.exit("需 --doc <doc_id>")
            v = corpus.verify_quote(args.doc, args.arg)
            if not v:
                sys.exit("❌ 这句话不在该文献里（引用非法）")
            print(f"✅ p{v['page']} 第 {v['offset']} 字\n   claim_hash {v['claim_hash']}")
        elif args.action == "tree":
            st = corpus.structure(args.arg or args.doc)
            if not st:
                sys.exit("无此文献")
            print(f"{st['title']}（{st['mode']}）")
            for pg in (st.get("pages") or [])[:40]:
                print(f"  p{pg['page']:<4} {pg['head']}")
    elif args.cmd == "survey":
        bcon = basemap.connect()
        cells = basemap.cells(bcon, args.scheme)
        if not cells:
            sys.exit("底图为空——先跑 misaka basemap --load")
        body = basemap.survey_body(cells, args.proposition)
        print(f"网格 {len(cells)} 格（{'/'.join(args.scheme)}）→ 触达判定卡")
        if not args.dry:
            tid = db.create_task(con, f"网格扫描：{args.proposition[:30]}", body=body,
                                 assignee=args.assignee, timeout_seconds=1800)
            print(" ", tid)
    elif args.cmd == "immune":
        b = budget.status(con, CFG["token_cap"])
        print(f"预算：{b['used']:,} tokens" + (f" / 上限 {b['cap']:,}（{b['ratio']:.0%}，档位 {b['mode']}）"
                                              if b["cap"] else "（未设上限，档位 normal）"))
        cs = cdcl.clauses(con)
        print(f"禁令：{len(cs)} 条" + ("" if not cs else "  最常命中："))
        for cid, text, scope, hits in cs[:5]:
            print(f"  #{cid} [{scope or '全部'}] 命中 {hits} 次  {text[:60]}")
        n = con.execute("SELECT COUNT(*) FROM precedents").fetchone()[0]
        print(f"判例：{n} 条")
        for s_, r_ in con.execute("SELECT situation, ruling FROM precedents ORDER BY id DESC LIMIT 3"):
            print(f"  情形 {s_[:50]} → {r_[:50]}")
        hooks = [h for h in sorted(os.listdir(CFG["hooks_dir"]))
                 if os.access(os.path.join(CFG["hooks_dir"], h), os.X_OK)] if os.path.isdir(CFG["hooks_dir"]) else []
        print(f"钩子闸：{len(hooks)} 个  {' '.join(hooks)}")
    elif args.cmd == "claims":
        cov = evidence.coverage(con, store)
        print(f"证据键覆盖：{cov['backed']}/{cov['findings']} 条发现有引文撑着（{cov['ratio']:.0%}）")
        n = con.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        print(f"台账条目：{n}    证据库：{evidence.root()}")
        if args.audit:
            bad = evidence.audit(con)
            print(f"复核：{n - len(bad)}/{n} 通过" + ("" if not bad else f"，{len(bad)} 条有问题："))
            for cid, nid, why, extra in bad[:20]:
                print(f"  claim#{cid} {nid} {why} {extra}")
        for r in con.execute("SELECT node_id, source_file, quote FROM claims ORDER BY id DESC LIMIT 3"):
            print(f"  例 {r[0]} ← {r[1]}: 「{r[2][:50]}…」")
    elif args.cmd == "saturation":
        for kind in ("finding", "gap"):
            r = saturation.reading(con, kind)
            print(f"{kind}: 互异 {r['distinct']}  观测 {r['observations']}  只见过一次 {r['singletons']}"
                  f"  → 下一铲出新 ≈ {r['p_new']:.0%}")
            print("   ", saturation.verdict(r))
    elif args.cmd == "calibration":
        if args.drift:
            made = calibration.drift_precedents(con, canon)
            print(f"立漂移判例 {len(made)} 条" if made else "无新漂移（或桶样本 <8）")
        print(calibration.view(con, project=args.project))
    elif args.cmd == "hypothesize":
        from misaka.extensions.board import project as project_mod
        from misaka.extensions.board import worker
        proj = project_mod.require(args.project)
        hid, card, err = synthesize.synthesize_project(con, store, CFG, worker, proj,
                                                       assignee=args.assignee)
        if err:
            sys.exit(err)
        tid = db.create_task(con, card["title"], body=card["body"], assignee=card["assignee"],
                             project=card["project"])
        store.add_edge(con, hid, tid, "tested_by")
        print(f"假说 {hid} 落图（explains 见 misaka graph）；检验卡 {tid} 上板"
              "——要跑进 misaka 对话说开工")
    elif args.cmd == "falsify":
        cid, msg = synthesize.mark_outcome(con, store, canon, args.node_id, True,
                                           reason=args.reason)
        print(msg)
        sys.exit(0 if cid else 1)
    elif args.cmd == "verdict":
        from misaka.extensions.board import worker
        pairs = verdict.candidates(con, store, canon)
        print(f"矛盾候选 {len(pairs)} 对（相似度落在 {verdict.BAND[0]}–{verdict.BAND[1]} 带内）")
        for a_, b_, sim in pairs[:args.limit]:
            print(f"  [{sim}] {a_['text'][:34]}… ⟷ {b_['text'][:34]}…")
        if not args.dry and pairs:
            n_conf, n_judged = verdict.judge_pairs(con, store, pairs, CFG, worker, limit=args.limit)
            print(f"送审 {n_judged} 对，判定冲突 {n_conf} 对")
        lab = verdict.label(con, store)
        tally = {}
        for v in lab.values():
            tally[v] = tally.get(v, 0) + 1
        print("标注:", "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
        for nid, v in lab.items():
            if v != "in":
                print(f"  {v:<10} {store.get(con, nid)['text'][:70]}")
    elif args.cmd == "retract":
        t = db.get(con, args.task_id)
        if not t:
            sys.exit("无此卡")
        direct, down = tms.retract(con, store, args.task_id, args.reason)
        print(f"撤回 {len(direct)} 个直接节点，连坐 {len(down)} 个下游节点")
        if direct and not args.no_card:
            stale = [store.get(con, x) for x in direct + list(down)]
            c = tms.recheck_card(stale, t["title"], args.reason, args.assignee)
            tid = db.create_task(con, c["title"], body=c["body"], assignee=c["assignee"])
            print("复核卡:", tid)
    elif args.cmd == "synth":
        from misaka.extensions.board import report
        rows = report.gather(con, args.ids)
        if not rows:
            sys.exit("没有可综合的 done 卡")
        print(report.create(con, rows, args.title), "已上板（跑 dispatch 执行）")


if __name__ == "__main__":
    main()

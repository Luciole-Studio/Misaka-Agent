"""MISAKA command-line entry point."""
import argparse
import os
import signal
import sys

from misaka.platform import tasks as db
from misaka.platform import budget
from misaka.documents import index as corpus
from misaka.config import CFG
from misaka.observability import board as tail


def _parser():
    p = argparse.ArgumentParser(prog="misaka")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="Create a task card")
    a.add_argument("title")
    a.add_argument("--body", default="")
    a.add_argument("--body-file")
    a.add_argument("--assignee", required=True)
    a.add_argument("--model")
    a.add_argument("--priority", type=int, default=0)
    a.add_argument("--timeout", type=int, default=900)

    tk = sub.add_parser("task", help="Manage task cards")
    tk.add_argument("task_id")
    tk.add_argument("--delete", action="store_true", required=True,
                    help="Delete the card and its event history")

    sub.add_parser("board", help="Show the task board")
    tl = sub.add_parser("tell", help="Send a message from inside a running card")
    tl.add_argument("message", help="Message body")
    tl.add_argument("--to", default="last-order", help="Recipient; defaults to Last Order")
    tl.add_argument("--summary", help="Short audit-log summary")

    dmp = sub.add_parser("dm", help="Deliver a message to an agent's contact session and run one turn")
    dmp.add_argument("to", help="Recipient: last-order or a Sister ID")
    dmp.add_argument("message", help="Message body")
    dmp.add_argument("--from", dest="sender", help="Sender role (default: the user)")
    dmp.add_argument("--model", help="Override the recipient's model")
    dmp.add_argument("--timeout", type=int, default=600, help="Seconds to wait for a reply")
    dmp.add_argument("--summary", help="Short audit-log summary")
    dmp.add_argument("--task-id", dest="dm_task", help=argparse.SUPPRESS)
    dmp.add_argument("--generation", dest="dm_gen", type=int, help=argparse.SUPPRESS)

    ini = sub.add_parser("init", help="Make this folder a MISAKA project (git repo + PROJECT.md + cards/) and initialize the database")
    ini.add_argument("--migrate", action="store_true",
                     help="One-shot: write card files for every existing board row (all projects)")

    nt = sub.add_parser("net", help="Control the Misaka Network daemon and its panes")
    nt_sub = nt.add_subparsers(dest="net_cmd", required=True)
    nt_sub.add_parser("status", help="Show daemon and pane status")
    nt_sub.add_parser("panes", help="List all panes")
    nr = nt_sub.add_parser("run-card", help="Run a ready card in a pane")
    nr.add_argument("task_id")
    nd = nt_sub.add_parser("read", help="Print the last lines of a pane's screen")
    nd.add_argument("pane_id")
    nd.add_argument("--lines", type=int, default=40)
    ns = nt_sub.add_parser("send", help="Type text into a pane (followed by Enter unless --no-enter)")
    ns.add_argument("pane_id")
    ns.add_argument("text")
    ns.add_argument("--no-enter", action="store_true")
    nc = nt_sub.add_parser("close", help="Close a pane and kill its process group")
    nc.add_argument("pane_id")
    nx = nt_sub.add_parser("explain", help="Explain why a pane is busy or idle")
    nx.add_argument("pane_id", nargs="?")
    nt_sub.add_parser("stop", help="Stop the daemon and close every pane")
    sub.add_parser("net-daemon", help="Internal: the Misaka Network daemon")
    cs = sub.add_parser("card-shell", help="Internal: run a card session inside a pane")
    cs.add_argument("task_id")
    cs.add_argument("--resume", action="store_true",
                    help="Resume the card's existing session")
    cs.add_argument("--say", metavar="TEXT",
                    help="With --resume: deliver this message as the first turn")

    sub.add_parser("panel", help="Open the Misaka Network panel (default in a terminal)")
    ch = sub.add_parser("chat", help="Chat with Last Order, or with a Sister via --as")
    ch.add_argument("--model", help="Override the model")
    ch.add_argument("--as", dest="as_agent", metavar="SISTER",
                    help="Chat with this Sister instead of Last Order")
    ch.add_argument("-c", "--continue", dest="cont", action="store_true",
                    help="Continue the most recent session instead of starting a new one")
    ch.add_argument("--pick", action="store_true", help="Pick a past session to resume")
    ch.add_argument("--session", help="Resume a session by path or UUID prefix")

    rs = sub.add_parser("research", help="Run the Research Workflow on a question")
    rs.add_argument("goal", nargs="?", help="Research question for a new run")
    rs.add_argument("--resume", metavar="RUN_ID", help="Resume an existing research run")
    rs.add_argument("--depth", type=int, default=3, help="Maximum branch depth")
    rs.add_argument("--node", nargs=2, metavar=("RUN_ID", "NODE_ID"),
                    help="(internal) run one research node in this process")
    rs.add_argument("--probe", nargs=2, metavar=("RUN_ID", "ISSUE_ID"),
                    help="(internal) run Last Order's fork on one issue in this process")


    lc = sub.add_parser("lcm", help="Inspect, back up, repair, or rebuild the LCM context database")
    lc.add_argument("op", nargs="?", default="status",
                    choices=["status", "doctor", "backup", "repair", "rebuild"])
    lc.add_argument("target", nargs="?", help="Session JSONL path for rebuild")

    sk = sub.add_parser("skills", help="Discover, review, approve, and manage skills")
    sk.add_argument("op", nargs="?", default="list",
                    choices=["list", "scan", "pending", "approve", "reject",
                             "ledger", "rollback", "mode"])
    sk.add_argument("name", nargs="?",
                    help="Skill name or pending ID; ledger ID for rollback; mode name for mode")
    sk.add_argument("--dir", help="Project folder to scan (default: the current directory)")
    sk.add_argument("--as", dest="role", default="sisters/10032",
                    help="Role whose skill stack to show")

    mo = sub.add_parser("moa", help="List or delete Mixture-of-Agents presets")
    mo.add_argument("op", nargs="?", default="list", choices=["list", "delete"])
    mo.add_argument("name", nargs="?", help="Preset name (for delete)")

    ac = sub.add_parser("auth", help="Check provider credentials")
    ac.add_argument("op", nargs="?", default="check", choices=["check"])
    ac.add_argument("provider", nargs="?", help="Provider ID (default: every configured provider)")
    ac.add_argument("--show", action="store_true", help="Print the resolved credential")



    dc = sub.add_parser("doc", help="Index documents, search them, show their structure, and verify quotes")
    dc.add_argument("action", choices=["add", "scan", "list", "find", "verify", "tree"])
    dc.add_argument("arg", nargs="?", help="File path (add), folder (scan; default: this folder), "
                                           "query (find), quote (verify), or document ID (tree)")
    dc.add_argument("--doc", help="Restrict to one document ID")
    dc.add_argument("--no-tree", action="store_true", help="Skip PageIndex structure extraction")


    cr = sub.add_parser("create", help="Create a new Sister")
    cr.add_argument("sid", nargs="?", help="Sister ID, such as 10033")
    cr.add_argument("--desc", help="Personality or specialty, written to SOUL.md")
    cr.add_argument("--model", help="Pinned model, such as claude-opus-5")
    rm = sub.add_parser("remove", help="Remove a Sister along with her sessions and workspace")
    rm.add_argument("sid", help="Sister ID")
    rm.add_argument("--yes", action="store_true", help="Skip confirmation")
    return p

def _doc_tree_lines(tree, depth=1):
    """``misaka doc tree``: one indented line per outline node, with its page span."""
    import json as _json
    if isinstance(tree, str):
        try:
            tree = _json.loads(tree)
        except ValueError:
            return []
    lines = []
    for node in tree or []:
        start, end = node.get("start_index"), node.get("end_index")
        span = f"p{start}" if start == end or end is None else f"p{start}-{end}"
        lines.append(f"{'  ' * depth}{node.get('title', '')}  ({span})")
        lines.extend(_doc_tree_lines(node.get("nodes") or [], depth + 1))
    return lines


def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    argv = sys.argv[1:]
    if not argv:
        # No arguments: open the panel in a terminal, plain chat when piped.
        argv = ["panel"] if sys.stdin.isatty() and sys.stdout.isatty() else ["chat"]
    args = _parser().parse_args(argv)
    con = db.connect(CFG["db"])

    if args.cmd == "chat":
        from misaka.cli import chat
        chat.launch(args.as_agent, model=args.model, cont=args.cont, pick=args.pick,
                    session=args.session)
    elif args.cmd == "panel":
        from misaka.ui.panel import panel
        panel.launch()
    elif args.cmd == "net-daemon":
        from misaka.ui.panel import daemon
        daemon.main()
    elif args.cmd == "card-shell":
        from misaka.cli import card_shell
        card_shell.launch(args.task_id, resume_only=args.resume, say=args.say)
    elif args.cmd == "net":
        from misaka.ui.panel import client as net
        if args.net_cmd == "stop":
            try:
                net.request("server.stop")
                print("Daemon shutdown requested.")
            except (ConnectionError, FileNotFoundError, OSError):
                print("Daemon is not running.")
            sys.exit(0)
        info = net.ensure()
        if args.net_cmd == "status":
            print(f"Daemon pid={info['pid']}, panes={info['panes']}")
        elif args.net_cmd == "panes":
            for p in net.request("panes.list")["panes"]:
                state = "running" if p["alive"] else f"exited:{p['exit_code']}"
                card = f" card:{p['card']}" if p["card"] else ""
                print(f"{p['id']}  [{state}]{card}  {p['title']}  {p['cwd']}")
        elif args.net_cmd == "run-card":
            out = net.request("pane.run_card", {"task_id": args.task_id})
            print(f"Card {args.task_id} started in pane {out['pane_id']} (pid {out['pid']}).")
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
                mark = "● busy" if out["busy"] else "○ idle"
                print(f"{out['id']}  {mark}  {out['title']}\n    {out['why']}")
    elif args.cmd == "init":
        from misaka.platform import cards
        if args.migrate:
            written, existed, no_folder = cards.migrate(con)
            print(f"migrated {written} card(s) to files ({existed} already had files, "
                  f"{no_folder} skipped: project folder gone)")
        else:
            for line in cards.init_project(os.getcwd()):
                print(line)
        print("board:", os.path.expanduser(CFG["db"]))
    elif args.cmd == "create":
        from misaka.network import roster
        sys.exit(roster.cli_create(args.sid, desc=args.desc, model=args.model))
    elif args.cmd == "remove":
        from misaka.network import roster
        sys.exit(roster.cli_remove(args.sid, yes=args.yes))
    elif args.cmd == "add":
        from misaka.platform import cards
        body = args.body
        if args.body_file:
            with open(args.body_file, encoding="utf-8") as f:
                body = f.read()
        tid = cards.create(con, os.getcwd(), args.title, body, args.assignee,
                           model=args.model, priority=args.priority,
                           timeout_seconds=args.timeout)
        print(tid)
    elif args.cmd == "tell":
        from misaka.extensions.last_order.ally import tell as ally_tell
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
        from misaka.platform import cards as card_files
        row = db.get(con, args.task_id)
        ok, msg = (card_files.remove(con, row["workspace"], args.task_id) if row
                   else (False, f"Card not found: {args.task_id}"))
        print(msg)
        sys.exit(0 if ok else 1)
    elif args.cmd == "board":
        tail.board_view(con, db.canonical_workspace())
    elif args.cmd == "research":
        import asyncio as _asyncio

        from misaka.network import worker as worker_mod
        from misaka.research import node as research_node, planner, runs, workflow
        if args.node:
            sys.exit(research_node.main(*args.node))
        if args.probe:
            sys.exit(research_node.main_probe(*args.probe))
        runs.init(con)
        if args.resume:
            run = runs.get(con, args.resume)
            if not run:
                sys.exit(f"Research run not found: {args.resume}")
            runs.resume(con, run["id"])
        else:
            if not args.goal:
                sys.exit("A new research run requires a question; use --resume RUN_ID to continue one.")
            brief = planner.ensure_project_brief(dict(CFG), worker_mod, args.goal, os.getcwd())
            from misaka.platform import cards as card_files
            card_files.init_project(os.getcwd())
            run = runs.create(con, workspace=os.getcwd(), question=args.goal,
                              limits={"max_depth": args.depth},
                              token_start=budget.spent(con))
            print(f"Project brief: {brief}\nResearch run {run['id']}: {runs.run_dir(run)}")
        out = _asyncio.run(workflow.run(
            con, dict(CFG), research_node.spawner(), worker_mod,
            run_id=run["id"], poll_seconds=1.0))
        print(f"Research run {run['id']}: {out['reason']}")
        final = out.get("final") or {}
        if final.get("path"):
            print(final["path"])
    elif args.cmd == "lcm":
        import os as _os

        from misaka.extensions.lcm import maintenance as lcm_maint
        lcm_db = _os.path.expanduser(CFG.get("lcm_db") or "~/.misaka/lcm.db")
        if args.op == "status":
            st = lcm_maint.status(lcm_db)
            print(
                f"Database {st['db']} | {st['size_bytes']:,} bytes | "
                f"{st['sessions']} sessions | {st['messages']} source messages | "
                f"{st['nodes']} summary nodes"
            )
            for sid_, v in st["per_session"].items():
                print(f"  {sid_}: {v['messages']} source messages, {v['nodes']} summaries")
        elif args.op == "doctor":
            for c in lcm_maint.doctor(lcm_db):
                mark = {"pass": "✅", "warn": "⚠️", "fail": "❌"}[c["status"]]
                suffix = f"  → {c['action']}" if c["status"] != "pass" else ""
                print(f"{mark} {c['check']}: {c['detail']}{suffix}")
            print("Read-only check; nothing was modified.")
        elif args.op == "backup":
            dest, err = lcm_maint.backup(lcm_db)
            print(err if err else f"Backup created: {dest}")
        elif args.op == "repair":
            result = lcm_maint.repair(lcm_db)
            print(
                f"Backup {result['backup'] or '-'} | messages FTS {result['messages_fts']} | "
                f"nodes FTS {result['nodes_fts']} | removed orphaned pending attempts "
                f"{result['orphan_pending_deleted']}"
            )
        else:
            if not args.target:
                print("Usage: misaka lcm rebuild SESSION.jsonl")
                sys.exit(2)
            result = lcm_maint.rebuild_from_session_file(lcm_db, args.target)
            print(
                f"Rebuild complete | session {result['session_id']} | "
                f"source messages {result['messages']} | summaries {result['nodes']} | "
                f"backup {result['backup'] or '-'}"
            )
    elif args.cmd == "auth":
        # Verify credentials up front so a run does not fail halfway through.
        import asyncio as _asyncio

        from misaka.core.auth_storage import AuthStorage
        storage = AuthStorage.create()
        targets = [args.provider] if args.provider else sorted(storage.getAll())
        if not targets:
            print("No provider credentials are configured. See ~/.misaka/auth.json.")
            sys.exit(1)
        bad = 0
        for provider in targets:
            status = storage.getAuthStatus(provider)
            mark = "✓" if status.configured or status.source else "✗"
            detail = status.source or "not configured"
            if status.label:
                detail += f" ({status.label})"
            line = f"{mark} {provider}  {detail}"
            if args.show and (status.configured or status.source):
                key = _asyncio.run(storage.getApiKey(provider))
                line += f"  {key}" if key else " (credential could not be resolved)"
            print(line)
            if not (status.configured or status.source):
                bad += 1
        sys.exit(1 if bad else 0)
    elif args.cmd == "moa":
        import json as _json
        import os as _os

        from misaka.extensions.moa.provider import MOA_CONFIG_PATH, load_moa_config, slot_label

        path = _os.path.expanduser(MOA_CONFIG_PATH)
        cfg = load_moa_config()
        if args.op == "delete":
            if not args.name:
                sys.exit("Usage: misaka moa delete <preset>")
            raw = {}
            try:
                with open(path, encoding="utf-8") as f:
                    raw = _json.load(f)
            except (OSError, ValueError):
                pass
            presets = raw.get("presets") if isinstance(raw.get("presets"), dict) else {}
            if args.name not in presets:
                known = ", ".join(cfg["presets"]) or "none"
                sys.exit(f'Unknown preset "{args.name}". Available presets: {known}.')
            if len(presets) <= 1:
                sys.exit("Cannot delete the last remaining MoA preset.")
            del presets[args.name]
            if raw.get("default_preset") == args.name:
                raw["default_preset"] = next(iter(presets))
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(raw, f, ensure_ascii=False, indent=2)
            print(f"Deleted preset '{args.name}'; default: {raw.get('default_preset')}")
        else:
            print(f"MoA presets in {path}")
            print("Use /model to select MoA·<preset>; every turn then runs the mixture until you switch away.")
            for name, preset in cfg["presets"].items():
                mark = "*" if name == cfg["default_preset"] else " "
                state = "" if preset["enabled"] else " (disabled)"
                print(f"\n{mark} {name}{state}  fanout={preset['fanout']}")
                for i, slot in enumerate(preset["reference_models"], 1):
                    off = "" if slot.get("enabled", True) else " (disabled)"
                    print(f"    advisor{i}: {slot_label(slot)}{off}")
                print(f"    aggregator: {slot_label(preset['aggregator'])}")
    elif args.cmd == "skills":
        import os as _os

        from misaka.skills import layers as skill_layers
        if args.op == "mode":
            from misaka.skills import write as skill_write
            if not args.name:
                mode = skill_write.write_mode()
                desc = {
                    "off": "agents cannot create or update skills",
                    "forbid": "agent writes are staged for your review (misaka skills pending); same as ask",
                    "ask": "agent writes are staged for your review (misaka skills pending)",
                    "allow": "agent writes are applied immediately",
                }[mode]
                print(f"skill_write_mode = {mode} — {desc}")
            elif args.name not in skill_write.WRITE_MODES:
                sys.exit(
                    f"Unknown skill write mode: {args.name}. "
                    f"Available modes: {', '.join(skill_write.WRITE_MODES)}"
                )
            elif _os.environ.get("MISAKA_WHO") or _os.environ.get("MISAKA_USAGE_TASK_ID"):
                # These variables are set in every agent shell; only a human may change this.
                sys.exit("Skill write mode can only be changed by the user, not from an agent session.")
            else:
                cfg = skill_layers.load_skills_config()
                cfg["skill_write_mode"] = args.name
                skill_layers.write_skills_config(cfg)
                print(f"Skill write mode set to {args.name} (effective immediately).")
        elif args.op in ("pending", "approve", "reject", "ledger", "rollback"):
            import shutil as _shutil

            from misaka.skills import write as skill_write
            staging = _os.path.expanduser("~/.misaka/pending/skills/workspace")
            live = _os.path.join(CFG["roles_root"], args.role, "skills")

            def _staged():
                if not _os.path.isdir(staging):
                    return []
                return sorted(d for d in _os.listdir(staging)
                              if _os.path.isdir(_os.path.join(staging, d)))

            if args.op == "pending":
                print(f"Skill write mode: {skill_write.write_mode()}")
                records = skill_write.list_pending()
                legacy = _staged()
                if not records and not legacy:
                    print("No skills are awaiting review.")
                from misaka.skills.linter import format_findings, lint_content
                for r in records:
                    payload = r.get("payload") or {}
                    print(f"  [{r['id']}] {r['summary']} (submitted by {r['origin']})")
                    if payload.get("content"):
                        # Advisory lint findings, shown before approval.
                        print(format_findings(lint_content(payload["content"])))
                    diff = skill_write.pending_diff(r)
                    if diff:
                        print("    " + diff.replace("\n", "\n    "))
                    print(f"    Approve: misaka skills approve {r['id']}")
                for n in legacy:
                    print(f"  [directory] {n}  approve: misaka skills approve {n} --as {args.role}")
            elif args.op == "approve":
                if not args.name:
                    sys.exit("Usage: misaka skills approve <pending-id-or-skill-name>")
                from misaka.skills import manage as skill_manage
                record = skill_write.get_pending(args.name) or next(
                    (r for r in skill_write.list_pending()
                     if (r.get("payload") or {}).get("name") == args.name), None)
                if record is not None:
                    # Re-run the reviewed request through the normal validation path.
                    result = skill_manage.apply_pending(record["payload"])
                    if not result.get("success"):
                        sys.exit(f"Approval failed: {result.get('error')}")
                    skill_write.discard_pending(record["id"])
                    print("Approved and applied.")
                else:
                    src = _os.path.join(staging, args.name)
                    if not _os.path.isdir(src):
                        sys.exit(f"No pending skill named '{args.name}'.")
                    dst = _os.path.join(live, args.name)
                    before = skill_write.snapshot(dst)
                    _os.makedirs(live, exist_ok=True)
                    if _os.path.isdir(dst):
                        _shutil.rmtree(dst)
                    _shutil.copytree(src, dst)
                    _shutil.rmtree(src)
                    eid = skill_write.record("approve", args.name, before=before,
                                             after_root=dst,
                                             evidence={"from": "pending", "role": args.role})
                    print(f"Approved and installed: {dst} (ledger entry {eid})")
            elif args.op == "reject":
                if not args.name:
                    sys.exit("Usage: misaka skills reject <pending-id-or-skill-name>")
                record = skill_write.get_pending(args.name) or next(
                    (r for r in skill_write.list_pending()
                     if (r.get("payload") or {}).get("name") == args.name), None)
                if record is not None:
                    skill_write.discard_pending(record["id"])
                    skill_write.record("reject", (record.get("payload") or {}).get("name", ""),
                                       evidence={"pending_id": record["id"]})
                    print(f"Rejected: {record['summary']}")
                else:
                    src = _os.path.join(staging, args.name)
                    if not _os.path.isdir(src):
                        sys.exit(f"No pending skill named '{args.name}'.")
                    _shutil.rmtree(src)
                    skill_write.record("reject", args.name, evidence={"role": args.role})
                    print(f"Rejected and removed pending skill: {args.name}")
            elif args.op == "ledger":
                rows = skill_write.entries(limit=30)
                if not rows:
                    print("The skill ledger is empty.")
                for e in rows:
                    print(
                        f"{e['ts']}  {e['id']}  {e['actor']:<12} {e['action']:<12} "
                        f"{e['skill']}  (before {len(e['before'])}/after {len(e['after'])})"
                    )
            else:   # rollback
                if not args.name:
                    sys.exit("Usage: misaka skills rollback <ledger-id>")
                target = next((e for e in skill_write.entries() if e["id"] == args.name), None)
                if target is None:
                    sys.exit(f"Skill ledger entry not found: {args.name}")
                ok, why = skill_write.rollback(
                    args.name, _os.path.join(live, target["skill"]))
                print(why)
                sys.exit(0 if ok else 1)
        elif args.op == "scan":
            from misaka.skills.guard import format_scan_report, scan_skill
            found = False
            for layer, root in skill_layers.skill_roots(None, cwd=args.dir or _os.getcwd()):
                if layer != "project":
                    continue
                for md in skill_layers.iter_skill_files(root):
                    found = True
                    print(format_scan_report(scan_skill(md.parent, source="project")))
            if not found:
                print("No project skills found under skills/.")
        else:
            from misaka.skills import index as skill_index
            prof = _os.path.join(_os.path.expanduser(CFG["roles_root"]), args.role)
            for e in skill_index.build(skill_layers.skill_roots(prof, cwd=_os.getcwd())):
                print(f"{e['layer']:<9}{e['category']}/{e['name']}  {e['description']}  ({e['dir']})")
    elif args.cmd == "doc":
        if args.action == "add":
            if not args.arg:
                sys.exit("Usage: misaka doc add <file> [--no-tree]")
            did, n = corpus.ingest(args.arg, with_tree=not args.no_tree)
            has = corpus.doc_dir(did) and os.path.exists(os.path.join(corpus.doc_dir(did), "tree.json"))
            structure = "with PageIndex structure" if has else "page navigation only"
            print(f"Added {os.path.basename(args.arg)} as {did}: {n} pages, {structure}.")
        elif args.action == "scan":
            ingested, skipped = corpus.scan(args.arg or os.getcwd())
            for did, path in ingested:
                print(f"  {did}  {path}")
            for path, reason in skipped:
                print(f"  skipped {path}: {reason}")
            print(f"Indexed {len(ingested)} file(s); skipped {len(skipped)}.")
        elif args.action == "list":
            for d_ in corpus.docs(workspace=db.canonical_workspace()):
                print(f"  {d_['doc_id']}  {d_['pages']:>4} pages  {d_['title']}")
        elif args.action == "find":
            hits = corpus.search_literal(args.arg, doc_id=args.doc, workspace=db.canonical_workspace())
            for h in hits:
                print(f"  {h['doc_id']} p{h['page']}  {h['s'][:90]}")
            print(f"{len(hits)} match(es). Use `misaka doc verify` before citing a quotation.")
        elif args.action == "verify":
            if not args.arg or not args.doc:
                sys.exit("Usage: misaka doc verify <quote> --doc <doc-id>")
            v = corpus.verify_quote(args.doc, args.arg)
            if not v:
                sys.exit("❌ Quote not found in that document.")
            print(f"✅ p{v['page']} offset {v['offset']}\n   claim_hash {v['claim_hash']}")
        elif args.action == "tree":
            if not (args.arg or args.doc):
                sys.exit("Usage: misaka doc tree <doc-id>")
            st = corpus.structure(args.arg or args.doc)
            if not st:
                sys.exit("Document not found.")
            print(f"{st['title']} ({st['mode']})")
            for line in _doc_tree_lines(st.get("tree")):
                print(line)
            for pg in (st.get("pages") or [])[:40]:
                print(f"  p{pg['page']:<4} {pg['head']}")

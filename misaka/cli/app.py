"""MISAKA command-line entry point."""
import argparse
import os
import sys
import time

from misaka.cli import bootstrap
from misaka.config import CFG, VERSION, current_config, home, layout
from misaka.core.documents import index as corpus
from misaka.core.network import board as tail
from misaka.core.platform import budget
from misaka.core.platform import tasks as db


class _ExactArgumentParser(argparse.ArgumentParser):
    """Require documented option names instead of accepting ambiguous prefixes."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)


def _parser(extension_commands=None):
    """The product CLI's grammar. ``extension_commands`` (``misaka.extensions.commands()``) are
    listed for help only: they are dispatched in ``main`` before this parser runs."""
    p = _ExactArgumentParser(prog="misaka")
    # The first thing anyone types after installing. Without it argparse answers the version
    # question with a usage error and exit 2, which reads as "this install is broken".
    p.add_argument("--version", "-V", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    tk = sub.add_parser("task", help="Add a task card, start one, or delete one",
                        usage="misaka task add TITLE --to SISTER (--body TEXT | --body-file PATH) [options]\n"
                              "       misaka task start ID\n"
                              "       misaka task ID --delete")
    tk.add_argument("words", nargs="+", help=argparse.SUPPRESS)
    tk.add_argument("--delete", action="store_true", help="Delete the card and its event history")
    tk.add_argument("--to", metavar="SISTER", help="add: the Sister (or ally) the card is for")
    tk.add_argument("--body", help="add: the task contract; it must have a `## acceptance criteria` section")
    tk.add_argument("--body-file", help="add: read the task contract from this file")
    tk.add_argument("--reviewer", help="add: an independent reviewer, a Sister other than the assignee")
    tk.add_argument("--priority", type=int, default=0, help="add: higher runs first (default: 0)")
    tk.add_argument("--model", help="add: the model for this card only (default: the Sister's own)")
    tk.add_argument("--needs", action="append", default=[], metavar="ID",
                    help="add: a card this one waits for (repeatable)")

    sub.add_parser("board", help="Show the task board")
    sub.add_parser("allies", help="List the enabled allies and whether each can take a card (spends no quota)")

    dmp = sub.add_parser("dm", help="Deliver a message to an agent's contact session and run one turn "
                         "(a running research node is told with misaka research --tell)")
    dmp.add_argument("to", help="Recipient: last-order or a Sister ID")
    dmp.add_argument("message", nargs="?", help="Message body (omitted: deliver what is already queued)")
    dmp.add_argument("--from", dest="sender", help="Sender role (default: the user)")
    dmp.add_argument("--model", help="Override the recipient's model")
    dmp.add_argument("--timeout", type=int, default=600, help="Seconds to wait for a reply")
    dmp.add_argument("--summary", help="Short audit-log summary")
    dmp.add_argument("--task-id", dest="dm_task", help=argparse.SUPPRESS)
    dmp.add_argument("--generation", dest="dm_gen", type=int, help=argparse.SUPPRESS)
    dmp.add_argument("--wait-message", type=int, help=argparse.SUPPRESS)

    st = sub.add_parser("setup", help="First-run wizard: environment, model & provider, Sisters, skills, documents, web search, research, project")
    up = sub.add_parser("update", help="Report whether this install is behind the repository; --apply fast-forwards it")
    up.add_argument("--apply", action="store_true", help="Run the update instead of only reporting it")
    un = sub.add_parser("uninstall", help=f"Remove the program and keep your data ({home.display()}); --full removes both. "
                                          "Project folders are never touched")
    scope = un.add_mutually_exclusive_group()
    scope.add_argument("--full", dest="mode", action="store_const", const="full",
                       help="Also remove the data, and MISAKA's derived files and links outside it")
    scope.add_argument("--data", dest="mode", action="store_const", const="data",
                       help="Remove the data only; the program stays installed")
    un.add_argument("--yes", action="store_true", help="Skip the questions (and the backup offer)")
    un.add_argument("--dry-run", action="store_true", help="List what would be removed and stop")
    st.add_argument("section", nargs="?", help="Run one section only: environment | model | sisters | skills | documents | web | research | project")

    sub.add_parser("init", help="Make this folder a MISAKA project (git repo + PROJECT.md + cards/) and initialize the database")

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
    ac = sub.add_parser("ally-card", help="Internal: run an ally's card over ACP inside a pane")
    ac.add_argument("task_id")
    ac.add_argument("--say", metavar="TEXT", help="Continue the card with this message as the first turn")
    sub.add_parser("ally-bridge", help="Internal: the MCP server lending an ally its card's tools")

    sub.add_parser("panel", help="Open the Misaka Network panel (default in a terminal)")
    gui = sub.add_parser("gui", help="打开中文图形界面（本地浏览器，支持 Windows / macOS / Linux）")
    gui.add_argument("--port", type=int, default=0, help="本地端口；默认自动选择")
    gui.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ch = sub.add_parser("chat", help="Chat with Last Order, or with a Sister via --as")
    ch.add_argument("--model", help="Override the model")
    ch.add_argument("-s", "--skills", action="append", help="Preload Skill names; repeat or separate with commas")
    ch.add_argument("--as", dest="as_agent", metavar="SISTER",
                    help="Chat with this Sister instead of Last Order")
    ch.add_argument("-c", "--continue", dest="cont", action="store_true",
                    help="Continue the most recent session instead of starting a new one")
    ch.add_argument("--pick", action="store_true", help="Pick a past session to resume")
    source = ch.add_mutually_exclusive_group()
    source.add_argument("--session", help="Open a session by path or UUID prefix")
    source.add_argument("--catalog", help="With --attach/--read-only: locate an unsaved session")
    access = ch.add_mutually_exclusive_group()
    access.add_argument("--attach", action="store_true", help="Send input to the original live session, without starting another agent")
    access.add_argument("--read-only", action="store_true",
                    help="Show existing session records without starting an agent")

    us = sub.add_parser("usage", help="What research runs spent: tokens and money, by conversation and by model")
    us.add_argument("--run", metavar="RUN_ID", help="One run, by conversation and by model")
    us.add_argument("--limit", type=int, default=10, help="How many recent runs to list without --run (default: 10)")

    rs = sub.add_parser("research", help="Run the Research Workflow on a question")
    rs.add_argument("goal", nargs="?", help="Research question for a new run")
    rs.add_argument("--resume", metavar="RUN_ID", help="Resume an existing research run")
    rs.add_argument("--tell", metavar="RUN_ID",
                    help="Say GOAL (the message) to a running node's Last Order, the root's unless --to names a node")
    rs.add_argument("--to", metavar="NODE_ID", help="With --tell: the node whose Last Order hears it")
    rs.add_argument("--limits", metavar="RUN_ID",
                    help="Change the limits of a running run to the options given with it (--sister-parallel 2 ...)")
    rs.add_argument("--depth", type=int, help="Maximum branch depth (default: 3)")
    rs.add_argument("--parallel", type=int, help="Maximum concurrent LO nodes (default: 4); saved with the run")
    rs.add_argument("--sister-parallel", type=int,
                    help="Maximum active Sister cards per LO node (default: 4); saved with the run, subject to global admission limits")
    rs.add_argument("--followups", type=int,
                    help="After its first cards are back, how many more times a node may send Sisters out, over its whole life (default: 2, 0-6)")
    rs.add_argument("--revisions", type=int,
                    help="How many times a node may revise its conclusion after the red team's review (default: 2, 0-6)")
    rs.add_argument("--max-nodes", type=int,
                    help="How many nodes the research graph may hold, the root included (default: 30)")
    rs.add_argument("--runner-key", help=argparse.SUPPRESS)
    rs.add_argument("--node", nargs=2, metavar=("RUN_ID", "NODE_ID"),
                    help="(internal) run one research node in this process")



    from misaka.core.skills.distribution import OPERATIONS as distribution_operations
    sk = sub.add_parser("skills", help="Discover, review, approve, and manage skills")
    sk.add_argument("op", nargs="?", default="list",
                    choices=["list", "scan", "pending", "approve", "reject",
                             "ledger", "rollback", "mode", 'usage', 'adopt', 'pin', 'unpin', 'archive', 'restore', 'sync-on', 'sync-off', 'curator-status', 'curator-run', 'curator-pause', 'curator-resume', 'backup', 'backups', 'restore-backup', 'audit', 'setup', *sorted(distribution_operations)])
    sk.add_argument("name", nargs="?",
                    help="Skill name or pending ID; ledger ID for rollback; mode name for mode")
    sk.add_argument("--dir", help="Project folder to scan (default: the current directory)")
    sk.add_argument("--dry-run", action="store_true", help="Preview a curator pass")
    sk.add_argument("--consolidate", action="store_true", default=None, help="Opt into model-assisted curator consolidation")
    sk.add_argument("--expected-digest", help="Unchanged physical Skill digest from generation-status")
    sk.add_argument("--source", default="all", help="Hub source filter")
    sk.add_argument("--category", default="", help="Hub install category or tap path")
    sk.add_argument("--force", action="store_true", help="Explicitly allow replacing local edits; does not bypass write policy")
    sk.add_argument("--restore", action="store_true", help="Restore original bundled or optional bytes")
    sk.add_argument("--repo", default="", help="GitHub publication owner/repo")
    sk.add_argument("--bundled-dir", help="Bundled catalog source directory")
    sk.add_argument("--optional-dir", help="Optional catalog source directory")
    sk.add_argument("--as", dest="role", default="sisters/10032",
                    help="Role whose skill stack to show")

    bu = sub.add_parser("bundles", help="List, inspect, save, or delete role-scoped Skill bundles")
    bu.add_argument("op", nargs="?", default="list", choices=["list", "show", "save", "delete"])
    bu.add_argument("name", nargs="?")
    bu.add_argument("skills", nargs="*", help="Ordered member Skill names (save)")
    bu.add_argument("--as", dest="role", default="sisters/10032", help="Role whose bundles to use")
    bu.add_argument("--dir", help="Project workspace (default: current directory)")
    bu.add_argument("--description", default="")
    bu.add_argument("--instruction", default="")
    bu.add_argument("--overwrite", action="store_true", help="Replace an existing role bundle")

    mo = sub.add_parser("moa", help="Configure, list, or delete Mixture-of-Agents presets")
    mo.add_argument("op", nargs="?", default="list", choices=["list", "configure", "delete"])
    mo.add_argument("name", nargs="?", help="Preset name (configure defaults to the default preset)")

    # Registered so `misaka --help` lists it, and so `misaka auth --help` is not an
    # "invalid choice" -- but it is never dispatched: `main` short-circuits `argv[0] ==
    # "auth"` before argparse runs, because auth has its own Pi-compatible grammar.
    ac = sub.add_parser("auth", help="Check or print provider credentials")
    ac.add_argument("auth_args", nargs=argparse.REMAINDER)
    for name, command in (extension_commands or {}).items():
        sub.add_parser(name, help=(command.__doc__ or "").strip().split("\n")[0]).add_argument(
            "args", nargs=argparse.REMAINDER)

    wb = sub.add_parser("web", help=f"Show or change web-search configuration ({home.display(home.path('settings'))}, keys in {home.display(home.path('env'))})")
    wb.add_argument("op", nargs="?", default=None, choices=["configure", "status", "set", "unset", "providers", "setup", "enable", "disable", "accounts", "login", "logout", "browser-status", "browser-providers", "browser-setup", "browser-install", "browser-connect", "browser-disconnect", "gateway-login", "gateway-logout", "gateway-status"],
                    help="No operation: interactive settings in a terminal, status otherwise")
    wb.add_argument("key", nargs="?", help="backend | search_backend | extract_backend | "
                                           "keyless_fallback | keyless_rescue | allow_private_urls | "
                                           "cache_enabled | cache_ttl_minutes | cache_exempt_hosts | "
                                           "extract_char_limit | env.<VAR> | provider_tier.<vendor> | "
                                           "website_blocklist.<enabled|domains|shared_files> | xai.<key>")
    wb.add_argument("value", nargs="?", help="The value to set (omit for unset)")
    wb.add_argument("--profile", metavar="DIR", help="Use the web section of DIR/settings.json over the shared Web defaults")
    wb.add_argument("--extension", action="append", default=[], metavar="PATH", help="Load an explicit session extension (repeatable)")
    wb.add_argument("--capability", choices=["search", "extract", "both"], help="Setup only these capabilities")
    wb.add_argument("--tier", choices=["auto", "free", "paid"], help="Setup tier from the provider's rows")
    wb.add_argument("--yes", action="store_true", help="Setup without prompts; keep existing credentials")
    wb.add_argument("--install", action="store_true", help="Explicitly install the selected provider's optional dependency")
    wb.add_argument("--login", action="store_true", help="Explicitly run the selected provider's OAuth login")


    dc = sub.add_parser("doc", help="Index documents, search them, show their structure, and verify quotes")
    dc.add_argument("action", choices=["add", "scan", "list", "find", "verify", "tree"])
    dc.add_argument("arg", nargs="?", help="File path (add), folder (scan; default: this folder), "
                                           "query (find), quote (verify), or document ID (tree)")
    dc.add_argument("--doc", help="Restrict to one document ID")
    dc.add_argument("--page", type=int, help="verify: the page the quotation is cited on")
    dc.add_argument("--no-tree", action="store_true", help="Skip PageIndex structure extraction")


    cr = sub.add_parser("create", help="Create a new Sister")
    cr.add_argument("sid", nargs="?", help="Sister ID, such as 10033")
    cr.add_argument("--desc", help="Specialty for Last Order's task routing, written to DESCRIBE.md")
    cr.add_argument("--model", help="Pinned model as provider/model, such as anthropic/claude-opus-5")
    cr.add_argument("--thinking", help="Her default thinking level: off, minimal, low, medium, high, xhigh or max")
    rm = sub.add_parser("remove", help="Remove a Sister's profile; her cards, conversations and workspaces are kept")
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


def _cmd_chat(args):
    from misaka.cli import chat
    return chat.launch(args.as_agent, model=args.model, cont=args.cont, pick=args.pick,
                       session=args.session, read_only=args.read_only, catalog=args.catalog, attach=args.attach,
                       **({"skills": args.skills} if args.skills else {}))


def _cmd_panel(args):
    from misaka.ui.panel import panel
    try:
        panel.launch()
    except RuntimeError as error:
        # The panel is the last step of a first run, straight after the wizard's summary.
        # A daemon that will not start already explains itself (and names its log); letting
        # the exception out replaces that explanation, and the summary above it, with a
        # traceback whose first readable line is somewhere on page two.
        print(f"The panel could not start: {error}", file=sys.stderr)
        print("`misaka chat` opens plain chat with Last Order in the meantime.", file=sys.stderr)
        sys.exit(1)


def _cmd_net_daemon(args):
    from misaka.ui.panel import daemon
    daemon.main()


def _cmd_card_shell(args):
    from misaka.cli import card_shell
    card_shell.launch(args.task_id, resume_only=args.resume, say=args.say)


def _cmd_ally_card(args):
    from misaka.core.network.ally import card
    card.launch(args.task_id, say=args.say)


def _cmd_ally_bridge(args):
    from misaka.core.network.ally import bridge
    bridge.main()


def _cmd_allies(args):
    from misaka.core.network.ally import presets
    enabled = presets.configured()
    if not enabled:
        print(f"No ally is enabled. Add one to `allies` in settings.json, e.g. "
              f"{{\"{'\": {}, \"'.join(presets.PRESETS)}\": {{}}}}.")
        return 0
    for name, ally in enabled.items():
        print(f"{name}  {presets.check(ally)}\n    {' '.join(ally.command)}")
    return 0


def _cmd_net(args):
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


def _cmd_init(args):
    from misaka.core.platform import cards
    try:
        lines = cards.init_project(os.getcwd())
    except RuntimeError as err:
        # Missing or failing git: a prerequisite the user has to install, not a bug.
        sys.exit(str(err))
    for line in lines:
        print(line)


def _cmd_create(args):
    from misaka.core.network import roster
    sys.exit(roster.cli_create(args.sid, desc=args.desc, model=args.model, thinking=args.thinking))


def _cmd_remove(args):
    from misaka.core.network import roster
    sys.exit(roster.cli_remove(args.sid, yes=args.yes))


def _cmd_dm(args):
    from misaka.cli import dm as dm_cli
    sys.exit(dm_cli.deliver(args.to, args.message, sender=args.sender,
                            model=args.model, timeout=args.timeout,
                            task_id=args.dm_task, generation=args.dm_gen,
                            summary=args.summary, wait_message=args.wait_message))


def _cmd_task(args):
    from misaka.core.platform import cards as card_files
    con = db.connect(CFG["db"])
    op, rest = args.words[0], args.words[1:]
    if op == "add":
        sys.exit(_task_add(con, " ".join(rest), args))
    if op == "start" and len(rest) == 1:
        sys.exit(_task_start(con, rest[0]))
    if not args.delete or rest:
        sys.exit("usage: misaka task add TITLE --to SISTER (--body TEXT | --body-file PATH) | "
                 "misaka task start ID | misaka task ID --delete")
    row = db.get(con, op)
    ok, msg = (card_files.remove(con, row["workspace"], op) if row
               else (False, f"Card not found: {op}"))
    print(msg)
    sys.exit(0 if ok else 1)


def _task_add(con, title, args):
    """A card made by hand: the front door, checks and defaults of Last Order's ``misaka_card``."""
    from misaka.core.network import roster, validate
    from misaka.core.platform import cards as card_files
    body = args.body
    if args.body_file:
        try:
            with open(args.body_file, encoding="utf-8-sig") as f:   # Notepad's BOM is not the body's
                body = f.read()
        except OSError as error:
            return f"Cannot read {args.body_file}: {error}"
        except UnicodeDecodeError:
            return f"{args.body_file} is not UTF-8 text; save it as UTF-8 (Notepad: Save as, Encoding: UTF-8)."
    cards, errors = validate.validate_cards(
        [{"title": title, "body": body, "assignee": args.to, "priority": args.priority, "model": args.model}],
        roster.executors())
    if args.reviewer and args.reviewer not in set(roster.roster_names()):
        errors.append(f"reviewer {args.reviewer} is not in the Sister roster")
    if args.reviewer and args.reviewer == args.to:
        errors.append("the reviewer must be different from the assignee")
    if errors:
        return ("Card not added: " + "; ".join(errors) +
                "\nThe body is the task contract: `## goal`, `## boundaries` and `## acceptance criteria`.")
    card = cards[0]
    try:
        tid = card_files.create(con, db.canonical_workspace(), card["title"], card["body"], card["assignee"],
                                model=card["model"], priority=card["priority"], reviewer=args.reviewer,
                                needs=args.needs)
    except ValueError as error:
        return f"Card not added: {error}"
    print(f"Added {tid}: {card['title']} → {card['assignee']}. Work has not started: "
          f"misaka task start {tid}, or ask Last Order to dispatch it.")
    return 0


def _task_start(con, task_id):
    """Run one ready card in this terminal: the claim, admission, budget and settling a pane or
    Last Order's dispatch would give it (``dispatch.run_task``)."""
    from misaka.core.network import dispatch
    from misaka.core.research import runs
    row = db.get(con, task_id)
    if row is None:
        return f"Card not found: {task_id}"
    context = runs.task_contexts(con).get(task_id)
    if context:
        return (f"Card {task_id} belongs to research run {context['run_id']}, whose driver starts it; "
                f"/research resume {context['run_id']} continues a paused run.")
    if row["status"] != "ready":
        return f"Card {task_id} is {row['status']}; only a ready card can be started."
    from misaka.utils import loop_watchdog
    loop_watchdog.configure(f"task-{task_id}", exit_on_stall=False)
    print(f"Running {task_id} → {row['assignee']} in this terminal (Ctrl+C stops it)…", flush=True)
    ran = dispatch.run_task(con, row, current_config())
    after = db.get(con, task_id)
    print(f"Card {task_id}: {after['status'] if after else 'gone'}.")
    return 0 if ran else 1


def _cmd_board(args):
    tail.board_view(db.connect(CFG["db"]), db.canonical_workspace())


def _cmd_usage(args):
    from misaka.core.research import runs, usage
    cfg = current_config()
    con = db.connect(cfg["db"])
    runs.init(con)
    if args.run:
        run = runs.get(con, args.run)
        if not run:
            sys.exit(f"Research run not found: {args.run}")
        scope = {run["id"], *(task["id"] for task in runs.tasks(con, run["id"]))}
        print(usage.report(con, run, ledger_tokens=budget.spent(con, task_ids=scope)))
        return
    recent = con.execute("SELECT * FROM research_runs ORDER BY created_at DESC LIMIT ?", (max(1, args.limit),)).fetchall()
    for run in recent:
        spent = usage.run_usage(con, run)["total"]
        print(f"{run['id']}  {time.strftime('%Y-%m-%d', time.localtime(run['created_at']))}  {run['status']:<8} "
              f"{spent['tokens']:>13,} tokens  {usage.money(spent):<10}  {' '.join(run['question'].split())[:50]}")
    if not recent:
        print("No research runs yet.")
    cap = int(cfg.get("token_cap") or 0)
    print(f"Board token ledger: {budget.spent(con):,} tokens recorded"
          + (f"; research.token_cap is {cap:,} for each research run" if cap else "")
          + ". One run in detail: misaka usage --run RUN_ID")


def _cmd_research(args):
    import asyncio as _asyncio

    from misaka.core.research import node as research_node
    from misaka.core.research import planner, runs, workflow
    if args.node:
        sys.exit(research_node.main(*args.node, runner_key=args.runner_key))
    cfg = current_config()
    # Preflight before anything is written: a run with no Sister to assign to dies deep
    # inside the workflow (planner._roster), after the project skeleton has been written, and
    # the message that surfaces there names neither the roster nor the command that fills it.
    roster = {sister["id"] for sister in planner.sister_catalog(cfg.get("profiles_root"))}
    empty_roster = ("The Sister roster is empty; research tasks cannot be assigned.\n"
                    "Create at least one Sister first, for example: misaka create 10032")
    con = db.connect(cfg["db"])
    runs.init(con)
    def chosen_limits():
        chosen = {"max_depth": args.depth, "parallel": args.parallel, "sister_parallel": args.sister_parallel,
                  "max_followups": args.followups, "max_revisions": args.revisions, "max_nodes": args.max_nodes}
        return {key: value for key, value in chosen.items() if value is not None}

    if args.tell:
        from misaka.core.research import window
        run = runs.get(con, args.tell)
        if not run:
            sys.exit(f"Research run not found: {args.tell}")
        try:
            path = _asyncio.run(window.tell(con, run, args.goal, node_id=args.to))
        except ValueError as error:
            sys.exit(str(error))
        print(f"Told the Last Order of {args.to or 'the root'} ({run['id']}). Her answer is written to her "
              f"conversation; follow it with: misaka chat --attach --session {path}")
        return
    if args.limits:
        chosen = chosen_limits()
        if not chosen:
            sys.exit("Name the limits to change, for example: misaka research --limits RUN_ID --sister-parallel 2")
        try:
            before, after = runs.update_limits(con, args.limits, chosen)
        except ValueError as error:
            sys.exit(str(error))
        print(f"Research run {args.limits}: {runs.limits_changed(before, after)}")
        return
    if args.resume:
        run = runs.get(con, args.resume)
        if not run:
            sys.exit(f"Research run not found: {args.resume}")
        # A resumed run keeps its cards, so what it needs is the Sisters those cards name,
        # not any Sister: `misaka create 10032` would satisfy an empty-roster check and still
        # leave every card assigned to somebody who no longer exists.
        assigned = sorted({row["assignee"] for row in runs.tasks(con, run["id"]) if row["assignee"]})
        missing = [sid for sid in assigned if sid not in roster]
        if missing:
            sys.exit(f"Research run {run['id']} is assigned to Sisters {', '.join(assigned)}; "
                     f"not in the roster now: {', '.join(missing)}.\n"
                     f"Recreate them first, for example: misaka create {missing[0]}")
        if not assigned and not roster:
            sys.exit(empty_roster)
        if run["status"] == "done":
            sys.exit(f"Research run {run['id']} is done; start a new run.")
    else:
        if not roster:
            sys.exit(empty_roster)
        if not args.goal:
            sys.exit("A new research run requires a question; use --resume RUN_ID to continue one.")
        try:
            limits = runs.normalize_limits(chosen_limits())
        except ValueError as error:
            sys.exit(str(error))
        from misaka.core.platform import cards as card_files
        card_files.init_project(os.getcwd(), draft_brief=False, git=False)
        run = runs.create(con, workspace=os.getcwd(), question=args.goal,
                          limits=limits,
                          token_start=budget.spent(con))
        print(f"Research run {run['id']}: {run['workspace']}")
    def progress(event):
        print(event["message"], flush=True)
        for item in event.get("tasks") or []:
            print(f"  - {item['title']} → Sister {item['assignee']}", flush=True)

    try:
        from misaka.utils import loop_watchdog
        loop_watchdog.configure("research", exit_on_stall=False)
        out = _asyncio.run(loop_watchdog.watched(workflow.run(
            con, cfg, research_node.ProcessSpawner(show_output=True),   # nodes print beside this output, in a pane's shell too
            run_id=run["id"], poll_seconds=1.0, resume=bool(args.resume), progress=progress)))
    except RuntimeError as err:
        # Node failures already print their own reason above; the workflow's own summary is
        # the useful part, and a traceback of the event loop is not.
        sys.exit(f"Research run {run['id']} stopped: {err}")
    print(f"Research run {run['id']}: {out['reason']}")
    for question in out.get("questions") or []:
        print(f"  - {question}")
    final = out.get("final") or {}
    if final.get("path"):
        print(final["path"])


def _cmd_web(args):
    from misaka.cli.web import run

    return run(args)


def _cmd_moa(args):
    from misaka.core.moa.provider import (
        load_moa_config,
        raw_moa_config,
        save_moa_config,
    )

    cfg = load_moa_config()
    if args.op == "configure":
        from misaka.core.moa import configure as moa_configure
        try:
            moa_configure.configure(args.name)
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nNothing was written.")
        return
    if args.op == "delete":
        if not args.name:
            sys.exit("Usage: misaka moa delete <preset>")
        raw = raw_moa_config()
        presets = raw.get("presets") if isinstance(raw.get("presets"), dict) else {}
        if args.name not in presets:
            # `moa list` prints load_moa_config(), which invents one preset when none is
            # written; delete can only touch presets the settings actually declare.
            if args.name in cfg["presets"]:
                sys.exit(f'"{args.name}" is the built-in MoA preset, not one your settings define, '
                         f"so there is nothing to delete. Write your own presets under \"moa\" in "
                         f"{home.display(home.path('settings'))} to replace it.")
            known = ", ".join(cfg["presets"]) or "none"
            sys.exit(f'Unknown preset "{args.name}". Available presets: {known}.')
        if len(presets) <= 1:
            sys.exit("Cannot delete the last remaining MoA preset.")
        del presets[args.name]
        if raw.get("default_preset") == args.name:
            raw["default_preset"] = next(iter(presets))
        save_moa_config(raw)
        print(f"Deleted preset '{args.name}'; default: {raw.get('default_preset')}")
    else:
        from misaka.core.moa import configure as moa_configure
        moa_configure.describe(cfg, print)


def _cmd_skills(args):
    import os as _os

    from misaka.core.skills import layers as skill_layers
    from misaka.core.skills.distribution import OPERATIONS as distribution_operations
    if args.op in distribution_operations or args.op in ('usage', 'adopt', 'pin', 'unpin', 'archive', 'restore', 'sync-on', 'sync-off', 'curator-status', 'curator-run', 'curator-pause', 'curator-resume', 'backup', 'backups', 'restore-backup', 'audit', 'setup'):
        import json
        from pathlib import Path

        from misaka.core.skills.operations import execute
        role = Path(args.role)
        if role.is_absolute() or ".." in role.parts:
            sys.exit("Role must be relative to roles_root.")
        root = Path(_os.path.expanduser(CFG["roles_root"]))
        profile = root / role
        if not profile.resolve().is_relative_to(root.resolve()):
            sys.exit("Role resolves outside roles_root.")
        options = ({"dry_run": args.dry_run, "consolidate": args.consolidate} if args.op == "curator-run" else {})
        if args.op in distribution_operations:
            options = {"source": args.source, "category": args.category, "force": args.force,
                       "restore": args.restore, "repo": args.repo, "dry_run": args.dry_run,
                       "bundled_root": args.bundled_dir, "optional_root": args.optional_dir, "expected_digest": args.expected_digest}
        result = execute(args.op, args.name, profile_dir=profile, workspace=args.dir or _os.getcwd(), **options)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if not result.get("success"):
            sys.exit(1)
        return
    if args.op == "mode":
        from misaka.core.skills import write as skill_write
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
        elif skill_write.agent_session():
            sys.exit("Marked agent sessions do not change skill write mode; this is a workflow guard, not an OS sandbox.")
        else:
            cfg = skill_layers.load_skills_config()
            cfg["skill_write_mode"] = args.name
            skill_layers.write_skills_config(cfg)
            print(f"Skill write mode set to {args.name} (effective immediately).")
    elif args.op in ("pending", "approve", "reject", "ledger", "rollback"):
        from misaka.core.skills import write as skill_write
        if args.op in ("approve", "reject", "rollback") and skill_write.agent_session():
            sys.exit("Marked agent sessions do not make skill review decisions; this is a workflow guard, not an OS sandbox.")

        if args.op == "pending":
            print(f"Skill write mode: {skill_write.write_mode()}")
            records = skill_write.list_pending()
            if not records:
                print("No skills are awaiting review.")
            from misaka.core.skills.linter import format_findings, lint_content
            for r in records:
                payload = r.get("payload") or {}
                pending_id = r.get("_pending_file_id") or r.get("id") or "invalid"
                print(f"  [{pending_id}] {r.get('summary') or '(no summary)'} "
                      f"(submitted by {r.get('origin') or 'unknown'})")
                print(f"    Payload SHA-256: {r.get('payload_sha256') or '(missing)'}")
                if r.get("_integrity_error"):
                    print(f"    INVALID: {r['_integrity_error']}")
                    print(f"    Reject: misaka skills reject {pending_id}")
                    continue
                if payload.get("content"):
                    # Advisory lint findings, shown before approval.
                    print(format_findings(lint_content(payload["content"])))
                diff = skill_write.pending_diff(r)
                if diff:
                    print("    " + diff.replace("\n", "\n    "))
                print(f"    Approve: misaka skills approve {r['id']}")
        elif args.op == "approve":
            if not args.name:
                sys.exit("Usage: misaka skills approve <pending-id>")
            from misaka.core.skills import manage as skill_manage
            record = skill_write.get_pending(args.name)
            if record is not None:
                # Re-run the reviewed request through the normal validation path.
                result = skill_manage.apply_pending(record)
                if not result.get("success"):
                    sys.exit(f"Approval failed: {result.get('error')}")
                skill_write.discard_pending(record.get("_pending_file_id") or record["id"])
                print("Approved and applied.")
            else:
                sys.exit(f"No pending write with ID '{args.name}'.")
        elif args.op == "reject":
            if not args.name:
                sys.exit("Usage: misaka skills reject <pending-id-or-skill-name>")
            record = skill_write.get_pending(args.name) or next(
                (r for r in skill_write.list_pending()
                 if (r.get("payload") or {}).get("name") == args.name), None)
            if record is not None:
                skill_write.discard_pending(record.get("_pending_file_id") or record["id"])
                skill_write.record("reject", (record.get("payload") or {}).get("name", ""),
                                   evidence={"pending_id": record["id"]})
                print(f"Rejected: {record['summary']}")
            else:
                sys.exit(f"No pending write named '{args.name}'.")
        elif args.op == "ledger":
            rows = skill_write.entries(limit=30)
            if not rows:
                print("The skill ledger is empty.")
            for e in rows:
                rollbackable = e.get("rollbackable", e["action"] in skill_write.MUTATING_ACTIONS)
                print(
                    f"{e['ts']}  {e['id']}  {e['actor']:<12} {e['action']:<12} "
                    f"{e['skill']}  (before {len(e['before'])}/after {len(e['after'])})"
                    + ("" if rollbackable else "  [not rollbackable]")
                )
        else:   # rollback
            if not args.name:
                sys.exit("Usage: misaka skills rollback <ledger-id>")
            target = next((e for e in skill_write.entries() if e["id"] == args.name), None)
            if target is None:
                sys.exit(f"Skill ledger entry not found: {args.name}")
            ok, why = skill_write.rollback(args.name)
            print(why)
            sys.exit(0 if ok else 1)
    elif args.op == "scan":
        from misaka.core.skills.guard import format_scan_report, scan_skill
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
        from misaka.core.skills import index as skill_index
        prof = _os.path.join(_os.path.expanduser(CFG["roles_root"]), args.role)
        for e in skill_index.build(skill_layers.skill_roots(prof, cwd=_os.getcwd())):
            print(f"{e['layer']:<9}{e['category']}/{e['name']}  {e['description']}  ({e['dir']})")


def _cmd_bundles(args):
    import json
    from pathlib import Path

    from misaka.core.skills import bundles
    from misaka.core.skills.vendor.commands import slugify_skill_name
    roles = Path(CFG["roles_root"]).expanduser().resolve()
    profile = roles / args.role
    if profile.resolve() == roles or not profile.resolve().is_relative_to(roles):
        sys.exit("Role must be a profile under roles_root.")
    try:
        if args.op in ("save", "delete"):
            if not args.name:
                sys.exit(f"Usage: misaka bundles {args.op} <name>")
            path = (bundles.delete(args.name, profile_dir=profile) if args.op == "delete" else
                    bundles.save(args.name, args.skills, profile_dir=profile, description=args.description,
                                 instruction=args.instruction, overwrite=args.overwrite))
            print(path)
            return
        found = bundles.scan(bundles.bundle_roots(profile, args.dir or os.getcwd()))
        if args.op == "show":
            info = found.get("/" + slugify_skill_name(args.name or ""))
            if info is None:
                sys.exit(f"Bundle not found: {args.name or ''}")
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            for key, info in sorted(found.items()):
                print(f"{key}  [{info['layer']}]  {', '.join(info['skills'])}  ({info['path']})")
    except (OSError, ValueError) as error:
        sys.exit(str(error))


def _doc_notes(did, since):
    """What indexing ``did`` left unread, and what it re-read at or after ``since``."""
    ddir = corpus.resolve_doc(did, workspace=db.canonical_workspace())
    meta = (corpus._read_meta_at(ddir) if ddir else None) or {}
    notes = []
    listed = meta.get("unread_pages") if isinstance(meta.get("unread_pages"), dict) else {}
    unread = {n: why for n, why in listed.items() if not str(why).endswith(corpus.OCR_FOUND_NOTHING)}
    if unread:
        reasons = sorted(set(unread.values()))
        notes.append(f"{len(unread)} page(s) need OCR and were not read: {reasons[0]}"
                     + (f" (and {len(reasons) - 1} other reason(s))" if len(reasons) > 1 else ""))
    if textless := len(listed) - len(unread):
        notes.append(f"{textless} page(s) hold no text OCR could read (blank pages, pictures, maps)")
    reread = meta.get("reread")
    if isinstance(reread, dict) and int(reread.get("at") or 0) >= since:
        notes.append(f"re-read by the current extractor: {len(reread.get('pages') or [])} page(s) changed "
                     f"({reread.get('pages_before')} -> {meta.get('pages')} pages)")
    return notes


def _cmd_doc(args):
    if args.action == "add":
        if not args.arg:
            sys.exit("Usage: misaka doc add <file> [--no-tree]")
        since = int(time.time())
        did, n = corpus.ingest(args.arg, with_tree=not args.no_tree, workspace=db.canonical_workspace())
        doc = corpus.resolve_doc(did, workspace=db.canonical_workspace())
        has = doc and os.path.exists(os.path.join(doc, "tree.json"))
        structure = "with PageIndex structure" if has else "page navigation only"
        print(f"Added {os.path.basename(args.arg)} as {did}: {n} pages, {structure}.")
        for note in _doc_notes(did, since):
            print(f"  {note}")
    elif args.action == "scan":
        target = args.arg or os.getcwd()
        since = int(time.time())
        ingested, skipped = corpus.scan(target, with_tree=not args.no_tree, workspace=db.canonical_workspace())
        for did, path in ingested:
            print(f"  {did}  {path}")
            for note in _doc_notes(did, since):
                print(f"      {note}")
        for path, reason in skipped:
            print(f"  skipped {path}: {reason}")
        if not ingested:
            # A scan that indexed nothing failed at its one job, whatever the reason: a
            # script trusting exit 0 would go on to search a corpus this never filled.
            if skipped:
                sys.exit(f"Indexed 0 file(s); all {len(skipped)} candidate(s) were skipped.")
            sys.exit(f"Nothing to index under {target}: no files with a readable suffix "
                     f"({', '.join(sorted(corpus.SCAN_SUFFIXES))}).")
        print(f"Indexed {len(ingested)} file(s); skipped {len(skipped)}.")
    elif args.action == "list":
        for d_ in corpus.docs(workspace=db.canonical_workspace()):
            print(f"  {d_['doc_id']}  {d_['pages']:>4} pages  {d_['title']}")
    elif args.action == "find":
        hits = corpus.search_literal(args.arg, doc_id=args.doc, workspace=db.canonical_workspace())
        for h in hits:
            printed = f" (printed p. {h['printed']})" if h.get("printed") else ""
            print(f"  {h['doc_id']} p{h['page']}{printed}  {h['s'][:90]}")
        print(f"{len(hits)} match(es). Use `misaka doc verify` before citing a quotation.")
    elif args.action == "verify":
        if not args.arg or not args.doc:
            sys.exit("Usage: misaka doc verify <quote> --doc <doc-id>")
        found = corpus.locate_quote(args.doc, args.arg, args.page, workspace=db.canonical_workspace())
        if found["status"] in ("not_found", "no_document"):
            sys.exit("❌ Quote not found in that document.")
        v = found
        if found["status"] == "elsewhere":
            print(f"⚠ Not on page {args.page}: it is on page {v['page']}.")
        printed = f" (printed p. {v['printed']}, from {v['printed_from']})" if v.get("printed") else ""
        print(f"✅ p{v['page']}{printed} offset {v['offset']}\n   claim_hash {v['claim_hash']}")
    elif args.action == "tree":
        if not (args.arg or args.doc):
            sys.exit("Usage: misaka doc tree <doc-id>")
        st = corpus.structure(args.arg or args.doc, workspace=db.canonical_workspace())
        if not st:
            sys.exit("Document not found.")
        print(f"{st['title']} ({st['mode']})")
        for line in _doc_tree_lines(st.get("tree")):
            print(line)
        for pg in (st.get("pages") or [])[:40]:
            print(f"  p{pg['page']:<4} {pg['head']}")

def _cmd_setup(args):
    from misaka.cli import setup
    sys.exit(setup.run(args.section))


def _cmd_update(args):
    from misaka.cli import update
    sys.exit(update.run(apply=args.apply))


def _cmd_uninstall(args):
    from misaka.cli import uninstall
    sys.exit(uninstall.run(mode=args.mode, assume_yes=args.yes, dry_run=args.dry_run))


def _cmd_gui(args):
    from misaka.ui.gui.server import serve
    return serve(port=args.port, open_browser=not args.no_browser)


COMMANDS = {
    "gui": _cmd_gui,
    "setup": _cmd_setup,
    "update": _cmd_update,
    "uninstall": _cmd_uninstall,
    "chat": _cmd_chat,
    "panel": _cmd_panel,
    "net-daemon": _cmd_net_daemon,
    "card-shell": _cmd_card_shell,
    "ally-card": _cmd_ally_card,
    "ally-bridge": _cmd_ally_bridge,
    "allies": _cmd_allies,
    "net": _cmd_net,
    "init": _cmd_init,
    "create": _cmd_create,
    "remove": _cmd_remove,
    "dm": _cmd_dm,
    "task": _cmd_task,
    "board": _cmd_board,
    "research": _cmd_research,
    "usage": _cmd_usage,
    "web": _cmd_web,
    "moa": _cmd_moa,
    "skills": _cmd_skills,
    "bundles": _cmd_bundles,
    "doc": _cmd_doc,
}


def main(argv=None):
    """The CLI entry point: one handler per sub-command (``COMMANDS``), each opening the board only
    if it uses it; ``argv`` defaults to the process arguments so tests can drive it directly."""
    argv = list(sys.argv[1:] if argv is None else argv)
    # setup, update and uninstall must still run on a settings.json that stops everything else.
    bootstrap.install(check_settings=argv[:1] not in (["setup"], ["update"], ["uninstall"]))
    from misaka.config import env as env_file
    from misaka.config.engine import configure_logging
    home.ensure()
    configure_logging()      # warnings go to the log file, never to a TUI's screen (B6)
    env_file.load()          # the home's .env: environment for code that is not MISAKA
    if argv and argv[0] not in COMMANDS and argv[0] != "auth" and not argv[0].startswith("-"):
        # A command a bundled extension offers runs here, before the project layout exists: it
        # decides for itself what it creates (a dry run or read-only operator creates nothing).
        from misaka import extensions
        extension_command = extensions.commands().get(argv[0])
        if extension_command is not None:
            return extension_command(argv[1:])
    # Attaching/reading uses the existing owner's configuration. Other commands,
    # including first-run help/version, retain the normal CLI bootstrap contract.
    if not (argv[:1] == ["chat"] and ({"--read-only", "--attach"} & set(argv))):
        layout.ensure()
    if not argv:
        # No arguments: open the panel in a terminal, plain chat when piped.
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if interactive:
            from misaka.cli import setup
            if not setup.configured_anywhere():
                # A first run: no credential for the default provider anywhere. The wizard
                # is the door, and the panel opens once it is done.
                print("No provider credential is configured yet; starting `misaka setup`.")
                if setup.run() != 0:
                    return 1
            from misaka.ui.panel import ghostty
            if reason := ghostty.unavailable():
                # The panel's terminal emulator is a prebuilt library; a platform without a
                # build it can load (a musl system, an unusual architecture) still gets plain chat.
                # The reason names the file and the loader's error: the chat redraws over this line
                # at once, so it is often read from a screenshot of the scrollback.
                print("The panel's terminal library (libghostty-vt) is not available on this system; "
                      f"opening plain chat. See `misaka setup environment`.\n  {reason}", file=sys.stderr)
                interactive = False
        argv = ["panel"] if interactive else ["chat"]
    if argv[0] == "auth":
        # Auth has its own Pi-compatible grammar; preserve the raw option order instead
        # of sending it through the product CLI's unrelated parser.
        from misaka.cli.auth import run_auth_command

        return run_auth_command(argv[1:])
    if argv[0] in ("-h", "--help"):
        from misaka import extensions
        _parser(extensions.commands()).parse_args(argv)
    args = _parser().parse_args(argv)
    from misaka.core.skills.layers import SkillsConfigError

    try:
        return COMMANDS[args.cmd](args)
    except SkillsConfigError as error:
        # A settings.json the user broke by hand is their file to fix, not a traceback.
        print(f"misaka: {error}", file=sys.stderr)
        return 1

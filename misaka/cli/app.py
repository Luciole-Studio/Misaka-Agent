"""MISAKA command-line entry point."""
import argparse
import os
import sys

from misaka.cli import bootstrap
from misaka.config import CFG, VERSION, current_config
from misaka.core.documents import index as corpus
from misaka.core.network import board as tail
from misaka.core.platform import budget
from misaka.core.platform import tasks as db
from misaka.utils import atomic


class _ExactArgumentParser(argparse.ArgumentParser):
    """Require documented option names instead of accepting ambiguous prefixes."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)


def _parser():
    p = _ExactArgumentParser(prog="misaka")
    # The first thing anyone types after installing. Without it argparse answers the version
    # question with a usage error and exit 2, which reads as "this install is broken".
    p.add_argument("--version", "-V", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

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
    dmp.add_argument("message", nargs="?", help="Message body (omitted: deliver what is already queued)")
    dmp.add_argument("--from", dest="sender", help="Sender role (default: the user)")
    dmp.add_argument("--model", help="Override the recipient's model")
    dmp.add_argument("--timeout", type=int, default=600, help="Seconds to wait for a reply")
    dmp.add_argument("--summary", help="Short audit-log summary")
    dmp.add_argument("--task-id", dest="dm_task", help=argparse.SUPPRESS)
    dmp.add_argument("--generation", dest="dm_gen", type=int, help=argparse.SUPPRESS)

    ini = sub.add_parser("init", help="Make this folder a MISAKA project (git repo + PROJECT.md + cards/) and initialize the database")
    ini.add_argument("--migrate", action="store_true",
                     help="One-shot: write card files for every existing board row (all projects) and run the "
                          "agent-directory migrations (credentials → auth.json; sessions/tools/commands/keybindings layout)")

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


    lc = sub.add_parser("lcm", help="Inspect, back up, and maintain the LCM context database")
    lc.add_argument("op", nargs="?", default="status",
                    choices=["status", "doctor", "backup", "embed",
                             "rollups", "externalize-backfill", "assertions", "rotate", "preset"])
    lc.add_argument("target", nargs="?",
                    help="warmup|backfill for embed; rebuild for assertions; "
                         "session id for rotate; show|suggest|apply for preset")
    lc.add_argument("name", nargs="?",
                    help="preset: which preset show or apply reports on (show defaults to the active one)")
    lc.add_argument("--apply", action="store_true",
                    help="embed/externalize backfill/assertions/rotate: do it for real "
                         "(the default only prints the plan); preset has nothing to commit and "
                         "says so")
    lc.add_argument("--limit", type=int,
                    help="embed/externalize backfill: how many rows to move in this run; "
                         "assertions: how many source rows one --apply pass may re-derive, "
                         "one auxiliary model call each (upstream default 100, maximum 500)")
    lc.add_argument("--rebuild", action="store_true",
                    help="rollups: re-seed and rebuild every temporal rollup (calls the summariser)")

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

    # Registered so `misaka --help` lists it, and so `misaka auth --help` is not an
    # "invalid choice" -- but it is never dispatched: `main` short-circuits `argv[0] ==
    # "auth"` before argparse runs, because auth has its own Pi-compatible grammar.
    ac = sub.add_parser("auth", help="Check or print provider credentials")
    ac.add_argument("auth_args", nargs=argparse.REMAINDER)

    wb = sub.add_parser("web", help="Show or change web-search configuration (~/.misaka/web.json)")
    wb.add_argument("op", nargs="?", default="status", choices=["status", "set", "unset"])
    wb.add_argument("key", nargs="?", help="backend | search_backend | extract_backend | "
                                           "keyless_fallback | keyless_rescue | allow_private_urls | "
                                           "cache_enabled | cache_ttl_minutes | cache_exempt_hosts | "
                                           "extract_char_limit | env.<VAR> | provider_tier.<vendor> | "
                                           "website_blocklist.<enabled|domains|shared_files> | xai.<key>")
    wb.add_argument("value", nargs="?", help="The value to set (omit for unset)")


    dc = sub.add_parser("doc", help="Index documents, search them, show their structure, and verify quotes")
    dc.add_argument("action", choices=["add", "scan", "list", "find", "verify", "tree"])
    dc.add_argument("arg", nargs="?", help="File path (add), folder (scan; default: this folder), "
                                           "query (find), quote (verify), or document ID (tree)")
    dc.add_argument("--doc", help="Restrict to one document ID")
    dc.add_argument("--no-tree", action="store_true", help="Skip PageIndex structure extraction")


    cr = sub.add_parser("create", help="Create a new Sister")
    cr.add_argument("sid", nargs="?", help="Sister ID, such as 10033")
    cr.add_argument("--desc", help="Personality or specialty, written to SOUL.md")
    cr.add_argument("--model", help="Pinned model, such as claude-opus-4-5")
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


def _cmd_chat(args):
    from misaka.cli import chat
    chat.launch(args.as_agent, model=args.model, cont=args.cont, pick=args.pick,
                session=args.session)


def _cmd_panel(args):
    from misaka.ui.panel import panel
    panel.launch()


def _cmd_net_daemon(args):
    from misaka.ui.panel import daemon
    daemon.main()


def _cmd_card_shell(args):
    from misaka.cli import card_shell
    card_shell.launch(args.task_id, resume_only=args.resume, say=args.say)


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
    if args.migrate:
        written, existed, no_folder = cards.migrate(db.connect(CFG["db"]))
        print(f"migrated {written} card(s) to files ({existed} already had files, "
              f"{no_folder} skipped: project folder gone)")
        from misaka.config import migrations
        result = migrations.run_migrations(os.getcwd())
        if result["migratedAuthProviders"]:
            print("migrated credentials to auth.json: " + ", ".join(result["migratedAuthProviders"]))
        if result["movedSessions"]:
            print(f"moved {result['movedSessions']} session file(s) to per-folder buckets")
        for warning in result["deprecationWarnings"]:
            print(f"warning: {warning}")
    else:
        try:
            lines = cards.init_project(os.getcwd())
        except RuntimeError as err:
            # Missing or failing git: a prerequisite the user has to install, not a bug.
            sys.exit(str(err))
        for line in lines:
            print(line)
    print("board:", os.path.expanduser(CFG["db"]))


def _cmd_create(args):
    from misaka.core.network import roster
    sys.exit(roster.cli_create(args.sid, desc=args.desc, model=args.model))


def _cmd_remove(args):
    from misaka.core.network import roster
    sys.exit(roster.cli_remove(args.sid, yes=args.yes))


def _cmd_tell(args):
    from misaka.core.network.ally import tell as ally_tell
    ok, msg = ally_tell.tell(args.message, to_addr=args.to, summary=args.summary)
    print(msg)
    sys.exit(0 if ok else 1)


def _cmd_dm(args):
    from misaka.cli import dm as dm_cli
    sys.exit(dm_cli.deliver(args.to, args.message, sender=args.sender,
                            model=args.model, timeout=args.timeout,
                            task_id=args.dm_task, generation=args.dm_gen,
                            summary=args.summary))


def _cmd_task(args):
    from misaka.core.platform import cards as card_files
    con = db.connect(CFG["db"])
    row = db.get(con, args.task_id)
    ok, msg = (card_files.remove(con, row["workspace"], args.task_id) if row
               else (False, f"Card not found: {args.task_id}"))
    print(msg)
    sys.exit(0 if ok else 1)


def _cmd_board(args):
    tail.board_view(db.connect(CFG["db"]), db.canonical_workspace())


def _cmd_research(args):
    import asyncio as _asyncio

    from misaka.core.network import worker as worker_mod
    from misaka.core.research import node as research_node
    from misaka.core.research import planner, runs, workflow
    if args.node:
        sys.exit(research_node.main(*args.node))
    if args.probe:
        sys.exit(research_node.main_probe(*args.probe))
    cfg = current_config()
    # Preflight before anything is written: a run with no Sister to assign to dies deep
    # inside the workflow (planner._roster), after the workspace has been git-initialised
    # and committed into, and the message that surfaces there names neither the roster nor
    # the command that fills it.
    roster = {sister["id"] for sister in planner.sister_catalog(cfg.get("profiles_root"))}
    empty_roster = ("The Sister roster is empty; research tasks cannot be assigned.\n"
                    "Create at least one Sister first, for example: misaka create 10032")
    con = db.connect(cfg["db"])
    runs.init(con)
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
        try:
            runs.resume(con, run["id"])
        except ValueError as err:      # a finished run refuses in its own words
            sys.exit(str(err))
    else:
        if not roster:
            sys.exit(empty_roster)
        if not args.goal:
            sys.exit("A new research run requires a question; use --resume RUN_ID to continue one.")
        try:
            brief = planner.ensure_project_brief(cfg, worker_mod, args.goal, os.getcwd())
        except RuntimeError as err:
            # The intake step needs a model; its own text says which credential is missing
            # and where it is kept, so print that rather than a traceback around it.
            sys.exit(f"{err}\n\n(/login is typed inside `misaka chat`; "
                     f"`misaka auth check --provider {cfg['provider']}` verifies the result.)")
        from misaka.core.platform import cards as card_files
        card_files.init_project(os.getcwd())
        run = runs.create(con, workspace=os.getcwd(), question=args.goal,
                          limits={"max_depth": args.depth},
                          token_start=budget.spent(con))
        print(f"Project brief: {brief}\nResearch run {run['id']}: {runs.run_dir(run)}")
    try:
        out = _asyncio.run(workflow.run(
            con, cfg, research_node.spawner(), worker_mod,
            run_id=run["id"], poll_seconds=1.0))
    except RuntimeError as err:
        # Node failures already print their own reason above; the workflow's own summary is
        # the useful part, and a traceback of the event loop is not.
        sys.exit(f"Research run {run['id']} stopped: {err}")
    print(f"Research run {run['id']}: {out['reason']}")
    final = out.get("final") or {}
    if final.get("path"):
        print(final["path"])


def _cmd_lcm(args):
    """The LCM operator surface: everything upstream's own `/lcm` offers, plus the four
    opt-in families upstream's doctor predates."""
    from misaka.extensions.hermes_lcm.host import config_bridge as lcm_config
    from misaka.extensions.hermes_lcm.host import operations as lcm_ops

    lcm_db = lcm_config.database_path()

    if args.op in {"status", "doctor", "backup"}:
        print(lcm_ops.report(args.op))
    elif args.op == "externalize-backfill":
        # Old rows do not benefit from switching externalization on; this is how they
        # catch up. Dry run first, like migrate: `--apply` is what rewrites anything.
        from misaka.extensions.hermes_lcm.host import externalize as lcm_externalize
        result = (lcm_externalize.run(args.limit) if args.apply
                  else lcm_externalize.plan(args.limit))
        print(f"Database {result['database'] or lcm_db}")
        if result["note"]:
            print(result["note"])
        print(f"Payload directory {result['directory'] or '(unset)'} | threshold "
              f"{result['threshold_chars']:,} chars")
        if result.get("applied"):
            print(f"{result['moved']} tool result(s) moved to payload files; "
                  f"{result['chars_moved']:,} characters left the database, "
                  f"{result['bytes']:,} bytes reclaimed on disk.")
        else:
            print(f"{result['rows']} tool result(s) totalling {result['chars']:,} characters "
                  "would move to payload files.")
            if not args.apply:
                print("Dry run; nothing was written. Re-run with --apply to externalize.")
    elif args.op == "embed":
        # Upstream's own `/lcm embed` implementation, forwarded whole: the dry run is the
        # default and `--apply` is what spends anything.
        from misaka.extensions.hermes_lcm.host import embed as lcm_embed
        if args.target not in {"warmup", "backfill"}:
            print("Usage: misaka lcm embed warmup|backfill [--apply] [--limit N]")
            sys.exit(2)
        print(lcm_embed.run(args.target, apply=args.apply, limit=args.limit))
    elif args.op == "assertions":
        # Upstream's own `/lcm assertions rebuild`, forwarded whole. The dry run is the
        # default and never constructs an extractor; `--apply` is what calls a model, once
        # per source row it re-derives.
        from misaka.extensions.hermes_lcm.host import assertions as lcm_assertions
        if args.target not in {None, "rebuild"}:
            print("Usage: misaka lcm assertions rebuild [--apply] [--limit N]")
            sys.exit(2)
        print(lcm_assertions.rebuild(apply=args.apply, limit=args.limit))
    elif args.op == "rollups":
        from misaka.extensions.hermes_lcm.host import rollups as lcm_rollups
        # The engine resolves its database through `config_bridge.database_path()`, which lets
        # upstream's own `LCM_DATABASE_PATH` win. Reporting on `lcm_db` instead would
        # answer "nothing has been built" about a file the engine never opened -- and
        # `--rebuild` would build into one database and print a status from another.
        rollup_db = lcm_db
        if args.rebuild:
            outcome = lcm_rollups.rebuild(rollup_db)
            if outcome.get("error"):
                print(f"Database {rollup_db}\n{outcome['error']}")
                sys.exit(2)
            print(f"Seeded {sum(outcome['seeded'].values())} periods in "
                  f"{len(outcome['seeded'])} scopes; built {outcome['built']}.")
            for scope in outcome["exhausted"]:
                print(f"  {scope}: still had work when the pass budget ran out; run it again.")
            report = outcome["status"]
        else:
            report = lcm_rollups.status(rollup_db)
        state = "enabled" if report["enabled"] else "disabled (set LCM_TEMPORAL_ROLLUPS_ENABLED=true)"
        print(f"Database {report['database']} | temporal rollups {state} | "
              f"{report['pending_invalidations']} pending invalidations")
        if not report["installed"]:
            print("No rollup tables in this database yet; nothing has been built.")
        for scope, kinds in sorted(report["scopes"].items()):
            counted = " | ".join(
                f"{kind}: " + ", ".join(f"{state} {count}" for state, count in sorted(states.items()))
                for kind, states in sorted(kinds.items())
            )
            oldest = report["oldest_stale"].get(scope)
            print(f"  {scope}  {counted}" + (f"  (oldest stale {oldest})" if oldest else ""))
        if report["last_error"]:
            print(f"Last build error: {report['last_error']}")
    elif args.op == "rotate":
        # Upstream's own in-place compact, forwarded whole: same session id, same
        # conversation id, no summariser call, and the raw rows stay recoverable. The
        # preview is the default and `--apply` is what writes the rolling backup and
        # advances the lifecycle frontier. The session is an argument because a CLI has
        # no active session for upstream to rotate.
        from misaka.extensions.hermes_lcm.host import operations as lcm_operations
        print(lcm_operations.rotate(args.target or "", apply=args.apply))
    elif args.op == "preset":
        # Upstream's benchmarked model-family presets, forwarded whole. Its `apply`
        # writes no configuration in any mode, so `--apply` gets the same preview plus a
        # line saying it had nothing to commit.
        from misaka.extensions.hermes_lcm.host import operations as lcm_operations
        if args.target not in {"show", "suggest", "apply"}:
            print("Usage: misaka lcm preset show|suggest|apply [NAME] [--apply]")
            sys.exit(2)
        print(lcm_operations.preset(args.target, args.name or "", apply=args.apply))


def _cmd_web(args):
    from misaka.core.web import config as web_config
    from misaka.core.web import dispatch, registry

    if args.op == "set":
        if not args.key or args.value is None:
            print("Usage: misaka web set <key> <value>")
            sys.exit(2)
        before = dict(web_config.web_config().get("provider_tier") or {})
        try:
            path = web_config.set_config(args.key, args.value)
        except ValueError as err:
            print(err)
            sys.exit(2)
        print(f"Set {args.key} in {path}")
        # Choosing a backend clears that backend's own tier pin, the way Hermes' picker
        # does for a row that names no tier. Saying so out loud is the difference between
        # a rule and a surprise: a user who pinned the tier first would otherwise watch it
        # vanish for no visible reason.
        after = web_config.web_config().get("provider_tier") or {}
        cleared = sorted(set(before) - set(after))
        if cleared:
            print(f"Cleared tier pin on {', '.join(cleared)} "
                  f"(re-pin with `misaka web set provider_tier.{cleared[0]} free`)")
        return
    if args.op == "unset":
        if not args.key:
            print("Usage: misaka web unset <key>")
            sys.exit(2)
        path = web_config.unset_config(args.key)
        print(f"Unset {args.key} in {path}")
        return

    # status (default): what will actually serve a search and an extract, and why.
    registry.ensure_backends_registered()
    provider, backend, err = dispatch.resolve_provider()
    if provider is not None:
        print(f"Search backend: {backend}  ({'ready' if registry.provider_is_ready(provider) else 'not ready'})")
    else:
        print(f"Search backend: {backend or 'none'}  — {err or 'no provider can serve'}")
    extractor, extract_backend, extract_err = dispatch.resolve_extractor()
    if extractor is not None:
        ready = "ready" if registry.provider_is_ready(extractor) else "not ready"
        print(f"Extract backend: {extract_backend}  ({ready})")
    else:
        print(f"Extract backend: {extract_backend or 'none'}  — {extract_err}")
    print(f"Keyless ring: {'on' if web_config.keyless_tier_enabled() else 'off'}"
          f"   rescue: {'on' if web_config.keyless_rescue_enabled() else 'off'}")
    print(f"Searchable now: {'yes' if registry.web_search_available() else 'no'}")
    # A tier pinned on a vendor nothing routes to is invisible until it surprises someone:
    # `provider_tier.exa: free` still decides where the keyless ring starts its walk.
    stale = [
        vendor
        for vendor in (web_config.web_config().get("provider_tier") or {})
        if vendor not in {backend, extract_backend}
    ]
    if stale:
        print(f"Tier pins on other vendors: {', '.join(sorted(stale))}"
              "   (`misaka web unset provider_tier.<vendor>` to clear)")
    print("Credentials:")
    for name, is_set, source in web_config.credential_status():
        mark = "✓" if is_set else "·"
        print(f"  {mark} {name}" + (f"  ({source})" if is_set else ""))
    print(f"\nConfig file: {os.path.expanduser(CFG['web_config'])}")


def _cmd_moa(args):
    import json as _json
    import os as _os

    from misaka.core.moa.provider import (
        MOA_CONFIG_PATH,
        load_moa_config,
        slot_label,
    )

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
            # `moa list` prints load_moa_config(), which invents one preset when the file has
            # none; delete can only touch presets the file actually declares. Naming a preset
            # the listing shows but the file does not hold used to report it as unknown and
            # then list it as available in the same sentence.
            if args.name in cfg["presets"]:
                sys.exit(f'"{args.name}" is the built-in MoA preset, not one this file defines, '
                         f"so there is nothing to delete. Write your own presets into {path} "
                         "to replace it.")
            known = ", ".join(cfg["presets"]) or "none"
            sys.exit(f'Unknown preset "{args.name}". Available presets: {known}.')
        if len(presets) <= 1:
            sys.exit("Cannot delete the last remaining MoA preset.")
        del presets[args.name]
        if raw.get("default_preset") == args.name:
            raw["default_preset"] = next(iter(presets))
        atomic.write_text(path, _json.dumps(raw, ensure_ascii=False, indent=2))
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


def _cmd_skills(args):
    import os as _os

    from misaka.core.skills import layers as skill_layers
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
        live = _os.path.join(CFG["roles_root"], args.role, "skills")
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
            ok, why = skill_write.rollback(
                args.name, _os.path.join(live, target["skill"]))
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


def _cmd_doc(args):
    if args.action == "add":
        if not args.arg:
            sys.exit("Usage: misaka doc add <file> [--no-tree]")
        did, n = corpus.ingest(args.arg, with_tree=not args.no_tree)
        doc = corpus.resolve_doc(did)
        has = doc and os.path.exists(os.path.join(doc, "tree.json"))
        structure = "with PageIndex structure" if has else "page navigation only"
        print(f"Added {os.path.basename(args.arg)} as {did}: {n} pages, {structure}.")
    elif args.action == "scan":
        target = args.arg or os.getcwd()
        ingested, skipped = corpus.scan(target, with_tree=not args.no_tree)
        for did, path in ingested:
            print(f"  {did}  {path}")
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
            print(f"  {h['doc_id']} p{h['page']}  {h['s'][:90]}")
        print(f"{len(hits)} match(es). Use `misaka doc verify` before citing a quotation.")
    elif args.action == "verify":
        if not args.arg or not args.doc:
            sys.exit("Usage: misaka doc verify <quote> --doc <doc-id>")
        v = corpus.verify_quote(args.doc, args.arg, workspace=db.canonical_workspace())
        if not v:
            sys.exit("❌ Quote not found in that document.")
        print(f"✅ p{v['page']} offset {v['offset']}\n   claim_hash {v['claim_hash']}")
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

COMMANDS = {
    "chat": _cmd_chat,
    "panel": _cmd_panel,
    "net-daemon": _cmd_net_daemon,
    "card-shell": _cmd_card_shell,
    "net": _cmd_net,
    "init": _cmd_init,
    "create": _cmd_create,
    "remove": _cmd_remove,
    "tell": _cmd_tell,
    "dm": _cmd_dm,
    "task": _cmd_task,
    "board": _cmd_board,
    "research": _cmd_research,
    "lcm": _cmd_lcm,
    "web": _cmd_web,
    "moa": _cmd_moa,
    "skills": _cmd_skills,
    "doc": _cmd_doc,
}


def main(argv=None):
    """The CLI entry point: one handler per sub-command (``COMMANDS``), each opening the board only
    if it uses it; ``argv`` defaults to the process arguments so tests can drive it directly."""
    bootstrap.install()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        # No arguments: open the panel in a terminal, plain chat when piped.
        argv = ["panel"] if sys.stdin.isatty() and sys.stdout.isatty() else ["chat"]
    if argv[0] == "auth":
        # Auth has its own Pi-compatible grammar; preserve the raw option order instead
        # of sending it through the product CLI's unrelated parser.
        from misaka.cli.auth import run_auth_command

        return run_auth_command(argv[1:])
    args = _parser().parse_args(argv)
    from misaka.core.skills.layers import SkillsConfigError

    try:
        return COMMANDS[args.cmd](args)
    except SkillsConfigError as error:
        # A skills.json the user broke by hand is their file to fix, not a traceback.
        print(f"misaka: {error}", file=sys.stderr)
        return 1

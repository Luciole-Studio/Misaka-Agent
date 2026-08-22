"""Switch the interactive UI between Last Order and Sister profiles."""
import os
import sys



def _roster():
    from misaka.config import sisters
    return ["last-order"] + sorted(sisters())


def _current():
    return os.environ.get("MISAKA_WHO") or "last-order"


def register(harn):
    async def misaka_switch(args, ctx):
        roster, cur = _roster(), _current()
        raw = (args or "").strip()
        force = raw.endswith("!")
        name = raw.rstrip("!").strip()

        if not name:
            from misaka.config import CFG
            from misaka.network.roster import describe_line

            def _label(n):
                bits = (["current"] if n == cur else []) + list(filter(None, [
                    describe_line(n, root=CFG["profiles_root"]) if n != "last-order" else None]))
                return n + (f" ({' | '.join(bits)})" if bits else "")

            picked = await ctx.ui.select(f"Current role: {cur}. Switch to:", [_label(n) for n in roster])
            if not picked:
                return
            name = picked.split(' (')[0]

        if name not in roster:
            ctx.ui.notify(f"Unknown role '{name}'. Available: {', '.join(roster)}", "error")
            return
        if name == cur:
            ctx.ui.notify(f"Already using {cur}.", "info")
            return
        if os.environ.get("MISAKA_NET_PANE"):
            from misaka.net import client as net
            argv = [sys.executable, "-m", "misaka", "chat"]
            title = "Last Order" if name == "last-order" else name
            if name != "last-order":
                argv += ["--as", name]
            out = net.request("pane.create",
                              {"argv": argv, "cwd": os.getcwd(), "title": title,
                               "parent": os.environ["MISAKA_NET_PANE"]})
            ctx.ui.notify(
                f"{name} is now open in pane {out['pane_id']}. "
                "Select it from the sidebar or press Ctrl+B and its number.",
                "info",
            )
            return
        if not force:
            ok = await ctx.ui.confirm(
                f"Switch to {name}?",
                "This opens that role's session; the current conversation is not copied.")
            if not ok:
                return

        argv = [sys.executable, "-m", "misaka", "chat"]
        if name != "last-order":
            argv += ["--as", name]
        ctx.ui.notify(f"Switching to {name}…", "info")
        os.execv(sys.executable, argv)

    harn.registerCommand("sister", {
        "description": "List roles or switch between Last Order and a Sister; use `/sister last-order` to return.",
        "handler": misaka_switch,
    })

SESSION_KINDS = {"foreground", "dm"}


def activate(spec):
    from misaka.network import roster

    def both(harn):
        register(harn)
        roster.register(harn)
    return both

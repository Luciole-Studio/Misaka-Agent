"""Switch the interactive UI between Last Order and Sister profiles."""
import os
import sys

from misaka.core.moments import CoreCommand


def _roster():
    from misaka.config import sisters
    return ["last-order"] + sorted(sisters())


def _current():
    return os.environ.get("MISAKA_WHO") or "last-order"


def _sister_command():
    async def misaka_switch(args, ctx):
        roster, cur = _roster(), _current()
        raw = (args or "").strip()
        force = raw.endswith("!")
        name = raw.rstrip("!").strip()

        if not name:
            from misaka.config import CFG
            from misaka.core.network.roster import describe_line

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
            from misaka.ui.panel import client as net
            argv = [sys.executable, "-m", "misaka", "chat"]
            title = "Last Order" if name == "last-order" else name
            if name != "last-order":
                argv += ["--as", name]
            out = net.request("pane.create",
                              {"argv": argv, "cwd": os.getcwd(), "title": title,
                               "place": {"grid": os.environ["MISAKA_NET_PANE"]}})   # a pane beside her Last Order (grid)
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

    return CoreCommand(
        "sister",
        "List roles or switch between Last Order and a Sister; use `/sister last-order` to return.",
        misaka_switch,
    )


class SisterSwitchPart:
    """``/sister``: one command, no tools."""

    def __init__(self):
        self.tools = []
        self.commands = [_sister_command()]


SESSION_KINDS = {"foreground", "dm"}


def part(spec):
    return SisterSwitchPart()

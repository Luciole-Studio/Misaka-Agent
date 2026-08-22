"""Read-only slash commands for board and execution-tree views of the current project folder."""


def register(harn):
    from misaka.config import CFG
    from misaka.platform import tasks as db

    def _con():
        return db.connect(CFG["db"])

    def _workspace(ctx):
        return db.canonical_workspace(getattr(ctx, "cwd", None))

    async def board_cmd(args, ctx):
        from misaka.observability import board as tail
        ctx.ui.notify(tail.board_text(_con(), _workspace(ctx)) or "(No task cards on the board.)", "info")

    harn.registerCommand("board", {
        "handler": board_cmd,
        "description": "Show the read-only task board of this project folder."})

    async def trace_cmd(args, ctx):
        from misaka.observability import overview as observe
        ctx.ui.notify(observe.render(_con(), _workspace(ctx)), "info")

    harn.registerCommand("trace", {
        "handler": trace_cmd,
        "description": "Show the read-only task card → to-do → agent execution tree of this project folder."})

SESSION_KINDS = {"foreground", "dm"}


def activate(spec):
    return register

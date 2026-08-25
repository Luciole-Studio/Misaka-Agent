"""Read-only slash command for the task board of the current project folder."""


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


SESSION_KINDS = {"foreground", "dm"}


def activate(spec):
    return register

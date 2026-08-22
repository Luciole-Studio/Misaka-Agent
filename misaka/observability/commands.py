"""Read-only slash commands for board and execution-tree views."""


def register(harn):
    from misaka.config import CFG
    from misaka.platform import tasks as db

    def _con():
        return db.connect(CFG["db"])

    async def board_cmd(args, ctx):
        from misaka.observability import board as tail
        ctx.ui.notify(tail.board_text(_con()) or "(No task cards on the board.)", "info")

    harn.registerCommand("board", {
        "handler": board_cmd,
        "description": "Show the read-only task board grouped by Project."})

    async def trace_cmd(args, ctx):
        from misaka.observability import overview as observe
        project = (args or "").strip() or None
        ctx.ui.notify(observe.render(_con(), project), "info")

    harn.registerCommand("trace", {
        "handler": trace_cmd,
        "description": "Show the read-only Project → task card → to-do → agent execution tree; pass a project to filter it."})

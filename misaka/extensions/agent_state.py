"""Tell the Misaka Network daemon what this session is doing (herdr's pi hook, MISAKA edition).

herdr installs ``herdr-agent-state.ts`` into pi so the agent reports its own state instead of
being guessed from the screen (src/integration/assets/pi/herdr-agent-state.ts). Same here:
``agent_start`` -> working, ``agent_end`` -> idle, an AskUserQuestion in flight -> blocked
(the panel's red dot: she is waiting for a person). Reports go to ``pane.report_state`` with a
monotonic seq; a duplicate state is not resent. Only sessions that live in a pane take part.

Each report also carries the session file this pane is writing, so the panel can mark the
open ones in its sessions list and jump to the tab instead of opening a second one.
"""
from __future__ import annotations

import asyncio
import os

# Sessions with a UI inside a pane. Children inherit MISAKA_NET_PANE from their parent but run
# headless, and must not speak for her pane.
SESSION_KINDS = {"foreground", "dm", "card"}
BLOCKING_TOOLS = frozenset({"AskUserQuestion"})
BLOCKED_MESSAGE = "waiting for your answer"


class Reporter:
    """herdr-agent-state.ts:180-204 desiredState / publishState as a small state machine:
    blocked (a question is open) beats working (a turn is running) beats idle."""

    def __init__(self, send):
        self.send = send            # send(state, message, seq, session): blocking, may raise
        self.active = False
        self.blocked = 0
        self.message = ""
        self.session = ""           # the file this session writes; the panel matches its list against it
        self.last = None
        self.seq = 0

    def desired(self):
        if self.blocked > 0:
            return "blocked", self.message
        return ("working", "") if self.active else ("idle", "")

    def note_session(self, ctx):
        """The session file can change under us (resume, fork), so read it at every report."""
        manager = getattr(ctx, "sessionManager", None)
        self.session = getattr(manager, "sessionFile", None) or self.session

    async def publish(self, force=False):
        state, message = self.desired()
        if not force and (state, message, self.session) == self.last:
            return False
        self.last = (state, message, self.session)
        self.seq += 1
        try:
            await asyncio.to_thread(self.send, state, message, self.seq, self.session)
        except Exception:  # noqa: BLE001 - the daemon may be gone; a status ping never breaks the session
            pass
        return True


def activate(spec):
    if os.environ.get("MISAKA_SUBAGENT_ID"):
        return None            # a child process inherits its parent's pane id but must not speak for that pane
    return register if os.environ.get("MISAKA_NET_PANE") else None


def register(harn, send=None):
    pane_id = os.environ.get("MISAKA_NET_PANE", "")
    if send is None:
        from misaka.ui.panel import client as net

        def send(state, message, seq, session=""):
            net.request("pane.report_state",
                        {"id": pane_id, "state": state, "message": message, "seq": seq,
                         "session": session},
                        timeout=3)
    reporter = Reporter(send)

    async def session_start(_event, ctx):
        reporter.note_session(ctx)
        await reporter.publish(force=True)

    async def agent_start(_event, ctx):
        reporter.active = True
        reporter.note_session(ctx)
        await reporter.publish()

    async def agent_end(_event, ctx):
        reporter.active = False
        reporter.note_session(ctx)
        await reporter.publish()

    async def tool_start(event, _ctx):
        if event.get("toolName") in BLOCKING_TOOLS:
            reporter.blocked += 1
            reporter.message = BLOCKED_MESSAGE
            await reporter.publish()

    async def tool_end(event, _ctx):
        if event.get("toolName") in BLOCKING_TOOLS:
            reporter.blocked = max(0, reporter.blocked - 1)
            if reporter.blocked == 0:
                reporter.message = ""
            await reporter.publish()

    async def shutdown(_event, _ctx):
        reporter.active, reporter.blocked, reporter.message = False, 0, ""
        await reporter.publish()

    harn.on("session_start", session_start)
    harn.on("agent_start", agent_start)
    harn.on("agent_end", agent_end)
    harn.on("tool_execution_start", tool_start)
    harn.on("tool_execution_end", tool_end)
    harn.on("session_shutdown", shutdown)
    return reporter

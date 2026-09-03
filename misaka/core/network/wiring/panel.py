"""The session side of the panel: what a session in a pane tells the daemon, and how it forks there.

Two things the panel needs from inside a session, both formerly bundled extensions:

* state reports (herdr's ``herdr-agent-state.ts``, MISAKA edition): ``agent_start`` -> working,
  ``agent_end`` -> idle, an extension UI prompt in flight -> blocked (the panel's red dot: she is
  waiting for a person). Reports go to ``pane.report_state`` with a seq that stays monotonic across
  reloads; a duplicate state is not resent. Each report carries the session file this pane writes,
  so the panel can mark the open sessions in its list and jump to the tab.
* fork as a split, not a swap: pi's fork moves THIS pane onto the branched file, which in the panel
  hides the divergence. Inside a pane the part cancels the swap, creates the same branched file the
  runtime would, and asks the daemon to seat a fresh ``misaka chat --session <branch>`` as a split
  of this pane. Both branches stay live, side by side. Foreground only: a card pane belongs to
  card_shell's Supervisor, and a headless chat has no pane to split.

Only sessions that live in a pane take part; a child process inherits its parent's pane id but
must not speak for that pane.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# Sessions with a UI inside a pane. Children inherit MISAKA_NET_PANE from their parent but run
# headless, and must not speak for her pane.
SESSION_KINDS = {"foreground", "dm", "card"}
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
        self.seq = max(self.seq + 1, time.monotonic_ns())
        try:
            await asyncio.to_thread(self.send, state, message, self.seq, self.session)
        except Exception:  # noqa: BLE001, S110 - the daemon may be gone; a status ping never breaks the session
            pass
        return True


class PanelPart:
    def __init__(self, role, kind, *, send=None, request=None, open_manager=None):
        self.tools = []
        self.commands = []
        self.session = None
        self.role = role
        self.kind = kind
        # ``--as`` (cli/app.py) names a Sister the way chat.assembly resolves her -- relative
        # to profiles/sisters/ -- while the role is the path relative to profiles/
        # (``sisters/10032``). Same trim as messages.py and todo.py.
        self.who = role.rsplit("/", 1)[-1]
        self.pane_id = os.environ.get("MISAKA_NET_PANE", "")
        self._request = request
        self._open_manager = open_manager
        self.reporter = Reporter(send or self._send)

    def _send(self, state, message, seq, session=""):
        from misaka.ui.panel import client as net

        net.request("pane.report_state",
                    {"id": self.pane_id, "state": state, "message": message, "seq": seq,
                     "session": session},
                    timeout=3)

    # -- state reports --

    async def session_start(self, _event, ctx):
        self.reporter.note_session(ctx)
        await self.reporter.publish(force=True)

    async def agent_start(self, _event, ctx):
        self.reporter.active = True
        self.reporter.note_session(ctx)
        await self.reporter.publish()

    async def agent_end(self, _event, ctx):
        self.reporter.active = False
        self.reporter.note_session(ctx)
        await self.reporter.publish()

    async def ui_prompt_start(self, event, _ctx):
        self.reporter.blocked += 1
        self.reporter.message = str(event.get("title") or BLOCKED_MESSAGE)
        await self.reporter.publish()

    async def ui_prompt_end(self, _event, _ctx):
        self.reporter.blocked = max(0, self.reporter.blocked - 1)
        if self.reporter.blocked == 0:
            self.reporter.message = ""
        await self.reporter.publish()

    async def session_shutdown(self, _event, _ctx):
        self.reporter.active, self.reporter.blocked, self.reporter.message = False, 0, ""
        await self.reporter.publish()

    # -- fork as a split --

    async def session_before_fork(self, event, ctx):
        if self.kind != "foreground":
            return None
        request = self._request
        if request is None:
            from misaka.ui.panel import client as net
            request = net.request
        open_manager = self._open_manager
        if open_manager is None:
            from misaka.core.session_manager import SessionManager
            open_manager = SessionManager.open
        manager = getattr(ctx, "sessionManager", None)
        source = getattr(manager, "sessionFile", None)
        entry = manager.getEntry(str(event.get("entryId"))) if source else None
        if not entry:
            return None
        if event.get("position") == "at":            # /tree "clone here"
            leaf = str(entry["id"])
        else:                                        # /fork a user message: branch at its parent
            if (entry.get("message") or {}).get("role") != "user":
                return None                          # the runtime validates and refuses as pi does
            leaf = entry.get("parentId")
        if not leaf:
            return None
        role, who, pane_id = self.role, self.who, self.pane_id

        def split():
            branch_manager = open_manager(source, manager.getSessionDir())
            branched = branch_manager.createBranchedSession(str(leaf))
            if not branched:
                return False
            if not os.path.isfile(branched):
                # createBranchedSession defers the write until the branch holds an
                # assistant message; the new pane resumes from disk, so write it now.
                branch_manager.rewrite_file()
            chat = [sys.executable, "-m", "misaka", "chat"]
            if role != "last_order":           # chat.py -- Last Order's role name
                chat += ["--as", who]
            created = request("pane.create",
                              {"argv": [*chat, "--session", branched], "cwd": manager.getCwd(),
                               "title": "Last Order" if role == "last_order" else who,
                               "place": {"split": pane_id}})
            # Only a seated pane (daemon.py answers with its id) earns the cancel:
            # cancelling first and failing second loses the fork at both ends.
            return bool(created and created.get("pane_id"))

        try:
            done = await asyncio.to_thread(split)
        except Exception:  # noqa: BLE001 - daemon gone or disk trouble: pi's in-place fork still works
            return None
        return {"cancel": True} if done else None


def part(spec):
    if os.environ.get("MISAKA_SUBAGENT_ID"):
        return None            # a child process inherits its parent's pane id but must not speak for that pane
    if not os.environ.get("MISAKA_NET_PANE"):
        return None
    return PanelPart(spec.role, spec.kind)

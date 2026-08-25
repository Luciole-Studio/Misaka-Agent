"""Fork opens a split, not a swap (MISAKA over pi's session tree).

pi's fork (core/agent_session_runtime.fork) moves THIS pane onto the branched file, which
in the panel hides the divergence: the original line silently vanishes from view. Inside a
panel pane this extension cancels the swap, creates the same branched file the runtime
would, and asks the daemon to seat a fresh ``misaka chat --session <branch>`` beside this
pane in the same tab (the panel tiles children the way it seats a summoned Sister). Both
branches stay live, side by side.

Untouched on purpose: bare/headless chats (no pane to split), card panes (card_shell's
Supervisor owns this process; SESSION_KINDS keeps them out), and forking from before the
first message (the runtime starts an empty session then -- there is no shared line to keep).
"""
from __future__ import annotations

import asyncio
import os
import sys

SESSION_KINDS = {"foreground"}


def activate(spec):
    if not os.environ.get("MISAKA_NET_PANE"):
        return None
    role = spec.role

    def register(harn, request=None, open_manager=None):
        pane_id = os.environ["MISAKA_NET_PANE"]
        if request is None:
            from misaka.net import client as net
            request = net.request
        if open_manager is None:
            from misaka.core.session_manager import SessionManager
            open_manager = SessionManager.open

        async def before_fork(event, ctx):
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

            def split():
                branch_manager = open_manager(source, manager.getSessionDir())
                branched = branch_manager.createBranchedSession(str(leaf))
                if not branched:
                    return False
                if not os.path.isfile(branched):
                    # createBranchedSession defers the write until the branch holds an
                    # assistant message; the new pane resumes from disk, so write it now.
                    branch_manager._rewriteFile()  # noqa: SLF001
                chat = [sys.executable, "-m", "misaka", "chat"]
                if role != "last_order":           # chat.py:42 -- Last Order's role name
                    chat += ["--as", role]
                request("pane.create",
                        {"argv": [*chat, "--session", branched], "cwd": manager.getCwd(),
                         "title": "Last Order" if role == "last_order" else role,
                         "parent": pane_id})
                return True

            try:
                done = await asyncio.to_thread(split)
            except Exception:  # noqa: BLE001 - daemon gone or disk trouble: pi's in-place fork still works
                return None
            return {"cancel": True} if done else None

        harn.on("session_before_fork", before_fork)
        return before_fork

    return register

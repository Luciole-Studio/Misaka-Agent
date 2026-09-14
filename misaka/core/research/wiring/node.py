"""A research node's routine inside its own interactive window.

``node.run_interactive`` opens a fork Last Order's conversation as a full chat in a pane of
the panel and names the node in ``MISAKA_RESEARCH_NODE``; this part, present only then, runs
the node's routine (``workflow.expand_node``) on that very session -- the way the root's
``/research`` driver runs the root's routine on the user's window. Her phase turns and the
user's own turns share one conversation, so the wait for a plan's go-ahead and every later
word need no relay. When the routine ends the node row is released to the parent driver and
the window stays open for the user; a window closed before that ends the node, and the parent
reports it.
"""
from __future__ import annotations

import asyncio
import os

from misaka.config import CFG, current_config
from misaka.core.platform import tasks as task_store
from misaka.core.research import runs

SESSION_KINDS = {"foreground"}
ROLES = {"last_order"}


def node_identity():
    """``(run_id, node_id, runner_key)`` from ``MISAKA_RESEARCH_NODE``; None outside a node's window."""
    parts = os.environ.get("MISAKA_RESEARCH_NODE", "").split()
    return tuple(parts) if len(parts) == 3 else None


class NodePart:
    """Drives one node's routine on the window's own session, once, from session start."""

    def __init__(self, run_id, node_id, runner_key):
        self.run_id, self.node_id, self.runner_key = run_id, node_id, runner_key
        self.tools = []
        self.commands = []
        self.session = None
        self.routine = None

    def attach(self, session):
        self.session = session

    async def session_start(self, _event, _ctx):
        if self.routine is None:            # once per process: a later session switch is not a new node
            self.routine = asyncio.create_task(self.run())

    async def session_shutdown(self, _event, _ctx):
        if self.routine is not None and not self.routine.done():
            self.routine.cancel()
            await asyncio.gather(self.routine, return_exceptions=True)

    def show(self, content, details=None):
        self.session.moments.send_message(
            {"customType": "research-progress", "display": True, "content": content,
             "details": {"run_id": self.run_id, **(details or {})}},
            {"deliverAs": "followUp", "triggerTurn": False})

    async def run(self):
        """The node's routine, then release: the window outlives it, the runner does not."""
        from misaka.core.platform import notifications
        from misaka.core.research import workflow
        from misaka.core.research.node import PaneRunner
        from misaka.core.research.window import WindowLO
        from misaka.core.session_control import for_session
        con = task_store.connect(os.path.expanduser(CFG["db"]))
        runs.init(con)
        cfg = current_config()
        run = runs.get(con, self.run_id)
        label = f"node {self.node_id}"

        def check_active():
            if runs.stop_requested(con, self.run_id):
                raise InterruptedError("Research stopped.")
            owner = runs.node(con, self.node_id)
            if owner["runner_key"] != self.runner_key or runs.get(con, self.run_id)["driver_lock"] != run["driver_lock"]:
                raise RuntimeError("Research node or driver changed owners.")

        def describe():
            branch = runs.node(con, self.node_id)
            return {"node": self.node_id, "depth": branch["depth"], "phase": branch["status"]}

        def progress(event):
            branch = runs.node(con, self.node_id)
            payload = {**event, "node_id": branch["id"], "depth": branch["depth"], "issue_id": None}
            notifications.publish(con, "research", self.run_id, "progress", payload)   # the root window's feed
            content = f"Research `{self.run_id}` | {event['message']}"
            for item in event.get("tasks") or []:
                content += f"\n- {item['title']} → Sister {item['assignee']}"
            self.show(content, payload)

        def failed(text):
            con.execute('UPDATE research_branches SET last_error=COALESCE(last_error,?) WHERE id=? AND runner_key=?',
                        (text, self.node_id, self.runner_key))

        window = None
        control = None
        try:
            window = WindowLO(self.session, check_active)
            runs.set_node(con, self.node_id, session_file=window.session_file)
            control = for_session(self.session)
            if control is not None:         # a chat attached from elsewhere sees the node, not just a session
                previous_check, previous_describe = control.check_active, control.describe
                control.check_active = check_active
                control.describe = describe
            runner = PaneRunner(con, cfg, label, os.environ.get("MISAKA_NET_PANE"))
            result = await workflow.expand_node(con, cfg, runner, window, run_id=self.run_id, node_id=self.node_id,
                                                progress=progress, session=self.session)
            if isinstance(result, dict):
                questions = "; ".join(result.get("questions") or [])
                self.show(f"{label} needs input: {questions}. Answer with `/research resume` in the root window.")
            else:
                self.show(f"{label}: {result}. This window stays open; ask its Last Order about what she found.")
        except asyncio.CancelledError:
            if runs.node(con, self.node_id)["status"] not in runs.NODE_TERMINAL:
                failed("the node's window was closed before its routine finished")
            raise
        except Exception as error:  # noqa: BLE001 - the persisted attempt names the actual cause
            text = f"{type(error).__name__}: {error}"
            failed(text)
            self.show(f"{label} failed: {text}. Resume the run to retry it.")
        finally:
            try:
                if window is not None:
                    await window.close()
            finally:
                # The conversation outlives this runner and its connection. Do not
                # detach callbacks installed by a subsequent owner during cleanup.
                if control is not None:
                    if control.check_active is check_active:
                        control.check_active = previous_check
                    if control.describe is describe:
                        control.describe = previous_describe
                try:
                    runs.release_runner(con, "research_branches", self.node_id, self.runner_key)
                finally:
                    con.close()


def part(_spec):
    named = node_identity()
    return NodePart(*named) if named else None

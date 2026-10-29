"""Drive all phases through a node's original AgentSession, in its owning event loop."""
from __future__ import annotations

import asyncio
import os
from contextlib import ExitStack, asynccontextmanager

from misaka.ai.utils._output_limit import output_limit_error
from misaka.core.network import worker
from misaka.core.platform import budget
from misaka.core.research.report import DRAFT_CONTRACT, FINAL_CONTRACT
from misaka.core.session_control import for_session, wait_for_session
from misaka.utils.async_lifecycle import settle
from misaka.utils.values import read_field

# Hermes' text-truncation recovery (agent/turn_truncation.py ``_continue_text``): a reply cut at the
# output cap with no tool call gets this nudge in the same conversation, up to the fourth cut reply,
# and the pieces are joined. The wording is Hermes' own (conversation_loop.py), so Hermes' context
# compressor -- the LCM engine here -- still recognizes the nudge as synthetic.
LENGTH_CONTINUATION_OUTPUT_LIMIT = (
    "[System: Your previous response was truncated by the output length limit. Continue exactly "
    "where you left off. Do not restart or repeat prior text. Finish the answer directly.]"
)
LENGTH_CONTINUATION_LIMIT = 4
# Below this many free tokens the prompt itself filled the window: each nudge and fragment only
# shrinks the room (Hermes ``_MIN_CONTINUATION_HEADROOM``, #106120).
MIN_CONTINUATION_HEADROOM = 512


def join_truncated_parts(parts):
    """Hermes ``_join_truncated_parts`` for replies cut at the output cap: a newline goes between
    two pieces only where neither side already has whitespace. (Its overlap trimming is for
    streams cut by a network error, which never reach this path.)"""
    joined = ""
    for part in parts:
        if joined and not joined[-1].isspace() and part and not part[0].isspace():
            joined += "\n"
        joined += part
    return joined


def _prompt_filled_window(session, message):
    """``(prompt_tokens, context_window)`` when the cut reply's own prompt left less than
    ``MIN_CONTINUATION_HEADROOM`` in the window; None (keep continuing) when either is unknown."""
    window = read_field(getattr(session, "model", None), "contextWindow") or 0
    usage = read_field(message, "usage")
    prompt = sum(read_field(usage, name) or 0 for name in ("input", "cacheRead", "cacheWrite")) if usage else 0
    return (prompt, window) if window and prompt and window - prompt < MIN_CONTINUATION_HEADROOM else None


def node_description(con, node_id):
    """Keep run-wide progress distinct from the current node's lifecycle."""
    from misaka.core.research import runs

    node = runs.node(con, node_id)
    run = runs.get(con, node["run_id"])
    return {"run_id": run["id"], "node": node["id"], "depth": node["depth"],
            "run_phase": run["phase"], "run_status": run["status"], "node_phase": node["status"]}


def _listening(con, run, node):
    """The catalog record of the live session a node's Last Order is running in, or None."""
    from misaka.core import session_catalog
    from misaka.core.research import planner

    path = planner.lo_session_file(run, node)
    record = session_catalog.owner_record(path) if path else {}
    live = session_catalog.live_session(record.get("id")) if record.get("control") else None
    return live if live and live.get("control") else None


async def tell(con, run, text, *, node_id=None):
    """Say something to a node's Last Order while the run goes on -- the root's when no node is
    named -- as if it were typed in her window: through the input socket every live session opens
    (``misaka chat --attach``), a steer while she is mid-turn, a turn of its own while she waits on
    her cards. Returns the session file her answer is written to. Raises ValueError naming who can
    hear when the node named cannot."""
    from misaka.core.research import runs
    from misaka.core.session_control import request

    text = (text or "").strip()
    if not text:
        raise ValueError("Say what to tell the run.")
    every = runs.nodes(con, run["id"])
    node = next((row for row in every if (row["id"] == node_id if node_id else runs.is_root(row))), None)
    listening = [(row, _listening(con, run, row)) for row in every]
    listening = [(row, record) for row, record in listening if record]
    who = "; ".join(f"{row['id']} (depth {row['depth']}): {' '.join(row['question'].split())[:60]}"
                    for row, _record in listening) or "none"
    if node is None:
        raise ValueError(f"Research run {run['id']} has no node {node_id}. Listening now: {who}")
    record = next((record for row, record in listening if row["id"] == node["id"]), None)
    if record is None:
        raise ValueError(f"The Last Order of node {node['id']} is not running now ({node['status']}). "
                         f"Listening now: {who}")
    try:
        await request(record, "input", text=text)
    except (OSError, KeyError, TimeoutError) as error:
        raise ValueError(f"Delivery to node {node['id']} is unconfirmed: {error or 'no answer'}. "
                         "Not sent again automatically.") from error
    return record.get("path")


class WindowLO:
    """The synchronous planner calls back onto the window's own event loop and session.

    Root reuses its chat window or owns a headless session through final adjudication (and every
    reconciliation of the graph); every other node's Last Order owns a resident session in its
    isolated node process. Closing cancels pending callbacks too:
    cancelling asyncio.to_thread alone does not stop its thread.
    """

    def __init__(self, session, check_active, *, describe, headless=False):
        self.session = session
        self.session_file = session.sessionManager.sessionFile
        self.loop = asyncio.get_running_loop()
        self.check_active = check_active
        self.pending = set()
        self.closed = False
        self.turn_lock = asyncio.Lock()
        self.headless = headless
        from misaka.core.network.wiring.capabilities import SisterCapabilitiesPart

        self.publisher = next((part for part in session.moments.parts
                               if isinstance(part, SisterCapabilitiesPart)), None)
        if self.publisher is None:
            raise RuntimeError("Research requires the coordinator's system-prompt capability catalog.")
        # The mode outlives a phase turn: approval waits and human input inherit
        # the same filter. A phase scope only adds its temporary commands.
        self.mode = ExitStack()
        self.mode.enter_context(self.publisher.snapshot())
        control = for_session(session)
        if control is not None:
            previous = control.describe

            def restore_description():
                # A later owner may have replaced this window's description.
                if control.describe is describe:
                    control.describe = previous

            self.mode.callback(restore_description)
            control.describe = describe

    def run_llm_json(self, _profile, prompt, _provider, _model, **options):
        return asyncio.run_coroutine_threadsafe(self._turn(prompt, options), self.loop).result()

    async def close(self):
        self.closed = True
        pending = list(self.pending)
        for task in pending:
            task.cancel()
        try:
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self.mode.close()

    async def _turn(self, prompt, options):
        if self.closed:
            raise InterruptedError("Research window driver closed.")
        task = asyncio.create_task(self._execute(prompt, options))
        self.pending.add(task)
        try:
            while True:
                self.check_active()
                done, _ = await asyncio.wait({task}, timeout=0.2)
                if done:
                    return await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.pending.discard(task)

    async def _execute(self, prompt, options, *, preflight=None):
        if preflight is None:
            await wait_for_session(self.session)
        async with self.turn_lock:
            return await self._execute_turn(prompt, options, preflight=preflight)

    async def _execute_turn(self, prompt, options, *, preflight=None):
        session = self.session
        # No tools, reservations or subscriptions are changed during another chat turn.
        while not session.isIdle:
            await session.waitForIdle()
        self.check_active()
        if session.sessionManager.sessionFile != self.session_file:
            raise RuntimeError("Research window changed conversation; resume in the intended session.")
        if os.path.realpath(options["session_dir"]) != os.path.realpath(os.path.dirname(self.session_file)):
            raise RuntimeError("A research call was routed to a different node's session.")
        if worker.over_cap(options.get("usage_db"), options.get("usage_task_id"), options.get("usage_token_cap")):
            # Last Order's window, not a card: nothing "stays ready" here; the run halts at its cap.
            return None, "", f"{budget.EXHAUSTED_MESSAGE}: this research run stops here until the cap is raised."
        definitions = list(options.get("extra_tools", ()))
        scope = ExitStack()
        guard_hooks = None
        answer, error, final = None, None, None

        def observe(event):
            nonlocal answer, error, final
            if read_field(event, "type") != "message_end":
                return
            message = read_field(event, "message")
            if read_field(message, "role") != "assistant":
                return
            if answer is None:
                final = message                  # the turn's last reply so far
            stop = read_field(message, "stopReason")
            if stop in {"error", "aborted", "length"} and answer is None:
                error = read_field(message, "errorMessage") or (
                    output_limit_error(message) if stop == "length" else f"request {stop}")
            elif stop == "stop" and answer is None:
                # A queued user/notification follow-up belongs to the same window, but
                # must not replace the research phase's completed Markdown.
                answer = "".join(read_field(part, "text", "") for part in read_field(message, "content", [])
                                 if read_field(part, "type") == "text")
                error = None

        unsubscribe = session.subscribe(observe)
        try:
            from misaka.core.research.planner import session_tools
            # Re-read at the actual turn boundary; a queued phase's old selection
            # must not resurrect capabilities the user has since disabled.
            names = [*session_tools(self), *(tool.name for tool in definitions)]
            scope.enter_context(session.toolScope(names))
            scope.enter_context(self.publisher.snapshot(options.get("sister_catalog")))
            session.registerCustomTools(definitions)
            from misaka.core.research.planner import OPTIONAL_MATERIAL_TOOLS
            # Credentials/executables decide availability; missing mandatory tools still fail.
            missing = set(names) - set(session.getActiveToolNames()) - set(OPTIONAL_MATERIAL_TOOLS)
            if missing:
                raise RuntimeError(f"The window is missing research tools: {', '.join(sorted(missing))}")
            if self.headless and not hasattr(session.agent, "_misaka_guards"):
                from misaka.agent.guards import install_guards
                from misaka.core.platform import metering
                from misaka.core.platform.vocabulary import NO_PROGRESS_VOCABULARY

                guard_hooks = (session.agent.finishTurn, session.agent.prepareNextTurnWithContext)
                install_guards(session, metering.allowance, no_progress_vocabulary=NO_PROGRESS_VOCABULARY)
            # Direct await enters the session's active-run guard before yielding. A
            # queued notification cannot take the window between idle and this turn.
            # The turn's model requests -- the phase's own, compaction, the vision bridge --
            # are billed to the run through this context (metering).
            with budget.usage_context(options.get("usage_db"), options.get("usage_task_id"),
                                      options.get("usage_generation"), options.get("usage_token_cap")):
                from misaka.core.moments import TURN

                async def send(text):
                    await session.sendCustomMessage(
                        {"customType": "research-phase", "display": True, "content": text,
                         "details": {"run_id": options.get("usage_task_id"), TURN: True,
                                     "stage": "adjudication_draft" if text.startswith((DRAFT_CONTRACT, FINAL_CONTRACT)) else None}},
                        {"triggerTurn": True})

                if preflight is not None:
                    await session.prompt(prompt, {"streamingBehavior": "steer", "preflightResult": preflight})
                else:
                    await send(prompt)
                fragments = []
                # A phase whose reply is its deliverable (``continue_truncated``) is continued the way
                # Hermes continues a cut reply. A phase that delivers through a tool already has its own
                # nudge for a reply cut before the call (``planner.commanded``). A cut reply with no
                # visible text is not continued: Hermes turns reasoning off for that one request, and a
                # pi session can only change its thinking level by recording the change in the transcript.
                while (options.get("continue_truncated") and answer is None and final is not None
                       and read_field(final, "stopReason") == "length"
                       and not any(read_field(part, "type") == "toolCall" for part in read_field(final, "content", []))):
                    text = "".join(read_field(part, "text", "") for part in read_field(final, "content", [])
                                   if read_field(part, "type") == "text")
                    if not text:
                        break
                    fragments.append(text)
                    if len(fragments) >= LENGTH_CONTINUATION_LIMIT:
                        error = (f"Response remained truncated after {LENGTH_CONTINUATION_LIMIT} continuation "
                                 f"attempts: {error}")
                        break
                    filled = _prompt_filled_window(session, final)
                    if filled is not None:
                        error = f"Prompt used {filled[0]} of {filled[1]} context tokens; no room to answer: {error}"
                        break
                    self.check_active()
                    error, final = None, None
                    await send(LENGTH_CONTINUATION_OUTPUT_LIMIT)
                if answer is not None and fragments:
                    answer = join_truncated_parts([*fragments, answer])
            self.check_active()
            return None, answer or "", error or (None if answer is not None else "no completed research response")
        finally:
            unsubscribe()
            if guard_hooks is not None:
                session.agent.finishTurn, session.agent.prepareNextTurnWithContext = guard_hooks
                del session.agent._misaka_guards
            try:
                session.unregisterCustomTools(definitions)
            finally:
                scope.close()


@asynccontextmanager
async def node_session(con, cfg, run, node):
    """One headless runtime per node; the root also owns the run-level report and review."""
    from misaka.core.platform.session import _env_window, dispose, open_session
    from misaka.core.research import planner, runs

    directory = planner._lo_session(run, node)
    session_file = planner.lo_session_file(run, node)
    from misaka.core.wiring import role_session_setup

    flags, assembly, env = role_session_setup(
        os.path.join(cfg["roles_root"], "last_order"), run["workspace"],
        workflow_context=True, overrides=runs.session_overrides(run))
    flags += ["--session-dir", directory]
    # A conversation recorded but never written -- the run failed before its first entry was
    # persisted -- is opened anew: asked for by name it raised FileNotFoundError on every resume,
    # and --continue would pick whatever else is newest in its directory, for a root started from
    # a chat that chat's other conversations (0.18.10 sweep).
    if session_file and os.path.exists(session_file):
        flags += ["--session", session_file]
    elif not session_file:
        flags.append("--continue")
    env.update(MISAKA_USAGE_DB=str(cfg["db"]), MISAKA_USAGE_TASK_ID=run["id"],
               MISAKA_USAGE_GENERATION="1", MISAKA_USAGE_TOKEN_CAP="")

    def check_active():
        if runs.stop_requested(con, run["id"]):
            raise InterruptedError("Research stopped.")
        owner = runs.node(con, node["id"])
        if owner["runner_key"] != node["runner_key"] or runs.get(con, run["id"])["driver_lock"] != run["driver_lock"]:
            raise RuntimeError("Research node or driver changed owners.")

    async with _env_window():
        previous = {key: os.environ.get(key) for key in (*env, "MISAKA_NET_PANE", "MISAKA_RESEARCH_NODE")}
        os.environ.update(env)
        # A CLI root runs in its driver, not in ProcessSpawner which clears pane identity.
        os.environ.pop("MISAKA_NET_PANE", None)
        # This runtime is driven here, not by an inherited pane's NodePart.
        os.environ.pop("MISAKA_RESEARCH_NODE", None)
        runtime = bridge = None
        try:
            runtime, session, error = await open_session(flags, run["workspace"], assembly)
            if error:
                raise RuntimeError(error)
            check_active()
            bridge = WindowLO(session, check_active, describe=lambda: node_description(con, node["id"]), headless=True)
            runs.set_node(con, node["id"], session_file=bridge.session_file)
            if runs.is_root(node):
                runs.set_state(con, run["id"], root_session=bridge.session_file, driver_lock=run["driver_lock"])
            control = for_session(session)
            control.check_active = check_active

            async def human_input(text, preflight):
                if session.isStreaming:
                    await session.prompt(text, {"streamingBehavior": "steer", "preflightResult": preflight})
                else:
                    _obj, _answer, error = await bridge._execute(text, {
                        "session_dir": directory,
                        "extra_tools": list(getattr(control, "review_tools", ()) or ()),
                        "usage_db": cfg["db"], "usage_task_id": run["id"], "usage_generation": 1,
                        "usage_token_cap": None,
                    }, preflight=preflight)
                    if error:
                        raise RuntimeError(error)

            control.on_input = human_input
            yield bridge
            # Stop new ingress before draining already accepted human turns. Closing
            # a view never reaches this lifecycle; only the owning node does.
            control.accepting = False
            control.paused = False
            await asyncio.gather(*control.inputs)
            await session.waitForIdle()
        finally:
            async def cleanup():
                try:
                    if bridge is not None:
                        await bridge.close()
                finally:
                    await dispose(runtime)
            try:
                _result, cancelled = await settle(asyncio.create_task(cleanup()))
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            if cancelled is not None:
                raise cancelled

"""Drive all phases through a node's original AgentSession, in its owning event loop."""
from __future__ import annotations

import asyncio
import os
from contextlib import ExitStack, asynccontextmanager

from misaka.agent.request_budget import install_turn_budget
from misaka.core.network import worker
from misaka.core.platform import budget
from misaka.core.platform.session import event_line
from misaka.core.research.report import DRAFT_CONTRACT, FINAL_CONTRACT
from misaka.core.session_control import for_session, wait_for_session
from misaka.utils.values import read_field


class WindowLO:
    """The synchronous planner calls back onto the window's own event loop and session.

    Root uses its existing window; fork LOs use their own resident session in
    their isolated node process. Closing cancels pending callbacks too:
    cancelling asyncio.to_thread alone does not stop its thread.
    """

    def __init__(self, session, check_active, *, headless=False):
        self.session = session
        self.session_file = session.sessionManager.sessionFile
        self.loop = asyncio.get_running_loop()
        self.check_active = check_active
        self.pending = set()
        self.closed = False
        self.turn_lock = asyncio.Lock()
        self.headless = headless

    def run_llm_json(self, _profile, prompt, _provider, _model, **options):
        return asyncio.run_coroutine_threadsafe(self._turn(prompt, options), self.loop).result()

    async def close(self):
        self.closed = True
        pending = list(self.pending)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

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
        reservation = worker._reserve_usage(
            options.get("usage_db"), options.get("usage_task_id"), options.get("usage_generation"),
            options.get("usage_token_cap"), None)
        if not reservation.get("allowed"):
            return None, "", "shared token budget exhausted"
        recorder = worker._UsageRecorder(None, options.get("usage_db"), options.get("usage_task_id"),
                                        options.get("usage_generation"), reservation)
        definitions = list(options.get("extra_tools", ()))
        scope = ExitStack()
        stream = session.agent.streamFn
        previous_limiter = getattr(session.agent, "_misaka_turn_budget", None)
        guard_hooks = None
        limiter = None
        answer, error = None, None

        def observe(event):
            nonlocal answer, error
            recorder(event_line(event))
            if read_field(event, "type") != "message_end":
                return
            message = read_field(event, "message")
            if read_field(message, "role") != "assistant":
                return
            stop = read_field(message, "stopReason")
            if stop in {"error", "aborted", "length"}:
                error = read_field(message, "errorMessage") or f"request {stop}"
            elif stop == "stop" and answer is None:
                # A queued user/notification follow-up belongs to the same window, but
                # must not replace the research phase's completed Markdown.
                answer = "".join(read_field(part, "text", "") for part in read_field(message, "content", [])
                                 if read_field(part, "type") == "text")
                error = None

        unsubscribe = session.subscribe(observe)
        heartbeat = None
        reservation_errors = []

        async def keep_reservation():
            while True:
                await asyncio.sleep(worker.RESERVATION_HEARTBEAT_SECONDS)
                try:
                    alive = await asyncio.to_thread(budget.touch_agent_path, options["usage_db"], reservation["token"], 600)
                    if alive:
                        continue
                    reason = "shared token budget lease lost"
                except Exception as error:  # noqa: BLE001 - a failed heartbeat must stop spending
                    reason = f"shared token budget heartbeat failed: {error}"
                reservation_errors.append(reason)
                await session.abort()
                return

        try:
            names = [*options.get("tools", ()), *(tool.name for tool in definitions)]
            from misaka.core.network.wiring.capabilities import SisterCapabilitiesPart
            scope.enter_context(session.toolScope(names))
            publisher = next((part for part in session.moments.parts if isinstance(part, SisterCapabilitiesPart)), None)
            if publisher is None:
                raise RuntimeError("Research requires the coordinator's system-prompt capability catalog.")
            scope.enter_context(publisher.snapshot(options.get("sister_catalog")))
            session.registerCustomTools(definitions)
            from misaka.core.research.planner import OPTIONAL_MATERIAL_TOOLS
            # Credentials/executables decide availability; missing mandatory tools still fail.
            missing = set(names) - set(session.getActiveToolNames()) - set(OPTIONAL_MATERIAL_TOOLS)
            if missing:
                raise RuntimeError(f"The window is missing research tools: {', '.join(sorted(missing))}")
            if reservation.get("tokens"):
                # Nest inside any existing session cap, then restore the exact provider
                # wrapper. No process-wide environment or session identity is changed.
                if previous_limiter is not None:
                    del session.agent._misaka_turn_budget
                limiter = install_turn_budget(session, int(reservation["tokens"]))
            if self.headless and not hasattr(session.agent, "_misaka_guards"):
                from misaka.agent.guards import install_guards
                from misaka.core.platform.session import BOOKKEEPING_TOOLS

                guard_hooks = (session.agent.shouldStopAfterTurn, session.agent.prepareNextTurnWithContext)
                install_guards(session, limiter, bookkeeping_tools=BOOKKEEPING_TOOLS)
            if reservation.get("token"):
                heartbeat = asyncio.create_task(keep_reservation())
            # Direct await enters the session's active-run guard before yielding. A
            # queued notification cannot take the window between idle and this turn.
            with budget.usage_context(options.get("usage_db"), options.get("usage_task_id"), options.get("usage_generation")):
                if preflight is not None:
                    await session.prompt(prompt, {"streamingBehavior": "steer", "preflightResult": preflight})
                else:
                    await session.sendCustomMessage(
                        {"customType": "research-phase", "display": True, "content": prompt,
                         "details": {"run_id": options.get("usage_task_id"),
                                     "stage": "adjudication_draft" if prompt.startswith((DRAFT_CONTRACT, FINAL_CONTRACT)) else None}},
                        {"triggerTurn": True, "prepareTurn": True})
            self.check_active()
            if reservation_errors:
                raise RuntimeError(reservation_errors[0])
            return None, answer or "", error or (None if answer is not None else "no completed research response")
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            unsubscribe()
            session.agent.streamFn = stream
            if guard_hooks is not None:
                session.agent.shouldStopAfterTurn, session.agent.prepareNextTurnWithContext = guard_hooks
                del session.agent._misaka_guards
            if previous_limiter is not None:
                session.agent._misaka_turn_budget = previous_limiter
            elif hasattr(session.agent, "_misaka_turn_budget"):
                del session.agent._misaka_turn_budget
            try:
                try:
                    session.unregisterCustomTools(definitions)
                finally:
                    scope.close()
            finally:
                try:
                    recorder.settle(limiter.accounted if limiter else None)
                finally:
                    if heartbeat is not None:
                        await asyncio.gather(heartbeat, return_exceptions=True)


# The tool allow-list a fork Last Order's resident session starts with. It gates every tool the
# session can ever activate, including the ones a phase registers for one turn: a name missing
# here is silently filtered out, and the turn that needed it fails as "missing research tools".
# So every tool a phase or a wait can hand her must be listed.
def node_lo_tools():
    from misaka.core.research import planner
    return (*planner.session_tools(None),
            "misaka_research_assign", "misaka_research_investigate",       # phase commands
            "misaka_research_start", "misaka_research_withdraw", "misaka_research_skip",   # the plan-approval wait
            "lcm_grep", "lcm_load_session")                                # this run's conversations


@asynccontextmanager
async def node_session(con, cfg, run, node):
    """One runtime per node execution, including waits for Sisters and red team."""
    from misaka.core.platform.session import _env_window, dispose, open_session
    from misaka.core.research import planner, runs

    directory = planner._lo_session(run, node)
    session_file = run["root_session"] if node["parent_id"] is None else node["session_file"]
    flags, assembly, env = worker.bare_session_setup(
        os.path.join(cfg["roles_root"], "last_order"), cfg["provider"], cfg["default_model"],
        cwd=run["workspace"], tools=node_lo_tools(), soul=False,
        session_dir=directory, session_file=session_file,
        continue_session=True, thinking="high", research_context=True)
    env.update(MISAKA_USAGE_DB=str(cfg["db"]), MISAKA_USAGE_TASK_ID=run["id"],
               MISAKA_USAGE_GENERATION="1", MISAKA_USAGE_TOKEN_CAP=str(cfg.get("token_cap") or 0))

    def check_active():
        if runs.stop_requested(con, run["id"]):
            raise InterruptedError("Research stopped.")
        owner = runs.node(con, node["id"])
        if owner["runner_key"] != node["runner_key"] or runs.get(con, run["id"])["driver_lock"] != run["driver_lock"]:
            raise RuntimeError("Research node or driver changed owners.")

    async with _env_window():
        previous = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        runtime = bridge = None
        try:
            runtime, session, error = await open_session(flags, run["workspace"], assembly)
            if error:
                raise RuntimeError(error)
            check_active()
            bridge = WindowLO(session, check_active, headless=True)
            runs.set_node(con, node["id"], session_file=bridge.session_file)
            if node["parent_id"] is None:
                runs.set_state(con, run["id"], root_session=bridge.session_file, driver_lock=run["driver_lock"])
            control = for_session(session)
            control.check_active = check_active
            control.describe = lambda: {"node": node["id"], "depth": node["depth"],
                                        "phase": runs.node(con, node["id"])["status"]}

            async def human_input(text, preflight):
                if session.isStreaming:
                    await session.prompt(text, {"streamingBehavior": "steer", "preflightResult": preflight})
                else:
                    _obj, _answer, error = await bridge._execute(text, {
                        "session_dir": directory, "tools": planner.session_tools(bridge),
                        "extra_tools": list(getattr(control, "review_tools", ()) or ()),
                        "usage_db": cfg["db"], "usage_task_id": run["id"], "usage_generation": 1,
                        "usage_token_cap": cfg.get("token_cap"),
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
            if bridge is not None:
                await bridge.close()
            await dispose(runtime)
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

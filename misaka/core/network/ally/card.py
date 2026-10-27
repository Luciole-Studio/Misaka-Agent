"""One attempt at an ally's card: misaka drives the ally over ACP.

An ally is an executor on the board as a Sister is: the same claim, lease, contract, completion,
submission, notices and mail. What differs is who runs the turns -- the ally's own agent, reached
over ACP (``acp``), with the card's tools lent to it by the MCP bridge (``bridge``). This is the
host side: one ACP session per attempt, its turns written as the card's pi transcript
(``transcript``), and the card settled by the rules a Sister's session settles it by
(``todo.TodoPart``):

* a turn that ends after ``misaka_card_complete`` submits the card;
* a submission missing what the card's kind requires gets one more turn to supply it;
* a turn that ends without the call leaves the card running, said once per attempt;
* a cut reply gets one more turn; an error, a refusal, a dead agent, or a turn with nothing in it
  (a failed login shows only on the agent's stderr) fails the attempt.

ACP has no way into a running turn, so what arrives meanwhile -- a line typed in the pane, mail --
waits for the turn to end and goes in as the next one; a turn is never cancelled to deliver a
message (raven's rule). Ctrl-C in the pane cancels the turn and the card stays running.

Two hosts: ``misaka ally-card`` in a daemon pane (the daemon claimed the card and hands the claim
over in the environment, as for ``card-shell``), and ``dispatch.run_task`` in process for a headless
workflow. In process nobody types, so an attempt that goes idle without completing is over and
its dispatcher settles it.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import threading
from types import SimpleNamespace

from misaka.config import CFG, home
from misaka.config import sessions as session_roots
from misaka.core.network import messages, worker
from misaka.core.network.ally import presets
from misaka.core.network.ally.acp import (
    CLIENT_CAPABILITIES,
    PROTOCOL_VERSION,
    AcpClient,
    AcpError,
    AcpRemoteError,
    choose_permission,
)
from misaka.core.network.ally.presets import START_SECONDS
from misaka.core.network.ally.transcript import Transcript
from misaka.core.platform import tasks as db

PROGRESS_SECONDS = 60       # how often a busy ally stamps progress on its card (as a Sister does)
UNSETTLED_GRACE_SECONDS = 30   # how long a card told it is still running may wait with nothing typed or mailed
CANCEL_SECONDS = 10         # how long a cancelled turn has to end before its agent is stopped
PREVIEW_CHARS = 160

EARLIER = ("This card was worked on before, in a conversation you do not have. It is recorded at `{path}`; "
           "read it where you need what was said or done there.")


class _Over(Exception):
    """The attempt ends with nothing left for it to settle."""


class _Failed(Exception):
    """The attempt ends in a failure the card records."""

    def __init__(self, reason, kind=None):
        super().__init__(reason)
        self.kind = kind


def _text(content):
    return str(content.get("text") or "") if isinstance(content, dict) and content.get("type") == "text" else ""


def _preview(text):
    lines = text.strip().splitlines() or [""]
    first = lines[0][:PREVIEW_CHARS]
    return first + (" …" if len(lines) > 1 or len(lines[0]) > PREVIEW_CHARS else "")


class Attempt:
    """One claimed attempt at an ally's card. ``out`` is the pane's terminal; None in process.
    With ``consult`` -- ``(run_id, lock)`` -- it is instead a finished card answering a message
    (``tasks.start_consultation``): no claim, and the card stays as it is."""

    def __init__(self, task, generation, claim_lock, *, db_path, out=None, consult=None):
        self.task = task
        self.task_id, self.name = task["id"], task["assignee"]
        self.workspace = db.workspace_for(task)
        self.generation, self.claim_lock = int(generation), claim_lock
        self.consult = consult
        self.db_path, self.out = os.path.expanduser(db_path), out
        self.space = os.environ.get("MISAKA_NET_SPACE") or None
        self.con = db.connect(db_path)
        self.mail = None
        self.client = None
        self.transcript = None
        self.earlier = None             # the card's transcript from before, when the agent does not have it
        self.acp_session = None
        self.parts = []
        self.busy = False               # a turn is running
        self.replaying = False          # ``session/load`` is replaying history, which is dropped
        self.stopping = False
        self.typed = []                 # lines typed in the pane, waiting for the next turn
        self.wake = asyncio.Event()     # something arrived for an idle session
        self._progressed = False        # an update came since the last progress stamp
        self._midline = False
        self._held = self._nudged = self._continued = False
        self._idle_ends = False         # the wait after this turn ends the attempt unsettled (``idle``)

    # -- the pane --

    def _print(self, text):
        if self.out is not None:
            self.out.write(("\n" if self._midline else "") + text + "\n")
            self.out.flush()
            self._midline = False

    def _stream(self, chunk):
        if self.out is not None and chunk:
            self.out.write(chunk)
            self.out.flush()
            self._midline = not chunk.endswith("\n")

    def typed_line(self, text):
        text = text.strip()
        if not text:
            return
        self.typed.append(text)
        if self.busy:
            self._print(f"[queued: {self.name} reads it when this turn ends]")
        self.wake.set()

    def interrupt(self):
        """Ctrl-C: end the running turn; the card keeps running and the session waits."""
        if self.busy:
            self._print("[cancelling this turn]")
            asyncio.ensure_future(self._cancel())
        else:
            self._print(f"[{self.name} is idle; close the pane or stop the card to end it]")

    def leave(self):
        """The attempt ends now: the pane is closing, or the card left this claim."""
        if self.stopping:
            return
        self.stopping = True
        self.wake.set()
        if self.busy:
            asyncio.ensure_future(self._cancel(force=True))

    async def _cancel(self, force=False):
        with contextlib.suppress(AcpError):
            await self.client.notify("session/cancel", {"sessionId": self.acp_session})
        if force:
            await asyncio.sleep(CANCEL_SECONDS)
            if self.busy:
                await self.client.close()   # the pending prompt fails and the attempt unwinds

    # -- the agent --

    def _update(self, params):
        if self.replaying or params.get("sessionId") != self.acp_session:
            return
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            chunk = _text(update.get("content"))
            self.transcript.text(chunk)
            self._stream(chunk)
        elif kind == "agent_thought_chunk":
            self.transcript.thought(_text(update.get("content")))
        elif kind == "tool_call":
            self.transcript.tool_call(update)
            self._print(f"· {update.get('title') or update.get('kind') or 'tool'}")
            self.transcript.tool_update(update)        # a call reported already finished
        elif kind == "tool_call_update":
            self.transcript.tool_update(update)
            if update.get("status") == "failed":
                self._print(f"  ✗ {update.get('title') or 'the call'} failed")
        elif kind == "plan":
            self.transcript.plan(update)
        else:
            return
        self._progressed = True

    async def _request(self, method, params):
        if method == "session/request_permission":
            return choose_permission(params.get("options"))
        raise NotImplementedError(method)

    def _bridge(self):
        """The MCP server the ally starts for the card's tools, told who it serves."""
        env = {home.ENV_HOME: str(home.home()),
               "MISAKA_USAGE_DB": self.db_path,
               "MISAKA_USAGE_TASK_ID": self.task_id,
               "MISAKA_USAGE_GENERATION": str(self.generation),
               **({worker.CONSULT_RUN_ENV: self.consult[0], worker.CONSULT_LOCK_ENV: self.consult[1]}
                  if self.consult is not None else {"MISAKA_USAGE_CLAIM_LOCK": self.claim_lock}),
               "MISAKA_ALLY_SESSION": self.transcript.session_id,
               "MISAKA_WORKSPACE": self.workspace}
        if self.space:
            env["MISAKA_NET_SPACE"] = self.space
        return {"name": "misaka", "command": sys.executable, "args": ["-m", "misaka", "ally-bridge"],
                "env": [{"name": key, "value": value} for key, value in env.items()]}

    async def open(self):
        """Start the agent and its session: the card's own ACP session when the agent can load it."""
        ally = presets.get(self.name)
        if ally is None:
            raise _Failed(f"{self.name} is not an enabled ally (settings.json `allies`).", "configuration")
        if why := presets.unavailable(ally):
            raise _Failed(f"Ally {self.name} cannot start: {why}.", "configuration")
        row = await asyncio.to_thread(self._owned)
        if row is None:
            raise _Over
        from misaka.core.session_manager import SessionManager

        # The card's transcript goes on in the same file, whichever ACP session writes it.
        session_dir = session_roots.card_session_dir(self.task)
        previous = worker.continue_flags(self.task.get("session_file"), session_dir)
        manager = (SessionManager.open(previous[1]) if previous
                   else SessionManager.create(self.workspace, session_dir))
        self.transcript = Transcript(manager, self.name)

        self.client = AcpClient(ally.command, cwd=self.workspace, env=presets.environment(ally),
                                on_update=self._update, on_request=self._request, detach=self.out is not None)
        try:
            await self.client.start()
        except OSError as error:
            raise _Failed(f"Ally {self.name} did not start: {error}", "configuration") from error
        try:
            init = await self.client.request(
                "initialize", {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": CLIENT_CAPABILITIES},
                timeout=START_SECONDS) or {}
            servers = [self._bridge()]
            loaded = False
            if self.task.get("agent_id") and (init.get("agentCapabilities") or {}).get("loadSession"):
                self.replaying = True
                try:
                    await self.client.request("session/load", {
                        "sessionId": self.task["agent_id"], "cwd": self.workspace, "mcpServers": servers},
                        timeout=START_SECONDS)
                    self.acp_session, loaded = self.task["agent_id"], True
                except AcpRemoteError:
                    pass                  # the agent no longer has it: a new one, told where the old one is
                finally:
                    self.replaying = False
            if not loaded:
                opened = await self.client.request(
                    "session/new", {"cwd": self.workspace, "mcpServers": servers}, timeout=START_SECONDS) or {}
                self.acp_session = opened.get("sessionId")
                if not self.acp_session:
                    raise AcpError("session/new answered without a sessionId")
        except (AcpError, TimeoutError) as error:
            raise _Failed(f"Ally {self.name} did not open a session: {error or 'timed out'}"
                          + self._stderr()) from error
        if previous and not loaded:
            self.earlier = previous[1]

        def record():
            db.set_runtime(self.con, self.task_id, self.acp_session, manager.getSessionFile(),
                           generation=self.generation, claim_lock=self.claim_lock)
            worker.record_output_baseline(self.con, row)
        if self.consult is None:           # a consultation changes nothing the claim fences
            await asyncio.to_thread(record)

        from misaka.core import session_catalog
        from misaka.core.network.wiring import panel
        from misaka.core.wiring import SessionSpec

        spec = SessionSpec(profile_dir="", role=self.name, workspace=self.workspace, kind="card",
                           sender=self.name, receive_messages=True, task_id=self.task_id)
        catalog = session_catalog.part(spec)
        catalog.steer = False               # mail waits for the turn to end; SendMessage says so
        self.parts = [catalog, *filter(None, [panel.part(spec)])]
        await self._announce("session_start")
        return loaded

    def _stderr(self):
        tail = self.client.stderr_tail if self.client is not None else ""
        return f"\n{tail[-2000:]}" if tail else ""

    async def _announce(self, hook):
        """The catalog record and the pane's state, as a session's own parts publish them."""
        ctx = SimpleNamespace(sessionManager=self.transcript.manager, isIdle=lambda: not self.busy)
        for part in self.parts:
            with contextlib.suppress(Exception):   # a record or a status ping never stops the card
                await getattr(part, hook)(None, ctx)

    # -- the board --

    def _owned(self):
        """The card, its lease renewed, while this attempt still holds it; else None. For a
        consultation: the finished card, while the consultation runs."""
        if self.consult is not None:
            run = db.get_run(self.con, self.consult[0])
            row = db.get(self.con, self.task_id)
            if (run is None or run["status"] != "running" or run["claim_lock"] != self.consult[1]
                    or row is None or row["status"] not in db.CONSULTABLE
                    or int(row["generation"]) != self.generation):
                return None
            db.heartbeat_consultation(self.con, *self.consult)
            return row
        return worker.owned_card(self.con, self.task_id, self.generation, self.claim_lock)

    def _still_mine(self):
        if self.consult is not None:
            return self._owned() is not None
        row = db.get(self.con, self.task_id)
        return (row is not None and row["status"] == "running" and int(row["generation"]) == self.generation
                and row["claim_lock"] == self.claim_lock)

    def _take_mail(self):
        """This card's queued mail, taken for the next turn, rendered; None when there is none."""
        if self.mail is None:
            self.mail = messages.connect()
        rows = messages.pending(self.mail, self.name, session_id=self.transcript.session_id, task_id=self.task_id)
        deliverable, discard = messages.delivery_plan(rows, self.db_path, task_generation=self.generation)
        token = messages.new_lease_token()
        won = messages.claim(self.mail, list(deliverable | discard), token=token)
        messages.ack(self.mail, list(won), token=token)
        mine = [row for row in rows if row["id"] in won and row["id"] in deliverable]
        return messages.render_mail(mine) if mine else None

    def _fail(self, reason, kind):
        if self.consult is not None:
            messages.settle_consultation(self.con, *self.consult, "failed", reason=reason)
            return
        if db.add_event(self.con, self.task_id, "failed", {"reason": reason},
                        generation=self.generation, claim_lock=self.claim_lock):
            db.mark_failed(self.con, self.task_id, generation=self.generation, claim_lock=self.claim_lock,
                           failure_kind=kind, reason=reason)

    def _submit(self, row, summary):
        """The Sister's finalizer: build the submission, freeze it, land it, index it."""
        from misaka.core.network import dispatch

        submission = worker.build_submission(self.con, row, summary)
        prepared = dispatch.prepare_submission(row, submission)
        if dispatch.accept_state(self.con, row, prepared, generation=row["generation"],
                                 claim_lock=row["claim_lock"]):
            dispatch.accept_side_effects(self.con, row, submission, generation=row["generation"])
            return True
        return False

    async def _stamp(self):
        """While a turn runs: progress on the card once a minute when the agent is getting somewhere
        (an update came, or a tool call is still open). A card that has left this claim -- stopped,
        reclaimed -- ends the attempt."""
        while True:
            await asyncio.sleep(PROGRESS_SECONDS)
            if not (self._progressed or self.transcript.calls_open):
                continue
            self._progressed = False
            with contextlib.suppress(Exception):   # a busy board skips one stamp, not the rest
                if await asyncio.to_thread(self._owned) is None:
                    self.leave()
                    return

    # -- turns --

    async def turn(self, text):
        """One ACP turn; returns ``(stopReason, error)``."""
        self.transcript.user(text)
        self._print(f"› {_preview(text)}")
        self.busy, self._progressed, self._idle_ends = True, True, False
        await self._announce("agent_start")
        stamper = asyncio.create_task(self._stamp())
        error = None
        try:
            result = await self.client.request(
                "session/prompt", {"sessionId": self.acp_session, "prompt": [{"type": "text", "text": text}]})
            stop = (result or {}).get("stopReason") or "end_turn"
        except AcpError as failure:
            stop, error = "error", str(failure)
        finally:
            self.busy = False
            stamper.cancel()
        self.transcript.end(stop, error)
        if self._midline:
            self._print("")
        await self._announce("agent_settled")
        return stop, error

    async def after(self, stop, error):
        """What follows a turn: the next turn's text, or None to wait. Raises when the attempt ends."""
        if self.stopping:
            raise _Over
        if error:
            raise _Failed(f"Ally {self.name}: {error}" + self._stderr())
        if stop == "refusal":
            raise _Failed(f"Ally {self.name} refused the turn." + self._stderr())
        if stop == "cancelled":
            return self._typed()
        if stop in ("max_tokens", "max_turn_requests"):
            if self._continued:
                raise _Failed(f"Ally {self.name}'s reply was cut off twice ({stop}).")
            self._continued = True
            return self._typed() or (f"Your last reply was cut off ({stop}). Continue the card from where it "
                                     "stopped, writing long material in several smaller steps.")
        if not self.transcript.produced:
            raise _Failed(f"Ally {self.name} ended the turn without saying or doing anything." + self._stderr())
        if text := self._typed() or await asyncio.to_thread(self._take_mail):
            return text
        row = await asyncio.to_thread(self._owned)
        if row is None:
            raise _Over                   # stopped, parked for input, or reclaimed
        if self.consult is not None:
            # Answered. What the turn changed is declared again (a Sister's _redeclare_if_done).
            from misaka.core.network import dispatch
            if row["status"] == "done":
                await asyncio.to_thread(dispatch.redeclare, self.con, row)
            await asyncio.to_thread(messages.settle_consultation, self.con, *self.consult, "done")
            self._print(f"[card {self.task_id} answered]")
            raise _Over
        summary = await asyncio.to_thread(worker.declared_completion, self.con, row)
        if summary is not None:
            try:
                await asyncio.to_thread(self._submit, row, summary or self.transcript.last_text)
            except worker.IncompleteSubmission as incomplete:
                if self._nudged:
                    raise _Failed(f"finalizer: {incomplete}", "crash") from incomplete
                self._nudged = True
                return (f"Submission incomplete: {incomplete} Then call misaka_card_complete again and end the "
                        "turn with the plain-text summary.")
            except Exception as fault:
                raise _Failed(f"finalizer: {fault}", "crash") from fault
            self._print(f"[card {self.task_id} submitted]")
            raise _Over
        if self._held:
            self._idle_ends = True
            return None
        self._held = True
        missing = worker.missing_deliverable(row)
        detail = (f"its deliverable `{missing}` is not under `{row['output_dir']}` yet"
                  if missing else "the turn ended without `misaka_card_complete`")
        return (f"[Card still running] The card is still running: {detail}. Call `misaka_card_complete` when "
                "the work is done; if this turn only answered a message, keep working.")

    def _typed(self):
        text, self.typed = "\n".join(self.typed), []
        self.wake.clear()
        return text or None

    async def idle(self):
        """Wait for the next turn's text: a typed line or mail. None ends the attempt. After the
        card's one notice that it is still running, a wait that brings neither for
        ``UNSETTLED_GRACE_SECONDS`` ends the attempt unsettled, as in process (``dispatch._run_ally``)
        and as a Sister's pane does (``todo.TodoPart._watch_idle``)."""
        if self.out is None:
            return await asyncio.to_thread(self._take_mail)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + UNSETTLED_GRACE_SECONDS if self._idle_ends else None
        while not self.stopping:
            if text := self._typed() or await asyncio.to_thread(self._take_mail):
                return text
            if not await asyncio.to_thread(self._still_mine):
                return None
            if deadline is not None and loop.time() >= deadline:
                row = await asyncio.to_thread(self._owned)
                if row is None:
                    return None
                raise _Failed(worker.unsettled_reason(row), "protocol_violation")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), messages.POLL_SECONDS)
        return None

    async def run(self, say=None):
        try:
            loaded = await self.open()
            if self.out is not None:
                self._print(f"Ally {self.name} · card {self.task_id}: {self.task['title']}\n"
                            "Type a line to send it; Ctrl-C cancels the running turn.")
            text = _first_prompt(self.task, loaded=loaded, say=say, earlier=self.earlier)
            while text is not None and not self.stopping:
                text = await self.after(*await self.turn(text))
                if text is None:
                    text = await self.idle()
        except _Over:
            pass
        except _Failed as failure:
            self._print(f"[card {self.task_id} failed: {failure}]")
            await asyncio.to_thread(self._fail, str(failure), failure.kind)
        finally:
            await self.close()

    async def close(self):
        if self.client is not None:
            await self.client.close()
        if self.transcript is not None:
            await self._announce("session_shutdown")
        for con in (self.con, self.mail):
            if con is not None:
                con.close()


def _first_prompt(task, *, loaded, say, earlier):
    """A message to a session that has the card goes in alone, as ``card-shell --resume --say``
    sends it; otherwise the contract, after the conversation the agent does not have, if any."""
    if loaded and say is not None:
        return say
    contract = worker.card_prompt(task)
    if task.get("_owner_context"):
        # A Sister has this in her system prompt; ACP has none, so it opens the conversation.
        from misaka.core.platform import owners
        owner = owners.named(task.get("_owner"))
        if owner is not None:
            contract = owner.card_discipline(task["_owner_context"]) + "\n" + contract
    if earlier:
        contract = EARLIER.format(path=earlier) + "\n\n" + contract
    return contract + (f"\n\n## Message\n{say}" if say is not None else "")


async def run_attempt(task, generation, claim_lock, *, db_path, say=None, out=None, consult=None):
    """Run one claimed attempt at an ally's card until it settles, fails, or leaves this claim.
    ``task`` is the card as ``worker.card_prompt`` reads it; ``say`` continues the card with a
    message; ``out`` is the pane's terminal (None in process), whose keys reach the attempt.
    ``consult`` -- ``(run_id, lock)`` -- runs a finished card's answer to a message instead."""
    attempt = Attempt(task, generation, claim_lock, db_path=db_path, out=out, consult=consult)
    if out is not None:
        loop = asyncio.get_running_loop()
        if sys.platform == "win32":
            # A Windows event loop takes no signal handlers, and ctrl+c is the one signal a
            # console program gets: closing the pane's console ends it outright.
            signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(attempt.interrupt))
        else:
            loop.add_signal_handler(signal.SIGINT, attempt.interrupt)  # windows-footgun: ok - POSIX branch
            for signum in (signal.SIGTERM, signal.SIGHUP):  # windows-footgun: ok - POSIX branch
                loop.add_signal_handler(signum, attempt.leave)  # windows-footgun: ok - POSIX branch
        _read_lines(loop, attempt.typed_line)
    await attempt.run(say)


def _read_lines(loop, deliver):
    """Lines typed in the pane (its terminal is in line mode), each handed to ``deliver``."""
    if sys.platform == "win32":
        # A console cannot be watched by the event loop: a thread reads its lines.
        def pump():
            for line in sys.stdin:
                loop.call_soon_threadsafe(deliver, line.rstrip("\r\n"))

        threading.Thread(target=pump, name="pane-lines", daemon=True).start()
        return
    try:
        fd = sys.stdin.fileno()
    except (OSError, ValueError):
        return
    pending = bytearray()

    def ready():
        try:
            data = os.read(fd, 4096)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(fd)
            return
        pending.extend(data)
        *lines, rest = pending.split(b"\n")
        pending[:] = rest
        if lines:
            deliver(b"\n".join(lines).decode("utf-8", errors="replace"))

    with contextlib.suppress(OSError, ValueError):   # no terminal to read (a stdin kqueue refuses)
        loop.add_reader(fd, ready)


def launch(task_id, say=None, consult=False):
    """``misaka ally-card``: the daemon claimed the card for this pane (``MISAKA_USAGE_*``), or
    with ``consult`` opened a consultation of the finished card (``MISAKA_CONSULT_*``)."""
    from misaka.core.platform import cards as card_files

    with contextlib.closing(db.connect(CFG["db"])) as con:
        row = db.get(con, task_id)
        if row is None:
            sys.exit(f"Card not found: {task_id}")
        task = {**dict(row), "_handoffs": worker.card_handoffs(con, row), **worker.card_extras(con, row)}
    consultation = None
    if consult:
        generation, run_id, lock = worker.consult_env(task_id)
        with contextlib.closing(db.connect(CFG["db"])) as con:
            run = db.get_run(con, run_id) if run_id else None
        if (not say or run is None or run["status"] != "running" or generation != int(task["generation"])
                or task["status"] not in db.CONSULTABLE):
            sys.exit(f"Card {task_id}: no consultation of this finished card is open for this pane.")
        claim_lock, consultation = None, (run_id, lock)
    else:
        generation, claim_lock = worker.claim_env(task_id)
        if generation is None or task["claim_lock"] != claim_lock or int(task["generation"]) != generation:
            sys.exit(f"Card {task_id} is not claimed by this pane; the daemon owns the claim.")
    workspace = db.workspace_for(task)
    if not os.path.isdir(workspace):
        sys.exit(f"Card {task_id}: its folder {workspace} no longer exists.")
    task["_attachments"] = card_files.attachment_list(workspace, task_id, workspace=workspace)
    os.chdir(workspace)
    from misaka.utils import loop_watchdog
    loop_watchdog.configure(f"card-{task_id}", exit_on_stall=True)
    asyncio.run(loop_watchdog.watched(
        run_attempt(task, generation, claim_lock, db_path=CFG["db"], say=say, out=sys.stdout,
                    consult=consultation)))

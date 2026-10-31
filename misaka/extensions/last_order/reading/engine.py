"""A reading's operations: open it, do one action on a node, fork a node into lines, compare
lines, reread an earlier node. The same operations serve ``misaka read``, ``/read`` and the tools
Last Order uses as the reading's leader.

An action is done by a study reader: an in-process session with MISAKA's document tools and a
``reading_submit`` tool, given the method of that action and what the context mode lets it see.

- ``continue`` deepens the node in the node's own session: a fork of it, which becomes the node's
  session when the action is recorded, so an action stopped or refused leaves nothing in it.
- ``independent`` starts a successor in a fresh session that sees the material, the question and
  the lens, not other lines' conclusions: independence is a new session, not a shorter prompt.
- ``compare`` hands one fresh session the full notes of the compared nodes.
- ``reread`` hands one fresh session the earlier node's note and material with the newer notes.

An operation holds the reading's lease while it runs and writes the graph once, when its readers
have answered -- the sections they wrote with it -- so a failed or stopped action leaves nothing
half written; the project's files are then rendered from the graph, still under the lease.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from typing import Any

from . import materials, prompts, store, views
from .store import ReadingError

EXCERPT_CHARS = 8_000          # the opening window of each material a study reader is handed
EXCERPT_SOURCES = 3
PRIOR_CHARS = 12_000           # a continued node's own note; compare and reread carry notes whole
DOC_TOOLS = ("doc_outline", "doc_read", "doc_find", "doc_verify", "doc_page_image")
HOW = {"independent": "fork", "compare": "compare", "reread": "reread"}


class Progress:
    """What an operation reports while it runs; the default reports nothing."""

    def text(self, role, node_id, delta):
        pass

    def note(self, line):
        pass

    def changed(self, run_id, node_ids):
        pass


QUIET = Progress()


def lo_profile():
    from misaka.config import home

    return str(home.path("roles_root") / "last_order")


# -- opening -------------------------------------------------------------------------------------


async def open_reading(sources, question, *, workspace, intent="", runs_dir=None, progress=QUIET):
    """Add the materials and make the reading with its entry node ``N0``. No model is called."""
    if not sources:
        raise ReadingError("A reading needs at least one material.")
    workspace = os.path.realpath(workspace)
    con = store.connect()
    parent = os.path.realpath(runs_dir) if runs_dir else os.path.join(workspace, "reading")
    run_id = store.new_run_id(con, question, parent)
    directory = os.path.join(parent, run_id)
    os.makedirs(directory)
    try:
        # The folder's layout from the start, as PR #12 lays it out; sources/ when copies go there.
        inbox = materials.inbox(workspace, directory)
        for part in (os.path.join(directory, "nodes"), os.path.join(directory, "sessions"),
                     *([inbox] if os.path.dirname(inbox) == directory else [])):
            os.makedirs(part, exist_ok=True)
        records = []
        for source in sources:
            progress.note(f"adding {source}")
            record, copy = await materials.add(source, workspace, directory)
            if any(known["doc_id"] == record["doc_id"] for known in records):
                # The same material twice (one text, two names): one material, and no second copy.
                progress.note(f"  {record['title']} is already in this reading")
                if copy:
                    os.remove(copy)
                continue
            progress.note(f"  {record['title']}: {record['pages']} pages, "
                          f"{'an outline' if record['outline'] else 'no outline'}")
            records.append(record)
        run = store.create(con, run_id, question=question, intent=intent, workspace=workspace,
                           directory=directory, sources=records)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    with run.hold():
        render(run, progress)
    return run


def find(run_id):
    return store.Run(store.connect(), run_id)


def write(run, op, payload, apply, progress=QUIET):
    """A small change outside an operation (binding the leader, say), rendered under the same lease."""
    with run.hold() as token:
        run.commit(token, op, payload, apply)
        render(run, progress)


def forget(path):
    """Remove a study session no recorded action refers to. Tidying only: a file another program
    holds open for a moment (Windows) stays, rather than turning what was recorded into a failure."""
    try:
        if path:
            os.remove(path)
    except OSError:
        pass


def render(run, progress=QUIET):
    """Rewrite the reading's files from its graph. They are derived, so one that cannot be written
    (held open by a viewer, a full disk) is reported, and what the graph recorded still stands."""
    try:
        views.render(run)
    except Exception as error:  # noqa: BLE001 - derived files; what the graph recorded stands either way
        progress.note(f"could not update the reading's files: {type(error).__name__}: {error}")


# -- models --------------------------------------------------------------------------------------


async def model_runtime():
    """The models and credentials of the home, as a session made without one would load them."""
    from misaka.config import home
    from misaka.core.model_runtime import ModelRuntime

    return await ModelRuntime.create({"authPath": None, "modelsPath": str(home.path("models"))})


def choose_model(runtime, reference=None, current=None):
    """``reference`` (provider/id or an id), else the window's model, else Last Order's pin, else the
    global default. A model without credentials is refused rather than swapped for another."""
    from misaka.config import current_config, profiles

    def resolve(wanted, fallback_provider=None):
        try:
            provider, _, model_id = profiles.resolve_model_reference(
                wanted, runtime, fallback_provider=fallback_provider).partition("/")
        except ValueError as error:
            raise ReadingError(f"{error}; pass --model provider/id, as /model lists them.") from error
        return runtime.getModel(provider, model_id)

    if reference:
        model = resolve(reference)
    elif current is not None:
        model = current
    else:
        config = current_config()
        model = resolve(profiles.pinned_model(lo_profile()) or f"{config['provider']}/{config['default_model']}",
                        config["provider"])
    if not runtime.hasConfiguredAuth(model.provider):
        raise ReadingError(f"No configured authentication for {model.provider}/{model.id}; use misaka setup.")
    return model


# -- one study reader ----------------------------------------------------------------------------


@dataclass
class Result:
    summary: str
    understanding: str
    concepts: list[dict[str, str]]
    refs: list[dict[str, str]]
    relations: list[dict[str, str]]
    questions: list[str]
    session: str | None = None
    forked_from: str | None = None     # the session this one continues; None for a fresh one
    prose: bool = False


def _submit_tool(sink):
    from typing import Literal

    from pydantic import BaseModel, Field

    from misaka.ai.utils.typebox_helpers import typeboxSchema
    from misaka.core.extensions.types import ToolDefinition

    class Concept(BaseModel):
        term: str = Field(description="A concept or term.")
        note: str = Field("", description="What it means and what work it does in this reading.")

    class Place(BaseModel):
        doc_id: str = Field(description="The material's doc_id, as the materials list gives it.")
        node: str = Field("", description="Outline node id, when the place is a section.")
        pages: str = Field("", description="A page or range, such as 32 or 32-40.")

    class Relation(BaseModel):
        to: str = Field(description="An existing node of this reading, such as N1.")
        kind: Literal[store.RELATIONS] = Field(description="How this reading relates to that node's.")
        explanation: str = Field(description="Where exactly the relation lies.")

    class Submit(BaseModel):
        summary: str = Field(description="One sentence: where this action took the understanding.")
        understanding: str = Field(description="The reading as Markdown a reader can follow on its own; quotations "
                                               "name their material and section or pages.")
        concepts: list[Concept] = Field(default_factory=list, description="Concepts this reading turns on; "
                                                                          "no need to be complete.")
        sources: list[Place] = Field(default_factory=list, description="The places in the material the reading "
                                                                       "actually cites.")
        relations: list[Relation] = Field(default_factory=list, description="What this reading says about other "
                                                                            "nodes' readings; it does not order "
                                                                            "the reading.")
        questions: list[str] = Field(default_factory=list, description="Questions this reading leads to.")

    async def execute(tool_call_id, params, signal, on_update, ctx):
        sink["value"] = params if isinstance(params, Submit) else Submit(**(params or {}))
        return {"content": [{"type": "text", "text": "Reading received."}], "details": {}}

    return ToolDefinition(name="reading_submit", label="Submit the reading", description=prompts.SUBMIT_DESCRIPTION,
                          parameters=typeboxSchema(Submit), execute=execute)


def _stop_reason(session):
    """Why the last answer ended, when that says more than "no text": a reasoning model that spent
    its whole output thinking stops at ``length`` with nothing written."""
    from misaka.utils.values import read_field

    for message in reversed(list(session.messages)):
        if read_field(message, "role") != "assistant":
            continue
        if read_field(message, "stopReason") == "length":
            return "the answer hit the output limit"
        if read_field(message, "errorMessage"):
            return f"the provider reported: {read_field(message, 'errorMessage')}"
        reason = read_field(message, "stopReason")
        return f"stopReason={reason}" if reason else ""
    return ""


class Reader:
    """Runs a reading's operations on one model."""

    def __init__(self, model, runtime, progress=QUIET):
        self.model, self.runtime, self.progress = model, runtime, progress

    async def _study(self, run, node_id, role, prompt, session_file, fallback_refs):
        from misaka.core.documents.wiring import documents
        from misaka.core.resource_loader import DefaultResourceLoader
        from misaka.core.sdk import createAgentSession
        from misaka.core.session_manager import SessionManager
        from misaka.core.wiring import ToolCollector
        from misaka.utils.values import read_field

        collector = ToolCollector()
        documents.register(collector)
        sink: dict[str, Any] = {}
        tools = [tool for tool in collector.tools if tool.name in DOC_TOOLS] + [_submit_tool(sink)]
        workspace, sessions = run.workspace, run.sessions_directory()
        loader = DefaultResourceLoader({"cwd": workspace, "agentDir": lo_profile(), "noExtensions": True,
                                        "noContextFiles": True,
                                        "systemPrompt": prompts.study_system_prompt(compare=role == "compare")})
        await loader.reload()
        manager, forked_from = None, None
        if session_file and os.path.exists(session_file):
            # A fork, not the file itself: the node keeps its session until this action is recorded.
            try:
                manager, forked_from = SessionManager.forkFrom(session_file, workspace, sessions), session_file
            except Exception as error:  # noqa: BLE001 - a damaged session must not take the reading with it
                self.progress.note(f"could not reopen {session_file} ({error}); starting a new session")
        manager = manager or SessionManager.create(workspace, sessions)
        session, unsubscribe, kept, written = None, None, False, []

        def listen(event):
            if read_field(event, "type") != "message_update":
                return
            inner = read_field(event, "assistantMessageEvent")
            if read_field(inner, "type") == "text_delta" and read_field(inner, "delta"):
                written.append(read_field(inner, "delta"))
                self.progress.text(role, node_id, read_field(inner, "delta"))

        # From here the session's file is this action's: kept only if it returns a reading.
        try:
            built = await createAgentSession({"cwd": workspace, "agentDir": lo_profile(), "noTools": "builtin",
                                              "tools": [tool.name for tool in tools], "customTools": tools,
                                              "resourceLoader": loader, "sessionManager": manager,
                                              "model": self.model, "modelRuntime": self.runtime})
            session = built["session"]
            # Nobody watches a study reader turn by turn: MISAKA's guards against a session repeating
            # itself or spinning on its tools stand in, as for every headless session (platform.session).
            from misaka.agent.guards import install_guards
            from misaka.core.platform import metering
            from misaka.core.platform.vocabulary import NO_PROGRESS_VOCABULARY

            install_guards(session, metering.allowance, no_progress_vocabulary=NO_PROGRESS_VOCABULARY)
            unsubscribe = session.subscribe(listen)
            try:
                await session.prompt(prompt)
                await session.agent.waitForIdle()
            except asyncio.CancelledError:
                await session.abort()
                raise
            last = next((m for m in reversed(list(session.messages)) if read_field(m, "role") == "assistant"), None)
            if last is not None and read_field(last, "stopReason") == "error":
                raise ReadingError(f"{node_id}: {_stop_reason(session)}")
            text = "".join(written).strip()
            if sink.get("value") is not None:
                result = _normalize(sink["value"], run, fallback_refs)
            elif text:
                # The reader answered in prose: still its reading of the material, so it is kept.
                self.progress.note(f"{node_id}: the reader answered in prose instead of calling reading_submit")
                first = next((line.lstrip("#").strip() for line in text.splitlines() if line.strip()), "")
                result = Result(first[:160], text, [], list(fallback_refs), [], [], prose=True)
            else:
                result = None
            if result is None or not (result.summary or result.understanding):
                reason = _stop_reason(session)
                raise ReadingError(f"The {role} reader produced no reading for {node_id}; "
                                   f"{'it wrote text but submitted nothing' if text else 'the model returned no text'}"
                                   f"{f' ({reason})' if reason else ''}.")
            result.session, result.forked_from, kept = session.sessionFile, forked_from, True
            return result
        finally:
            if unsubscribe is not None:
                unsubscribe()
            if session is not None:
                session.dispose()
            if not kept:
                forget(manager.getSessionFile())

    # -- context -----------------------------------------------------------------------------

    async def _excerpts(self, run, refs):
        """One opening window per material, preferring the places the action starts from."""
        from misaka.core.documents import index as corpus

        names, wanted = views.titles(run), {}
        for ref in sorted(refs, key=lambda ref: not (ref.get("node") or ref.get("pages"))):
            wanted.setdefault(ref["doc_id"], ref)
        for source in run.sources():
            if len(wanted) >= EXCERPT_SOURCES:
                break
            wanted.setdefault(source.id, {"doc_id": source.id})
        out = []
        for doc_id, ref in list(wanted.items())[:EXCERPT_SOURCES]:
            if doc_id not in names:
                continue
            start = 1
            try:
                if ref.get("node"):
                    span = await asyncio.to_thread(corpus.node_pages, doc_id, ref["node"], workspace=run.workspace)
                    start, end = span or (1, 1)
                else:
                    numbers = [int(n) for n in re.findall(r"\d+", str(ref.get("pages") or ""))] or [1]
                    start, end = numbers[0], numbers[-1]
                text = await asyncio.to_thread(corpus.read_pages, doc_id, start, end, max_chars=EXCERPT_CHARS,
                                               workspace=run.workspace)
            except Exception as error:  # noqa: BLE001 - one unreadable material must not stop the action
                text = f"(this material could not be read: {error})"
            out.append((names[doc_id], views.place(ref) or f"p. {start}", text or ""))
        return out

    def _prior(self, run, node_ids, whole):
        out = []
        for node_id in node_ids:
            node = run.reading(node_id)
            # A continuation goes on from the latest sections; the earlier ones are in the note.
            body = (views.note(run, node_id, limit=None if whole else PRIOR_CHARS) if run.sections(node_id)
                    else node.attrs["summary"])
            out.append((node_id, node.attrs.get("lens"), node.attrs["question"], body))
        return out

    async def _prompt(self, run, action, mode, node, *, question, lens, focus, using, refs):
        prior_ids = {"continue": [node.id], "independent": [], "compare": list(using) or [node.id],
                     "reread": [*using, node.id]}[mode]
        if not refs:
            refs = [ref for prior in (prior_ids or [node.id]) for ref in run.reading(prior).attrs["refs"]]
        return prompts.action_prompt(
            action=action, question=question, intent=run.info.get("intent") or "", lens=lens, focus=focus,
            method_text=prompts.method(action), materials=prompts.catalogue(run),
            excerpts=await self._excerpts(run, refs),
            prior=self._prior(run, prior_ids, whole=mode in ("compare", "reread"))), refs

    # -- writing a result ----------------------------------------------------------------------

    def _record(self, change, run, node_id, action, mode, result, at):
        """One action's result onto ``node_id``: its section, its attributes, its relations."""
        model = f"{self.model.provider}/{self.model.id}"
        section = change.add_node("section", {"action": action, "mode": mode, "at": at,
                                              "text": result.understanding, "model": model})
        change.add_edge("part_of", section, node_id)
        node = change.view.node(node_id)
        attrs = dict(node.attrs)
        refs = list(attrs["refs"])
        for ref in result.refs:
            if ref not in refs:
                refs.append(ref)
        concepts = {c["term"]: c for c in attrs.get("concepts", [])}
        concepts.update({c["term"]: c for c in result.concepts})
        change.update(node_id, {
            "summary": result.summary or attrs["summary"], "refs": refs,
            "actions": [*attrs["actions"], {"action": action, "contextMode": mode, "at": at,
                                             "summary": result.summary, "model": model}],
            "questions": list(dict.fromkeys([*attrs["questions"], *result.questions])),
            "concepts": list(concepts.values()), "session": result.session or attrs.get("session")})
        known = {n.id for n in change.view.nodes("reading")}
        for relation in result.relations:
            if relation["to"] in known and relation["to"] != node_id:
                change.add_edge("relation", node_id, relation["to"],
                                {"kind": relation["kind"], "explanation": relation["explanation"]})
        change.update("run", {"current": node_id})

    def _commit(self, run, token, op, payload, apply, results):
        """Record the operation and render it, still under the lease. The study sessions of an
        operation the graph refuses are forgotten with it; nothing awaits in between, so a stop
        cannot come between the recording and the results it records."""
        try:
            run.commit(token, op, payload, apply)
        except BaseException:
            for result in results:
                forget(result.session)
            raise
        render(run, self.progress)

    # -- operations ----------------------------------------------------------------------------

    async def act(self, run, node_id, action, *, question=None, lens=None, focus=None, mode=None, using=(),
                  refs=None):
        """One action on ``node_id``: ``continue`` writes onto that node, every other mode onto a
        successor whose parents are the node and ``using``. Without a ``mode``, a lens starts an
        independent line, as the leader's tool does. Returns ``(node id, Result)``."""
        if action not in store.ACTIONS:
            raise ReadingError(f"Unknown action {action}; the actions are {', '.join(store.ACTIONS)}.")
        mode = mode or {"compare": "compare", "reread": "reread"}.get(action) or ("independent" if lens else "continue")
        if mode not in store.MODES:
            raise ReadingError(f"Unknown context mode {mode}; the modes are {', '.join(store.MODES)}.")
        async with run.operation() as token:
            node = run.reading(node_id)
            using = [run.reading(other).id for other in using]
            question = (question or "").strip() or node.attrs["question"]
            text, refs = await self._prompt(run, action, mode, node, question=question, lens=lens, focus=focus,
                                            using=using, refs=list(refs or []))
            target = node.id if mode == "continue" else run.next_ids(1)[0]
            role = "compare" if mode == "compare" else "study"
            result = await self._study(run, target, role, text,
                                       node.attrs.get("session") if mode == "continue" else None, refs)
            at = store.now()

            def apply(change):
                if target != node.id:
                    change.add_node("reading", store.reading_attrs(question, lens, refs, at), id=target)
                    for parent in dict.fromkeys([node.id, *using]):
                        change.add_edge("derives", parent, target, {"how": HOW[mode]})
                self._record(change, run, target, action, mode, result, at)

            self._commit(run, token, "act", {"node": node.id, "action": action, "mode": mode, "target": target,
                                             **({"lens": lens} if lens else {}), **({"using": using} if using else {})},
                         apply, [result])
            if result.forked_from:
                forget(result.forked_from)          # its fork, now the node's session, holds all of it
        self.progress.changed(run.id, [target])
        return target, result

    async def fork(self, run, node_id, lenses, *, action="reconstruct", questions=None):
        """Independent lines from ``node_id``, one per lens, read in parallel and written together.
        A line that fails is reported after the others are written."""
        if not lenses:
            raise ReadingError("A fork needs at least one lens.")
        if action not in store.ACTIONS:
            raise ReadingError(f"Unknown action {action}; the actions are {', '.join(store.ACTIONS)}.")
        async with run.operation() as token:
            node = run.reading(node_id)
            targets = run.next_ids(len(lenses))
            plans = []
            for index, (target, lens) in enumerate(zip(targets, lenses)):
                question = ((questions or [])[index:index + 1] or [""])[0].strip() or node.attrs["question"]
                text, refs = await self._prompt(run, action, "independent", node, question=question, lens=lens,
                                                focus=None, using=(), refs=list(node.attrs["refs"]))
                plans.append((target, lens, question, text, refs))
            studies = [asyncio.ensure_future(self._study(run, target, "study", text, None, refs))
                       for target, _lens, _question, text, refs in plans]
            try:
                outcomes = await asyncio.gather(*studies, return_exceptions=True)
                if any(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes):
                    raise asyncio.CancelledError
            except asyncio.CancelledError:
                # Stopped: the lines that had finished are not recorded, so neither are their sessions.
                for study in studies:
                    if study.done() and not study.cancelled() and study.exception() is None:
                        forget(study.result().session)
                raise
            done = [(plan, outcome) for plan, outcome in zip(plans, outcomes) if isinstance(outcome, Result)]
            at = store.now()

            def apply(change):
                for (target, lens, question, _text, refs), result in done:
                    change.add_node("reading", store.reading_attrs(question, lens, refs, at), id=target)
                    change.add_edge("derives", node.id, target, {"how": "fork", "label": lens})
                    self._record(change, run, target, action, "independent", result, at)

            if done:
                self._commit(run, token, "fork", {"node": node.id, "action": action, "lenses": list(lenses),
                                                  "targets": [plan[0] for plan, _ in done]}, apply,
                             [result for _plan, result in done])
        written = [plan[0] for plan, _ in done]
        if written:
            self.progress.changed(run.id, written)
        failed = [(plan[0], plan[1], outcome) for plan, outcome in zip(plans, outcomes) if not isinstance(outcome, Result)]
        if failed:
            raise ReadingError("; ".join(f"line {lens} ({target}) failed: {error}" for target, lens, error in failed)
                               + (f". Written: {', '.join(written)}." if written else "."))
        return written, [outcome for _plan, outcome in done]

    async def compare(self, run, node_ids, question):
        """A comparison of finished nodes: a node with one parent per compared node."""
        if len(node_ids) < 2:
            raise ReadingError("A comparison needs at least two nodes.")
        nodes = [run.reading(node_id) for node_id in node_ids]
        refs = []
        for node in nodes:
            refs += [ref for ref in node.attrs["refs"] if ref not in refs]
        return await self.act(run, nodes[0].id, "compare", question=question or run.info["question"],
                              mode="compare", using=[node.id for node in nodes], refs=refs)

    async def reread(self, run, node_id, using, focus=None):
        """Back to ``node_id``'s material with the understanding of ``using``; a successor node."""
        if not using:
            raise ReadingError("A rereading needs at least one newer node to bring back.")
        old = run.reading(node_id)
        return await self.act(run, old.id, "reread", question=old.attrs["question"], mode="reread",
                              using=list(using), focus=focus, refs=list(old.attrs["refs"]))


def _normalize(submitted, run, fallback_refs):
    """The submitted reading tied to this reading's materials: a place in a material the reading
    does not have is dropped rather than kept as a link the reader cannot follow."""
    known = {source.id for source in run.sources()}
    refs = []
    for place in submitted.sources:
        if place.doc_id in known:
            ref = {"doc_id": place.doc_id, **({"node": place.node} if place.node else {}),
                   **({"pages": place.pages} if place.pages else {})}
            if ref not in refs:
                refs.append(ref)
    return Result(summary=submitted.summary.strip(), understanding=submitted.understanding.strip(),
                  concepts=[{"term": c.term.strip(), "note": c.note.strip()} for c in submitted.concepts if c.term.strip()],
                  refs=refs or list(fallback_refs),
                  relations=[{"to": r.to.strip(), "kind": r.kind, "explanation": r.explanation.strip()}
                             for r in submitted.relations if r.explanation.strip()],
                  questions=[q.strip() for q in submitted.questions if q.strip()])

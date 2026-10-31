"""Reading mode (``misaka/extensions/last_order/reading``): the reading's operations on the faux
model, its graph's rules, its materials, ``misaka read`` and ``/read`` with the window as leader.

The first group carries PR #12's continuity tests (``packages/reading/tests/continuity.test.mjs``)
over to the port: a provider error is an error, a continued line keeps its session across a
restart, an independent line gets a fresh one, comparison and rereading carry whole notes, and
the reader's interest survives a reopening."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from misaka.extensions.last_order.reading import (
    cli,
    engine,
    materials,
    store,
    views,
    window,
)
from misaka.extensions.last_order.reading.store import ReadingError

# -- the faux model ------------------------------------------------------------------------------


class Model:
    """A faux model whose study readers answer by script; ``prompts`` keeps each action's message."""

    def __init__(self, faux, model, runtime):
        self.faux, self.model, self.runtime, self.prompts, self.script = faux, model, runtime, [], []

    def reader(self, progress=engine.QUIET):
        return engine.Reader(self.model, self.runtime, progress)


@pytest.fixture
async def model():
    from misaka.ai.auth.types import ApiKeyCredential
    from misaka.ai.compat import registerFauxProvider
    from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
    from misaka.core.auth_storage import AuthStorage
    from misaka.utils.values import read_field
    from tests.model_runtime_test_utils import (
        createInMemoryModelRegistry,
        getModelRuntime,
    )

    faux = registerFauxProvider({"models": [{"id": "faux-1"}]})
    auth = AuthStorage.inMemory()

    async def key(_):
        return ApiKeyCredential(type="api_key", key="faux-key")

    await auth.modify("faux", key)
    registry = await createInMemoryModelRegistry(auth)
    m = faux.getModel("faux-1")
    registry.registerProvider("faux", {"baseUrl": m.baseUrl, "apiKey": "faux-key", "api": faux.api, "models": [
        {"id": m.id, "name": m.name, "api": m.api, "reasoning": m.reasoning, "input": m.input, "cost": m.cost,
         "contextWindow": m.contextWindow, "maxTokens": m.maxTokens, "baseUrl": m.baseUrl}]})
    handle = Model(faux, m, getModelRuntime(registry))

    def step(context, options, state, model_):
        last = context.messages[-1]
        if read_field(last, "role") == "toolResult":
            return fauxAssistantMessage([fauxText("done")])
        content = read_field(last, "content")
        handle.prompts.append(content if isinstance(content, str)
                              else "".join(read_field(part, "text") or "" for part in content))
        answer = handle.script.pop(0) if handle.script else {}
        if isinstance(answer, str):          # prose, no reading_submit
            return fauxAssistantMessage([fauxText(answer)])
        if answer.get("error"):
            return fauxAssistantMessage([], {"stopReason": "error", "errorMessage": answer["error"]})
        n = len(handle.prompts)
        submitted = {"summary": f"summary {n}", "understanding": f"body {n}", "questions": [f"q{n}"], **answer}
        return fauxAssistantMessage([fauxToolCall("reading_submit", submitted)], {"stopReason": "toolUse"})

    faux.setResponses([step] * 200)
    yield handle
    faux.unregister()


@pytest.fixture
def project(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "nora.md").write_text("# Nora\n\nWhat happens after Nora leaves home?\n", encoding="utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "doll.md").write_text("# A Doll's House\n\nI must stand quite alone.\n", encoding="utf-8")
    return SimpleNamespace(workspace=str(workspace), inside=str(workspace / "nora.md"),
                           outside=str(outside / "doll.md"))


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


async def opened(project, intent=""):
    return await engine.open_reading([project.inside], "What happens after Nora leaves?",
                                     workspace=project.workspace, intent=intent)


# -- PR #12's continuity guarantees --------------------------------------------------------------


async def test_a_provider_error_is_an_error_not_an_empty_reading(project, model):
    run = await opened(project)
    model.script.append({"error": "401 Unauthorized"})
    with pytest.raises(ReadingError, match="401 Unauthorized"):
        await model.reader().act(run, "N0", "reconstruct")
    assert run.reading("N0").attrs["actions"] == []          # nothing half written


async def test_continuing_a_node_appends_to_its_note_and_keeps_its_questions_in_order(project, model):
    run = await opened(project)
    reader = model.reader()
    await reader.act(run, "N0", "reconstruct")
    node_id, result = await reader.act(run, "N0", "presuppose")
    node = run.reading("N0")
    note = read(run.note_path("N0"))
    assert node_id == "N0" and result.summary == "summary 2" and node.attrs["summary"] == "summary 2"
    assert "body 1" in note and "body 2" in note and note.index("body 1") < note.index("body 2")
    assert list(node.attrs["questions"]) == ["q1", "q2"]
    assert [a["action"] for a in node.attrs["actions"]] == ["reconstruct", "presuppose"]


async def test_a_continued_line_keeps_its_session_across_a_restart(project, model):
    run = await opened(project)
    await model.reader().act(run, "N0", "reconstruct")
    again = engine.find(run.id)                               # a new connection, a new reader
    _, second = await model.reader().act(again, "N0", "reconstruct")
    # The line's session goes on: the second action's session holds the first one's turns too,
    # and the node keeps one session file, not one per action.
    assert run.reading("N0").attrs["session"] == second.session
    assert read(second.session).count('"role":"user"') == 2
    assert os.listdir(run.sessions_directory()) == [os.path.basename(second.session)]


async def test_an_independent_line_gets_a_new_node_and_a_fresh_session(project, model):
    run = await opened(project)
    _, root = await model.reader().act(run, "N0", "reconstruct")
    before = dict(run.reading("N0").attrs)
    node_id, line = await model.reader().act(run, "N0", "overview", question="Who pays for leaving?",
                                             lens="economic conditions", mode="independent")
    node = run.reading(node_id)
    assert node_id == "N1" and node.attrs["lens"] == "economic conditions"
    assert node.attrs["question"] == "Who pays for leaving?" and line.session != root.session
    assert dict(run.reading("N0").attrs) == before
    assert run.parents("N1") == [("N0", {"how": "fork"})]
    assert "Earlier understanding" not in model.prompts[-1]   # independence: no sibling conclusion


async def test_comparison_and_rereading_carry_whole_notes(project, model):
    run = await opened(project)
    long_note = "start " + "x" * 18_000 + " FINAL-MARK"
    model.script += [{"understanding": long_note}, {"understanding": long_note}]
    written, _ = await model.reader().fork(run, "N0", ["agency", "economy"])
    compare_id, _ = await model.reader().compare(run, written, "What do they share?")
    assert model.prompts[-1].count("FINAL-MARK") == 2
    assert sorted(run.parents(compare_id)) == [(node, {"how": "compare"}) for node in written]
    reread_id, _ = await model.reader().reread(run, "N1", [compare_id])
    assert "FINAL-MARK" in model.prompts[-1]
    assert sorted(run.parents(reread_id)) == [("N1", {"how": "reread"}), (compare_id, {"how": "reread"})]


async def test_the_readers_interest_survives_reopening(project, model):
    run = await opened(project, intent="freedom and money")
    await model.reader().act(engine.find(run.id), "N0", "reconstruct")
    assert "freedom and money" in model.prompts[-1]
    assert "freedom and money" in read(os.path.join(run.directory, "reader.md"))


# -- the operations ------------------------------------------------------------------------------


async def test_a_prose_answer_is_kept_as_the_reading(project, model):
    run = await opened(project)
    model.script.append("## The difficulty\n\nLeaving needs money.")
    _, result = await model.reader().act(run, "N0", "reconstruct")
    assert result.prose and result.summary == "The difficulty"
    assert "Leaving needs money." in read(run.note_path("N0"))


async def test_places_outside_the_readings_materials_are_dropped(project, model):
    run = await opened(project)
    doc = run.sources()[0].id
    model.script.append({"sources": [{"doc_id": doc, "pages": "1"}, {"doc_id": "not-a-material"}],
                         "relations": [{"to": "N0", "kind": "differs", "explanation": "self"}]})
    await model.reader().act(run, "N0", "reconstruct")
    assert {"doc_id": doc, "pages": "1"} in run.reading("N0").attrs["refs"]
    assert all(ref["doc_id"] == doc for ref in run.reading("N0").attrs["refs"])
    assert run.relations() == []                              # a node does not relate to itself


async def test_a_fork_writes_the_lines_that_finished_and_reports_the_rest(project, model):
    run = await opened(project)
    model.script += [{}, {"error": "400 invalid request"}]
    with pytest.raises(ReadingError, match="400 invalid request") as caught:
        await model.reader().fork(run, "N0", ["agency", "economy"])
    written = [node.id for node in run.readings() if node.id != "N0"]
    assert len(written) == 1 and f"Written: {written[0]}" in str(caught.value)


async def test_the_graph_refuses_a_cycle_of_derivations(project, model):
    run = await opened(project)
    await model.reader().act(run, "N0", "overview", lens="x", mode="independent")
    with pytest.raises(ReadingError, match="cycle"):
        run.write("bad", {}, lambda change: change.add_edge("derives", "N1", "N0", {"how": "fork"}))


async def test_one_operation_at_a_time(project, model):
    run = await opened(project)
    async with run.operation():
        with pytest.raises(ReadingError, match="busy"):
            await model.reader().act(engine.find(run.id), "N0", "reconstruct")


async def test_a_stopped_action_writes_nothing(project, model):
    run = await opened(project)
    gate = asyncio.Event()
    model.faux.setResponses([lambda *a: gate.wait()])         # never answers until cancelled
    task = asyncio.ensure_future(model.reader().act(run, "N0", "reconstruct"))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert run.reading("N0").attrs["actions"] == []
    async with run.operation():                               # the lease was given back
        pass


async def test_the_rendered_files(project, model):
    run = await opened(project)
    await model.reader().fork(run, "N0", ["agency", "economy"])
    data = json.loads(read(os.path.join(run.directory, "graph.json")))
    assert [n["id"] for n in data["nodes"]] == ["N0", "N1", "N2"]
    assert {(e["from"], e["to"], e["kind"]) for e in data["generationEdges"]} == {("N0", "N1", "fork"), ("N0", "N2", "fork")}
    graph_md = read(os.path.join(run.directory, "graph.md"))
    assert "N0 -->|fork: agency| N1" in graph_md
    assert "## Map" in read(os.path.join(run.directory, "reading.md"))


def test_headings_in_a_reading_go_below_the_actions_heading():
    assert views.demote_headings("# A\n```\n# code\n```\n### B") == "### A\n```\n# code\n```\n##### B"


# -- materials -----------------------------------------------------------------------------------


async def test_a_file_outside_the_project_is_copied_in_and_never_overwritten(project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "doll.md").write_text("# Another doll\n\nA different text under the same name.\n", encoding="utf-8")
    run = await engine.open_reading([project.inside, project.outside, str(other / "doll.md")], "q",
                                    workspace=project.workspace)
    origins = [source.attrs.get("origin") for source in run.sources()]
    assert origins == [project.inside, project.outside, str(other / "doll.md")]      # where each came from
    assert sorted(os.listdir(os.path.join(run.directory, "sources"))) == ["doll-2.md", "doll.md"]


async def test_the_same_material_twice_is_one_material(project):
    run = await engine.open_reading([project.outside, project.outside], "q", workspace=project.workspace)
    assert len(run.sources()) == 1
    assert os.listdir(os.path.join(run.directory, "sources")) == ["doll.md"]


async def test_opening_with_a_missing_file_leaves_no_reading_behind(project):
    with pytest.raises(ReadingError, match="No file at"):
        await engine.open_reading([project.inside, "/nonexistent/x.md"], "q", workspace=project.workspace)
    assert not os.path.exists(os.path.join(project.workspace, "reading")) or \
        os.listdir(os.path.join(project.workspace, "reading")) == []


async def test_a_url_is_downloaded_named_by_its_type_and_bounded(project, monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    from misaka.core.web import bounded

    body = {"bytes": b"# Page\n\ntext\n", "truncated": False}

    @asynccontextmanager
    async def stream(url, **kwargs):
        assert kwargs["service"] == "reading_material"
        yield SimpleNamespace(status_code=200, url=url, headers={"content-type": "text/markdown; charset=utf-8"})

    async def read(response, max_bytes, *, deadline=None, signal=None):
        assert max_bytes == materials.MAX_BYTES and deadline is not None
        return body["bytes"], body["truncated"]

    monkeypatch.setattr(bounded, "open_checked_stream", stream)
    monkeypatch.setattr(bounded, "read_bounded", read)
    target = str(tmp_path / "in")
    path = await materials._download("https://example.org/essays/after", target)
    assert path == os.path.join(target, "after.md")
    body["truncated"] = True
    with pytest.raises(ReadingError, match="larger than 64 MB"):
        await materials._download("https://example.org/big.md", target)


def test_names_keep_their_letters_and_lose_what_a_path_cannot_hold():
    assert materials.safe_name("娜拉走后怎样?.pdf") == "娜拉走后怎样.pdf"
    assert materials.safe_name(" ..\x00a/b.md. ") == "ab.md"


# -- misaka read ---------------------------------------------------------------------------------


def test_the_command_line_refuses_without_exiting():
    with pytest.raises(ReadingError):
        cli.parse(["act", "RUN", "nonsense", "--node", "N0"])
    with pytest.raises(ReadingError, match="at least two"):
        cli.parse(["compare", "RUN", "N1", "--question", "q"])
    with pytest.raises(cli.Help):
        cli.parse(["--help"])


def test_open_and_graph_need_no_model(project, capsys):
    assert cli.main(["open", project.inside, "--question", "q", "--workspace", project.workspace, "--json"]) == 0
    opened_run = json.loads(capsys.readouterr().out)
    assert opened_run["currentNodeId"] == "N0" and os.path.isdir(opened_run["runDirectory"])
    assert cli.main(["graph", opened_run["runId"], "--json"]) == 0
    graph = json.loads(capsys.readouterr().out)
    assert [node["id"] for node in graph["nodes"]] == ["N0"] and graph["nodes"][0]["actions"] == []
    assert cli.main(["graph", "no-such-run"]) == 1
    assert cli.main(["--help"]) == 0


# -- /read and the window as leader -------------------------------------------------------------


class Harn:
    def __init__(self):
        self.tools, self.commands, self.handlers, self.entries = {}, {}, {}, []
        self.active = ["doc_read"]

    def registerTool(self, tool):
        self.tools[tool.name] = tool
        self.active.append(tool.name)

    def registerCommand(self, name, spec):
        self.commands[name] = spec

    def on(self, event, handler):
        self.handlers[event] = handler

    def getActiveTools(self):
        return list(self.active)

    def setActiveTools(self, names):
        self.active = list(names)

    def appendEntry(self, custom_type, data=None):
        self.entries.append({"type": "custom", "customType": custom_type, "data": data})


class UI:
    def __init__(self):
        self.notices, self.widgets, self.status = [], {}, {}

    def notify(self, message, kind=None):
        self.notices.append((kind, message))

    def setWidget(self, key, lines, options=None):
        self.widgets[key] = lines

    def setStatus(self, key, text):
        self.status[key] = text


def linked(entries):
    """The window's entries as a session keeps them: one branch, each the parent of the next."""
    out, parent = [], None
    for index, entry in enumerate(entries):
        out.append({"timestamp": 0, **entry, "id": entry.get("id") or f"e{index}", "parentId": parent})
        parent = out[-1]["id"]
    return out


def window_ctx(harn, model, project, session_file="/tmp/lo-session.jsonl", mode="tui"):
    manager = SimpleNamespace(getBranch=lambda: list(harn.entries), getEntries=lambda: linked(harn.entries),
                              getLeafId=lambda: linked(harn.entries)[-1]["id"] if harn.entries else None,
                              getSessionFile=lambda: session_file)
    aborted = []
    return SimpleNamespace(mode=mode, ui=UI(), sessionManager=manager, cwd=project.workspace,
                           model=model.model if model else None, aborted=aborted, abort=lambda: aborted.append(True),
                           modelRegistry=SimpleNamespace(modelRuntime=model.runtime if model else None))


async def settle():
    """Let a /read operation running in the background finish."""
    for _ in range(100):
        await asyncio.sleep(0.02)


async def run_starts(harn, ctx):
    """Last Order's next run starts: the notice the reading adds to it is kept in the conversation,
    as the session keeps a before_agent_start message. Returns that message, or None."""
    out = await harn.handlers["before_agent_start"]({}, ctx)
    if not out:
        return None
    message = out["message"]
    harn.entries.append({"type": "custom_message", **message})
    return message


async def test_a_window_that_opens_a_reading_leads_it(project, model):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.handlers["session_start"]({}, ctx)
    assert not set(window.TOOLS) & set(harn.active)            # no reading, no reading tools
    assert await run_starts(harn, ctx) is None
    await harn.commands["read"]["handler"]('open nora.md --question "After Nora?"', ctx)   # the window's folder
    await settle()
    run_id = next(entry["data"]["run"] for entry in harn.entries)
    assert set(window.TOOLS) <= set(harn.active)
    assert engine.find(run_id).info["leader"] == "/tmp/lo-session.jsonl"
    assert engine.find(run_id).workspace == os.path.realpath(project.workspace)
    notice = await run_starts(harn, ctx)
    assert notice["customType"] == "reading-mode" and notice["display"] is False
    assert run_id in notice["content"] and "Reading map" in notice["content"] and "## Reading tools" in notice["content"]

    # the leader acts through its tool; her next run is given the map with the new node
    act = harn.tools["reading_act"]
    updates = []
    out = await act.execute("call", {"action": "reconstruct"}, None, updates.append, ctx)
    assert "N0" in out["content"][0]["text"] and updates and updates[-1]["details"]["reading"]["node"] == "N0"
    update = await run_starts(harn, ctx)
    assert "summary 1" in update["content"] and "## Reading tools" not in update["content"]

    # a restart reads the binding back from the conversation; /read close ends it
    again = Harn()
    window.register(again)
    again.entries = list(harn.entries)
    await again.handlers["session_start"]({}, window_ctx(again, model, project))
    assert set(window.TOOLS) <= set(again.active)
    await harn.commands["read"]["handler"]("close", ctx)
    assert not set(window.TOOLS) & set(harn.active)
    ended = await run_starts(harn, ctx)
    assert "no longer leads reading" in ended["content"] and await run_starts(harn, ctx) is None


async def test_another_window_takes_a_reading_up_only_when_asked(project, model, tmp_path):
    run = await opened(project)
    leader = tmp_path / "leader.jsonl"
    leader.write_text("{}\n", encoding="utf-8")
    run.write("lead", {}, lambda change: change.update("run", {"leader": str(leader)}))
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project, session_file=str(tmp_path / "other.jsonl"))
    await harn.commands["read"]["handler"](f"chat {run.id}", ctx)
    assert "is led by the conversation" in ctx.ui.notices[-1][1] and not harn.entries
    await harn.commands["read"]["handler"](f"chat {run.id} --here", ctx)
    assert harn.entries[-1]["data"]["run"] == run.id
    assert engine.find(run.id).info["leader"] == str(tmp_path / "other.jsonl")


async def test_read_commands_in_the_window(project, model):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    handler = harn.commands["read"]["handler"]
    await handler("", ctx)
    assert any("/read close" in line for line in ctx.ui.widgets["reading"]) and len(ctx.ui.widgets["reading"]) <= 10
    await handler("stop", ctx)
    assert ctx.ui.notices[-1] == ("info", "No reading is running in this window.")
    await handler("act RUN nonsense --node N0", ctx)
    assert ctx.ui.notices[-1][0] == "error"
    run = await opened(project)
    await handler(f"graph {run.id}", ctx)
    assert "\n  N0" in ctx.ui.notices[-1][1]
    headless = window_ctx(harn, model, project, mode="print")
    await handler("", headless)
    assert headless.ui.notices[-1] == ("warning", "/read is available in interactive mode.")


async def test_the_window_runs_one_operation_at_a_time_and_stops_it(project, model):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    run = await opened(project)
    gate = asyncio.Event()
    model.faux.setResponses([lambda *a: gate.wait()])
    await harn.commands["read"]["handler"](f"act {run.id} reconstruct --node N0", ctx)
    await asyncio.sleep(0.2)
    await harn.commands["read"]["handler"](f"act {run.id} reconstruct --node N0", ctx)
    assert ctx.ui.notices[-1] == ("warning", "This window is reading; wait for it to finish, or /read stop.")
    await harn.commands["read"]["handler"]("stop", ctx)
    await settle()
    assert "Reading stopped" in ctx.ui.notices[-1][1]
    assert run.reading("N0").attrs["actions"] == []


def test_words_split_like_a_shell_but_keep_windows_paths():
    assert window.split(r'open "C:\Users\me\Nora.pdf" --question "a b"') == \
        ["open", r"C:\Users\me\Nora.pdf", "--question", "a b"]
    assert window.split('open "unbalanced') == ["open", '"unbalanced']


@pytest.mark.parametrize("settings, loads", [({}, True), ({"extensions": ["-builtin:reading"]}, False)])
async def test_reading_mode_can_be_switched_off_in_settings(tmp_path, settings, loads):
    from misaka import extensions
    from misaka.config import home
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.wiring import SessionSpec

    agent_dir, cwd = tmp_path / "agent", tmp_path / "project"
    agent_dir.mkdir()
    cwd.mkdir()
    (agent_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    spec = SessionSpec(profile_dir=str(home.path("roles_root") / "last_order"), role="last_order",
                       workspace=str(cwd), kind="foreground")
    factories = [d for d in extensions.discover(spec) if d["name"] == "reading"]
    assert factories and factories[0]["builtin"] is True
    loader = DefaultResourceLoader({"cwd": str(cwd), "agentDir": str(agent_dir), "extensionFactories": factories})
    await loader.reload()
    assert ("builtin:reading" in [e.path for e in loader.getExtensions().extensions]) is loads


def test_a_lease_left_by_a_dead_process_on_this_host_can_be_taken():
    import socket

    from misaka.extensions.last_order.reading import store

    host = socket.gethostname()
    assert store._dead(f"reading:{host}:999999999:abcd") is True
    assert store._dead(f"reading:{host}:{os.getpid()}:abcd") is False
    assert store._dead("reading:another-host:999999999:abcd") is False      # not ours to judge
    assert store._dead("research:x") is False


# -- found in review (2026-10-30) ----------------------------------------------------------------


async def test_an_action_the_graph_does_not_record_leaves_no_note_behind(project, model, monkeypatch):
    run = await opened(project)
    real = store.Run.commit

    def refuse(self, *args, **kwargs):
        raise ReadingError("the board is busy")

    monkeypatch.setattr(store.Run, "commit", refuse)
    model.script.append({"understanding": "OLD-ATTEMPT"})
    with pytest.raises(ReadingError, match="busy"):
        await model.reader().act(run, "N0", "overview", question="Old?", lens="old-lens")
    assert not [name for name in os.listdir(os.path.join(run.directory, "nodes")) if name.startswith("N1")]
    monkeypatch.setattr(store.Run, "commit", real)
    model.script.append({"understanding": "NEW-ATTEMPT"})
    node_id, _ = await model.reader().act(run, "N0", "overview", question="New?", lens="new-lens")
    note = read(run.note_path(node_id))
    assert node_id == "N1" and "OLD-ATTEMPT" not in note and "old-lens" not in note and "NEW-ATTEMPT" in note


async def test_a_lens_from_the_command_line_starts_a_line(project, model):
    run = await opened(project)
    node_id, _ = await model.reader().act(run, "N0", "overview", lens="economic conditions")
    assert node_id == "N1" and run.parents("N1") == [("N0", {"how": "fork"})]


async def test_a_lease_left_by_a_dead_process_is_taken_over_and_a_live_one_is_not(project, model):
    import socket

    from misaka.core import graph

    run = await opened(project)
    dead = f"reading:{socket.gethostname()}:999999999:dead"
    graph.acquire(run.con, run.gid, dead, ttl=3600)
    async with run.operation():
        pass
    live = f"reading:{socket.gethostname()}:{os.getppid()}:live"            # another live process
    graph.acquire(run.con, run.gid, live, ttl=3600)
    with pytest.raises(ReadingError, match="busy"):
        async with run.operation():
            pass
    graph.release(run.con, run.gid, live)
    # this process's own lease that the board was too busy to take back is not waited out
    leftover = f"reading:{socket.gethostname()}:{os.getpid()}:left"
    graph.acquire(run.con, run.gid, leftover, ttl=3600)
    async with run.operation():
        pass


async def test_materials_from_outside_go_to_downloads_when_the_reading_is_kept_outside(project, tmp_path):
    run = await engine.open_reading([project.outside], "q", workspace=project.workspace,
                                    runs_dir=str(tmp_path / "readings"))
    assert run.directory.startswith(os.path.realpath(str(tmp_path / "readings")))
    assert os.listdir(os.path.join(project.workspace, "downloads")) == ["doll.md"]


def test_long_names_are_cut_to_fit_and_keep_their_suffix():
    name = materials.safe_name("娜" * 200 + ".pdf")
    assert name.endswith(".pdf") and len(name.encode()) <= materials.MAX_NAME_BYTES


def test_an_options_quoted_value_loses_its_quotes():
    assert window.split('act R overview --question="a b" --focus \'c d\'') == \
        ["act", "R", "overview", "--question=a b", "--focus", "c d"]


def test_reading_tools_run_one_at_a_time():
    harn = Harn()
    window.register(harn)
    assert {harn.tools[name].executionMode for name in window.TOOLS} == {"sequential"}


def test_the_leaders_terminal_relays_only_reading_updates():
    from dataclasses import dataclass

    @dataclass(slots=True)
    class BashDetails:
        truncation: object = None

    seen = []

    class Seen(engine.Progress):
        def text(self, role, node_id, delta):
            seen.append((role, node_id, delta))

    cli.relay(Seen(), {"type": "tool_execution_update", "partialResult": {"details": BashDetails()}})
    cli.relay(Seen(), {"type": "tool_execution_update",
                       "partialResult": {"details": {"reading": {"role": "study", "node": "N1", "delta": "x"}}}})
    assert seen == [("study", "N1", "x")]


async def test_the_leaders_terminal_takes_lines_in_order_and_stops_on_a_stop_word():
    async def run(lines_in, slow=0.0, fail_on=None):
        lines, done, aborted = asyncio.Queue(), [], []
        for line in lines_in:
            lines.put_nowait(line)

        async def turn(line):
            await asyncio.sleep(slow)
            if line == fail_on:
                raise ReadingError("provider down")
            done.append(line)

        async def abort():
            aborted.append(True)

        failed = await cli.converse(lines, turn, abort)
        return done, aborted, failed

    # lines piped in before the end of input all run, in order, even when they arrive mid-turn
    assert await run(["a", "b", "c", None], slow=0.05) == (["a", "b", "c"], [], False)
    # a stop word typed during a turn stops it and ends; nothing after it runs
    done, aborted, _ = await run(["a", "/stop", "b", None], slow=0.05)
    assert aborted and "b" not in done
    # a failed turn is reported, the rest still run, and the end status says so
    assert await run(["a", "bad", "c", None], fail_on="bad") == (["a", "c"], [], True)


async def test_the_leaders_terminal_will_not_write_a_conversation_a_window_has_open(project, model, monkeypatch,
                                                                                      tmp_path):
    from misaka.core import session_catalog
    from misaka.core.platform import processes

    run = await opened(project)
    leader = tmp_path / "leader.jsonl"
    leader.write_text("{}\n", encoding="utf-8")
    run.write("lead", {}, lambda change: change.update("run", {"leader": str(leader)}))
    monkeypatch.setattr(session_catalog, "owner_record", lambda path: {"state": "running", "pid": 1, "identity": "x"})
    monkeypatch.setattr(processes, "identity_is_alive", lambda pid, identity: True)
    with pytest.raises(ReadingError, match="open in a window"):
        await cli.chat(SimpleNamespace(run=run.id, model=None))


async def test_opening_another_reading_hands_the_first_one_back(project, model, tmp_path):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project, session_file=str(tmp_path / "lo.jsonl"))
    await harn.commands["read"]["handler"]('open nora.md --question "First?"', ctx)
    await settle()
    first = harn.entries[-1]["data"]["run"]
    await harn.commands["read"]["handler"]('open nora.md --question "Second?"', ctx)
    await settle()
    second = harn.entries[-1]["data"]["run"]
    assert first != second
    assert engine.find(first).info["leader"] is None
    assert engine.find(second).info["leader"] == str(tmp_path / "lo.jsonl")


async def test_a_window_leads_only_a_reading_of_its_own_project(project, model, tmp_path):
    other = tmp_path / "other-project"
    other.mkdir()
    (other / "x.md").write_text("# X\n\nAnother project's material, long enough to be indexed.\n", encoding="utf-8")
    run = await engine.open_reading([str(other / "x.md")], "q", workspace=str(other))
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.commands["read"]["handler"](f"chat {run.id} --here", ctx)
    assert ctx.ui.notices[-1][0] == "warning" and "belongs to" in ctx.ui.notices[-1][1] and not harn.entries


async def test_read_stop_reaches_an_operation_last_order_started(project, model):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    gate = asyncio.Event()
    model.faux.setResponses([lambda *a: gate.wait()])
    call = asyncio.ensure_future(harn.tools["reading_act"].execute("c", {"action": "reconstruct"}, None, None, ctx))
    await asyncio.sleep(0.2)
    await harn.commands["read"]["handler"]("stop", ctx)
    out = await call
    assert out["content"][0]["text"].startswith("Stopped by the reader; nothing was written.")
    assert ctx.aborted == [True]                              # her turn stops with it
    run_id = harn.entries[-1]["data"]["run"]
    assert engine.find(run_id).reading("N0").attrs["actions"] == []


async def test_a_window_that_goes_away_mid_reading_ends_it_quietly(project, model):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    run = await opened(project)
    gate = asyncio.Event()
    model.faux.setResponses([lambda *a: gate.wait()])
    errors = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))
    await harn.commands["read"]["handler"](f"act {run.id} reconstruct --node N0", ctx)
    await asyncio.sleep(0.2)
    await harn.handlers["session_shutdown"]({}, ctx)

    def gone(*args):
        raise RuntimeError("This extension ctx is stale after session replacement or reload")

    ctx.ui.notify = ctx.ui.setWidget = ctx.ui.setStatus = gone
    await settle()
    import gc
    gc.collect()
    await asyncio.sleep(0)
    assert errors == []
    assert run.reading("N0").attrs["actions"] == []


# -- found in the second review (2026-10-30) -----------------------------------------------------


async def test_a_cancellation_of_the_call_itself_goes_on_up(project, model):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    gate = asyncio.Event()
    model.faux.setResponses([lambda *a: gate.wait()] * 2)
    call = asyncio.ensure_future(harn.tools["reading_act"].execute("c", {"action": "reconstruct"}, None, None, ctx))
    await asyncio.sleep(0.2)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await harn.tools["reading_act"].execute("c", {"action": "reconstruct"}, None, None, ctx)


async def test_a_tool_called_after_escape_does_nothing(project, model):
    from misaka.utils.js import AbortController

    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    controller = AbortController()
    controller.abort()
    out = await harn.tools["reading_act"].execute("c", {"action": "reconstruct"}, controller.signal, None, ctx)
    assert out is window.STOPPED and model.prompts == []


async def test_notes_are_the_graphs_and_survive_a_render_that_fails(project, model, monkeypatch):
    run = await opened(project)
    real = views.render

    def locked(run_):
        raise PermissionError("graph.md is open in another program")

    monkeypatch.setattr(views, "render", locked)
    notes = []

    class Notes(engine.Progress):
        def note(self, line):
            notes.append(line)

    model.script.append({"understanding": "KEPT-READING"})
    node_id, _ = await model.reader(Notes()).act(run, "N0", "reconstruct")   # recorded: no failure
    assert any("could not update" in line for line in notes)
    assert "KEPT-READING" in views.note(run, node_id)
    monkeypatch.setattr(views, "render", real)
    engine.render(run)
    assert "KEPT-READING" in read(run.note_path(node_id))


async def test_an_action_not_recorded_leaves_its_nodes_session_as_it_was(project, model, monkeypatch):
    run = await opened(project)
    _, first = await model.reader().act(run, "N0", "reconstruct")
    gate = asyncio.Event()
    model.faux.setResponses([lambda *a: gate.wait()])
    task = asyncio.ensure_future(model.reader().act(run, "N0", "presuppose"))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert run.reading("N0").attrs["session"] == first.session
    assert read(first.session).count('"role":"user"') == 1
    assert os.listdir(run.sessions_directory()) == [os.path.basename(first.session)]


async def test_the_reading_notice_is_added_to_the_history_never_rewritten_in_it(project, model):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    assert "context" not in harn.handlers                   # a request goes out as the history has it
    whole = await run_starts(harn, ctx)
    assert await run_starts(harn, ctx) is None                # nothing changed: nothing more to say
    run = engine.find(harn.entries[0]["data"]["run"])
    await model.reader().act(run, "N0", "reconstruct")        # the reader's own /read act, say
    update = await run_starts(harn, ctx)
    assert "summary 1" in update["content"] and await run_starts(harn, ctx) is None
    assert [entry.get("content") for entry in harn.entries if entry["type"] == "custom_message"] == \
        [whole["content"], update["content"]]                 # the first notice is where it was


async def test_a_notice_a_compaction_summarized_is_given_again_whole(project, model):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    await run_starts(harn, ctx)
    run = engine.find(harn.entries[0]["data"]["run"])
    await model.reader().act(run, "N0", "reconstruct")
    await run_starts(harn, ctx)                               # a map after the whole notice
    del harn.entries[1]                                       # the whole notice was before the compaction
    again = await run_starts(harn, ctx)
    assert "## Reading tools" in again["content"] and "summary 1" in again["content"]


async def test_a_notice_lcm_kept_through_a_compaction_still_counts(project, model):
    """LCM, MISAKA's context engine, compacts by replacing the whole context (contextMessages): a
    notice it keeps is in what the model sees, though no longer an entry of its own."""
    import time

    from misaka.core.session_manager import SessionManager

    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    session = SessionManager.inMemory(project.workspace)
    ctx.sessionManager = SimpleNamespace(getBranch=lambda: list(harn.entries), getEntries=session.getEntries,
                                         getLeafId=session.getLeafId, getSessionFile=lambda: "/tmp/lo-session.jsonl")

    async def run_starts_here():
        out = await harn.handlers["before_agent_start"]({}, ctx)
        if out:
            message = out["message"]
            session.appendCustomMessageEntry(message["customType"], message["content"], message["display"],
                                             message["details"])
        return out and out["message"]

    def compact():
        session.appendCompaction("LCM", None, 100, {"lcm": {}}, True, None,
                                 contextMessages=session.buildSessionProjection().messages, contextSummaries=[])

    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    assert "## Reading tools" in (await run_starts_here())["content"]
    session.appendMessage({"role": "user", "content": [{"type": "text", "text": "hi"}],
                           "timestamp": int(time.time() * 1000)})
    compact()
    assert await run_starts_here() is None                    # kept: not given again
    await harn.commands["read"]["handler"]("close", ctx)
    assert "no longer leads reading" in (await run_starts_here())["content"]


async def test_a_notice_for_another_reading_or_after_an_end_does_not_count(project, model, tmp_path):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    first = harn.entries[0]["data"]["run"]
    await run_starts(harn, ctx)
    await harn.commands["read"]["handler"]('open nora.md --question "Another?"', ctx)   # hands the first back
    await settle()
    second = harn.entries[-1]["data"]["run"]
    notice = await run_starts(harn, ctx)
    assert second != first and second in notice["content"] and "## Reading tools" in notice["content"]
    assert f"no longer leads reading {first}" in notice["content"]                  # the first one ended
    await harn.commands["read"]["handler"]("close", ctx)
    assert second in (await run_starts(harn, ctx))["content"]                         # it ended
    await harn.commands["read"]["handler"](f"chat {second}", ctx)
    assert "## Reading tools" in (await run_starts(harn, ctx))["content"]             # led again: whole again


async def test_a_run_in_a_conversation_that_never_led_a_reading_keeps_its_tools(project, model):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    before = harn.getActiveTools()                            # a session that never ran session_start, say
    assert await run_starts(harn, ctx) is None
    assert harn.getActiveTools() == before                    # preparing a run changes no default tools


async def test_a_window_another_conversation_took_the_reading_from_stops_leading_it(project, model, tmp_path):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project, session_file=str(tmp_path / "a.jsonl"))
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    run_id = harn.entries[-1]["data"]["run"]
    await run_starts(harn, ctx)
    engine.write(engine.find(run_id), "lead", {}, lambda change: change.update("run", {"leader": str(tmp_path / "b")}))
    assert set(window.TOOLS) <= set(harn.active)              # the run is still to choose its tools:
    assert "no longer leads reading" in (await run_starts(harn, ctx))["content"]
    assert not set(window.TOOLS) & set(harn.active)           # the run that is told goes without them
    with pytest.raises(ReadingError, match="No reading is led from this window"):
        await harn.tools["reading_act"].execute("c", {"action": "reconstruct"}, None, None, ctx)


async def test_going_back_in_the_tree_past_the_binding_leaves_reading_mode(project, model):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    harn.entries.clear()                                      # the branch before /read open
    await harn.handlers["session_tree"]({}, ctx)
    assert not set(window.TOOLS) & set(harn.active)


async def test_the_lease_is_borrowed_only_from_this_process(project):
    import socket

    from misaka.core import graph

    run = await opened(project)
    other = f"reading:{socket.gethostname()}:{os.getppid()}:other"      # a live process, not this one
    graph.acquire(run.con, run.gid, other, ttl=3600)
    with pytest.raises(ReadingError, match="busy"), run.hold():
        pass
    graph.release(run.con, run.gid, other)
    async with run.operation() as token:
        with run.hold() as held:
            assert held == token


async def test_a_lease_the_board_cannot_take_back_does_not_fail_the_operation(project, model, monkeypatch):
    import sqlite3

    from misaka.core import graph

    run = await opened(project)
    real = graph.release

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(graph, "release", busy)
    node_id, _ = await model.reader().act(run, "N0", "reconstruct")
    monkeypatch.setattr(graph, "release", real)
    assert node_id == "N0" and run.reading("N0").attrs["actions"][-1]["model"] == "faux/faux-1"


def test_a_run_id_never_ends_in_a_dot(tmp_path):
    run_id = store.new_run_id(store.connect(), "Read Lu Xun.", str(tmp_path))
    assert not run_id.endswith((".", " ")) and run_id.endswith("Read-Lu-Xun")


async def test_the_graph_can_be_seen_while_an_operation_runs(project, model):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    run = await opened(project)
    gate = asyncio.Event()
    model.faux.setResponses([lambda *a: gate.wait()])
    await harn.commands["read"]["handler"](f"act {run.id} reconstruct --node N0", ctx)
    await asyncio.sleep(0.2)
    await harn.commands["read"]["handler"](f"graph {run.id}", ctx)
    assert ctx.ui.notices[-1][1].startswith(f"Run {run.id}")
    await harn.commands["read"]["handler"]("stop", ctx)
    await settle()


# -- found in the third review (2026-10-30) ------------------------------------------------------


async def test_a_window_without_a_conversation_file_cannot_lead(project, model, tmp_path):
    run = await opened(project)
    engine.write(run, "lead", {}, lambda change: change.update("run", {"leader": str(tmp_path / "a.jsonl")}))
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project, session_file=None)
    await harn.commands["read"]["handler"](f"chat {run.id} --here", ctx)
    assert ctx.ui.notices[-1][0] == "error" and "cannot lead" in ctx.ui.notices[-1][1]
    assert engine.find(run.id).info["leader"] == str(tmp_path / "a.jsonl") and not harn.entries
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    assert ctx.ui.notices[-1][0] == "warning" and "cannot lead it" in ctx.ui.notices[-1][1]


async def test_taking_up_a_busy_reading_leaves_the_window_as_it_was(project, model, tmp_path):
    import socket

    from misaka.core import graph

    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project, session_file=str(tmp_path / "lo.jsonl"))
    await harn.commands["read"]["handler"]('open nora.md --question "First?"', ctx)
    await settle()
    first = harn.entries[-1]["data"]["run"]
    second = await opened(project)
    busy = f"reading:{socket.gethostname()}:{os.getppid()}:busy"
    graph.acquire(second.con, second.gid, busy, ttl=3600)
    await harn.commands["read"]["handler"](f"chat {second.id}", ctx)
    graph.release(second.con, second.gid, busy)
    assert ctx.ui.notices[-1][0] == "error" and "busy" in ctx.ui.notices[-1][1]
    assert engine.find(first).info["leader"] == str(tmp_path / "lo.jsonl")
    assert harn.entries[-1]["data"]["run"] == first and set(window.TOOLS) <= set(harn.active)


async def test_a_stopped_fork_keeps_no_session_of_the_lines_that_had_finished(project, model):
    run = await opened(project)
    gate = asyncio.Event()

    from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
    from misaka.utils.values import read_field

    calls = {"n": 0}

    def answer(context, options, state, model_):
        if read_field(context.messages[-1], "role") == "toolResult":
            return fauxAssistantMessage([fauxText("done")])
        calls["n"] += 1
        if calls["n"] == 1:
            return fauxAssistantMessage([fauxToolCall("reading_submit", {"summary": "s", "understanding": "u"})],
                                        {"stopReason": "toolUse"})
        return gate.wait()

    model.faux.setResponses([answer] * 6)
    task = asyncio.ensure_future(model.reader().fork(run, "N0", ["fast", "slow"]))
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [node.id for node in run.readings()] == ["N0"]
    assert os.listdir(run.sessions_directory()) == []


async def test_a_session_that_could_not_be_forked_is_not_deleted(project, model, monkeypatch):
    from misaka.core.session_manager import SessionManager

    run = await opened(project)
    _, first = await model.reader().act(run, "N0", "reconstruct")

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(SessionManager, "forkFrom", full)
    _, second = await model.reader().act(run, "N0", "presuppose")
    assert os.path.exists(first.session) and second.session != first.session


async def test_a_continuation_stopped_while_its_session_is_set_up_leaves_no_fork(project, model, monkeypatch):
    from misaka.core import sdk

    run = await opened(project)
    _, first = await model.reader().act(run, "N0", "reconstruct")
    gate = asyncio.Event()

    async def slow(options):
        await gate.wait()

    monkeypatch.setattr(sdk, "createAgentSession", slow)
    task = asyncio.ensure_future(model.reader().act(run, "N0", "presuppose"))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert os.listdir(run.sessions_directory()) == [os.path.basename(first.session)]


async def test_a_reading_led_from_a_live_window_is_not_sent_to_resume(project, model, monkeypatch, tmp_path):
    run = await opened(project)
    leader = str(tmp_path / "live.jsonl")
    engine.write(run, "lead", {}, lambda change: change.update("run", {"leader": leader}))
    monkeypatch.setattr(cli, "leader_in_use", lambda path: True)
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project, session_file=str(tmp_path / "other.jsonl"))
    await harn.commands["read"]["handler"](f"chat {run.id}", ctx)
    assert "open in another window" in ctx.ui.notices[-1][1] and "/resume" not in ctx.ui.notices[-1][1]


async def test_the_terminal_refuses_a_live_leader_even_before_its_file_is_written(project, model, monkeypatch,
                                                                                  tmp_path):
    run = await opened(project)
    engine.write(run, "lead", {}, lambda change: change.update("run", {"leader": str(tmp_path / "unwritten.jsonl")}))
    monkeypatch.setattr(cli, "leader_in_use", lambda path: True)
    with pytest.raises(ReadingError, match="open in a window"):
        await cli.chat(SimpleNamespace(run=run.id, model=None))


async def test_a_continuation_is_handed_the_newest_of_a_long_note(project, model):
    run = await opened(project)
    model.script += [{"understanding": "OLDEST " + "x" * 13_000}, {"understanding": "NEWEST-MARK"}, {}]
    await model.reader().act(run, "N0", "reconstruct")
    await model.reader().act(run, "N0", "reconstruct")
    await model.reader().act(run, "N0", "reconstruct")
    assert "NEWEST-MARK" in model.prompts[-1] and "OLDEST" not in model.prompts[-1]


async def test_a_failing_view_or_notice_never_fails_the_reading(project, model, monkeypatch):
    run = await opened(project)
    monkeypatch.setattr(views, "graph_markdown", lambda run_: 1 / 0)
    node_id, _ = await model.reader().act(run, "N0", "reconstruct")
    assert run.reading(node_id).attrs["actions"]
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    monkeypatch.undo()
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    from misaka.extensions.last_order.reading import prompts

    monkeypatch.setattr(prompts, "leader_mode", lambda run_: 1 / 0)
    assert await run_starts(harn, ctx) is None
    assert set(window.TOOLS) <= set(harn.active)


async def test_a_board_that_cannot_tell_neither_ends_reading_mode_nor_turns_its_tools_off(project, model,
                                                                                          monkeypatch):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    import sqlite3

    await run_starts(harn, ctx)

    def busy(run_):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store.Run, "info", property(busy))
    assert await run_starts(harn, ctx) is None
    assert set(window.TOOLS) <= set(harn.active)


# -- found in the fourth review (2026-10-30) -----------------------------------------------------


async def test_a_session_file_held_open_elsewhere_does_not_fail_a_recorded_action(project, model, monkeypatch):
    run = await opened(project)
    _, first = await model.reader().act(run, "N0", "reconstruct")
    real = os.remove

    def held(path, *args, **kwargs):
        if path == first.session:
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", held)
    node_id, _ = await model.reader().act(run, "N0", "presuppose")
    assert [a["action"] for a in run.reading(node_id).attrs["actions"]] == ["reconstruct", "presuppose"]


async def test_the_terminal_leads_only_a_conversation_that_binds_the_reading(project, tmp_path):
    run = await opened(project)
    path = str(tmp_path / "c.jsonl")
    engine.write(run, "lead", {}, lambda change: change.update("run", {"leader": path}))

    def manager(entries):
        return SimpleNamespace(getBranch=lambda: entries, getSessionFile=lambda: path)

    bind = {"type": "custom", "customType": store.BIND, "data": {"run": run.id}}
    unbind = {"type": "custom", "customType": store.BIND, "data": {"run": None}}
    assert cli.leads(manager([bind]), run.id)
    assert not cli.leads(manager([]), run.id)                 # named by the reading, not bound by the branch
    assert not cli.leads(manager([bind, unbind]), run.id)


async def test_a_board_too_busy_to_answer_is_a_reading_error(project, monkeypatch):
    import sqlite3

    from misaka.core import graph

    run = await opened(project)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(graph, "acquire", locked)
    with pytest.raises(ReadingError, match="too busy"), run.hold():
        pass
    monkeypatch.setattr(graph, "get", locked)
    with pytest.raises(ReadingError, match="too busy"), run.hold():
        pass


async def test_an_open_that_finishes_late_does_not_undo_what_the_window_did_meanwhile(project, model, monkeypatch):
    existing = await opened(project)
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    gate = asyncio.Event()
    real = materials.add

    async def slow(*args, **kwargs):
        await gate.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(materials, "add", slow)
    await harn.commands["read"]["handler"]('open nora.md --question "Late?"', ctx)
    await asyncio.sleep(0.1)
    await harn.commands["read"]["handler"](f"chat {existing.id}", ctx)
    gate.set()
    await settle()
    assert harn.entries[-1]["data"]["run"] == existing.id
    assert engine.find(existing.id).info["leader"] == "/tmp/lo-session.jsonl"
    assert "took up another reading meanwhile" in ctx.ui.notices[-1][1]


async def test_last_order_hears_when_the_readings_files_could_not_be_written(project, model, monkeypatch):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()

    def locked(run_):
        raise PermissionError("graph.md is open in another program")

    monkeypatch.setattr(views, "render", locked)
    out = await harn.tools["reading_act"].execute("c", {"action": "reconstruct"}, None, None, ctx)
    assert "Note: could not update the reading's files" in out["content"][0]["text"]


async def test_a_shortened_note_keeps_whole_sections(project, model):
    run = await opened(project)
    model.script += [{"understanding": "FIRST " + "x" * 500}, {"understanding": "```\ncode\n```\nSECOND"}]
    await model.reader().act(run, "N0", "reconstruct")
    await model.reader().act(run, "N0", "reconstruct")
    short = views.note(run, "N0", limit=200)
    assert "SECOND" in short and "FIRST" not in short and "earlier sections are in" in short
    assert short.count("```") == 2 and "## reconstruct" in short


# -- a lease lost while an operation runs (2026-10-30) ------------------------------------------


def taken_over(run):
    """Another live process takes the reading's lease once this one's has run out."""
    import socket
    import time

    from misaka.core import graph

    run.con.execute("UPDATE graphs SET lease_expires=? WHERE id=?", (int(time.time()) - 1, run.gid))
    run.con.commit()
    other = f"reading:{socket.gethostname()}:{os.getppid()}:other"
    assert graph.acquire(run.con, run.gid, other, ttl=3600, stale=store._stale).acquired
    return other


def session_files(run):
    directory = run.sessions_directory()
    return sorted(os.listdir(directory)) if os.path.isdir(directory) else []


@pytest.fixture
def renewing(monkeypatch):
    monkeypatch.setattr(store, "RENEW_SECONDS", 0.1)


async def test_an_action_whose_lease_is_taken_stops_at_the_next_renewal(project, model, renewing):
    from misaka.core import graph

    run = await opened(project)
    model.faux.setResponses([lambda *a: asyncio.Event().wait()])      # the model never answers
    task = asyncio.ensure_future(model.reader().act(run, "N0", "reconstruct"))
    await asyncio.sleep(0.3)
    other = taken_over(run)
    with pytest.raises(ReadingError, match="taken over"):
        await asyncio.wait_for(task, 2)
    assert run.reading("N0").attrs["actions"] == [] and session_files(run) == []
    assert graph.get(run.con, run.gid).lease_token == other           # the new holder keeps it


async def test_a_fork_whose_lease_is_taken_keeps_none_of_its_lines(project, model, renewing):
    run = await opened(project)
    model.faux.setResponses([lambda *a: asyncio.Event().wait()] * 2)
    task = asyncio.ensure_future(model.reader().fork(run, "N0", ["agency", "money"]))
    await asyncio.sleep(0.3)
    taken_over(run)
    with pytest.raises(ReadingError, match="taken over"):
        await asyncio.wait_for(task, 2)
    assert [node.id for node in run.readings()] == ["N0"] and session_files(run) == []


async def test_a_lease_taken_while_a_document_tool_runs_stops_the_action_too(project, model, renewing,
                                                                             monkeypatch):
    import time

    from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
    from misaka.core.documents import index as corpus
    from misaka.utils.values import read_field

    run = await opened(project)
    reads, real = [], corpus.read_pages

    def slow(*args, **kwargs):
        reads.append(True)
        time.sleep(1)
        return real(*args, **kwargs)

    def step(context, options, state, model_):
        if read_field(context.messages[-1], "role") == "toolResult":
            return fauxAssistantMessage([fauxToolCall("reading_submit", {"summary": "s", "understanding": "u"})],
                                        {"stopReason": "toolUse"})
        return fauxAssistantMessage([fauxToolCall("doc_read", {"doc_id": run.sources()[0].id, "pages": "1"})],
                                    {"stopReason": "toolUse"})

    monkeypatch.setattr(corpus, "read_pages", slow)
    model.faux.setResponses([step] * 4)
    task = asyncio.ensure_future(model.reader().act(run, "N0", "reconstruct"))
    while len(reads) < 2:              # the opening excerpt, then the reader's own doc_read
        await asyncio.sleep(0.02)
    taken_over(run)
    with pytest.raises(ReadingError, match="taken over"):
        await asyncio.wait_for(task, 2)
    assert run.reading("N0").attrs["actions"] == [] and session_files(run) == []


async def test_last_order_hears_of_a_lease_taken_as_an_error_not_as_the_readers_stop(project, model, renewing):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    run = engine.find(harn.entries[-1]["data"]["run"])
    model.faux.setResponses([lambda *a: asyncio.Event().wait()])
    call = asyncio.ensure_future(harn.tools["reading_act"].execute("c", {"action": "reconstruct"}, None, None, ctx))
    await asyncio.sleep(0.3)
    taken_over(run)
    with pytest.raises(ReadingError, match="taken over"):
        await asyncio.wait_for(call, 2)
    assert ctx.aborted == []


async def test_a_stop_that_comes_with_a_lost_lease_is_still_a_stop(project, model, renewing, monkeypatch):
    from misaka.core import graph

    run = await opened(project)
    model.faux.setResponses([lambda *a: asyncio.Event().wait()])
    task = asyncio.ensure_future(model.reader().act(run, "N0", "reconstruct"))
    await asyncio.sleep(0.3)

    def lost_as_the_reader_stops(*args, **kwargs):
        task.cancel()
        return False

    monkeypatch.setattr(graph, "renew", lost_as_the_reader_stops)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)


async def test_a_board_too_busy_to_renew_is_not_a_lost_lease(project, model, renewing, monkeypatch):
    import sqlite3

    from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
    from misaka.core import graph

    run = await opened(project)
    tries = []

    def busy(*args, **kwargs):
        tries.append(True)
        raise sqlite3.OperationalError("database is locked")

    async def slowly(*args):
        await asyncio.sleep(0.5)       # several renewals fail meanwhile
        return fauxAssistantMessage([fauxToolCall("reading_submit", {"summary": "s", "understanding": "u"})],
                                    {"stopReason": "toolUse"})

    monkeypatch.setattr(graph, "renew", busy)
    model.faux.setResponses([slowly, lambda *a: fauxAssistantMessage([fauxText("done")])])
    node_id, _ = await asyncio.wait_for(model.reader().act(run, "N0", "reconstruct"), 5)
    assert len(tries) >= 3 and node_id == "N0" and run.reading("N0").attrs["actions"]


# -- what PR #12's scripts read, and where the port does better (2026-10-31) ---------------------


async def test_graph_json_has_what_pr12s_demo_reads(project, model):
    """``scripts/run_reading_demo.sh`` of PR #12 reads graph.json's session index, and a node's
    line and session; a reference names its pages as numbers and the material it came from."""
    run = await opened(project)
    model.script.append({"sources": [{"doc_id": run.sources()[0].id, "pages": "1"}]})
    await model.reader().act(run, "N0", "reconstruct")
    await model.reader().fork(run, "N0", ["agency"])
    await model.reader().compare(run, ["N0", "N1"], "Where do they part?")
    engine.write(run, "lead", {}, lambda change: change.update(
        "run", {"leader": "/tmp/2026-10-31T01-02-03-456Z_0000-leader.jsonl"}))
    data = json.loads(read(os.path.join(run.directory, "graph.json")))
    sessions = {(record["role"], record.get("nodeId")) for record in data["sessions"]}
    assert sessions == {("leader", None), ("study", "N0"), ("study", "N1"), ("compare", "N2")}
    for record in data["sessions"]:
        assert os.path.basename(record["sessionFile"]).endswith(f"_{record['sessionId']}.jsonl")
        assert record["createdAt"].endswith("Z")
    leader = next(record for record in data["sessions"] if record["role"] == "leader")
    assert leader["sessionId"] == "0000-leader" and leader["createdAt"] == "2026-10-31T01:02:03.456Z"
    nodes = {node["id"]: node for node in data["nodes"]}
    assert [nodes[n]["lineId"] for n in ("N0", "N1", "N2")] == ["L0", "L1", "L2"]
    assert nodes["N1"]["sessionId"] and data["updatedAt"] >= data["createdAt"]
    ref = next(ref for ref in nodes["N0"]["sourceRefs"] if "pages" in ref)      # the place the reader cited
    assert (ref["sourceId"], ref["pageStart"], ref["pageEnd"], ref["origin"]) == \
        (run.sources()[0].id, 1, 1, project.inside)
    assert data["sources"][0]["addedAt"] == data["createdAt"]


def test_the_command_line_takes_pr12s_options_and_prints_paths_whole(project, capsys, monkeypatch):
    assert cli.main(["open", project.inside, "--question", "q", "--workspace", project.workspace,
                     "--model", "any/model", "--json"]) == 0          # open calls no model, but takes --model
    printed = capsys.readouterr().out
    assert printed.count("\n") == 1                                   # one line of JSON
    opened_run = json.loads(printed)
    assert {"graph.json", "nodes", "sessions", "sources"} <= set(os.listdir(opened_run["runDirectory"]))
    assert cli.main(["graph", opened_run["runId"], "--model", "any/model"]) == 0
    out = capsys.readouterr().out
    assert os.path.join(opened_run["runDirectory"], "graph.md") in out
    assert os.path.join(opened_run["runDirectory"], "reading.md") in out
    seen = {}

    async def remember(args, **_):
        seen["model"] = args.model
        return {"runId": "r", "nodeId": "N0", "bodyPath": "nodes/N0.md"}

    monkeypatch.setattr(cli, "run_command", remember)
    monkeypatch.setenv("MISAKA_READING_MODEL", "faux/faux-1")
    assert cli.main(["act", opened_run["runId"], "overview", "--node", "N0"]) == 0
    assert seen["model"] == "faux/faux-1"                             # PR #12's variable, without --model
    assert cli.main(["act", opened_run["runId"], "overview", "--node", "N0", "--model", "other/m"]) == 0
    assert seen["model"] == "other/m"                                 # --model wins

    async def chatting(args):
        seen["chat"] = args.model
        return 0

    monkeypatch.setattr(cli, "chat", chatting)
    assert cli.main(["chat", opened_run["runId"]]) == 0
    assert seen["chat"] is None                    # Last Order keeps her conversation's model in chat


async def test_the_leader_is_handed_the_reading_an_operation_wrote(project, model):
    harn = Harn()
    window.register(harn)
    ctx = window_ctx(harn, model, project)
    await harn.commands["read"]["handler"]('open nora.md --question "Q?"', ctx)
    await settle()
    model.script.append({"understanding": "## What the door means\n\nIt closes on a marriage, not a house."})
    out = await harn.tools["reading_act"].execute("c", {"action": "reconstruct"}, None, None, ctx)
    text = out["content"][0]["text"]
    assert "Summary:" in text and "It closes on a marriage, not a house." in text
    assert "#### What the door means" in text                         # under the action's heading, as in the note


def test_the_leaders_answer_is_what_her_latest_message_says():
    from misaka.ai.types import AssistantMessage, TextContent, ThinkingContent

    said = AssistantMessage(content=[ThinkingContent(thinking="hmm"), TextContent(text="Line one. "),
                                     TextContent(text="Line two.")], api="x", provider="x", model="x",
                            usage={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                                   "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
                            stopReason="stop", timestamp=0)
    assert cli._reply(SimpleNamespace(messages=[{"role": "user", "content": "q"}, said])) == "Line one. Line two."
    assert cli._reply(SimpleNamespace(messages=[])) == ""


async def test_a_chat_turn_that_fails_says_so_on_its_json_line(project, model, monkeypatch, capsys):
    import io
    import sys

    from misaka.agent import guards
    from misaka.core import wiring
    from misaka.core.platform import session as platform_session

    run = await opened(project)

    class Session:
        def __init__(self):
            self.messages = [{"role": "assistant", "content": [{"type": "text", "text": "an earlier answer"}]}]
            self.sessionManager = None

        def subscribe(self, listener):
            return lambda: None

        async def abort(self):
            pass

        async def prompt(self, line):
            raise RuntimeError("Authentication failed")

    async def opened_session(flags, cwd, assembly):
        return object(), Session(), None

    async def disposed(runtime):
        pass

    monkeypatch.setattr(wiring, "role_session_setup", lambda *a, **k: ([], None, {}))
    monkeypatch.setattr(platform_session, "open_session", opened_session)
    monkeypatch.setattr(platform_session, "dispose", disposed)
    monkeypatch.setattr(guards, "install_guards", lambda *a, **k: None)
    monkeypatch.setattr(cli, "leads", lambda manager, run_id: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))
    with pytest.raises(RuntimeError):
        await cli.chat(SimpleNamespace(run=run.id, model=None, json=True))
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line == {"runId": run.id, "reply": "", "ok": False, "error": "Authentication failed"}

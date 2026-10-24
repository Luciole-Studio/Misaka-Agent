"""A research run's own compaction threshold and output limit, chosen in the ``/research`` picker.

They apply to the sessions working for that run -- the window where it was started, its fork
nodes, its Sisters' cards -- and to nothing else: the global settings are never written.
"""
import asyncio
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from misaka.config import home
from misaka.core import session_overrides
from misaka.core.network import worker
from misaka.core.platform import cards, repo, tasks
from misaka.core.research import runs, workflow
from misaka.core.research.wiring import research
from misaka.core.wiring import SessionSpec, spec_overrides


def _session(session_id="s-1", stream=None):
    calls = []

    def default_stream(model, context, options=None):
        calls.append(options)
        return "streamed"

    session = SimpleNamespace(
        sessionManager=SimpleNamespace(getSessionId=lambda: session_id),
        agent=SimpleNamespace(streamFunction=stream or default_stream),
        calls=calls)
    return session


# --- the picker's answers -------------------------------------------------------------------

@pytest.mark.parametrize("answer,expected", [
    ("Global setting", None), ("", None),
    ("0.5", 0.5), ("0.85", 0.85), ("60%", 0.6), ("60", 0.6), (" 0.6 ", 0.6)])
def test_threshold_answers(answer, expected):
    assert research._parse_threshold(answer) == expected


@pytest.mark.parametrize("answer", ["half", "0", "1", "5%", "0.05", "0.99", "100%", "-0.2", "150"])
def test_unusable_threshold_answers_are_refused(answer):
    with pytest.raises(ValueError, match="Compaction threshold"):
        research._parse_threshold(answer)


@pytest.mark.parametrize("answer,expected", [
    ("Model default", None), ("", None), ("64k", 64_000), ("32k", 32_000), ("48000", 48_000),
    ("48,000", 48_000), ("100 k", 100_000), ("1.5k", 1_500)])
def test_output_answers(answer, expected):
    assert research._parse_output(answer) == expected


@pytest.mark.parametrize("answer", ["lots", "500", "64 kb"])
def test_unusable_output_answers_are_refused(answer):
    with pytest.raises(ValueError, match="Output limit"):
        research._parse_output(answer)


@pytest.mark.parametrize("limits,message", [
    ({"context_threshold": 1.2}, "context_threshold"), ({"context_threshold": True}, "context_threshold"),
    ({"context_threshold": 0.05}, "context_threshold"),
    ({"max_output_tokens": 500}, "max_output_tokens"), ({"max_output_tokens": "64k"}, "max_output_tokens")])
def test_saved_limits_are_validated(limits, message):
    with pytest.raises(ValueError, match=message):
        runs.normalize_limits(limits)


def test_a_run_without_its_own_settings_follows_the_global_ones():
    assert runs.session_overrides({"limits_json": json.dumps({"max_depth": 2})}) == {}
    chosen = {"limits_json": json.dumps({"context_threshold": 0.5, "max_output_tokens": 64_000})}
    assert runs.session_overrides(chosen) == {"context_threshold": 0.5, "max_output_tokens": 64_000}


# --- the output limit -----------------------------------------------------------------------

def test_the_output_limit_caps_every_request_but_never_raises_the_model_maximum():
    session = _session()
    release = session_overrides.apply(session, {"max_output_tokens": 64_000})
    session.agent.streamFunction(SimpleNamespace(maxTokens=128_000), None, SimpleNamespace(maxTokens=None))
    session.agent.streamFunction(SimpleNamespace(maxTokens=32_000), None, None)
    session.agent.streamFunction(SimpleNamespace(maxTokens=128_000), None, {"maxTokens": 8_000})
    assert [getattr(o, "maxTokens", None) or o["maxTokens"] for o in session.calls] == [64_000, 32_000, 8_000]
    release()
    session.agent.streamFunction(SimpleNamespace(maxTokens=128_000), None, None)
    assert session.calls[-1] is None


def test_a_session_is_wrapped_once_however_often_it_is_applied():
    session = _session()
    session_overrides.apply(session, {"max_output_tokens": 64_000})()
    wrapped = session.agent.streamFunction
    session_overrides.apply(session, {"max_output_tokens": 32_000})
    assert session.agent.streamFunction is wrapped


def test_nothing_is_applied_without_overrides_or_a_session_id():
    session = _session()
    stream = session.agent.streamFunction
    session_overrides.apply(session, {})
    session_overrides.apply(SimpleNamespace(agent=session.agent), {"max_output_tokens": 64_000})
    assert session.agent.streamFunction is stream


# --- the compaction threshold ---------------------------------------------------------------

def _settings(document):
    home.path("settings").parent.mkdir(parents=True, exist_ok=True)
    home.path("settings").write_text(json.dumps(document), encoding="utf-8")


def _ctx(session_id):
    return SimpleNamespace(sessionManager=SimpleNamespace(getSessionId=lambda: session_id), cwd="/tmp")


def test_lcm_reads_the_session_threshold_through_its_own_config_channel(monkeypatch):
    config_bridge = pytest.importorskip("misaka.extensions.misaka_lcm.host.config_bridge")   # a plugin
    monkeypatch.delenv("LCM_CONTEXT_THRESHOLD", raising=False)
    _settings({"lcm": {"context_threshold": 0.7}})
    assert config_bridge.load_config(ctx=_ctx("plain")).context_threshold == 0.7
    release = session_overrides.apply(_session("researching"), {"context_threshold": 0.5})
    try:
        config = config_bridge.load_config(ctx=_ctx("researching"))
        assert config.context_threshold == 0.5
        assert config.config_sources["context_threshold"] == "config_yaml:lcm.context_threshold"
        assert config_bridge.load_config(ctx=_ctx("plain")).context_threshold == 0.7
    finally:
        release()
    assert config_bridge.load_config(ctx=_ctx("researching")).context_threshold == 0.7


def test_a_running_engine_recomputes_its_trigger_when_the_threshold_changes(monkeypatch):
    config_bridge = pytest.importorskip("misaka.extensions.misaka_lcm.host.config_bridge")   # a plugin
    lcm_settings = pytest.importorskip("misaka.extensions.misaka_lcm.host.settings")
    monkeypatch.delenv("LCM_CONTEXT_THRESHOLD", raising=False)
    _settings({"lcm": {"context_threshold": 0.7}})
    config = config_bridge.load_config(ctx=_ctx("window"))
    recomputed = []
    built = SimpleNamespace(_config=config, raw_context_length=1_000_000, _context_length_source="update_model",
                            _set_context_length=lambda length, source: recomputed.append(
                                (length, source, built._config.context_threshold)))
    lcm_settings.refresh(built, _ctx("window"))
    assert recomputed == []                                   # nothing changed, nothing recomputed
    release = session_overrides.apply(_session("window"), {"context_threshold": 0.5})
    lcm_settings.refresh(built, _ctx("window"))
    assert recomputed == [(1_000_000, "update_model", 0.5)]
    release()
    lcm_settings.refresh(built, _ctx("window"))
    assert recomputed[-1] == (1_000_000, "update_model", 0.7)


# --- sessions built for the run -------------------------------------------------------------

async def test_a_spec_with_overrides_applies_them_to_its_session_and_again_after_a_switch():
    part = session_overrides.part(SessionSpec(profile_dir="p", role="r", workspace="w", kind="card",
                                              overrides=spec_overrides({"context_threshold": 0.5})))
    ids = iter(["first", "second"])
    current = {"id": next(ids)}
    session = _session()
    session.sessionManager.getSessionId = lambda: current["id"]
    part.attach(session)
    await part.session_start({}, None)
    assert session_overrides.value("first", "context_threshold") == 0.5
    current["id"] = next(ids)
    await part.session_start({}, None)
    assert session_overrides.value("first", "context_threshold") is None
    assert session_overrides.value("second", "context_threshold") == 0.5
    await part.session_shutdown({}, None)
    assert session_overrides.value("second", "context_threshold") is None
    assert session_overrides.part(SessionSpec(profile_dir="p", role="r", workspace="w", kind="card")) is None


def test_a_child_reads_what_its_parent_handed_it(monkeypatch):
    assert session_overrides.to_env({}) == {}
    env = session_overrides.to_env({"context_threshold": 0.5, "max_output_tokens": 64_000, "other": 1})
    monkeypatch.setenv(session_overrides.ENV, env[session_overrides.ENV])
    assert session_overrides.from_env() == {"context_threshold": 0.5, "max_output_tokens": 64_000}
    monkeypatch.setenv(session_overrides.ENV, "not json")
    assert session_overrides.from_env() == {}


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        yield con


def test_a_research_card_carries_its_runs_settings(board, tmp_path):
    run = runs.create(board, workspace=str(tmp_path), question="Why did it fail?",
                      limits={"context_threshold": 0.5, "max_output_tokens": 64_000})
    plain = runs.create(board, workspace=str(tmp_path), question="Why did it succeed?")
    root = runs.nodes(board, run["id"])[0]
    card = cards.create(board, run["workspace"], "Read the sources", "body", "10032")
    runs.link_task(board, run["id"], card, kind="research", node=root, local_id="sources")
    other = cards.create(board, run["workspace"], "An ordinary card", "body", "10032")
    extras = worker.card_extras(board, tasks.get(board, card), include_materials=False)
    assert extras["_session_overrides"] == {"context_threshold": 0.5, "max_output_tokens": 64_000}
    assert worker.card_extras(board, tasks.get(board, other), include_materials=False)["_session_overrides"] == {}
    assert runs.session_overrides(plain) == {}


# --- the picker, end to end -----------------------------------------------------------------

@pytest.fixture
async def chat(board, tmp_path, monkeypatch):
    state = SimpleNamespace(notices=[], seen=[], started=asyncio.Event(), result={"answers": {}})
    monkeypatch.setattr(research, "_con", lambda: board)
    monkeypatch.setattr(research, "_cfg", dict)
    monkeypatch.delenv("MISAKA_NET_PANE", raising=False)
    monkeypatch.setattr(cards, "init_project", lambda *a, **k: None)

    async def drive(con, cfg, spawner, **kwargs):
        # What the window's own session runs with while the run works in it.
        state.seen.append((session_overrides.value("window", "context_threshold"),
                           session_overrides.value("window", "max_output_tokens")))
        state.run = runs.get(con, kwargs["run_id"])
        state.started.set()
        return {"reason": "waiting_input", "questions": ["Scope?"]}

    async def custom(factory):
        factory(None, None, None, lambda result: None)
        return state.result

    monkeypatch.setattr(workflow, "run", drive)
    state.part = research.ResearchPart()
    window = _session("window")
    window.moments = SimpleNamespace(send_message=lambda *a: None, send_user_message=lambda *a: None)
    state.part.attach(window)
    state.ctx = SimpleNamespace(cwd=str(tmp_path), isIdle=lambda: True, sessionManager=SimpleNamespace(sessionId="window"),
                                ui=SimpleNamespace(custom=custom, notify=lambda text, kind: state.notices.append(text)))
    state.command = state.part.commands[0].handler
    try:
        yield state
    finally:
        await state.part._cleanup({}, state.ctx)


async def _start(chat, answers):
    chat.result["answers"].update(answers)
    await chat.command("", chat.ctx)
    await chat.part.input({"text": "Why did it fail?", "source": "user"}, chat.ctx)
    await asyncio.wait_for(chat.started.wait(), 3)
    await asyncio.sleep(0.05)


async def test_the_pickers_choice_is_the_runs_and_its_window_uses_it_while_the_run_works(chat, board, tmp_path):
    _settings({"lcm": {"context_threshold": 0.7}})
    await _start(chat, {research._THRESHOLD_QUESTION: "0.5", research._OUTPUT_QUESTION: "64k"})
    assert runs.session_overrides(chat.run) == {"context_threshold": 0.5, "max_output_tokens": 64_000}
    assert chat.seen == [(0.5, 64_000)]
    assert session_overrides.value("window", "context_threshold") is None     # given back when the run paused
    assert any("compaction at 0.5 of the window | output limit 64,000 tokens" in text for text in chat.notices)
    assert "compaction at 0.5" in research._status(board, chat.run["id"], str(tmp_path))
    assert json.loads(home.path("settings").read_text(encoding="utf-8")) == {"lcm": {"context_threshold": 0.7}}


async def test_the_defaults_leave_the_run_on_the_global_settings(chat):
    """The default names no value: which threshold applies globally is the context engine's own
    setting, and the core does not read another subsystem's settings to show it."""
    await _start(chat, {research._THRESHOLD_QUESTION: "Global setting", research._OUTPUT_QUESTION: "Model default"})
    assert runs.session_overrides(chat.run) == {}
    assert chat.seen == [(None, None)]


def test_a_sisters_card_child_is_handed_the_runs_settings_and_nothing_else_is():
    from misaka.core.network.sister_runtime import _SisterManager

    manager = object.__new__(_SisterManager)
    manager.owner_context = {"run_id": "r_1"}
    manager.session_overrides = {"context_threshold": 0.5, "max_output_tokens": 64_000}
    env = manager.child_env_extra(None)
    assert json.loads(env[session_overrides.ENV]) == {"context_threshold": 0.5, "max_output_tokens": 64_000}
    manager.session_overrides = {}
    assert session_overrides.ENV not in manager.child_env_extra(None)

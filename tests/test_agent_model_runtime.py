"""Native model routing/persistence against a loopback-only SSE endpoint."""
import asyncio
import json
import socket
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_subagent_native_startup import (
    isolated as isolated,  # noqa: PLC0414 - explicit fixture re-export
)

from misaka.config import home


@pytest.fixture
def local_models(isolated, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    connect, lookup = socket.socket.connect, socket.getaddrinfo

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] != "127.0.0.1":
            raise AssertionError(f"External connection forbidden: {address}")
        return connect(sock, address)

    def local_lookup(host, *args, **kwargs):
        if host not in {"127.0.0.1", b"127.0.0.1"}:
            raise AssertionError(f"External DNS forbidden: {host}")
        return lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket, "getaddrinfo", local_lookup)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path.split("/")[1], payload))
            events = [
                {"type": "message_start", "message": {
                    "id": "fixture-message", "type": "message", "role": "assistant",
                    "model": payload["model"], "content": [], "stop_reason": None,
                    "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "Loopback fixture answer."}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                 "usage": {"output_tokens": 3}},
                {"type": "message_stop"},
            ]
            body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                           for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pairs = {"fixture-lo": "shared", "fixture-a": "shared", "fixture-b": "different",
             "fixture-global": "global"}
    home.path("models").write_text(json.dumps({"providers": {
        provider: {"baseUrl": f"http://127.0.0.1:{server.server_port}/{provider}",
                   "api": "anthropic-messages", "apiKey": "fixture-not-a-real-key",
                   "models": [{"id": model, "name": model, "reasoning": False, "input": ["text"],
                               "contextWindow": 200000, "maxTokens": 1024,
                               "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}
        for provider, model in pairs.items()}}))
    home.path("settings").write_text(json.dumps({
        "defaultProvider": "fixture-global", "defaultModel": "global", "retry": {"enabled": False},
        # A shared cycling scope must not replace a role's independent default.
        "enabledModels": ["fixture-global/global"], "theme": "dark",
    }))
    for role, provider in (("last_order", "fixture-lo"), ("sisters/10032", "fixture-a"),
                           ("sisters/10036", "fixture-b")):
        profile = home.path("roles_root") / role
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "settings.json").write_text(json.dumps({"defaultProvider": provider, "defaultModel": pairs[provider],
                                                         "custom": "preserve"}))
    try:
        yield isolated, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@asynccontextmanager
async def opened(root, role, kind, flags=()):
    from misaka.core.platform.session import dispose, open_session
    from misaka.core.wiring import Assembly, SessionSpec

    cwd = str(root / "workspace")
    assembly = Assembly(extension_factories=[], custom_tools=[], parts=[], spec=SessionSpec(
        profile_dir=str(home.path("roles_root") / role), role=role, workspace=cwd, kind=kind))
    runtime, session, error = await open_session([
        "--no-tools", "--no-extensions", "--no-prompt-templates", "--no-themes", "--no-context-files",
        "--session-dir", str(root / "sessions" / role / kind), *flags,
    ], cwd, assembly)
    try:
        assert not error, error
        yield runtime, session
    finally:
        await dispose(runtime)


async def prompt(session, text):
    async with asyncio.timeout(15):
        await session.prompt(text)
    assert "Loopback fixture answer." in str(session.agent.state.messages[-1])


@pytest.mark.parametrize("role,kind,pair", [
    ("last_order", "foreground", ("fixture-lo", "shared")),
    ("sisters/10032", "bare", ("fixture-a", "shared")),
    ("sisters/10036", "card", ("fixture-b", "different")),
])
async def test_native_role_startup_routes_each_provider(local_models, role, kind, pair):
    root, requests = local_models
    async with opened(root, role, kind) as (_, session):
        assert (session.model.provider, session.model.id) == pair
        await prompt(session, f"ROUTE_MARKER {role}")
    assert [(provider, data["model"]) for provider, data in requests] == [pair]


async def test_native_saved_default_isolated_and_reused(local_models):
    root, requests = local_models
    paths = [home.path("settings"), home.path("roles_root") / "last_order/settings.json",
             home.path("profiles_root") / "10036/settings.json"]
    before = [path.read_bytes() for path in paths]
    async with opened(root, "sisters/10032", "foreground") as (_, session):
        target = session.modelRegistry.find("fixture-lo", "shared")
        await session.setModel(target, persist=True)
        await session.settingsManager.flush()
        assert session.settingsManager.getDefaultModelPair() == ("fixture-lo", "shared")
        await prompt(session, "SAVED_MARKER")
    assert [path.read_bytes() for path in paths] == before
    assert json.loads((home.path("profiles_root") / "10032/settings.json").read_text()) == {
        "defaultProvider": "fixture-lo", "defaultModel": "shared", "custom": "preserve"}
    async with opened(root, "sisters/10032", "foreground") as (_, session):
        assert (session.model.provider, session.model.id) == ("fixture-lo", "shared")
        await prompt(session, "REOPEN_MARKER")
    assert [(provider, data["model"]) for provider, data in requests] == [("fixture-lo", "shared")] * 2


async def test_native_resume_preserves_temporary_model_and_transcript(local_models):
    root, requests = local_models
    profile = home.path("profiles_root") / "10032/settings.json"
    before = profile.read_bytes()
    async with opened(root, "sisters/10032", "foreground") as (_, session):
        await session.setModel(session.modelRegistry.find("fixture-b", "different"))
        await prompt(session, "BEFORE_RESUME_MARKER")
        path, session_id = session.sessionFile, session.sessionId
    assert profile.read_bytes() == before
    async with opened(root, "sisters/10032", "foreground", ["--session", path]) as (_, session):
        assert session.sessionId == session_id
        assert (session.model.provider, session.model.id) == ("fixture-b", "different")
        await prompt(session, "AFTER_RESUME_MARKER")
    assert profile.read_bytes() == before
    assert [(provider, data["model"]) for provider, data in requests] == [("fixture-b", "different")] * 2
    assert "BEFORE_RESUME_MARKER" in json.dumps(requests[-1][1]["messages"])
    assert "AFTER_RESUME_MARKER" in json.dumps(requests[-1][1]["messages"])


async def test_native_child_default_guard_preserves_parent_and_global(local_models):
    root, requests = local_models
    paths = [home.path("settings"), home.path("profiles_root") / "10032/settings.json"]
    before = [path.read_bytes() for path in paths]
    async with opened(root, "sisters/10032", "child", ["--model", "fixture-a/shared"]) as (_, session):
        assert session.settingsManager.getModelProfile() is None
        target = session.modelRegistry.find("fixture-b", "different")
        with pytest.raises(ValueError, match="/agents"):
            await session.setModel(target, persist=True)
        assert (session.model.provider, session.model.id) == ("fixture-a", "shared")
        await session.setModel(target)
        await prompt(session, "CHILD_MARKER")
        await session.settingsManager.flush()
    assert [path.read_bytes() for path in paths] == before
    assert [(provider, data["model"]) for provider, data in requests] == [("fixture-b", "different")]


@pytest.mark.parametrize("model_preference", [None, "medium"])
async def test_lo_thinking_defaults_and_native_research_fork(local_models, model_preference):
    from misaka.core.research.planner import fork_session

    root, requests = local_models
    models_path = home.path("models")
    models = json.loads(models_path.read_text())
    model = models["providers"]["fixture-lo"]["models"][0]
    model.update(reasoning=True, thinkingLevelMap={"max": "max"},
                 compat={"forceAdaptiveThinking": True})
    models_path.write_text(json.dumps(models))
    settings_path = home.path("settings")
    settings = json.loads(settings_path.read_text())
    settings["defaultThinkingLevel"] = "max"
    if model_preference:
        settings["modelThinkingLevels"] = {"fixture-lo/shared": model_preference}
    settings_path.write_text(json.dumps(settings))

    # Both transports enter the same native settings path; no phase sets its own effort.
    for kind in ("foreground", "headless"):
        async with opened(root, "last_order", kind) as (_, session):
            assert session.agent.state.thinkingLevel == (model_preference or "max")
    async with opened(root, "last_order", "foreground") as (_, session):
        session.setThinkingLevel("low")
        await prompt(session, "THINKING_FORK_MARKER")
        source = session.sessionFile
    fork = fork_session(source, str(root / "research-fork"))
    assert fork and fork != source
    for path in (source, fork):
        async with opened(root, "last_order", "headless", ["--session", path]) as (_, session):
            assert session.agent.state.thinkingLevel == "low"
    assert len(requests) == 1
    assert requests[0][1]["output_config"]["effort"] == "low"

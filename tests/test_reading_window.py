"""The window must pass its selected location and model into the reading engine."""
import asyncio
from types import SimpleNamespace

from misaka.extensions.last_order import reading


class UI:
    def notify(self, *_args):
        pass

    def setStatus(self, *_args):
        pass

    def setWidget(self, *_args):
        pass


def test_reading_window_uses_requested_workspace_and_runs_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(reading.host, "persona_for", lambda _profile: {})
    original, other = tmp_path / "original", tmp_path / "other"
    original.mkdir()
    other.mkdir()
    window = reading._Window(SimpleNamespace(workspace=str(original), profile_dir=None))
    first_args = SimpleNamespace(workspace=str(other), runs_dir=str(other / "runs"), intent=None)
    model = {"provider": "example", "id": "text"}
    first = window.session_for(first_args, model)
    assert first.workspace == str(other.resolve())
    assert first.runs_directory == str((other / "runs").resolve())
    assert window.session_for(SimpleNamespace(), None) is first
    second = window.session_for(SimpleNamespace(runs_dir=str(other / "more-runs")), None)
    assert second is not first
    assert second.workspace == first.workspace
    assert second.runs_directory == str((other / "more-runs").resolve())
    assert second.handover == model
    window.terminate()


def test_reading_say_resolves_model_after_a_model_free_open(tmp_path, monkeypatch):
    class Session:
        def __init__(self, workspace, *, handover=None, **_kwargs):
            self.workspace = workspace
            self.runs_directory = None
            self.reader_intent = None
            self.handover = handover
            self.messages = []

        def terminate(self):
            pass

        async def say(self, _run_id, _message):
            self.messages.append(self.handover)
            return "A reading reply"

    selected = {"provider": "selected", "id": "text"}

    async def handover(_args, _ctx, _ui):
        return selected

    monkeypatch.setattr(reading.host, "ReadingSession", Session)
    monkeypatch.setattr(reading.host, "persona_for", lambda _profile: {})
    monkeypatch.setattr(reading, "_handover", handover)
    window = reading._Window(SimpleNamespace(workspace=str(tmp_path), profile_dir=None))
    window.session_for(SimpleNamespace(), None)  # /read open creates a model-free engine.

    async def run():
        await reading._say(window, ["run", "Continue reading"], SimpleNamespace(ui=UI()))
        await window.task

    asyncio.run(run())
    assert window.session.messages == [selected]

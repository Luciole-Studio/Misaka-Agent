"""A run whose root conversation was recorded but never written (it failed before the first entry
was persisted) is resumed in a new conversation: asked for by name, the missing file raised
FileNotFoundError on every resume, and the CLI showed a traceback (0.18.10 sweep)."""
import os
from contextlib import closing

import pytest

from misaka.config import current_config
from misaka.core.platform import repo, tasks
from misaka.core.research import node as research_node
from misaka.core.research import runs, workflow


async def test_a_resume_does_not_ask_for_a_conversation_that_was_never_written(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    cfg = dict(current_config())
    cfg["db"] = str(tmp_path / "board.db")
    with closing(tasks.connect(cfg["db"])) as con:
        runs.init(con)
        run = runs.create(con, workspace=str(tmp_path), question="Fixture question")
        missing = str(tmp_path / "sessions" / "root.jsonl")
        os.makedirs(os.path.dirname(missing))
        runs.set_state(con, run["id"], status="failed", root_session=missing)
        with pytest.raises(RuntimeError) as raised:
            await workflow.run(con, cfg, research_node.ProcessSpawner(), run_id=run["id"], resume=True,
                               poll_seconds=0.01)
        assert not isinstance(raised.value.__context__, FileNotFoundError)
        assert "No model is available" in str(raised.value)

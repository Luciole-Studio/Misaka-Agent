"""Optional material metadata must not prevent preparing or restoring a card."""
import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from misaka.core.network import sister_runtime, worker
from misaka.core.platform import cards


class CardRestorePromptContextTests(unittest.TestCase):
    def test_restore_ignores_corrupt_markdown_and_keeps_research_relation(self):
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(":memory:")) as con:
            root = Path(directory)
            (root / "downloads").mkdir()
            (root / "downloads" / "broken.md").write_bytes(b"\xff")
            con.row_factory = sqlite3.Row
            con.execute("CREATE TABLE research_run_tasks(task_id TEXT, run_id TEXT, branch_id TEXT, kind TEXT)")
            con.execute("INSERT INTO research_run_tasks VALUES ('card-1','run-1','node-1','investigation')")
            row = {"id": "card-1", "workspace": directory, "generation": 1, "agent_id": "agent-1",
                   "session_file": "", "assignee": "10032", "claim_lock": None}
            runtime = SimpleNamespace(con=con, cfg={}, _handles={}, session=object(),
                                      _sister_semaphore=None, _owned_claims=set())
            manager = SimpleNamespace(
                _session_dir=lambda _: None,
                _find_task_async=AsyncMock(return_value=SimpleNamespace(transcript=root / "session.jsonl")),
            )
            with patch.object(sister_runtime.db, "get", return_value=row), \
                    patch.object(sister_runtime, "_SisterManager", return_value=manager) as factory, \
                    patch.object(worker, "materials_on_hand", wraps=worker.materials_on_hand) as materials:
                handle = asyncio.run(sister_runtime.SisterRuntime._restore(runtime, "card-1", None))
            materials.assert_not_called()
            self.assertEqual(handle.board_id, "card-1")
            prepared = factory.call_args.args[2]
            self.assertEqual(prepared["_research"], {
                "run_id": "run-1", "branch_id": "node-1", "kind": "investigation",
            })
            self.assertEqual(prepared["_materials"], "")
            assert "_colleagues" not in prepared
            self.assertNotIn("_research", row)

    def test_prepare_keeps_material_snapshot_despite_corrupt_markdown(self):
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(":memory:")) as con:
            root = Path(directory)
            (root / "downloads").mkdir()
            (root / "downloads" / "notes.txt").write_text("Existing source material")
            (root / "downloads" / "plain.md").write_text("Markdown without provenance")
            (root / "downloads" / "broken.md").write_bytes(b"\xff")
            runtime = SimpleNamespace(con=con, cfg={"token_cap": 100})
            row = {"id": "card-1", "workspace": directory, "generation": 1,
                   "claim_lock": "fixture", "assignee": "10032"}
            with patch.object(cards, "attachment_list", return_value=[]), \
                    patch.object(worker, "card_handoffs", return_value=[]), \
                    patch.object(sister_runtime.budget, "status", return_value={"mode": "normal"}):
                prepared = sister_runtime.SisterRuntime._prepare_card(runtime, row)
            self.assertIn("downloads/notes.txt", prepared["_materials"])
            self.assertIn("- `downloads/plain.md`", prepared["_materials"].splitlines())
            self.assertIn("- `downloads/broken.md`", prepared["_materials"].splitlines())
            self.assertIn("snapshot", prepared["_materials"])
            self.assertNotIn("Before any new fetch", prepared["_materials"])
            self.assertIsNone(prepared["_research"])

    def test_material_snapshot_retains_path_if_file_disappears_during_metadata_read(self):
        from misaka.core.web import evidence

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "downloads" / "removed.md"
            path.parent.mkdir()
            path.write_text("---\nsource_url: \"https://example.invalid/source\"\n---\n")
            read_provenance = evidence.read_provenance

            def remove_before_read(filename):
                Path(filename).unlink()
                return read_provenance(filename)

            with patch.object(evidence, "read_provenance", side_effect=remove_before_read):
                materials = worker.materials_on_hand(directory)
            self.assertFalse(path.exists())
            self.assertIn("- `downloads/removed.md`", materials.splitlines())


if __name__ == "__main__":
    unittest.main()

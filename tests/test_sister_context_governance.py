"""Offline role/catalog and card-entry parity checks; no sessions or live databases."""
import asyncio
import inspect
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from misaka.cli import card_shell
from misaka.core import wiring
from misaka.core.network import dispatch, roster, sister_runtime, worker
from misaka.core.network.wiring import capabilities
from misaka.core.platform import cards
from misaka.core.subagent.runtime import SubagentManager


def catalog_payload(text):
    return json.loads(text.split('<<<UNTRUSTED-DATA name="sister-capabilities">>>\n', 1)[1]
                      .split('\n<<<END-UNTRUSTED-DATA>>>', 1)[0])


class SisterCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_matrix_and_self_exclusion(self):
        entries = [{"id": sid, "description": "Summary", "profile": "中" * 220,
                    "skills": ["PRIVATE_SKILL"], "model": "PRIVATE_MODEL"}
                   for sid in ("10032", "10033")]
        for role, profile in (("last_order", "/fixture/last_order"),
                              ("sisters/10032", "/fixture/sisters/10032")):
            allowed = ({"foreground", "dm", "bare"} if role == "last_order"
                       else {"foreground", "dm", "card", "beast"})
            for kind in wiring.KINDS:
                with self.subTest(role=role, kind=kind):
                    spec = wiring.SessionSpec(profile, role, "/fixture", kind,
                                              sister_catalog=tuple(entries))
                    part = (capabilities.part(spec)
                            if wiring._qualifies(capabilities, wiring.role_key(spec), kind) else None)
                    self.assertEqual(part is not None, kind in allowed)
                    if part is None:
                        continue
                    text = (await part.before_agent_start({"systemPrompt": "BASE"}, None))["systemPrompt"]
                    data = catalog_payload(text)
                    self.assertEqual([item["id"] for item in data],
                                     ["10032", "10033"] if role == "last_order" else ["10033"])
                    self.assertEqual(text.count("## Sister capability profiles"), 1)
                    self.assertNotIn("PRIVATE_", text)
                    for item in data:
                        self.assertEqual(set(item), {"id", "description", "profile_preview"})
                        self.assertEqual(len(item["profile_preview"]), 200)
        self.assertIn("skills", entries[0])

    async def test_empty_sister_snapshot_never_reloads_or_adds_lo_research_contract(self):
        part = capabilities.SisterCapabilitiesPart("/fixture", [], sister_id="10032", research_context=True)
        with patch.object(capabilities, "routing_catalog", side_effect=AssertionError("unexpected reload")), \
                patch("misaka.core.research.prompting.system_context", side_effect=AssertionError("LO contract")):
            text = (await part.before_agent_start({"systemPrompt": "BASE"}, None))["systemPrompt"]
        self.assertEqual(catalog_payload(text), [])

    async def test_live_catalog_reads_only_introductions(self):
        with tempfile.TemporaryDirectory() as directory:
            for sid in ("10032", "10033"):
                folder = Path(directory) / sid
                folder.mkdir()
                (folder / "DESCRIBE.md").write_text("---\ndescription: Methods\n---\n" + "x" * 201)
                (folder / "config.json").write_text('{"model":"PRIVATE_MODEL"}')
            with patch.object(roster, "capability_catalog", side_effect=AssertionError("skill discovery")):
                data = roster.routing_catalog(directory)
            self.assertEqual([item["id"] for item in data], ["10032", "10033"])
            self.assertTrue(all(set(item) == {"id", "description", "profile_preview"} for item in data))
            self.assertTrue(all(len(item["profile_preview"]) == 200 for item in data))


class CardContextTests(unittest.TestCase):
    def test_card_extras_and_single_catalog_owner(self):
        with closing(sqlite3.connect(":memory:")) as con:
            con.row_factory = sqlite3.Row
            con.execute("CREATE TABLE research_run_tasks(task_id TEXT, run_id TEXT, branch_id TEXT, kind TEXT)")
            con.execute("INSERT INTO research_run_tasks VALUES ('card-1','run-1','node-1','investigation')")
            row = {"id": "card-1", "assignee": "10032", "workspace": "/fixture"}
            with patch.object(worker, "colleague_lines", return_value=["PRIVATE_DUPLICATE_CATALOG"]) as peers, \
                    patch.object(worker, "materials_on_hand", return_value="Existing material"):
                extra = worker.card_extras(con, row, include_colleagues=False)
                peers.assert_not_called()
                self.assertEqual(worker.card_extras(con, row)["_colleagues"], ["PRIVATE_DUPLICATE_CATALOG"])
            self.assertEqual(extra["_research"], {"run_id": "run-1", "branch_id": "node-1", "kind": "investigation"})
            self.assertEqual(extra["_materials"], "Existing material")
            text = worker.card_prompt({**row, **extra, "body": "Task", "_colleagues": ["PRIVATE_DUPLICATE_CATALOG"]})
            self.assertIn("Existing material", text)
            self.assertNotIn("PRIVATE_DUPLICATE_CATALOG", text)
            self.assertNotIn("misaka_ally_list", text)
            self.assertNotIn("request_input=true", text)  # owned by the active messaging tool
            self.assertTrue(worker.research_addendum_flags(extra))
            self.assertEqual(worker.research_addendum_flags({"_research": None}), [])

    def test_durable_prepare_uses_shared_card_context(self):
        runtime = SimpleNamespace(con=object(), cfg={"token_cap": 100})
        row = {"id": "card-1", "workspace": "/fixture", "generation": 1, "claim_lock": "fixture"}
        extra = {"_research": {"run_id": "run-1"}, "_materials": "Materials", "_colleagues": []}
        with patch.object(cards, "attachment_list", return_value=[]), \
                patch.object(worker, "card_handoffs", return_value=[]), \
                patch.object(worker, "card_extras", return_value=extra) as context, \
                patch.object(sister_runtime.budget, "status", return_value={"mode": "normal"}):
            prepared = sister_runtime.SisterRuntime._prepare_card(runtime, row)
        context.assert_called_once_with(runtime.con, row, runtime.cfg, include_colleagues=False)
        self.assertEqual(prepared["_research"], extra["_research"])
        self.assertEqual(prepared["_materials"], "Materials")
        self.assertNotIn("_research", row)

    def test_pane_prepare_uses_shared_card_context_before_session_setup(self):
        class SetupReached(Exception):
            pass

        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "10032"
            profile.mkdir()
            row = {"id": "card-1", "workspace": directory, "assignee": "10032"}
            extra = {"_research": {"run_id": "run-1"}, "_materials": "Materials", "_colleagues": []}
            con = sqlite3.connect(":memory:")
            with patch.object(card_shell.db, "connect", return_value=con), \
                    patch.object(card_shell.db, "get", return_value=row), \
                    patch.object(card_shell.db, "workspace_for", return_value=directory), \
                    patch.object(worker, "card_handoffs", return_value=[]), \
                    patch.object(worker, "card_extras", return_value=extra) as context, \
                    patch.object(cards, "attachment_list", return_value=[]), \
                    patch.dict(card_shell.CFG, {"db": str(Path(directory) / "fixture.db"), "profiles_root": directory}), \
                    patch.object(card_shell, "current_config", return_value={"provider": "fixture", "default_model": "fixture"}), \
                    patch.dict("os.environ", {"MISAKA_USAGE_CLAIM_LOCK": "", "MISAKA_USAGE_GENERATION": ""}), \
                    patch.object(worker, "card_session_setup", side_effect=SetupReached) as setup, \
                    self.assertRaises(SetupReached):
                card_shell.launch("card-1")
            context.assert_called_once_with(con, row, include_colleagues=False)
            self.assertEqual(setup.call_args.args[0]["_research"], extra["_research"])
            self.assertEqual(setup.call_args.args[0]["_materials"], "Materials")

    def test_dispatch_and_restore_keep_shared_context_path(self):
        # The asynchronous owners are not run: verify both entrances feed the same
        # tested helper, without starting processes or advancing any task lease.
        for function in (dispatch.run_task, sister_runtime.SisterRuntime._restore):
            source = inspect.getsource(function)
            self.assertIn("worker.card_extras(", source)
            self.assertIn("include_colleagues=False", source)

    def test_durable_child_flags_include_research_on_fresh_and_resumed_starts(self):
        from misaka.config.identity import COMMON_CHARTER, SISTER_ROLE
        from misaka.core.system_prompt import build_system_prompt
        from misaka.core.tools.office import office_tool_system_prompt_contribution
        manager = object.__new__(sister_runtime._SisterManager)
        for persona in ("Persisted old persona", f"Current persona\n\n{COMMON_CHARTER}\n\n{SISTER_ROLE}"):
            task = SimpleNamespace(definition=SimpleNamespace(prompt=persona))
            for research in (None, {"run_id": "run-1"}):
                manager.research = research
                with patch.object(SubagentManager, "_child_flags", new=AsyncMock(
                        return_value=["--system-prompt", "/fixture/persona.md", "--approve"])):
                    flags = asyncio.run(manager._child_flags(task))
                self.assertNotIn("--system-prompt", flags)
                self.assertEqual(flags[:3], ["--append-system-prompt", "/fixture/persona.md", "--approve"])
                self.assertEqual(task.definition.prompt, persona)
                sections = [persona] + [flags[i + 1] for i, value in enumerate(flags)
                                        if value == "--append-system-prompt" and i]
                prompt = build_system_prompt({"cwd": "/fixture", "appendSystemPrompt": "\n\n".join(sections),
                                              "selectedTools": ["office"],
                                              "toolSnippets": {"office": "Office"},
                                              "promptGuidelines": office_tool_system_prompt_contribution["guidelines"]})
                self.assertEqual(prompt.count(COMMON_CHARTER), 1)
                self.assertEqual(prompt.count(SISTER_ROLE), 1)
                for rule in office_tool_system_prompt_contribution["guidelines"]:
                    self.assertIn(rule, prompt)
                self.assertEqual("[Research card]" in prompt, research is not None)


if __name__ == "__main__":
    unittest.main()

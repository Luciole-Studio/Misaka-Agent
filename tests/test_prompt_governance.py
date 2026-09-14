"""Offline checks for role/personality separation and Research's actual prompt paths."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from misaka.config import identity, profiles
from misaka.core.research import planner, prompting, report, workflow
from misaka.core.skills import index
from misaka.core.system_prompt import build_system_prompt


class PromptGovernanceTests(unittest.TestCase):
    def test_personality_never_removes_or_duplicates_duties(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            for role, charter in identity.ROLE_CHARTER.items():
                for soul in (None, "", "A custom voice, not a replacement task contract."):
                    with self.subTest(role=role, soul=soul):
                        path = profile / "SOUL.md"
                        if soul is None:
                            path.unlink(missing_ok=True)
                        else:
                            path.write_text(soul)
                        sections = identity.prompt_sections(directory, role)
                        prompt = "\n\n".join(sections)
                        self.assertEqual(prompt.count(identity.COMMON_CHARTER), 1)
                        self.assertEqual(prompt.count(charter), 1)
                        self.assertNotIn("You are an agent of the MISAKA Network", prompt)
                        if soul:
                            self.assertEqual(sections[0], soul)
                        self.assertEqual(path.read_text() if path.exists() else None, soul)
            self.assertIn(identity.SISTER_ROLE, identity.prompt_sections(directory, "10032"))
            self.assertIn(identity.COORDINATOR_ROLE, identity.prompt_sections(directory, "last-order"))

    def test_existing_shared_personality_is_not_overwritten(self):
        from misaka.config import CFG
        with tempfile.TemporaryDirectory() as directory, patch.dict(CFG, {"roles_root": directory}):
            path = Path(directory) / "MISAKA.md"
            path.write_text("User's existing language and personality preferences.")
            before = path.read_bytes()
            self.assertEqual(profiles.shared_soul(), str(path))
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn("not into the conversation", profiles.SHARED_SOUL_TEMPLATE)

    def test_base_prompt_tool_selection_and_exact_dedup(self):
        common = {"cwd": "/fixture", "toolSnippets": {"bash": "Shell", "grep": "Search"}}
        empty = build_system_prompt({**common, "selectedTools": []})
        self.assertIn("Available tools:\n(none)", empty)
        self.assertNotIn("Use bash for file operations", empty)
        shell = build_system_prompt({**common, "selectedTools": ["bash"]})
        self.assertIn("Use bash for file operations", shell)
        dedicated = build_system_prompt({**common, "selectedTools": ["bash", "grep"],
                                         "promptGuidelines": [" same rule ", "same rule"]})
        self.assertNotIn("Prefer grep/find/ls", dedicated)
        self.assertEqual(dedicated.count("same rule"), 1)
        self.assertIn("humanities and social sciences", dedicated)
        self.assertIn("Sisters are domain specialists", dedicated)

    def test_custom_prompt_contract_is_preserved(self):
        prompt = build_system_prompt({"cwd": "/fixture", "customPrompt": "USER SYSTEM",
                                      "appendSystemPrompt": "APPEND",
                                      "contextFiles": [{"path": "/fixture/PROJECT.md", "content": "PROJECT"}],
                                      "selectedTools": []})
        self.assertTrue(prompt.startswith("USER SYSTEM\n\nAPPEND"))
        self.assertIn("PROJECT", prompt)
        self.assertNotIn("Available tools:", prompt)
        self.assertTrue(prompt.endswith("Current working directory: /fixture\n"))

    def test_research_bare_and_normal_share_duties_once(self):
        session = SimpleNamespace(getToolDefinition=lambda name: SimpleNamespace(
            promptGuidelines=["Navigate live state"] if name == "misaka_research_view" else ["Scan when useful"]))
        for initial in ("Custom bare prompt", "\n\n".join(identity.prompt_sections(None, "last_order"))):
            with self.subTest(initial=initial[:20]):
                names = ["misaka_research_view", "coverage_scan"]
                prompt = initial + "\n" + prompting.system_context(session, names, initial)
                self.assertEqual(prompt.count(identity.COMMON_CHARTER), 1)
                self.assertEqual(prompt.count(identity.COORDINATOR_ROLE), 1)
                self.assertEqual(prompt.count(identity.COORDINATOR_APPROVAL), 1)
                self.assertEqual(prompt.count(identity.COORDINATOR_RECEIPTS), 1)
                self.assertEqual(prompting.system_context(session, names, prompt), "")
        prompt = prompting.system_context(session, [], "")
        self.assertNotIn("Navigate live state", prompt)
        self.assertNotIn("Scan when useful", prompt)

    def test_approval_policy_matches_driver_for_initial_and_followup_plans(self):
        run = {"id": "run", "root_session": "/fixture/session.jsonl",
               "workspace": "/fixture", "question": "Question"}
        for parent in (None, "parent"):
            node = {"id": "node", "parent_id": parent, "depth": int(parent is not None),
                    "trigger_text": "Question", "session_file": None}
            for enabled in (True, False):
                for resident in (True, False):
                    cfg = {"research_plan_approval": enabled}
                    worker = SimpleNamespace(session=object() if resident else None)
                    expected = planner.PLAN_WAITS if enabled and resident else planner.PLAN_AUTOMATIC
                    with self.subTest(parent=parent, enabled=enabled, resident=resident), \
                            patch.object(planner, "_roster", return_value=[]), \
                            patch.object(planner, "_lo_session", return_value="/fixture"), \
                            patch.object(planner.runs, "limits", return_value={"max_depth": 3}), \
                            patch.object(planner, "_command", return_value=(
                                {"payload": {}, "session_file": "/fixture/session.jsonl"}, "raw")) as command:
                        self.assertEqual(planner.plan_approval_prompt(cfg, worker), expected)
                        planner.plan(run, cfg, worker, node, con=object())
                        self.assertIn(expected, command.call_args.args[5])
                    with patch.object(planner, "_lo_session", return_value="/fixture"), \
                            patch.object(planner, "task_sources", return_value={}), \
                            patch.object(planner, "find_most_recent_session", return_value=None), \
                            patch.object(planner.runs, "plan_round", return_value=1), \
                            patch.object(planner, "_call", return_value=(None, "Conclusion", None)) as call:
                        planner.synthesize(object(), run, cfg, worker, node, [], followup=object(), left=1)
                        self.assertIn(expected, call.call_args.args[2])
                        self.assertIn("only if you submit a follow-up plan", call.call_args.args[2])

    def test_research_phase_boundaries_and_saving_contract(self):
        self.assertIn("only deliverable is a research design", planner.ROOT_CONTRACT)
        self.assertNotIn("perform the `coverage_scan` check", planner.ROOT_CONTRACT)
        self.assertIn("not automatically a defect", planner.RED_TEAM_CONTRACT)
        self.assertIn("not a fixed number", planner.ROOT_CONTRACT)
        self.assertNotIn("Every plan --", workflow.RESEARCH_DISCIPLINE)
        self.assertNotIn("lifts only at final", workflow.RESEARCH_DISCIPLINE)
        self.assertIn("Node synthesis produces a working conclusion", workflow.RESEARCH_DISCIPLINE)
        for text in (planner.SYNTHESIS_CONTRACT, report.DRAFT_CONTRACT, report.FINAL_CONTRACT):
            self.assertIn(planner.MARKDOWN_OUTPUT, text)
        self.assertIn("without replanning the whole project", __import__("inspect").getsource(planner.plan))
        self.assertNotIn("one line naming", planner.task_body({"instructions": "Task"}))

    def test_skill_policy_has_one_owner_and_respects_read_only_sessions(self):
        entries = [{"name": "test", "description": "x" * 200, "category": "general", "layer": "role"}]
        for manage in (False, True):
            prompt = index.render_prompt(entries, can_manage=manage, available_tools={"skill_view"})
            self.assertIn("work you are actually performing", prompt)
            self.assertIn("x" * 200, prompt)
            self.assertNotIn("Only proceed without", prompt)
            self.assertNotIn("always better to have context", prompt)
            self.assertEqual("skill_manage" in prompt, manage)
            self.assertEqual("read-only in this session" in prompt, not manage)
            self.assertNotIn("terminal", prompt)
        self.assertEqual(index.render_prompt([]), "")
        self.assertEqual(index.SKILL_PROMPT_DESC_LIMIT, 200)

    def test_vendor_provenance_records_native_policy_change(self):
        vendor = Path(index.__file__).parent / "vendor"
        entries = json.loads((vendor / "PROVENANCE.json").read_text())["files"]
        entry = next(e for e in entries if e["file"] == "visibility.py")
        self.assertEqual(entry["sha256"], hashlib.sha256((vendor / "visibility.py").read_bytes()).hexdigest())
        self.assertIn("can_manage", entry["patch"])


if __name__ == "__main__":
    unittest.main()

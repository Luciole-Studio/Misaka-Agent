"""Offline checks for role/personality separation and Research's actual prompt paths."""
import hashlib
import json
import os
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
    def test_shared_research_guidance_stays_concise_and_conditional(self):
        heading = "## Research and reasoning"
        self.assertEqual(identity.COMMON_CHARTER.count(heading), 1)
        guidance = identity.COMMON_CHARTER.split(heading, 1)[1]
        self.assertTrue(guidance.isascii())
        self.assertLessEqual(len(guidance.split()), 250)
        self.assertIn("For substantive research and analysis", guidance)
        self.assertIn("not as a checklist", guidance)
        for scope in ("Before relying on them", "in this task", "when useful"):
            with self.subTest(scope=scope):
                self.assertIn(scope, guidance)

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
        from misaka.config import home
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {home.ENV_HOME: directory}):
            path = home.path("shared_soul")
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

    def test_research_delta_does_not_reconstruct_or_duplicate_the_role_base(self):
        session = SimpleNamespace(getToolDefinition=lambda name: SimpleNamespace(
            promptGuidelines=["Navigate live state"] if name == "misaka_research_view" else ["Scan when useful"]))
        for initial in ("Custom bare prompt", "\n\n".join(identity.prompt_sections(None, "last_order"))):
            with self.subTest(initial=initial[:20]):
                names = ["misaka_research_view", "coverage_scan"]
                prompt = initial + "\n" + prompting.system_context(session, names, initial)
                self.assertEqual(prompt.count(identity.COMMON_CHARTER), initial.count(identity.COMMON_CHARTER))
                self.assertEqual(prompt.count(identity.COORDINATOR_ROLE), initial.count(identity.COORDINATOR_ROLE))
                self.assertEqual(prompt.count(identity.COORDINATOR_APPROVAL), 1)
                self.assertEqual(prompt.count(identity.COORDINATOR_RECEIPTS), 1)
                self.assertEqual(prompting.system_context(session, names, prompt), "")
        prompt = prompting.system_context(session, [], "")
        self.assertNotIn("Navigate live state", prompt)
        self.assertNotIn("Scan when useful", prompt)

    def test_approval_policy_matches_driver_for_initial_and_followup_plans(self):
        run = {"id": "run", "root_session": "/fixture/session.jsonl",
               "workspace": "/fixture", "question": "Question", "limits_json": "{}"}
        for parent in (None, "parent"):
            node = {"id": "node", "parent_id": parent, "depth": int(parent is not None),
                    "trigger_text": "Question", "session_file": None}
            for enabled in (True, False):
                for resident in (True, False):
                    cfg = {"research_plan_approval": enabled}
                    worker = SimpleNamespace(session=object() if resident else None)
                    expected = planner.PLAN_WAITS if enabled else planner.PLAN_AUTOMATIC
                    with self.subTest(parent=parent, enabled=enabled, resident=resident), \
                            patch.object(planner, "_roster", return_value=[]), \
                            patch.object(planner, "_lo_session", return_value="/fixture"), \
                            patch.object(planner.runs, "limits", return_value={"max_depth": 3}), \
                            patch.object(planner, "_command", return_value=(
                                {"payload": {}, "session_file": "/fixture/session.jsonl"}, "raw")) as command:
                        self.assertEqual(planner.plan_approval_prompt(cfg), expected)
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

    def test_method_guidance_uses_existing_skills_without_repeating_common_charter(self):
        for tools in (planner.RESEARCH_TOOLS, report.SURVEY_TOOLS, report.FINAL_TOOLS):
            self.assertIn("skills_list", tools)
            self.assertIn("skill_view", tools)
        for contract in (planner.ROOT_CONTRACT, planner.RESEARCH_SISTER_DISCIPLINE,
                         planner.SYNTHESIS_CONTRACT, planner.RED_TEAM_CONTRACT,
                         report.DRAFT_CONTRACT, report.SURVEY_CONTRACT, report.FINAL_CONTRACT):
            with self.subTest(phase=contract.splitlines()[0]):
                self.assertNotIn("## Research and reasoning", contract)
                self.assertNotRegex(contract, r"[\u3400-\u9fff]")
        for field in ("method", "source_strategy", "falsifiers"):
            self.assertIn(f"`{field}`", planner.ROOT_CONTRACT)

    def test_synthesis_followup_keeps_the_existing_assignment_window(self):
        run = {"id": "run", "root_session": "/fixture/session.jsonl", "workspace": "/fixture", "limits_json": "{}"}
        node = {"id": "node", "parent_id": None, "trigger_text": "Question"}
        followup = object()
        for tool, round_number, left in ((followup, 1, 1), (None, 2, 0), (None, 1, 0)):
            with self.subTest(round=round_number, left=left), \
                    patch.object(planner, "_lo_session", return_value="/fixture"), \
                    patch.object(planner, "task_sources", return_value={}), \
                    patch.object(planner, "find_most_recent_session", return_value=None), \
                    patch.object(planner.runs, "plan_round", return_value=round_number), \
                    patch.object(planner, "_call", return_value=(None, "Conclusion", None)) as call:
                result = planner.synthesize(object(), run, {"research_plan_approval": False},
                                            SimpleNamespace(session=None), node, [],
                                            followup=tool, round=round_number, left=left)
                prompt = call.call_args.args[2]
                self.assertEqual(result, "Conclusion\n")
                self.assertEqual(call.call_args.kwargs["extra_tools"], (tool,) if tool is not None else ())
                self.assertIn(planner.SOURCES_FOOTER, prompt)
                self.assertIn(planner.MARKDOWN_OUTPUT, prompt)
                if tool is not None:
                    self.assertIn("same question", prompt)
                    self.assertIn("conceptual distinctions or reasoning", prompt)
                    self.assertIn("do not use follow-up rounds to review yourself", prompt)
                    self.assertIn("1 more round(s)", prompt)
                    self.assertIn("only if you submit a follow-up plan", prompt)
                elif round_number > 1:
                    self.assertIn("No further cards can be assigned", prompt)
                    self.assertNotIn("request follow-up research", prompt)
                else:
                    self.assertNotIn("# Round", prompt)

    def test_review_prompts_preserve_independence_and_terminal_boundaries(self):
        node = {"trigger_text": "Question"}
        node_review = planner.red_team_body(node, synthesis_path="node/conclusion.md", plan_path="node/plan.md")
        self.assertIn("do not extend the report", node_review)
        self.assertIn("does not start investigations on your behalf", node_review)
        self.assertIn("Only material=true issues require investigation", node_review)
        draft = {"path": "draft.md", "id": "draft-id", "sha256": "draft-hash"}
        with patch.object(report, "materials", return_value="Full material map"):
            final_review = report.review_body(object(), {"question": "Question"}, draft)
        self.assertIn("artifact `draft-id`, sha256 `draft-hash`", final_review)
        self.assertIn("node critiques and full sources", final_review)
        self.assertIn("Do not edit the draft", final_review)
        self.assertIn("no new research branches\nare created", final_review)
        self.assertTrue(final_review.endswith("Full material map"))
        for review in (node_review, final_review):
            self.assertIn("critique.md", review)
            self.assertIn("Use issues=[] explicitly", review)
        self.assertIn("not another research-tree expansion", report.FINAL_CONTRACT)
        self.assertIn("Do not vote, rank nodes or adjudicate", report.SURVEY_CONTRACT)

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

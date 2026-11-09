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
from misaka.core.system_prompt import buildSystemPrompt


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
                            path.write_text(soul, encoding="utf-8")
                        sections = identity.prompt_sections(directory, role)
                        prompt = "\n\n".join(sections)
                        self.assertEqual(prompt.count(identity.COMMON_CHARTER), 1)
                        self.assertEqual(prompt.count(charter), 1)
                        self.assertNotIn("You are an agent of the MISAKA Network", prompt)
                        if soul:
                            self.assertEqual(sections[0], soul)
                        self.assertEqual(path.read_text(encoding="utf-8") if path.exists() else None, soul)
            self.assertIn(identity.SISTER_ROLE, identity.prompt_sections(directory, "10032"))
            self.assertIn(identity.COORDINATOR_ROLE, identity.prompt_sections(directory, "last-order"))

    def test_existing_shared_personality_is_not_overwritten(self):
        from misaka.config import home
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {home.ENV_HOME: directory}):
            path = home.path("shared_soul")
            path.write_text("User's existing language and personality preferences.", encoding="utf-8")
            before = path.read_bytes()
            self.assertEqual(profiles.shared_soul(), str(path))
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn("not into the conversation", profiles.SHARED_SOUL_TEMPLATE)

    def test_base_prompt_tool_selection_and_exact_dedup(self):
        common = {"cwd": "/fixture", "toolSnippets": {"bash": "Shell", "grep": "Search"}}
        empty = buildSystemPrompt({**common, "selectedTools": []})
        # pi 0.87: the prompt is built from tagged, independently replaceable sections.
        self.assertIn("<tools>\n(none)", empty)
        self.assertNotIn("Use bash for file operations", empty)
        shell = buildSystemPrompt({**common, "selectedTools": ["bash"]})
        self.assertIn("Use bash for file operations", shell)
        dedicated = buildSystemPrompt({**common, "selectedTools": ["bash", "grep"],
                                         "promptGuidelines": [" same rule ", "same rule"]})
        self.assertNotIn("Prefer grep/find/ls", dedicated)
        self.assertEqual(dedicated.count("same rule"), 1)
        self.assertIn("humanities and social sciences", dedicated)
        self.assertIn("Sisters are domain specialists", dedicated)

    def test_custom_prompt_contract_is_preserved(self):
        prompt = buildSystemPrompt({"cwd": "/fixture", "customPrompt": "USER SYSTEM",
                                      "appendSystemPrompt": "APPEND",
                                      "contextFiles": [{"path": "/fixture/PROJECT.md", "content": "PROJECT"}],
                                      "selectedTools": []})
        self.assertTrue(prompt.startswith("USER SYSTEM\n\n<addendum>\nAPPEND\n</addendum>"))
        self.assertIn("PROJECT", prompt)
        self.assertNotIn("<tools>", prompt)
        self.assertTrue(prompt.endswith("<cwd>\n/fixture\n</cwd>"))

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
            node = {"id": "node", "depth": int(parent is not None), "question": "Question", "session_file": None}
            for enabled in (True, False):
                for resident in (True, False):
                    cfg = {"research_plan_approval": enabled}
                    worker = SimpleNamespace(session=object() if resident else None)
                    expected = planner.PLAN_WAITS if enabled else planner.PLAN_AUTOMATIC
                    with self.subTest(parent=parent, enabled=enabled, resident=resident), \
                            patch.object(planner, "_roster", return_value=[]), \
                            patch.object(planner, "_lo_session", return_value="/fixture"), \
                            patch.object(planner, "position", return_value="- fixture option"), \
                            patch.object(planner.runs, "current_limits", return_value={"max_depth": 3}), \
                            patch.object(planner, "_command", return_value=(
                                {"payload": {}, "session_file": "/fixture/session.jsonl"}, "raw")) as command:
                        self.assertEqual(planner.plan_approval_prompt(cfg), expected)
                        planner.plan(run, cfg, worker, node, con=object())
                        self.assertIn(expected, command.call_args.args[5])
                    with patch.object(planner, "_lo_session", return_value="/fixture"), \
                            patch.object(planner, "position", return_value="- fixture option"), \
                            patch.object(planner, "errata_above", return_value=""), \
                            patch.object(planner, "task_sources", return_value={}), \
                            patch.object(planner, "findMostRecentSession", return_value=None), \
                            patch.object(planner.runs, "plan_round", return_value=1), \
                            patch.object(planner, "_call", return_value=(None, "Conclusion", None)) as call:
                        planner.synthesize(object(), run, cfg, worker, node, [], followup=object(), left=1)
                        self.assertIn(expected, call.call_args.args[2])
                        self.assertIn("only if you submit a follow-up plan", call.call_args.args[2])

    def test_the_user_s_instructions_reach_every_node_s_plan_and_the_reconciliation(self):
        """B122 (2026-10-01): the user allowed two Sisters and two forks; a depth-1 plan took a third
        Sister by fit, and the reconciliation opened six forks -- its contract admitted no limit of the
        user's among the reasons to leave an option unopened."""
        words = "只派俩sis，分歧点如果很多那就先只派出两个"
        run = {"id": "run", "root_session": "/fixture/session.jsonl", "workspace": "/fixture",
               "question": words, "limits_json": "{}"}
        node = {"id": "node", "depth": 1, "question": "a branch", "session_file": None}
        with patch.object(planner, "_roster", return_value=[]), \
                patch.object(planner, "_lo_session", return_value="/fixture"), \
                patch.object(planner, "position", return_value="- fixture option"), \
                patch.object(planner.runs, "current_limits", return_value={"max_depth": 3}), \
                patch.object(planner, "_command", return_value=(
                    {"payload": {}, "session_file": "/fixture/session.jsonl"}, "raw")) as command:
            planner.plan(run, {}, SimpleNamespace(session=None), node, con=object())
        self.assertIn(planner.user_instructions(run), command.call_args.args[5])
        self.assertIn(words, planner.user_instructions(run))
        self.assertIn("a limit\n  the user set for this run", planner.RECONCILE_CONTRACT)

    def test_research_phase_boundaries_and_saving_contract(self):
        self.assertIn("only deliverable is a research design", planner.ROOT_CONTRACT)
        self.assertNotIn("perform the `coverage_scan` check", planner.ROOT_CONTRACT)
        self.assertIn("not automatically a defect", planner.RED_TEAM_CONTRACT)
        self.assertIn("not a fixed number", planner.ROOT_CONTRACT)
        self.assertNotIn("Every plan --", workflow.RESEARCH_DISCIPLINE)
        self.assertNotIn("lifts only at final", workflow.RESEARCH_DISCIPLINE)
        self.assertIn("Node synthesis produces a working conclusion", workflow.RESEARCH_DISCIPLINE)
        self.assertIn(planner.MARKDOWN_OUTPUT, planner.SYNTHESIS_CONTRACT)
        for text in (report.DRAFT_CONTRACT, report.FINAL_CONTRACT):   # the report tool delivers these
            self.assertNotIn(planner.MARKDOWN_OUTPUT, text)
            self.assertIn(planner.SOURCES_FOOTER, text)
        self.assertIn("doc_read", report.STAGE_OUTPUT)
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
        node = {"id": "node", "depth": 0, "question": "Question"}
        followup = object()
        for tool, round_number, left in ((followup, 1, 1), (None, 2, 0), (None, 1, 0)):
            with self.subTest(round=round_number, left=left), \
                    patch.object(planner, "_lo_session", return_value="/fixture"), \
                    patch.object(planner, "task_sources", return_value={}), \
                    patch.object(planner, "findMostRecentSession", return_value=None), \
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
        node = {"question": "Question"}
        node_review = planner.red_team_body(node, synthesis_path="node/conclusion.md", plan_path="node/plan.md",
                                            graph_path="final/run-graph.md")
        self.assertIn("do not extend the report", node_review)
        self.assertIn("Your review opens no node by itself", node_review)
        self.assertIn("Only material=true issues require an answer from Last Order", node_review)
        self.assertIn("final/run-graph.md", node_review)
        self.assertIn("name that node's id", node_review)
        draft = {"path": "draft.md", "id": "draft-id", "sha256": "draft-hash"}
        with patch.object(report, "_checkpoint", return_value={"path": "survey.md"}), \
                patch.object(report, "index_run", return_value={}), \
                patch.object(report, "_catalog", return_value=["catalog entry"]):
            final_review = report.review_body(object(), {"question": "Question"}, draft)
        self.assertIn("artifact `draft-id`, sha256 `draft-hash`", final_review)
        self.assertIn("node critiques and full sources", final_review)
        self.assertIn("Do not edit the draft", final_review)
        self.assertIn("no new research branches\nare created", final_review)
        self.assertIn("## catalog", final_review)
        self.assertIn("catalog entry", final_review)
        for review in (node_review, final_review):
            self.assertIn("critique.md", review)
            self.assertIn("Use issues=[] explicitly", review)
        self.assertIn("it opens no new node of the graph", " ".join(report.FINAL_CONTRACT.split()))
        # 2026-09-29 (user): the report is a humanities research article, complete and traceable.
        draft, final = (" ".join(text.split()) for text in (report.DRAFT_CONTRACT, report.FINAL_CONTRACT))
        self.assertIn("Write for a reader of the humanities and social sciences", draft)
        self.assertIn("The body carries no run vocabulary or identifiers", draft)
        for appendix in ("Appendix I, the lines of inquiry: every node of the catalog",
                         "Appendix II, the record", "Appendix III, the materials"):
            self.assertIn(appendix, draft)
        self.assertIn("Every line's final conclusion has its place in the argument", draft)
        self.assertIn("Appendix IV", final)
        self.assertIn("in the draft's form", final)
        self.assertIn("Appendix I lists every node", " ".join(final_review.split()))
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
        entries = json.loads((vendor / "PROVENANCE.json").read_text(encoding="utf-8"))["files"]
        entry = next(e for e in entries if e["file"] == "visibility.py")
        self.assertEqual(entry["sha256"], hashlib.sha256((vendor / "visibility.py").read_bytes()).hexdigest())
        self.assertIn("can_manage", entry["patch"])


if __name__ == "__main__":
    unittest.main()

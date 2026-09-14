"""Tool-owned prompt rules: standalone activation, deduplication, and real file behavior."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from misaka.core.agent_session import AgentSession
from misaka.core.documents.prompt import (
    MATERIAL_REUSE_GUIDELINE,
    QUOTATION_LOCATOR_GUIDELINE,
    WEB_EVIDENCE_GUIDELINE,
)
from misaka.core.documents.wiring import documents
from misaka.core.tools import _office
from misaka.core.tools.download_file import create_download_file_tool_definition
from misaka.core.tools.office import create_office_tool_definition
from misaka.core.tools.web_fetch import create_web_fetch_tool_definition
from misaka.core.web import extract
from misaka.core.web import tool as web_search
from misaka.core.wiring import ToolCollector
from misaka.extensions import coverage


class ToolPromptGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = self.directory.name
        collector = ToolCollector()
        for register in (documents.register, extract.register, web_search.register, coverage.register):
            register(collector)
        for factory in (create_office_tool_definition, create_download_file_tool_definition,
                        create_web_fetch_tool_definition):
            collector.registerTool(factory(self.workspace))
        self.tools = {definition.name: definition for definition in collector.tools}

    def prompt(self, names):
        loader = SimpleNamespace(
            getSystemPrompt=lambda: None,
            getAppendSystemPrompt=list,
            getAgentsFiles=lambda: {"agentsFiles": []},
        )
        session = SimpleNamespace(
            _cwd=self.workspace, _resourceLoader=loader,
            _toolRegistry=self.tools, _toolDefinitions=self.tools,
        )
        return AgentSession._rebuild_system_prompt(session, names)

    def test_shared_rules_survive_standalone_tools_and_deduplicate(self):
        cases = (
            (MATERIAL_REUSE_GUIDELINE, ("doc_list", "download_file")),
            (QUOTATION_LOCATOR_GUIDELINE, ("doc_verify", "download_file")),
            (WEB_EVIDENCE_GUIDELINE, ("web_fetch", "web_extract")),
        )
        for guideline, owners in cases:
            for names in ([owner] for owner in owners):
                with self.subTest(names=names):
                    self.assertEqual(self.prompt(names).count(guideline), 1)
            self.assertEqual(self.prompt(list(owners)).count(guideline), 1)
            self.assertNotIn(guideline, self.prompt([]))
        combined = self.prompt(list(self.tools))
        for guideline, _ in cases:
            self.assertEqual(combined.count(guideline), 1)
        self.assertNotIn(WEB_EVIDENCE_GUIDELINE, self.prompt(["not_registered"]))

    def test_office_rules_match_available_capabilities(self):
        definition = self.tools["office"]
        guidelines = "\n".join(definition.promptGuidelines)
        for required in ("not a prerequisite", "overwrite=true", "externally computed",
                         "uncached", ".md and .html", "CJK", "does not verify layout"):
            self.assertIn(required, guidelines)
        for removed in ("for every deliverable", "ONLY after", "never a number",
                        "name only Arial", "One family per document", "Pass ops as"):
            self.assertNotIn(removed, guidelines)
        self.assertIn("single-key objects", definition.description)
        self.assertIn("content (or rows/data)", definition.description)
        self.assertIn("rejects an existing path", definition.description)

    def test_coverage_and_web_avoid_unconditional_work_or_evidence_promises(self):
        scan = " ".join(self.tools["coverage_scan"].promptGuidelines)
        self.assertNotIn("two or three", scan)
        self.assertIn("not measures of relevance, quality, or completeness", scan)
        self.assertIn("failed scan does not establish a research gap", scan)
        search = " ".join(self.tools["web_search"].promptGuidelines)
        self.assertNotIn("costs nothing", search)
        self.assertIn("not guaranteed to be free or fresh", search)
        for name in ("web_fetch", "web_extract"):
            prompt = self.prompt([name])
            for required in ("captured material", "final_url", "truncation", "untrusted"):
                self.assertIn(required, prompt)
            self.assertNotIn("Never curl", prompt)
        self.assertIn("size-capped", self.tools["web_extract"].description)
        self.assertIn("indexing failure", self.tools["download_file"].description)

    def test_literal_markup_overwrite_protection_and_atomic_batch_remain(self):
        path = Path(self.workspace) / "report.md"
        content = "# 研究\n\n**Evidence** and <em>markup</em>\n"
        _office.run_ops(path, [{"create": {"content": content}}])
        self.assertEqual(path.read_text(), content)
        _office.run_ops(path, [{"create": {"content": "replacement"}}])
        self.assertEqual(path.read_text(), content)
        _office.run_ops(path, [{"append": {"content": "changed"}}, {"unknown_op": {}}])
        self.assertEqual(path.read_text(), content)
        _office.run_ops(path, [{"create": {"content": "intentional rebuild"}}], overwrite=True)
        self.assertEqual(path.read_text(), "intentional rebuild")

    def test_web_results_keep_untrusted_fences_and_saved_paths(self):
        saved = "downloads/pages/source.md"
        envelope = {"success": True, "results": [{
            "content": "SOURCE_INSTRUCTION_SENTINEL", "saved_path": saved,
            "final_url": "https://example.invalid/source", "text_sha256": "SOURCE_HASH",
        }]}
        for name, module, function in (("web_extract", extract, "web_extract_tool"),
                                       ("web_search", web_search, "web_search_tool")):
            with self.subTest(name=name), patch.object(
                module, function, AsyncMock(return_value=json.dumps(envelope)),
            ), patch.object(module.debug, "result_json"):
                result = asyncio.run(self.tools[name].execute("call", {}, None, None, None))
            text = result["content"][0]["text"]
            self.assertIn("UNTRUSTED-DATA", text)
            self.assertIn("SOURCE_INSTRUCTION_SENTINEL", text)
            self.assertIn("SOURCE_HASH", text)
            self.assertFalse(result["isError"])
            if name == "web_extract":
                self.assertEqual(result["details"]["saved_paths"], [saved])

    def test_numeric_values_and_uncached_formula_receipt_remain(self):
        import openpyxl

        for value, expected in ((12, 12), (12.5, 12.5), (-12.5, -12.5),
                                ("12", 12), ("12.5", 12.5), (True, 1), (False, 0),
                                ("not a number", "not a number")):
            with self.subTest(value=value):
                self.assertEqual(_office._xlsx._typed(value, "number"), expected)
        path = Path(self.workspace) / "data.xlsx"
        with patch.object(_office, "_recalculate", return_value=False):
            receipt = _office.run_ops(path, [
                {"create": {}},
                {"set_cell": {"cell": "A1", "value": 12.5, "type": "number"}},
                {"set_cell": {"cell": "B1", "value": "=A1*2", "type": "formula"}},
            ])
        self.assertIn("uncached", receipt)
        workbook = openpyxl.load_workbook(path, data_only=False)
        try:
            self.assertEqual(workbook.active["A1"].value, 12.5)
            self.assertEqual(workbook.active["B1"].value, "=A1*2")
        finally:
            workbook.close()
        values = openpyxl.load_workbook(path, data_only=True)
        try:
            self.assertIsNone(values.active["B1"].value)
        finally:
            values.close()


if __name__ == "__main__":
    unittest.main()

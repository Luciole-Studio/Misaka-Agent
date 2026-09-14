"""Capability-driven collaboration prose: no sessions, processes, or model calls."""
import unittest
from types import SimpleNamespace

from misaka.core import wiring
from misaka.core.network.ally import extension as allies
from misaka.core.network.wiring import collaboration


class CollaborationPromptTests(unittest.IsolatedAsyncioTestCase):
    def make_part(self, names):
        part = collaboration.CollaborationPart()
        part.attach(SimpleNamespace(getActiveToolNames=lambda: names))
        self.assertEqual(part.tools, [])
        return part

    async def test_no_capabilities_does_not_claim_collaborators(self):
        for names in ([], ["read"], ["unrelated_extension_tool"]):
            with self.subTest(names=names):
                part = self.make_part(names)
                self.assertIsNone(await part.before_agent_start({"systemPrompt": "CUSTOM\n"}, None))

    async def test_sister_tools_have_own_section_without_ally_capabilities(self):
        names = ["Agent", "TaskOutput", "TaskStop", "SendMessage"]
        part = self.make_part(names)
        text = (await part.before_agent_start({"systemPrompt": "CUSTOM"}, None))["systemPrompt"]
        self.assertTrue(text.startswith("CUSTOM\n\n## Last Order / Sister coordination\n"))
        self.assertIn("\n\n## Sub-agents\n", text)
        for name in names:
            self.assertIn(f"`{name}`", text)
        self.assertIn("Sister number as subagent_type", text)
        self.assertIn("current description", text)
        self.assertIn("Verify delegated evidence", text)
        self.assertNotIn("## Allies", text)
        self.assertNotIn("misaka_ally_", text)
        self.assertNotIn("Explore:", text)  # the dynamic agent catalog has one owner

    async def test_role_messages_do_not_grant_card_management(self):
        part = self.make_part(["SendMessage"])
        text = (await part.before_agent_start({"systemPrompt": "BASE"}, None))["systemPrompt"]
        self.assertEqual(text.count("## Last Order / Sister coordination"), 1)
        self.assertIn("`last-order` or a registered Sister ID", text)
        self.assertIn("may wake a contact session", text)
        self.assertIn("do not create, start, complete or park a card", text)
        self.assertNotIn("misaka_card", text)
        self.assertNotIn("misaka_sister_message", text)
        self.assertNotIn("request_input=true", text)  # card-only protocol remains tool-owned
        self.assertNotIn("## Sub-agents", text)

    async def test_card_routes_distinguish_creation_start_reply_and_review(self):
        names = [name for name in collaboration.SISTER_TOOLS if not name.startswith("misaka_research_")]
        part = self.make_part(names)
        text = (await part.before_agent_start({"systemPrompt": "BASE"}, None))["systemPrompt"]
        for phrase in ("creation does not start work", "task ID and current generation", "parked card's help request",
                       "not through a role-wide message", "independent reviewer", "real dependency"):
            self.assertIn(phrase, text)
        self.assertNotIn("misaka_research_assign", text)
        for name in names:
            self.assertEqual(text.count(f"- `{name}`:"), 1)

    async def test_research_routes_do_not_expose_ordinary_board_or_message_tools(self):
        names = [name for name in collaboration.SISTER_TOOLS if name.startswith("misaka_research_")]
        part = self.make_part(names)
        text = (await part.before_agent_start({"systemPrompt": "BASE"}, None))["systemPrompt"]
        self.assertIn("driver creates and launches", text)
        self.assertIn("Do not duplicate those assignments", text)
        for name in ("SendMessage", "misaka_card", "misaka_dispatch", "misaka_sister", "Agent"):
            self.assertNotIn(f"`{name}`", text)
        for name in names:
            self.assertIn(f"`{name}`", text)
        self.assertNotIn("## Sub-agents", text)

    def test_sister_coordination_lists_only_the_current_route(self):
        for name in collaboration.SISTER_TOOLS:
            with self.subTest(name=name):
                text = "\n\n".join(collaboration.collaboration_sections([name]))
                self.assertIn(f"- `{name}`:", text)
                for hidden in collaboration.SISTER_TOOLS.keys() - {name}:
                    self.assertNotIn(f"`{hidden}`", text)

    async def test_management_only_and_hidden_message_tool(self):
        part = self.make_part(["TaskOutput", "TaskStop"])
        text = (await part.before_agent_start({"systemPrompt": "BASE"}, None))["systemPrompt"]
        self.assertIn("Only existing-task management", text)
        self.assertNotIn("`Agent`", text)
        self.assertNotIn("subagent_type", text)
        self.assertNotIn("SendMessage", text)

    async def test_lo_ally_tools_do_not_advertise_agent_launch(self):
        part = self.make_part(["misaka_ally_list", "misaka_ally_start", "misaka_ally_stop"])
        text = (await part.before_agent_start({"systemPrompt": "BASE"}, None))["systemPrompt"]
        self.assertIn("## Allies", text)
        self.assertIn("core bridge", text)
        self.assertIn("outside MISAKA's accounting", text)
        self.assertNotIn("## Sub-agents", text)
        self.assertNotIn("`Agent`", text)
        self.assertNotIn("misaka_ally_dispatch", text)
        self.assertNotIn("misaka_ally_close", text)
        for name in ("misaka_ally_start", "misaka_ally_stop"):
            line = next(line for line in text.splitlines() if f"`{name}`" in line)
            self.assertIn("user explicitly requests", line)

    async def test_idempotent_and_scope_changes_preserve_other_prompt_text(self):
        names = ["Agent", "SendMessage", *collaboration.ALLY_TOOLS]
        part = self.make_part(names)
        original = "CUSTOM\n## Sub-agents\nUser-authored section, not ours.\n"
        first = (await part.before_agent_start({"systemPrompt": original}, None))["systemPrompt"]
        self.assertIsNone(await part.before_agent_start({"systemPrompt": first}, None))
        self.assertEqual((await part.before_agent_start({"systemPrompt": original}, None))["systemPrompt"], first)
        tail = "\n\nAnother part's contribution."
        names[:] = ["TaskOutput"]
        changed = (await part.before_agent_start({"systemPrompt": first + tail}, None))["systemPrompt"]
        self.assertTrue(changed.startswith(original + tail))
        self.assertNotIn("## Allies", changed)
        self.assertNotIn("`Agent`", changed)
        self.assertNotIn("SendMessage", changed)
        self.assertNotIn("## Last Order / Sister coordination", changed)
        self.assertIn("User-authored section, not ours.", changed)
        names.clear()
        self.assertEqual((await part.before_agent_start({"systemPrompt": changed}, None))["systemPrompt"], original + tail)

    async def test_fresh_custom_text_matching_entire_publication_is_preserved(self):
        names = ["Agent", "SendMessage", *collaboration.ALLY_TOOLS]
        publication = "\n\n" + "\n\n".join(collaboration.collaboration_sections(names))
        original = "User-authored notes" + publication + "\nUSER END"
        part = self.make_part(names)
        first = (await part.before_agent_start({"systemPrompt": original}, None))["systemPrompt"]
        self.assertEqual(first, original + publication)
        # A fresh base contains matching user text but not our appended copy.
        names.clear()
        self.assertIsNone(await part.before_agent_start({"systemPrompt": original}, None))

    async def test_folded_duplicate_removes_only_the_owned_copy(self):
        names = ["Agent"]
        publication = "\n\n" + "\n\n".join(collaboration.collaboration_sections(names))
        original = "User-authored notes" + publication + "\nUSER END"
        part = self.make_part(names)
        first = (await part.before_agent_start({"systemPrompt": original}, None))["systemPrompt"]
        tail = "\n\nDownstream contribution"
        names.clear()
        result = await part.before_agent_start({"systemPrompt": first + tail}, None)
        self.assertEqual(result["systemPrompt"], original + tail)

    def test_all_roles_and_kinds_can_publish_but_never_register_tools(self):
        self.assertEqual(wiring.PART_MODULES.count("misaka.core.network.wiring.collaboration"), 1)
        for role in wiring.ROLE_KEYS:
            for kind in wiring.KINDS:
                with self.subTest(role=role, kind=kind):
                    self.assertTrue(wiring._qualifies(collaboration, role, kind))
                    self.assertEqual(collaboration.part(None).tools, [])

    async def test_ally_confirmation_checks_and_parameter_contracts_remain(self):
        collector = wiring.ToolCollector()
        allies.register(collector)
        tools = {tool.name: tool for tool in collector.tools}
        self.assertEqual(set(tools), set(collaboration.ALLY_TOOLS))
        cases = {
            "misaka_ally_start": {"argv": ["fixture-cli"], "confirmed": False},
            "misaka_ally_dispatch": {"task_id": "fixture-task", "confirmed": False},
            "misaka_ally_stop": {"task_id": "fixture-task", "confirmed": False},
            "misaka_ally_close": {"pane_id": "fixture-pane", "confirmed": False},
        }
        for name, args in cases.items():
            with self.subTest(name=name):
                tool = tools[name]
                self.assertIn("confirmed", tool.parameters["required"])
                self.assertEqual(tool.promptGuidelines, [])
                with self.assertRaises(ValueError):
                    await tool.execute("fixture", args, None, None, SimpleNamespace())


if __name__ == "__main__":
    unittest.main()

"""Capability-driven collaboration prose: no sessions, processes, or model calls."""
import unittest
from types import SimpleNamespace

from misaka.core import wiring
from misaka.core.network.wiring import collaboration


class CollaborationPromptTests(unittest.IsolatedAsyncioTestCase):
    def make_part(self, names):
        part = collaboration.CollaborationPart()
        part.attach(SimpleNamespace(getActiveToolNames=lambda: names))
        self.assertEqual(part.tools, [])
        return part

    async def publish(self, part, base="BASE", sections=None):
        """The run's prompt sections after the part: it adds its `collaboration` section as a pi
        extension adds one, and returns no prompt of its own."""
        options = {"sections": dict(sections or {})}
        self.assertIsNone(await part.before_agent_start({"systemPrompt": base, "systemPromptOptions": options}, None))
        return options["sections"]

    async def test_no_capabilities_does_not_claim_collaborators(self):
        for names in ([], ["read"], ["unrelated_extension_tool"]):
            with self.subTest(names=names):
                part = self.make_part(names)
                self.assertEqual(await self.publish(part, "CUSTOM\n"), {})

    async def test_sister_tools_have_own_section_without_ally_capabilities(self):
        names = ["Agent", "TaskOutput", "TaskStop", "SendMessage"]
        part = self.make_part(names)
        text = (await self.publish(part, "CUSTOM"))["collaboration"]
        self.assertTrue(text.startswith("## Last Order / Sister coordination\n"))
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
        text = (await self.publish(part))["collaboration"]
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
        text = (await self.publish(part))["collaboration"]
        for phrase in ("creation does not start work", "task ID and current generation", "parked card's help request",
                       "not through SendMessage", "independent reviewer", "real dependency"):
            self.assertIn(phrase, text)
        self.assertNotIn("misaka_research_assign", text)
        for name in names:
            self.assertEqual(text.count(f"- `{name}`:"), 1)

    async def test_research_routes_do_not_expose_ordinary_board_or_message_tools(self):
        from misaka.core.research.owner import TOOL_DESCRIPTIONS

        names = list(TOOL_DESCRIPTIONS)
        part = self.make_part(names)
        text = (await self.publish(part))["collaboration"]
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
        text = (await self.publish(part))["collaboration"]
        self.assertIn("Only existing-task management", text)
        self.assertNotIn("`Agent`", text)
        self.assertNotIn("subagent_type", text)
        self.assertNotIn("SendMessage", text)

    async def test_the_part_writes_its_own_section_and_nothing_else(self):
        """Its section follows the session's tools and goes when they do; the base prompt and the other
        sections, whatever they say, are never touched."""
        names = ["Agent", "SendMessage"]
        part = self.make_part(names)
        others = {"skills": "Another part's section.", "notes": "## Sub-agents\nUser-authored, not ours."}
        first = await self.publish(part, "CUSTOM\n## Sub-agents\nUser-authored section, not ours.\n", others)
        self.assertEqual({name: text for name, text in first.items() if name != "collaboration"}, others)
        self.assertIn("## Sub-agents", first["collaboration"])
        self.assertEqual(await self.publish(part, sections=first), first)
        names[:] = ["TaskOutput"]
        changed = await self.publish(part, sections=first)
        self.assertIn("Only existing-task management", changed["collaboration"])
        for absent in ("`Agent`", "SendMessage", "## Last Order / Sister coordination"):
            self.assertNotIn(absent, changed["collaboration"])
        names.clear()
        self.assertEqual(await self.publish(part, sections=changed), others)

    def test_all_roles_and_kinds_can_publish_but_never_register_tools(self):
        self.assertEqual(wiring.PART_MODULES.count("misaka.core.network.wiring.collaboration"), 1)
        for role in wiring.ROLE_KEYS:
            for kind in wiring.KINDS:
                with self.subTest(role=role, kind=kind):
                    self.assertTrue(wiring._qualifies(collaboration, role, kind))
                    self.assertEqual(collaboration.part(None).tools, [])


if __name__ == "__main__":
    unittest.main()

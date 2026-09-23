"""Offline regression checks for research-card method and round handoffs."""
import copy
import sqlite3
import unittest
from contextlib import closing

from misaka.core.research import planner


class ResearchMethodHandoffTests(unittest.TestCase):
    def setUp(self):
        self.task = {"local_id": "r2/a", "title": "Current task", "assignee": "10032",
                     "question": "Which explanation holds?", "rationale": "Compare the premises.",
                     "deliverable": "findings.md"}

    def test_method_fields_reach_the_card_without_changing_inputs(self):
        task = {**self.task, "method": "  Compare explanations.\nTest a counterexample.  ",
                "source_strategy": "Read both texts in context.",
                "falsifiers": "A case violating the claimed necessary condition."}
        before = copy.deepcopy(task)
        body = planner.task_body(task)
        for heading, value in (("method", task["method"].strip()),
                               ("source strategy", task["source_strategy"]),
                               ("falsifiers", task["falsifiers"])):
            with self.subTest(heading=heading):
                self.assertIn(f"## {heading}\n{value}\n", body)
                self.assertEqual(body.count(f"## {heading}\n"), 1)
        self.assertEqual(task, before)
        self.assertIn("## Execution approach", body)
        self.assertIn("misaka_card_note", body)

    def test_optional_fields_do_not_create_empty_sections(self):
        for fields in ({}, {"method": "", "source_strategy": " \n ", "falsifiers": None}):
            with self.subTest(fields=fields):
                body = planner.task_body({**self.task, **fields})
                for heading in ("method", "source strategy", "falsifiers"):
                    self.assertNotIn(f"## {heading}\n", body)
                self.assertNotIn("None", body)
                self.assertIn(self.task["question"], body)

    def test_each_method_field_can_be_used_independently(self):
        for key, heading in (("method", "method"), ("source_strategy", "source strategy"),
                             ("falsifiers", "falsifiers")):
            with self.subTest(key=key):
                self.assertIn(f"## {heading}\n0\n", planner.task_body({**self.task, key: "0"}))

    def test_previous_and_sibling_cards_are_both_preserved(self):
        sibling = {"local_id": "r2/b", "title": "Other task", "assignee": "10032"}
        # Board queries return sqlite3.Row, not dictionaries.
        with closing(sqlite3.connect(":memory:")) as con:
            con.row_factory = sqlite3.Row
            previous = con.execute("SELECT 'old-card' AS id, 'Current task' AS title, "
                                   "'nodes/n/old-card' AS output_dir").fetchall()
        for with_previous in (False, True):
            for with_sibling in (False, True):
                siblings = [self.task, sibling] if with_sibling else [self.task]
                before = copy.deepcopy(siblings)
                with self.subTest(previous=with_previous, sibling=with_sibling):
                    body = planner.task_body(self.task, siblings=siblings,
                                             previous=previous if with_previous else ())
                    self.assertEqual(body.count("## earlier cards on this node"), int(with_previous))
                    self.assertEqual(body.count("[old-card] Current task → `nodes/n/old-card`"),
                                     int(with_previous))
                    self.assertEqual(body.count("## sibling cards"), int(with_sibling))
                    self.assertEqual(body.count("r2/b · Other task → Sister 10032"), int(with_sibling))
                    self.assertNotIn("r2/a · Current task", body)
                    self.assertEqual(siblings, before)
                    self.assertEqual(previous[0]["output_dir"], "nodes/n/old-card")

    def test_instructions_keep_the_review_contract_separate(self):
        task = {"instructions": "Review the saved draft.\n", "method": "Ordinary research method"}
        for run_id in (None, "fixture-run"):
            with self.subTest(run_id=run_id):
                # Internal review cards need no ordinary research fields or context rows.
                body = planner.task_body(task, run_id=run_id, node={}, siblings=[{}], previous=[{}])
                self.assertTrue(body.startswith("Review the saved draft.\n\n## Execution approach"))
                self.assertEqual(body.count("## Execution approach"), 1)
                self.assertNotIn("Ordinary research method", body)
                self.assertNotIn("## research question", body)
                self.assertNotIn("misaka_card_note", body)
                self.assertEqual("# Research context" in body, run_id is not None)


if __name__ == "__main__":
    unittest.main()

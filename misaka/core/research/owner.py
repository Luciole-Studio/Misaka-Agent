"""Research as the owner of its cards, answering what the board asks of a workflow (``platform.owners``).

Everything the board used to know about research lives here: a card's run and node, the run's own
session settings, the red-team and divergence-review fields of a card note and the checks on their
submission, the guard that keeps ordinary board tools off a driven card, the run's token cap, who a
message sent from inside a run means, and the run's chat. A card's scope is its run, its unit the node.
"""

from __future__ import annotations

import json
import sqlite3

from pydantic import Field

from misaka.core.platform import owners
from misaka.core.research import runs


def _has_runs(con):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_runs'").fetchone())


TOOL_DESCRIPTIONS = {
    "misaka_research_assign": ("Submit or revise this phase's research plan -- its cards and, in a node's first plan, "
                               "its forks; recording it is not a launch receipt."),
    "misaka_research_start": ("Record the user's explicit go-ahead for the pending research plan or reconciliation "
                              "through its owning session."),
    "misaka_research_dispose": ("Answer every material red-team issue of this node: revise, rebut, concede, covered, "
                                "park or branch; corrections stay in the node."),
    "misaka_research_decide": "Record the forks this node opens and why the alternatives not taken are not opened.",
    "misaka_research_reconcile": ("Reconcile the research graph at a level: open options as nodes, join finished "
                                  "nodes, review coverage claims across lines, record options not pursued and "
                                  "relations between nodes."),
    "misaka_research_dissolve": ("Record that a conclusion dissolves a question rather than answering it; dissolving "
                                 "the research question waits for the user's consent."),
    "misaka_research_consent": ("Record the user's decision on dissolving the research question, once they have "
                                "decided in conversation."),
    "misaka_research_relate": "Record connections between research nodes that the survey brings to light.",
    "misaka_research_withdraw": "Withdraw the pending research round when the user chooses to conclude without it.",
    "misaka_research_skip": "Skip the current research node only when the user chooses that outcome.",
    "misaka_research_retry": "Run a failed research node again without waiting for the level to end.",
}


# What research derives in a project and git should not record: the source bundles, rebuilt at
# every settle as hard links of files kept elsewhere.
GITIGNORE = """# research source bundles: rebuilt at every settle, hard links of files kept elsewhere
nodes/**/SOURCES.md
nodes/**/sources/
nodes/**/.SOURCES-*.part
final/*-SOURCES.md
final/*-sources/
final/.SOURCES-*.part
"""


class ResearchOwner(owners.CardOwner):
    name = "research"
    scope_label = "research run"
    token_cap_setting = "research.token_cap"
    resume_command = "/research resume"

    # --- its cards ------------------------------------------------------------------------------

    def card(self, con, task_id):
        context = runs.task_context(con, task_id)
        return {**context, "scope": context["run_id"]} if context else None

    def session_overrides(self, con, context):
        # The run's own compaction threshold and output limit, when it chose them.
        return runs.session_overrides(runs.get(con, context["run_id"]))

    def card_deleted(self, con, task_id):
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_cards'").fetchone():
            con.execute("DELETE FROM research_cards WHERE task_id=?", (task_id,))   # a link without its card is a trap for resume

    def note_fields(self):
        from misaka.core.research.commands import Alternative, Issue

        return {
            "issues": (list[Issue] | None, Field(
                None, description="Red-team cards: complete issue list for your node Last Order; [] means no issues.")),
            "alternatives": (list[Alternative] | None, Field(
                None, description="Divergence-review cards: the complete list of alternatives; [] means none.")),
        }

    def note(self, con, task_id, context, values, record):
        recorded = []
        if values.get("issues") is not None:
            if not context or context["kind"] not in runs.CRITIQUE_KINDS:
                raise ValueError("Only this node's red-team card may record critique issues.")
            if not record("research_critique", {"issues": [item.model_dump() for item in values["issues"]]}):
                raise ValueError("Card ownership changed before its review was recorded.")
            recorded.append(f"{len(values['issues'])} issue(s)")
        if values.get("alternatives") is not None:
            if not context or context["kind"] != "divergence":
                raise ValueError("Only this node's divergence-review card may record alternatives.")
            if not record("research_divergence", {"alternatives": [item.model_dump() for item in values["alternatives"]]}):
                raise ValueError("Card ownership changed before its alternatives were recorded.")
            recorded.append(f"{len(values['alternatives'])} alternative(s)")
        return recorded

    def submission(self, con, task, context, submission, attempt_payload):
        from misaka.core.network.worker import IncompleteSubmission

        # Typed tool data, frozen with the card's completion. Never reopen a model-written JSON file.
        critique = attempt_payload("research_critique")
        if critique is not None:
            submission["issues"] = critique["issues"]
        divergence = attempt_payload("research_divergence")
        if divergence is not None:
            submission["alternatives"] = divergence["alternatives"]
        if context["kind"] in runs.CRITIQUE_KINDS and "issues" not in submission:
            raise IncompleteSubmission(
                "Record the red-team issues with misaka_card_note before completing the card "
                "(issues=[] if none)."
            )
        if context["kind"] == "divergence" and "alternatives" not in submission:
            raise IncompleteSubmission(
                "Record the alternatives with misaka_card_note before completing the card "
                "(alternatives=[] if none)."
            )

    def guard(self, con, task_id, context):
        raise ValueError(
            f"Card {task_id} belongs to research run {context['run_id']}; its driver owns "
            "assignment, dependencies and review. Use the current research phase tools, "
            f"or ask the user to run /research resume {context['run_id']} for a paused run."
        )

    def reads_results(self, con, context):
        try:
            return runs.is_active(con, context["run_id"])
        except Exception:  # noqa: BLE001 - a broken board must not silence the notification; wake LO
            return False

    def budget_scope(self, con, task_id):
        """The run a card (or the run itself) is capped with, and every card of that run."""
        if not task_id or not _has_runs(con):
            return None
        run = con.execute("SELECT id FROM research_runs WHERE id=?", (str(task_id),)).fetchone()
        if run is None:
            run = con.execute("SELECT run_id FROM research_cards WHERE task_id=?", (str(task_id),)).fetchone()
        if run is None:
            return None
        run_id = run[0]
        return {run_id, *(row[0] for row in con.execute(
            "SELECT task_id FROM research_cards WHERE run_id=?", (run_id,)))}

    # --- who works where --------------------------------------------------------------------------

    def sender_scope(self, con, task_id, session_id):
        """The run and node the sender works on: her card's node, or the node whose Last Order
        conversation she is; a run's window outside its nodes has no node."""
        try:
            if task_id:
                row = con.execute("SELECT run_id, branch_id FROM research_cards WHERE task_id=?", (task_id,)).fetchone()
                if row:
                    return row["run_id"], row["branch_id"]
            if session_id:
                tail = f"%{session_id}.jsonl"   # stored session files end in their session id
                row = con.execute("SELECT run_id, id FROM research_execution WHERE session_file LIKE ?",
                                  (tail,)).fetchone()
                if row:
                    return row["run_id"], row["id"]
                row = con.execute("SELECT id FROM research_runs WHERE origin_session=? OR root_session LIKE ?",
                                  (session_id, tail)).fetchone()
                if row:
                    return row["id"], None
        except sqlite3.OperationalError:     # a board research has never touched has no such tables
            pass
        return None, None

    def unit_session_file(self, con, unit):
        row = con.execute("SELECT session_file FROM research_execution WHERE id=?", (unit,)).fetchone()
        return row["session_file"] if row else None

    def scope_cards(self, con, scope):
        return [(row["task_id"], row["branch_id"]) for row in con.execute(
            "SELECT task_id, branch_id FROM research_cards WHERE run_id=?", (scope,))]

    def chat_scopes(self, con, session_id):
        if not _has_runs(con):
            return []
        return [row["id"] for row in con.execute("SELECT id FROM research_runs WHERE origin_session=?", (session_id,))]

    def owns_chat(self, con, scope, session_id):
        return bool(con.execute("SELECT 1 FROM research_runs WHERE id=? AND origin_session=?",
                                (scope, session_id)).fetchone())

    def progress(self, scope, payload):
        content = f"Research `{scope}` | depth {payload['depth']} | node {payload['node_id']} | {payload['message']}"
        if payload.get("plan"):
            content += "\n```json\n" + json.dumps(payload["plan"], ensure_ascii=False, indent=2) + "\n```"
        for item in ([] if payload.get("plan") else payload.get("tasks") or []):
            content += f"\n- {item['title']} → Sister {item['assignee']}"
            if item.get("dependencies"):
                content += f" (depends on {', '.join(item['dependencies'])})"
        if payload.get("red_team") and not payload.get("plan"):
            content += "\nRed team: " + json.dumps(payload["red_team"], ensure_ascii=False)
        return "research-progress", content

    # --- sessions -----------------------------------------------------------------------------------

    def system_context(self, session, tool_names, system_prompt, sister_id):
        from misaka.core.research.prompting import system_context

        return system_context(session, tool_names, system_prompt, sister_id=sister_id) or ""

    def tool_descriptions(self):
        return dict(TOOL_DESCRIPTIONS)

    def coordination_note(self, tool_names):
        if any(name in TOOL_DESCRIPTIONS for name in tool_names):
            return ("In Research, the driver creates and launches the planned cards after the applicable phase gate. "
                    "Do not duplicate those assignments through ordinary board tools or messages.")
        return ""

    def card_discipline(self, context):
        from misaka.core.research.planner import RESEARCH_SISTER_DISCIPLINE

        return RESEARCH_SISTER_DISCIPLINE

    def borrowed_tools(self, collector):
        from misaka.core.research import tools as research_tools

        research_tools.register(collector)

    def owns_window(self):
        from misaka.core.research.wiring.node import node_identity

        return node_identity() is not None   # a research node's window: the run's, not the user's desk

    def catalog(self, con):
        if not _has_runs(con):
            return {}, {}, {}
        scopes = {row["id"]: row for row in runs.listing(con)}
        units = {row["id"]: row for run_id in scopes for row in runs.nodes(con, run_id)}
        cards = {row["task_id"]: dict(row) for row in con.execute("SELECT * FROM research_cards")}
        return scopes, units, cards

    def session_dirs(self):
        return frozenset({"research"})

    def session_place(self, parts, units):
        """A run's Last Order conversations live under ``research/<run>--root-lo`` and
        ``research/<run>--node-<node>`` (``runs.session_dir``)."""
        if len(parts) < 3 or parts[0] != "research":
            return None
        run_parts = parts[1].split("--", 3)
        if len(run_parts) < 2:
            return None
        run, scope = run_parts[0], run_parts[1]
        if scope == "root-lo":
            root = next((unit for unit in units.values() if unit["run_id"] == run and unit["depth"] == 0), None)
            return run, root["id"] if root else None, True
        if scope.startswith("node-"):
            return run, scope[5:], True
        return run, None, False

    # --- the board's settings and files -------------------------------------------------------------

    def token_cap(self):
        """settings.json ``research.token_cap``: what one run may spend (0 = none)."""
        from misaka.config.product import setting

        return max(0, setting("research", "token_cap", 0, int))      # a negative cap is none, as 0 is

    def beast_at(self):
        """settings.json ``research.beast_at``: the share of the cap where cards switch to beast mode."""
        from misaka.config.product import setting

        return setting("research", "beast_at", 0.85, float)

    def read_only_tools(self):
        return frozenset({"misaka_research_view"})

    def owns_card_process(self, argv):
        # The research worker wrapper and its durable Sister child may both own the attempt.
        module = next((argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "-m"), None)
        return module == "misaka.cli.research_node" and "--run-card" in argv

    def gitignore(self):
        return GITIGNORE

    def frozen_refusal(self, target):
        return runs.frozen_refusal(target)


def install():
    """Make research the owner of its cards in this process (``misaka.cli.bootstrap``)."""
    owners.register(ResearchOwner())

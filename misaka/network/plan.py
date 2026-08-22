"""Last Order batch planner for hypotheses and independent task cards."""
from misaka.platform import tasks as db
from misaka.network import validate

# Planning contract used by the Last Order batch planner.
PLAN_CONTRACT = """# Last Order — Misaka Network Planner

Decompose the requested goal; do not execute it. You have no tools. Return exactly one JSON object and no other text:
{"bet": "A concise, falsifiable working hypothesis", "cards": [ ... ]}

The bet should express the plan's most useful, non-obvious hypothesis. It guides investigation but is never treated as
truth. State what evidence could overturn it. If no honest non-obvious hypothesis exists, say so and name the
overlooked evidence that could change the picture, rather than forcing one.

Card rules:
1. Each card is {"title":"...", "body":"...", "assignee":"roster Sister id", "priority":0}.
2. Every body must contain these sections:
   ## goal
   The exact deliverable and content.
   ## boundaries
   What this card does not cover, to prevent overlap and scope drift.
   ## acceptance criteria
   Verifiable files and required contents for independent review.
3. Cards are independent; this batch planner does not create dependencies.
4. Use one to four cards. Use one when meaningful decomposition is unavailable.
5. Choose assignees only from the supplied roster.
6. State unavailable scope honestly in boundaries rather than pretending it is covered.
"""


def build_prompt(goal, sisters):
    """Build the pure planning prompt from a goal and Sister roster."""
    roster = "\n".join(f"- {s}" for s in sorted(sisters))
    return (
        f"{PLAN_CONTRACT}\n# Research objective\n{goal}\n\n"
        f"# Sister roster (choose assignees only from this list)\n{roster}\n\n"
        "Return only the required JSON object."
    )


def make(cfg, goal, sisters):
    """Ask Last Order for a plan, then validate its schema."""
    from misaka.network import worker
    obj, raw, err = worker.run_llm_json(
        f"{cfg['roles_root']}/last_order", build_prompt(goal, sisters),
        cfg["provider"], cfg["default_model"], timeout=300, soul=False)
    if err:
        return None, [], [f"""Last Order failed: {err}"""], raw
    bet, cards, errors = validate.validate_plan(obj, sisters)
    return bet, cards, errors, raw


def submit(con, bet, cards, *, workspace=None):
    """Persist the hypothesis and task cards, returning their IDs."""
    out = []
    for card in cards:
        body = card["body"]
        if bet:
            body = f"""## Working hypothesis (to be tested, not assumed true)
{bet}

{body}"""
        task_id = db.create_task(
            con, card["title"], body=body, assignee=card["assignee"],
            model=card["model"], priority=card["priority"],
            timeout_seconds=card["timeout"], workspace=workspace,
        )
        if bet:
            db.add_event(con, task_id, "plan_hypothesis", {"text": bet})
        out.append(task_id)
    return out

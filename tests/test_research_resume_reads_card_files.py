"""R31 (2026-10-25): a halt's stop reached card X4's file, then the process died before the index
committed. The resume read the index (todo), skipped her, and the next dispatch tick put the
file's stop back: a card stranded stopped while its run went on. A resume reads the files first."""
from test_research_node_routine import RESEARCHER, lab  # noqa: F401 - the fixture

from misaka.core.platform import cards, tasks
from misaka.core.research import runs


def _linked(lab, title, needs=()):  # noqa: F811
    con, run, root = lab.con, lab.run, lab.root
    tid = cards.create(con, str(lab.tmp), title, "## deliverable\nx.md\n", RESEARCHER, needs=needs)
    runs.link_task(con, run["id"], tid, kind="research", node=root, local_id=title.lower())
    con.commit()
    return tid


def _file_status(lab, task_id):  # noqa: F811
    return cards.read(cards.card_path(str(lab.tmp), task_id))["fields"]["status"]


def test_a_stop_that_reached_only_the_card_file_is_reopened_by_the_resume(lab):  # noqa: F811
    con, run = lab.con, lab.run
    parent = _linked(lab, "P")
    clean = _linked(lab, "Clean", needs=[parent])
    torn = _linked(lab, "Torn", needs=[parent])
    tasks.mark_stopped(con, clean, generation=1)
    cards.set_fields(str(lab.tmp), torn, status="stopped", generation=1)    # the commit never came
    con.commit()
    assert tasks.get(con, torn)["status"] == "todo"

    runs.resume(con, run["id"], driver_lock="driver:test")

    for card in (clean, torn):
        row = tasks.get(con, card)
        assert (row["status"], row["generation"]) == ("todo", 2), card
        assert tasks.latest_payload(con, card, "research_resumed", generation=2) is not None
    cards.reconcile(con, str(lab.tmp))                                       # the next tick
    assert tasks.get(con, torn)["status"] == "todo" and _file_status(lab, torn) == "todo"


def test_a_running_card_is_left_alone(lab):  # noqa: F811
    con, run = lab.con, lab.run
    card = _linked(lab, "Busy")
    assert tasks.claim(con, card, "lock:1", generation=1)
    cards.set_fields(str(lab.tmp), card, status="stopped", generation=1)
    con.commit()

    runs.resume(con, run["id"], driver_lock="driver:test")

    row = tasks.get(con, card)
    assert (row["status"], row["generation"]) == ("running", 1)

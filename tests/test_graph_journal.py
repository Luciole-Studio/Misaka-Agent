"""The graph on a real board database: its tables, its journal, its versions and its lease."""

from __future__ import annotations

from contextlib import closing

import pytest

from misaka.core import graph
from misaka.core.platform import tasks

SCHEMA = {
    "nodes": {"q": {"max": 5, "frozen": ["depth"]}, "note": {}},
    "edges": {
        "fork": {"from": ["q"], "to": ["q"], "acyclic": True, "max_depth": 2, "frozen": True},
        "about": {"from": ["note"], "to": ["q"], "max_out": 1},
    },
    "gates": ["dissolve"],
}


@pytest.fixture
def board(tmp_path):
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        graph.init(con)
        yield con


@pytest.fixture
def held(board):
    made = graph.create(board, "demo", SCHEMA)
    assert graph.acquire(board, made.id, "driver", ttl=60).acquired
    return board, made.id


def _grow(change, entry):
    """A handler: a root, or a child of the node the entry names."""
    parent = entry.payload.get("parent")
    node = change.add_node("q", {"depth": 0 if parent is None else 1, "question": entry.payload["question"]})
    if parent is not None:
        change.add_edge("fork", parent, node)
    return {"node": node}


HANDLERS = {"grow": _grow, "dissolve": lambda change, entry: None}


def _grow_root(con, graph_id, question="Why?"):
    return graph.commit(con, graph_id, "grow", {"question": question}, token="driver", handlers=HANDLERS)["node"]


# --- tables and graphs ----------------------------------------------------------------------------------


def test_init_makes_the_tables_once_and_marks_their_version(board):
    graph.init(board)
    marker = board.execute("SELECT version FROM schema_migrations WHERE component='graph'").fetchall()
    assert [row[0] for row in marker] == [1]


def test_a_new_graph_is_empty_at_version_0(board):
    made = graph.create(board, "demo", SCHEMA)
    assert made.id.startswith("g_") and made.kind == "demo"
    assert graph.get(board, made.id).schema.to_json() == SCHEMA
    empty = graph.snapshot(board, made.id)
    assert empty.version == 0 and empty.nodes() == () and graph.version(board, made.id) == 0


def test_create_refuses_bad_names_ids_and_schemas(board):
    graph.create(board, "demo", SCHEMA, id="mine")
    for attempt in (lambda: graph.create(board, "demo", SCHEMA, id="mine"), lambda: graph.create(board, "", SCHEMA),
                    lambda: graph.create(board, "demo", {"nodes": {}, "edges": {"x": {"from": ["q"], "to": ["q"]}}})):
        with pytest.raises(graph.GraphError):
            attempt()
    with pytest.raises(graph.GraphError):
        graph.snapshot(board, "nowhere")


# --- applying -------------------------------------------------------------------------------------------


def test_only_the_lease_holder_applies(board):
    made = graph.create(board, "demo", SCHEMA)
    with pytest.raises(graph.LeaseError):
        _grow_root(board, made.id)
    assert graph.acquire(board, made.id, "driver", ttl=60).acquired
    assert not graph.acquire(board, made.id, "intruder", ttl=60).acquired
    _grow_root(board, made.id)
    with pytest.raises(graph.LeaseError):
        graph.commit(board, made.id, "grow", {"question": "?"}, token="intruder", handlers=HANDLERS)


def test_each_application_is_a_version_and_every_version_stays_readable(held):
    con, graph_id = held
    root = _grow_root(con, graph_id)
    child = graph.commit(con, graph_id, "grow", {"question": "How?", "parent": root}, token="driver",
                         handlers=HANDLERS)["node"]
    graph.commit(con, graph_id, "changes", [{"op": "update", "id": child, "attrs": {"status": "closed"}}],
                 token="driver")
    now = graph.snapshot(con, graph_id)
    assert now.version == 3 == graph.version(con, graph_id)
    assert [node.id for node in now.nodes()] == [root, child]
    assert now.node(child).attrs == {"depth": 1, "question": "How?", "status": "closed"}
    assert graph.snapshot(con, graph_id, at=2).node(child).attrs == {"depth": 1, "question": "How?"}
    assert [node.id for node in graph.snapshot(con, graph_id, at=1).nodes()] == [root]
    assert graph.snapshot(con, graph_id, at=0).nodes() == ()
    [edge] = now.out_edges(root, "fork")
    assert edge.target == child and now.in_edges(child) == (edge,)


def test_retiring_a_node_takes_its_edges_and_the_past_keeps_them(held):
    con, graph_id = held
    root = _grow_root(con, graph_id)
    note = graph.commit(con, graph_id, "changes", [{"op": "add_node", "kind": "note", "id": "n1"},
                                                   {"op": "add_edge", "kind": "about", "source": "n1", "target": root}],
                        token="driver")["ids"][0]
    graph.commit(con, graph_id, "changes", [{"op": "retire", "id": note}], token="driver")
    assert graph.snapshot(con, graph_id).edges() == () and graph.snapshot(con, graph_id).node(note) is None
    assert len(graph.snapshot(con, graph_id, at=2).edges("about")) == 1
    with pytest.raises(graph.GraphError):
        graph.commit(con, graph_id, "changes", [{"op": "add_node", "kind": "note", "id": "n1"}], token="driver")


def test_a_change_that_breaks_the_schema_writes_nothing_at_all(held):
    con, graph_id = held
    root = _grow_root(con, graph_id)
    con.execute("CREATE TABLE side (value TEXT)")

    def cyclic(change, entry):
        con.execute("INSERT INTO side VALUES ('written by the handler')")
        other = change.add_node("q", {"depth": 1})
        change.add_edge("fork", root, other)
        change.add_edge("fork", other, root)

    before = len(graph.entries(con, graph_id))
    with pytest.raises(graph.ConstraintError) as refused:
        graph.commit(con, graph_id, "cyclic", token="driver", handlers={"cyclic": cyclic})
    assert refused.value.violations[0].rule == "edge.acyclic"
    assert graph.version(con, graph_id) == 1 and len(graph.snapshot(con, graph_id).nodes()) == 1
    assert len(graph.entries(con, graph_id)) == before
    assert con.execute("SELECT COUNT(*) FROM side").fetchone()[0] == 0


def test_a_handler_sees_its_own_edits_and_may_join_nodes_it_makes_later(held):
    con, graph_id = held
    root = _grow_root(con, graph_id)

    def late(change, entry):
        change.add_edge("about", "n_late", root, id="e_late")
        assert change.view.edge("e_late").target == root and change.view.node("n_late") is None
        change.add_node("note", {"text": "added after its edge"}, id="n_late")
        return {"notes": len(change.view.nodes("note"))}

    assert graph.commit(con, graph_id, "late", token="driver", handlers={"late": late}) == {"notes": 1}
    assert graph.snapshot(con, graph_id).in_edges(root, "about")[0].source == "n_late"


def test_a_failing_handler_leaves_its_entry_proposed(held):
    con, graph_id = held
    proposed = graph.propose(con, graph_id, "boom", {"x": 1}, key="boom")

    def boom(change, entry):
        change.add_node("q", {"depth": 0})
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError):
        graph.apply(con, proposed.id, token="driver", handlers={"boom": boom})
    assert graph.entry(con, proposed.id).status == graph.PROPOSED and graph.version(con, graph_id) == 0
    with pytest.raises(graph.JournalError):
        graph.apply(con, proposed.id, token="driver")


def test_frozen_attributes_and_depth_hold_through_the_journal(held):
    con, graph_id = held
    root = _grow_root(con, graph_id)
    with pytest.raises(graph.ConstraintError):
        graph.commit(con, graph_id, "changes", [{"op": "update", "id": root, "attrs": {"depth": 4}}], token="driver")
    child = graph.commit(con, graph_id, "grow", {"question": "a", "parent": root}, token="driver",
                         handlers=HANDLERS)["node"]
    grandchild = graph.commit(con, graph_id, "grow", {"question": "b", "parent": child}, token="driver",
                              handlers=HANDLERS)["node"]
    with pytest.raises(graph.ConstraintError) as refused:
        graph.commit(con, graph_id, "grow", {"question": "c", "parent": grandchild}, token="driver", handlers=HANDLERS)
    assert refused.value.violations[0].rule == "edge.max_depth"


def test_the_schema_changes_only_to_one_the_graph_already_keeps(held):
    con, graph_id = held
    for question in ("a", "b", "c"):
        _grow_root(con, graph_id, question)
    tighter = {**SCHEMA, "nodes": {**SCHEMA["nodes"], "q": {"max": 2, "frozen": ["depth"]}}}
    with pytest.raises(graph.ConstraintError):
        graph.commit(con, graph_id, "schema", tighter, token="driver")
    looser = {**SCHEMA, "nodes": {**SCHEMA["nodes"], "q": {"max": 9, "frozen": ["depth"]}}}
    graph.commit(con, graph_id, "schema", looser, token="driver")
    assert graph.get(con, graph_id).schema.nodes["q"].max == 9 and graph.version(con, graph_id) == 4


def test_ops_need_a_handler_and_edits_must_be_known(held):
    con, graph_id = held
    with pytest.raises(graph.JournalError):
        graph.commit(con, graph_id, "grow", {"question": "?"}, token="driver")
    with pytest.raises(graph.JournalError):
        graph.commit(con, graph_id, "changes", [{"op": "teleport"}], token="driver")
    with pytest.raises(graph.GraphError):
        graph.commit(con, graph_id, "changes", [{"op": "update", "id": "ghost", "attrs": {}}], token="driver")


# --- the journal ----------------------------------------------------------------------------------------


def test_proposing_again_is_safe_and_a_different_payload_needs_supersede(held):
    con, graph_id = held
    first = graph.propose(con, graph_id, "plan", {"cards": 2}, key="plan:1", scope="b1", idem="call-1")
    assert graph.propose(con, graph_id, "plan", {"cards": 2}, key="plan:1", scope="b1") == first
    assert graph.propose(con, graph_id, "plan", {"cards": 9}, key="other", idem="call-1") == first
    with pytest.raises(graph.JournalError):
        graph.propose(con, graph_id, "plan", {"cards": 3}, key="plan:1", scope="b1")
    second = graph.propose(con, graph_id, "plan", {"cards": 3}, key="plan:1", scope="b1", supersede=True)
    assert second.version == 2 and graph.entry(con, first.id).status == graph.VOID
    assert graph.current(con, graph_id, "plan:1", scope="b1") == second
    assert graph.current(con, graph_id, "plan:1") is None


def test_a_superseded_proposal_loses_its_approval(held):
    con, graph_id = held
    first = graph.propose(con, graph_id, "plan", {"cards": 2}, key="plan:1", gated=True)
    graph.approve(con, first.id, approver={"summary": "go"})
    second = graph.propose(con, graph_id, "plan", {"cards": 3}, key="plan:1", gated=True, supersede=True)
    assert second.status == graph.PROPOSED and second.approver is None
    with pytest.raises(graph.JournalError):
        graph.approve(con, first.id)
    with pytest.raises(graph.JournalError):
        graph.apply(con, second.id, token="driver", handlers={"plan": lambda change, entry: None})


def test_gated_entries_wait_for_their_approval(held):
    con, graph_id = held
    floor = graph.propose(con, graph_id, "dissolve", {"why": "moot"}, key="dissolve:1")
    assert floor.gated
    asked = graph.propose(con, graph_id, "plan", {"cards": 1}, key="plan:1", gated=True)
    for gated in (floor, asked):
        with pytest.raises(graph.JournalError):
            graph.apply(con, gated.id, token="driver", handlers={"plan": lambda change, entry: None, **HANDLERS})
    approved = graph.approve(con, asked.id, approver={"summary": "first"})
    approved = graph.approve(con, asked.id, approver={"summary": "again"})
    assert approved.status == graph.APPROVED and approved.approver == {"summary": "again"}
    receipt = graph.apply(con, asked.id, token="driver", handlers={"plan": lambda change, entry: {"cards": ["t_1"]}})
    assert receipt == {"cards": ["t_1"]}
    assert graph.apply(con, asked.id, token="driver") == receipt
    assert graph.entry(con, asked.id).seq == 1
    with pytest.raises(graph.JournalError):
        graph.commit(con, graph_id, "dissolve", {"why": "now"}, token="driver", handlers=HANDLERS)


def test_an_applied_key_cannot_be_replaced(held):
    con, graph_id = held
    graph.commit(con, graph_id, "grow", {"question": "a"}, key="root", token="driver", handlers=HANDLERS)
    again = graph.commit(con, graph_id, "grow", {"question": "a"}, key="root", token="driver", handlers=HANDLERS)
    assert again == graph.current(con, graph_id, "root").receipt and graph.version(con, graph_id) == 1
    with pytest.raises(graph.JournalError):
        graph.propose(con, graph_id, "grow", {"question": "b"}, key="root", supersede=True)


def test_withdrawing_voids_a_proposal_but_never_an_applied_entry(held):
    con, graph_id = held
    proposed = graph.propose(con, graph_id, "plan", {"cards": 1}, key="plan:1")
    assert graph.withdraw(con, proposed.id).status == graph.VOID
    assert graph.withdraw(con, proposed.id).status == graph.VOID
    applied = graph.propose(con, graph_id, "grow", {"question": "a"}, key="root")
    graph.apply(con, applied.id, token="driver", handlers=HANDLERS)
    with pytest.raises(graph.JournalError):
        graph.withdraw(con, applied.id)


def test_entries_list_every_version_narrowed_as_asked(held):
    con, graph_id = held
    graph.propose(con, graph_id, "plan", {"r": 1}, key="plan:1", scope="b1")
    graph.propose(con, graph_id, "plan", {"r": 1.5}, key="plan:1", scope="b1", supersede=True)
    graph.propose(con, graph_id, "plan", {"r": 2}, key="plan:2", scope="b1")
    graph.propose(con, graph_id, "skip", {}, key="skip", scope="b1")
    graph.propose(con, graph_id, "plan", {"r": 1}, key="plan:1", scope="b2")
    plans = graph.entries(con, graph_id, scope="b1", key_prefix="plan:")
    assert [(entry.key, entry.version, entry.status) for entry in plans] == [
        ("plan:1", 1, graph.VOID), ("plan:1", 2, graph.PROPOSED), ("plan:2", 1, graph.PROPOSED)]
    standing = graph.entries(con, graph_id, scope="b1", key_prefix="plan:", status=[graph.PROPOSED, graph.APPROVED])
    assert [entry.key for entry in standing] == ["plan:1", "plan:2"]
    assert [entry.scope for entry in graph.entries(con, graph_id, op="skip")] == ["b1"]


# --- snapshots and the lease -------------------------------------------------------------------------------


def test_a_snapshot_is_reused_until_the_graph_moves_and_its_values_cannot_change(held):
    con, graph_id = held
    root = _grow_root(con, graph_id)
    first = graph.snapshot(con, graph_id)
    assert graph.snapshot(con, graph_id) is first
    with pytest.raises(TypeError):
        first.node(root).attrs["question"] = "changed"
    with tasks.write_txn(con):
        assert graph.snapshot(con, graph_id) is not first
    _grow_root(con, graph_id, "Again?")
    assert graph.snapshot(con, graph_id) is not first and len(graph.snapshot(con, graph_id).nodes()) == 2


def test_the_lease_passes_on_when_it_expires_or_its_holder_is_gone(held):
    con, graph_id = held
    assert graph.acquire(con, graph_id, "driver", ttl=60) == graph.Lease(acquired=True, holder="driver",
                                                                         previous="driver")
    assert graph.acquire(con, graph_id, "next", ttl=60, stale=lambda holder: False).acquired is False
    taken = graph.acquire(con, graph_id, "next", ttl=60, stale=lambda holder: holder == "driver")
    assert taken == graph.Lease(acquired=True, holder="next", previous="driver")
    assert graph.renew(con, graph_id, "driver", ttl=60) is False
    with pytest.raises(graph.LeaseError):
        _grow_root(con, graph_id)
    con.execute("UPDATE graphs SET lease_expires=0 WHERE id=?", (graph_id,))
    assert graph.acquire(con, graph_id, "third", ttl=60).previous == "next"
    graph.release(con, graph_id, "next")
    assert graph.get(con, graph_id).lease_token == "third"
    graph.release(con, graph_id, "third")
    assert graph.get(con, graph_id).lease_token is None


def test_locate_finds_the_graph_that_holds_an_element(board):
    first = graph.create(board, "demo", SCHEMA)
    second = graph.create(board, "demo", SCHEMA)
    for made in (first, second):
        graph.acquire(board, made.id, "driver", ttl=60)
        graph.commit(board, made.id, "changes", [{"op": "add_node", "kind": "note", "id": "shared"}], token="driver")
    graph.commit(board, first.id, "changes", [{"op": "add_node", "kind": "note", "id": "mine"}], token="driver")
    assert graph.locate(board, "mine") == first.id and graph.locate(board, "nowhere") is None
    with pytest.raises(graph.GraphError):
        graph.locate(board, "shared")
    graph.commit(board, first.id, "changes", [{"op": "retire", "id": "mine"}], token="driver")
    assert graph.locate(board, "mine") is None

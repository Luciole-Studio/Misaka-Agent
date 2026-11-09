"""The graph's values and its rules, without a database: schemas, frozen JSON, and the constraint check."""

from __future__ import annotations

import copy
import json
import pickle
import random
from graphlib import CycleError, TopologicalSorter

import pytest

from misaka.core.graph import EdgeKind, NodeKind, Schema, SchemaError, Snapshot, thaw
from misaka.core.graph.constraints import check
from misaka.core.graph.model import (
    Edge,
    FrozenDict,
    FrozenList,
    GraphError,
    Node,
    freeze,
)


def _snapshot(nodes=(), edges=()):
    return Snapshot.of(
        "g", 1,
        [Node(id=node_id, kind=kind, attrs=freeze(attrs), ordinal=ordinal, since=1)
         for ordinal, (node_id, kind, attrs) in enumerate(nodes, 1)],
        [Edge(id=edge_id, kind=kind, source=source, target=target, attrs=freeze(attrs), ordinal=ordinal, since=1)
         for ordinal, (edge_id, kind, source, target, attrs) in enumerate(edges, 1)],
    )


def _rules(schema, after, before=None):
    return sorted(violation.rule for violation in check(schema, before or _snapshot(), after))


TREE = Schema.from_json({
    "nodes": {"q": {"max": 4, "frozen": ["depth"]}, "note": {}},
    "edges": {
        "fork": {"from": ["q"], "to": ["q"], "acyclic": True, "max_depth": 2, "max_in": 1, "frozen": True},
        "about": {"from": ["note"], "to": ["q"], "max_out": 1, "unique": True},
    },
})


# --- schema ---------------------------------------------------------------------------------------


def test_a_schema_reads_and_writes_its_json_form_with_defaults_left_out():
    data = {
        "nodes": {"q": {"max": 4, "frozen": ["depth"]}, "note": {}},
        "edges": {"about": {"from": ["note"], "to": ["q"], "max_out": 1, "unique": True}},
        "gates": ["reconcile"],
    }
    schema = Schema.from_json(data)
    assert schema.to_json() == data
    assert Schema.from_json(json.loads(json.dumps(schema.to_json()))) == schema
    assert schema.nodes["q"] == NodeKind(max=4, frozen=frozenset({"depth"}))
    assert schema.edges["about"].source == frozenset({"note"})


def test_a_schema_reports_every_problem_at_once():
    with pytest.raises(SchemaError) as refused:
        Schema.from_json({
            "nodes": {"q": {"max": -1, "colour": "red"}},
            "edges": {"fork": {"from": ["q"], "to": ["q"], "max_depth": 2, "unique": "yes"}},
            "gates": "plan",
            "extra": 1,
        })
    message = str(refused.value)
    for fragment in ("unknown fields: extra", "unknown fields: colour", "max must be a whole number",
                     "unique must be true or false", "gates must be a list of names"):
        assert fragment in message


def test_a_schema_refuses_edges_to_undeclared_kinds_and_depth_without_acyclic():
    with pytest.raises(SchemaError) as refused:
        Schema.from_json({"nodes": {"q": {}}, "edges": {"fork": {"from": ["q"], "to": ["ghost"], "max_depth": 1},
                                                          "loose": {"to": ["q"]}}})
    message = str(refused.value)
    assert "runs to undeclared node kinds: ghost" in message
    assert "needs acyclic: true" in message
    assert "needs at least one node kind to run from" in message


def test_kinds_built_in_code_normalize_their_names_and_check_their_limits():
    assert EdgeKind(source=["a"], target=("b",)).source == frozenset({"a"})
    assert NodeKind(frozen=["x"]).frozen == frozenset({"x"})
    with pytest.raises(SchemaError):
        NodeKind(max=-2)
    with pytest.raises(SchemaError):
        EdgeKind(source=["a"], target=["b"], acyclic="no")


# --- frozen JSON ------------------------------------------------------------------------------------


def test_frozen_values_refuse_change_yet_read_as_plain_json():
    value = freeze({"a": [1, {"b": 2}], "c": "x"})
    assert isinstance(value, FrozenDict) and isinstance(value["a"], FrozenList)
    assert value == {"a": [1, {"b": 2}], "c": "x"}
    assert json.loads(json.dumps(value)) == value
    for attempt in (lambda: value.__setitem__("c", "y"), lambda: value.update(c="y"), lambda: value.pop("c"),
                    lambda: value["a"].append(3), lambda: value["a"][1].__setitem__("b", 3)):
        with pytest.raises(TypeError):
            attempt()
    items = value["a"]
    with pytest.raises(TypeError):
        items += [4]
    plain = thaw(value)
    plain["a"][1]["b"] = 3
    assert type(plain) is dict and value["a"][1]["b"] == 2
    copied = copy.deepcopy(value)
    copied["a"].append(9)
    assert type(copied) is dict and value["a"] == [1, {"b": 2}]
    assert pickle.loads(pickle.dumps(value)) == value


def test_freeze_takes_only_json():
    assert freeze((1, 2)) == [1, 2]
    for bad in ({1: "x"}, float("nan"), {"set": {1}}, object()):
        with pytest.raises(GraphError):
            freeze(bad)


# --- the constraint check -----------------------------------------------------------------------------


def test_a_graph_that_keeps_its_schema_has_no_violations():
    after = _snapshot([("a", "q", {"depth": 0}), ("b", "q", {"depth": 1}), ("n", "note", {})],
                      [("e1", "fork", "a", "b", {}), ("e2", "about", "n", "b", {})])
    assert check(TREE, _snapshot(), after) == []


def test_undeclared_kinds_and_bad_ends_are_violations():
    after = _snapshot([("a", "q", {}), ("x", "ghost", {}), ("n", "note", {})],
                      [("e1", "fork", "a", "missing", {}), ("e2", "fork", "n", "a", {}), ("e3", "wire", "a", "a", {})])
    assert _rules(TREE, after) == ["edge.endpoint", "edge.endpoint", "edge.kind", "node.kind"]


def test_counts_degrees_and_duplicates_are_bounded():
    after = _snapshot([(f"q{i}", "q", {}) for i in range(5)] + [("n", "note", {})],
                      [("e1", "fork", "q0", "q1", {}), ("e2", "fork", "q2", "q1", {}),
                       ("e3", "about", "n", "q0", {}), ("e4", "about", "n", "q0", {})])
    assert _rules(TREE, after) == ["edge.max_in", "edge.max_out", "edge.unique", "node.max"]


def test_frozen_attributes_and_edges_cannot_change():
    before = _snapshot([("a", "q", {"depth": 0}), ("b", "q", {"depth": 1})], [("e1", "fork", "a", "b", {})])
    changed = _snapshot([("a", "q", {"depth": 3}), ("b", "q", {"depth": 1, "status": "closed"})])
    assert _rules(TREE, changed, before) == ["edge.frozen", "node.frozen"]
    added = _snapshot([("a", "q", {}), ("b", "q", {"depth": 1})], [("e1", "fork", "a", "b", {"w": 1})])
    assert _rules(TREE, added, _snapshot([("a", "q", {}), ("b", "q", {"depth": 1})], [("e1", "fork", "a", "b", {})])) \
        == ["edge.frozen"]
    assert _rules(TREE, _snapshot([("a", "q", {"depth": 0})]), _snapshot([("a", "q", {})])) == ["node.frozen"]


def test_a_cycle_is_named_even_when_nodes_hang_below_it():
    schema = Schema.from_json({"nodes": {"q": {}}, "edges": {"fork": {"from": ["q"], "to": ["q"], "acyclic": True}}})
    after = _snapshot([("a", "q", {}), ("b", "q", {}), ("c", "q", {}), ("s", "q", {})],
                      [("e1", "fork", "s", "a", {}), ("e2", "fork", "a", "b", {}), ("e3", "fork", "b", "a", {}),
                       ("e4", "fork", "b", "c", {})])
    [violation] = check(schema, _snapshot(), after)
    assert violation.rule == "edge.acyclic"
    assert violation.message.endswith(("a -> b -> a", "b -> a -> b"))
    [loop] = check(schema, _snapshot(), _snapshot([("a", "q", {})], [("e1", "fork", "a", "a", {})]))
    assert loop.message.endswith("a -> a")


def test_depth_is_the_longest_path_from_a_node_nothing_enters():
    schema = Schema.from_json({"nodes": {"q": {}},
                               "edges": {"fork": {"from": ["q"], "to": ["q"], "acyclic": True, "max_depth": 2}}})
    diamond = [("e1", "fork", "r", "a", {}), ("e2", "fork", "a", "b", {}), ("e3", "fork", "r", "b", {})]
    nodes = [("r", "q", {}), ("a", "q", {}), ("b", "q", {}), ("c", "q", {})]
    assert check(schema, _snapshot(), _snapshot(nodes, diamond)) == []
    [too_deep] = check(schema, _snapshot(), _snapshot(nodes, [*diamond, ("e4", "fork", "b", "c", {})]))
    assert too_deep.rule == "edge.max_depth" and too_deep.elements == ("c",)


def test_the_acyclic_and_depth_rules_agree_with_graphlib_on_random_graphs():
    for seed in range(300):
        rng = random.Random(seed)
        size = rng.randint(1, 9)
        names = [f"n{i}" for i in range(size)]
        pairs = {(rng.choice(names), rng.choice(names)) for _ in range(rng.randint(0, 14))}
        edges = [(f"e{i}", "fork", source, target, {}) for i, (source, target) in enumerate(sorted(pairs))]
        limit = rng.randint(0, 4)
        schema = Schema.from_json({"nodes": {"q": {}}, "edges": {"fork": {"from": ["q"], "to": ["q"], "acyclic": True,
                                                                         "max_depth": limit}}})
        found = check(schema, _snapshot(), _snapshot([(name, "q", {}) for name in names], edges))
        sorter = TopologicalSorter({name: {source for source, target in pairs if target == name} for name in names})
        try:
            order = list(sorter.static_order())
        except CycleError:
            assert [violation.rule for violation in found] == ["edge.acyclic"], seed
            continue
        depth = {}
        for name in order:
            depth[name] = max((depth[source] + 1 for source, target in pairs if target == name), default=0)
        assert sorted(violation.elements[0] for violation in found) == sorted(
            name for name, level in depth.items() if level > limit), seed

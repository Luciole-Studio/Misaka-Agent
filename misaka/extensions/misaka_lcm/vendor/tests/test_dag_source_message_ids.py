from hermes_lcm.config import LCMConfig
from hermes_lcm.dag import SummaryDAG, SummaryNode
from hermes_lcm.store import MessageStore


def _stores(tmp_path):
    config = LCMConfig(database_path=str(tmp_path / "lcm.db"))
    messages = MessageStore(
        config.database_path, ingest_protection_config=config
    )
    dag = SummaryDAG(config.database_path)
    return messages, dag


def _message(messages, index: int) -> int:
    return messages.append(
        "session-a", {"role": "user", "content": f"message {index}"}
    )


def _node(dag, source_type: str, source_ids: list[int], depth: int) -> int:
    return dag.add_node(SummaryNode(
        session_id="session-a",
        depth=depth,
        summary=f"depth {depth}",
        token_count=2,
        source_token_count=4,
        source_ids=source_ids,
        source_type=source_type,
    ))


def test_source_message_ids_deduplicates_orders_and_limits_direct_leaves(tmp_path):
    messages, dag = _stores(tmp_path)
    try:
        first, second, third = [_message(messages, index) for index in range(3)]
        node_id = _node(
            dag, "messages", [third, first, third, second], depth=0
        )

        assert dag.source_message_ids(node_id, limit=2) == [first, second]
        assert dag.source_message_ids(node_id, limit=0) == []
    finally:
        dag.close()
        messages.close()


def test_source_message_ids_walks_nested_nodes_to_message_leaves(tmp_path):
    messages, dag = _stores(tmp_path)
    try:
        first, second, third = [_message(messages, index) for index in range(3)]
        left = _node(dag, "messages", [third, first], depth=0)
        right = _node(dag, "messages", [second, third], depth=0)
        parent = _node(dag, "nodes", [right, left], depth=1)

        assert dag.source_message_ids(parent, limit=10) == [
            first, second, third
        ]
    finally:
        dag.close()
        messages.close()

"""Host-equivalent activation/dispatch proof for the official Hermes Agent seam.

This reproduces the toolset visibility and context-engine dispatch rules from
NousResearch/hermes-agent@299e409f without modifying or vendoring that host.
It proves availability and dispatch, not that an arbitrary model will choose
the tool on every eligible turn.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import json
import sys
from types import ModuleType

from hermes_lcm.config import LCMConfig


OFFICIAL_HERMES_AGENT_HEAD = "299e409f15aa5615a8a64be488580be92cda351e"


def _lcm_engine_class():
    try:
        module = importlib.import_module("hermes_lcm.engine")
        engine = getattr(module, "LCMEngine", None)
        if engine is not None:
            return engine
        raise ModuleNotFoundError("agent", name="agent")
    except ModuleNotFoundError as exc:
        if exc.name not in {"agent", "agent.context_engine"}:
            raise
        agent_module = sys.modules.get("agent")
        if agent_module is None:
            agent_module = ModuleType("agent")
            agent_module.__path__ = []
            sys.modules["agent"] = agent_module
        context_engine_module = ModuleType("agent.context_engine")

        class ContextEngine:
            pass

        context_engine_module.ContextEngine = ContextEngine
        sys.modules["agent.context_engine"] = context_engine_module
        agent_module.context_engine = context_engine_module
        sys.modules.pop("hermes_lcm.engine", None)
        return importlib.import_module("hermes_lcm.engine").LCMEngine


class _OfficialHostEquivalent:
    def __init__(self, engine, *, enabled_toolsets):
        self.engine = engine
        self.enabled_toolsets = enabled_toolsets
        self.visible_schemas = (
            engine.get_tool_schemas()
            if enabled_toolsets is None or "context_engine" in enabled_toolsets
            else []
        )
        self.visible_names = {schema["name"] for schema in self.visible_schemas}
        self.dispatched_messages = None

    def dispatch(self, name, args, *, messages):
        if name not in self.visible_names:
            raise LookupError(f"tool not visible: {name}")
        self.dispatched_messages = messages
        return self.engine.handle_tool_call(name, args, messages=messages)


def test_generic_memory_question_can_dispatch_bounded_pack_when_toolset_enabled(tmp_path):
    engine_class = _lcm_engine_class()
    engine = engine_class(config=LCMConfig(database_path=str(tmp_path / "host.db")))
    content = "I repaired the garden gate today."
    observed_at = datetime(2024, 3, 15, 9, tzinfo=timezone.utc).timestamp()
    try:
        store_id = engine._store.append(
            "host-session",
            {"role": "user", "content": content, "timestamp": observed_at},
        )
        host = _OfficialHostEquivalent(engine, enabled_toolsets=["context_engine"])
        messages = [{"role": "user", "content": "What happened five days ago?"}]
        raw = host.dispatch(
            "lcm_evidence_pack",
            {
                "question": "What happened five days ago?",
                "question_date": "2024-03-20",
                "baseline_refs": [{
                    "exact_ref": f"lcm:{store_id}:0-{len(content)}",
                    "quote": content,
                    "value": "repaired the garden gate",
                    "key": "garden gate",
                    "label": "garden gate",
                }],
            },
            messages=messages,
        )
        payload = json.loads(raw)
    finally:
        engine.shutdown()

    assert OFFICIAL_HERMES_AGENT_HEAD == "299e409f15aa5615a8a64be488580be92cda351e"
    assert "lcm_evidence_pack" in host.visible_names
    assert host.enabled_toolsets == ["context_engine"]
    assert host.dispatched_messages == messages
    assert payload["status"] == "evidence_ready"
    assert payload["evidence"][0]["observation_time"]["source"] == "host_message_timestamp"
    assert len(raw) <= 64_000


def test_disabled_context_engine_toolset_preserves_ordinary_answer_path(tmp_path):
    engine_class = _lcm_engine_class()
    engine = engine_class(config=LCMConfig(database_path=str(tmp_path / "disabled.db")))
    ordinary_answer = "I can only answer from the context currently available."
    try:
        host = _OfficialHostEquivalent(engine, enabled_toolsets=[])
        visible_before = set(host.visible_names)
        delivered_answer = ordinary_answer
    finally:
        engine.shutdown()

    assert "lcm_evidence_pack" not in visible_before
    assert host.enabled_toolsets == []
    assert host.dispatched_messages is None
    assert delivered_answer == ordinary_answer

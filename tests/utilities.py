"""Shared test utilities for coding-agent tests: pi's ``packages/coding-agent/test/utilities.ts`` (v1.1.0).

MISAKA fork: the credential helpers (``API_KEY``, ``resolveApiKey``, ``hasAuthForProvider``, ``PI_AGENT_DIR``,
``getRealAuthStorage``) and ``createTestSession`` serve the e2e tests that call real models with the developer's
``~/.pi/agent/auth.json``; those tests are not ported, so neither are these. ``createTestExtensionsResult`` and
``createTestResourceLoader`` live in ``tests/suite_harness.py`` and are re-exported here.
"""

from __future__ import annotations

import json
import time
from typing import Any

from misaka.core.session_manager import SessionManager
from tests.suite_harness import createTestExtensionsResult, createTestResourceLoader


def userMsg(text: str) -> dict[str, Any]:
    """Create a minimal user message for testing."""
    return {"role": "user", "content": text, "timestamp": int(time.time() * 1000)}


def assistantMsg(text: str) -> dict[str, Any]:
    """Create a minimal assistant message for testing."""
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": "anthropic-messages",
        "provider": "anthropic",
        "model": "test",
        "usage": {
            "input": 1,
            "output": 1,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 2,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        "stopReason": "stop",
        "timestamp": int(time.time() * 1000),
    }


def readSessionFileRoles(file: str) -> list[str]:
    """Read a session JSONL file and return one label per record: the message role for message entries, otherwise the
    entry type (e.g. "session", "model_change")."""
    with open(file, encoding="utf-8") as handle:
        lines = handle.read().strip().split("\n")
    labels: list[str] = []
    for line in lines:
        record = json.loads(line)
        role = (record.get("message") or {}).get("role")
        labels.append(role if role is not None else record.get("type"))
    return labels


def buildTestTree(session: SessionManager, structure: dict[str, Any]) -> dict[str, str]:
    """Build a session tree for testing using SessionManager. Returns the IDs of all created entries.

    Example tree structure::

        u1 -> a1 -> u2 -> a2
                 -> u3 -> a3  (branch from a1)
        u4 -> a4              (another root)
    """
    ids: dict[str, str] = {}
    for msg in structure["messages"]:
        if msg.get("branchFrom"):
            branchFromId = ids.get(msg["branchFrom"])
            if not branchFromId:
                raise RuntimeError(f"Cannot branch from unknown entry: {msg['branchFrom']}")
            session.branch(branchFromId)
        id = (
            session.appendMessage(userMsg(msg["text"]))
            if msg["role"] == "user"
            else session.appendMessage(assistantMsg(msg["text"]))
        )
        ids[msg["text"]] = id
    return ids


__all__ = [
    "assistantMsg",
    "buildTestTree",
    "createTestExtensionsResult",
    "createTestResourceLoader",
    "readSessionFileRoles",
    "userMsg",
]

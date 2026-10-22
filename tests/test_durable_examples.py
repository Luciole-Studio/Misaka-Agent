"""Runs every walk-through of ``tests/durable_examples`` against its scripted model, so the examples cannot rot.

Upstream ships these files as runnable examples only. Each expectation below lists the lines an example prints, in
order; the output only has to contain the expected text, and anything else may sit between them.
"""

from __future__ import annotations

import importlib
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

EXAMPLES: dict[str, list[str]] = {
    "example_00_conversation": ["standalone conversation: {'id': 2}"],
    "example_01_documents": [
        "latest notes: {'text': 'after goodbye'}",
        "notes at first entry: {'text': 'after hello'}",
        "notes at second entry: {'text': 'after goodbye'}",
    ],
    "example_02_forks": [
        "fork transcript: ['hello']",
        "fork notes: {'text': 'after hello'}",
        "fork notes after edit: {'text': 'changed only in the fork'}",
        "parent notes after edit: {'text': 'after goodbye'}",
    ],
    "example_03_owned_conversations": [
        "supervisor task: 3",
        "child conversation: {'id': 4, 'owner': {'conversationId': 2, 'taskId': 3}}",
        "registry: {'agents': {'researcher': {'conversationId': 4, 'requestId': 'researcher:first-message:3'}}}",
    ],
    "example_04_chord_state": [
        "Chord notes: hydrate 0 {'text': 'first'}",
        "Chord notes: update 1 {'text': 'published through Chord'}",
    ],
    "example_05_watches": [
        "watch baseline: {'text': 'first'}",
        "watch update: {'text': 'observed asynchronously'}",
    ],
    "example_06_harness": [
        "root: 1 {'text': 'root notes'}",
        "stored agent: {'thinkingLevel': 'low'}",
        "resolved: low ['files'] ['read']",
    ],
    "example_07_configuration": [
        "default tools: ['read', 'write', 'grep']",
        ("stored: {'model': {'provider': 'anthropic', 'modelId': 'claude-sonnet-4-5'}, 'thinkingLevel': 'high', "
        "'tools': ['write', 'read']}"),
        "model: {'provider': 'anthropic', 'modelId': 'claude-sonnet-4-5'} thinking: high tools: ['write', 'read']",
        "without search: ['read', 'write']",
        "files uninstalled: ['grep']",
        "files reinstalled: ['read', 'write', 'grep']",
    ],
    "example_08_harness_conversations": [
        "typed entry: True example",
        "helper thinking: minimal",
        "fork thinking: high",
        "lookup: True",
    ],
    "example_09_context": [
        ("raw active entries: ['message', 'message', 'message', 'message', 'pi.system', 'message', 'message', 'edit', "
        "'note']"),
        # pi 1.3.0 (#10904): the first `pi.system` entry is the baseline, which leads the request even after answers.
        ("request messages: ['system: {\"cwd\":\"<cwd>/repo</cwd>\"}', 'user: read files a and b', "
        "'assistant: reading call(a) call(b)', 'result(a)', 'result(b)', 'assistant: a and b look fine']"),
        ("fork messages: ['user: read a and b', 'assistant: reading call(a) call(b)', 'result(a) error', "
        "'result(b) error']"),
        "after summary: summary ['user: Summary: a and b are fine.']",
        "newest stored entries: ['summary', 'note', 'edit'] more: True",
    ],
    "example_10_registry_reload": [
        "after reload: ['read: Read a file', 'grep: Search files, faster']",
        "with audit: ['read: Read a file (audited)', 'grep: Search files, faster']",
        "after uninstall: ['read: Read a file', 'grep: Search files, faster']",
    ],
    "example_11_extension_state": [
        "todos: {'items': ['fix the build']}",
        ("system messages: [{'sections': None, 'toolsAdded': ['todo']}, "
        "{'sections': {'todos': '<todos>\\nfix the build\\n</todos>'}, 'toolsAdded': None}]"),
    ],
    "example_12_tasks": ["payment outcome: {'status': 'completed', 'result': {'receipt': 500}}"],
    "example_13_recovery": [
        "tick 1",
        "tick 2",
        ("closed; saved checkpoint: {'status': 'pending', 'checkpoint': {'phase': 'tick', 'n': 2}} "
        "memos: {'printed-1': True, 'printed-2': True}"),
        "tick 3",
        "tick 4",
        "tick 5",
        "after reopen: {'status': 'completed', 'result': 'counted to 5'}",
    ],
    "example_14_chat": [
        "answer: [{'type': 'text', 'text': 'Paris.'}]",
        "transcript: ['pi.user', 'pi.system', 'pi.assistant']",
    ],
    "example_15_system_prompt": [
        "root system prompt:",
        "'cwd': '<cwd>\\n/repo\\n</cwd>'",
        "'agents_md': '<agents_md>\\nRun npm run check after changes.\\n</agents_md>'",
        "subagent system prompt:",
        "'instructions': '<instructions>\\nOnly read; never edit files.\\n</instructions>'",
        "root system entries after cwd change:",
        "'cwd': '<cwd>\\n/repo/packages\\n</cwd>'",
    ],
    "example_16_real_model": ["skipped: OPENAI_API_KEY is not set"],
    "example_17_coding_tools": [
        "status: done",
        'pi.tool-result read: "hello world\\n"',
        'pi.tool-result edit: "Successfully replaced 1 block(s) in notes.txt."',
        'pi.tool-result bash: "hello durable\\n"',
        "No such file or directory",
        "answer: [{'type': 'text', 'text': 'The file now greets durable.'}]",
        'file: "hello durable\\n"',
    ],
    "example_18_print": ["This directory holds the durable package sources, tests, and docs."],
    "example_20_inbox": [
        "rejected: Conversation 1 is busy",
        "withdraw: aborted",
        # pi 1.3.0 (#10407): a data-only write is appended at once, even while busy, so the note never queues and lands
        # in time order, before the first answer.
        "inbox: ['10 followUp', '11 steer']",
        "first: done",
        "follow-up: done",
        "steer: done",
        "note: done",
        "withdrawn: unanswered aborted",
        "transcript: ['pi.user', 'app.note', 'pi.assistant', 'pi.user', 'pi.user', 'pi.assistant']",
    ],
    "example_21_late_join": [
        "view entries: ['pi.user', 'pi.system', 'pi.assistant']",
        "view tool slot: running",
        "snapshot tools: ['count running']",
        'event output now: "1\\n2\\n3\\n4\\n5\\n6\\n7\\n8\\n9\\n10\\n"',
        "event: tool_execution_end",
        "event text delta:",
        "event: run_end",
        "event: usage_changed",
    ],
    "example_22_subagent_foreground": [
        'tool subagent({"task":"Name three prime numbers."})',
        "  assistant: 2, 3, and 5.",
        "assistant: The subagent says: 2, 3, and 5.",
    ],
    "example_23_subagent_background": [
        "> Start a subagent named reader that summarizes Moby Dick.",
        'subagent spawn reader "Summarize the plot of Moby Dick."',
        # pi 1.3.0 renamed a tool result's `content` to `output`. Upstream's example 23 still returns `content`, which
        # results no longer read, so each subagent call answers with its (empty) retained output and the scripted
        # model only says "OK.". The calls themselves still spawn, message, stop, and list the subagent.
        "  → \nOK. \n",
        "> reader: A whale, a captain, an obsession.",
        "> Ask reader to summarize every chapter.",
        'subagent send reader "Now go through all chapters in detail."',
        "> Stop reader.",
        "subagent stop reader",
        "> What are my subagents doing?",
        "subagent status",
        "> Ask reader for the whale's name.",
        'subagent send reader "What is the whale called?"',
        "(process restarts)",
        "> reader: Moby Dick.",
    ],
    "example_24_child_tasks": [
        "One card is declined:",
        "payments: aborted, failed, aborted, aborted",
        "  checkout: failed",
        "The customer cancels:",
        "waiting on 13, 14, 15, 16",
        "  checkout aborted",
        "  checkout: aborted",
        "The process stops while the payments run, and a new one continues:",
        "  payments: completed, completed, completed, completed",
        "  checkout: completed",
    ],
    "example_25_compaction": [
        'after "Where should we stay?" (answered): 2 messages in context, 2 entries stored',
        "(threshold compaction summary first)",
        "manual compaction finished; its summary is queued",
        "after the answer, the summary is done",
        "(manual compaction summary first)",
        'after "What should we pack?" (answered)',
        # pi 1.1.0 prints model_error here too: requests now carry the conversation's sessionId, so the faux provider
        # simulates prompt caching, its usage counts the new prompt part twice, and a blocking threshold compaction
        # runs first; generation compacts for an overflow only once per attempt.
        'after "Summarize the plan for my partner" (model_error)',
        "(threshold compaction summary first)",
    ],
    "example_26_coding_agent": ["bash ran in: ", "bash ran in: "],
    "example_27_plan_mode": [
        "tools: ['read', 'write', 'edit', 'bash']",
        "plan mode tools: ['read', 'submit_plan']",
        "plan: ['Read PORT from the environment', 'Default to 3000']",
        "tools again: ['read', 'write', 'edit', 'bash']",
        "'added': ['read', 'submit_plan'], 'removed': None",
        "'removed': ['submit_plan']",
    ],
    "example_28_reviewer": [
        "reviewer: small ['coding-tools', 'reviewer'] ['read'] True",
        "> Review user.ts.",
        "reviewer: 1. `name` does not handle a missing user.",
        '> Look again for anything you missed. Say "No further findings." when there is nothing left.',
        "reviewer: 2. `user` has no type. No further findings.",
    ],
    "example_29_sandbox_per_conversation": ["alice's sandbox: from alice", "bob's sandbox: from bob"],
    "example_30_tool_override": [
        "plain conversation: venv:",
        "python conversation: venv: <project>/.venv",
        "timed bash calls: 2",
    ],
    "example_31_reload_and_restart": [
        "call running during the reload: v1",
        "next call: v2",
        "after restart, before install: []",
        "after install: ['version']",
    ],
}


def assertInOrder(output: str, expected: list[str]) -> None:
    position = 0
    for text in expected:
        found = output.find(text, position)
        assert found >= 0, f"{text!r} is missing from, or out of order in:\n{output}"
        position = found + len(text)


@pytest.fixture(autouse=True)
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The examples leave their storage behind; keep it in a throwaway directory, and never reach a real model."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.mark.parametrize("name", list(EXAMPLES))
async def test_example_prints_what_it_documents(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    module = importlib.import_module(f"tests.durable_examples.{name}")
    await module.main()
    assertInOrder(capsys.readouterr().out, EXAMPLES[name])


async def test_coding_agent_example_runs_bash_in_the_conversations_directory(capsys: pytest.CaptureFixture[str]) -> None:
    module = importlib.import_module("tests.durable_examples.example_26_coding_agent")
    await module.main()
    first, second = [line for line in capsys.readouterr().out.splitlines() if line.startswith("bash ran in: ")]
    # The bash tool reports POSIX paths even where the shell comes from a Windows installation.
    assert first.split("pi-durable-agent-")[1].count("/") == 0
    assert second.endswith("/app")


async def test_print_example_exits_zero_with_the_answer(capsys: pytest.CaptureFixture[str]) -> None:
    module = importlib.import_module("tests.durable_examples.example_18_print")
    assert await module.main("What is in this directory?") == 0
    assert capsys.readouterr().out == "This directory holds the durable package sources, tests, and docs.\n"


@pytest.mark.parametrize("storage", ["sqlite", "jsonl", "memory"])
async def test_json_example_streams_agent_events_as_lines(storage: str, capsys: pytest.CaptureFixture[str]) -> None:
    module = importlib.import_module("tests.durable_examples.example_19_json")
    await module.main(["--events", "--storage", storage, "List the files."])
    captured = capsys.readouterr()
    events: list[dict[str, Any]] = [json.loads(line) for line in captured.out.splitlines()]
    assert events[0]["type"] == "snapshot"
    kinds = [event["type"] for event in events]
    assert kinds[1:3] == ["message_start", "message_end"]
    assert kinds.index("run_start") < kinds.index("tool_execution_start") < kinds.index("run_end")
    submission = [event for event in events if event["type"] == "submission"][-1]
    assert submission["record"]["status"] == "done"
    assert (storage in captured.err) == (storage != "memory")


async def test_json_example_streams_view_frames_with_the_ops_flag(capsys: pytest.CaptureFixture[str]) -> None:
    module = importlib.import_module("tests.durable_examples.example_19_json")
    await module.main(["--ops", "--storage", "memory"])
    frames: list[dict[str, Any]] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert list(frames[0]) == ["view"]
    assert frames[0]["view"]["entries"] == []
    assert len(frames) > 2
    assert all(list(frame) == ["ops"] for frame in frames[1:])


async def test_json_example_rejects_an_unknown_storage() -> None:
    module = importlib.import_module("tests.durable_examples.example_19_json")
    with pytest.raises(RuntimeError, match="Unknown --storage nowhere"):
        await module.main(["--storage", "nowhere"])

"""pi durable's ``test/harness-conversations.test.ts``."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest

from misaka.ai.models import createModels
from misaka.durable import (
    ROOT_CONVERSATION_ID,
    AgentDoc,
    Harness,
    LiveDoc,
    MemoryStorage,
    ProviderDoc,
    configure,
    createRegistry,
    createSession,
    defineDoc,
    defineEntry,
    defineExtension,
    defineTask,
)
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from misaka.durable.types import ScanOrder
from misaka.utils.js import UNDEFINED
from tests.durable_support import ControlledStorage, context, toMatchObject
from tests.harness_support import addTool, openHarness, tool, user


@pytest.fixture
def sqlitePath() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-harness-")
    yield os.path.join(directory, "session.sqlite")
    shutil.rmtree(directory, ignore_errors=True)


UUID_V7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

NoteDoc = defineDoc(
    {
        "kind": "test.note",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
        "initial": lambda: {"text": ""},
    }
)

Message = defineEntry("message")


async def noAbort(_task: Any, runtime: Any, ctx: Any) -> None:
    return None


def unreachable(*_: Any) -> None:
    raise AssertionError("unreachable")


def append(conversation: Any, text: str) -> Any:
    return conversation.commit(
        lambda tx: tx.appendEntry(conversation.id, {"kind": "message", "model": [user(text)]}),
        context,
    )


async def allEntries(conversation: Any, order: ScanOrder | None = None) -> list[str]:
    texts: list[str] = []
    cursor = None
    while True:
        page = await conversation.entries({} if order is None else {"order": order}, 2, cursor, context)
        for entry in page["items"]:
            texts.append(entry["model"][0]["content"])
        cursor = page.get("next")
        if cursor is None:
            return texts


async def test_creates_the_root_lazily_with_its_agent_change_and_init_in_one_commit():
    storage = ControlledStorage()
    opened = await openHarness(storage, ["read", "bash"])
    harness = opened.harness
    assert len(storage.commits) == 0

    async def init(tx: Any, id: int) -> None:
        # The agent change applied before init.
        assert (await tx.doc(AgentDoc, id))["thinkingLevel"] == "high"
        (await tx.doc(NoteDoc, id))["text"] = "root note"

    root = await harness.root(context, {"agent": {"thinkingLevel": "high"}, "init": init})
    assert root.id == ROOT_CONVERSATION_ID
    assert len(storage.commits) == 1
    # Conversation, five built-in documents, and the init note.
    assert [write["type"] for write in storage.commits[0]] == [
        "conversation",
        "document.create",
        "document.create",
        "document.create",
        "document.create",
        "document.create",
        "document.create",
    ]
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    provider = await harness.snapshot(ProviderDoc, root.id, context)
    assert list(provider) == ["sessionId"]
    assert UUID_V7.match(provider["sessionId"])
    assert await harness.snapshot(AgentDoc, root.id, context) == {"thinkingLevel": "high"}
    agent = await root.agent(context)
    assert agent["thinkingLevel"] == "high"
    assert [each["name"] for each in agent["tools"]] == ["read", "bash"]
    assert await harness.snapshot(NoteDoc, root.id, context) == {"text": "root note"}

    again = await harness.root(context, {"agent": {"thinkingLevel": "low"}, "init": unreachable})
    assert again.id == root.id
    assert len(storage.commits) == 1
    await harness.close(context)


async def test_keeps_root_and_conversation_identity_and_state_across_reopen(sqlitePath):
    first = await openHarness(await openNodeSqliteStorage(sqlitePath), ["read"])
    root = await first.harness.root(context)
    await root.configure({"model": {"provider": "anthropic", "modelId": "claude"}, "cwd": "/repo"}, context)
    entry = await append(root, "hello")
    child = await first.harness.createConversation({"ownership": {"kind": "ownerless"}}, context)
    fork = await root.fork(entry["id"], {"ownership": {"kind": "ownerless"}}, context)

    async def providerSessionId(id: int) -> Any:
        stored = await first.harness.snapshot(ProviderDoc, id, context)
        return None if stored is None else stored["sessionId"]

    providerSessionIds = await asyncio.gather(*(providerSessionId(each.id) for each in [root, child, fork]))
    # Regression coverage for #10424: a fork must not inherit its parent's provider identity.
    for sessionId in providerSessionIds:
        assert UUID_V7.match(sessionId)
    assert len(set(providerSessionIds)) == 3
    await first.harness.close(context)
    with pytest.raises(Exception):  # noqa: B017
        await root.agent(context)

    # The new process installs nothing: the stored choices survive, the tools do not resolve.
    first = await openHarness(await openNodeSqliteStorage(sqlitePath), [])
    reopened = await first.harness.root(context, {"init": unreachable})
    assert reopened.id == ROOT_CONVERSATION_ID
    toMatchObject(
        await reopened.agent(context),
        {"model": {"provider": "anthropic", "modelId": "claude"}, "cwd": "/repo", "tools": []},
    )
    assert await allEntries(reopened) == ["hello"]
    assert (await first.harness.conversation(child.id, context)).id == child.id
    assert await asyncio.gather(*(providerSessionId(id) for id in [root.id, child.id, fork.id])) == providerSessionIds
    reopenedFork = await first.harness.conversation(fork.id, context)
    assert await allEntries(reopenedFork) == ["hello"]
    assert (await reopenedFork.agent(context))["model"] == {"provider": "anthropic", "modelId": "claude"}
    assert await first.harness.conversation(999, context) is None
    await first.harness.close(context)


async def test_creates_independent_conversations_atomically_with_init_and_rolls_back_failures():
    storage = ControlledStorage()
    opened = await openHarness(storage, ["read"])
    harness, registry = opened.harness, opened.registry

    async def seed(tx: Any, id: int) -> None:
        await tx.appendEntry(id, {"kind": "message", "model": [user("seed")]})

    created = await harness.createConversation({"ownership": {"kind": "ownerless"}, "init": seed}, context)
    assert len(storage.commits) == 1
    assert await allEntries(created) == ["seed"]

    before = len(storage.commits)

    def failing(tx: Any, id: int) -> None:
        raise RuntimeError("init failed")

    with pytest.raises(Exception, match="init failed"):
        await harness.createConversation(
            {"ownership": {"kind": "ownerless"}, "agent": {"thinkingLevel": "high"}, "init": failing}, context
        )
    assert len(storage.commits) == before

    # A conversation on the default selection follows installs live.
    addTool(registry, tool("bash"))

    async def names(conversation: Any) -> list[str]:
        return [each["name"] for each in (await conversation.agent(context))["tools"]]

    assert await names(created) == ["read", "bash"]
    await harness.close(context)


async def test_forks_at_a_concrete_entry_with_the_as_of_agent_and_applies_agent_and_init_overrides():
    opened = await openHarness(MemoryStorage(), ["read"])
    harness, registry = opened.harness, opened.registry
    legacy = tool("legacy")
    addTool(registry, legacy)
    root = await harness.root(context)
    await root.configure({"thinkingLevel": "low"}, context)
    at = await append(root, "one")
    await root.configure({"thinkingLevel": "high", "tools": [tool("read")]}, context)
    await append(root, "two")

    child = await root.fork(at["id"], {"ownership": {"kind": "ownerless"}}, context)
    assert await harness.snapshot(AgentDoc, child.id, context) == {"thinkingLevel": "low"}
    assert await allEntries(child) == ["one"]

    async def init(tx: Any, id: int) -> None:
        assert (await tx.doc(AgentDoc, id))["thinkingLevel"] == "minimal"

    overridden = await root.fork(
        at["id"],
        {"ownership": {"kind": "ownerless"}, "agent": {"thinkingLevel": "minimal", "tools": [legacy]}, "init": init},
        context,
    )
    assert await harness.snapshot(AgentDoc, overridden.id, context) == {"thinkingLevel": "minimal", "tools": ["legacy"]}
    assert await harness.snapshot(AgentDoc, root.id, context) == {"thinkingLevel": "high", "tools": ["read"]}

    unrelated = await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)
    assert await root.fork(at["id"], {"ownership": {"kind": "ownerless"}}, context) is not None
    with pytest.raises(Exception, match="is not visible"):
        await unrelated.fork(at["id"], {"ownership": {"kind": "ownerless"}}, context)
    await harness.close(context)


async def test_paginates_fork_aware_history_through_deep_ancestor_caps_and_same_commit_prefixes():
    opened = await openHarness(MemoryStorage())
    harness = opened.harness
    root = await harness.root(context)
    await append(root, "r1")

    async def two(tx: Any) -> list[dict[str, Any]]:
        return [
            await tx.appendEntry(root.id, {"kind": "message", "model": [user("r2")]}),
            await tx.appendEntry(root.id, {"kind": "message", "model": [user("r3")]}),
        ]

    [r2, _r3] = await root.commit(two, context)
    child = await root.fork(r2["id"], {"ownership": {"kind": "ownerless"}}, context)
    c1 = await append(child, "c1")
    await append(child, "c2")
    grandchild = await child.fork(c1["id"], {"ownership": {"kind": "ownerless"}}, context)
    await append(grandchild, "g1")

    assert await allEntries(root) == ["r3", "r2", "r1"]
    assert await allEntries(child) == ["c2", "c1", "r2", "r1"]
    assert await allEntries(grandchild) == ["g1", "c1", "r2", "r1"]
    # #10546
    assert await allEntries(grandchild, "ascending") == ["r1", "r2", "c1", "g1"]
    bounded = await grandchild.entries(
        {"minEntryId": r2["id"], "maxEntryId": c1["id"], "conversationId": root.id}, 10, None, context
    )
    assert [entry["id"] for entry in bounded["items"]] == [c1["id"], r2["id"]]
    assert Message.is_(bounded["items"][0]) is True
    assert Message.is_(None) is False
    await harness.close(context)


async def test_binds_commits_and_task_creation_to_the_conversation():
    opened = await openHarness(MemoryStorage())
    harness = opened.harness
    conversation = await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)

    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    task = defineTask(
        {
            "name": "test.work",
            "version": 1,
            "initial": lambda input: {"phase": "run"},
            "phases": {"run": run},
            "abort": noAbort,
        }
    )
    taskId = await conversation.commit(
        lambda tx: tx.createTask(task, {"n": 1}, {"ownership": {"kind": "conversation"}}), context
    )
    record = await harness.commit(lambda tx: tx.task(taskId), context)
    assert record["conversationId"] == conversation.id
    with pytest.raises(Exception, match="requires options.conversationId"):
        await harness.commit(lambda tx: tx.createTask(task, {"n": 2}, {"ownership": {"kind": "conversation"}}), context)
    await harness.close(context)


async def test_runs_conversation_created_in_every_creating_commit_after_the_built_ins_and_before_agent_and_init():
    seen: list[str] = []

    async def conversationCreated(tx: Any, conversation: dict[str, Any]) -> None:
        agent = await tx.doc(AgentDoc, conversation["id"])
        cwd = agent["cwd"]
        kind = "new" if conversation.get("parent") is None else "fork"
        seen.append(f"{conversation['id']}:{kind}:{'undefined' if cwd is UNDEFINED else cwd}")
        note = await tx.doc(NoteDoc, conversation["id"])
        if not note["text"]:
            note["text"] = "created"
        if cwd == "/fail":
            raise RuntimeError("no")

    harness = await Harness.open(
        MemoryStorage(),
        {"models": createModels(), "registry": createRegistry(), "conversationCreated": conversationCreated},
        context,
    )

    async def init(tx: Any, id: int) -> None:
        seen.append(f"init:{(await tx.doc(NoteDoc, id))['text']}")

    root = await harness.root(context, {"agent": {"cwd": "/root"}, "init": init})

    # A raw creation in a commit, as in a tool, and a fork, which already has the asOf copies.
    async def createRaw(tx: Any) -> int:
        return (await tx.createConversation({"ownership": {"kind": "ownerless"}}))["id"]  # type: ignore[no-any-return]

    raw = await harness.commit(createRaw, context)
    entry = await append(root, "hello")
    fork = await root.fork(entry["id"], {"ownership": {"kind": "ownerless"}}, context)
    assert seen == [
        f"{root.id}:new:undefined",
        "init:created",
        f"{raw}:new:undefined",
        f"{fork.id}:fork:/root",
    ]
    assert await harness.snapshot(NoteDoc, raw, context) == {"text": "created"}
    # A throw fails the creating commit.
    await root.configure({"cwd": "/fail"}, context)
    assert await root.fork(entry["id"], {"ownership": {"kind": "ownerless"}}, context) is not None
    failing = await append(root, "after")
    with pytest.raises(Exception, match="no"):
        await root.fork(failing["id"], {"ownership": {"kind": "ownerless"}}, context)
    await harness.close(context)


async def test_replaces_whole_fields_clears_them_with_null_and_leaves_undefined_fields_alone():
    opened = await openHarness(MemoryStorage())
    harness, registry = opened.harness, opened.registry
    read = tool("read")
    bash = tool("bash")
    edit = tool("edit")
    registry.install(defineExtension({"name": "coding", "tools": [read, bash, edit]}))
    root = await harness.root(context)

    def stored() -> Any:
        return harness.snapshot(AgentDoc, root.id, context)

    async def offered() -> list[str]:
        return [each["name"] for each in (await root.agent(context))["tools"]]

    toMatchObject(await root.agent(context), {"thinkingLevel": "off", "tools": [read, bash, edit]})
    assert (await root.agent(context)).get("model") is None

    await root.configure({"model": {"provider": "openai", "modelId": "gpt"}, "thinkingLevel": "medium"}, context)
    await root.configure({"model": UNDEFINED, "instructions": "Be terse."}, context)
    assert await stored() == {
        "model": {"provider": "openai", "modelId": "gpt"},
        "thinkingLevel": "medium",
        "instructions": "Be terse.",
    }
    await root.configure({"model": None, "instructions": None}, context)
    assert await stored() == {"thinkingLevel": "medium"}

    await root.configure({"tools": {"remove": [edit]}}, context)
    assert await offered() == ["read", "bash"]
    # A new filter replaces the old one: edit is offered again.
    await root.configure({"tools": {"remove": [bash]}}, context)
    assert await offered() == ["read", "edit"]
    await root.configure({"tools": [edit, read]}, context)
    assert await offered() == ["edit", "read"]
    await root.configure({"tools": None}, context)
    assert await offered() == ["read", "bash", "edit"]
    # Names are stored without checking the registry.
    await root.configure({"tools": [tool("missing"), read]}, context)
    toMatchObject(await stored(), {"tools": ["missing", "read"]})
    assert await offered() == ["read"]
    await harness.close(context)


async def test_gives_conversations_created_through_tx_their_documents_empty_an_owner_copy_or_the_forks_as_of_copy():
    opened = await openHarness(MemoryStorage(), ["read"])
    harness = opened.harness
    root = await harness.root(
        context,
        {"agent": {"model": {"provider": "faux", "modelId": "m"}, "instructions": "Main role.", "cwd": "/repo"}},
    )

    async def never(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    owner = defineTask(
        {
            "name": "test.owner",
            "version": 1,
            "initial": lambda input: {"phase": "never"},
            "phases": {"never": never},
            "abort": noAbort,
        }
    )

    async def create(tx: Any) -> dict[str, int]:
        taskId = await tx.createTask(owner, {}, {"ownership": {"kind": "conversation"}})
        plain = await tx.createConversation({"ownership": {"kind": "ownerless"}})
        owned = await tx.createConversation({"ownership": {"kind": "task", "taskId": taskId}})
        # The copy exists when createConversation() returns, so a configure() in the same callback overrides it.
        assert (await tx.doc(AgentDoc, owned["id"]))["cwd"] == "/repo"
        await configure(tx, owned["id"], {"cwd": "/worktree"})
        return {"taskId": taskId, "plain": plain["id"], "owned": owned["id"]}

    ids = await root.commit(create, context)
    assert await harness.snapshot(AgentDoc, ids["plain"], context) == {}
    assert await harness.snapshot(LiveDoc, ids["plain"], context) == {}
    plainProvider = await harness.snapshot(ProviderDoc, ids["plain"], context)
    assert list(plainProvider) == ["sessionId"]
    assert UUID_V7.match(plainProvider["sessionId"])
    assert (await harness.snapshot(ProviderDoc, ids["owned"], context))["sessionId"] != (
        await harness.snapshot(ProviderDoc, root.id, context)
    )["sessionId"]
    assert await harness.snapshot(AgentDoc, ids["owned"], context) == {
        "model": {"provider": "faux", "modelId": "m"},
        "instructions": "Main role.",
        "cwd": "/worktree",
    }

    # A later owner change does not reach the child.
    await root.configure({"thinkingLevel": "high"}, context)
    assert "thinkingLevel" not in (await harness.snapshot(AgentDoc, ids["owned"], context))

    # A task-owned fork keeps its as-of copy of its fork parent, not its owner's agent.
    async def appendNote(tx: Any) -> int:
        return (await tx.appendEntry(ids["plain"], {"kind": "note"}))["id"]  # type: ignore[no-any-return]

    at = await root.commit(appendNote, context)

    async def forkOwned(tx: Any) -> int:
        forked = await tx.forkConversation(ids["plain"], at, {"ownership": {"kind": "task", "taskId": ids["taskId"]}})
        return forked["id"]  # type: ignore[no-any-return]

    fork = await root.commit(forkOwned, context)
    assert await harness.snapshot(AgentDoc, fork, context) == {}
    assert await harness.snapshot(LiveDoc, fork, context) == {}
    assert (await harness.snapshot(ProviderDoc, fork, context))["sessionId"] != (
        await harness.snapshot(ProviderDoc, ids["plain"], context)
    )["sessionId"]
    await harness.close(context)


async def test_reads_an_absent_agent_for_conversations_a_plain_session_created_without_writing():
    storage = ControlledStorage()

    async def createPlain(tx: Any) -> int:
        return (await tx.createConversation({"ownership": {"kind": "ownerless"}}))["id"]  # type: ignore[no-any-return]

    id = await createSession(storage).commit(createPlain, context)
    opened = await openHarness(storage, ["read"])
    harness = opened.harness
    raw = await harness.conversation(id, context)
    assert await harness.snapshot(LiveDoc, id, context) is None
    commits = len(storage.commits)
    toMatchObject(await raw.agent(context), {"thinkingLevel": "off", "tools": [{"name": "read"}]})
    assert len(storage.commits) == commits
    await raw.configure({"thinkingLevel": "low"}, context)
    assert await harness.snapshot(AgentDoc, id, context) == {"thinkingLevel": "low"}
    await harness.close(context)


async def test_returns_stateless_handles_and_rejects_operations_after_close():
    opened = await openHarness(MemoryStorage())
    harness = opened.harness
    root = await harness.root(context)
    again = await harness.root(context)
    assert again is not root
    assert again.id == root.id
    await harness.close(context)
    with pytest.raises(Exception, match="is closed"):
        await harness.root(context)
    with pytest.raises(Exception, match="is closed"):
        await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)
    with pytest.raises(Exception, match="is closed"):
        await harness.conversation(root.id, context)


async def test_forwards_generic_session_document_apis():
    opened = await openHarness(MemoryStorage())
    harness = opened.harness
    root = await harness.root(context)

    async def first(tx: Any) -> dict[str, Any]:
        (await tx.doc(NoteDoc, root.id))["text"] = "first"
        return await tx.appendEntry(root.id, {"kind": "message", "model": [user("m")]})  # type: ignore[no-any-return]

    entry = await root.commit(first, context)

    async def second(tx: Any) -> None:
        (await tx.doc(NoteDoc, root.id))["text"] = "second"

    await harness.commit(second, context)
    assert await harness.snapshot(NoteDoc, root.id, context) == {"text": "second"}
    assert await harness.snapshotAsOf(NoteDoc, root.id, entry["id"], context) == {"text": "first"}
    state = await harness.documentState(NoteDoc, root.id, context)
    assert state.value == {"text": "second"}
    state.dispose()
    await harness.close(context)

"""pi durable's ``test/spec-usage.test.ts``.

The usage examples of docs/spec.md. Upstream compile-checks them: ``examples()`` is never called, only type-checked. Python
has no compile step, so ``examples()`` is called here, which builds every extension, task and document definition the spec
shows; the sequences that need a live Session or Harness are returned, not run. Each block is copied as written apart from
formatting, with the names the spec leaves to the application declared below. Keep the two in sync; ``test_durable_examples``
runs the same patterns end to end.
"""

from __future__ import annotations

import os
import time
from typing import Any

from misaka.durable import (
    GenerationTask,
    Harness,
    LiveDoc,
    ToolTask,
    configure,
    createRegistry,
    defineDoc,
    defineExtension,
    defineTask,
    defineTool,
    hook,
    section,
    wrapTool,
)
from misaka.durable.tools import (
    CodingTools,
    createBashTool,
    createEditTool,
    createReadTool,
)

# ─── Names the spec leaves to the application ────────────────────────────────

context: Any = None
storage: Any = None
models: Any = None
session: Any = None
conversationId: int = 0
message: dict[str, Any] = {"kind": "note"}
haiku: dict[str, str] = {"provider": "anthropic", "modelId": "haiku"}
sonnet: dict[str, str] = {"provider": "anthropic", "modelId": "sonnet"}
worktree: str = "/work/tree"
name: str = "reviewer"


def localEnv(cwd: str) -> Any:
    raise NotImplementedError


class containers:
    @staticmethod
    async def env(image: str, cwd: str, context: Any) -> Any:
        raise NotImplementedError


def renderAgentsMd(*_: Any) -> str | None:
    raise NotImplementedError


def renderSkills(*_: Any) -> str | None:
    raise NotImplementedError


def isDangerous(call: dict[str, Any]) -> bool:
    raise NotImplementedError


def writes(call: dict[str, Any]) -> bool:
    raise NotImplementedError


async def requestSecondPass(answer: dict[str, Any], api: Any, context: Any) -> dict[str, Any] | None:
    raise NotImplementedError


class metrics:
    @staticmethod
    def record(name: str, ms: int) -> None:
        raise NotImplementedError


async def answerText(api: Any, entry: int, context: Any) -> str:
    raise NotImplementedError


class payments:
    @staticmethod
    async def charge(key: str) -> dict[str, str]:
        raise NotImplementedError

    @staticmethod
    async def cancel(checkpoint: dict[str, Any]) -> None:
        raise NotImplementedError


def newKey() -> str:
    raise NotImplementedError


def receiptEntry(receipt: dict[str, str]) -> dict[str, Any]:
    raise NotImplementedError


class SettingsManager:
    """The application's settings: ``get("timeoutMs")``, ``get("autoCompact")`` and ``set("autoCompact", value)``."""

    def get(self, key: str) -> Any:
        raise NotImplementedError

    def set(self, key: str, value: Any) -> None:
        raise NotImplementedError


manager = SettingsManager()


async def noPhase(_task: Any, runtime: Any, ctx: Any) -> None:
    return None


Anchor = defineTask(
    {
        "name": "app.anchor",
        "version": 1,
        "initial": lambda input: {"phase": "hold"},
        "phases": {"hold": noPhase},
        "abort": noPhase,
    }
)
Reporter = defineTask(
    {
        "name": "app.reporter",
        "version": 1,
        "initial": lambda input: {"phase": "report"},
        "phases": {"report": noPhase},
        "abort": noPhase,
    }
)
subagentTool = createReadTool()


def examples() -> dict[str, Any]:
    """Never run end to end: the declared names above have no values."""
    readTool = createReadTool()
    editTool = createEditTool()
    bashTool = createBashTool()

    # ─── Section 7.1: extensions and host setup ──────────────────────────────

    ContextFiles = defineExtension({"name": "context-files", "sections": [section("agents-md", renderAgentsMd)]})
    Skills = defineExtension({"name": "skills", "sections": [section("skills", renderSkills)]})
    SkillsV2 = defineExtension({"name": "skills", "sections": [section("skills", renderSkills)]})
    Coding = defineExtension(
        {
            "name": "coding",
            "sections": [
                section("preamble", lambda *_: "You are an expert coding assistant.", {"tag": False}),
                # The environment the host built for this conversation, in the conversation's directory.
                section(
                    "cwd",
                    lambda input, *_: None if input["env"] is None else f"Working directory: {input['env'].cwd}",
                ),
            ],
        }
    )

    async def requireApproval(call: dict[str, Any], *_: Any) -> dict[str, str] | None:
        return {"block": "Needs approval"} if isDangerous(call) else None

    Permissions = defineExtension({"name": "permissions", "hooks": [hook(ToolTask, {"beforeTool": requireApproval})]})
    # A role and a review loop, for conversations that select it.
    Reviewer = defineExtension(
        {
            "name": "reviewer",
            "sections": [section("role", lambda *_: "You review diffs. Report problems as a list. Never edit files.")],
            "hooks": [hook(GenerationTask, {"onYield": requestSecondPass})],
        }
    )

    def timed(tool: dict[str, Any]) -> dict[str, Any]:
        async def execute(args: Any, api: Any, context: Any) -> Any:
            start = time.time()
            try:
                return await tool["execute"](args, api, context)
            finally:
                metrics.record("bash", int((time.time() - start) * 1000))

        return {**tool, "execute": execute}

    Timing = defineExtension({"name": "timing", "wraps": [wrapTool(bashTool, timed)]})
    # A bash inside a Python virtualenv for one conversation: it replaces CodingTools' bash in place,
    # and Timing, if selected, wraps it.
    Venv = defineExtension({"name": "venv", "tools": [createBashTool({"commandPrefix": "source .venv/bin/activate"})]})

    # ─── Section 7.2: hooks reading extension state ──────────────────────────

    PlanModeDoc = defineDoc(
        {
            "kind": "app.plan-mode",
            "version": 1,
            "scope": "conversation",
            "history": "latest",
            "fork": "current",
            "initial": lambda: {"enabled": False},
        }
    )

    async def readOnlyInPlanMode(call: dict[str, Any], api: Any, context: Any) -> dict[str, str] | None:
        # An absent document means plan mode is off.
        planMode = await api.snapshot(PlanModeDoc, api.conversationId, context)
        return {"block": "Plan mode: read-only"} if planMode is not None and planMode["enabled"] and writes(call) else None

    PlanMode = defineExtension({"name": "plan-mode", "hooks": [hook(ToolTask, {"beforeTool": readOnlyInPlanMode})]})

    # ─── Section 7.3: subagents ──────────────────────────────────────────────

    async def delegate(args: Any, api: Any, context: Any) -> dict[str, Any]:
        task = args["task"]

        async def spawnChild(tx: Any) -> int:
            existing = (await tx.scanConversations({"ownerTaskId": api.taskId}, 1))["items"]
            if existing:
                return existing[0]["id"]  # type: ignore[no-any-return]
            # Starts as a copy of this conversation's agent: model, thinking level, cwd, extensions, tools.
            created = await tx.createConversation({"ownership": {"kind": "task", "taskId": api.taskId}})
            # Without this extension, the child is not offered this tool.
            await configure(tx, created["id"], {"extensions": {"remove": [Subagent]}})
            return created["id"]  # type: ignore[no-any-return]

        child = await api.commit(spawnChild, context)
        await api.details({"conversationId": child}, context)
        handle = await api.conversation(child, context)
        request = {"type": "input", "content": task, "requestId": f"subagent:{api.taskId}"}
        settled = await (await handle.submit(request, context)).wait(context)
        if settled["status"] != "done" or settled["type"] != "input":
            raise RuntimeError(f"Subagent failed: {settled['status']}")
        return {"output": [{"type": "text", "text": await answerText(api, settled["answer"], context)}]}

    Subagent = defineExtension(
        {
            "name": "subagent",
            "tools": [
                defineTool(
                    {
                        "name": "subagent",
                        "description": "Delegate a self-contained task to a subagent and get its answer back.",
                        "parameters": {
                            "type": "object",
                            "properties": {"task": {"type": "string"}},
                            "required": ["task"],
                        },
                        "replay": "safe",
                        "execute": delegate,
                    }
                )
            ],
        }
    )

    SubagentTools = defineExtension({"name": "subagent-tools", "tasks": [Anchor, Reporter], "tools": [subagentTool]})

    # ─── Section 7.4 ─────────────────────────────────────────────────────────

    Chat = defineExtension(
        {"name": "chat", "sections": [section("preamble", lambda *_: "You are a helpful assistant.", {"tag": False})]}
    )

    # ─── Section 5.1: a task with intent, effect, outcome ────────────────────

    async def prepare(_task: Any, runtime: Any, context: Any) -> None:
        # Intent, effect, outcome.
        await runtime.commit(
            lambda tx, current: {"status": "running", "checkpoint": {"phase": "charge", "key": newKey()}}, context
        )

    async def charge(task: Any, runtime: Any, context: Any) -> None:
        receipt = await payments.charge(task["state"]["checkpoint"]["key"])  # idempotent by key

        async def finish(tx: Any, current: Any) -> dict[str, Any]:
            entry = await tx.appendEntry(current["conversationId"], receiptEntry(receipt))
            return {"status": "terminal", "outcome": {"status": "completed", "result": {"entryId": entry["id"]}}}

        await runtime.commit(finish, context)

    # The abort handler decides the outcome; returning without one faults the task.
    async def cancel(task: Any, runtime: Any, context: Any) -> None:
        await payments.cancel(task["state"]["checkpoint"])
        await runtime.commit(
            lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted", "reason": "user"}}, context
        )

    Payment = defineTask(
        {
            "name": "app.payment",
            "version": 1,
            "initial": lambda input: {"phase": "prepare"},
            "phases": {"prepare": prepare, "charge": charge},
            "abort": cancel,
        }
    )

    Follow = defineTask(
        {
            "name": "app.follow",
            "version": 1,
            "initial": lambda input: {"phase": "follow"},
            "phases": {"follow": noPhase},
            "abort": noPhase,
        }
    )

    # ─── Sequences ───────────────────────────────────────────────────────────

    # Section 2.2: a tool's commit creates a configured child.
    async def childInToolCommit(tx: Any, api: Any) -> None:
        # In a tool's commit. The child starts as a copy of this conversation's agent: model, extensions, tools, cwd.
        child = await tx.createConversation({"ownership": {"kind": "task", "taskId": api.taskId}})
        # A cheaper model, only the read tool, and its own worktree; everything else stays as copied.
        await configure(tx, child["id"], {"model": haiku, "tools": [readTool], "cwd": worktree})

    # Section 2.2: settings read live through getters.
    def liveSettings() -> Any:
        # The user's settings, read live through getters.
        class UserSettings(dict[str, Any]):
            def __init__(self, manager: SettingsManager) -> None:
                super().__init__()
                self.manager = manager

            def get(self, key: str, default: Any = None) -> Any:
                if key == "stream":
                    return {"timeoutMs": self.manager.get("timeoutMs")}
                if key == "compaction":
                    return {"enabled": self.manager.get("autoCompact")}
                return super().get(key, default)

        manager.set("autoCompact", False)  # no Session write; every conversation follows at its next threshold check
        return UserSettings(manager)

    # Section 2.2: an environment per conversation.
    async def containerEnv() -> Any:
        registry = createRegistry()
        # Absent: the conversation runs locally. Only conversations with this document run in a container.
        # Subagents do not copy it: their creator writes it too when they should run in the container.
        ContainerDoc = defineDoc(
            {
                "kind": "app.container",
                "version": 1,
                "scope": "conversation",
                "history": "latest",
                "fork": "current",
                "initial": lambda: {"image": "node:22"},
            }
        )

        async def env(target: dict[str, Any], context: Any) -> Any:
            cwd = target.get("cwd")
            container = await target["read"].snapshot(ContainerDoc, target["conversationId"], context)
            if container is not None:
                return await containers.env(container["image"], cwd if cwd is not None else "/work", context)
            return localEnv(cwd if cwd is not None else os.getcwd())  # cached NodeExecutionEnv per directory

        return await Harness.open(storage, {"models": models, "registry": registry, "env": env}, context)

    # Sections 2.2 and 7.1: host setup, tool filters, extension selection, plan mode, and reload.
    async def host() -> Any:
        registry = createRegistry()
        for extension in (CodingTools, Coding, ContextFiles, Skills, Permissions, Reviewer):
            registry.install(extension)
        harness = await Harness.open(
            storage,
            {
                "models": models,
                "registry": registry,
                # Reviewer is installed but not selected by default: only conversations that select it get its role
                # and hooks.
                "settings": {"extensions": [CodingTools, Coding, ContextFiles, Skills, Permissions]},
                "env": lambda target, context: localEnv(target.get("cwd") or os.getcwd()),  # cached per directory
            },
            context,
        )
        # The conversation remembers its model and directory; a restart elsewhere keeps both.
        root = await harness.root(context, {"agent": {"model": sonnet, "cwd": os.getcwd()}})

        await root.configure({"tools": {"remove": [editTool]}}, context)
        await root.configure({"tools": {"remove": [bashTool]}}, context)  # edit is offered again
        await root.configure({"tools": None}, context)  # every tool of the selected extensions again

        registry.install(Timing)
        registry.install(Venv)  # installed, but not in the default selection
        conversation = root
        await conversation.configure({"extensions": {"add": [Venv]}}, context)

        registry.install(PlanMode)

        # PlanMode is in the default selection; /plan toggles this conversation's state.
        async def enablePlanMode(tx: Any) -> None:
            (await tx.doc(PlanModeDoc, root.id))["enabled"] = True

        await root.commit(enablePlanMode, context)

        registry.install(Subagent)
        registry.install(Chat)
        registry.install(SkillsV2)  # same name: conversations selecting skills render v2 at their next request
        registry.uninstall(Skills)  # selecting conversations get a system delta removing its section; nothing is rewritten
        # Restart: the host installs its extensions again; stored names resolve against them.
        return harness

    # Section 7.3: a named subagent's spawn commit.
    async def spawn(tx: Any) -> None:
        # In subagentTool's spawn commit, after the name checks:
        anchor = await tx.createTask(Anchor, None, {"ownership": {"kind": "conversation"}, "background": True})
        # Owned by a task of the parent: starts as a copy of the parent's agent.
        child = await tx.createConversation({"ownership": {"kind": "task", "taskId": anchor}})
        await configure(
            tx,
            child["id"],
            {
                "extensions": {"remove": [SubagentTools]},
                "instructions": f'You are the subagent "{name}". Answer the main agent\'s requests.',
            },
        )

    # Section 4: table reads before the first table write; documents stay usable.
    async def tableRules() -> None:
        async def change(tx: Any) -> None:
            conversation = await tx.conversation(conversationId)  # table read
            live = await tx.doc(LiveDoc, conversationId)

            await tx.appendEntry(conversationId, message)  # first table write
            del live["generation"]  # document mutation remains valid
            await tx.createTask(
                Follow, {}, {"ownership": {"kind": "conversation"}, "conversationId": conversationId}
            )  # further table writes are fine
            del conversation

        await session.commit(change, context)

    # Section 3.4: drafts are revoked after their commit.
    async def revokedDraft() -> None:
        escaped: dict[str, Any] = {}

        async def change(tx: Any) -> None:
            escaped["draft"] = await tx.doc(LiveDoc, conversationId)

        await session.commit(change, context)
        escaped["draft"]["generation"] = None  # throws: the draft was revoked

    sequences = {
        "childInToolCommit": childInToolCommit,
        "liveSettings": liveSettings,
        "containerEnv": containerEnv,
        "host": host,
        "spawn": spawn,
        "tableRules": tableRules,
        "revokedDraft": revokedDraft,
    }
    return {"sequences": sequences, "Payment": Payment}


def test_compiles_the_specs_usage_examples():
    built = examples()
    assert "sequences" in built
    assert built["Payment"]["definition"]["name"] == "app.payment"
    assert sorted(built["sequences"]) == [
        "childInToolCommit",
        "containerEnv",
        "host",
        "liveSettings",
        "revokedDraft",
        "spawn",
        "tableRules",
    ]
    assert callable(examples)

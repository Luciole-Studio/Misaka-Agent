"""pi durable's ``test/harness-registry.test.ts``."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.durable import (
    CompactionTask,
    GenerationTask,
    ToolTask,
    createRegistry,
    defineExtension,
    defineTask,
    hook,
    section,
    wrapSection,
    wrapTool,
)
from misaka.durable.harness.agent import agentHooks, resolveAgent, resolveSettings
from misaka.utils.js import UNDEFINED
from tests.durable_support import context


def tool(name: str, **extra: Any) -> dict[str, Any]:
    async def execute(args: Any, api: Any, ctx: Any) -> dict[str, Any]:
        return {"output": []}

    return {
        "name": name,
        "description": f"{name} tool",
        "parameters": {"type": "object", "properties": {}},
        "execute": execute,
        **extra,
    }


def task(name: str, version: int = 1) -> dict[str, Any]:
    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    return defineTask(
        {
            "name": name,
            "version": version,
            "initial": lambda input: {"phase": "run"},
            "phases": {"run": run},
            "abort": abort,
        }
    )


def names(items: list[dict[str, Any]]) -> list[str]:
    return [item["name"] for item in items]


def resolve(state: dict[str, Any] | None, snapshot: Any, settings: dict[str, Any] | None = None, reports: list[Any] | None = None):
    """Resolve ``state`` against ``snapshot`` and ``settings``, collecting reports."""
    collected = reports if reports is not None else []
    return resolveAgent(state, snapshot, resolveSettings(settings or {}), collected.append)


async def rendered(agent: dict[str, Any]) -> list[list[Any]]:
    async def snapshot(*args: Any) -> None:
        return None

    promptInput = {
        "conversationId": 1,
        "agent": agent,
        "env": None,
        "shown": {},
        "read": SimpleNamespace(snapshot=snapshot, snapshotAsOf=snapshot),
    }
    result = []
    for item in agent["sections"]:
        text = item["render"](promptInput, context)
        if inspect.isawaitable(text):
            text = await text
        result.append([item["key"], text])
    return result


def test_installs_replaces_in_place_and_uninstalls_extensions_by_name():
    registry = createRegistry()
    listener: list[str] = []
    registry.subscribe(lambda: listener.append(",".join(names(registry.snapshot().installed()))))
    a = defineExtension({"name": "a", "tools": [tool("read", snippet="Read files")]})
    b = defineExtension({"name": "b", "tools": [tool("read"), tool("bash")]})
    registry.install(a)
    registry.install(b)
    before = registry.snapshot()
    # A new object with an installed name replaces it at its position.
    a2 = defineExtension({"name": "a", "tools": [tool("grep")]})
    registry.install(a2)
    assert names(registry.snapshot().installed()) == ["a", "b"]
    assert registry.snapshot().extension("a") is a2
    assert [f"{entry['tool']['name']}@{entry['extension']['name']}" for entry in registry.snapshot().tools()] == [
        "grep@a",
        "read@b",
        "bash@b",
    ]
    # Old snapshots stay as they were.
    assert before.extension("a") is a
    assert before.tools()[0]["tool"]["snippet"] == "Read files"
    # Uninstall matches the name, whichever object; a later install appends.
    registry.uninstall(a)
    registry.uninstall(a)
    assert names(registry.snapshot().installed()) == ["b"]
    registry.install(a)
    assert names(registry.snapshot().installed()) == ["b", "a"]
    assert listener == ["a", "a,b", "a,b", "b", "b,a"]


def test_validates_the_registry_as_it_would_be_after_an_install_and_publishes_nothing_when_invalid():
    registry = createRegistry()
    registry.install(defineExtension({"name": "tasks", "tasks": [task("app.index")]}))
    published: list[int] = []
    registry.subscribe(lambda: published.append(1))
    before = registry.snapshot()
    invalid = [
        (defineExtension({"name": "x", "tools": [tool("read"), tool("read")]}), "two tools named read"),
        (
            defineExtension({"name": "x", "sections": [section("a", lambda *_: "1"), section("a", lambda *_: "2")]}),
            "two sections",
        ),
        (defineExtension({"name": "x", "sections": [section("Bad Key", lambda *_: "")]}), "must match"),
        (defineExtension({"name": "x", "sections": [section("instructions", lambda *_: "")]}), "reserved"),
        (defineExtension({"name": "x", "tasks": [task("pi.generation")]}), "already installed"),
        (defineExtension({"name": "x", "tasks": [task("app.index")]}), "already installed"),
    ]
    for extension, message in invalid:
        with pytest.raises(Exception, match=message):
            registry.install(extension)
    assert registry.snapshot() is before
    assert published == []
    # Replacing the extension that holds a task name is valid: the check runs on the state after replacement.
    registry.install(defineExtension({"name": "tasks", "tasks": [task("app.index", 2)]}))
    assert registry.snapshot().task("app.index")["definition"]["version"] == 2


def test_always_holds_the_built_in_tasks_which_are_not_an_extension():
    registry = createRegistry()
    assert registry.snapshot().installed() == []
    assert registry.snapshot().tasks() == [GenerationTask, ToolTask, CompactionTask]
    custom = task("app.custom")
    registry.install(defineExtension({"name": "custom", "tasks": [custom]}))
    assert registry.snapshot().tasks() == [GenerationTask, ToolTask, CompactionTask, custom]
    assert registry.snapshot().task("app.custom") is custom
    registry.uninstall(defineExtension({"name": "custom"}))
    assert registry.snapshot().task("app.custom") is None


def test_settings_keeps_the_default_of_a_progress_interval_given_as_undefined():
    assert resolveSettings({"progress": {"partialIntervalMs": UNDEFINED, "outputIntervalMs": 250}})["progress"] == {
        "partialIntervalMs": 100,
        "outputIntervalMs": 250,
    }


read = tool("read")
bash = tool("bash")
edit = tool("edit")
Coding = defineExtension(
    {
        "name": "coding",
        "tools": [read, bash, edit],
        "sections": [section("preamble", lambda *_: "You code.", {"tag": False}), section("cwd", lambda *_: "/repo")],
    }
)
Skills = defineExtension({"name": "skills", "sections": [section("skills", lambda *_: "S")]})
Reviewer = defineExtension({"name": "reviewer", "sections": [section("role", lambda *_: "Review.")]})


def agentRegistry() -> Any:
    registry = createRegistry()
    for extension in [Coding, Skills, Reviewer]:
        registry.install(extension)
    return registry


def test_selects_the_default_an_array_or_the_default_edited_by_add_and_remove():
    snapshot = agentRegistry().snapshot()
    assert names(resolve(None, snapshot)["extensions"]) == ["coding", "skills", "reviewer"]
    settings = {"extensions": [Coding, Skills]}
    assert names(resolve({}, snapshot, settings)["extensions"]) == ["coding", "skills"]
    assert names(resolve({"extensions": ["reviewer", "coding"]}, snapshot, settings)["extensions"]) == [
        "reviewer",
        "coding",
    ]
    # Add appends, remove drops, duplicates keep their first position, uninstalled names are skipped.
    edited = {"extensions": {"add": ["reviewer", "coding", "gone"], "remove": ["skills"]}}
    assert names(resolve(edited, snapshot, settings)["extensions"]) == ["coding", "reviewer"]
    # An old object stands for its name: the installed extension is selected.
    stale = {"extensions": [defineExtension({"name": "skills"}), Skills]}
    assert resolve({}, snapshot, stale)["extensions"] == [Skills]


def test_skips_uninstalled_names_and_resolves_them_again_once_they_are_installed():
    local = createRegistry()
    local.install(Coding)
    state = {"extensions": ["coding", "skills"]}
    assert names(resolve(state, local.snapshot())["extensions"]) == ["coding"]
    local.install(Skills)
    assert names(resolve(state, local.snapshot())["extensions"]) == ["coding", "skills"]


def test_replaces_same_name_tools_in_place_wraps_the_winner_then_applies_the_filter():
    local = createRegistry()
    venvBash = tool("bash", description="venv bash")
    calls: list[str] = []
    Venv = defineExtension({"name": "venv", "tools": [venvBash]})

    def grep(inner: dict[str, Any]) -> dict[str, Any]:
        calls.append("grep")
        return tool("grep")

    Timing = defineExtension(
        {
            "name": "timing",
            "wraps": [
                wrapTool(bash, lambda inner: {**inner, "description": f"{inner['description']} (timed)"}),
                wrapTool(bash, lambda inner: {**inner, "description": f"{inner['description']} [2]"}),
                # No ``grep`` is selected: the wrapper does nothing and reports nothing.
                wrapTool(tool("grep"), grep),
            ],
        }
    )
    for extension in [Coding, Venv, Timing]:
        local.install(extension)
    reports: list[Any] = []
    agent = resolve(None, local.snapshot(), {}, reports)
    assert [[each["name"], each["description"]] for each in agent["tools"]] == [
        ["read", "read tool"],
        ["bash", "venv bash (timed) [2]"],
        ["edit", "edit tool"],
    ]
    assert reports == []
    assert calls == []

    # An array keeps exactly these names in its order, a repeated name at its first position.
    filtered = resolve({"tools": ["edit", "missing", "read", "edit"]}, local.snapshot())
    assert names(filtered["tools"]) == ["edit", "read"]
    assert names(resolve({"tools": {"remove": ["bash"]}}, local.snapshot())["tools"]) == ["read", "edit"]


def test_drops_a_tool_or_section_whose_wrapper_throws_or_renames_it_and_reports_the_failure():
    local = createRegistry()

    def failingTool(inner: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("wrapper failed")

    def failingSection(inner: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("section wrapper failed")

    Broken = defineExtension(
        {
            "name": "broken",
            "wraps": [
                wrapTool(read, failingTool),
                wrapTool(edit, lambda inner: {**inner, "name": "renamed"}),
                wrapSection("cwd", failingSection),
            ],
        }
    )
    local.install(Coding)
    local.install(Broken)
    reports: list[Any] = []
    agent = resolve(None, local.snapshot(), {}, reports)
    assert names(agent["tools"]) == ["bash"]
    assert [each["key"] for each in agent["sections"]] == ["preamble"]
    assert [str(error) for error in reports] == [
        "wrapper failed",
        "Wrapper renamed edit to renamed",
        "section wrapper failed",
    ]


async def test_orders_sections_by_extension_replaces_same_keys_in_place_and_renders_instructions_last_and_unwrapped():
    local = createRegistry()

    def wrapCwd(inner: dict[str, Any]) -> dict[str, Any]:
        async def render(promptInput: Any, ctx: Any) -> str:
            text = inner["render"](promptInput, ctx)
            if inspect.isawaitable(text):
                text = await text
            return f"{text}!"

        return {**inner, "render": render}

    def neverWrapped(inner: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("never")

    Override = defineExtension(
        {
            "name": "override",
            "sections": [section("preamble", lambda *_: "You review.", {"tag": False})],
            "wraps": [
                wrapSection("cwd", wrapCwd),
                # Instructions are not wrapped.
                wrapSection("instructions", neverWrapped),
            ],
        }
    )
    for extension in [Coding, Skills, Override]:
        local.install(extension)
    reports: list[Any] = []
    agent = resolve({"instructions": "Be terse."}, local.snapshot(), {}, reports)
    assert await rendered(agent) == [
        ["preamble", "You review."],
        ["cwd", "/repo!"],
        ["skills", "S"],
        ["instructions", "Be terse."],
    ]
    assert agent["sections"][-1].get("tag") is None
    assert reports == []


def test_collects_hooks_of_the_selected_extensions_in_extension_order_and_applies_field_defaults():
    local = createRegistry()
    first = {"beforeTool": lambda *_: None}
    second = {"beforeTool": lambda *_: None}
    onYield = {"onYield": lambda *_: None}
    local.install(defineExtension({"name": "a", "hooks": [hook(ToolTask, first), hook(GenerationTask, onYield)]}))
    local.install(defineExtension({"name": "b", "hooks": [hook(ToolTask, second)]}))
    assert agentHooks(resolve(None, local.snapshot()), "pi.tool") == [first, second]
    assert agentHooks(resolve({"extensions": ["b", "a"]}, local.snapshot()), "pi.tool") == [second, first]
    assert agentHooks(resolve({"extensions": ["b"]}, local.snapshot()), "pi.generation") == []

    defaults = resolve(None, local.snapshot())
    assert defaults.get("model") is None
    assert defaults["thinkingLevel"] == "off"
    assert defaults.get("cwd") is None
    configured = resolve(
        {"model": {"provider": "p", "modelId": "m"}, "thinkingLevel": "high", "cwd": "/w"}, local.snapshot()
    )
    assert configured["model"] == {"provider": "p", "modelId": "m"}
    assert configured["thinkingLevel"] == "high"
    assert configured["cwd"] == "/w"

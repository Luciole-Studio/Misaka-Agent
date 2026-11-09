"""pi chord's ``test/tutorial.test.ts``: the runnable sources of ``misaka/chord/docs/tutorial.md``.

Every printed example of the tutorial is a region of this file, between an ``example:NAME`` and an
``end-example:NAME`` comment; ``checkExamples`` (upstream's) compares each printed copy with its region (whitespace at
the start and end of lines aside), so changing either without the other fails a test. The tutorial is upstream's, with
its examples in Python.

MISAKA adaptations: ``Promise.withResolvers<void>()`` is an ``asyncio.Event``; ``{ name, ...defineFacet(...) }`` is
``Facet(name=..., **vars(defineFacet(...)))``; object spreads and rest destructuring are dict merges, comprehensions and
``pop``; a mutating ``change`` callback returns ``None`` (MISAKA fork of ``Cell.change``); ``assert.rejects`` is
``pytest.raises``; ``onTestFinished(() => host.close())`` is the ``onTestFinished`` fixture, whose callbacks run after
the test as vitest's do (last registered first).
"""

from __future__ import annotations

# example:imports
import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

import pytest

from misaka.chord import (
    ChordError,
    Ctx,
    Facet,
    FacetApi,
    Factory,
    Host,
    StateOptions,
    createHost,
    defineFacet,
    defineRegistry,
    forbidden,
    hostNamed,
    invalid,
    isChordError,
    local,
    memoryLinkPair,
    method,
    opaque,
    remote,
)
from misaka.chord._typebox import Type

# end-example:imports

# Compare every printed example with its runnable source, not a second handwritten copy.
tutorial = (Path(__file__).resolve().parents[1] / "misaka" / "chord" / "docs" / "tutorial.md").read_text(encoding="utf-8")
source = Path(__file__).read_text(encoding="utf-8")
examples = list(re.finditer(r"^[\t ]*# example:([\w-]+)\n([\s\S]*?)^[\t ]*# end-example:\1$", source, re.MULTILINE))
printed = list(re.finditer(r"<!-- example:([\w-]+) -->\n```python\n([\s\S]*?)\n```", tutorial))


def normalize(text: str) -> str:
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def checkExamples(section: str) -> None:
    assert len(printed) == len(examples), "every tutorial example must have runnable source"
    assert len(re.findall(r"```python\n", tutorial)) == len(printed), "no untracked Python examples"
    for match in [entry for entry in examples if entry[1] == "imports" or entry[1].startswith(section)]:
        copies = [entry for entry in printed if entry[1] == match[1]]
        assert len(copies) == 1, f"one printed copy of {match[1]}"
        assert normalize(copies[0][2]) == normalize(match[2]), f"tutorial drift: {match[1]}"


@pytest.fixture
async def onTestFinished():
    """vitest's ``onTestFinished``: callbacks (returning awaitables) that run after the test, last registered first. A
    callback naming a variable the test never assigned (it failed before) is skipped."""
    callbacks: list[Callable[[], Awaitable[Any]]] = []
    yield callbacks.append
    for callback in reversed(callbacks):
        try:
            awaitable = callback()
        except NameError:
            continue
        await awaitable


# example:02-contract
TodoLabel = local(
    "tutorial.todos",
    {
        "version": 1,
        "input": Type.Object({"list": Type.String()}),
        "members": {"label": method(None, Type.String())},
    },
)
# end-example:02-contract

# example:03-contract
Todo = Type.Object({"text": Type.String(), "done": Type.Boolean()})
Rows = Type.Array(Todo)
TodoStore = local(
    "tutorial.todos",
    {
        "version": 1,
        "input": Type.Object({"list": Type.String()}),
        "members": {
            "label": method(None, Type.String()),
            "rows": Rows,
            "openCount": Type.Number(),
            "add": method(Type.Object({"text": Type.String()})),
            "finish": method(Type.Object({"index": Type.Integer()})),
            "reset": method(Rows),
        },
    },
)
# end-example:03-contract


# example:03-factory
def todoStore(options: StateOptions | None = None) -> Factory:
    """The store's factory; ``options`` versions its rows (section 9)."""

    def factory(input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        list_ = input["list"]
        rows = self.state("rows", Rows, [], options)
        openCount = self.derive([rows], lambda todos: len([todo for todo in todos if not todo["done"]]))

        def add(args: Any, ctx: Ctx) -> None:
            def push(draft: Any) -> None:
                draft.push({"text": args["text"], "done": False})

            rows.change(ctx, push)

        def finish(args: Any, ctx: Ctx) -> None:
            def markDone(draft: Any) -> None:
                todo = draft[args["index"]]
                if not todo:
                    raise invalid("args", "No such todo")
                todo["done"] = True

            rows.change(ctx, markDone)

        def reset(next: Any, ctx: Ctx) -> None:
            rows.replace(ctx, next, {"diff": True})

        return {
            "label": lambda _ctx: f"Todos for {list_}",
            "rows": rows,
            "openCount": openCount,
            "add": add,
            "finish": finish,
            "reset": reset,
        }

    return factory


todoFactory = todoStore()
# end-example:03-factory

# example:05-views
Todos = remote(
    "tutorial.todo-view",
    {
        "version": 1,
        "input": Type.Object({"list": Type.String()}),
        "host": hostNamed("server"),
        "variants": {
            "viewer": {"state": {"rows": Rows}},
            "editor": {
                "state": {"rows": Rows},
                "methods": {"add": method(Type.Object({"text": Type.String()}))},
            },
        },
    },
)

# Who may ask: the owner, a guest, or the server's own code.
type Principal = Literal["owner", "guest", "server"]


def serveViews(api: Host) -> None:
    def factory(input: Any, principal: Principal, self: Any) -> dict[str, Any]:
        list_ = input["list"]
        if list_ != "family":
            raise forbidden("This list is not shared")
        store = self.get(TodoStore, {"list": list_})
        if principal == "guest":
            return {"variant": "viewer", "rows": store.rows}

        def add(args: Any, ctx: Ctx) -> Any:
            if len(args["text"].strip()) == 0:
                raise invalid("args", "A todo needs text")
            return store.add({"text": args["text"]}, ctx)

        return {"variant": "editor", "rows": store.rows, "add": add}

    api.serve(Todos, factory)


# end-example:05-views


async def test_01_objects_live_inside_hosts():
    checkExamples("01")
    # example:01
    host = createHost({"self": "server", "name": "todo-server"})
    assert host.name == "todo-server"
    await host.close()
    # end-example:01


async def test_02_first_contract_factory_reference_and_root(onTestFinished):
    checkExamples("02")
    onTestFinished(lambda: host.close())
    # example:02-call
    host = createHost({"self": "server"})
    host.serve(TodoLabel, lambda input, *_: {"label": lambda _ctx: f"Todos for {input['list']}"})

    async def labelOf(ctx: Ctx) -> str:
        todos = await ctx.get(TodoLabel, {"list": "family"})
        return await todos.label(ctx)

    result = await host.root(labelOf)
    assert result == "Todos for family"
    # end-example:02-call


async def test_03_cells_publish_values_and_derivations(onTestFinished):
    checkExamples("03")
    onTestFinished(lambda: host.close())
    # example:03-call
    host = createHost({"self": "server", "dev": True})
    host.serve(TodoStore, todoFactory)
    todos = await host.get(TodoStore, {"list": "family"})
    counts: list[int] = []
    stop = todos.openCount.subscribe(counts.append)

    async def edit(ctx: Ctx) -> None:
        await todos.add({"text": "Buy milk"}, ctx)
        await todos.finish({"index": 0}, ctx)
        await todos.reset([], ctx)

    await host.root(edit)
    assert counts == [0, 1, 0]
    assert todos.rows.value == []
    stop()
    todos.release()
    # end-example:03-call


async def test_04_holds_asynchronous_resources_and_release(onTestFinished):
    checkExamples("04")
    onTestFinished(lambda: host.close())
    # example:04-database
    Database = local(
        "tutorial.database",
        {
            "version": 1,
            "input": Type.Object({}),
            "members": {"db": opaque("tutorial.database@1")},
        },
    )
    SavedTodos = local(
        "tutorial.saved-todos",
        {
            "version": 1,
            "input": Type.Object({"list": Type.String()}),
            "members": {"add": method(Type.Object({"text": Type.String()}))},
        },
    )
    host = createHost({"self": "server"})
    opens = 0
    closed = asyncio.Event()

    def databaseFactory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        async def open(_options: Any) -> dict[str, list[str]]:
            nonlocal opens
            opens += 1
            return {"saved": []}

        db = self.resource("db", {"version": 1, "open": open, "close": lambda _db: closed.set()})
        return {"db": self.expose(db)}

    host.serve(Database, databaseFactory)
    # end-example:04-database
    # example:04-saving
    host.serve(TodoStore, todoFactory)

    def savedTodos(input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        database = self.get(Database, {})
        todos = self.get(TodoStore, {"list": input["list"]})

        async def add(args: Any, ctx: Ctx) -> None:
            database.db["saved"].append(args["text"])
            await todos.add({"text": args["text"]}, ctx)

        return {"add": add}

    host.serve(SavedTodos, savedTodos, {"linger": 0})
    # end-example:04-saving
    # example:04-call
    saved = await host.get(SavedTodos, {"list": "family"})
    database = await host.get(Database, {})
    await host.root(lambda ctx: saved.add({"text": "Buy milk"}, ctx))
    assert database.db["saved"] == ["Buy milk"]
    assert opens == 1
    statuses: list[str] = []
    saved.onStatus(lambda status, _error: statuses.append(status))
    database.release()
    saved.release()
    await closed.wait()
    assert saved.status == "released"
    assert "released" in statuses
    # end-example:04-call


async def test_05_principals_select_views_of_one_shared_store(onTestFinished):
    checkExamples("05")
    onTestFinished(lambda: host.close())
    # example:05-call
    host = createHost({"name": "server", "self": "server", "principalKey": lambda principal: principal})
    host.serve(TodoStore, todoFactory)
    serveViews(host)
    owner = await host.get(Todos, {"list": "family"}, {"principal": "owner"})
    guest = await host.get(Todos, {"list": "family"}, {"principal": "guest"})
    assert owner.variant == "editor"  # the variant that has add
    assert guest.variant == "viewer"
    await host.root(lambda ctx: owner.add({"text": "Buy milk"}, ctx), {"principal": "owner"})
    assert guest.rows.value == [{"text": "Buy milk", "done": False}]
    # A viewer has no add: the provider refuses it.
    with pytest.raises(ChordError) as rejected:
        await guest.add({"text": "Not allowed"})
    assert isChordError(rejected.value, "not_in_variant")
    with pytest.raises(ChordError) as rejected:
        await owner.add({"text": " "})
    assert isChordError(rejected.value, "invalid")
    # end-example:05-call


async def test_06_remote_methods_and_live_state_over_a_memory_link(onTestFinished):
    checkExamples("06")
    onTestFinished(lambda: server.close())
    onTestFinished(lambda: browser.close())
    # example:06
    server = createHost({"self": "server", "name": "server"})
    server.serve(TodoStore, todoFactory)
    serveViews(server)
    down, up = memoryLinkPair()
    server.accept(down, {"principal": "owner", "kind": "client"})
    browser = createHost(
        {
            "self": "browser",
            "name": "browser",
            "kind": "client",
            "upstream": {"link": up, "principal": "server"},
        }
    )
    todos = await browser.get(Todos, {"list": "family"})
    assert todos.variant == "editor"
    assert todos.rows.value == []
    updated = asyncio.Event()

    def onRows(rows: Any) -> None:
        if len(rows) == 1:
            updated.set()

    stop = todos.rows.subscribe(onRows)
    await browser.root(lambda ctx: todos.add({"text": "Buy milk"}, ctx))
    await updated.wait()
    assert todos.rows.value == [{"text": "Buy milk", "done": False}]
    # Deliberately break the schema to simulate a malformed caller.
    with pytest.raises(ChordError) as rejected:
        await todos.add({"text": 123})
    assert isChordError(rejected.value, "invalid")
    stop()
    # A held reference keeps its state without listeners.
    assert todos.rows.value == [{"text": "Buy milk", "done": False}]
    todos.release()
    # end-example:06


async def test_07_listeners_react_and_tasks_start_fresh_calls(onTestFinished):
    checkExamples("07")
    onTestFinished(lambda: host.close())
    # example:07
    Activity = local(
        "tutorial.activity",
        {
            "version": 1,
            "input": Type.Object({}),
            "members": {"seen": Type.Number()},
        },
    )
    host = createHost({"self": "server"})
    host.serve(TodoStore, todoFactory)
    observed = asyncio.Event()
    seeded = asyncio.Event()
    stopped = asyncio.Event()

    def activityFactory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        todos = self.get(TodoStore, {"list": "family"})
        seen = self.state("seen", Type.Number(), 0)

        def onRows(rows: Any, ctx: Ctx) -> None:
            seen.replace(ctx, len(rows) if isinstance(rows, list) else 0)
            if isinstance(rows, list) and len(rows) == 1:
                observed.set()

        self.on("rows", todos.rows, onRows)

        async def task(ctx: Ctx) -> None:
            await todos.add({"text": "Review the list"}, ctx)
            seeded.set()
            aborted = asyncio.Event()
            if ctx.signal.aborted:
                aborted.set()
            else:
                ctx.signal.addEventListener("abort", aborted.set, once=True)
            await aborted.wait()
            stopped.set()

        self.task(task)
        return {"seen": seen}

    host.serve(Activity, activityFactory)
    activity = await host.get(Activity, {})
    await asyncio.gather(seeded.wait(), observed.wait())
    assert activity.seen.value == 1
    activity.release()
    await stopped.wait()
    # end-example:07


async def test_08_plugins_contribute_registry_entries_and_expose_callbacks(onTestFinished):
    checkExamples("08")
    onTestFinished(lambda: host.close())
    # example:08
    Actions = defineRegistry("tutorial.actions")
    Menu = local("tutorial.menu", {"version": 1, "input": Type.Object({})})
    click: Callable[[], Awaitable[int]] | None = None
    storeFacet = Facet(
        name="todo-store",
        **vars(defineFacet(lambda api: api.serve(TodoStore, todoFactory))),
    )

    def menuSetup(api: FacetApi) -> None:
        nonlocal click

        def menu(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            todos = self.get(TodoStore, {"list": "family"})

            async def addSample(ctx: Ctx) -> int:
                await todos.add({"text": "Try plugins"}, ctx)
                return todos.openCount.value

            self.register(Actions, {"addSample": addSample})
            return {}

        api.serve(Menu, menu, {"lifetime": "host"})

        def clickAddSample() -> Awaitable[int]:
            return api.root(lambda ctx: api.registry(Actions).get("addSample", ctx)(ctx))

        click = clickAddSample

    menuFacet = Facet(
        name="todo-menu",
        **vars(defineFacet(menuSetup, {"uses": [TodoStore]})),
    )
    host = createHost({"self": "server"})
    await host.load([menuFacet, storeFacet], {"awaitHostObjects": False})
    await host.ready()
    assert click is not None
    assert await click() == 1
    assert len(host.registry(Actions).table()) == 1
    # end-example:08


async def test_09_compatible_reload_migration_and_an_old_contract_adapter(onTestFinished):
    checkExamples("09")
    onTestFinished(lambda: host.close())
    # example:09-compatible
    host = createHost({"self": "server"})

    def facet(label: str) -> Facet:
        def factory(input: Any, principal: Any, self: Any) -> dict[str, Any]:
            return {**todoFactory(input, principal, self), "label": lambda _ctx: label}

        return Facet(name="todos", setup=lambda api: api.serve(TodoStore, factory))

    await host.load([facet("Original")])
    todos = await host.get(TodoStore, {"list": "family"})
    await todos.add({"text": "Buy milk"})
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def oldCaller(ctx: Ctx) -> list[str]:
        first = await todos.label(ctx)
        entered.set()
        await resume.wait()  # Test-only pause: pin an old caller during reload.
        return [first, await todos.label(ctx)]

    oldRoot = host.root(oldCaller)
    await entered.wait()
    compatible = await host.reload([facet("Updated")])
    assert compatible["gated"] == []
    assert await todos.label() == "Updated"
    resume.set()
    assert await oldRoot == ["Original", "Original"]
    assert todos.rows.value[0]["text"] == "Buy milk"
    # end-example:09-compatible
    # example:09-migrate
    events: list[str] = []
    stopEvents = todos.rows.events(lambda event: events.append(event["type"]))

    def migrate(old: Any, from_: int) -> Any:
        assert from_ == 1
        return [{**todo, "text": todo["text"].upper()} for todo in old]

    migrated = await host.reload(
        [
            Facet(
                name="todos",
                setup=lambda api: api.serve(TodoStore, todoStore({"version": 2, "migrate": migrate})),
            )
        ]
    )
    assert len(migrated["gated"]) == 1
    assert todos.rows.value == [{"text": "BUY MILK", "done": False}]
    assert events == ["snap", "reset"]
    stopEvents()
    # end-example:09-migrate
    # example:09-contract
    # Remove label from the new provider's contract: it is now called heading.
    members = {name: member for name, member in TodoStore.def_["members"].items() if name != "label"}
    RenamedStore = local(
        "tutorial.todos",
        {
            "version": 2,
            "input": TodoStore.def_["input"],
            "members": {**members, "heading": method(None, Type.String())},
        },
    )

    def renamed(input: Any, principal: Any, self: Any) -> dict[str, Any]:
        implementation = todoStore({"version": 2})(input, principal, self)
        label = implementation.pop("label")
        return {**implementation, "heading": label}

    await host.reload(
        [
            Facet(
                name="todos",
                setup=lambda api: api.serve(
                    RenamedStore,
                    renamed,
                    {"accepts": {1: lambda current: {**current, "label": current["heading"]}}},
                ),
            )
        ]
    )
    assert await todos.label() == "Todos for family"
    newer = await host.get(RenamedStore, {"list": "family"})
    assert await newer.heading() == "Todos for family"
    assert todos.rows.value[0]["text"] == "BUY MILK"
    # end-example:09-contract


async def test_10_stub_a_provider_with_a_fake_facet_and_check_error_codes(onTestFinished):
    checkExamples("10")
    onTestFinished(lambda: host.close())
    # example:10
    host = createHost({"name": "server", "self": "server", "dev": True})
    received: list[str] = []

    def fakeSetup(api: FacetApi) -> None:
        def factory(input: Any, principal: Any, self: Any) -> dict[str, Any]:
            return {
                **todoFactory(input, principal, self),
                "add": lambda args, _ctx: received.append(args["text"]),
            }

        api.serve(TodoStore, factory)

    fake = Facet(name="fake-store", **vars(defineFacet(fakeSetup)))
    await host.load([fake])
    serveViews(host)
    owner = await host.get(Todos, {"list": "family"}, {"principal": "owner"})
    assert owner.variant == "editor"
    await owner.add({"text": "Test delegation"})
    assert received == ["Test delegation"]
    with pytest.raises(ChordError) as rejected:
        await owner.add({"text": ""})
    assert isChordError(rejected.value, "invalid")
    owner.release()
    with pytest.raises(ChordError) as rejected:
        await owner.add({"text": "Too late"})
    assert isChordError(rejected.value, "released")
    # end-example:10


async def test_11_short_roots_release_temporary_holds(onTestFinished):
    checkExamples("11")
    onTestFinished(lambda: host.close())
    # example:11
    host = createHost({"self": "server"})
    host.serve(TodoStore, todoFactory)
    screen = await host.get(TodoStore, {"list": "family"})

    async def click(ctx: Ctx) -> None:
        temporary = await ctx.get(TodoStore, {"list": "family"})
        await temporary.add({"text": "Keep roots short"}, ctx)

    await host.root(click)  # The temporary reference is released here; screen still holds the store.
    assert screen.openCount.value == 1
    screen.release()  # No reference remains; without persistence, disposal loses these rows.
    # end-example:11

"""pi chord's ``test/state-init.test.ts``.

MISAKA adaptations: ``setImmediate`` is ``tick()``, a few event-loop turns; a latch is an ``asyncio.Future``; a
mutating ``change`` callback returns ``None`` (MISAKA fork of ``Cell.change``); ``undefined`` is ``UNDEFINED``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from misaka.chord import ChordError, createHost, isChordError, local, method
from misaka.chord._typebox import Type
from misaka.chord.host import Facet
from misaka.utils.js import UNDEFINED


async def tick(turns: int = 20) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


async def until(check: Callable[[], bool]) -> None:
    for _ in range(200):
        if check():
            break
        await tick()
    assert check(), "condition not reached"


Notes = local(
    "t.notes",
    {
        "version": 1,
        "input": Type.Object({}),
        "members": {
            "notes": Type.Array(Type.String()),
            "count": Type.Number(),
            "has": method(Type.String(), Type.Boolean()),
            "add": method(Type.String()),
        },
    },
)

NotesSchema = Type.Array(Type.String())


def quiet() -> Any:
    return createHost({"self": {"kind": "host"}, "onError": lambda _error: None})


def push(note: str) -> Callable[[Any], None]:
    def mutate(draft: Any) -> None:
        draft.push(note)

    return mutate


async def test_runs_after_the_resources_are_open_and_before_the_object_goes_live_nothing_sees_another_value():
    host = quiet()
    loaded: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()
    log: list[str] = []

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        def open(_options: Any) -> Any:
            log.append("open")
            return {"load": lambda: loaded}

        db = self.resource("db", {"version": 1, "open": open})

        async def init(_ctx: Any) -> Any:
            log.append("init")
            return await db.value["load"]()

        notes = self.state("notes", NotesSchema, {"init": init})
        count = self.derive([notes], lambda list_: len(list_))
        self.on("notes", notes, lambda list_, _ctx: log.append(f"listener {','.join(list_)}"))

        async def task(_ctx: Any) -> None:
            log.append(f"task {','.join(notes.value)}")

        self.task(task)
        return {
            "notes": notes,
            "count": count,
            "has": lambda note, _ctx: note in notes.value,
            "add": lambda note, ctx: notes.change(ctx, push(note)),
        }

    host.serve(Notes, factory)
    ref = host.hold(Notes, {})
    seen: list[Any] = []
    ref.notes.subscribe(seen.append)
    # A call made while init runs waits until the object is live, then reads the loaded value.
    has = host.root(lambda ctx: ref.has("a", ctx))
    await until(lambda: "init" in log)
    assert ref.status == "loading"
    loaded.set_result(["a", "b"])
    assert await has is True
    assert ref.count.value == 2
    await until(lambda: len(log) == 4)
    assert log == ["open", "init", "listener a,b", "task a,b"]
    # Before live, a reference's member has no value (undefined); its first value is the loaded one.
    assert [value for value in seen if value is not UNDEFINED] == [["a", "b"]]


async def test_an_init_cell_cannot_be_read_inside_the_factory():
    host = quiet()

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        notes = self.state("notes", NotesSchema, {"init": lambda _ctx: []})
        count = self.derive([notes], lambda list_: len(list_))
        notes.value  # noqa: B018 - the read is the check
        return {"notes": notes, "count": count, "has": lambda *_: False, "add": lambda *_: None}

    host.serve(Notes, factory)
    with pytest.raises(ChordError) as info:
        await host.hold(Notes, {}).ready
    assert isChordError(info.value) and info.value.code == "not_ready"


async def test_a_throwing_or_invalid_init_fails_the_activation_and_closes_its_resources_the_next_get_tries_again():
    host = quiet()

    def fails() -> Any:
        raise RuntimeError("db down")

    result: list[Callable[[], Any]] = [fails]
    closed: list[str] = []

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        self.resource("db", {"version": 1, "open": lambda _o: "db", "close": closed.append})
        notes = self.state("notes", NotesSchema, {"init": lambda _ctx: result[0]()})
        count = self.derive([notes], lambda list_: len(list_))
        return {"notes": notes, "count": count, "has": lambda *_: False, "add": lambda *_: None}

    host.serve(Notes, factory)
    with pytest.raises(ChordError, match="db down"):
        await host.hold(Notes, {}).ready
    assert closed == ["db"]
    result[0] = lambda: [1]
    with pytest.raises(ChordError) as info:
        await host.hold(Notes, {}).ready
    assert isChordError(info.value) and info.value.code == "invalid"
    assert closed == ["db", "db"]
    result[0] = lambda: ["ok"]
    ref = host.hold(Notes, {})
    await ref.ready
    assert ref.notes.value == ["ok"]


async def test_init_runs_in_a_root_it_can_call_other_objects():
    host = quiet()
    Source = local(
        "t.source",
        {"version": 1, "input": Type.Object({}), "members": {"all": method(None, NotesSchema)}},
    )
    host.serve(Source, lambda *_: {"all": lambda _ctx: ["from source"]})

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        async def init(ctx: Any) -> Any:
            return await (await ctx.get(Source, {})).all(ctx)

        notes = self.state("notes", NotesSchema, {"init": init})
        count = self.derive([notes], lambda list_: len(list_))
        return {"notes": notes, "count": count, "has": lambda *_: False, "add": lambda *_: None}

    host.serve(Notes, factory)
    ref = host.hold(Notes, {})
    await ref.ready
    assert ref.notes.value == ["from source"]


# ─── state init: reload ───


def facet(inits: list[str], extra: Callable[[], list[str]] | None = None, countVersion: int | None = None) -> Facet:
    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        def initNotes(_ctx: Any) -> list[str]:
            inits.append("notes")
            return ["loaded"]

        notes = self.state("notes", NotesSchema, {"init": initNotes})
        counter = self.state("counter", Type.Number(), 0, {"version": countVersion or 1})
        more = None
        if extra is not None:

            def initMore(_ctx: Any) -> list[str]:
                inits.append("more")
                return extra()

            more = self.state("more", NotesSchema, {"init": initMore})

        def total(*lists: Any) -> float:
            return sum(item if isinstance(item, int | float) else len(item) for item in lists)

        count = self.derive([notes, counter] if more is None else [notes, counter, more], total)
        return {
            "notes": notes,
            "count": count,
            "has": lambda note, _ctx: note in notes.value,
            "add": lambda note, ctx: notes.change(ctx, push(note)),
        }

    return Facet("notes", lambda api: api.serve(Notes, factory))


async def test_keeps_an_existing_cell_without_running_init_a_cell_the_reload_adds_runs_init_while_it_prepares():
    host = quiet()
    inits: list[str] = []
    await host.install(facet(inits))
    ref = host.hold(Notes, {})
    await ref.ready
    await host.root(lambda ctx: ref.add("written", ctx))
    await host.reload([facet(inits, extra=lambda: ["x", "y"])])
    assert inits == ["notes", "more"]
    assert ref.notes.value == ["loaded", "written"]
    # The new derivation counts the kept cell and the added one: 2 + 0 + 2.
    assert ref.count.value == 4


async def test_a_failing_init_of_an_added_cell_refuses_the_reload_the_old_code_keeps_running():
    host = quiet()
    inits: list[str] = []
    await host.install(facet(inits))
    ref = host.hold(Notes, {})
    await ref.ready

    def extra() -> list[str]:
        raise RuntimeError("cannot load more")

    with pytest.raises(ChordError, match="cannot load more"):
        await host.reload([facet(inits, extra=extra)])
    assert ref.count.value == 1
    assert await host.root(lambda ctx: ref.has("loaded", ctx)) is True


async def test_in_a_gated_reload_an_added_cell_runs_init_at_the_gate():
    host = quiet()
    inits: list[str] = []
    await host.install(facet(inits))
    ref = host.hold(Notes, {})
    await ref.ready
    report = await host.reload([facet(inits, extra=lambda: ["x"], countVersion=2)])
    assert len(report["gated"]) == 1
    assert inits == ["notes", "more"]
    assert ref.count.value == 2

"""pi's ``models-store.test.ts`` cases (``misaka/core/models_store.py``).

Upstream's "preserves the mode of an existing models file" case is not ported: the store
shares the auth store's locked backend, which narrows every file it keeps to 0600.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from filelock import FileLock

from misaka.ai.models_store import ModelsStoreEntry, ModelsStoreOperationOptions
from misaka.ai.types import Model
from misaka.core import models_store
from misaka.core.auth_storage import FileAuthStorageBackend
from misaka.core.models_store import FileModelsStore
from misaka.utils.js import AbortController


@pytest.fixture(autouse=True)
def _fresh_shared_read_state(monkeypatch):
    # Each upstream test file gets a fresh module; the first store built here claims the slot.
    monkeypatch.setattr(models_store, "_sharedModelsFileReadState", None)


def model(provider: str, id: str) -> Model:
    return Model.model_validate({
        "id": id, "name": id, "api": "openai-completions", "provider": provider,
        "baseUrl": "https://example.test/v1", "reasoning": False, "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000, "maxTokens": 100,
    })


def _signal(controller: AbortController | None = None) -> ModelsStoreOperationOptions:
    return ModelsStoreOperationOptions(signal=(controller or AbortController()).signal)


def _raw(provider: str, id: str) -> dict:
    return model(provider, id).model_dump(mode="json", exclude_unset=True)


def _count_locks(monkeypatch) -> list[int]:
    calls: list[int] = []
    acquire = FileAuthStorageBackend.acquireLockAsync

    async def counting(self, options=None):
        calls.append(1)
        return await acquire(self, options)

    monkeypatch.setattr(FileAuthStorageBackend, "acquireLockAsync", counting)
    return calls


async def test_persists_provider_catalogs_without_replacing_unrelated_providers(tmp_path):
    path = tmp_path / "models-store.json"
    store = FileModelsStore(path)

    await store.write("one", ModelsStoreEntry(models=[model("one", "m1")], checkedAt=100))
    await store.write("two", ModelsStoreEntry(models=[model("two", "m2")], checkedAt=200))

    reloaded = FileModelsStore(path)
    assert [entry.id for entry in (await reloaded.read("one")).models] == ["m1"]
    assert (await reloaded.read("one")).checkedAt == 100
    assert [entry.id for entry in (await reloaded.read("two")).models] == ["m2"]

    await reloaded.delete("one")
    assert await reloaded.read("one") is None
    assert [entry.id for entry in (await reloaded.read("two")).models] == ["m2"]


async def test_coalesces_file_reloads_across_concurrent_readers_and_interleaved_storage_instances(
    tmp_path, monkeypatch
):
    path = tmp_path / "models-store.json"
    path.write_text(json.dumps({
        "one": {"models": [_raw("one", "old")]},
        "two": {"models": [_raw("two", "m2")]},
    }), encoding="utf-8")
    first = FileModelsStore(path)
    second = FileModelsStore(path)
    locks = _count_locks(monkeypatch)

    one, two, missing = await asyncio.gather(
        first.read("one", _signal()),
        second.read("two", _signal()),
        first.read("missing", _signal()),
    )
    assert [entry.id for entry in one.models] == ["old"]
    assert [entry.id for entry in two.models] == ["m2"]
    assert missing is None
    assert len(locks) == 1

    assert (await second.read("one")).models[0].id == "old"
    assert len(locks) == 1

    other_path = tmp_path / "other-models-store.json"
    other_path.write_text("{}", encoding="utf-8")
    other = FileModelsStore(other_path)
    assert await other.read("one") is None
    assert await other.read("one") is None
    third = FileModelsStore(path)
    path.write_text(json.dumps({"one": {"models": [_raw("one", "newest-model")]}}), encoding="utf-8")
    first_reload, third_reload = await asyncio.gather(first.read("one"), third.read("one"))
    assert first_reload.models[0].id == "newest-model"
    assert third_reload.models[0].id == "newest-model"
    assert len(locks) == 3


async def test_keeps_a_coalesced_reload_alive_while_another_reader_is_still_waiting(tmp_path, monkeypatch):
    path = tmp_path / "models-store.json"
    path.write_text(json.dumps({"one": {"models": [_raw("one", "stored")]}}), encoding="utf-8")
    store = FileModelsStore(path)
    lock_granted = asyncio.Event()
    locks: list[int] = []
    releases: list[int] = []

    class Lock:
        def release(self) -> None:
            releases.append(1)

    async def held_lock(self, options=None):
        locks.append(1)
        await lock_granted.wait()
        return Lock()

    monkeypatch.setattr(FileAuthStorageBackend, "acquireLockAsync", held_lock)
    first_controller = AbortController()
    second_controller = AbortController()
    first = asyncio.ensure_future(store.read("one", _signal(first_controller)))
    second = asyncio.ensure_future(store.read("one", _signal(second_controller)))
    await asyncio.sleep(0)

    first_controller.abort()
    with pytest.raises(Exception) as raised:
        await first
    assert getattr(raised.value, "name", None) == "AbortError"
    lock_granted.set()
    assert (await second).models[0].id == "stored"
    assert len(locks) == 1
    assert len(releases) == 1


async def test_cancels_a_catalog_write_waiting_for_a_held_file_lock_without_writing_later(tmp_path):
    path = tmp_path / "models-store.json"
    path.write_text(json.dumps({"one": {"models": [_raw("one", "existing")]}}), encoding="utf-8")
    store = FileModelsStore(path)
    held = FileLock(f"{path}.lock", timeout=0)
    held.acquire()
    try:
        controller = AbortController()
        pending = asyncio.ensure_future(
            store.write("two", ModelsStoreEntry(models=[model("two", "cancelled")]), _signal(controller))
        )

        await asyncio.sleep(0.01)
        controller.abort()
        with pytest.raises(Exception) as raised:
            await pending
        assert getattr(raised.value, "name", None) == "AbortError"
    finally:
        held.release()
    await asyncio.sleep(0.15)

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert "one" in stored
    assert "two" not in stored


async def test_a_stored_model_this_version_cannot_read_is_dropped_not_fatal(tmp_path):
    # pi keeps the raw entry and `Models` drops unknown types; a refresh must not fail on it.
    path = tmp_path / "models-store.json"
    path.write_text(json.dumps({"dyn": {"checkedAt": 1, "future": True, "models": [
        _raw("dyn", "chat"),
        {**_raw("dyn", "painter"), "type": "embedding"},
        {**_raw("dyn", "newer"), "futureField": 1},
    ]}}), encoding="utf-8")

    entry = await FileModelsStore(str(path)).read("dyn", _signal())

    assert [stored.id for stored in entry.models] == ["chat"]
    assert entry.checkedAt == 1


async def test_a_stored_model_with_a_non_string_type_is_dropped_not_fatal(tmp_path):
    # MISAKA: `Object.hasOwn` coerces any key; a list or object type must not raise here.
    path = tmp_path / "models-store.json"
    path.write_text(json.dumps({
        "one": {"models": [{**_raw("one", "bad"), "type": ["chat"]}, _raw("one", "good")], "checkedAt": 1},
    }), encoding="utf-8")

    entry = await FileModelsStore(path).read("one")

    assert [stored.id for stored in entry.models] == ["good"]

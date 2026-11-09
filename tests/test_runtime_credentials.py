"""pi's ``runtime-credentials.test.ts`` cases (``misaka/core/runtime_credentials.py``)."""

from __future__ import annotations

import time

import pytest

from misaka.ai.auth.types import ApiKeyCredential, AuthOperationOptions, CredentialInfo
from misaka.core.auth_storage import AuthStorage
from misaka.core.runtime_credentials import RuntimeCredentials
from misaka.utils._abort_signal import AbortController


def _store(data: dict) -> AuthStorage:
    return AuthStorage.inMemory(data)


async def test_runtime_overrides_mask_stored_credentials_without_persisting():
    storage = _store({"anthropic": {"type": "api_key", "key": "stored-key"}})
    credentials = RuntimeCredentials(storage)

    credentials.setRuntimeApiKey("anthropic", "runtime-key")
    assert await credentials.read("anthropic") == ApiKeyCredential(key="runtime-key")
    assert await storage.read("anthropic") == ApiKeyCredential(key="stored-key")

    credentials.removeRuntimeApiKey("anthropic")
    assert await credentials.read("anthropic") == ApiKeyCredential(key="stored-key")


async def test_enumeration_merges_overrides_without_exposing_keys():
    storage = _store({"anthropic": {
        "type": "oauth", "access": "access", "refresh": "refresh", "expires": int(time.time() * 1000) + 60_000,
    }})
    credentials = RuntimeCredentials(storage)
    credentials.setRuntimeApiKey("anthropic", "runtime-key")
    credentials.setRuntimeApiKey("openai", "other-runtime-key")

    assert await credentials.list() == [
        CredentialInfo(providerId="anthropic", type="api_key"),
        CredentialInfo(providerId="openai", type="api_key"),
    ]


async def test_forwards_operation_signals_to_the_persistent_store():
    controller = AbortController()
    received = []

    class Store:
        async def read(self, _providerId, options=None):
            received.append(options.signal)

        async def list(self, options=None):
            received.append(options.signal)
            return []

        async def modify(self, _providerId, _fn, options=None):
            received.append(options.signal)

        async def delete(self, _providerId, options=None):
            received.append(options.signal)

    credentials = RuntimeCredentials(Store())
    options = AuthOperationOptions(signal=controller)

    await credentials.read("anthropic", options)
    await credentials.list(options)
    await credentials.modify("anthropic", _none, options)
    await credentials.delete("anthropic", options)

    assert received == [controller, controller, controller, controller]


async def _none(_current):
    return None


async def test_keeps_a_runtime_override_when_persistent_deletion_is_cancelled(monkeypatch):
    aborted = RuntimeError("cancelled")
    storage = _store({"anthropic": {"type": "api_key", "key": "stored-key"}})
    calls = []

    async def failing_delete(providerId, options=None):
        calls.append(providerId)
        raise aborted

    monkeypatch.setattr(storage, "delete", failing_delete)
    credentials = RuntimeCredentials(storage)
    credentials.setRuntimeApiKey("anthropic", "runtime-key")

    with pytest.raises(RuntimeError) as raised:
        await credentials.delete("anthropic", AuthOperationOptions(signal=AbortController()))
    assert raised.value is aborted
    assert calls == ["anthropic"]
    assert await credentials.read("anthropic") == ApiKeyCredential(key="runtime-key")


async def test_delete_clears_both_the_override_and_persisted_credential():
    storage = _store({"anthropic": {"type": "api_key", "key": "stored-key"}})
    credentials = RuntimeCredentials(storage)
    credentials.setRuntimeApiKey("anthropic", "runtime-key")

    await credentials.delete("anthropic")

    assert await credentials.read("anthropic") is None
    assert await credentials.list() == []

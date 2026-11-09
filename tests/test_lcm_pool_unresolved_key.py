"""An LCM credential pool skips a stored key it cannot resolve instead of failing the route
(``misaka_lcm/host/pool.py``; the store now reads such a key as ``None``, as pi's does)."""

from __future__ import annotations

import time

from misaka.core.auth_storage import AuthStorage
from misaka.core.model_registry import ModelRegistry
from misaka.core.model_runtime import ModelRuntime
from misaka.extensions.misaka_lcm.host.pool import Pool


async def test_an_unresolvable_pooled_key_is_skipped(monkeypatch):
    monkeypatch.delenv("UNSET_LCM_POOL_KEY", raising=False)
    storage = AuthStorage.inMemory({
        "openai-codex": {"type": "oauth", "access": "good-token", "refresh": "r",
                         "expires": int(time.time() * 1000) + 3_600_000},
        "openai-codex:alt": {"type": "api_key", "key": "$UNSET_LCM_POOL_KEY"},
    })
    registry = ModelRegistry(await ModelRuntime.create({"authStorage": storage, "modelsPath": None}))
    pool = Pool(registry, "openai-codex", ["openai-codex", "openai-codex:alt"], "fill_first")

    entry = await pool._select(failed_key=None, error=None, signal=None)

    assert entry is not None and entry.id == "openai-codex"

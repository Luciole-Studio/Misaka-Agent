"""Translated from pi's ``coding-agent/test/suite/regressions/7443-model-command-cached-match.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from misaka.modes.interactive.interactive_mode import InteractiveMode
from tests.suite_harness import createHarness


async def test_matches_the_availability_snapshot_without_starting_a_catalog_refresh(monkeypatch):
    async with createHarness(models=[{"id": "cached", "name": "Cached"}]) as harness:

        def never(*_args, **_kwargs):
            return asyncio.get_running_loop().create_future()

        refresh = Mock(side_effect=never)
        monkeypatch.setattr(harness.session.modelRuntime, "refresh", refresh)
        context = SimpleNamespace(session=harness.session, showStatus=Mock(), showWarning=Mock())

        model = await InteractiveMode.findExactModelMatch(context, harness.models[0].id)

        assert model.id == "cached"
        refresh.assert_not_called()
        context.showStatus.assert_not_called()


async def test_uses_a_caller_owned_deadline_only_after_a_cache_miss(monkeypatch):
    async with createHarness(models=[{"id": "cached", "name": "Cached"}]) as harness:
        refresh = AsyncMock(return_value=SimpleNamespace(aborted=True, errors={}))
        monkeypatch.setattr(harness.session.modelRuntime, "refresh", refresh)
        context = SimpleNamespace(session=harness.session, showStatus=Mock(), showWarning=Mock())

        assert await InteractiveMode.findExactModelMatch(context, "not-cached") is None

        refresh.assert_called_once()
        # MISAKA fork: the refresh gets an abort signal object (the coordinator's own controller, which is its signal).
        assert hasattr(refresh.call_args.args[0].signal, "aborted")
        context.showStatus.assert_called_with("Refreshing model catalogs…")

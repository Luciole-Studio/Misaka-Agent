"""Translated from pi's ``coding-agent/test/interactive-mode-anthropic-warning.test.ts`` (v1.1.0)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from misaka.modes.interactive.interactive_mode import InteractiveMode


def createSettingsManager(warnings: dict[str, bool] | None = None):
    return SimpleNamespace(getWarnings=Mock(return_value=warnings or {}))


def createModelRuntime(credential, apiKey: str | None = None):
    return SimpleNamespace(
        checkAuth=AsyncMock(return_value=credential),
        getAuth=AsyncMock(return_value=SimpleNamespace(auth=SimpleNamespace(apiKey=apiKey)) if apiKey else None),
    )


def fakeThis(modelRuntime, settingsManager=None):
    return SimpleNamespace(
        anthropicSubscriptionWarningShown=False,
        settingsManager=settingsManager or createSettingsManager(),
        session=SimpleNamespace(modelRuntime=modelRuntime),
        showWarning=Mock(),
    )


async def test_warns_once_when_anthropic_subscription_auth_is_detected():
    modelRuntime = createModelRuntime(None, "sk-ant-oat01-test")
    context = fakeThis(modelRuntime)

    await InteractiveMode.maybeWarnAboutAnthropicSubscriptionAuth(context, SimpleNamespace(provider="anthropic"))
    await InteractiveMode.maybeWarnAboutAnthropicSubscriptionAuth(context, SimpleNamespace(provider="anthropic"))

    assert context.showWarning.call_count == 1
    assert modelRuntime.getAuth.await_count == 1


async def test_warns_when_anthropic_oauth_is_stored_even_if_token_refresh_lookup_would_fail():
    modelRuntime = createModelRuntime(SimpleNamespace(type="oauth"))
    context = fakeThis(modelRuntime)

    await InteractiveMode.maybeWarnAboutAnthropicSubscriptionAuth(context, SimpleNamespace(provider="anthropic"))

    assert context.showWarning.call_count == 1
    modelRuntime.getAuth.assert_not_called()


async def test_does_not_warn_for_non_anthropic_models():
    modelRuntime = createModelRuntime(None)
    context = fakeThis(modelRuntime)

    await InteractiveMode.maybeWarnAboutAnthropicSubscriptionAuth(context, SimpleNamespace(provider="openai"))

    context.showWarning.assert_not_called()
    modelRuntime.getAuth.assert_not_called()


async def test_does_not_warn_when_anthropic_extra_usage_warning_is_disabled():
    modelRuntime = createModelRuntime(None)
    context = fakeThis(modelRuntime, createSettingsManager({"anthropicExtraUsage": False}))

    await InteractiveMode.maybeWarnAboutAnthropicSubscriptionAuth(context, SimpleNamespace(provider="anthropic"))

    context.showWarning.assert_not_called()
    modelRuntime.checkAuth.assert_not_called()
    modelRuntime.getAuth.assert_not_called()

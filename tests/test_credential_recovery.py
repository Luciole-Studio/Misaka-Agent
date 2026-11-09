"""A credential the provider refuses is replaced once, and the turn is run again.

2026-09-18 (B7): another client on the same account rotated the shared OAuth refresh token,
so the access token misaka held had been invalidated server-side while still inside its own
expiry window. Four cards' first request came back "Encountered invalidated oauth token for
user", nothing refreshed because the clock said the token was fine, and a person restarted
each card by hand. pi ends the turn there: a 401 is not retryable and auth is resolved on a
clock, never on a rejection."""
import time
from types import SimpleNamespace

import pytest

from misaka.ai.auth.types import ModelAuth, OAuthAuth, ProviderAuth
from misaka.ai.models import ModelsRefreshOptions
from misaka.ai.types import Model
from misaka.core.agent_session import AgentSession
from misaka.core.auth_storage import AuthStorage
from misaka.core.model_runtime import ModelRuntime, is_rejected_credential_error

PROVIDER = "sub2api-claude"


@pytest.mark.parametrize("text", [
    "Encountered invalidated oauth token for user",
    "401 Unauthorized",
    "invalid_api_key",
    "Your access token is expired",
    "authentication failed",
    "token revoked",
])
def test_a_refused_credential_is_recognised(text):
    assert is_rejected_credential_error(text)


@pytest.mark.parametrize("text", [
    None,
    "",
    "overloaded, please retry",
    "rate limit exceeded",
    "500 Internal Server Error",
    "context length exceeded",
    "Monthly usage limit reached",
    # A number or an index, not a status.
    "max_tokens: 401 > 400",
    "Invalid 'input[401].content': string too long",
    "messages.401: `tool_use` ids were found without `tool_result` blocks immediately after",
    "Try again in ~401 min.",
    "invalid token count in request",
])
def test_a_failure_of_the_service_is_not_a_refused_credential(text):
    assert not is_rejected_credential_error(text)


def _model() -> Model:
    return Model.model_validate({
        "id": "m", "name": "m", "api": "openai-completions", "provider": PROVIDER,
        "baseUrl": "https://example.test/v1", "reasoning": False, "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000, "maxTokens": 100,
    })


def _unused(*_args):
    raise RuntimeError("unused")


class _Flow:
    """The OAuth side of a provider: what a refresh hands back, and whether it can run."""

    def __init__(self, replacement="fresh-key", *, header=False, failures=0):
        self.replacement = replacement
        self.header = header
        self.failures = failures
        self.calls: list[str] = []

    async def refresh(self, credential, _signal):
        self.calls.append(credential.access)
        if self.failures:
            self.failures -= 1
            raise OSError("network is down")
        return credential.model_copy(update={"access": self.replacement or credential.access})

    async def toAuth(self, credential):
        if self.header:
            return ModelAuth(headers={"authorization": f"Bearer {credential.access}"})
        return ModelAuth(apiKey=credential.access)

    async def login(self, _interaction):
        raise RuntimeError("not used")


async def _runtime(flow: _Flow, stored: dict | None) -> tuple[ModelRuntime, AuthStorage]:
    storage = AuthStorage.inMemory({PROVIDER: stored} if stored else {})
    runtime = await ModelRuntime.create({"authStorage": storage, "modelsPath": None})
    runtime.registerNativeProvider(SimpleNamespace(
        id=PROVIDER,
        name=PROVIDER,
        auth=ProviderAuth(oauth=OAuthAuth(name="OAuth", login=flow.login, refresh=flow.refresh, toAuth=flow.toAuth)),
        getModels=lambda: [_model()],
        stream=_unused,
        streamSimple=_unused,
    ))
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False, providers=[PROVIDER]))
    return runtime, storage


def _oauth(access="stale-key") -> dict:
    return {"type": "oauth", "access": access, "refresh": "r", "expires": int(time.time() * 1000) + 3_600_000}


async def _request(runtime: ModelRuntime, model: Model | None = None, **options) -> None:
    """What a request does before it reaches the provider: resolve and record its auth."""
    await runtime._prepareRequest(model or _model(), options or None)


async def _resolved(flow: _Flow, stored: dict | None = None) -> tuple[ModelRuntime, AuthStorage]:
    """A runtime that has handed the stored token to a request."""
    runtime, storage = await _runtime(flow, _oauth() if stored is None else stored)
    await _request(runtime)
    return runtime, storage


async def test_a_rejected_oauth_token_is_refreshed_and_the_new_key_reported():
    flow = _Flow()
    runtime, storage = await _resolved(flow)

    assert await runtime.recoverRejectedCredential(PROVIDER) is True
    assert flow.calls == ["stale-key"]
    assert (await storage.read(PROVIDER)).access == "fresh-key"
    assert (await runtime.getAuth(_model())).auth.apiKey == "fresh-key"


async def test_the_same_rejected_token_is_only_recovered_from_once():
    """A second refusal of the same token is the account's answer, not a stale copy."""
    flow = _Flow(replacement="stale-key")
    runtime, _ = await _resolved(flow)

    assert await runtime.recoverRejectedCredential(PROVIDER) is False
    assert await runtime.recoverRejectedCredential(PROVIDER) is False
    assert flow.calls == ["stale-key"]


async def test_a_token_another_session_already_replaced_is_read_back_without_refreshing():
    flow = _Flow()
    runtime, storage = await _resolved(flow)

    async def rotated(current):
        return current.model_copy(update={"access": "rotated"})

    await storage.modify(PROVIDER, rotated)

    assert await runtime.recoverRejectedCredential(PROVIDER) is True
    assert flow.calls == []


async def test_a_logged_out_provider_is_not_a_recovery():
    flow = _Flow()
    runtime, storage = await _resolved(flow)
    await storage.delete(PROVIDER)

    assert await runtime.recoverRejectedCredential(PROVIDER) is False
    assert flow.calls == []


async def test_a_configured_api_key_is_the_users_to_replace():
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory({"anthropic": {"type": "api_key", "key": "stale-key"}}), "modelsPath": None})
    await _request(runtime, runtime.getModels("anthropic")[0])

    assert await runtime.recoverRejectedCredential("anthropic") is False


async def test_a_runtime_api_key_is_the_users_to_replace():
    flow = _Flow()
    runtime, _ = await _runtime(flow, _oauth())
    await runtime.setRuntimeApiKey(PROVIDER, "cli-key")

    assert await runtime.recoverRejectedCredential(PROVIDER) is False
    assert flow.calls == []


async def test_a_provider_that_never_resolved_a_key_is_skipped():
    flow = _Flow()
    runtime, _ = await _runtime(flow, _oauth())

    assert await runtime.recoverRejectedCredential(PROVIDER) is False
    assert await runtime.recoverRejectedCredential("someone-else") is False
    assert flow.calls == []


async def test_a_refresh_that_could_not_run_does_not_spend_the_one_attempt():
    """A network blip is not the account's answer: the same token is still worth replacing."""
    flow = _Flow(failures=1)
    runtime, _ = await _resolved(flow)

    assert await runtime.recoverRejectedCredential(PROVIDER) is False
    assert await runtime.recoverRejectedCredential(PROVIDER) is True
    assert flow.calls == ["stale-key", "stale-key"]


async def test_only_a_request_records_the_token_it_carried():
    """Resolving auth for something else (LCM, a status line) says nothing about what a
    refused request sent."""
    flow = _Flow()
    runtime, _ = await _runtime(flow, _oauth())
    await runtime.getAuth(_model())

    assert await runtime.recoverRejectedCredential(PROVIDER) is False
    assert flow.calls == []


async def test_a_key_the_request_was_given_is_not_the_stores_to_replace():
    flow = _Flow()
    runtime, _ = await _resolved(flow)
    await _request(runtime, apiKey="task-key")

    assert await runtime.recoverRejectedCredential(PROVIDER) is False
    assert flow.calls == []


async def test_each_session_recovers_the_token_its_own_request_carried():
    """Session A replaces the token both sessions sent; session B's refusal of that same old
    token then reads A's replacement back instead of rotating it away from A."""
    flow = _Flow()
    runtime, storage = await _runtime(flow, _oauth())
    await _request(runtime, sessionId="a")
    await _request(runtime, sessionId="b")

    assert await runtime.recoverRejectedCredential(PROVIDER, "a") is True
    await _request(runtime, sessionId="a")
    assert await runtime.recoverRejectedCredential(PROVIDER, "b") is True
    assert flow.calls == ["stale-key"]
    assert (await storage.read(PROVIDER)).access == "fresh-key"


async def test_a_started_credential_operation_outlives_a_cancelled_caller():
    import asyncio

    runtime, _ = await _runtime(_Flow(), _oauth())
    finished = asyncio.Event()

    async def operation():
        await asyncio.sleep(0.05)
        finished.set()

    caller = asyncio.ensure_future(runtime._enqueueCredentialOperation(PROVIDER, None, operation))
    await asyncio.sleep(0.01)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.wait_for(finished.wait(), 1)


async def test_a_credential_carried_in_a_header_is_compared_as_its_token():
    """A flow that sends its credential as a header leaves apiKey unset; the token inside the
    header is what the refresh compares, or the refresh would never be forced."""
    flow = _Flow(header=True)
    runtime, storage = await _resolved(flow)

    assert await runtime.recoverRejectedCredential(PROVIDER) is True
    assert (await storage.read(PROVIDER)).access == "fresh-key"


class _Message:
    def __init__(self, stop="error", error="Encountered invalidated oauth token for user",
                 provider=PROVIDER):
        self.stopReason = stop
        self.errorMessage = error
        self.provider = provider


class _Settings:
    def __init__(self, enabled=True, retries=3):
        self._value = {"enabled": enabled, "maxRetries": retries, "baseDelayMs": 0}

    def getRetrySettings(self):
        return self._value


class _Session:
    """An AgentSession, as much of it as `_recoverRejectedCredential` reads."""

    sessionId = "session"

    def __init__(self, runtime, *, attempt=0, settings=None, model_provider="another-provider"):
        self._modelRuntime = runtime
        self._retryAttempt = attempt
        self.settingsManager = settings or _Settings()
        self.model = type("M", (), {"provider": model_provider})()

    recover = AgentSession._recoverRejectedCredential


class _Recovering:
    """The runtime's recovery, recorded."""

    def __init__(self, result=True):
        self.result = result
        self.calls: list[str] = []

    async def recoverRejectedCredential(self, provider, sessionId=None):
        self.calls.append((provider, sessionId))
        return self.result


async def test_the_session_asks_the_runtime_to_recover_the_provider_that_answered():
    """The session may already point at another model; the token to replace belongs to the
    provider whose request failed."""
    runtime = _Recovering()
    assert await _Session(runtime).recover(_Message()) is True
    assert runtime.calls == [(PROVIDER, "session")]


async def test_a_message_with_no_provider_recovers_nothing():
    runtime = _Recovering()
    assert await _Session(runtime).recover(_Message(provider=None)) is False
    assert runtime.calls == []


@pytest.mark.parametrize("attempt,settings,expected", [
    (1, None, False),                                   # already retrying: one rotation per turn
    (0, _Settings(enabled=False), False),               # retries off: nothing would follow it
    (0, _Settings(retries=0), False),                   # no budget: nothing would follow it
    (0, None, True),
])
async def test_a_token_is_only_rotated_when_a_retry_can_follow(attempt, settings, expected):
    runtime = _Recovering()
    session = _Session(runtime, attempt=attempt, settings=settings)
    assert await session.recover(_Message()) is expected
    assert bool(runtime.calls) is expected


@pytest.mark.parametrize("message", [
    _Message(stop="stop"),
    _Message(error="overloaded"),
    _Message(error=None),
])
async def test_only_a_refused_credential_triggers_recovery(message):
    runtime = _Recovering()
    assert await _Session(runtime).recover(message) is False
    assert runtime.calls == []


async def test_a_recovery_that_raises_leaves_the_original_error_standing():
    class Broken(_Recovering):
        async def recoverRejectedCredential(self, provider, sessionId=None):
            raise OSError("network is down")

    assert await _Session(Broken()).recover(_Message()) is False


class _Turn:
    """An AgentSession as much as the post-run decision reads it."""

    _stopHookContinuationPending = False
    _retryAttempt = 0
    _agentRunAbortRequested = False
    handle = AgentSession._handlePostAgentRun

    def __init__(self, recovered, *, retryable=False):
        self._lastAssistantMessage = _Message()
        self._lastAssistantToolResults = []
        self._recovered, self._retryable = recovered, retryable
        self.prepared = []
        self.agent = SimpleNamespace(hasQueuedMessages=lambda: False)

    def _emit(self, _event):
        pass

    def _isRetryableError(self, _message):
        return self._retryable

    async def _recoverRejectedCredential(self, message):
        return self._recovered

    async def _prepareRetry(self, message):
        self.prepared.append(message)
        return True

    async def _checkCompaction(self, _message, _skip_aborted_check=True, _tool_results=None):
        return False


async def test_a_recovered_credential_makes_the_failed_turn_run_again():
    """The retry itself is pi's: the recovery only makes the failed turn worth running again."""
    turn = _Turn(recovered=True)
    assert await turn.handle() is True
    assert len(turn.prepared) == 1


async def test_a_credential_that_could_not_be_replaced_ends_the_turn():
    turn = _Turn(recovered=False)
    assert await turn.handle() is False
    assert turn.prepared == []


async def test_an_ordinary_transient_error_never_reaches_the_credential_path():
    """A provider that is merely overloaded must not cost a token rotation."""
    turn = _Turn(recovered=False, retryable=True)
    assert await turn.handle() is True
    assert len(turn.prepared) == 1

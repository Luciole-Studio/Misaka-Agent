"""A credential the provider refuses is replaced once, and the turn is run again.

2026-09-18 (B7): another client on the same account rotated the shared OAuth refresh token,
so the access token misaka held had been invalidated server-side while still inside its own
expiry window. Four cards' first request came back "Encountered invalidated oauth token for
user", nothing refreshed because the clock said the token was fine, and a person restarted
each card by hand. pi ends the turn there: a 401 is not retryable and auth is resolved on a
clock, never on a rejection."""
from types import SimpleNamespace

import pytest

from misaka.core import model_registry as registry_module
from misaka.core.agent_session import AgentSession
from misaka.core.model_registry import ModelRegistry, is_rejected_credential_error


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
])
def test_a_failure_of_the_service_is_not_a_refused_credential(text):
    assert not is_rejected_credential_error(text)


class _Storage:
    """The credential store, as much of it as the recovery touches."""

    def __init__(self, stored, replacement="fresh-key"):
        self.stored = stored
        self.replacement = replacement
        self.calls = []

    def get(self, provider):
        return self.stored

    async def refreshOAuthTokenWithLock(self, provider, *, rejected_api_key=None, account=None):
        self.calls.append((provider, rejected_api_key))
        if self.replacement is None:
            return None
        return {"apiKey": self.replacement, "newCredentials": object()}


def _registry(storage, provider="sub2api-claude", key="stale-key"):
    registry = ModelRegistry.__new__(ModelRegistry)
    registry.authStorage = storage
    registry._lastResolvedApiKey = {provider: key}
    registry._recoveredCredentials = {}
    return registry


OAUTH = {"type": "oauth", "access": "stale-key"}


async def test_a_rejected_oauth_token_is_refreshed_and_the_new_key_reported():
    storage = _Storage(OAUTH)
    registry = _registry(storage)
    assert await registry.recoverRejectedCredential("sub2api-claude") is True
    assert storage.calls == [("sub2api-claude", "stale-key")]
    assert registry._lastResolvedApiKey["sub2api-claude"] == "fresh-key"


async def test_the_same_rejected_token_is_only_recovered_from_once():
    """A second refusal of the same token is the account's answer, not a stale copy."""
    storage = _Storage(OAUTH)
    registry = _registry(storage)
    assert await registry.recoverRejectedCredential("sub2api-claude") is True
    registry._lastResolvedApiKey["sub2api-claude"] = "stale-key"      # as if nothing changed
    assert await registry.recoverRejectedCredential("sub2api-claude") is False
    assert len(storage.calls) == 1


async def test_a_refresh_that_returns_the_same_key_is_not_a_recovery():
    storage = _Storage(OAUTH, replacement="stale-key")
    assert await _registry(storage).recoverRejectedCredential("sub2api-claude") is False


async def test_a_logged_out_provider_is_not_a_recovery():
    storage = _Storage(OAUTH, replacement=None)
    assert await _registry(storage).recoverRejectedCredential("sub2api-claude") is False


async def test_a_configured_api_key_is_the_users_to_replace():
    storage = _Storage({"type": "api_key", "key": "stale-key"})
    assert await _registry(storage).recoverRejectedCredential("sub2api-claude") is False
    assert storage.calls == []


async def test_a_provider_that_never_resolved_a_key_is_skipped():
    storage = _Storage(OAUTH)
    registry = _registry(storage)
    assert await registry.recoverRejectedCredential("someone-else") is False
    assert storage.calls == []


async def test_a_refresh_that_raises_leaves_the_original_error_standing():
    class Broken(_Storage):
        async def refreshOAuthTokenWithLock(self, provider, **kwargs):
            raise OSError("network is down")

    assert await _registry(Broken(OAUTH)).recoverRejectedCredential("sub2api-claude") is False


async def test_a_refresh_that_could_not_run_does_not_spend_the_one_attempt():
    """A network blip is not the account's answer: the same token is still worth replacing."""
    class Flaky(_Storage):
        def __init__(self):
            super().__init__(OAUTH)
            self.failed = False

        async def refreshOAuthTokenWithLock(self, provider, **kwargs):
            if not self.failed:
                self.failed = True
                raise OSError("network is down")
            return await super().refreshOAuthTokenWithLock(provider, **kwargs)

    storage = Flaky()
    registry = _registry(storage)
    assert await registry.recoverRejectedCredential("sub2api-claude") is False
    assert await registry.recoverRejectedCredential("sub2api-claude") is True


class _Message:
    def __init__(self, stop="error", error="Encountered invalidated oauth token for user",
                 provider="sub2api-claude"):
        self.stopReason = stop
        self.errorMessage = error
        self.provider = provider


class _Settings:
    def __init__(self, enabled=True, retries=3):
        self._value = {"enabled": enabled, "maxRetries": retries, "baseDelayMs": 0}

    def getRetrySettings(self):
        return self._value


class _Session:
    """An AgentSession, as much of it as `_recover_rejected_credential` reads."""

    def __init__(self, registry, *, attempt=0, settings=None, model_provider="another-provider"):
        self._modelRegistry = registry
        self._retryAttempt = attempt
        self.settingsManager = settings or _Settings()
        self.model = type("M", (), {"provider": model_provider})()

    recover = AgentSession._recover_rejected_credential


async def test_the_session_asks_the_registry_to_recover_the_provider_that_answered():
    """The session may already point at another model; the token to replace belongs to the
    provider whose request failed."""
    storage = _Storage(OAUTH)
    assert await _Session(_registry(storage)).recover(_Message()) is True
    assert storage.calls == [("sub2api-claude", "stale-key")]


async def test_a_message_with_no_provider_recovers_nothing():
    storage = _Storage(OAUTH)
    assert await _Session(_registry(storage)).recover(_Message(provider=None)) is False
    assert storage.calls == []


@pytest.mark.parametrize("attempt,settings,expected", [
    (1, None, False),                                   # already retrying: one rotation per turn
    (0, _Settings(enabled=False), False),               # retries off: nothing would follow it
    (0, _Settings(retries=0), False),                   # no budget: nothing would follow it
    (0, None, True),
])
async def test_a_token_is_only_rotated_when_a_retry_can_follow(attempt, settings, expected):
    storage = _Storage(OAUTH)
    session = _Session(_registry(storage), attempt=attempt, settings=settings)
    assert await session.recover(_Message()) is expected
    assert bool(storage.calls) is expected


@pytest.mark.parametrize("message", [
    _Message(stop="stop"),
    _Message(error="overloaded"),
    _Message(error=None),
])
async def test_only_a_refused_credential_triggers_recovery(message):
    storage = _Storage(OAUTH)
    assert await _Session(_registry(storage)).recover(message) is False
    assert storage.calls == []


async def test_a_registry_without_the_capability_is_tolerated():
    session = _Session(object())
    assert await session.recover(_Message()) is False


class _Turn:
    """An AgentSession as much as the post-run decision reads it."""

    _stopHookContinuationPending = False
    _retryAttempt = 0
    _agentRunAbortRequested = False
    handle = AgentSession._handle_post_agent_run

    def __init__(self, recovered, *, retryable=False):
        self._lastAssistantMessage = _Message()
        self._lastAssistantToolResults = []
        self._recovered, self._retryable = recovered, retryable
        self.prepared = []
        self.agent = SimpleNamespace(hasQueuedMessages=lambda: False)

    def _emit(self, _event):
        pass

    def _is_retryable_error(self, _message):
        return self._retryable

    async def _recover_rejected_credential(self, message):
        return self._recovered

    async def _prepare_retry(self, message):
        self.prepared.append(message)
        return True

    async def _check_compaction(self, _message, _skip_aborted_check=True, _tool_results=None):
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


def _resolution(api_key=None, headers=None):
    from misaka.ai.auth.types import AuthResult, ModelAuth
    return AuthResult(auth=ModelAuth(apiKey=api_key, headers=headers), source="OAuth")


def test_the_key_handed_to_a_request_is_what_gets_remembered():
    """The rejected key has to come from somewhere; resolving auth is the one door it leaves by."""
    registry = _registry(_Storage(OAUTH))
    registry._lastResolvedApiKey.clear()
    registry._rememberResolvedKey("sub2api-claude", _resolution(api_key="handed-out"))
    assert registry._lastResolvedApiKey == {"sub2api-claude": "handed-out"}


def test_a_credential_carried_in_a_header_is_remembered_as_its_token(monkeypatch):
    """A flow that sends its credential as a header leaves apiKey unset. Remembering the header
    string would leave the forced refresh unforced, since the store compares access tokens."""
    from misaka.ai.utils import oauth

    monkeypatch.setattr(oauth, "getOAuthProvider",
                        lambda provider: SimpleNamespace(getApiKey=lambda c: c.access))
    stored = {"type": "oauth", "access": "bare-token", "refresh": "r", "expires": 1}
    registry = _registry(_Storage(stored))
    registry._lastResolvedApiKey.clear()
    registry._rememberResolvedKey("sub2api-claude",
                                  _resolution(headers={"authorization": "Bearer bare-token"}))
    assert registry._lastResolvedApiKey == {"sub2api-claude": "bare-token"}


def test_a_provider_with_no_credential_to_remember_is_left_out(monkeypatch):
    from misaka.ai.utils import oauth

    monkeypatch.setattr(oauth, "getOAuthProvider", lambda provider: None)
    registry = _registry(_Storage({"type": "api_key", "key": "k"}))
    registry._lastResolvedApiKey.clear()
    registry._rememberResolvedKey("sub2api-claude", _resolution(headers={"x": "y"}))
    assert registry._lastResolvedApiKey == {}
    assert registry_module.is_rejected_credential_error("401")

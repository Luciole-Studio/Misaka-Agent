"""What a failed card's reason is classified as.

Everything the classifier used to call a dead credential or an exhausted allowance still is;
the providers' own status shapes are recognised too. A filesystem "Permission denied" is not a
credential, and a message that merely mentions a protocol is not a protocol violation. Faults
whose origin the caller knows (supervisor, finalizer, dispatch) never reach it: they pass
``failure_kind="crash"`` themselves."""
import pytest

from misaka.ai.utils.retry import _NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN
from misaka.core.platform import tasks


@pytest.mark.parametrize("reason,kind", [
    # Real provider wording that no retry can fix.
    ('401 {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}', "auth_or_quota"),
    ("Error code: 401 - {'error': {'message': 'Incorrect API key provided: sk-...', 'code': 'invalid_api_key'}}",
     "auth_or_quota"),
    ("Error code: 429 - {'error': {'message': 'You exceeded your current quota', 'code': 'insufficient_quota'}}",
     "auth_or_quota"),
    ("400 INVALID_ARGUMENT. API key not valid. Please pass a valid API key.", "auth_or_quota"),
    ("403 PERMISSION_DENIED. The caller does not have permission", "auth_or_quota"),
    ('No API key found for "anthropic"', "auth_or_quota"),
    ("Your credit balance is too low to access the Anthropic API", "auth_or_quota"),
    ("OpenAI Codex token refresh failed (401): expired", "auth_or_quota"),
    # Retryable provider trouble.
    ("429 RESOURCE_EXHAUSTED. Resource has been exhausted (e.g. check quota).", "rate_limit"),
    ("Error code: 529 - overloaded_error", "server_error"),
    ("The model is at capacity, please retry", "server_error"),
    ('{"error": {"code": "server_busy"}}', "server_error"),
    ("Our servers are currently busy, try again later", "server_error"),
    ("Authentication Fails, Your api key: ****abcd is invalid", "auth_or_quota"),
    ("invalid_token: the access token expired", "auth_or_quota"),
    # Text that is not about a credential at all.
    ("PermissionError: [Errno 13] Permission denied: '/tmp/out.md'", "failure"),
    ('File "worker.py", line 401, in run\nValueError: bad row', "failure"),
    ("MCP protocol version mismatch: server speaks 2024-11-05", "failure"),
    # Our own wording.
    (tasks.UNSETTLED_REASON, "protocol_violation"),
    ("supervisor: RuntimeError: boom", "crash"),
])
def test_reasons_are_classified_by_what_they_say(reason, kind):
    assert tasks.classify_failure(reason) == kind


def test_every_quota_word_pi_will_not_retry_is_terminal_here():
    for word in _NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN.pattern.split("|"):
        assert tasks.classify_failure(f"provider said: {word}") == "auth_or_quota", word


# One sample per alternative of the classifier as it stood before 2026-09-26: each was a dead
# credential or an exhausted allowance then, and none may quietly become retryable.
PREVIOUSLY_TERMINAL = [
    "Authentication failed", "401 Unauthorized", "unauthorised", "permission_error", "permission error",
    "PermissionDeniedError: 403", "403 Forbidden", "invalid api key", "invalid_api_key", "invalid-token",
    "invalidtoken", "api key missing", "API-key rejected", "insufficient quota", "insufficient_quota",
    "insufficient-quota", "quota exceeded", "You exceeded your current quota", "billing hard limit reached",
    "402 Payment Required", "Your credit balance is too low", "insufficient credits", "insufficient_credit",
]


@pytest.mark.parametrize("reason", PREVIOUSLY_TERMINAL)
def test_nothing_that_was_terminal_became_retryable(reason):
    assert tasks.classify_failure(reason) == "auth_or_quota"

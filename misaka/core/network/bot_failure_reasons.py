# Hermes bda3a363e9695e3f63fce4a7ca85ba17b5fecb61 / tools/bot_failure_reasons.py (MIT); see
# THIRD_PARTY_NOTICES.md. The vocabulary, the classifier and the retry policy, verbatim; what no
# contact turn reaches is left out (``HELD_NOT_SENT``, ``is_auto_retryable``, ``turn_failure_text``,
# ``result_retry_action``, ``delivery_failure_reason``).
"""Typed failure-reason codes for bot turns and relay replies.

A closed vocabulary of machine-readable reason codes carried ALONGSIDE the
free-text ``error`` fields (additive — old consumers keep working). Platform-side
codes are assigned by the transport/relay layer; agent-side codes are derived
from raw agent/provider error text via ``classify_agent_error``. Classifier
precedence is the order of ``_RULES``: auth outranks quota by design — real
provider 401 bodies (e.g. Anthropic) say "invalid, blocked or out of funds".
"""

from __future__ import annotations

import re

# platform-side
RUNTIME_OFFLINE = "runtime_offline"
QUEUED_EXPIRED = "queued_expired"
DELIVERY_TIMEOUT = "delivery_timeout"
AGENT_BLOCKED = "agent_blocked"
CANCELLED = "cancelled"

# agent-side
PROVIDER_AUTH_OR_ACCESS = "provider_auth_or_access"
PROVIDER_QUOTA_LIMIT = "provider_quota_limit"
PROVIDER_RATE_LIMIT = "provider_rate_limit"
PROVIDER_SERVER_ERROR = "provider_server_error"
CONTEXT_OVERFLOW = "context_overflow"
MISSING_CONFIG = "missing_config"
MODEL_UNAVAILABLE = "model_unavailable"
# A spawn site could not resolve the target profile's secret scope: no turn ran, no provider was called.
TARGET_SCOPE_UNRESOLVED = "target_scope_unresolved"
UNKNOWN = "unknown"

ALL_REASONS = frozenset({
    RUNTIME_OFFLINE, QUEUED_EXPIRED, DELIVERY_TIMEOUT, AGENT_BLOCKED, CANCELLED,
    PROVIDER_AUTH_OR_ACCESS, PROVIDER_QUOTA_LIMIT, PROVIDER_RATE_LIMIT,
    PROVIDER_SERVER_ERROR, CONTEXT_OVERFLOW, MISSING_CONFIG, MODEL_UNAVAILABLE,
    TARGET_SCOPE_UNRESOLVED, UNKNOWN,
})

#: Reasons a supervisor may retry automatically without human intervention.
AUTO_RETRYABLE = frozenset({RUNTIME_OFFLINE, DELIVERY_TIMEOUT, PROVIDER_RATE_LIMIT, PROVIDER_SERVER_ERROR})


# Retry session policy: a retried bot turn NEVER mints a fresh session. Transient
# classes resume as-is; context_overflow runs context compression (the one
# sanctioned context mutation) on the same session first; everything else
# (auth/quota/config/model/unknown) is never auto-retried — it can't be fixed by
# a retry and only burns quota.
# See #93091.
RETRY_RESUME = "resume"
RETRY_COMPRESS_THEN_RESUME = "compress_then_resume"
RETRY_NONE = "none"


def retry_action(reason: str) -> str:
    """Map a failure reason to the bot-turn retry action (see policy above)."""
    if reason in AUTO_RETRYABLE:
        return RETRY_RESUME
    if reason == CONTEXT_OVERFLOW:
        return RETRY_COMPRESS_THEN_RESUME
    return RETRY_NONE


_STATUS = r"(?:error code:?\s*|status(?:\s*code)?:?\s*|http\s*)"

# Ordered (pattern, code) — first match wins.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pat, re.IGNORECASE), code)
    for pat, code in (
        # FIRST: the copy says "API key" and "restart", so it must be claimed before the auth rules.
        # ``UnscopedSecretError`` names the secret when it has one ("this profile's OPENROUTER_API_KEY");
        # the ``served_profile_child_env`` detail rides in tracebacks.
        (r"could not read this profile's [^(]+\(an internal profile-scoping bug"  # noqa: ISC004 - verbatim
         r"|served_profile_child_env\(inherit_credentials=true\)", TARGET_SCOPE_UNRESOLVED),
        (rf"authentication_error|invalid api key|{_STATUS}(?:401|403)\b", PROVIDER_AUTH_OR_ACCESS),
        (rf"{_STATUS}402\b|out of funds|quota|balance", PROVIDER_QUOTA_LIMIT),
        (rf"{_STATUS}429\b|rate.?limit", PROVIDER_RATE_LIMIT),
        # ``server[ _]?error`` / ``overloaded_error`` are the providers' own JSON spellings of the
        # agent's typed ``server_error`` / ``overloaded`` verdicts (FailoverReason), which the
        # in-process lanes classify from ``failure_reason`` rather than a status number.
        (rf"{_STATUS}5\d{{2}}\b|server[ _]?error|overloaded", PROVIDER_SERVER_ERROR),
        (r"context length|context_overflow|maximum context", CONTEXT_OVERFLOW),
        (r"no llm provider configured|missing config|no access token", MISSING_CONFIG),
        (r"model .*(not found|does not exist)|model_not_found", MODEL_UNAVAILABLE),
    )
)


def classify_agent_error(text: str) -> str:
    """Map raw agent/provider error text to a closed reason code (``unknown`` when unmatched/empty)."""
    raw = str(text or "")
    if raw.strip():
        for pattern, code in _RULES:
            if pattern.search(raw):
                return code
    return UNKNOWN

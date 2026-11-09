"""A gateway's mid-stream drop is a transient transport error and gets the retry policy.

2026-09-23: sub2api relayed an Anthropic stream and reported the drop as
``{"error":{"message":"upstream stream disconnected: unexpected EOF","type":"stream_read_error"}}``.
pi's retryable pattern has no entry for that wording, so the Sister's turn ended on the error
and she sat idle until someone typed "继续"."""
import pytest

from misaka.ai.types import AssistantMessage
from misaka.ai.utils.retry import isRetryableAssistantError


def _error(text):
    return AssistantMessage(content=[], api="anthropic-messages", provider="sub2api-claude", model="claude-opus-5",
                            usage={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                                   "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
                            stopReason="error", errorMessage=text, timestamp=0)


@pytest.mark.parametrize("text", [
    '{"error":{"message":"upstream stream disconnected: unexpected EOF","type":"stream_read_error"},"type":"error"}',
    "upstream stream disconnected",
    "read: unexpected EOF",
])
def test_gateway_stream_drop_is_retryable(text):
    assert isRetryableAssistantError(_error(text))


def test_quota_walls_are_still_not_retryable():
    assert not isRetryableAssistantError(_error("You have hit your usage limit: quota exceeded"))
    assert not isRetryableAssistantError(_error("insufficient_quota"))

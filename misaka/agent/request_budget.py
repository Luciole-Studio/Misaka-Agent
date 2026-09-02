"""Per-provider-request hard token ceiling.

This limiter wraps ``streamFn`` itself: every provider request is bounded from
the exact context that will be sent (system prompt, messages and tool schemas)
and from usage already reported by earlier requests.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import secrets
from collections.abc import Mapping
from typing import Any

from misaka.utils.values import read_field

CONTEXT_FRAMING_TOKENS = 4096


def usage_tokens(usage: Any) -> int:
    total = read_field(usage, "totalTokens")
    if total is not None:
        return max(0, int(total or 0))
    return max(
        0,
        sum(
            int(read_field(usage, key, 0) or 0)
            for key in (
                "input",
                "output",
                "cacheRead",
                "cacheWrite",
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        ),
    )


def context_token_upper_bound(context: Any) -> int:
    """Conservatively bound tokenized input, including tools.

    Supported provider tokenizers cannot produce more ordinary text tokens
    than the number of UTF-8 bytes.  The fixed allowance covers provider
    message/tool framing that is not represented in ``Context`` itself.
    """

    def jsonable(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", exclude_none=True)
        if isinstance(value, Mapping):
            return {str(key): jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [jsonable(item) for item in value]
        return value

    tools = []
    for tool in read_field(context, "tools", []) or []:
        parameters = read_field(tool, "parameters", {})
        if hasattr(tool, "parameters_json_schema"):
            parameters = tool.parameters_json_schema()
        elif isinstance(parameters, type) and hasattr(parameters, "model_json_schema"):
            parameters = parameters.model_json_schema()
        tools.append(
            {
                "name": str(read_field(tool, "name", "")),
                "description": str(read_field(tool, "description", "")),
                "parameters": jsonable(parameters),
            }
        )
    payload = {
        "systemPrompt": read_field(context, "systemPrompt"),
        "messages": jsonable(read_field(context, "messages", []) or []),
        "tools": tools,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return len(encoded) + CONTEXT_FRAMING_TOKENS


def _limited_options(options: Any, maximum: int) -> Any:
    """Copy provider options and disable unbudgeted reasoning/retries."""

    if options is None:
        from misaka.ai.types import SimpleStreamOptions

        limited: Any = SimpleStreamOptions()
    elif hasattr(options, "model_copy"):
        limited = options.model_copy(deep=True)
    elif isinstance(options, dict):
        limited = dict(options)
    else:
        limited = copy.copy(options)

    configured = read_field(options, "maxTokens") if options is not None else None
    if configured is not None:
        try:
            configured_int = int(configured)
        except (TypeError, ValueError, OverflowError):
            configured_int = 0
        if configured_int > 0:
            maximum = min(maximum, configured_int)

    updates = {
        "maxTokens": max(1, int(maximum)),
        # Provider adapters add a separate thinking allowance when reasoning
        # is enabled.  A hard slice therefore runs with reasoning disabled.
        "reasoning": None,
        "thinkingBudgets": None,
        # A transparent provider retry could spend the same slice twice while
        # exposing usage only for the final attempt.
        "maxRetries": 0,
    }
    if isinstance(limited, dict):
        limited.update(updates)
    else:
        for key, value in updates.items():
            setattr(limited, key, value)
    return limited


class TurnBudgetLimiter:
    def __init__(self, limit: int):
        if int(limit) <= 0:
            raise ValueError("Token limit must be positive")
        self.limit = int(limit)
        self.used = 0
        self.in_flight: dict[str, int] = {}

    def observe(self, event: Any) -> None:
        if str(read_field(event, "type", "")) != "message_end":
            return
        message = read_field(event, "message")
        if str(read_field(message, "role", "")) == "assistant":
            self.used += usage_tokens(read_field(message, "usage", {}) or {})

    @property
    def accounted(self) -> int:
        """Settled usage plus conservative outstanding reservations."""

        return self.used + sum(self.in_flight.values())

    def allowance(self, context: Any) -> int:
        remaining = self.limit - self.used - sum(self.in_flight.values())
        maximum = remaining - context_token_upper_bound(context)
        if maximum <= 0:
            raise RuntimeError(
                "Shared token budget is too small for the next model request"
            )
        return maximum

    def reserve(self, context: Any, configured: Any = None) -> tuple[str, int]:
        maximum = self.allowance(context)
        try:
            configured_int = int(configured) if configured is not None else 0
        except (TypeError, ValueError, OverflowError):
            configured_int = 0
        if configured_int > 0:
            maximum = min(maximum, configured_int)
        token = secrets.token_hex(8)
        self.in_flight[token] = context_token_upper_bound(context) + maximum
        return token, maximum

    def settle(self, token: str, message: Any = None, *, failed: bool = False) -> None:
        reserved = self.in_flight.pop(token, 0)
        if not reserved:
            return
        actual = usage_tokens(read_field(message, "usage", {}) or {}) if message is not None else 0
        # Missing/error usage is not evidence that the provider spent nothing.
        # Retain the worst-case reservation so a failed transparent request can
        # never make the slice available a second time.
        self.used += reserved if failed or actual <= 0 else actual


class _BudgetedStream:
    """Proxy an assistant stream and settle its reservation exactly once."""

    def __init__(self, stream: Any, limiter: TurnBudgetLimiter, token: str):
        self._stream = stream
        self._limiter = limiter
        self._token = token
        result = stream.result()
        self._future = (
            result
            if hasattr(result, "add_done_callback")
            else asyncio.ensure_future(result)
        )
        if self._future.done():
            self._settle(self._future)
        else:
            self._future.add_done_callback(self._settle)

    def _settle(self, future: Any) -> None:
        token, self._token = self._token, ""
        if not token:
            return
        try:
            message = future.result()
        except BaseException:  # noqa: BLE001 - charge the reservation on uncertainty
            self._limiter.settle(token, failed=True)
        else:
            failed = str(read_field(message, "stopReason", "")) in {"error", "aborted"}
            self._limiter.settle(token, message, failed=failed)

    def result(self) -> Any:
        return self._future

    def __aiter__(self) -> Any:
        return self._stream.__aiter__()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def install_turn_budget(session: Any, limit: int | None = None) -> TurnBudgetLimiter | None:
    """Install one idempotent dynamic limiter on an engine session."""

    if limit is None:
        raw = os.environ.get("MISAKA_TURN_TOKEN_LIMIT", "")
        if not raw.isdigit() or int(raw) <= 0:
            return None
        limit = int(raw)
    if int(limit) <= 0:
        return None

    existing = getattr(session.agent, "_misaka_turn_budget", None)
    if isinstance(existing, TurnBudgetLimiter):
        return existing

    limiter = TurnBudgetLimiter(int(limit))
    original = session.agent.streamFn

    async def limited_stream(model: Any, context: Any, options: Any = None) -> Any:
        token, maximum = limiter.reserve(context, read_field(options, "maxTokens"))
        try:
            response = original(model, context, _limited_options(options, maximum))
            if inspect.isawaitable(response):
                response = await response
            return _BudgetedStream(response, limiter, token)
        except BaseException:
            limiter.settle(token, failed=True)
            raise

    session.agent.streamFn = limited_stream
    session.agent._misaka_turn_budget = limiter
    return limiter


__all__ = [
    "CONTEXT_FRAMING_TOKENS",
    "TurnBudgetLimiter",
    "context_token_upper_bound",
    "install_turn_budget",
    "usage_tokens",
]

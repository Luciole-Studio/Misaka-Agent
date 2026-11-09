"""Settings one session runs with instead of the global ones.

A research run chooses its own context-compaction threshold and per-request output limit; they
apply to the sessions working for that run and to nothing else. A session carries them here,
keyed by its session id, and the subsystems read them without knowing why they exist: LCM reads
``context_threshold`` (``extensions/misaka_lcm/host/config_bridge``), and every request of the
session is capped at ``max_output_tokens`` -- never above what the model itself allows.

A session built from a ``SessionSpec`` with ``overrides`` gets them through this module's part;
a session that already exists (the window where ``/research`` was typed) calls ``apply`` and
releases when the run leaves it. A parent hands them to a child process as ``ENV``.
"""
from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from misaka.utils.values import read_field

KEYS = ("context_threshold", "max_output_tokens")
ENV = "MISAKA_SESSION_OVERRIDES"

_BY_SESSION: dict[str, dict[str, Any]] = {}


def _session_id(session: Any) -> str:
    try:
        return str(session.sessionManager.getSessionId())
    except Exception:  # noqa: BLE001 - a session without an id carries no overrides
        return ""


def clean(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Only the known keys, and only those that are set."""
    return {key: values[key] for key in KEYS if values and values.get(key) is not None}


def value(session_id: str | None, key: str) -> Any:
    """The session's own value for ``key``, or None when it runs with the global one."""
    return (_BY_SESSION.get(str(session_id or "")) or {}).get(key)


def _capped(options: Any, cap: int) -> Any:
    current = read_field(options, "maxTokens") if options is not None else None
    if isinstance(current, int) and 0 < current <= cap:
        return options
    if options is None:
        from misaka.ai.types import SimpleStreamOptions

        return SimpleStreamOptions(maxTokens=cap)
    if hasattr(options, "model_copy"):
        return options.model_copy(update={"maxTokens": cap})
    if isinstance(options, dict):
        return {**options, "maxTokens": cap}
    capped = copy.copy(options)
    capped.maxTokens = cap
    return capped


def _install_output_cap(session: Any) -> None:
    """Wrap the session's stream once; the wrapper reads the session's current override per
    request, so applying or releasing never has to unwrap a stream others may have wrapped since."""
    agent = session.agent
    if getattr(agent, "_misaka_output_cap", False):
        return
    inner = agent.streamFunction

    def stream(model: Any, context: Any, options: Any = None) -> Any:
        limit = value(_session_id(session), "max_output_tokens")
        if limit:
            own = int(read_field(model, "maxTokens", 0) or 0)
            options = _capped(options, min(int(limit), own) if own > 0 else int(limit))
        return inner(model, context, options)

    agent.streamFunction = stream
    agent._misaka_output_cap = True


def apply(session: Any, overrides: Mapping[str, Any] | None) -> Callable[[], None]:
    """Run ``session`` with ``overrides`` until the returned release is called."""
    values = clean(overrides)
    session_id = _session_id(session)
    if not values or not session_id:
        return lambda: None
    _install_output_cap(session)
    _BY_SESSION[session_id] = values

    def release() -> None:
        if _BY_SESSION.get(session_id) is values:
            del _BY_SESSION[session_id]

    return release


def to_env(overrides: Mapping[str, Any] | None) -> dict[str, str]:
    """The hand-off for a child process whose session should run with ``overrides``."""
    values = clean(overrides)
    return {ENV: json.dumps(values)} if values else {}


def from_env() -> dict[str, Any]:
    """What a parent handed this process, if anything."""
    try:
        values = json.loads(os.environ.get(ENV) or "{}")
    except ValueError:
        return {}
    return clean(values) if isinstance(values, dict) else {}


class _OverridesPart:
    """Applies a spec's overrides to its session, and again after a session switch."""

    def __init__(self, overrides: dict[str, Any]) -> None:
        self.overrides = overrides
        self.tools: list[Any] = []
        self.commands: list[Any] = []
        self.session: Any = None
        self._release: Callable[[], None] = lambda: None

    def attach(self, session: Any) -> None:
        self.session = session

    async def session_start(self, _event: Any, _ctx: Any) -> None:
        self._release()
        self._release = apply(self.session, self.overrides)

    async def session_shutdown(self, _event: Any, _ctx: Any) -> None:
        self._release()
        self._release = lambda: None


def part(spec: Any) -> _OverridesPart | None:
    overrides = clean(dict(spec.overrides or ()))
    return _OverridesPart(overrides) if overrides else None


__all__ = ["ENV", "KEYS", "apply", "clean", "from_env", "part", "to_env", "value"]

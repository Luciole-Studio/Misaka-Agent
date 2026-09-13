"""HTTP phase policy and an awaited whole-tool deadline; no second I/O owner."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from contextvars import ContextVar

import httpx

PHASES = ("connect", "read", "write", "pool")
OPERATION_DEFAULTS = {
    "default": 180.0,
    "web_search": 180.0, "web_extract": 300.0, "web_fetch": 120.0,
    "download_file": 600.0, "download_transfer": 300.0,
    "firecrawl_scrape": 60.0, "ddgs": 30.0,
    "x_search": 600.0,
    "browser_navigate": 180.0, "browser_snapshot": 120.0, "browser_vision": 300.0,
    "browser_exec": 1900.0, "browser_cdp": 320.0, "browser_dialog": 60.0,
    "browser_click": 120.0, "browser_type": 120.0, "browser_press": 120.0,
    "browser_back": 120.0, "browser_scroll": 120.0, "browser_console": 120.0, "browser_get_images": 120.0,
}
_deadline: ContextVar[tuple[str, float, float] | None] = ContextVar("web_deadline", default=None)


class WebOperationTimeout(TimeoutError):
    def __init__(self, name, seconds):
        super().__init__(f"{name} timed out after {seconds:g}s (whole operation, including retries). "
                         f"Set operation_timeout.{name} with misaka web to change the limit.")


def _seconds(value, label, *, nullable=False, positive=False):
    if value is None and nullable:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not 0 <= value <= sys.float_info.max or (positive and value == 0)):
        requirement = "positive" if positive else "nonnegative"
        raise ValueError(f"{label} must be a finite {requirement} number of seconds"
                         + (" or null" if nullable else ""))
    return value


def validate_setting(section, key, value):
    """Shared file/CLI validation. Never coerce false, zero or null by truthiness."""
    label = f"{section}.{key}"
    if section == "operation_timeout":
        if key not in OPERATION_DEFAULTS:
            raise ValueError(f"Unknown operation timeout: {key}")
        _seconds(value, label)
    elif key == "ddgs":
        # The native library exposes one scalar timeout, not HTTPX's four phases.
        _seconds(value, label, positive=True)
    elif isinstance(value, dict):
        for phase, seconds in value.items():
            if phase not in PHASES:
                raise ValueError(f"Unknown HTTP timeout phase: {label}.{phase}")
            _seconds(seconds, f"{label}.{phase}", nullable=True)
    else:
        _seconds(value, label, nullable=True)
    return value


def _section(name):
    from misaka.core.web.config import web_config

    document = web_config(strict=True)
    section = document.get(name, {})
    if not isinstance(section, dict):
        raise ValueError(f"{name} must be an object")  # noqa: TRY004 - invalid configuration document
    for key, value in section.items():
        validate_setting(name, key, value)
    return section


def http_timeout(provider, default):
    """Caller default < global phase overrides < explicit provider fields."""
    section = _section("http_timeout")
    overrides = {}
    for key in ("default", provider):
        if key in section:
            value = section[key]
            overrides.update(value if isinstance(value, dict) else dict.fromkeys(PHASES, value))
    values = httpx.Timeout(default).as_dict() | overrides
    for phase, seconds in values.items():
        _seconds(seconds, f"http_timeout.{provider}.{phase}", nullable=True)
    return httpx.Timeout(**values) if overrides else default


def ddgs_request_timeout():
    """Transport-native timeout, passed to the isolated worker as data."""
    return _section("http_timeout").get("ddgs", 10)


def operation_seconds(name):
    section = _section("operation_timeout")
    default = OPERATION_DEFAULTS.get(name, OPERATION_DEFAULTS["default"])
    return section.get(name, section.get("default", default))


def remaining_operation():
    state = _deadline.get()
    if state is None:
        return None
    name, seconds, until = state
    remaining = until - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise WebOperationTimeout(name, seconds)
    return remaining


@asynccontextmanager
async def operation_deadline(name):
    # Validate phase settings before dispatch can mistake invalid config for a
    # vendor failure and try the same broken configuration around the rescue ring.
    _section("http_timeout")
    seconds = operation_seconds(name)
    if seconds == 0:
        raise WebOperationTimeout(name, seconds)
    loop = asyncio.get_running_loop()
    until = loop.time() + seconds
    limit = asyncio.timeout_at(until)
    token = _deadline.set((name, seconds, until))
    try:
        try:
            async with limit:
                yield
        except TimeoutError:
            if not limit.expired():
                raise  # An inner operation's timeout keeps its original identity.
        if limit.expired() or loop.time() >= until:
            # Also covers a provider that catches cancellation and returns a result.
            raise WebOperationTimeout(name, seconds)
    finally:
        _deadline.reset(token)

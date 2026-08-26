"""Small Claude-compatible hook runner used by sub-agent sessions.

Hook failures are fail-open unless the hook explicitly denies an operation.
That mirrors Claude's distinction between a blocking hook result and a
non-blocking hook execution error.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import os
import re
import signal
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

HookResult = dict[str, Any]
HookEvaluator = Callable[..., Awaitable[Any] | Any]

_hook_evaluator: HookEvaluator | None = None
_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FORBIDDEN_HEADERS = frozenset({"host", "content-length", "transfer-encoding"})


def set_hook_evaluator(callback: HookEvaluator | None) -> None:
    """Set the process-wide evaluator used by ``prompt`` and ``agent`` hooks."""

    global _hook_evaluator
    _hook_evaluator = callback


def _result(
    *,
    allowed: bool = True,
    decision: str = "passthrough",
    additional_context: str | None = None,
    reason: str | None = None,
    updated_input: Mapping[str, Any] | None = None,
) -> HookResult:
    return {
        "allowed": allowed,
        "decision": decision,
        "additional_context": additional_context,
        "reason": reason,
        "updated_input": dict(updated_input) if updated_input is not None else None,
    }


def _json_safe(value: Any, seen: set[int]) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, os.PathLike):
        return os.fspath(value)

    identity = id(value)
    if identity in seen:
        return "<recursive>"
    if isinstance(value, Mapping):
        seen.add(identity)
        try:
            return {
                str(key): _json_safe(item, seen)
                for key, item in value.items()
            }
        finally:
            seen.discard(identity)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        seen.add(identity)
        try:
            return [_json_safe(item, seen) for item in value]
        finally:
            seen.discard(identity)
    if isinstance(value, (set, frozenset)):
        seen.add(identity)
        try:
            return [_json_safe(item, seen) for item in value]
        finally:
            seen.discard(identity)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _json_safe(dump(), seen)
        except Exception:  # noqa: BLE001 - fall through to a string
            pass
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return f"<{type(value).__name__}>"


def json_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded-shape JSON-safe copy of hook input data."""

    normalized = _json_safe(payload, set())
    return normalized if isinstance(normalized, dict) else {}


def parse_hook_output(value: Any, *, expected_event: str | None = None) -> HookResult:
    """Normalize command, HTTP, prompt, and agent output to one result shape."""

    if value is None or value == "":
        return _result()
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return _result()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
            candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
            try:
                value = json.loads(candidate) if candidate.startswith("{") else None
            except json.JSONDecodeError:
                value = None
            if value is None:
                return _result(additional_context=text)
    if not isinstance(value, Mapping):
        return _result(reason="hook returned a non-object result")

    output = dict(value)
    specific = output.get("hookSpecificOutput")
    if specific is not None and not isinstance(specific, Mapping):
        return _result(reason="hookSpecificOutput must be an object")
    specific = dict(specific or {})
    returned_event = specific.get("hookEventName")
    if expected_event and returned_event and returned_event != expected_event:
        return _result(reason=f"hook returned event {returned_event!r}, expected {expected_event!r}")

    decision = specific.get("permissionDecision", output.get("permissionDecision"))
    reason = specific.get("permissionDecisionReason") or output.get("reason")
    updated_input = specific.get("updatedInput", output.get("updatedInput"))
    additional_context = specific.get("additionalContext", output.get("additionalContext"))

    permission_request = specific.get("decision")
    if isinstance(permission_request, Mapping):
        decision = permission_request.get("behavior", decision)
        reason = permission_request.get("message") or reason
        updated_input = permission_request.get("updatedInput", updated_input)

    legacy_decision = output.get("decision")
    if decision is None and legacy_decision in {"approve", "block"}:
        decision = "allow" if legacy_decision == "approve" else "deny"
    if decision is None and "ok" in output:
        decision = "allow" if output.get("ok") is True else "deny"

    if decision not in {None, "allow", "deny", "ask", "passthrough"}:
        return _result(reason=f"unknown hook decision: {decision}")
    decision = decision or "passthrough"
    if output.get("continue") is False:
        decision = "deny"
        reason = output.get("stopReason") or reason or "hook stopped continuation"
    if decision == "deny":
        reason = reason or "blocked by hook"

    return _result(
        allowed=decision not in {"deny", "ask"},
        decision=decision,
        additional_context=str(additional_context) if additional_context is not None else None,
        reason=str(reason) if reason is not None else None,
        updated_input=updated_input if isinstance(updated_input, Mapping) else None,
    )


def _timeout(hook: Mapping[str, Any]) -> float:
    default = 60.0 if hook.get("type") == "agent" else 30.0
    try:
        value = float(hook.get("timeout", default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


async def _kill_hook_process(process: asyncio.subprocess.Process) -> None:
    if os.name != "nt":
        # The shell leader may already have exited while descendants still own
        # the process group, so POSIX cleanup must not key only on returncode.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    elif process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    if process.returncode is None:
        await process.wait()


async def _command_hook_with_status(
    hook: Mapping[str, Any],
    payload: Mapping[str, Any],
    cwd: str | Path | None,
    environ: Mapping[str, str],
) -> tuple[HookResult, int | None]:
    command = hook.get("command")
    if not isinstance(command, str) or not command.strip():
        return _result(reason="command hook is missing command"), None
    try:
        normalized_payload = json_payload(payload)
        stdin = (
            json.dumps(normalized_payload, ensure_ascii=False) + "\n"
        ).encode()
    except Exception as error:  # noqa: BLE001 - never spawn on bad input
        return _result(reason=f"command hook failed: {error}"), None
    process: asyncio.subprocess.Process | None = None
    try:
        process_args = {
            "cwd": str(cwd) if cwd is not None else None,
            "env": dict(environ),
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "start_new_session": os.name != "nt",
        }
        if hook.get("shell") == "powershell":
            process = await asyncio.create_subprocess_exec(
                "pwsh", "-NoProfile", "-Command", command, **process_args
            )
        else:
            executable = environ.get("SHELL")
            process = await asyncio.create_subprocess_shell(
                command,
                executable=executable if executable and os.path.isabs(executable) else None,
                **process_args,
            )
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), _timeout(hook))
    except TimeoutError:
        if process is not None:
            await _kill_hook_process(process)
        return _result(reason="command hook timed out"), None
    except asyncio.CancelledError:
        if process is not None:
            await _kill_hook_process(process)
        raise
    except Exception as error:  # noqa: BLE001 - spawned hooks must be reaped
        if process is not None:
            await _kill_hook_process(process)
        return _result(reason=f"command hook failed: {error}"), None

    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace").strip()
    if process.returncode == 2:
        parsed = parse_hook_output(
            out, expected_event=str(payload.get("hook_event_name") or "") or None
        )
        if parsed["decision"] in {"deny", "ask"}:
            return parsed, 2
        return (
            _result(
                allowed=False,
                decision="deny",
                reason=(err.splitlines()[-1] if err else None)
                or parsed["reason"]
                or parsed["additional_context"]
                or "blocked by hook",
            ),
            2,
        )
    if process.returncode != 0:
        return (
            _result(
                reason=(err.splitlines()[-1] if err else None)
                or f"command hook exited with status {process.returncode}"
            ),
            process.returncode,
        )
    return (
        parse_hook_output(
            out,
            expected_event=str(payload.get("hook_event_name") or "") or None,
        ),
        0,
    )


async def _command_hook(
    hook: Mapping[str, Any],
    payload: Mapping[str, Any],
    cwd: str | Path | None,
    environ: Mapping[str, str],
) -> HookResult:
    result, _exit_code = await _command_hook_with_status(
        hook, payload, cwd, environ
    )
    return result


async def execute_async_command_hook(
    hook: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    cwd: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[HookResult, int | None]:
    """Run a brokered command hook while preserving its wakeup exit code."""

    return await _command_hook_with_status(
        hook,
        payload,
        cwd,
        environ if environ is not None else os.environ,
    )


def _interpolate_header(value: str, allowed: set[str], environ: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return environ.get(name, "") if name in allowed else ""

    return _ENV_PATTERN.sub(replace, value).replace("\r", "").replace("\n", "").replace("\x00", "")


async def _resolve_host(host: str, port: int) -> list[str]:
    records = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return sorted({record[4][0] for record in records})


async def _validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("HTTP hook URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("HTTP hook URL credentials are not allowed")
    host = parsed.hostname.rstrip(".").casefold()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("HTTP hook URL resolves to a local address")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await _resolve_host(host, port)
    except (OSError, socket.gaierror) as error:
        raise ValueError(f"HTTP hook host resolution failed: {error}") from error
    if not addresses:
        raise ValueError("HTTP hook host did not resolve")
    for raw in addresses:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
        if not address.is_global:
            raise ValueError("HTTP hook URL resolves to a local or private address")


async def _post_http(url: str, body: Mapping[str, Any], headers: Mapping[str, str], timeout: float) -> httpx.Response:
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=timeout) as client:
        return await client.post(url, json=dict(body), headers=dict(headers))


async def _http_hook(
    hook: Mapping[str, Any], payload: Mapping[str, Any], environ: Mapping[str, str]
) -> HookResult:
    url = hook.get("url")
    if not isinstance(url, str):
        return _result(reason="HTTP hook is missing url")
    try:
        normalized_payload = json_payload(payload)
        await _validate_public_url(url)
        allowed = {str(name) for name in hook.get("allowedEnvVars", []) if isinstance(name, str)}
        headers: dict[str, str] = {"Content-Type": "application/json"}
        configured = hook.get("headers", {})
        if not isinstance(configured, Mapping):
            raise ValueError("HTTP hook headers must be an object")  # noqa: TRY004 - callers treat bad input as ValueError
        for raw_name, raw_value in configured.items():
            name = str(raw_name)
            if not _HEADER_NAME.fullmatch(name) or name.casefold() in _FORBIDDEN_HEADERS:
                raise ValueError(f"HTTP hook header is not allowed: {name}")
            headers[name] = _interpolate_header(str(raw_value), allowed, environ)
        response = await _post_http(
            url, normalized_payload, headers, _timeout(hook)
        )
    except Exception as error:  # noqa: BLE001 - hook failures are fail-open
        return _result(reason=f"HTTP hook failed: {error}")
    if 300 <= response.status_code < 400:
        return _result(reason="HTTP hook redirect was blocked")
    if not 200 <= response.status_code < 300:
        return _result(reason=f"HTTP hook returned status {response.status_code}")
    return parse_hook_output(response.text, expected_event=str(payload.get("hook_event_name") or "") or None)


async def _evaluate_hook(
    hook: Mapping[str, Any], payload: Mapping[str, Any], evaluator: HookEvaluator | None
) -> HookResult:
    callback = evaluator or _hook_evaluator
    kind = str(hook.get("type") or "")
    if not callable(callback):
        return _result(reason=f"{kind} hook evaluator is not configured")
    prompt = hook.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return _result(reason=f"{kind} hook is missing prompt")
    try:
        normalized_payload = json_payload(payload)
        arguments = json.dumps(
            normalized_payload, ensure_ascii=False, separators=(",", ":")
        )
        rendered_prompt = (
            prompt.replace("$ARGUMENTS", arguments)
            if "$ARGUMENTS" in prompt
            else f"{prompt}\n\nHook input JSON:\n{arguments}"
        )
        value = callback(
            kind, rendered_prompt, normalized_payload, dict(hook)
        )
        if inspect.isawaitable(value):
            value = await asyncio.wait_for(value, _timeout(hook))
        return parse_hook_output(value, expected_event=str(payload.get("hook_event_name") or "") or None)
    except TimeoutError:
        return _result(reason=f"{kind} hook timed out")
    except Exception as error:  # evaluator failures must not stop the child
        return _result(reason=f"{kind} hook failed: {error}")


async def execute_hook(
    hook: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    evaluator: HookEvaluator | None = None,
    cwd: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> HookResult:
    """Execute one persisted hook definition and return a normalized result."""

    kind = str(hook.get("type") or "command").casefold()
    resolved_env = environ if environ is not None else os.environ
    if kind == "command":
        return await _command_hook(hook, payload, cwd, resolved_env)
    if kind == "http":
        return await _http_hook(hook, payload, resolved_env)
    if kind in {"prompt", "agent"}:
        return await _evaluate_hook(hook, payload, evaluator)
    return _result(reason=f"unknown hook type: {kind}")


run_hook = execute_hook


__all__ = [
    "HookEvaluator",
    "HookResult",
    "execute_async_command_hook",
    "execute_hook",
    "json_payload",
    "parse_hook_output",
    "run_hook",
    "set_hook_evaluator",
]

"""Small Claude-compatible hook runner used by sub-agent sessions.

Hook failures are fail-open unless the hook explicitly denies an operation.
That mirrors Claude's distinction between a blocking hook result and a
non-blocking hook execution error.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
from pydantic import AnyUrl

from misaka.core.tools._web.bounded import pin_to_address, vet_public_url

HookResult = dict[str, Any]
HookEvaluator = Callable[..., Awaitable[Any] | Any]

_hook_evaluator: HookEvaluator | None = None
_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FORBIDDEN_HEADERS = frozenset({"host", "content-length", "transfer-encoding"})

# A hook's output goes straight into the model's context, so it is bounded the way every
# other agent-facing output in this project is bounded. Without a cap a single hook can
# spend the whole context window -- and the caller has no way to tell that it did.
HOOK_MAX_OUTPUT_CHARS = 32_000

# One default per hook type, read by both the runtime and the config validator; they used
# to disagree (the validator assumed 60 for everything).
HOOK_DEFAULT_TIMEOUTS = {"command": 600.0, "agent": 60.0}
HOOK_DEFAULT_TIMEOUT = 30.0


def clamp_hook_output(text: str) -> str:
    """Keep the head and the tail: a hook's verdict is usually in one or the other."""
    if len(text) <= HOOK_MAX_OUTPUT_CHARS:
        return text
    keep = HOOK_MAX_OUTPUT_CHARS // 2
    dropped = len(text) - 2 * keep
    return f"{text[:keep]}\n[... {dropped} characters of hook output dropped ...]\n{text[-keep:]}"


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
) -> HookResult:
    return {
        "allowed": allowed,
        "decision": decision,
        "additional_context": additional_context,
        "reason": reason,
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
        except Exception:  # noqa: BLE001, S110 - fall through to a string
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
                return _result(additional_context=clamp_hook_output(text))
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
    additional_context = specific.get("additionalContext", output.get("additionalContext"))

    permission_request = specific.get("decision")
    if isinstance(permission_request, Mapping):
        decision = permission_request.get("behavior", decision)
        reason = permission_request.get("message") or reason
        if "updatedInput" in permission_request:
            specific["updatedInput"] = permission_request["updatedInput"]

    legacy_decision = output.get("decision")
    if legacy_decision is not None and not isinstance(legacy_decision, str):
        return _result(reason="hook decision must be a string")
    if decision is None and isinstance(legacy_decision, str) and legacy_decision in {"approve", "block"}:
        decision = "allow" if legacy_decision == "approve" else "deny"
    if decision is None and "ok" in output:
        decision = "allow" if output.get("ok") is True else "deny"

    if decision is not None and (not isinstance(decision, str) or decision not in {"allow", "deny", "ask", "passthrough"}):
        return _result(reason=f"unknown hook decision: {decision}")
    decision = decision or "passthrough"
    if output.get("continue") is False:
        decision = "deny"
        reason = output.get("stopReason") or reason or "hook stopped continuation"
    if decision == "deny":
        reason = reason or "blocked by hook"

    result = _result(
        allowed=decision not in {"deny", "ask"},
        decision=decision,
        additional_context=(clamp_hook_output(str(additional_context))
                            if additional_context is not None else None),
        reason=str(reason) if reason is not None else None,
    )
    if "updatedInput" in specific:
        if not isinstance(specific["updatedInput"], Mapping):
            return _result(reason="hook updatedInput must be an object")
        result["updated_input"] = dict(specific["updatedInput"])
    if "updatedMCPToolOutput" in specific:
        result["updated_mcp_tool_output"] = specific["updatedMCPToolOutput"]
    if output.get("continue") is False:
        result["prevent_continuation"] = True
    return result


def _timeout(hook: Mapping[str, Any]) -> float:
    default = HOOK_DEFAULT_TIMEOUTS.get(str(hook.get("type")), HOOK_DEFAULT_TIMEOUT)
    try:
        value = float(hook.get("timeout", default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


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
    # VCS lifecycle hooks return a path on stdout, not the model-hook schema.
    if payload.get("hook_event_name") in {"WorktreeCreate", "WorktreeRemove"}:
        result = _result()
        result["output"] = clamp_hook_output(out)
        result["reason"] = stderr.decode("utf-8", errors="replace").strip() or None
        return result, process.returncode
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


async def _post_http(
    url: str, address: str, body: Mapping[str, Any], headers: Mapping[str, str], timeout: float
) -> httpx.Response:
    """POST to the address vetting resolved, not to whatever the resolver says next.

    Vetting a name and then letting httpx resolve it again leaves the window a
    rebinding resolver needs: the check sees a public answer, the socket gets a
    private one. Dialling the checked address closes it; ``Host`` and SNI keep
    virtual-host routing and certificate verification working against the name.
    """
    dial_url, dial_headers, extensions = pin_to_address(url, address, dict(headers))
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=timeout) as client:
        return await client.post(dial_url, json=dict(body), headers=dial_headers, extensions=extensions)


async def _http_hook(
    hook: Mapping[str, Any], payload: Mapping[str, Any], environ: Mapping[str, str]
) -> HookResult:
    url = hook.get("url")
    if not isinstance(url, str):
        return _result(reason="HTTP hook is missing url")
    try:
        normalized_payload = json_payload(payload)
        addresses = await vet_public_url(url)
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
            url, addresses[0], normalized_payload, headers, _timeout(hook)
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
        # Model hooks have a different schema from command/HTTP hooks. In
        # particular {"ok":"false"} is a model error, not a blocking verdict.
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, Mapping) or not isinstance(value.get("ok"), bool):
            return _result(reason=f"{kind} hook response must contain boolean ok")
        if "reason" in value and not isinstance(value["reason"], str):
            return _result(reason=f"{kind} hook reason must be a string")
        result = parse_hook_output(
            {"ok": value["ok"], **({"reason": value["reason"]} if "reason" in value else {})},
            expected_event=str(payload.get("hook_event_name") or "") or None,
        )
        if not value["ok"]:
            result["prevent_continuation"] = True
        return result
    except TimeoutError:
        return _result(reason=f"{kind} hook timed out")
    except Exception as error:  # noqa: BLE001 - evaluator failures must not stop the child
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


__all__ = [
    "HookEvaluator",
    "HookResult",
    "execute_async_command_hook",
    "execute_hook",
    "json_payload",
    "parse_hook_output",
    "set_hook_evaluator",
]


# CCB src/entrypoints/sdk/coreSchemas.ts HOOK_EVENTS (same pin as agents.py).
HOOK_EVENTS = frozenset({
    'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Notification',
    'UserPromptSubmit', 'SessionStart', 'SessionEnd', 'Stop', 'StopFailure',
    'SubagentStart', 'SubagentStop', 'PreCompact', 'PostCompact',
    'PermissionRequest', 'PermissionDenied', 'Setup', 'TeammateIdle',
    'TaskCreated', 'TaskCompleted', 'Elicitation', 'ElicitationResult',
    'ConfigChange', 'WorktreeCreate', 'WorktreeRemove', 'InstructionsLoaded',
    'CwdChanged', 'FileChanged',
})


def validate_hooks(hooks: Mapping[str, Any]) -> dict[str, Any]:
    """CCB schemas/hooks.ts: validate AND return the z.object-normalized copy.

    Persisted schema only. Direct programmatic execute_hook calls retain their
    native adapter; no callback is serialized, no caller-owned object mutated.
    """
    if not isinstance(hooks, Mapping):
        raise TypeError("Agent hooks must be an event mapping")
    result = {}
    fields = {
        "command": {"command", "shell", "async", "asyncRewake"},
        "prompt": {"prompt", "model"}, "agent": {"prompt", "model"},
        "http": {"url", "headers", "allowedEnvVars"},
    }
    common = {"type", "if", "timeout", "statusMessage", "once"}
    for event, matchers in hooks.items():
        if event not in HOOK_EVENTS:
            raise ValueError(f"Agent hooks has unsupported event {event!r}")
        if not isinstance(matchers, list):
            raise ValueError(f"Agent hooks.{event} must be a list")  # noqa: TRY004 - persisted schema errors must propagate through Pydantic
        normalized = []
        for matcher in matchers:
            commands = matcher.get("hooks") if isinstance(matcher, dict) else None
            if not isinstance(commands, list):
                raise ValueError(f"Agent hooks.{event} entries need a hooks list")  # noqa: TRY004 - persisted schema errors must propagate through Pydantic
            if "matcher" in matcher and not isinstance(matcher["matcher"], str):
                raise ValueError("Hook matcher must be a string")
            clean = {key: value for key, value in matcher.items() if key == "matcher"}
            clean["hooks"] = []
            for hook in commands:
                if not isinstance(hook, dict):
                    raise ValueError(f"Agent hooks.{event} entries must be objects")  # noqa: TRY004 - persisted schema errors must propagate through Pydantic
                kind = hook.get("type")
                if not isinstance(kind, str) or kind not in fields:
                    raise ValueError(f"Agent hooks.{event} has unsupported type {kind!r}")
                hook = {key: value for key, value in hook.items() if key in common | fields[kind]}
                required = "url" if kind == "http" else "command" if kind == "command" else "prompt"
                if not isinstance(hook.get(required), str):
                    raise ValueError(f"Agent hooks.{event} {kind} hook needs a string {required}")  # noqa: TRY004 - persisted schema errors must propagate through Pydantic
                for key in ("if", "statusMessage", "model"):
                    if key in hook and not isinstance(hook[key], str):
                        raise ValueError(f"Hook {key} must be a string")
                for key in ("once", "async", "asyncRewake"):
                    if key in hook and not isinstance(hook[key], bool):
                        raise ValueError(f"Hook {key} must be a boolean")
                if "timeout" in hook:
                    timeout = hook["timeout"]
                    # JSON's JS-number domain is finite doubles, not arbitrary
                    # Python integers; oversized integer input must be a schema error.
                    if type(timeout) not in (int, float) or not 0 < timeout <= sys.float_info.max:
                        raise ValueError(f"Agent hooks.{event} timeout must be positive")
                if "shell" in hook and hook["shell"] not in ("bash", "powershell"):
                    raise ValueError("Hook shell must be bash or powershell")
                if kind == "http":
                    AnyUrl(hook["url"])
                    if "headers" in hook and (not isinstance(hook["headers"], dict) or any(
                            not isinstance(k, str) or not isinstance(v, str) for k, v in hook["headers"].items())):
                        raise ValueError("Hook headers must map strings to strings")
                    if "allowedEnvVars" in hook and (not isinstance(hook["allowedEnvVars"], list) or any(
                            not isinstance(v, str) for v in hook["allowedEnvVars"])):
                        raise ValueError("Hook allowedEnvVars must be a string list")
                clean["hooks"].append(hook)
            normalized.append(clean)
        result[event] = normalized
    return result

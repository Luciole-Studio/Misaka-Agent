"""Small JSONL protocol for a persisted sub-agent child process.

Commands arrive on stdin.  Stdout carries only control/progress frames; model
messages are written atomically to a per-turn sidecar next to the persisted
session transcript.  This keeps large tool results away from ``readline()``
and gives the parent an explicit ready/accepted handshake.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import secrets
import signal
import sys
from pathlib import Path
from typing import Any

from misaka.agent.guards import finish_turn_from_stop_predicate
from misaka.core.platform.vocabulary import MANAGEMENT_TOOL_NAMES, MANAGEMENT_TOOLS
from misaka.utils.values import read_field

PROTOCOL_VERSION = 2
PROCESS_GROUP_IDENTITY = "process-group|"

_SMALL_FAST_DEFAULTS: dict[str, tuple[str, ...]] = {
    "anthropic": (
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
        "claude-3-5-haiku-latest",
    ),
    "openai": ("gpt-5.4-mini", "gpt-5-mini", "gpt-4.1-mini"),
    "openai-codex": ("gpt-5.4-mini",),
    "azure-openai-responses": ("gpt-5.4-mini", "gpt-5-mini"),
    "google": (
        "gemini-flash-latest",
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
    ),
    "google-vertex": (
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ),
}


def _disallowed_tool_names(raw: str | None) -> list[str]:
    """Decode frontmatter ``disallowedTools`` passed by the parent."""

    if not raw:
        return []
    try:
        values = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ["*"]
    if not isinstance(values, list):
        return ["*"]

    aliases = {"glob": "find"}
    names: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        name = value.split("(", 1)[0].strip()
        if name:
            names.append(aliases.get(name.casefold(), name))
    return list(dict.fromkeys(names))


def _runtime_flags(argv: list[str]) -> tuple[list[str], int | None]:
    flags: list[str] = []
    max_turns: int | None = None
    index = 0
    while index < len(argv):
        if argv[index] == "--subagent-max-turns" and index + 1 < len(argv):
            try:
                max_turns = max(1, int(argv[index + 1]))
            except ValueError:
                max_turns = None
            index += 2
            continue
        flags.append(argv[index])
        index += 1
    return flags, max_turns


def _requested_tool_names(flags: list[str]) -> list[str]:
    """The tool names the parent named on ``-t``, or ``[]`` for an inherited pool.

    The runtime writes this list itself (``runtime._child_flags``), one ``-t``
    with a comma-separated value, so a positional scan reads it back exactly;
    a later ``-t`` wins, the way ``cli.args`` resolves a repeated flag.
    """

    names: list[str] = []
    for index, flag in enumerate(flags[:-1]):
        if flag in {"--tools", "-t"}:
            names = [item.strip() for item in flags[index + 1].split(",") if item.strip()]
    return names


def _hook_tools(kind: str, tools: list[Any]) -> list[Any]:
    """Prompt hooks are tool-free; agent hooks cannot spawn descendants."""

    if kind == "prompt":
        return []
    from misaka.core.subagent.tool_policy import (
        ALL_AGENT_DISALLOWED_TOOLS,
        canonical_tool_name,
    )

    return [
        tool for tool in tools
        if str(read_field(tool, "name", "")).casefold() not in MANAGEMENT_TOOL_NAMES
        and canonical_tool_name(str(read_field(tool, "name", ""))) not in ALL_AGENT_DISALLOWED_TOOLS
        and read_field(tool, "name") != "StructuredOutput"
    ]


def _small_fast_hook_model(parent: Any, available: list[Any]) -> Any:
    """Resolve the configured small-fast tier within the active provider."""

    parent_provider = str(read_field(parent, "provider", ""))
    provider_models = [
        model
        for model in available
        if str(read_field(model, "provider", "")) == parent_provider
    ]
    by_id = {
        str(read_field(model, "id", "")).casefold(): model
        for model in provider_models
    }

    if parent_provider in {"openai", "openai-codex", "azure-openai-responses"}:
        provider_override = os.environ.get("OPENAI_SMALL_FAST_MODEL")
        keywords = ("mini", "nano", "small", "fast")
    elif parent_provider in {"google", "google-vertex"}:
        provider_override = os.environ.get("GEMINI_SMALL_FAST_MODEL")
        keywords = ("flash", "small", "fast")
    else:
        provider_override = os.environ.get("ANTHROPIC_SMALL_FAST_MODEL")
        keywords = ("haiku", "small", "fast", "mini", "flash", "nano")

    from misaka.config.product import setting

    override = setting("subagents", "small_fast_model", None, str) or provider_override
    if override and override.casefold() in by_id:
        return by_id[override.casefold()]

    for model_id in _SMALL_FAST_DEFAULTS.get(parent_provider, ()):
        if model_id.casefold() in by_id:
            return by_id[model_id.casefold()]

    for keyword in keywords:
        candidates = [
            model
            for model in provider_models
            if keyword in str(read_field(model, "id", "")).casefold()
        ]
        if not candidates:
            continue

        def freshness(model: Any) -> tuple[int, tuple[int, ...], str]:
            model_id = str(read_field(model, "id", "")).casefold()
            numbers = tuple(int(value) for value in re.findall(r"\d+", model_id))
            return (1 if "latest" in model_id else 0, numbers, model_id)

        return max(candidates, key=freshness)
    # Third-party providers should retain their configured primary model rather
    # than silently crossing credentials into another provider.
    return parent


def _agent_hook_can_read_transcript(
    kind: str,
    transcript_path: str,
    tool_name: str,
    tool_input: Any,
) -> bool:
    """Grant an agent hook exactly its transcript, without glob grammar."""

    if kind != "agent" or tool_name.casefold() != "read" or not transcript_path:
        return False
    if not isinstance(tool_input, dict):
        return False
    candidate = tool_input.get("path") or tool_input.get("file_path")
    if not isinstance(candidate, str) or not candidate:
        return False
    try:
        return Path(candidate).expanduser().resolve() == Path(
            transcript_path
        ).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _message_text(message: Any) -> str:
    """Return the exact text carried by a user message event."""

    content = read_field(message, "content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    return "".join(
        str(read_field(block, "text", "") or "")
        for block in content
        if str(read_field(block, "type", "")) == "text"
    )


def _emit(payload: dict[str, Any]) -> bool:
    """Write one deliberately small protocol frame."""

    try:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)
        return True
    except BrokenPipeError:
        return False


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _turn_sidecar(session: Any, turn_id: str) -> Path:
    session_file = session.sessionManager.getSessionFile()
    if session_file:
        transcript = Path(session_file).expanduser().resolve()
        directory = transcript.parent / ".turn-results"
        stem = transcript.stem
    else:  # Defensive fallback; normal sub-agents always receive --session.
        from misaka.config.sessions import subagent_session_dir

        stem = session.sessionManager.getSessionId()
        directory = Path(subagent_session_dir(stem)) / ".turn-results"
    return directory / f"{stem}.{turn_id}.json"


def _terminal_error(messages: list[dict[str, Any]]) -> str | None:
    if not messages:
        return None
    last = messages[-1]
    reason = str(read_field(last, "stopReason", "") or "").casefold()
    if reason not in {"error", "aborted"}:
        return None
    detail = read_field(last, "errorMessage")
    return str(detail or f"request {reason}")


_PARENT_WATCH_SECONDS = 1.0


async def _watch_parent() -> None:
    """Fence the whole agent group if its direct supervisor disappears.

    This task is deliberately started before ``open_session``: provider/MCP
    initialization may hang, and no abandoned child may keep a workspace live
    after its owning Last Order process has died.
    """

    raw = os.environ.get("MISAKA_PARENT_PID", "")
    if not raw.isdigit():
        return
    expected = int(raw)
    while True:
        # Once a second: a supervisor that died is still noticed promptly, and a machine
        # running a dozen children no longer spends 120 syscalls a second asking.
        await asyncio.sleep(_PARENT_WATCH_SECONDS)
        if os.getppid() == expected:
            continue
        if os.name == "posix":
            try:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            except OSError:
                pass
        os._exit(70)


def _attach_durable_sister_owner() -> tuple[bool, str | None]:
    """Publish this root PID/PGID before doing any session initialization."""

    keys = {
        "db": os.environ.get("MISAKA_SISTER_OWNER_DB"),
        "task": os.environ.get("MISAKA_SISTER_OWNER_TASK_ID"),
        "generation": os.environ.get("MISAKA_SISTER_OWNER_GENERATION"),
        "claim": os.environ.get("MISAKA_SISTER_OWNER_CLAIM_LOCK"),
    }
    if not any(keys.values()):
        return True, None
    if not all(keys.values()) or not str(keys["generation"]).isdigit():
        return False, "Incomplete durable Sister ownership environment"
    if os.name == "posix" and os.getpgrp() != os.getpid():
        return False, "Durable Sister root has no isolated process group"

    from misaka.core.platform import processes as process_tree
    from misaka.core.platform import tasks as db

    process_identity = process_tree.identity(os.getpid())
    if not process_identity:
        return False, "Could not establish durable Sister process identity"
    con = db.connect(str(keys["db"]))
    try:
        attached = db.set_pid(
            con,
            str(keys["task"]),
            os.getpid(),
            PROCESS_GROUP_IDENTITY + process_identity,
            generation=int(str(keys["generation"])),
            claim_lock=str(keys["claim"]),
        )
    finally:
        con.close()
    return attached, None if attached else "Sister ownership was revoked before child attach"


async def amain() -> int:
    parent_watch = asyncio.create_task(_watch_parent())
    attached, attach_error = await asyncio.to_thread(_attach_durable_sister_owner)
    if not attached:
        _emit({"type": "child_error", "error": attach_error})
        parent_watch.cancel()
        await asyncio.gather(parent_watch, return_exceptions=True)
        return 2

    from misaka.agent.request_budget import install_turn_budget
    from misaka.core.platform import session as engine_session
    from misaka.core.subagent import hooks as subagent_hooks
    from misaka.core.subagent import policy as subagent_policy

    permission_waiters: dict[str, asyncio.Future[bool]] = {}
    settings_waiters: dict[str, asyncio.Future] = {}

    async def request_parent_settings():
        request_id = secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        settings_waiters[request_id] = future
        _emit({"type": "permission_settings_request", "requestId": request_id})
        try:
            return await asyncio.wait_for(future, 30)
        finally:
            settings_waiters.pop(request_id, None)


    async def request_parent_permission(payload: dict[str, Any]) -> bool:
        request_id = secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        permission_waiters[request_id] = future
        _emit(
            {
                "type": "permission_request",
                "requestId": request_id,
                **payload,
            }
        )
        try:
            return await future
        finally:
            permission_waiters.pop(request_id, None)

    async def submit_async_hook(
        hook: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        request_id = secrets.token_hex(8)
        transcript = Path(
            os.environ.get("MISAKA_SUBAGENT_TRANSCRIPT") or ".misaka-agent.jsonl"
        ).expanduser().resolve()
        request_file = transcript.parent / ".hooks" / f"{request_id}.json"
        agent_id = os.environ.get("MISAKA_SUBAGENT_ID") or ""
        parent_session_id = (
            os.environ.get("MISAKA_SUBAGENT_PARENT_SESSION_ID") or ""
        )
        turn_id = active_turn_id or ""
        envelope = {
            "schemaVersion": 1,
            "requestId": request_id,
            "agentId": agent_id,
            "parentSessionId": parent_session_id,
            "turnId": turn_id,
            "hook": hook,
            "payload": subagent_hooks.json_payload(payload),
        }
        try:
            await asyncio.to_thread(
                _atomic_write_json,
                request_file,
                envelope,
            )
            emitted = _emit(
                {
                    "type": "async_hook_request",
                    "schemaVersion": 1,
                    "requestId": request_id,
                    "agentId": agent_id,
                    "parentSessionId": parent_session_id,
                    "turnId": turn_id,
                    "requestFile": str(request_file),
                }
            )
            if not emitted:
                raise BrokenPipeError("parent protocol pipe is closed")
        except Exception:
            try:
                request_file.unlink()
            except OSError:
                pass
            raise

    subagent_policy.set_permission_broker(request_parent_permission)
    subagent_policy.set_async_hook_broker(submit_async_hook)

    flags, max_turns = _runtime_flags(sys.argv[1:])
    profile_dir = os.environ.get("MISAKA_PROFILE_DIR") or ""
    role = os.environ.get("MISAKA_WHO") or ""
    workspace = os.environ.get("MISAKA_WORKSPACE") or os.getcwd()
    mcp_role = os.environ.get("MISAKA_MCP_ROLE") or role
    card = os.environ.get("MISAKA_SISTER_OWNER_TASK_ID")    # set only on the child that is a card's own session
    sandbox = os.environ.get("MISAKA_SKILL_SANDBOX")         # a Sister card: the parent's read-only skill copies
    from misaka.core.wiring import SessionSpec, assemble
    runtime, session, error = await engine_session.open_session(
        flags,
        workspace,
        assemble(SessionSpec(
            profile_dir=profile_dir,
            role=role,
            workspace=workspace,
            kind="card" if card else "child",
            sender=role.rsplit("/", 1)[-1],
            mcp_role=mcp_role,
            receive_messages=bool(card),
            task_id=card,
            research_context=bool(card) and os.environ.get("MISAKA_RESEARCH_CONTEXT") == "1",
            skill_roots=(("sandbox", sandbox),) if sandbox else None,
        )),
    )
    if error:
        _emit({"type": "child_error", "error": error})
        await engine_session.dispose(runtime)
        subagent_policy.set_async_hook_broker(None)
        subagent_policy.set_permission_broker(None)
        subagent_policy.set_permission_settings_provider(None)
        parent_watch.cancel()
        await asyncio.gather(parent_watch, return_exceptions=True)
        return 2

    if not card:
        from misaka.core.subagent.resume import install_resume_filter
        install_resume_filter(session)

    from misaka.core.subagent import disallowed_management_tools
    from misaka.core.subagent.tool_policy import tool_allowed_for_agent

    fork_snapshot = None
    if os.environ.get("MISAKA_FORK_CHILD") == "1":
        try:
            path = Path(os.environ["MISAKA_SUBAGENT_TRANSCRIPT"]).with_suffix(".meta.fork.json")
            fork_snapshot = json.loads(path.read_text(encoding="utf-8"))
            if (fork_snapshot.get("schemaVersion") != 1
                or fork_snapshot.get("agentId") != os.environ.get("MISAKA_SUBAGENT_ID")
                or fork_snapshot.get("parentSessionId") != os.environ.get("MISAKA_SUBAGENT_PARENT_SESSION_ID")
                or not isinstance(fork_snapshot.get("systemPrompt"), str)
                or not isinstance(fork_snapshot.get("tools"), list)):
                raise ValueError("Fork snapshot identity or shape mismatch")
        except (OSError, ValueError, KeyError) as error:
            _emit({"type": "child_error", "error": f"Fork startup failed: {error}"})
            await engine_session.dispose(runtime)
            parent_watch.cancel()
            await asyncio.gather(parent_watch, return_exceptions=True)
            return 2

    disallowed_tools = list(dict.fromkeys([
        *_disallowed_tool_names(os.environ.get("MISAKA_SUBAGENT_DISALLOWED_TOOLS")),
        *disallowed_management_tools("card" if card else "child"),
    ]))
    denied = {t.casefold() for t in disallowed_tools}
    is_async = os.environ.get("MISAKA_SUBAGENT_BACKGROUND", "0") != "0"
    if disallowed_tools:
        session.setDisallowedToolsByName(
            disallowed_tools,
            # Structural and frontmatter denies both win over management always-allow.
            alwaysAllowed=([t for t in MANAGEMENT_TOOLS if t.casefold() not in denied] if card else []),
            admission=(None if card else
                       (lambda name: any(tool["name"] == name for tool in fork_snapshot["tools"]))
                       if fork_snapshot is not None else
                       lambda name: tool_allowed_for_agent(
                           name, is_async=is_async,
                           permission_mode=os.environ.get("MISAKA_SUBAGENT_PERMISSION_MODE"),
                       )),
        )

    from misaka.core.subagent.model import (
        install_effort_adapter,
        normalize_model_for_api,
    )

    install_effort_adapter(session.agent, os.environ)

    if os.environ.get("MISAKA_SUBAGENT_INHERIT_ALL_TOOLS") == "1":
        # No frontmatter allow/deny list means Claude's complete independently
        # assembled worker pool, not harn's four-tool interactive default.
        session.setActiveToolsByName([tool.name for tool in session.getAllTools()])

    async def evaluate_model_hook(
        kind: str,
        prompt: str,
        _payload: dict[str, Any],
        hook: dict[str, Any],
    ) -> str:
        """Run prompt/agent hooks on this child's configured provider stack."""

        from misaka.agent.agent import Agent, AgentOptions
        from misaka.agent.types import AgentTool, AgentToolResult, BeforeToolCallResult
        from misaka.ai.types import TextContent, UserMessage
        from misaka.core.subagent.runtime import (
            RoleContext,
            resolve_model_spec,
        )

        parent_model = session.model
        available = session.modelRegistry.getAvailable()
        requested_model = str(hook.get("model") or "").strip()
        if requested_model:
            provider, model_id = resolve_model_spec(
                {}, None, requested_model, parent_model, session.modelRegistry.getAll()
            )
            model = session.modelRegistry.find(provider, normalize_model_for_api(model_id))
            if model is None:
                raise RuntimeError(f"Hook model is not available: {provider}/{model_id}")
            if not session.modelRegistry.hasConfiguredAuth(model):
                raise RuntimeError(f"No configured authentication for hook model: {provider}/{model_id}")
        else:
            model = _small_fast_hook_model(parent_model, available)

        role_context = RoleContext.capture(
            role=role,
            profile_dir=profile_dir,
            workspace=workspace,
            mcp_role=mcp_role,
        )
        transcript_path = os.environ.get("MISAKA_SUBAGENT_TRANSCRIPT") or ""
        if kind == "agent" and transcript_path:
            transcript_path = str(Path(transcript_path).expanduser().resolve())
        tools = _hook_tools(kind, session.agent.state.tools)
        turns = 0
        structured_result: dict[str, Any] | None = None
        response_schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"}},
            "required": ["ok"],
            "additionalProperties": False,
        }

        async def structured_output(
            _id: str, args: Any, _signal: Any = None, _update: Any = None
        ) -> AgentToolResult:
            nonlocal structured_result
            structured_result = dict(args)
            return AgentToolResult(
                content=[TextContent(text=json.dumps(structured_result))],
                details=structured_result,
                terminate=True,
            )

        if kind == "agent":
            tools.append(AgentTool(
                name="StructuredOutput", label="Structured output",
                description="Return the hook verification result.",
                parameters=response_schema, execute=structured_output,
            ))

        async def output_format(payload: dict[str, Any], model: Any) -> dict[str, Any]:
            # Keep the native multi-provider wire adapter explicit; the CCB
            # implementation writes Anthropic outputFormat only.
            if kind != "prompt":
                return payload
            api = str(read_field(model, "api", ""))
            if api == "anthropic-messages":
                payload.setdefault("output_config", {})["format"] = {
                    "type": "json_schema", "schema": response_schema,
                }
            elif api in {"openai-responses", "openai-codex-responses"}:
                payload.setdefault("text", {})["format"] = {
                    "type": "json_schema", "name": "hook_response", "schema": response_schema,
                }
            elif api == "openai-completions":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "hook_response", "schema": response_schema},
                }
            return payload

        async def before_tool(call: Any, _signal: Any = None) -> BeforeToolCallResult | None:
            call_name = str(read_field(read_field(call, "toolCall"), "name", ""))
            call_input = read_field(call, "args", {}) or {}
            if call_name == "StructuredOutput":
                return None
            try:
                snapshot = subagent_policy._snapshot_office_input(call_name, call_input, workspace)
            except (TypeError, ValueError) as error:
                return BeforeToolCallResult(block=True, reason=f"Invalid Office input: {error}")
            action, reason = await subagent_policy.hook_tool_permission(
                session, role_context, call_name, snapshot, workspace,
                transcript_read=_agent_hook_can_read_transcript(
                    kind, transcript_path, call_name, call_input,
                ),
            )
            if action != "allow":
                return BeforeToolCallResult(
                    block=True,
                    reason=reason or "Agent hook permission denied",
                )
            return BeforeToolCallResult(updatedInput=snapshot) if snapshot is not call_input else None

        async def should_stop(_turn: Any, _signal: Any = None) -> bool:
            nonlocal turns
            turns += 1
            if structured_result is not None or turns >= (1 if kind == "prompt" else 50):
                return True
            # Source registerStructuredOutputEnforcement: an ordinary assistant
            # answer is not the final verdict. Request the synthetic output tool.
            message = read_field(_turn, "message", None)
            if kind == "agent" and not any(
                read_field(block, "type") == "toolCall"
                for block in (read_field(message, "content", []) or [])
            ):
                verifier.followUp(UserMessage(
                    content="Return your verification using the StructuredOutput tool.",
                    timestamp=0,
                ))
            return False

        verifier = Agent(
            AgentOptions(
                initialState={
                    "systemPrompt": (
                        "Evaluate the hook condition. Return only JSON in the form "
                        '{"ok":true} or {"ok":false,"reason":"..."}. '
                        + (
                            "Use the provided tools to verify facts. Return the result using StructuredOutput. "
                            + (
                                f"The conversation transcript is available at: {transcript_path}."
                                if transcript_path
                                else ""
                            )
                            if kind == "agent"
                            else "Do not call tools or include prose."
                        )
                    ),
                    "model": model,
                    "thinkingLevel": "off",
                    "tools": tools,
                    # The parent's transcript leads with its own system messages (prompt and
                    # tool declarations); the verifier must run under its own `systemPrompt`,
                    # which only seeds the head when no system message is copied in.
                    "messages": [
                        message
                        for message in copy.deepcopy(session.agent.state.messages)
                        if str(read_field(message, "role", "")) != "system"
                    ]
                    if kind == "prompt"
                    else [],
                },
                convertToLlm=session.agent.convertToLlm,
                onPayload=output_format,
                streamFn=getattr(session.agent, "_subagent_base_stream_fn", session.agent.streamFn),
                getApiKey=session.agent.getApiKey,
                beforeToolCall=before_tool if tools else None,
                finishTurn=finish_turn_from_stop_predicate(should_stop),
            )
        )
        try:
            await verifier.prompt(prompt)
        finally:
            # Timeout cancellation must not leave a model stream/tool task alive.
            verifier.abort()
        if kind == "agent":
            if structured_result is None:
                raise RuntimeError("Agent hook ended without StructuredOutput")
            return json.dumps(structured_result)
        for message in reversed(verifier.state.messages):
            if str(read_field(message, "role", "")) != "assistant":
                continue
            if str(read_field(message, "stopReason", "")) in {"error", "aborted"}:
                raise RuntimeError(str(read_field(message, "errorMessage", "hook model failed")))
            return "".join(
                str(read_field(block, "text", ""))
                for block in (read_field(message, "content", []) or [])
                if str(read_field(block, "type", "")) == "text"
            )
        raise RuntimeError("Hook model returned no assistant message")

    subagent_hooks.set_hook_evaluator(evaluate_model_hook)

    async def classify_auto_permission(payload: dict[str, Any]) -> bool:
        from misaka.modes.jsonl import to_jsonable

        transcript = json.dumps(
            to_jsonable(session.state.messages),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        # Keep the newest context when a very long side chain reaches the
        # classifier.  The shared token limiter remains the hard upper bound.
        transcript = transcript[-120_000:]
        prompt = (
            "Decide whether the proposed tool action is justified by the user's "
            "instructions and the conversation. Block secret exposure, privilege "
            "escalation, destructive or external writes, unrequested network side "
            "effects, and commands whose intent is ambiguous. Return only "
            '{"ok":true,"reason":"..."} to allow or '
            '{"ok":false,"reason":"..."} to block.\n\n'
            f"Conversation transcript:\n{transcript}\n\n"
            f"Proposed action:\n{json.dumps(payload, ensure_ascii=False)}"
        )
        output = await evaluate_model_hook("prompt", prompt, payload, {"model": "inherit"})
        result = subagent_hooks.parse_hook_output(output)
        return result["decision"] == "allow"

    subagent_policy.set_permission_classifier(classify_auto_permission)

    model_turn_count = 0
    turn_budget = install_turn_budget(session)
    async def stop_at_turn_boundary(context: Any, abort_signal: Any = None) -> bool:
        return max_turns is not None and model_turn_count >= max_turns

    if max_turns is not None:
        session.agent.finishTurn = finish_turn_from_stop_predicate(
            stop_at_turn_boundary, getattr(session.agent, "finishTurn", None)
        )
    sequence = 0
    active_turn_id: str | None = None
    active_messages: list[dict[str, Any]] | None = None
    accepts_steer = False
    turn: asyncio.Task[None] | None = None
    pending_steers: list[dict[str, Any]] = []

    def jsonable(value: Any) -> Any:
        return json.loads(engine_session.event_line(value))

    progress_jobs: set[asyncio.Task] = set()

    async def publish_assistant(turn_id: str, index: int, message: Any) -> None:
        sidecar = _turn_sidecar(session, f"{turn_id}-progress-{index}")
        await asyncio.to_thread(_atomic_write_json, sidecar, {
            "schemaVersion": 1, "turnId": turn_id,
            "agentId": os.environ.get("MISAKA_SUBAGENT_ID"), "messages": [message],
        })
        _emit({"type": "child_progress", "turnId": turn_id, "event": "assistant_message",
               "sequence": index, "messagesFile": str(sidecar)})

    def on_event(event: Any) -> None:
        """Collect assistant completions and expose only bounded progress data."""

        nonlocal model_turn_count
        event_type = str(read_field(event, "type", ""))

        # A SendMessage acknowledgement means the running agent actually
        # consumed the queued steering message, not merely that stdin accepted
        # it.  This closes the final-poll window where a turn could finish
        # immediately after queueing and silently drop an acknowledged message.
        if event_type == "message_start":
            message = read_field(event, "message")
            if str(read_field(message, "role", "")) == "user":
                consumed = _message_text(message)
                for index, pending in enumerate(pending_steers):
                    if (
                        pending["turnId"] == active_turn_id
                        and pending["message"] == consumed
                    ):
                        pending_steers.pop(index)
                        _emit(
                            {
                                "type": "steer_accepted",
                                "turnId": active_turn_id,
                                "requestId": pending["requestId"],
                            }
                        )
                        break

        if event_type == "message_end" and active_messages is not None:
            message = read_field(event, "message")
            if str(read_field(message, "role", "")) == "assistant":
                try:
                    normalized = jsonable(message)
                    active_messages.append(normalized)
                    if active_turn_id:
                        job = asyncio.create_task(publish_assistant(active_turn_id, len(active_messages), normalized))
                        progress_jobs.add(job)
                except (TypeError, ValueError):
                    pass

        if event_type == "turn_end":
            model_turn_count += 1
        if event_type in {"agent_start", "turn_start", "turn_end", "agent_end"}:
            _emit(
                {
                    "type": "child_progress",
                    "turnId": active_turn_id,
                    "event": event_type,
                }
            )
        elif event_type in {
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        }:
            _emit(
                {
                    "type": "child_progress",
                    "turnId": active_turn_id,
                    "event": event_type,
                    "toolCallId": read_field(event, "toolCallId", read_field(event, "toolUseID")),
                    "toolName": read_field(event, "toolName"),
                }
            )
        elif event_type == "message_end":
            message = read_field(event, "message")
            _emit(
                {
                    "type": "child_progress",
                    "turnId": active_turn_id,
                    "event": event_type,
                    "role": read_field(message, "role"),
                    "stopReason": read_field(message, "stopReason"),
                }
            )

    session.subscribe(on_event)

    async def run_turn(message: str, turn_id: str) -> None:
        nonlocal active_turn_id, active_messages, accepts_steer, model_turn_count
        model_turn_count = 0
        messages: list[dict[str, Any]] = []
        active_turn_id = turn_id
        active_messages = messages
        turn_error: str | None = None
        try:
            await session.prompt(message)
            accepts_steer = False
            # A nested child process is the supervisor for agents it launched, and a card
            # session may start one more turn at settle time. Keep the process alive until
            # all of that has ended before emitting this turn's sidecar to its parent.
            from misaka.core.platform.session import settle_after_prompt
            await settle_after_prompt(session)
        except Exception as exc:  # noqa: BLE001
            turn_error = f"{type(exc).__name__}: {exc}"
        finally:
            # No command arriving after prompt completion may be queued into a
            # run that has already stopped polling its steering queue.
            accepts_steer = False
            for pending in tuple(pending_steers):
                if pending["turnId"] != turn_id:
                    continue
                pending_steers.remove(pending)
                _emit(
                    {
                        "type": "steer_rejected",
                        "turnId": turn_id,
                        "requestId": pending["requestId"],
                        "error": "Agent turn completed before consuming steering",
                    }
                )
            if active_messages is messages:
                active_messages = None

        terminal_error = _terminal_error(messages)
        if terminal_error:
            turn_error = f"{turn_error}; {terminal_error}" if turn_error else terminal_error

        # Sidecars remain ordered ahead of the authoritative turn-done record.
        # A failed progress write must not discard the final transcript/result.
        if progress_jobs:
            await asyncio.gather(*progress_jobs, return_exceptions=True)
            progress_jobs.clear()
        messages_file: str | None = None
        try:
            sidecar = _turn_sidecar(session, turn_id)
            payload = {
                "schemaVersion": 1,
                "turnId": turn_id,
                "agentId": os.environ.get("MISAKA_SUBAGENT_ID"),
                "messages": messages,
            }
            await asyncio.to_thread(_atomic_write_json, sidecar, payload)
            messages_file = str(sidecar)
        except Exception as exc:  # noqa: BLE001
            detail = f"sidecar write failed: {type(exc).__name__}: {exc}"
            turn_error = f"{turn_error}; {detail}" if turn_error else detail

        _emit(
            {
                "type": "child_turn_done",
                "turnId": turn_id,
                "messagesFile": messages_file,
                "error": turn_error,
                "budgetUsage": turn_budget.accounted if turn_budget is not None else None,
            }
        )
        if active_turn_id == turn_id:
            active_turn_id = None

    async def start_turn(message: str, source: str, request_id: Any) -> None:
        nonlocal sequence, turn, accepts_steer, active_turn_id
        if turn is not None and not turn.done():
            await turn
        sequence += 1
        turn_id = f"t{sequence}-{secrets.token_hex(4)}"
        active_turn_id = turn_id
        accepts_steer = True
        turn = asyncio.create_task(run_turn(message, turn_id))
        _emit(
            {
                "type": "prompt_accepted",
                "turnId": turn_id,
                "source": source,
                "requestId": request_id,
            }
        )

    def mcp_server_names() -> list[str]:
        try:
            registered = session.getAllTools()
            return list(
                dict.fromkeys(
                    name.split("__", 2)[1]
                    for tool in registered
                    if (name := str(read_field(tool, "name", "")))
                    if name.startswith("mcp__") and name.count("__") >= 2
                )
            )
        except (AttributeError, RuntimeError):
            return []

    required_raw = os.environ.get("MISAKA_REQUIRED_MCP_SERVERS")
    try:
        decoded = json.loads(required_raw) if required_raw else []
        required_mcp = [str(item) for item in decoded] if isinstance(decoded, list) else []
    except (TypeError, ValueError):
        required_mcp = []
    if fork_snapshot is not None:
        required_mcp.extend(
            item["name"].split("__", 2)[1] for item in fork_snapshot["tools"]
            if str(item.get("name", "")).startswith("mcp__") and item["name"].count("__") >= 2
        )
    from misaka.config.product import setting

    deadline = asyncio.get_running_loop().time() + setting("mcp", "required_wait", 30.0, float)
    while required_mcp:
        active_mcp_servers = mcp_server_names()
        if all(
            any(pattern.casefold() in name.casefold() for name in active_mcp_servers)
            for pattern in required_mcp
        ):
            break
        if asyncio.get_running_loop().time() >= deadline:
            _emit(
                {
                    "type": "child_error",
                    "error": "Required MCP servers did not become ready: "
                    + ", ".join(required_mcp),
                }
            )
            await engine_session.dispose(runtime)
            parent_watch.cancel()
            await asyncio.gather(parent_watch, return_exceptions=True)
            return 2
        await asyncio.sleep(0.1)
    active_mcp_servers = mcp_server_names()

    # ``agent_session`` matches the ``-t`` allowlist against the registry and drops
    # every name it does not recognize, without a word.  A typo in an ``agents/*.md``
    # ``tools:`` line, or a tool since renamed, therefore costs the child that tool
    # and reads exactly like a model that chose not to use it.  This is the last
    # moment before the handshake, so what is missing here is missing for good --
    # except for MCP tools, which the parent names on purpose before their server
    # has registered (``runtime._child_flags``) and which are not late by accident.
    requested_tools = _requested_tool_names(flags)
    if requested_tools:
        assembled = {tool.name for tool in session.getAllTools()}
        missing = [
            name
            for name in requested_tools
            if name not in assembled and name.casefold() not in denied and not name.startswith("mcp__")
        ]
        if missing:
            _emit({"type": "child_tools_missing", "tools": missing})

    if fork_snapshot is not None:
        from misaka.core.subagent.fork import install as install_fork
        try:
            install_fork(session, fork_snapshot)
        except (ValueError, TypeError) as error:
            _emit({"type": "child_error", "error": f"Exact fork startup failed: {error}"})
            await engine_session.dispose(runtime)
            parent_watch.cancel()
            await asyncio.gather(parent_watch, return_exceptions=True)
            return 2

    # Discovery/sealing is complete before this handshake. The parent retains
    # ownership of blocking preload work, but resolves against THIS child's roots.
    from misaka.core.skills.wiring.skills import SkillsPart
    skills_part = next((p for p in session.moments.parts if isinstance(p, SkillsPart)), None)
    skill_roots = skills_part.resolution_roots() if skills_part is not None else []

    if os.environ.get("MISAKA_SUBAGENT_LIVE_PERMISSIONS") == "1":
        subagent_policy.set_permission_settings_provider(request_parent_settings)

    _emit(
        {
            "type": "child_ready",
            "protocolVersion": PROTOCOL_VERSION,
            "mcpServers": active_mcp_servers,
            "skillRoots": skill_roots,
        }
    )

    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                if turn is not None:
                    await turn
                break
            try:
                command = json.loads(line)
            except ValueError:
                _emit({"type": "child_protocol_error", "error": "invalid JSON command"})
                continue
            if not isinstance(command, dict):
                _emit({"type": "child_protocol_error", "error": "command must be an object"})
                continue

            kind = command.get("type")
            message = str(command.get("message") or "")
            request_id = command.get("requestId")
            active = turn is not None and not turn.done() and accepts_steer

            if kind == "prompt":
                if active:
                    await session.steer(message)
                    _emit(
                        {
                            "type": "prompt_accepted",
                            "turnId": active_turn_id,
                            "source": "prompt-as-steer",
                            "requestId": request_id,
                        }
                    )
                else:
                    await start_turn(message, "prompt", request_id)
            elif kind in {"steer", "message"}:
                if active:
                    try:
                        await session.steer(message)
                        if request_id is not None:
                            queued = session.getSteeringMessages()
                            pending_steers.append(
                                {
                                    "turnId": active_turn_id,
                                    "requestId": request_id,
                                    # steer() may expand a skill/prompt template;
                                    # message_start carries the expanded value.
                                    "message": queued[-1] if queued else message,
                                }
                            )
                    except Exception as exc:  # noqa: BLE001
                        _emit(
                            {
                                "type": "steer_rejected",
                                "turnId": active_turn_id,
                                "requestId": request_id,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                else:
                    _emit(
                        {
                            "type": "steer_rejected",
                            "turnId": active_turn_id,
                            "requestId": request_id,
                            "error": "Agent turn is no longer active",
                        }
                    )
            elif kind == "background":
                if not card:
                    from dataclasses import replace

                    is_async = True
                    os.environ["MISAKA_SUBAGENT_BACKGROUND"] = "1"
                    bubble = os.environ.get("MISAKA_SUBAGENT_BUBBLE") == "1"
                    if not bubble:
                        os.environ.pop("MISAKA_SUBAGENT_CAN_PROMPT", None)
                    for part in session.moments.parts:
                        agent_policy = getattr(part, "policy", None)
                        if agent_policy is not None:
                            agent_policy.context = replace(agent_policy.context, permission_can_prompt=bubble)
                    if not bubble:
                        for waiter in permission_waiters.values():
                            if not waiter.done():
                                waiter.set_result(False)
                    session.refreshTools()
                _emit({"type": "background_ready"})
            elif kind == "abort":
                await session.abort()
                _emit({"type": "abort_accepted", "turnId": active_turn_id, "requestId": request_id})
            elif kind == "permission_settings_response":
                waiter = settings_waiters.get(str(request_id or ""))
                if waiter is not None and not waiter.done():
                    waiter.set_result({"settings": command.get("settings"), "mode": command.get("mode")})
            elif kind == "permission_response":
                waiter = permission_waiters.get(str(request_id or ""))
                if waiter is not None and not waiter.done():
                    waiter.set_result(bool(command.get("allow")))
            elif kind == "shutdown":
                if turn is not None:
                    await turn
                break
            else:
                _emit({"type": "child_protocol_error", "error": f"unknown command: {kind}"})
        return 0
    finally:
        subagent_hooks.set_hook_evaluator(None)
        subagent_policy.set_async_hook_broker(None)
        subagent_policy.set_permission_broker(None)
        subagent_policy.set_permission_classifier(None)
        subagent_policy.set_permission_settings_provider(None)
        for waiter in settings_waiters.values():
            if not waiter.done():
                waiter.set_exception(RuntimeError("Parent permission settings channel closed"))
        settings_waiters.clear()
        for waiter in permission_waiters.values():
            if not waiter.done():
                waiter.set_exception(RuntimeError("Parent permission channel closed"))
        permission_waiters.clear()
        parent_watch.cancel()
        await asyncio.gather(parent_watch, return_exceptions=True)
        await engine_session.dispose(runtime)

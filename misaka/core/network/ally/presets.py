"""The allies misaka can drive over ACP, and which of them this home enables.

A preset is shipped only once its agent has been driven end to end -- a real turn, its tool
calls rendered, context held across two prompts, a session resumed from a fresh process --
because a roster entry that cannot run a task is worse than none: the model reads it as
available (the rule raven's presets follow). Claude Code and Codex are the two measured here.

Both are reached through the ACP project's adapters, fetched with ``npx -y`` at a pinned
version: an adapter is plumbing in front of an agent, holds no credential of its own, and an
unpinned ``npx -y`` would change which build runs without anyone choosing it. Each adapter
bundles its agent and uses the login this machine already has. Pins follow raven's
measurement of 2026-09-23 (claude-agent-acp 0.81.1 bundles Claude Code 2.1.280; codex-acp
1.13.1 bundles codex 0.156.1); a bump is a deliberate change here.

``settings.json``'s ``allies`` is a map from an ally's name to ``{}`` (the preset of that name)
or to ``{"command": [...], "env": {...}, "description": "..."}`` for any other ACP agent.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass

CLAUDE_ACP = ("npx", "-y", "@agentclientprotocol/claude-agent-acp@0.81.1")
CODEX_ACP = ("npx", "-y", "@agentclientprotocol/codex-acp@1.13.1")


@dataclass(frozen=True)
class Ally:
    name: str
    command: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    description: str = ""


PRESETS = {
    "claude": Ally(
        "claude", CLAUDE_ACP,
        description="Claude Code, Anthropic's coding agent: reads a codebase, edits files, runs commands."),
    "codex": Ally(
        "codex", CODEX_ACP,
        env=(("INITIAL_AGENT_MODE", "agent-full-access"),),
        description="Codex, OpenAI's coding agent: reads a codebase, edits files, runs commands."),
}


def _settings():
    from misaka.core.settings_manager import SettingsManager
    try:
        value = SettingsManager.forRole(None).settings.get("allies")
    except Exception:  # noqa: BLE001 - an unreadable settings file enables no ally
        return {}
    return value if isinstance(value, dict) else {}


def configured() -> dict[str, Ally]:
    """The allies this home enables, by name."""
    out = {}
    for name, value in _settings().items():
        if not isinstance(name, str) or not name.strip() or not isinstance(value, dict):
            continue
        preset = PRESETS.get(name)
        command = value.get("command")
        env = value.get("env") if isinstance(value.get("env"), dict) else {}
        description = str(value.get("description") or (preset.description if preset else ""))
        if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
            out[name] = Ally(name, tuple(command), tuple((str(k), str(v)) for k, v in env.items()), description)
        elif preset is not None:
            out[name] = Ally(name, preset.command,
                             preset.env + tuple((str(k), str(v)) for k, v in env.items()), description)
    return out


def names() -> set[str]:
    return set(configured())


def get(name) -> Ally | None:
    return configured().get(name)


def unavailable(ally: Ally) -> str | None:
    """Why ``ally`` cannot start on this machine, or None when it can."""
    if shutil.which(ally.command[0]) is None:
        return f"`{ally.command[0]}` is not installed"
    return None


def environment(ally: Ally) -> dict[str, str]:
    """The agent's environment: this process's, less misaka's own variables, plus the ally's."""
    return {**{key: value for key, value in os.environ.items() if not key.startswith("MISAKA_")},
            **dict(ally.env)}


START_SECONDS = 600


async def handshake(ally: Ally) -> str:
    """Whether ``ally`` can take a card, without spending its quota: it starts, answers
    ``initialize`` and opens a session (``session/new`` is where a missing login shows)."""
    from misaka.core.network.ally.acp import (
        CLIENT_CAPABILITIES,
        PROTOCOL_VERSION,
        AcpClient,
        AcpError,
    )

    if why := unavailable(ally):
        return f"not installed: {why}"

    async def refuse(method, params):
        raise NotImplementedError(method)

    with tempfile.TemporaryDirectory() as cwd:
        client = AcpClient(ally.command, cwd=cwd, env=environment(ally), on_update=lambda params: None,
                           on_request=refuse)
        try:
            await client.start()
            await client.request("initialize", {"protocolVersion": PROTOCOL_VERSION,
                                                "clientCapabilities": CLIENT_CAPABILITIES}, timeout=START_SECONDS)
            await client.request("session/new", {"cwd": cwd, "mcpServers": []}, timeout=START_SECONDS)
        except (AcpError, OSError, TimeoutError) as error:
            detail = " ".join((str(error) or "timed out").split())[:300]
            return f"needs login: {detail}" if "auth" in detail.casefold() else f"cannot start: {detail}"
        finally:
            await client.close()
    return "ready"


def check(ally: Ally) -> str:
    return asyncio.run(handshake(ally))

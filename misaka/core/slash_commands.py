"""Translated from pi's ``packages/coding-agent/src/core/slash-commands.ts`` (v1.3.0).

MISAKA forks:
- No ``bug`` (pi's reports go to the Pi developers) and no ``changelog`` (no changelog ships).
- ``thinking --default`` persists the level (pi 496185f6, after 1.1.0), ``login --account`` names an OAuth account,
  and ``new --carry`` keeps the context engine's summaries.
- A part's command has the source ``core``; ``clear`` and ``board`` are MISAKA's local commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from misaka.config import APP_NAME
from misaka.core.source_info import SourceInfo

type SlashCommandSource = Literal["extension", "prompt", "skill", "core"]  # MISAKA fork: "core" = a part's command


class SlashCommandInfo(TypedDict):
    name: str
    description: NotRequired[str]
    source: SlashCommandSource
    sourceInfo: SourceInfo


@dataclass(slots=True, frozen=True)
class BuiltinSlashCommand:
    name: str
    description: str
    argumentHint: str | None = None


BUILTIN_SLASH_COMMANDS: tuple[BuiltinSlashCommand, ...] = (
    BuiltinSlashCommand(name="settings", description="Open settings menu"),
    BuiltinSlashCommand(name="model", description="Select model (opens selector UI)", argumentHint="<provider/model>"),
    BuiltinSlashCommand(name="tree", description="Navigate session tree (switch branches)"),
    # MISAKA fork: --default also saves the level as the startup default (pi 496185f6).
    BuiltinSlashCommand(name="thinking", description="Select thinking level (--default persists it)", argumentHint="[--default] <level>"),
    BuiltinSlashCommand(name="scoped-models", description="Enable/disable models for Ctrl+P cycling"),
    BuiltinSlashCommand(name="export", description="Export session (HTML default, or specify path: .html/.jsonl)"),
    BuiltinSlashCommand(name="import", description="Import and resume a session from a JSONL file"),
    BuiltinSlashCommand(name="share", description="Share session via a Radius artifact or a GitHub gist"),
    BuiltinSlashCommand(name="copy", description="Copy last agent message to clipboard"),
    BuiltinSlashCommand(name="name", description="Set session display name"),
    BuiltinSlashCommand(name="session", description="Show session info and stats"),
    BuiltinSlashCommand(name="hotkeys", description="Show all keyboard shortcuts"),
    BuiltinSlashCommand(name="fork", description="Create a new fork from a previous user message"),
    BuiltinSlashCommand(name="clone", description="Duplicate the current session at the current position"),
    BuiltinSlashCommand(name="trust", description="Save project trust decision for future sessions"),
    # MISAKA fork: --account names an OAuth account.
    BuiltinSlashCommand(name="login", description="Configure provider authentication", argumentHint="<provider> [--account NAME]"),
    BuiltinSlashCommand(name="logout", description="Remove provider authentication"),
    # MISAKA fork: --carry keeps the context engine's summaries.
    BuiltinSlashCommand(name="new", description="Start a new session (--carry keeps the context engine's summaries)"),
    BuiltinSlashCommand(name="compact", description="Manually compact the session context"),
    BuiltinSlashCommand(name="resume", description="Resume a different session"),
    BuiltinSlashCommand(name="reload", description="Reload keybindings, extensions, skills, prompts, themes, and context files"),
    BuiltinSlashCommand(name="quit", description=f"Quit {APP_NAME}"),
)

# MISAKA fork: MISAKA's local commands.
_LOCAL_ALIAS_SLASH_COMMANDS: tuple[BuiltinSlashCommand, ...] = (
    # /clear wipes the current session in place (same id, same file, history erased); /new starts a new session
    BuiltinSlashCommand(name="clear", description="Wipe current session in place (same id, history erased)"),
    # the task board of this project folder, read-only; a human's in-session view
    BuiltinSlashCommand(name="board", description="Show the read-only task board of this project folder"),
)


def _make_slash_command_info(
    name: str,
    source: SlashCommandSource,
    sourceInfo: SourceInfo,
    description: str | None = None,
) -> SlashCommandInfo:
    """A ``SlashCommandInfo`` literal, with ``description`` left out when it is undefined."""
    commandInfo: SlashCommandInfo = {"name": name, "source": source, "sourceInfo": sourceInfo}
    if description is not None:
        commandInfo["description"] = description
    return commandInfo


__all__ = [
    "BUILTIN_SLASH_COMMANDS",
    "BuiltinSlashCommand",
    "SlashCommandInfo",
    "SlashCommandSource",
]

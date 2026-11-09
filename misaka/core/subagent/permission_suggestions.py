"""CCB's permission dialog (77a7934): "Yes", "Yes, and don't ask again …", "No".

What the middle option saves depends on why the tool call asks, as in CCB: a shell command saves
a prefix rule for it (``BashTool/bashPermissions.ts`` ``suggestionForExactCommand``); a path outside
the working directories adds that directory, or for a read a Read rule, for this session
(``BashTool/pathValidation.ts``, ``utils/permissions/filesystem.ts`` ``generateSuggestions``); a file
edit switches this session to acceptEdits; any other tool saves a rule for the whole tool
(``FallbackPermissionRequest.tsx``). The labels are CCB's (``shellPermissionHelpers.tsx``
``generateShellSuggestionsLabel``, ``FilePermissionDialog/permissionOptions.tsx``), without the
mode-cycle shortcut MISAKA has no key for.

A suggestion is CCB's ``PermissionUpdate``: ``addRules`` (behavior allow), ``addDirectories`` or
``setMode``, for ``session`` (this session's own grants, which its sub-agents inherit) or
``localSettings`` (CCB's ``.claude/settings.local.json``; here the project's ``.misaka/settings.json``,
or this session when the project is not trusted).
"""
from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path
from typing import Any

ENV_VAR_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
# CCB SAFE_ENV_VARS: variables that cannot execute code or load libraries, skipped before a prefix.
SAFE_ENV_VARS = frozenset({
    "GOEXPERIMENT", "GOOS", "GOARCH", "CGO_ENABLED", "GO111MODULE",
    "RUST_BACKTRACE", "RUST_LOG",
    "NODE_ENV",
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_DEBUG",
    "ANTHROPIC_API_KEY",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_TIME", "CHARSET",
    "TERM", "COLORTERM", "NO_COLOR", "FORCE_COLOR", "TZ",
    "LS_COLORS", "LSCOLORS", "GREP_COLOR", "GREP_COLORS", "GCC_COLORS",
    "TIME_STYLE", "BLOCK_SIZE", "BLOCKSIZE",
})
_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
YES, NO = "Yes", "No"


def _skip_safe_env(tokens: list[str]) -> int | None:
    """Index of the first token after the leading safe ``VAR=value`` words; None at an unsafe one."""
    i = 0
    while i < len(tokens) and ENV_VAR_ASSIGN_RE.match(tokens[i]):
        if tokens[i].split("=", 1)[0] not in SAFE_ENV_VARS:
            return None
        i += 1
    return i


def get_simple_command_prefix(command: str) -> str | None:
    """CCB ``getSimpleCommandPrefix``: command plus subcommand (``git commit``), or None."""
    tokens = command.split()
    i = _skip_safe_env(tokens)
    if i is None:
        return None
    remaining = tokens[i:]
    if len(remaining) < 2 or not _SUBCOMMAND_RE.match(remaining[1]):
        return None
    return " ".join(remaining[:2])


def _extract_prefix_before_heredoc(command: str) -> str | None:
    """CCB ``extractPrefixBeforeHeredoc``."""
    if "<<" not in command:
        return None
    index = command.index("<<")
    before = command[:index].strip()
    if index <= 0 or not before:
        return None
    prefix = get_simple_command_prefix(before)
    if prefix:
        return prefix
    tokens = before.split()
    i = _skip_safe_env(tokens)
    if i is None or i >= len(tokens):
        return None
    return " ".join(tokens[i:i + 2]) or None


def _add_rule(tool_name: str, rule_content: str | None, destination: str) -> dict[str, Any]:
    rule = {"toolName": tool_name} if rule_content is None else {"toolName": tool_name, "ruleContent": rule_content}
    return {"type": "addRules", "rules": [rule], "behavior": "allow", "destination": destination}


def suggestion_for_exact_command(command: str, tool_name: str = "Bash") -> list[dict[str, Any]]:
    """CCB ``suggestionForExactCommand``: a prefix rule where one is stable, else the command."""
    prefix = _extract_prefix_before_heredoc(command)
    if prefix is None and "\n" in command:
        prefix = command.split("\n")[0].strip() or None
    if prefix is None:
        prefix = get_simple_command_prefix(command)
    if prefix:
        return [_add_rule(tool_name, f"{prefix}:*", "localSettings")]
    return [_add_rule(tool_name, command, "localSettings")]


def get_directory_for_path(path: str) -> str:
    """CCB ``getDirectoryForPath``: the path itself when it is a directory, else its parent."""
    absolute = os.path.abspath(os.path.expanduser(path))
    return absolute if os.path.isdir(absolute) else os.path.dirname(absolute)


def create_read_rule_suggestion(directory: str, destination: str = "session") -> dict[str, Any] | None:
    """CCB ``createReadRuleSuggestion``; spelled ``/dir/**`` (CCB ``//dir/**``), as MISAKA's path
    rules match an absolute path."""
    posix = Path(directory).as_posix()
    if posix == "/":
        return None
    return _add_rule("Read", f"{posix}/**", destination)


def path_suggestions(blocked_path: str, operation: str | None) -> list[dict[str, Any]]:
    """CCB ``pathValidation``'s suggestions for a shell command that reaches ``blocked_path``."""
    directory = get_directory_for_path(blocked_path)
    if operation == "read":
        suggestion = create_read_rule_suggestion(directory)
        updates = [suggestion] if suggestion else []
    else:
        updates = [{"type": "addDirectories", "directories": [directory], "destination": "session"}]
    if operation in ("write", "create"):
        updates.append({"type": "setMode", "mode": "acceptEdits", "destination": "session"})
    return updates


def file_suggestions(path: str, operation: str, mode: str | None, outside: bool) -> list[dict[str, Any]]:
    """CCB ``filesystem.generateSuggestions`` for a file tool."""
    if operation == "read":
        if not outside:
            return []
        suggestion = create_read_rule_suggestion(get_directory_for_path(path))
        return [suggestion] if suggestion else []
    updates = ([{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]
               if (mode or "default") in ("default", "plan") else [])
    if outside:
        updates.append({"type": "addDirectories", "directories": [get_directory_for_path(path)],
                        "destination": "session"})
    return updates


def fallback_suggestions(tool_name: str) -> list[dict[str, Any]]:
    """CCB ``FallbackPermissionRequest``: the whole tool, in the project's settings."""
    return [_add_rule(tool_name, None, "localSettings")]


def _names(paths: list[str]) -> str:
    """CCB ``formatPathList``, as plain text."""
    names = [f"{os.path.basename(path) or path}/" for path in paths]
    if len(names) <= 2:
        return " and ".join(names)
    return f"{names[0]}, {names[1]} and {len(names) - 2} more"


def _commands(commands: list[str]) -> str:
    """CCB ``commandListDisplayTruncated``, as plain text."""
    if len(", ".join(commands)) > 50:
        return "similar"
    if len(commands) <= 2:
        return " and ".join(commands)
    return f"{', '.join(commands[:-1])}, and {commands[-1]}"


def shell_suggestions_label(suggestions: list[dict[str, Any]], cwd: str) -> str | None:
    """CCB ``generateShellSuggestionsLabel``, as plain text."""
    rules = [rule for s in suggestions if s.get("type") == "addRules" for rule in s.get("rules") or []]
    read_paths = [rule["ruleContent"].replace("/**", "") for rule in rules
                  if rule.get("toolName") == "Read" and rule.get("ruleContent")]
    commands = list(dict.fromkeys(
        content.removesuffix(":*")
        for rule in rules if rule.get("toolName") == "Bash" and (content := rule.get("ruleContent"))))
    directories = [d for s in suggestions if s.get("type") == "addDirectories" for d in s.get("directories") or []]
    if read_paths and not directories and not commands:
        return f"Yes, allow reading from {_names(read_paths)} from this project"
    if directories and not read_paths and not commands:
        return f"Yes, and always allow access to {_names(directories)} from this project"
    if commands and not directories and not read_paths:
        return f"Yes, and don't ask again for {_commands(commands)} commands in {cwd}"
    paths = [*directories, *read_paths]
    if paths and not commands:
        return f"Yes, and always allow access to {_names(paths)} from this project"
    if paths and commands:
        if len(paths) == 1 and len(commands) == 1:
            return f"Yes, and allow access to {_names(paths)} and {_commands(commands)} commands"
        return f"Yes, and allow {_names(paths)} access and {_commands(commands)} commands"
    return None


def file_suggestions_label(path: str, operation: str, outside: bool) -> str:
    """CCB ``getFilePermissionOptions``'s session option."""
    if not outside:
        return "Yes, during this session" if operation == "read" else "Yes, allow all edits during this session"
    name = os.path.basename(get_directory_for_path(path)) or "this directory"
    if operation == "read":
        return f"Yes, allow reading from {name}/ during this session"
    return f"Yes, allow all edits in {name}/ during this session"


async def ask_in_ui(ctx: Any, title: str, tool_name: str, tool_input: Any,
                    always: str | None = None) -> str | None:
    """Show CCB's permission dialog at ``ctx``'s UI: returns "yes", "always" or "no"; None when
    nobody can be asked there. ``always`` is the middle option's label, when there is one."""
    if ctx is None or not bool(getattr(ctx, "hasUI", False)):
        return None
    ui = getattr(ctx, "ui", None)
    rendered = json.dumps(tool_input or {}, ensure_ascii=False, sort_keys=True)[:4000]
    question = f"{title}: allow {tool_name}?\n{rendered}"
    select = getattr(ui, "select", None)
    if callable(select):
        value = select(question, [YES, always, NO] if always else [YES, NO])
        choice = await value if inspect.isawaitable(value) else value
        return {YES: "yes", always: "always"}.get(choice, "no") if always else ("yes" if choice == YES else "no")
    confirm = getattr(ui, "confirm", None)
    if not callable(confirm):
        return None
    value = confirm(title, f"Allow {tool_name}?\n{rendered}")
    return "yes" if (await value if inspect.isawaitable(value) else value) else "no"


def save_local_rules(session: Any, rules: list[str]) -> bool:
    """Add allow ``rules`` to the project's settings (CCB destination ``localSettings``); False
    when the project's settings cannot be written (not trusted, or the session runs in the home)."""
    manager = getattr(session, "settingsManager", None)
    storage = getattr(manager, "storage", None)
    if manager is None or not manager.isProjectTrusted() or getattr(storage, "projectSettingsPath", None) is None:
        return False
    settings = manager.getProjectSettings()
    permissions = dict(settings.get("permissions") or {})
    permissions["allow"] = list(dict.fromkeys([*(permissions.get("allow") or []), *rules]))
    settings["permissions"] = permissions
    manager.markProjectModified("permissions", "allow")
    manager.saveProjectSettings(settings)
    return True


def rule_string(rule: dict[str, Any]) -> str:
    """CCB ``permissionRuleValueToString``: ``Tool`` or ``Tool(content)``."""
    content = rule.get("ruleContent")
    return rule["toolName"] if content is None else f"{rule['toolName']}({content})"


__all__ = [
    "ask_in_ui",
    "create_read_rule_suggestion",
    "fallback_suggestions",
    "file_suggestions",
    "file_suggestions_label",
    "get_directory_for_path",
    "get_simple_command_prefix",
    "path_suggestions",
    "rule_string",
    "save_local_rules",
    "shell_suggestions_label",
    "suggestion_for_exact_command",
]

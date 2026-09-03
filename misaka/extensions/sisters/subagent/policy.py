"""Runtime policy carried by Claude-style agent frontmatter.

Tool names are filtered before a child starts.  This module keeps the part a
name-only allowlist cannot express: argument-scoped rules, plan mode, and
agent-local command hooks.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import inspect
import json
import os
import re
import secrets
import shlex
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from misaka.core.platform.prompt_guard import untrusted
from misaka.core.platform.vocabulary import MANAGEMENT_TOOL_NAMES
from misaka.extensions.sisters.subagent.hooks import (
    HOOK_DEFAULT_TIMEOUT,
    HOOK_DEFAULT_TIMEOUTS,
)
from misaka.utils.values import read_field

ALIASES = {"glob": "find"}
PLAN_READ_COMMANDS = frozenset(
    {"cat", "df", "du", "file", "find", "grep", "head", "ls", "pwd", "rg", "stat", "tail", "type", "wc", "which"}
)
PLAN_GIT_COMMANDS = frozenset(
    {"cat-file", "diff", "grep", "log", "ls-files", "ls-tree", "rev-parse", "show", "status"}
)
READ_ONLY_TOOLS = frozenset({"find", "grep", "glob", "ls", "read"})
# Tools that carry their own per-action classification further down
# ``_permission_action``.  Inheriting one of these names from the parent
# session must never short-circuit that classification.
CLASSIFIED_TOOLS = frozenset({"bash", "powershell", "edit", "write"})
ACCEPT_EDITS_COMMANDS = frozenset({"cp", "mkdir", "mv", "rm", "rmdir", "sed", "touch"})
SENSITIVE_DIRECTORIES = frozenset({".claude", ".git", ".idea", ".ssh", ".vscode"})
SENSITIVE_FILES = frozenset(
    {
        ".env",
        ".gitconfig",
        ".mcp.json",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
    }
)

_permission_broker: Any = None
_permission_classifier: Any = None
_async_hook_broker: Any = None
OnceKey = tuple[str, int, int, str]


def set_permission_broker(callback: Any) -> None:
    """Bind the child JSONL permission channel for this process."""

    global _permission_broker
    _permission_broker = callback


def set_permission_classifier(callback: Any) -> None:
    """Bind the transcript-aware auto-mode classifier for this child."""

    global _permission_classifier
    _permission_classifier = callback


def set_async_hook_broker(callback: Any) -> None:
    """Move background command hooks to the durable parent process."""

    global _async_hook_broker
    _async_hook_broker = callback


async def request_permission(payload: Mapping[str, Any]) -> bool:
    """Relay a permission request to this process's parent, if it has one."""

    if not callable(_permission_broker):
        return False
    return bool(await _permission_broker(dict(payload)))


async def classify_permission(payload: Mapping[str, Any]) -> bool:
    if not callable(_permission_classifier):
        return False
    return bool(await _permission_classifier(dict(payload)))


def _tool_name(value: str) -> str:
    name = value.strip().casefold()
    return ALIASES.get(name, name)


def split_rule(spec: str) -> tuple[str, str | None]:
    """Return a normalized tool name and optional permission-rule body."""

    base, separator, tail = str(spec).strip().partition("(")
    rule = tail[:-1].strip() if separator and tail.endswith(")") else None
    return _tool_name(base), rule


def normalize_rule(spec: str) -> str:
    name, rule = split_rule(spec)
    return name if rule is None else f"{name}({rule})"


def _rule_value(tool_name: str, tool_input: Mapping[str, Any]) -> str:
    if tool_name in {"bash", "powershell"}:
        return str(tool_input.get("command") or "")
    for key in ("path", "file_path", "pattern", "query", "subagent_type"):
        if tool_input.get(key) is not None:
            return str(tool_input[key])
    return json.dumps(dict(tool_input), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pattern_matches(pattern: str, value: str) -> bool:
    pattern, value = pattern.strip(), value.strip()
    if not pattern:
        return False
    # Claude permission rules commonly use ``command:*`` for a command plus
    # arbitrary arguments.  Plain comma-separated entries remain exact.
    if pattern.endswith(":*"):
        prefix = pattern[:-2].rstrip()
        return value == prefix or value.startswith(prefix + " ")
    if any(char in pattern for char in "*?["):
        return fnmatch.fnmatchcase(value, pattern)
    return value == pattern


def rule_matches(spec: str, tool_name: str, tool_input: Mapping[str, Any]) -> bool:
    name, body = split_rule(spec)
    if name != _tool_name(tool_name):
        return False
    if body is None:
        return True
    value = _rule_value(name, tool_input)
    return any(_pattern_matches(pattern, value) for pattern in body.split(","))


def _rule_allows(
    layers: Sequence[Sequence[str]], tool_name: str, tool_input: Mapping[str, Any]
) -> bool:
    return any(
        rule_matches(spec, tool_name, tool_input)
        for layer in layers
        for spec in layer
    )


def _has_shell_expansion(command: str) -> bool:
    """Detect active shell syntax while preserving quoted grep/sed patterns."""

    # Shells erase a backslash-newline pair before tokenization.  Reject every
    # multiline command up front so the path validator sees what is executed.
    if "\n" in command or "\r" in command:
        return True

    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if quote == '"':
            if char == "\\":
                escaped = True
            elif char == '"':
                quote = None
            elif char in {"$", "`"}:
                return True
            continue
        if char == "\\":
            escaped = True
        elif char in {"'", '"'}:
            quote = char
        elif char in ";&|><`\n$*?[]{}()":
            return True
    return quote is not None or escaped


def _plan_denial(tool_name: str, tool_input: Mapping[str, Any]) -> str | None:
    name = _tool_name(tool_name)
    if name in {"edit", "write"}:
        return f"permissionMode=plan denied mutating tool {tool_name}"
    # PowerShell has different tokenization, aliases and pipelines.  Until its
    # own read-only classifier exists, plan mode cannot prove a command safe.
    if name == "powershell":
        return "permissionMode=plan denied an unclassified PowerShell command"
    if name != "bash":
        return None
    command = str(tool_input.get("command") or "").strip()
    if not command or _has_shell_expansion(command):
        return "permissionMode=plan denied a non-read-only bash command"
    try:
        argv = shlex.split(command)
    except ValueError:
        return "permissionMode=plan denied an invalid bash command"
    executable = argv[0] if argv else ""
    if executable == "find" and any(
        arg in {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
        or arg.startswith(("-fprint", "-fprintf", "-fls"))
        for arg in argv[1:]
    ):
        return "permissionMode=plan denied a mutating find command"
    if executable == "file" and any(
        arg == "-C" or arg == "--compile" for arg in argv[1:]
    ):
        return "permissionMode=plan denied a mutating file command"
    if executable == "git":
        allowed = len(argv) > 1 and argv[1] in PLAN_GIT_COMMANDS
    else:
        allowed = executable in PLAN_READ_COMMANDS
    return None if allowed else "permissionMode=plan denied a non-read-only bash command"


def _path_in_workspace(raw: str, workspace: str) -> bool:
    try:
        root = Path(workspace).expanduser().resolve()
        candidate = Path(raw).expanduser()
        candidate = candidate if candidate.is_absolute() else root / candidate
        candidate.resolve(strict=False).relative_to(root)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _resolved_path(raw: str, workspace: str) -> Path | None:
    try:
        root = Path(workspace).expanduser().resolve()
        candidate = Path(raw).expanduser()
        return (candidate if candidate.is_absolute() else root / candidate).resolve(
            strict=False
        )
    except (OSError, RuntimeError, ValueError):
        return None


def _sensitive_path(raw: str, workspace: str) -> bool:
    candidate = _resolved_path(raw, workspace)
    if candidate is None:
        return True
    lowered_parts = {part.casefold() for part in candidate.parts}
    if lowered_parts & SENSITIVE_DIRECTORIES:
        return True
    name = candidate.name.casefold()
    return name in SENSITIVE_FILES or name.startswith(".env.")


def _path_rule_allows(
    layers: Sequence[Sequence[str]], tool_name: str, raw: str, workspace: str
) -> bool:
    values = [raw]
    resolved = _resolved_path(raw, workspace)
    if resolved is not None:
        values.append(str(resolved))
    # A bare ``Read`` grant is not an approval to cross the workspace
    # boundary.  Only an argument-scoped path rule can do that.
    return any(
        body is not None
        and name == _tool_name(tool_name)
        and any(rule_matches(spec, tool_name, {"path": value}) for value in values)
        for layer in layers
        for spec in layer
        for name, body in (split_rule(spec),)
    )


def _edit_in_workspace(tool_input: Mapping[str, Any], workspace: str) -> bool:
    raw = tool_input.get("path") or tool_input.get("file_path")
    return (
        isinstance(raw, str)
        and bool(raw.strip())
        and _path_in_workspace(raw, workspace)
        and not _sensitive_path(raw, workspace)
    )


def _bash_read_paths(command: str) -> list[str] | None:
    """Extract path operands for the read-only Bash subset; None is ambiguous."""

    if _has_shell_expansion(command):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return []
    executable = argv[0]
    args = argv[1:]
    if executable not in PLAN_READ_COMMANDS and executable != "git":
        return None
    if executable == "pwd":
        return [] if all(item in {"-L", "-P", "--logical", "--physical"} for item in args) else None
    if executable in {"type", "which"}:
        if any(item.startswith("--") and item not in {"--all", "--skip-alias"} for item in args):
            return None
        return [item for item in args if "/" in item or item.startswith((".", "~"))]
    if executable == "git":
        paths: list[str] = []
        index = 0
        while index < len(args) and args[index].startswith("-"):
            option = args[index]
            if option == "-C":
                if index + 1 >= len(args):
                    return None
                paths.append(args[index + 1])
                index += 2
                continue
            if option.startswith("-C") and len(option) > 2:
                paths.append(option[2:])
                index += 1
                continue
            if option in {"--git-dir", "--work-tree"}:
                if index + 1 >= len(args):
                    return None
                paths.append(args[index + 1])
                index += 2
                continue
            if option.startswith(("--git-dir=", "--work-tree=")):
                paths.append(option.split("=", 1)[1])
                index += 1
                continue
            if option == "-c":
                if index + 1 >= len(args):
                    return None
                index += 2
                continue
            if option in {"--no-pager", "--paginate", "-P", "-p", "--literal-pathspecs"}:
                index += 1
                continue
            return None
        if index >= len(args) or args[index] not in PLAN_GIT_COMMANDS:
            return None
        index += 1
        after_separator = False
        while index < len(args):
            item = args[index]
            index += 1
            if item == "--":
                after_separator = True
                continue
            if not after_separator and item.startswith("-"):
                # Options with external programs, output files, or file-fed
                # pathspecs are not a read-only fast path.
                if item.startswith(
                    ("--output", "--ext-diff", "--textconv", "--pathspec-from-file")
                ):
                    return None
                if "=" in item:
                    # Formatting and display values are harmless; unknown
                    # attached values remain ambiguous and require approval.
                    key = item.split("=", 1)[0]
                    if key not in {
                        "--format", "--pretty", "--date", "--color", "--stat-width",
                        "--stat-name-width", "--abbrev", "--max-count", "--skip",
                    }:
                        return None
                continue
            if executable == "git" and ":" in item and not item.startswith("http"):
                revision_path = item.split(":", 1)[1]
                if revision_path:
                    paths.append(revision_path)
            if after_separator or item.startswith(("/", "~", "..", "./")) or "/" in item:
                paths.append(item)
        return paths
    if executable == "find":
        index = 0
        while index < len(args):
            if args[index] in {"-H", "-L", "-P", "-E", "-X", "-x", "-s", "-d"} or re.fullmatch(r"-O[0-3]", args[index]):
                index += 1
                continue
            if args[index] == "-D":
                if index + 1 >= len(args):
                    return None
                index += 2
                continue
            break
        if index < len(args) and args[index] == "--":
            index += 1
        if index < len(args) and args[index].startswith("-files0-from"):
            return None
        expression_starters = {
            "-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-type",
            "-xtype", "-size", "-user", "-group", "-uid", "-gid", "-nouser",
            "-nogroup", "-perm", "-links", "-inum", "-samefile", "-fstype",
            "-newer", "-newermt", "-empty", "-true", "-false", "-readable",
            "-writable", "-executable", "-maxdepth", "-mindepth", "-mount",
            "-xdev", "-depth", "-daystart", "-follow", "-noleaf", "-warn",
            "-nowarn", "-print", "-print0", "-prune", "-quit",
        }
        if (
            index < len(args)
            and args[index].startswith("-")
            and args[index] not in expression_starters
        ):
            return None
        paths: list[str] = []
        while index < len(args):
            item = args[index]
            if item.startswith("-") or item in {"!", "(", ")"}:
                break
            paths.append(item)
            index += 1
        paths = paths or ["."]
        path_predicates = {"-newer", "-anewer", "-cnewer", "-samefile", "-f"}
        while index < len(args):
            predicate = args[index]
            index += 1
            if predicate.startswith("-files0-from"):
                return None
            consumes_path = predicate in path_predicates
            newer = re.fullmatch(r"-newer([aBcm]?)([aBcmt])", predicate)
            if newer is not None and newer.group(2) != "t":
                consumes_path = True
            if consumes_path:
                if index >= len(args):
                    return None
                paths.append(args[index])
                index += 1
        return paths

    if executable in {"grep", "rg"}:
        paths: list[str] = []
        positionals: list[str] = []
        pattern_supplied = False
        after_separator = False
        index = 0
        no_value = {
            "-E", "-F", "-G", "-P", "-i", "-v", "-w", "-x", "-n", "-H", "-h",
            "-l", "-L", "-c", "-o", "-q", "-s", "-r", "-R", "-a", "-I", "-U",
            "--extended-regexp", "--fixed-strings", "--basic-regexp", "--perl-regexp",
            "--ignore-case", "--invert-match", "--word-regexp", "--line-regexp",
            "--line-number", "--with-filename", "--no-filename", "--files-with-matches",
            "--files-without-match", "--count", "--only-matching", "--quiet", "--silent",
            "--recursive", "--dereference-recursive", "--text", "--binary", "--hidden",
            "--no-ignore", "--no-messages",
        }
        scalar_options = {
            "-A", "-B", "-C", "-m", "--after-context", "--before-context",
            "--context", "--max-count", "--label", "--binary-files", "--directories",
            "--devices", "--include", "--exclude", "--glob", "-g", "--iglob",
            "--type", "-t", "--type-not", "-T", "--type-add", "--engine", "--sort",
        }
        while index < len(args):
            item = args[index]
            index += 1
            if after_separator:
                positionals.append(item)
                continue
            if item == "--":
                after_separator = True
                continue
            if item in {"-e", "--regexp"}:
                if index >= len(args):
                    return None
                index += 1
                pattern_supplied = True
                continue
            if item.startswith(("-e", "--regexp=")) and item not in no_value:
                pattern_supplied = True
                continue
            if item in {"-f", "--file"}:
                if index >= len(args):
                    return None
                paths.append(args[index])
                index += 1
                pattern_supplied = True
                continue
            if item.startswith("--file="):
                paths.append(item.split("=", 1)[1])
                pattern_supplied = True
                continue
            if item in scalar_options:
                if index >= len(args):
                    return None
                index += 1
                continue
            if any(item.startswith(option + "=") for option in scalar_options if option.startswith("--")):
                continue
            if re.fullmatch(r"-[ABCm]\d+", item):
                continue
            if item in no_value or (item.startswith("-") and len(item) > 2 and all(f"-{c}" in no_value for c in item[1:])):
                continue
            if item.startswith("-"):
                return None
            positionals.append(item)
        if not pattern_supplied:
            if not positionals:
                return None
            positionals.pop(0)
        paths.extend(positionals or ["."])
        return paths

    positional: list[str] = []
    after_separator = False
    index = 0
    file_options: set[str] = set()
    if executable == "du":
        file_options.update({"--files0-from", "-X", "--exclude-from"})
    if executable == "wc":
        file_options.add("--files0-from")
    scalar_options: set[str] = set()
    if executable in {"head", "tail"}:
        scalar_options.update({"-n", "-c", "--lines", "--bytes"})
    if executable == "du":
        scalar_options.update({"--max-depth", "-d", "--block-size", "-B", "--exclude"})
    if executable == "ls":
        scalar_options.update(
            {
                "--block-size", "--time-style", "--color", "--sort", "--format",
                "--quoting-style", "--hide", "--ignore", "--indicator-style",
                "--tabsize", "--width",
            }
        )
    if executable == "stat":
        scalar_options.update({"-c", "--format", "--printf"})
    if executable == "file":
        scalar_options.update({"-e", "--exclude", "-F", "--separator"})
    while index < len(args):
        item = args[index]
        index += 1
        if after_separator:
            positional.append(item)
            continue
        if item == "--":
            after_separator = True
            continue
        if executable == "file" and item in {"-f", "--files-from", "-m", "--magic-file"}:
            if index >= len(args):
                return None
            positional.append(args[index])
            index += 1
            continue
        if executable == "file" and item.startswith(("--files-from=", "--magic-file=")):
            positional.append(item.split("=", 1)[1])
            continue
        if executable == "file" and re.match(r"^-[fm].+", item):
            positional.append(item[2:])
            continue
        if item in file_options:
            if index >= len(args):
                return None
            positional.append(args[index])
            index += 1
            continue
        if any(item.startswith(option + "=") for option in file_options):
            positional.append(item.split("=", 1)[1])
            continue
        if executable == "du" and item.startswith("-X") and len(item) > 2:
            positional.append(item[2:])
            continue
        if item in scalar_options:
            if index >= len(args):
                return None
            index += 1
            continue
        if any(item.startswith(option + "=") for option in scalar_options if option.startswith("--")):
            continue
        if executable in {"head", "tail"} and re.fullmatch(r"-[nc]?\d+", item):
            continue
        if item.startswith("--") and "=" in item:
            return None
        if item.startswith("-"):
            continue
        positional.append(item)
    if executable in {"head", "tail"}:
        positional = [item for item in positional if not item.isdigit()]
    return positional


def _bash_has_restricted_path(
    command: str, workspace: str, layers: Sequence[Sequence[str]]
) -> bool:
    paths = _bash_read_paths(command)
    if paths is None:
        if _has_shell_expansion(command):
            return True
        try:
            argv = shlex.split(command)
        except ValueError:
            return True
        if not argv:
            return False
        # A recognized read command with syntax we could not prove safe must
        # fail closed.  Other exact Bash grants may still run commands without
        # path operands (for example ``pytest``).
        if argv[0] in PLAN_READ_COMMANDS or argv[0] == "git":
            return True
        candidates: list[str] = []
        if "/" in argv[0] or argv[0].startswith((".", "~")):
            candidates.append(argv[0])
        after_separator = False
        path_commands = ACCEPT_EDITS_COMMANDS | {"chmod", "chown", "install", "ln"}
        for item in argv[1:]:
            if item == "--":
                after_separator = True
                continue
            if not after_separator and item.startswith("-"):
                if "=" in item:
                    value = item.split("=", 1)[1]
                    if value.startswith(("/", "~", ".")) or "/" in value:
                        candidates.append(value)
                continue
            if (
                argv[0] in path_commands
                or item.startswith(("/", "~", "."))
                or "/" in item
                or _resolved_path(item, workspace) is not None
                and _resolved_path(item, workspace).exists()
            ):
                candidates.append(item)
        paths = candidates
    for raw in paths:
        if _sensitive_path(raw, workspace):
            return True
        if not _path_in_workspace(raw, workspace) and not _path_rule_allows(
            layers, "read", raw, workspace
        ):
            return True
    return False


def _safe_sed_script(script: str) -> bool:
    """Reject sed's file-I/O and command-execution language extensions."""

    address = r"(?:\d+|\$|/[^/\n]*/|\?[^?\n]*\?)"
    if re.search(
        rf"(?:^|[;{{}}])\s*(?:{address}(?:\s*,\s*{address})?\s*)?!?\s*[erRwW](?=\s|$)",
        script,
    ):
        return False

    def strip_address(value: str) -> str:
        value = value.lstrip()
        if not value:
            return value
        if value[0].isdigit() or value[0] == "$":
            match = re.match(r"(?:\d+|\$)(?:\s*,\s*(?:\d+|\$))?", value)
            return value[match.end() :] if match else value
        if value[0] in {"/", "?"}:
            delimiter = value[0]
            escaped = False
            for index, char in enumerate(value[1:], 1):
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == delimiter:
                    return value[index + 1 :]
        return value

    # Splitting a script with a literal semicolon inside a regex is
    # intentionally conservative: uncertain scripts go through approval.
    for raw in script.split(";"):
        part = raw.strip().lstrip("{").rstrip("}").strip()
        if not part:
            continue
        part = strip_address(part).lstrip()
        if part.startswith(","):
            part = strip_address(part[1:]).lstrip()
        if part.startswith("!"):
            part = part[1:].lstrip()
        if not part:
            return False
        command = part[0]
        if command in {"e", "r", "R", "w", "W"}:
            return False
        if command != "s":
            continue
        if len(part) < 2 or part[1].isalnum() or part[1].isspace():
            return False
        delimiter = part[1]
        escaped = False
        separators = 0
        end = None
        for index, char in enumerate(part[2:], 2):
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == delimiter:
                separators += 1
                if separators == 2:
                    end = index
                    break
        if end is None:
            return False
        flags = part[end + 1 :].strip()
        if any(flag in flags for flag in "ewW"):
            return False
    return True


def _accept_edits_bash(command: str, workspace: str) -> bool:
    if not command.strip() or re.search(r"[`\n<>$*?\[\]{}]|\$\(", command):
        return False
    pieces = [
        part.strip()
        for part in re.split(r"(?:&&|\|\||[;&|])", command)
        if part.strip()
    ]
    if not pieces:
        return False
    for piece in pieces:
        try:
            argv = shlex.split(piece)
        except ValueError:
            return False
        if not argv or argv[0] not in ACCEPT_EDITS_COMMANDS:
            return False
        # Apply path constraints before the acceptEdits fast path.
        # path.  Bare operands resolve inside cwd; explicit escaping operands
        # must remain inside this worker's workspace.
        executable = argv[0]
        operands: list[str] = []
        sed_scripts: list[str] = []
        after_separator = False
        script_found = False
        index = 1
        while index < len(argv):
            argument = argv[index]
            index += 1
            if after_separator:
                if executable == "sed" and not script_found:
                    script_found = True
                else:
                    operands.append(argument)
                continue
            if argument == "--":
                after_separator = True
                continue
            if executable == "sed":
                if argument in {"-e", "--expression"}:
                    if index >= len(argv):
                        return False
                    sed_scripts.append(argv[index])
                    index += 1
                    script_found = True
                    continue
                if argument.startswith("-e") and argument != "-e":
                    sed_scripts.append(argument[2:])
                    script_found = True
                    continue
                if argument.startswith("--expression="):
                    sed_scripts.append(argument.split("=", 1)[1])
                    script_found = True
                    continue
                if argument in {"-f", "--file"}:
                    if index >= len(argv):
                        return False
                    operands.append(argv[index])
                    index += 1
                    script_found = True
                    continue
                if argument.startswith("-f") and argument != "-f":
                    operands.append(argument[2:])
                    script_found = True
                    continue
                if argument.startswith("--file="):
                    operands.append(argument.split("=", 1)[1])
                    script_found = True
                    continue
                if argument.startswith("-"):
                    if "=" in argument and not argument.startswith("--in-place="):
                        return False
                    continue
                if not script_found:
                    sed_scripts.append(argument)
                    script_found = True
                    continue
                operands.append(argument)
                continue
            if executable == "touch" and argument in {"-r", "--reference"}:
                if index >= len(argv):
                    return False
                operands.append(argv[index])
                index += 1
                continue
            if executable == "touch" and argument.startswith("--reference="):
                operands.append(argument.split("=", 1)[1])
                continue
            if executable == "touch" and argument.startswith("-r") and len(argument) > 2:
                operands.append(argument[2:])
                continue
            if argument in {"-t", "--target-directory"}:
                if index >= len(argv):
                    return False
                operands.append(argv[index])
                index += 1
                continue
            if argument.startswith("--target-directory="):
                operands.append(argument.split("=", 1)[1])
                continue
            if argument.startswith("-t") and len(argument) > 2 and executable in {"cp", "mv"}:
                operands.append(argument[2:])
                continue
            if argument.startswith("-"):
                if "=" in argument:
                    return False
                continue
            operands.append(argument)
        if executable == "sed" and (
            not sed_scripts or not all(_safe_sed_script(script) for script in sed_scripts)
        ):
            return False
        for operand in operands:
            if not _path_in_workspace(operand, workspace) or _sensitive_path(
                operand, workspace
            ):
                return False
    return True


def _permission_restriction(
    layers: Sequence[Sequence[str]],
    tool_name: str,
    tool_input: Mapping[str, Any],
    workspace: str,
    vocabulary: frozenset[str] = frozenset(),
) -> str | None:
    """Return a workspace/protected-path guard that grants cannot bypass."""

    name = _tool_name(tool_name)
    raw = tool_input.get("path") or tool_input.get("file_path")
    # An inherited tool reaches the same path guard as the built-in read
    # tools: gaining a name must never gain a way out of the workspace.
    guarded = name in READ_ONLY_TOOLS or (
        name in vocabulary and name not in CLASSIFIED_TOOLS
    )
    if guarded and isinstance(raw, str) and raw.strip():
        if _sensitive_path(raw, workspace):
            return f"protected path requires approval: {raw}"
        if not _path_in_workspace(raw, workspace) and not _path_rule_allows(
            layers, name, raw, workspace
        ):
            return f"path is outside the agent workspace: {raw}"
    if name in {"edit", "write"}:
        if not isinstance(raw, str) or not raw.strip():
            return "mutating tool did not provide a valid path"
        if _sensitive_path(raw, workspace):
            return f"protected path requires approval: {raw}"
        if not _path_in_workspace(raw, workspace):
            return f"path is outside the agent workspace: {raw}"
    if name == "bash":
        command = str(tool_input.get("command") or "")
        if _bash_has_restricted_path(command, workspace, layers):
            return "bash command has an external, protected, or ambiguous path"
    return None


def _permission_action(
    mode: str | None,
    layers: Sequence[Sequence[str]],
    tool_name: str,
    tool_input: Mapping[str, Any],
    workspace: str,
    vocabulary: frozenset[str] = frozenset(),
) -> tuple[str, str | None]:
    """Return ``allow``, ``ask``, ``classify`` or ``deny`` by mode precedence.

    ``vocabulary`` is the set of tool names inherited from the parent session.
    It answers only "may this child use a tool by this name at all"; every
    argument-scoped check below still runs, and an empty set reproduces the
    behaviour of the hard-coded name tables on their own.
    """

    effective = mode or "default"
    name = _tool_name(tool_name)
    if name in MANAGEMENT_TOOL_NAMES or effective == "bypassPermissions":
        return "allow", None

    if effective == "plan":
        reason = _plan_denial(name, tool_input)
        if reason:
            return "deny", reason
        # Plan's denial is decided before the workspace guard, which answers
        # ``ask``.  A guarded tool must not turn a plan denial into something a
        # parent can approve, so the worse the path the weaker the decision.
        if name not in READ_ONLY_TOOLS and name != "bash" and not _rule_allows(
            layers, name, tool_input
        ):
            return "deny", f"permissionMode=plan denied non-read-only tool {tool_name}"

    restriction = _permission_restriction(
        layers, name, tool_input, workspace, vocabulary
    )
    if restriction:
        reason = f"Workspace safety requires approval: {restriction}"
        if effective in {"dontAsk", "auto"}:
            return "deny", reason
        return "ask", reason

    if effective == "plan":
        return "allow", None
    if _rule_allows(layers, name, tool_input):
        return "allow", None
    # plan mode above keeps the strict built-in read-only table on purpose; an
    # inherited name is not evidence that a tool only reads.  Under ``auto`` it
    # is no evidence either: the transcript classifier is that mode's per-action
    # check, and a name must not buy a way around it.
    if name in READ_ONLY_TOOLS or (
        effective != "auto"
        and name in vocabulary
        and name not in CLASSIFIED_TOOLS
    ):
        return "allow", None
    if name == "bash" and _plan_denial(name, tool_input) is None:
        return "allow", None

    if effective in {"acceptEdits", "auto"}:
        if name in {"edit", "write"} and _edit_in_workspace(tool_input, workspace):
            return "allow", None
        if name == "bash" and _accept_edits_bash(
            str(tool_input.get("command") or ""), workspace
        ):
            return "allow", None
    if effective == "auto":
        return "classify", "permissionMode=auto requires transcript classification"

    if effective == "dontAsk":
        return "deny", "permissionMode=dontAsk denied an action that requires approval"
    return "ask", f"permissionMode={effective} requires parent approval for {tool_name}"


class AgentPolicy:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.layers = tuple(tuple(layer) for layer in getattr(context, "tool_rule_layers", ()) or ())
        self.permission_mode = getattr(context, "permission_mode", None)
        # Names this child inherited from its parent session.  Absent means the
        # parent could not report one, and the hard-coded tables decide alone.
        self.vocabulary = frozenset(
            _tool_name(str(item))
            for item in (getattr(context, "tool_vocabulary", None) or ())
            if str(item).strip()
        )
        raw_hooks = getattr(context, "agent_hooks", None)
        try:
            hooks = json.loads(raw_hooks) if raw_hooks else {}
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid agent hooks JSON: {error}") from error
        if not isinstance(hooks, dict):
            raise ValueError("Agent hooks must be an event mapping")  # noqa: TRY004 - callers treat bad input as ValueError
        self.hooks: dict[str, Any] = hooks
        self.harness: Any = None
        raw_once_path = os.environ.get("MISAKA_SUBAGENT_HOOK_ONCE_FILE")
        self.once_path = (
            Path(raw_once_path).expanduser() if raw_once_path else None
        )
        self.once: set[OnceKey] = self._load_once()
        # A one-shot hook is reserved while its matched batch is running.  It
        # is persisted only after successful execution/submission, matching
        # Claude's onHookSuccess behavior without allowing concurrent batches
        # to launch the same hook twice.
        self.once_inflight: set[OnceKey] = set()
        self.background_hooks: set[asyncio.Task[None]] = set()
        self.stop_hook_active = False
        self._validate_hooks()

    def _load_once(self) -> set[OnceKey]:
        if self.once_path is None:
            return set()
        try:
            loaded = json.loads(self.once_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return set()
        if not isinstance(loaded, list):
            return set()
        result: set[OnceKey] = set()
        for item in loaded:
            if (
                isinstance(item, list)
                and len(item) == 4
                and isinstance(item[0], str)
                and isinstance(item[1], int)
                and isinstance(item[2], int)
                and isinstance(item[3], str)
            ):
                result.add((item[0], item[1], item[2], item[3]))
        return result

    @staticmethod
    def _once_fingerprint(
        matcher: Mapping[str, Any], hook: Mapping[str, Any]
    ) -> str:
        encoded = json.dumps(
            {"matcher": matcher.get("matcher"), "hook": dict(hook)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]

    def _persist_once(self) -> None:
        if self.once_path is None:
            return
        path = self.once_path
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary.write_text(
                json.dumps(sorted(self.once), ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _validate_hooks(self) -> None:
        for event, matchers in self.hooks.items():
            if not isinstance(matchers, list):
                raise ValueError(f"Agent hooks.{event} must be a list")  # noqa: TRY004 - callers treat bad input as ValueError
            for matcher in matchers:
                commands = matcher.get("hooks") if isinstance(matcher, dict) else None
                if not isinstance(commands, list):
                    raise ValueError(f"Agent hooks.{event} entries need a hooks list")  # noqa: TRY004 - callers treat bad input as ValueError
                for hook in commands:
                    if not isinstance(hook, dict):
                        raise ValueError(f"Agent hooks.{event} entries must be objects")  # noqa: TRY004 - callers treat bad input as ValueError
                    kind = str(hook.get("type") or "command")
                    if kind not in {"command", "prompt", "agent", "http"}:
                        raise ValueError(f"Agent hooks.{event} has unsupported type {kind!r}")
                    required = "url" if kind == "http" else "command" if kind == "command" else "prompt"
                    if not isinstance(hook.get(required), str) or not str(hook[required]).strip():
                        raise ValueError(
                            f"Agent hooks.{event} {kind} hook needs a non-empty {required}"
                        )
                    try:
                        raw_timeout = hook.get("timeout")
                        timeout = (HOOK_DEFAULT_TIMEOUTS.get(str(kind), HOOK_DEFAULT_TIMEOUT)
                                   if raw_timeout is None else float(raw_timeout))
                    except (TypeError, ValueError) as error:
                        raise ValueError(
                            f"Agent hooks.{event} has an invalid timeout"
                        ) from error
                    if timeout <= 0:
                        raise ValueError(f"Agent hooks.{event} timeout must be positive")
                    if kind != "command" and (
                        hook.get("async") or hook.get("asyncRewake")
                    ):
                        raise ValueError(
                            f"Agent hooks.{event} {kind} hooks cannot run asynchronously"
                        )

    @staticmethod
    def _matcher_matches(pattern: Any, subject: str) -> bool:
        if pattern in (None, "", "*"):
            return True
        try:
            return re.search(str(pattern), subject, re.IGNORECASE) is not None
        except re.error:
            return fnmatch.fnmatchcase(subject.casefold(), str(pattern).casefold())

    def _iter_hooks(
        self, event_name: str, subject: str, tool_name: str = "", tool_input: Mapping[str, Any] | None = None
    ) -> Iterable[tuple[OnceKey, Mapping[str, Any]]]:
        names = (event_name, "Stop") if event_name == "SubagentStop" else (event_name,)
        for name in names:
            for matcher_index, matcher in enumerate(self.hooks.get(name, ())):
                if not self._matcher_matches(matcher.get("matcher"), subject):
                    continue
                for hook_index, hook in enumerate(matcher["hooks"]):
                    condition = hook.get("if")
                    if condition and not rule_matches(str(condition), tool_name, tool_input or {}):
                        continue
                    key = (
                        name,
                        matcher_index,
                        hook_index,
                        self._once_fingerprint(matcher, hook),
                    )
                    if hook.get("once"):
                        if key in self.once or key in self.once_inflight:
                            continue
                        self.once_inflight.add(key)
                    yield key, hook

    def _settle_once(
        self, key: OnceKey, hook: Mapping[str, Any], success: bool
    ) -> None:
        if not hook.get("once"):
            return
        self.once_inflight.discard(key)
        if success:
            self.once.add(key)
            self._persist_once()

    async def _execute_hook_with_outcome(
        self, hook: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        from misaka.extensions.sisters.subagent import hooks as subagent_hooks

        async def execute() -> tuple[dict[str, Any], bool]:
            kind = str(hook.get("type") or "command").casefold()
            if kind == "command":
                result, exit_code = await subagent_hooks.execute_async_command_hook(
                    hook,
                    payload,
                    cwd=getattr(self.context, "workspace", None) or None,
                    environ=os.environ,
                )
                return result, exit_code == 0

            result = await subagent_hooks.execute_hook(
                hook,
                payload,
                cwd=getattr(self.context, "workspace", None) or None,
                environ=os.environ,
            )
            decision = str(result.get("decision") or "passthrough")
            reason = str(result.get("reason") or "")
            if kind == "http":
                success = not (
                    decision == "passthrough" and reason.startswith("HTTP hook ")
                )
            elif kind in {"prompt", "agent"}:
                success = decision not in {"deny", "ask"} and not (
                    decision == "passthrough"
                    and reason.startswith(f"{kind} hook ")
                )
            else:
                success = False
            return result, success

        if hook.get("async") or hook.get("asyncRewake"):
            if callable(_async_hook_broker):
                try:
                    submitted = _async_hook_broker(dict(hook), dict(payload))
                    if inspect.isawaitable(submitted):
                        await submitted
                except Exception as error:  # noqa: BLE001 - hooks fail open
                    # Claude falls back to the already-defined synchronous
                    # path when a process cannot be backgrounded.  This both
                    # fails open on execution errors and preserves a real
                    # blocking result from the hook itself.
                    _ = error
                    return await execute()
                return (
                    {
                        "allowed": True,
                        "decision": "passthrough",
                        "additional_context": None,
                        "reason": None,
                    },
                    True,
                )

            async def background() -> None:
                result, exit_code = (
                    await subagent_hooks.execute_async_command_hook(
                        hook,
                        payload,
                        cwd=getattr(self.context, "workspace", None) or None,
                        environ=os.environ,
                    )
                )
                if not hook.get("asyncRewake") or exit_code != 2:
                    return
                sender = getattr(self.harness, "sendMessage", None)
                if callable(sender):
                    sender(
                        {
                            "customType": "hook-notification",
                            "content": result["reason"] or "Asynchronous agent hook blocked",
                            "display": True,
                        },
                        {"deliverAs": "followUp", "triggerTurn": True},
                    )

            task = asyncio.create_task(background())
            self.background_hooks.add(task)

            def finished(done: asyncio.Task[None]) -> None:
                self.background_hooks.discard(done)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
            return (
                {
                    "allowed": True,
                    "decision": "passthrough",
                    "additional_context": None,
                    "reason": None,
                },
                True,
            )
        return await execute()

    async def _execute_hook(
        self, hook: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Compatibility wrapper used by direct hook contract tests."""

        result, _success = await self._execute_hook_with_outcome(hook, payload)
        return result

    async def _execute_hooks(
        self,
        event_name: str,
        subject: str,
        payload: Mapping[str, Any],
        tool_name: str = "",
        tool_input: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        matched = list(
            self._iter_hooks(event_name, subject, tool_name, tool_input)
        )
        if not matched:
            return []

        async def run(
            key: OnceKey, hook: Mapping[str, Any]
        ) -> dict[str, Any]:
            success = False
            try:
                result, success = await self._execute_hook_with_outcome(
                    hook, payload
                )
                return result
            finally:
                self._settle_once(key, hook, success)

        # Claude runs every hook in a matched batch concurrently.
        return list(
            await asyncio.gather(
                *(run(key, hook) for key, hook in matched)
            )
        )

    def _payload(self, event_name: str, **fields: Any) -> dict[str, Any]:
        agent_id = os.environ.get("MISAKA_SUBAGENT_ID")
        transcript = os.environ.get("MISAKA_SUBAGENT_TRANSCRIPT") or ""
        payload = {
            "session_id": os.environ.get("MISAKA_SUBAGENT_PARENT_SESSION_ID") or agent_id or "",
            "transcript_path": transcript,
            "hook_event_name": event_name,
            "agent_id": agent_id,
            "agent_type": getattr(self.context, "role", ""),
            "cwd": getattr(self.context, "workspace", ""),
            **fields,
        }
        if self.permission_mode:
            payload["permission_mode"] = self.permission_mode
        return {key: value for key, value in payload.items() if value is not None}

    async def before_agent(self, event: Any) -> Any:
        payload = self._payload("SubagentStart", prompt=str(read_field(event, "prompt", "")))
        context: list[str] = []
        results = await self._execute_hooks(
            "SubagentStart",
            str(getattr(self.context, "role", "")),
            payload,
        )
        for result in results:
            if result["decision"] in {"deny", "ask"} or not result["allowed"]:
                return {
                    "block": True,
                    "reason": result["reason"] or "SubagentStart hook blocked",
                }
            if result["additional_context"]:
                context.append(str(result["additional_context"]))
        if not context:
            return None
        now = int(time.time() * 1000)
        return {
            # ExtensionRunner's public contract is singular ``message``; it
            # aggregates those into the ``messages`` consumed by AgentSession.
            "message": {
                "role": "custom",
                "customType": "hook_additional_context",
                "content": [{"type": "text", "text": untrusted("hook", "\n".join(context))}],
                "display": False,
                "timestamp": now,
            }
        }

    async def before_tool(self, event: Any) -> Any:
        name = str(read_field(event, "toolName", ""))
        tool_input = read_field(event, "input", {})
        tool_input = tool_input if isinstance(tool_input, Mapping) else {}
        workspace = str(getattr(self.context, "workspace", "") or os.getcwd())
        payload = self._payload(
            "PreToolUse",
            tool_name=name,
            tool_input=dict(tool_input),
            tool_use_id=read_field(event, "toolCallId"),
        )
        hook_results = await self._execute_hooks(
            "PreToolUse", name, payload, name, tool_input
        )
        hook_decision = "passthrough"
        for result in hook_results:
            decision = str(result["decision"])
            if decision == "deny":
                return {
                    "block": True,
                    "reason": result["reason"] or "PreToolUse hook blocked",
                }
            if decision == "ask":
                hook_decision = "ask"
            elif decision == "allow" and hook_decision != "ask":
                hook_decision = "allow"

        action, reason = _permission_action(
            self.permission_mode,
            self.layers,
            name,
            tool_input,
            workspace,
            self.vocabulary,
        )
        restriction = _permission_restriction(
            self.layers, name, tool_input, workspace, self.vocabulary
        )
        if hook_decision == "ask" and action != "deny":
            action = "ask"
            reason = reason or "PreToolUse hook requested parent approval"
        elif hook_decision == "allow" and action in {"ask", "classify"} and not restriction:
            action = "allow"
            reason = None
        if action == "classify":
            try:
                classifier_allowed = await classify_permission(
                    {
                        "toolName": name,
                        "toolInput": dict(tool_input),
                        "toolCallId": read_field(event, "toolCallId"),
                        "mode": "auto",
                    }
                )
            except Exception as error:  # noqa: BLE001 - auto mode fails closed
                return {
                    "block": True,
                    "reason": f"Auto-mode classifier failed: {error}",
                }
            if not classifier_allowed:
                return {
                    "block": True,
                    "reason": "permissionMode=auto transcript classifier denied this action",
                }
            action, reason = "allow", None
        if action == "deny":
            return {"block": True, "reason": reason or "Agent permission denied"}
        if action == "ask":
            permission_payload = self._payload(
                "PermissionRequest",
                tool_name=name,
                tool_input=dict(tool_input),
                tool_use_id=read_field(event, "toolCallId"),
                permission_suggestions=[],
            )
            permission_results = await self._execute_hooks(
                "PermissionRequest", name, permission_payload, name, tool_input
            )
            permission_allow = False
            for result in permission_results:
                decision = str(result["decision"])
                if decision == "deny":
                    return {
                        "block": True,
                        "reason": result["reason"] or "PermissionRequest hook denied the action",
                    }
                if decision == "allow":
                    permission_allow = True
            if permission_allow and not restriction:
                action = "allow"

            can_prompt = bool(getattr(self.context, "permission_can_prompt", False))
            if action == "ask" and (not can_prompt or not callable(_permission_broker)):
                return {
                    "block": True,
                    "reason": reason
                    or "Background agent cannot display a permission prompt",
                }
            if action == "ask":
                try:
                    approved = await request_permission(
                        {
                            "toolName": name,
                            "toolInput": dict(tool_input),
                            "toolCallId": read_field(event, "toolCallId"),
                            "mode": self.permission_mode or "default",
                            "reason": reason,
                        }
                    )
                except Exception as error:  # noqa: BLE001 - an ask path fails closed
                    return {
                        "block": True,
                        "reason": f"Parent permission request failed: {error}",
                    }
                if not approved:
                    return {
                        "block": True,
                        "reason": reason or "Parent denied agent permission",
                    }
        return None

    async def after_tool(self, event: Any) -> None:
        name = str(read_field(event, "toolName", ""))
        tool_input = read_field(event, "input", {})
        tool_input = tool_input if isinstance(tool_input, Mapping) else {}
        event_name = "PostToolUseFailure" if bool(read_field(event, "isError", False)) else "PostToolUse"
        payload = self._payload(
            event_name,
            tool_name=name,
            tool_input=dict(tool_input),
            tool_use_id=read_field(event, "toolCallId"),
            tool_response={
                "content": read_field(event, "content"),
                "details": read_field(event, "details"),
                "is_error": bool(read_field(event, "isError", False)),
            },
        )
        await self._execute_hooks(event_name, name, payload, name, tool_input)

    async def on_event(self, event: Any, _context: Any = None) -> Any:
        if read_field(event, "type") != "agent_end":
            return
        messages = read_field(event, "messages", [])
        if isinstance(messages, Sequence):
            for message in reversed(messages):
                if str(read_field(message, "role", "")) != "assistant":
                    continue
                if str(read_field(message, "stopReason", "")) in {"error", "aborted"}:
                    self.stop_hook_active = False
                    return None
                break
        last_assistant_message = None
        if isinstance(messages, Sequence):
            for message in reversed(messages):
                if str(read_field(message, "role", "")) != "assistant":
                    continue
                content = read_field(message, "content", [])
                if isinstance(content, str):
                    last_assistant_message = content.strip() or None
                elif isinstance(content, Sequence):
                    text = "\n".join(
                        str(read_field(block, "text", ""))
                        for block in content
                        if read_field(block, "type") == "text" and read_field(block, "text")
                    ).strip()
                    last_assistant_message = text or None
                break
        payload = self._payload(
            "SubagentStop",
            stop_hook_active=self.stop_hook_active,
            agent_transcript_path=os.environ.get("MISAKA_SUBAGENT_TRANSCRIPT") or "",
            last_assistant_message=last_assistant_message,
        )
        subject = str(getattr(self.context, "role", ""))
        results = await self._execute_hooks("SubagentStop", subject, payload)
        for result in results:
            if result["decision"] in {"deny", "ask"} or not result["allowed"]:
                self.stop_hook_active = True
                return {
                    "block": True,
                    "reason": result["reason"] or "SubagentStop hook blocked",
                }
        self.stop_hook_active = False
        return None


def register(harness: Any, context: Any) -> AgentPolicy | None:
    """Attach policy only when this process represents a configured agent."""

    if not (
        getattr(context, "tool_rule_layers", ())
        or getattr(context, "permission_mode", None)
        or getattr(context, "agent_hooks", None)
    ):
        return None
    policy = AgentPolicy(context)
    policy.harness = harness
    harness.on("before_agent_start", policy.before_agent)
    harness.on("tool_call", policy.before_tool)
    harness.on("tool_result", policy.after_tool)
    harness.on("agent_end", policy.on_event)
    return policy


__all__ = [
    "AgentPolicy",
    "classify_permission",
    "normalize_rule",
    "register",
    "request_permission",
    "rule_matches",
    "set_async_hook_broker",
    "set_permission_broker",
    "set_permission_classifier",
    "split_rule",
]

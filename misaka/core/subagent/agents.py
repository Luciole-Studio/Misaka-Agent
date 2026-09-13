"""Claude Code-style sub-agent definitions.

Definitions are Markdown files with YAML frontmatter.  Built-ins ship with the
package (``misaka/core/subagent/agents/``); users and projects may
override them by ``name`` from ``~/.misaka/agent/agents`` and ``.misaka/agents``
respectively.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

from misaka.core.prompt_templates import _ECMASCRIPT_WHITESPACE
from misaka.core.subagent._frontmatter import split_frontmatter

COLORS = ("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan")
_JS_SPACE = "".join(_ECMASCRIPT_WHITESPACE)

ROOT = str(Path(__file__).resolve().parent / "agents")

_GENERAL_NAMES = frozenset({"general", "general-purpose"})
_PERMISSION_MODES = frozenset(
    {"acceptEdits", "bypassPermissions", "default", "dontAsk", "plan", "auto"}
)
_MEMORY_SCOPES = frozenset({"user", "project", "local"})
_ISOLATION_MODES = frozenset({"worktree"})


@dataclass(slots=True)
class AgentDefinition:
    """Normalized agent definition (Claude Code's frontmatter vocabulary is mapped in ``_ALIASES``)."""

    name: str
    description: str
    prompt: str
    source: str = "built-in"
    path: str | None = None
    base_dir: str | None = None
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    model: str = "inherit"
    effort: str | int | None = None
    permission_mode: str | None = None
    max_turns: int | None = None
    background: bool = False
    isolation: str | None = None
    skills: list[str] = field(default_factory=list)
    initial_prompt: str | None = None
    memory: str | None = None
    required_mcp_servers: list[str] = field(default_factory=list)
    mcp_servers: list[Any] = field(default_factory=list)
    hooks: dict[str, Any] | None = None
    color: str | None = None
    critical_system_reminder: str | None = None
    omit_context_files: bool = False

    # Frontmatter key -> field name, for the keys `_agent_value` is actually asked for.
    # It is not a general vocabulary table: `parse` reads the frontmatter directly and
    # only in camelCase (`permissionMode`, `maxTurns`, `initialPrompt`,
    # `requiredMcpServers`, `mcpServers`), and `name`/`description` have no accepted
    # alias at all -- a file written with `agentType:`/`whenToUse:` is skipped. Eleven
    # further entries once sat here for spellings no caller ever looks up; they were
    # removed rather than left implying a tolerance the parser does not have.
    _ALIASES: ClassVar[dict[str, str]] = {
        "disallowed": "disallowed_tools",
        "disallowedTools": "disallowed_tools",
    }


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str] | None:
    return split_frontmatter(raw)


def _split_specs(value: Any) -> list[str]:
    """CCB parseToolListString/parseToolListFromCLI, including duplicate rules."""
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        current = ""
        in_parens = False
        for char in item:
            if char == "(":
                in_parens = True
                current += char
            elif char == ")":
                in_parens = False
                current += char
            elif char == "," and not in_parens:
                if current.strip(_JS_SPACE):
                    result.append(current.strip(_JS_SPACE))
                current = ""
            elif char == " " and not in_parens:
                if current.strip(_JS_SPACE):
                    result.append(current.strip(_JS_SPACE))
                    current = ""
            else:
                current += char
        if current.strip(_JS_SPACE):
            result.append(current.strip(_JS_SPACE))
    return ["*"] if "*" in result else result


def _string_list(value: Any) -> list[str]:
    """Parse names/patterns without splitting spaces inside YAML list items."""
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, Sequence):
        values = value
    else:
        return []
    return list(
        dict.fromkeys(item.strip() for item in values if isinstance(item, str) and item.strip())
    )


def _mcp_servers(value: Any) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, (str, dict))]


def _tools(value: Any) -> list[str] | None:
    specs = _split_specs(value)
    return None if any(spec.lower() in {"*", "inherit"} for spec in specs) else specs


def _string(value: Any) -> str | None:
    return value.strip(_JS_SPACE) if isinstance(value, str) and value.strip(_JS_SPACE) else None


def _choice(value: Any, choices: Sequence[str] | frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in choices else None


def _effort(value: Any) -> str | int | None:
    from misaka.core.subagent.model import parse_effort

    return parse_effort(value)


def _positive_int(value: Any) -> int | None:
    from misaka.core.subagent.model import _as_js_number, parse_decimal_prefix

    # CCB parsePositiveIntFromFrontmatter: numeric fractions are invalid;
    # unlike effort, only non-number inputs go through String/parseInt.
    parsed = _as_js_number(value) if type(value) in (int, float) else parse_decimal_prefix(value)
    return int(parsed) if parsed is not None and math.isfinite(parsed) and parsed.is_integer() and parsed > 0 else None


def _boolean(value: Any) -> bool:
    return value is True or value == "true"


def _source_name(source: str) -> str:
    return {
        "builtin": "built-in",
        "built-in": "built-in",
        "user": "userSettings",
        "userSettings": "userSettings",
        "project": "projectSettings",
        "projectSettings": "projectSettings",
    }.get(source, source)


def parse(path: str | os.PathLike[str], *, source: str = "built-in", base_dir: str | None = None, diagnostics: list[dict[str, str]] | None = None, plugin: Mapping[str, Any] | None = None) -> AgentDefinition | None:
    """Parse one agent file; non-agent or malformed Markdown is skipped."""
    file_path = Path(path)
    try:
        with file_path.open(encoding="utf-8", newline="") as stream:
            raw = stream.read()
        split = _split_frontmatter(raw)
    except (OSError, UnicodeError) as error:
        _diagnostic(diagnostics, str(file_path), str(error))
        return None
    if split is None:
        if source != "plugin":
            return None
        # Plugin Markdown may omit frontmatter; file stem is its default name.
        split = {}, raw.strip(_JS_SPACE)
    meta, prompt = split
    if source == "plugin":
        try:
            return _from_plugin_metadata(meta, prompt, file_path, base_dir, plugin, diagnostics)
        except (OSError, ValueError, TypeError) as error:
            _diagnostic(diagnostics, str(file_path), str(error))
            return None

    if meta.get("name") and not (isinstance(meta.get("description"), str) and meta["description"]):
        _diagnostic(diagnostics, str(file_path), 'Missing required "description" field in frontmatter')
    for key, valid in (
        ("permissionMode", _choice(meta.get("permissionMode"), _PERMISSION_MODES)),
        ("maxTurns", _positive_int(meta.get("maxTurns"))),
        ("effort", _effort(meta.get("effort"))),
        ("memory", _choice(meta.get("memory"), _MEMORY_SCOPES)),
        ("isolation", _choice(meta.get("isolation"), _ISOLATION_MODES)),
        ("color", _choice(meta.get("color"), COLORS)),
    ):
        if key in meta and valid is None:
            _diagnostic(diagnostics, str(file_path), f"Invalid {key}: {meta[key]!r}; field ignored")
    if "background" in meta and not (type(meta["background"]) is bool or isinstance(meta["background"], str) and meta["background"] in ("true", "false")):
        _diagnostic(diagnostics, str(file_path), "Invalid background; expected true or false")
    if "hooks" in meta:
        try:
            from misaka.core.subagent.hooks import validate_hooks

            if not isinstance(meta["hooks"], dict):
                raise TypeError("hooks must be an object")
            meta["hooks"] = validate_hooks(meta["hooks"])
        except (ValueError, TypeError) as error:
            _diagnostic(diagnostics, str(file_path), str(error))
            meta.pop("hooks")
    if isinstance(meta.get("mcpServers"), list):
        servers = []
        for item in meta["mcpServers"]:
            try:
                servers.append(_validate_mcp_spec(item))
            except ValueError as error:
                _diagnostic(diagnostics, str(file_path), str(error))
        meta["mcpServers"] = servers
    return _from_metadata(meta, prompt, file_path=file_path, source=source, base_dir=base_dir)


def _plugin_string(value: Any, seen: set[int] | None = None) -> str:
    """String(value) for persisted plugin JSON/YAML values, not arbitrary JS objects."""
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if type(value) in (int, float):
        from misaka.core.subagent.model import _as_js_number

        number = _as_js_number(value)
        if math.isnan(number):
            return "NaN"
        if math.isinf(number):
            return "Infinity" if number > 0 else "-Infinity"
        if number == 0:
            return "0"
        sign = "-" if number < 0 else ""
        raw = repr(abs(number))
        if "e" not in raw:
            return sign + raw.removesuffix(".0")
        mantissa, exponent = raw.split("e")
        power = int(exponent)
        if 1e-6 <= abs(number) < 1e21:
            integer, _, fraction = mantissa.partition(".")
            digits = integer + fraction
            point = len(integer) + power
            if point <= 0:
                raw = "0." + "0" * -point + digits
            elif point >= len(digits):
                raw = digits + "0" * (point - len(digits))
            else:
                raw = digits[:point] + "." + digits[point:]
        else:
            raw = mantissa.removesuffix(".0") + "e" + ("+" if power >= 0 else "-") + str(abs(power))
        return sign + raw
    if isinstance(value, list):
        seen = set() if seen is None else seen
        if id(value) in seen:
            return ""
        seen.add(id(value))
        try:
            return ",".join("" if item is None else _plugin_string(item, seen) for item in value)
        finally:
            seen.remove(id(value))
    if isinstance(value, Mapping) and "toString" in value:
        # JSON cannot contain a callable own toString; JS String then throws.
        raise ValueError("Plugin value cannot be converted to a primitive string")
    return "[object Object]"


def _from_plugin_metadata(meta, prompt, file_path, base_dir, plugin, diagnostics):
    """CCB loadPluginAgents.loadAgentFromFile; not ordinary Markdown metadata."""
    directory = Path(base_dir or file_path.parent)
    plugin_root = Path(plugin["root"]) if plugin else directory.parent
    plugin_name = str(plugin.get("name") or plugin_root.name) if plugin else plugin_root.name
    raw_name = meta.get("name")
    missing_name = raw_name is None or raw_name is False or raw_name == "" or (
        type(raw_name) in (int, float) and (raw_name == 0 or isinstance(raw_name, float) and math.isnan(raw_name)))
    base_name = file_path.stem if missing_name else _plugin_string(raw_name)
    name = ":".join((plugin_name, *file_path.parent.relative_to(directory).parts, base_name))

    def description(value):
        if isinstance(value, str):
            return _string(value)
        return _plugin_string(value) if type(value) in (bool, int, float) else None

    when_to_use = description(meta.get("description")) or description(meta.get("when-to-use")) or f"Agent from {plugin_name} plugin"
    for key in ("permissionMode", "hooks", "mcpServers", "requiredMcpServers", "initialPrompt"):
        if key in meta:
            _diagnostic(diagnostics, str(file_path), f"Plugin agent {key} ignored; use a user/project agent definition")

    def path_string(value):
        text = str(value)
        return text.replace("\\", "/") if sys.platform == "win32" else text

    prompt = prompt.replace("${CLAUDE_PLUGIN_ROOT}", path_string(plugin_root)).replace("${MISAKA_PLUGIN_ROOT}", path_string(plugin_root))
    if plugin:
        if plugin.get("data_dir"):
            def data_path(_match):
                directory = Path(plugin["data_dir"])
                if plugin.get("data_home"):
                    # Native local extensions have no marketplace identity.
                    # The catalog supplies a role/root-scoped path, never a
                    # manifest-controlled path. Like source getPluginDataDir,
                    # mkdir happens only when DATA actually occurs in prose.
                    home = Path(plugin["data_home"])
                    current = home
                    for part in directory.relative_to(home).parts:
                        current /= part
                        if current.is_symlink():
                            raise ValueError("Plugin data directories must not be symlinks")
                        current.mkdir(parents=True, exist_ok=True, mode=0o700)
                return path_string(directory)

            prompt = re.sub(r"\$\{(?:CLAUDE|MISAKA)_PLUGIN_DATA\}", data_path, prompt)
        schema = plugin.get("userConfig")
        # Source only reads/substitutes options when the manifest declares a
        # schema. Empty objects are truthy in JS and still enable substitution.
        if isinstance(schema, Mapping):
            options = plugin.get("options") or {}

            def substitute(match):
                key = match[1]
                declaration = schema.get(key)
                if isinstance(declaration, Mapping) and declaration.get("sensitive") is True:
                    return f"[sensitive option '{key}' not available in skill content]"
                return _plugin_string(options[key]) if isinstance(options, Mapping) and key in options else match[0]

            prompt = re.sub(r"\$\{user_config\.([^}]+)\}", substitute, prompt)
    for key, value in (
        ("memory", _choice(meta.get("memory"), _MEMORY_SCOPES)),
        ("effort", _effort(meta.get("effort"))),
        ("maxTurns", _positive_int(meta.get("maxTurns"))),
    ):
        if key in meta and value is None:
            _diagnostic(diagnostics, str(file_path), f"Invalid {key}: {meta[key]!r}; field ignored")
    color = _choice(meta.get("color"), COLORS)
    if "color" in meta and color is None:
        _diagnostic(diagnostics, str(file_path), f"Invalid color: {meta['color']!r}; field ignored")
    model = _string(meta.get("model")) or "inherit"
    return AgentDefinition(
        name=name, description=when_to_use, prompt=prompt, source="plugin",
        path=str(file_path), base_dir=base_dir or str(file_path.parent),
        tools=_tools(meta.get("tools")) if "tools" in meta else None,
        disallowed_tools=_split_specs(meta.get("disallowedTools")) if "disallowedTools" in meta else None,
        model="inherit" if model.lower() == "inherit" else model,
        effort=_effort(meta.get("effort")), max_turns=_positive_int(meta.get("maxTurns")),
        background=_boolean(meta.get("background")),
        memory=_choice(meta.get("memory"), _MEMORY_SCOPES),
        isolation=_choice(meta.get("isolation"), _ISOLATION_MODES),
        skills=_split_specs(meta.get("skills")), color=color,
    )


def _from_metadata(meta: Mapping[str, Any], prompt: str, *, file_path: Path | None = None, source: str = "built-in", base_dir: str | None = None) -> AgentDefinition | None:
    name, description = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or not name or not isinstance(description, str) or not description:
        return None

    tools = None if "tools" not in meta else _tools(meta.get("tools"))
    denied = None if "disallowedTools" not in meta else _split_specs(meta.get("disallowedTools"))
    model = _string(meta.get("model")) or "inherit"
    if source == "built-in" and name == "Explore" and os.environ.get("USER_TYPE") == "ant":
        model = "inherit"
    if model.lower() == "inherit":
        model = "inherit"
    initial_prompt = meta.get("initialPrompt")
    if not isinstance(initial_prompt, str) or not initial_prompt.strip(_JS_SPACE):
        initial_prompt = None

    return AgentDefinition(
        name=name,
        description=description.replace("\\n", "\n"),
        prompt=prompt,
        source=_source_name(source),
        path=str(file_path) if file_path else None,
        base_dir=base_dir or (str(file_path.parent) if file_path else None),
        tools=tools,
        disallowed_tools=denied,
        model=model,
        effort=_effort(meta.get("effort")),
        permission_mode=_choice(meta.get("permissionMode"), _PERMISSION_MODES),
        max_turns=_positive_int(meta.get("maxTurns")),
        background=_boolean(meta.get("background")),
        isolation=_choice(meta.get("isolation"), _ISOLATION_MODES),
        skills=_split_specs(meta.get("skills")),
        initial_prompt=initial_prompt,
        memory=_choice(meta.get("memory"), _MEMORY_SCOPES),
        required_mcp_servers=_string_list(meta.get("requiredMcpServers")),
        mcp_servers=_mcp_servers(meta.get("mcpServers")),
        hooks=meta.get("hooks") if isinstance(meta.get("hooks"), dict) else None,
        color=_choice(meta.get("color"), COLORS),
        # Builtin metadata transports the source programmatic reminder field;
        # user/project/plugin Markdown does not gain this private declaration.
        critical_system_reminder=_string(meta.get("criticalSystemReminder")) if source == "built-in" else None,
        omit_context_files=meta.get("omitContextFiles") is True,
    )


def _definitions(root: str | os.PathLike[str] | None, source: str, diagnostics: list[dict[str, str]] | None = None) -> list[AgentDefinition]:
    if root is None:
        return []
    directory = Path(root).expanduser()
    if not directory.is_dir():
        return []
    return [
        agent
        for path in sorted(directory.rglob("*.md"))
        if (agent := parse(path, source=source, base_dir=str(directory), diagnostics=diagnostics)) is not None
    ]



def _plugin_definitions(plugin: Mapping[str, Any], diagnostics: list[dict[str, str]]) -> list[AgentDefinition]:
    """Pinned loadPluginAgents: default dir, then manifest dirs/files, realpath dedup."""
    root = Path(plugin["root"]).expanduser().resolve()
    paths = [root / "agents", *(Path(path) for path in plugin.get("paths", []))]
    loaded: set[Path] = set()
    result = []
    for path in paths:
        path = path if path.is_absolute() else root / path
        directory = path if path.is_dir() else path.parent
        for candidate in sorted(path.rglob("*.md")) if path.is_dir() else [path]:
            try:
                resolved = candidate.resolve()
                # Package agent discovery inherits the enabled extension's trust;
                # a manifest is not a grant to read arbitrary files outside it.
                resolved.relative_to(root)
                if resolved in loaded or not candidate.is_file() or candidate.suffix != ".md":
                    continue
                loaded.add(resolved)
                agent = parse(candidate, source="plugin", base_dir=str(directory),
                              diagnostics=diagnostics, plugin=plugin)
                if agent is not None:
                    result.append(agent)
            except (OSError, ValueError) as error:
                _diagnostic(diagnostics, str(candidate), str(error))
    return result


def _package_agent_source(base: str, settings: Mapping[str, Any], diagnostics: list[dict[str, str]], context: Any = None) -> dict[str, Any]:
    """Adapt an already-enabled package, never discover/enable plugins here."""
    root = Path(base).expanduser().resolve()
    manifest: Mapping[str, Any] = {}
    for path in (root / ".misaka-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json", root / "package.json"):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, Mapping):
                raise TypeError("Plugin manifest must be an object")
            manifest = data.get("misaka", {}) if path.name == "package.json" else data
            if not isinstance(manifest, Mapping):
                raise TypeError("Package misaka manifest must be an object")
            break
        except (OSError, ValueError, TypeError) as error:
            _diagnostic(diagnostics, str(path), str(error))
    name = str(manifest.get("name") or root.name)
    paths = manifest.get("agents", [])
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        _diagnostic(diagnostics, str(root), "Plugin agents must be a path or list of paths")
        paths = []
    configured = settings.get("pluginOptions") or {}
    options = configured.get(name, {}) if isinstance(configured, Mapping) else {}
    from misaka.config import get_agent_dir

    home = Path(getattr(context, "profile_dir", None) or get_agent_dir()).expanduser().resolve()
    role = hashlib.sha256(str(getattr(context, "role", "") or "").encode()).hexdigest()[:16]
    # Source storage identity is name@marketplace; the native enabled local
    # extension identity is its canonical root. Hash it (not the display name)
    # to avoid cross-project collisions, traversal and filesystem name limits.
    identity = hashlib.sha256(str(root).encode()).hexdigest()
    return {
        "root": str(root), "name": name, "paths": paths,
        "userConfig": manifest.get("userConfig"), "options": options,
        "data_home": str(home), "data_dir": str(home / "plugins" / "data" / role / identity),
    }


def _user_agents_dir() -> Path:
    agent_home = os.environ.get("MISAKA_CODING_AGENT_DIR")
    return Path(agent_home).expanduser() / "agents" if agent_home else Path.home() / ".misaka" / "agent" / "agents"


def _project_agent_dirs(cwd: str | os.PathLike[str] | None) -> list[Path]:
    """Return project directories from least to most specific (nearest wins)."""
    current = Path(cwd or os.getcwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent
    found: list[Path] = []
    for directory in (current, *current.parents):
        candidate = directory / ".misaka" / "agents"
        if candidate.is_dir():
            found.append(candidate)
        if (directory / ".git").exists() or directory == Path.home():
            break
    return list(reversed(found))


def _canonical_name(name: str) -> str:
    return "general-purpose" if name in _GENERAL_NAMES else name


def discover_result(
    root: str | os.PathLike[str] | None = None,
    *,
    user_root: str | os.PathLike[str] | None = None,
    project_root: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    include_project: bool = True,
    plugin_roots: Sequence[str | os.PathLike[str] | Mapping[str, Any]] = (),
    role_root: str | os.PathLike[str] | None = None,
    policy_root: str | os.PathLike[str] | None = None,
    providers: Sequence[AgentDefinition] = (),
    include_builtin: bool = True,
) -> AgentDefinitionsResult:
    """CCB getAgentDefinitionsWithOverrides / getActiveAgentsFromList.

    Passing ``root`` makes discovery isolated unless a user/project root is also
    supplied, which keeps tests and embedders independent of local config.
    """
    builtin_root = root or ROOT
    use_defaults = root is None
    user_dir = user_root if user_root is not None else (_user_agents_dir() if use_defaults else None)
    if not include_project:
        project_dirs = []
    elif project_root is not None:
        project_dirs = [Path(project_root)]
    elif use_defaults:
        project_dirs = _project_agent_dirs(cwd)
    else:
        project_dirs = []

    failed: list[dict[str, str]] = []
    all_agents = _definitions(builtin_root, "built-in", failed) if include_builtin else []
    from misaka.core.subagent.background import _truthy

    # Source VERIFICATION_AGENT + tengu_hive_evidence defaults off. The native
    # opt-in replaces that product's compile/GrowthBook gate, not its algorithm.
    if not _truthy(os.environ.get("MISAKA_VERIFICATION_AGENT")):
        all_agents = [agent for agent in all_agents if agent.name != "verification"]
    for directory in plugin_roots:
        all_agents.extend(_plugin_definitions(directory, failed) if isinstance(directory, Mapping)
                          else _definitions(directory, "plugin", failed))
    for directory in (user_dir, role_root):
        all_agents.extend(_definitions(directory, "userSettings", failed))
    for directory in project_dirs:
        all_agents.extend(_definitions(directory, "projectSettings", failed))
    all_agents.extend(providers)
    all_agents.extend(_definitions(policy_root, "policySettings", failed))
    order = {name: index for index, name in enumerate(("built-in", "plugin", "userSettings", "projectSettings", "flagSettings", "policySettings"))}
    merged: dict[str, AgentDefinition] = {}
    for agent in sorted(all_agents, key=lambda item: order.get(item.source, 0)):
        if include_project or agent.source != "projectSettings":
            merged[_canonical_name(agent.name)] = agent

    result = dict(merged)
    general = result.get("general-purpose")
    if general is not None:
        result["general"] = general
    loaded_paths = {agent.path for agent in all_agents}
    return AgentDefinitionsResult(result, all_agents, [item for item in failed if item["path"] not in loaded_paths], failed)


def discover(root=None, **kwargs) -> dict[str, AgentDefinition]:
    """Compatibility view; the catalog retains shadowed definitions and diagnostics."""
    return discover_result(root, **kwargs).active_agents


def _agent_value(agent: AgentDefinition | Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """Read one field by any of its spellings: the frontmatter key or the field name.

    A parsed definition is a slotted dataclass, so subscripting it raises TypeError --
    which is how the tool allowlist silently stopped reaching the child (audit 2026-08-27).
    Attribute access through ``_ALIASES`` is the one way that works for both shapes.
    """
    for key in keys:
        if isinstance(agent, Mapping):
            value = agent.get(key)
        else:
            value = getattr(agent, AgentDefinition._ALIASES.get(key, key), None)
        if value is not None:
            return value
    return default


def _tool_name(spec: str) -> str:
    return spec.split("(", 1)[0].strip()


def resolve_tools(agent: AgentDefinition | Mapping[str, Any]) -> list[str] | None:
    """The child's ``-t`` allowlist, or ``None`` to inherit the parent's dynamic pool.

    An absent ``tools`` and ``tools: inherit`` both mean None (the runtime keeps the
    pool open so late MCP tools stay reachable); ``disallowedTools: "*"`` means no tools.
    """
    raw_allow = _agent_value(agent, "tools")
    allow = _tools(raw_allow) if raw_allow is not None else None
    denied = _split_specs(_agent_value(agent, "disallowedTools", "disallowed", default=[]))
    if not denied:
        return allow
    if any(spec == "*" for spec in denied):
        return []
    if allow is None:
        return None
    denied_names = {_tool_name(spec).casefold() for spec in denied}
    return [spec for spec in allow if _tool_name(spec).casefold() not in denied_names]


def roster_text(agents: Mapping[str, AgentDefinition]) -> str:
    """Human/model-facing roster, with general aliases shown only once."""
    if not agents:
        return "(no agent types available)"
    seen: set[int] = set()
    lines: list[str] = []
    for agent in agents.values():
        if id(agent) in seen:
            continue
        seen.add(id(agent))
        lines.append(f"- {agent.name}: {agent.description} (Tools: {tools_description(agent)})")
    return "\n".join(lines)


def tools_description(agent: AgentDefinition) -> str:
    """CCB prompt.ts getToolsDescription; explicit [] correctly advertises None."""
    if agent.tools is not None:
        denied = set(agent.disallowed_tools or ())
        effective = [tool for tool in agent.tools if tool not in denied]
        return ", ".join(effective) if effective else "None"
    if agent.disallowed_tools:
        return "All tools except " + ", ".join(agent.disallowed_tools)
    return "All tools"


@dataclass(slots=True)
class AgentDefinitionsResult:
    active_agents: dict[str, AgentDefinition]
    all_agents: list[AgentDefinition]
    failed_files: list[dict[str, str]]
    diagnostics: list[dict[str, str]] = field(default_factory=list)


def _diagnostic(items: list[dict[str, str]] | None, path: str, error: str) -> None:
    if items is not None:
        items.append({"path": path, "error": error})
    else:
        logging.getLogger(__name__).warning("Agent definition %s: %s", path, error)


def _validate_mcp_spec(value: Any) -> str | dict[str, Any]:
    """Structural port of CCB AgentMcpServerSpecSchema/McpServerConfigSchema."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        raise ValueError("mcpServers items must be a name or server mapping")  # noqa: TRY004 - Pydantic validators require ValueError for schema diagnostics
    normalized = {}
    for name, config in value.items():
        if not isinstance(name, str) or not isinstance(config, dict):
            raise ValueError("MCP server configurations must be named objects")  # noqa: TRY004 - Pydantic validators require ValueError for schema diagnostics
        kind = config.get("type", "stdio")
        required = {
            "stdio": ("command",), "sse": ("url",), "http": ("url",), "ws": ("url",),
            "sse-ide": ("url", "ideName"), "ws-ide": ("url", "ideName"),
            "sdk": ("name",), "claudeai-proxy": ("url", "id"),
        }
        if not isinstance(kind, str) or kind not in required:
            raise ValueError(f"Unsupported MCP transport {kind!r}")
        # Source z.object selects fields per union member and strips unknowns.
        optional = {
            "stdio": ("args", "env"), "sse": ("headers", "headersHelper", "oauth"),
            "http": ("headers", "headersHelper", "oauth"), "ws": ("headers", "headersHelper"),
            "sse-ide": ("ideRunningInWindows",), "ws-ide": ("authToken", "ideRunningInWindows"),
            "sdk": (), "claudeai-proxy": (),
        }
        config = {key: item for key, item in config.items() if key in ("type", *required[kind], *optional[kind])}
        if kind == "stdio":
            config.setdefault("args", [])
        if any(not isinstance(config.get(key), str) for key in required[kind]):
            raise ValueError(f"MCP {name}: {kind} needs string fields {required[kind]}")
        if kind == "stdio" and not config["command"]:
            raise ValueError(f"MCP {name}: command must not be empty")
        if "args" in config and (not isinstance(config["args"], list) or any(not isinstance(v, str) for v in config["args"])):
            raise ValueError(f"MCP {name}: args must be a string list")
        for key in ("env", "headers"):
            if key in config and (not isinstance(config[key], dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in config[key].items())):
                raise ValueError(f"MCP {name}: {key} must map strings to strings")
        for key in ("headersHelper", "authToken"):
            if key in config and not isinstance(config[key], str):
                raise ValueError(f"MCP {name}: {key} must be a string")
        if "ideRunningInWindows" in config and not isinstance(config["ideRunningInWindows"], bool):
            raise ValueError(f"MCP {name}: ideRunningInWindows must be a boolean")
        if "oauth" in config:
            oauth = config["oauth"]
            if not isinstance(oauth, dict):
                raise ValueError(f"MCP {name}: oauth must be an object")
            oauth = {key: item for key, item in oauth.items() if key in ("clientId", "callbackPort", "authServerMetadataUrl", "xaa")}
            config["oauth"] = oauth
            if "clientId" in oauth and not isinstance(oauth["clientId"], str):
                raise ValueError(f"MCP {name}: oauth.clientId must be a string")
            if "callbackPort" in oauth:
                port = oauth["callbackPort"]
                if type(port) not in (int, float) or port <= 0 or port > 9_007_199_254_740_991 or (isinstance(port, float) and not port.is_integer()):
                    raise ValueError(f"MCP {name}: oauth.callbackPort must be a positive integer")
            if "xaa" in oauth and not isinstance(oauth["xaa"], bool):
                raise ValueError(f"MCP {name}: oauth.xaa must be a boolean")
            if "authServerMetadataUrl" in oauth:
                url = oauth["authServerMetadataUrl"]
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise ValueError(f"MCP {name}: oauth.authServerMetadataUrl must be an HTTPS URL")
                AnyUrl(url)  # Validate without replacing the source string.
        normalized[name] = config
    return normalized


def inline_mcp_entries(specs, *, on_invalid=None):
    """CCB setupAgentMcpServers: an inline definition has exactly one key.

    Schema records can contain multiple keys, but runtime setup skips those
    records. Roster, required-server checks and child setup share this rule.
    """
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        if len(spec) != 1:
            if on_invalid is not None:
                on_invalid("Invalid MCP server spec: expected exactly one key")
            continue
        name, config = next(iter(spec.items()))
        if isinstance(name, str) and isinstance(config, dict):
            yield name, config


class _JsonAgentDefinition(BaseModel):
    """CCB loadAgentsDir.ts AgentJsonSchema: JSON is strict, Markdown tolerant."""
    model_config = ConfigDict(extra="ignore")
    description: StrictStr = Field(min_length=1)
    prompt: StrictStr = Field(min_length=1)
    tools: list[StrictStr] | None = None
    disallowedTools: list[StrictStr] | None = None
    model: StrictStr | None = Field(default=None, min_length=1)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | StrictInt | None = None
    permissionMode: Literal["acceptEdits", "bypassPermissions", "default", "dontAsk", "plan", "auto"] | None = None
    mcpServers: list[Any] | None = None
    hooks: dict[str, Any] | None = None
    maxTurns: StrictInt | None = Field(default=None, gt=0)
    skills: list[StrictStr] | None = None
    initialPrompt: StrictStr | None = None
    memory: Literal["user", "project", "local"] | None = None
    background: StrictBool | None = None
    isolation: Literal["worktree"] | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if isinstance(value, dict) and any(value[key] is None for key in value.keys() & cls.model_fields.keys()):
            raise ValueError("Agent fields may be omitted, not null")
        return value

    @field_validator("effort", "maxTurns", mode="before")
    @classmethod
    def normalize_json_integer(cls, value):
        # JavaScript has one number type: JSON 2.0 passes z.number().int().
        if type(value) in (int, float) and abs(value) > 9_007_199_254_740_991:
            raise ValueError("JSON integer exceeds the source safe-integer range")
        return int(value) if isinstance(value, float) and value.is_integer() else value

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value):
        value = value.strip(''.join(_ECMASCRIPT_WHITESPACE))
        if not value:
            raise ValueError("Model must not be empty")
        return "inherit" if value.lower() == "inherit" else value

    @field_validator("mcpServers")
    @classmethod
    def validate_mcp(cls, value):
        return [_validate_mcp_spec(item) for item in value]

    @field_validator("hooks")
    @classmethod
    def validate_agent_hooks(cls, value):
        from misaka.core.subagent.hooks import validate_hooks

        return validate_hooks(value)


def parse_agents_json(value: Any, *, source: str = "flagSettings", diagnostics: list[dict[str, str]] | None = None) -> list[AgentDefinition]:
    """CCB parseAgentsFromJson: one malformed member invalidates the JSON batch."""
    try:
        if isinstance(value, str):
            value = json.loads(value)
        validated = TypeAdapter(dict[str, _JsonAgentDefinition]).validate_python(value)
    except (ValueError, TypeError) as error:
        _diagnostic(diagnostics, source, str(error))
        return []
    # CCB parseAgentFromJson is NOT the tolerant Markdown parser. Preserve
    # accepted JSON descriptions, names, skill names and initialPrompt verbatim.
    # Memory tool/prompt injection stays at the native role-aware launch boundary.
    return [AgentDefinition(
        name=name, description=item.description, prompt=item.prompt, source=_source_name(source),
        tools=_tools(item.tools) if item.tools is not None else None,
        # Keep MISAKA's explicit deny-* fence and inherit tool alias.
        disallowed_tools=_split_specs(item.disallowedTools) if item.disallowedTools is not None else None,
        model=item.model if item.model is not None else "inherit", effort=item.effort,
        permission_mode=item.permissionMode, max_turns=item.maxTurns,
        background=item.background is True, isolation=item.isolation,
        skills=item.skills if item.skills is not None else [],
        initial_prompt=item.initialPrompt or None, memory=item.memory,
        mcp_servers=item.mcpServers if item.mcpServers is not None else [], hooks=item.hooks,
    ) for name, item in validated.items()]


def session_catalog(session: Any, context: Any, *, cwd: str, include_project: bool, flag_agents: Any = None) -> AgentDefinitionsResult:
    """MISAKA host adapter: enabled extension roots and role/project settings.

    No extension is imported here and an untrusted project is never scanned.
    Managed agents are an operator-selected directory, not project-controlled.
    """
    from misaka.utils.values import read_field

    providers: list[AgentDefinition] = []
    diagnostics: list[dict[str, str]] = []
    plugin_roots: list[Mapping[str, Any]] = []
    global_settings: Mapping[str, Any] = {}
    settings = getattr(session, "settingsManager", None)
    same_project = Path(cwd).resolve() == Path(getattr(session, "cwd", None) or context.workspace).resolve()
    if settings is not None:
        for source, getter in (("userSettings", "getGlobalSettings"), ("projectSettings", "getProjectSettings"), ("policySettings", "getManagedSettings")):
            if source == "projectSettings" and (not include_project or not same_project):
                continue
            read_settings = getattr(settings, getter, None)
            values = read_settings() if callable(read_settings) else {}
            if not isinstance(values, Mapping):
                _diagnostic(diagnostics, source, "Settings must be an object")
                continue
            if source == "userSettings":
                global_settings = values
            if values.get("agents") is not None:
                providers.extend(parse_agents_json(values["agents"], source=source, diagnostics=diagnostics))
    if include_project and not same_project:
        from misaka.core.subagent.configuration import project_settings
        values = project_settings(cwd)
        if values.get("agents") is not None:
            providers.extend(parse_agents_json(values["agents"], source="projectSettings", diagnostics=diagnostics))
    loader = getattr(session, "resourceLoader", None)
    if loader is not None:
        for extension in read_field(loader.getExtensions(), "extensions", []):
            info = read_field(extension, "sourceInfo")
            base = read_field(info, "baseDir")
            if base and read_field(info, "origin") == "package" and ((include_project and same_project) or read_field(info, "scope") != "project"):
                plugin = _package_agent_source(str(base), global_settings, diagnostics, context)
                if all(previous["root"] != plugin["root"] for previous in plugin_roots):
                    plugin_roots.append(plugin)
    if flag_agents is not None:
        providers.extend(parse_agents_json(flag_agents, diagnostics=diagnostics))
    profile = getattr(context, "profile_dir", "")
    from misaka.core.subagent.background import _truthy

    # CCB getBuiltInAgents: SDK blank slate does not disable custom agents,
    # and has no effect on interactive sessions. Use native run mode, not
    # hasUI (a JSON/RPC client can supply an interactive prompt callback).
    mode = read_field(getattr(session, "extensionRunner", None), "mode", "print")
    include_builtin = not (_truthy(os.environ.get("MISAKA_AGENT_SDK_DISABLE_BUILTIN_AGENTS")) and mode != "tui")
    result = discover_result(cwd=cwd, include_project=include_project, include_builtin=include_builtin,
                             plugin_roots=plugin_roots, providers=providers,
                             role_root=Path(profile) / "agents" if profile else None,
                             policy_root=os.environ.get("MISAKA_MANAGED_AGENTS_DIR"))
    result.failed_files.extend(diagnostics)
    result.diagnostics.extend(diagnostics)
    return result


def has_required_mcp_servers(agent: AgentDefinition, available: Sequence[str]) -> bool:
    """CCB hasRequiredMcpServers: every pattern matches at least one server."""
    return all(any(pattern.lower() in name.lower() for name in available)
               for pattern in agent.required_mcp_servers)

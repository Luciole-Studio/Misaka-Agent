"""Claude Code-style sub-agent definitions.

Definitions are Markdown files with YAML frontmatter.  Built-ins ship with the
package (``misaka/extensions/subagent/agents/``); users and projects may
override them by ``name`` from ``~/.misaka/agent/agents`` and ``.misaka/agents``
respectively.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

ROOT = str(Path(__file__).resolve().parent / "agents")

_GENERAL_NAMES = frozenset({"general", "general-purpose"})
_COLORS = frozenset({"red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan"})
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_PERMISSION_MODES = frozenset(
    {"acceptEdits", "bypassPermissions", "default", "dontAsk", "plan", "auto", "bubble"}
)
_MEMORY_SCOPES = frozenset({"user", "project", "local"})
_ISOLATION_MODES = frozenset({"worktree"})
_MISSING = object()


@dataclass(slots=True)
class AgentDefinition(Mapping[str, Any]):
    """Normalized definition with the old dict interface kept for callers."""

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
    color: str | None = None
    required_mcp_servers: list[str] = field(default_factory=list)
    mcp_servers: list[Any] = field(default_factory=list)
    hooks: dict[str, Any] | None = None

    _KEYS = (
        "name",
        "description",
        "prompt",
        "source",
        "path",
        "baseDir",
        "tools",
        "disallowedTools",
        "model",
        "effort",
        "permissionMode",
        "maxTurns",
        "background",
        "isolation",
        "skills",
        "initialPrompt",
        "memory",
        "color",
        "requiredMcpServers",
        "mcpServers",
        "hooks",
    )
    _ALIASES: ClassVar[dict[str, str]] = {
        "agentType": "name",
        "whenToUse": "description",
        "baseDir": "base_dir",
        "disallowed": "disallowed_tools",
        "disallowedTools": "disallowed_tools",
        "permissionMode": "permission_mode",
        "permission_mode": "permission_mode",
        "maxTurns": "max_turns",
        "max_turns": "max_turns",
        "initialPrompt": "initial_prompt",
        "initial_prompt": "initial_prompt",
        "requiredMcpServers": "required_mcp_servers",
        "required_mcp_servers": "required_mcp_servers",
        "mcpServers": "mcp_servers",
        "mcp_servers": "mcp_servers",
    }

    def __getitem__(self, key: str) -> Any:
        attr = self._ALIASES.get(key, key)
        if not hasattr(self, attr) or attr.startswith("_"):
            raise KeyError(key)
        return getattr(self, attr)

    def __iter__(self) -> Iterator[str]:
        return iter(self._KEYS)

    def __len__(self) -> int:
        return len(self._KEYS)

    @property
    def agentType(self) -> str:  # Claude Code vocabulary
        return self.name

    @property
    def whenToUse(self) -> str:
        return self.description

    @property
    def filename(self) -> str | None:
        return Path(self.path).stem if self.path else None

    @property
    def baseDir(self) -> str | None:
        return self.base_dir

    @property
    def disallowedTools(self) -> list[str] | None:
        return self.disallowed_tools

    @property
    def permissionMode(self) -> str | None:
        return self.permission_mode

    @property
    def maxTurns(self) -> int | None:
        return self.max_turns

    @property
    def initialPrompt(self) -> str | None:
        return self.initial_prompt

    @property
    def requiredMcpServers(self) -> list[str]:
        return self.required_mcp_servers

    @property
    def mcpServers(self) -> list[Any]:
        return self.mcp_servers

    def getSystemPrompt(self) -> str:
        return self.prompt

    def to_dict(self) -> dict[str, Any]:
        return dict(self)


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str] | None:
    """Split a Markdown document without mis-parsing ``---`` inside YAML."""
    lines = raw.removeprefix("\ufeff").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
        loaded = yaml.safe_load("".join(lines[1:end])) or {}
    except (StopIteration, yaml.YAMLError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded, "".join(lines[end + 1 :]).strip()


def _split_specs(value: Any) -> list[str]:
    """Parse comma/space lists while preserving permission text in ``(...)``."""
    if value is None or value is False:
        return []
    values = [value] if isinstance(value, str) else value if isinstance(value, Sequence) else []
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        current: list[str] = []
        depth = 0
        for char in item:
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            if depth == 0 and (char == "," or char.isspace()):
                spec = "".join(current).strip()
                if spec:
                    result.append(spec)
                current = []
            else:
                current.append(char)
        spec = "".join(current).strip()
        if spec:
            result.append(spec)
    return list(dict.fromkeys(result))


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
    return value.strip() if isinstance(value, str) and value.strip() else None


def _choice(value: Any, choices: frozenset[str]) -> str | None:
    value = _string(value)
    return value if value in choices else None


def _effort(value: Any) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _string(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in _EFFORTS:
        return lowered
    try:
        return int(text)
    except ValueError:
        return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 and str(value).strip() == str(parsed) else None


def _boolean(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _source_name(source: str) -> str:
    return {
        "builtin": "built-in",
        "built-in": "built-in",
        "user": "userSettings",
        "userSettings": "userSettings",
        "project": "projectSettings",
        "projectSettings": "projectSettings",
    }.get(source, source)


def parse(path: str | os.PathLike[str], *, source: str = "built-in", base_dir: str | None = None) -> AgentDefinition | None:
    """Parse one agent file; non-agent or malformed Markdown is skipped."""
    file_path = Path(path)
    try:
        split = _split_frontmatter(file_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None
    if split is None:
        return None
    meta, prompt = split
    name, description = _string(meta.get("name")), _string(meta.get("description"))
    if name is None or description is None:
        return None

    tools = None if "tools" not in meta else _tools(meta.get("tools"))
    denied = None if "disallowedTools" not in meta else _split_specs(meta.get("disallowedTools"))
    model = _string(meta.get("model")) or "inherit"
    if model.lower() == "inherit":
        model = "inherit"
    initial_prompt = meta.get("initialPrompt")
    if not isinstance(initial_prompt, str) or not initial_prompt.strip():
        initial_prompt = None

    return AgentDefinition(
        name=name,
        description=description.replace("\\n", "\n"),
        prompt=prompt,
        source=_source_name(source),
        path=str(file_path),
        base_dir=base_dir or str(file_path.parent),
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
        color=_choice(meta.get("color"), _COLORS),
        required_mcp_servers=_string_list(meta.get("requiredMcpServers")),
        mcp_servers=_mcp_servers(meta.get("mcpServers")),
        hooks=meta.get("hooks") if isinstance(meta.get("hooks"), dict) else None,
    )


def _definitions(root: str | os.PathLike[str] | None, source: str) -> list[AgentDefinition]:
    if root is None:
        return []
    directory = Path(root).expanduser()
    if not directory.is_dir():
        return []
    return [
        agent
        for path in sorted(directory.rglob("*.md"))
        if (agent := parse(path, source=source, base_dir=str(directory))) is not None
    ]


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


def discover(
    root: str | os.PathLike[str] | None = None,
    *,
    user_root: str | os.PathLike[str] | None = None,
    project_root: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> dict[str, AgentDefinition]:
    """Load definitions with Claude Code precedence: built-in < user < project.

    Passing ``root`` makes discovery isolated unless a user/project root is also
    supplied, which keeps tests and embedders independent of local config.
    """
    builtin_root = root or ROOT
    use_defaults = root is None
    user_dir = user_root if user_root is not None else (_user_agents_dir() if use_defaults else None)
    if project_root is not None:
        project_dirs = [Path(project_root)]
    elif use_defaults:
        project_dirs = _project_agent_dirs(cwd)
    else:
        project_dirs = []

    merged: dict[str, AgentDefinition] = {}
    for source, directory in (("built-in", builtin_root), ("userSettings", user_dir)):
        for agent in _definitions(directory, source):
            merged[_canonical_name(agent.name)] = agent
    for directory in project_dirs:
        for agent in _definitions(directory, "projectSettings"):
            merged[_canonical_name(agent.name)] = agent

    result = {name: agent for name, agent in merged.items()}
    general = result.get("general-purpose")
    if general is not None:
        result["general"] = general
    return result


def _agent_value(agent: AgentDefinition | Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        try:
            value = agent[key]
        except (KeyError, TypeError):
            continue
        if value is not _MISSING:
            return value
    return default


def _tool_name(spec: str) -> str:
    return spec.split("(", 1)[0].strip()


def resolve_tools(
    agent: AgentDefinition | Mapping[str, Any], inherited: Sequence[str] | None = None
) -> list[str] | None:
    """Resolve the allowlist, then apply ``disallowedTools`` by base tool name.

    ``None`` means unrestricted/inherit.  An explicit empty list means no tools.
    Pass the runtime's available tool names as ``inherited`` to resolve a
    deny-only definition into a concrete list.
    """
    raw_allow = _agent_value(agent, "tools", default=None)
    allow = _tools(raw_allow) if raw_allow is not None else None
    denied = _split_specs(_agent_value(agent, "disallowedTools", "disallowed", default=[]))

    if allow is None:
        resolved = None if inherited is None else list(dict.fromkeys(inherited))
    elif inherited is None:
        resolved = allow
    else:
        available = {_tool_name(tool).casefold() for tool in inherited}
        resolved = [spec for spec in allow if _tool_name(spec).casefold() in available]

    if not denied:
        return resolved
    if any(spec == "*" for spec in denied):
        return []
    if resolved is None:
        return None
    denied_names = {_tool_name(spec).casefold() for spec in denied}
    return [spec for spec in resolved if _tool_name(spec).casefold() not in denied_names]


def is_tool_allowed(agent: AgentDefinition | Mapping[str, Any], tool: str) -> bool:
    """Check one tool without needing an enumerable inherited tool pool."""
    name = _tool_name(tool).casefold()
    denied = _split_specs(_agent_value(agent, "disallowedTools", "disallowed", default=[]))
    if any(spec == "*" or _tool_name(spec).casefold() == name for spec in denied):
        return False
    raw_allow = _agent_value(agent, "tools", default=None)
    allow = _tools(raw_allow) if raw_allow is not None else None
    return allow is None or any(_tool_name(spec).casefold() == name for spec in allow)


def has_required_mcp_servers(
    agent: AgentDefinition | Mapping[str, Any], available_servers: Sequence[str]
) -> bool:
    """Every required pattern must occur in an available server name."""
    required = _string_list(
        _agent_value(agent, "requiredMcpServers", "required_mcp_servers", default=[])
    )
    available = [server.lower() for server in available_servers]
    return all(any(pattern.lower() in server for server in available) for pattern in required)


def filter_agents_by_mcp_requirements(
    agents: Sequence[AgentDefinition], available_servers: Sequence[str]
) -> list[AgentDefinition]:
    return [agent for agent in agents if has_required_mcp_servers(agent, available_servers)]


def roster_text(agents: Mapping[str, AgentDefinition]) -> str:
    """Human/model-facing roster, with general aliases shown only once."""
    if not agents:
        return "(no specialized types; use general-purpose/general)"
    seen: set[int] = set()
    lines: list[str] = []
    for agent in agents.values():
        if id(agent) in seen:
            continue
        seen.add(id(agent))
        lines.append(f"- {agent.name}: {agent.description}")
    return "\n".join(lines)

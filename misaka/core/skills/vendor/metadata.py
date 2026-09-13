# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / agent/skill_utils.py; see PROVENANCE.json and LICENSE.
import ast
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple
from ..host import is_termux

PLATFORM_MAP = {"macos": "darwin", "linux": "linux", "windows": "win32"}


_yaml_load_fn = None


def yaml_load(content: str):
    """Parse YAML with lazy import and CSafeLoader preference."""
    global _yaml_load_fn
    if _yaml_load_fn is None:
        import functools
        import yaml
        _yaml_load_fn = functools.partial(yaml.load, Loader=getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader)
    return _yaml_load_fn(content)


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from markdown; returns (frontmatter_dict, body).
    Malformed YAML falls back to key:value line splitting. A leading UTF-8 BOM
    (Windows editors) is stripped first or it would defeat the ``---`` fence check."""
    content = content.removeprefix("\ufeff")
    end_match = re.search(r"\n---\s*\n", content[3:]) if content.startswith("---") else None
    if not end_match:
        return {}, content
    yaml_content = content[3 : end_match.start() + 3]
    body = content[end_match.end() + 3 :]
    frontmatter: Dict[str, Any] = {}
    try:
        parsed = yaml_load(yaml_content)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        for line in yaml_content.strip().split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                frontmatter[key.strip()] = value.strip()
    return frontmatter, body


def skill_matches_platform_list(platforms: Any) -> bool:
    """Return True when *platforms* is compatible with the current OS."""
    if not platforms:
        return True
    running_in_termux = is_termux()
    for platform in platforms if isinstance(platforms, list) else [platforms]:
        normalized = str(platform).lower().strip()
        mapped = PLATFORM_MAP.get(normalized, normalized)
        # Termux is a Linux userland on Android: accept linux-tagged skills
        # whether sys.platform is "linux" (pre-3.13) or "android" (3.13+).
        if sys.platform.startswith(mapped) or (running_in_termux and mapped in ("linux", "termux", "android")):
            return True
    return False


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    """True when the skill's ``platforms:`` list (absent = all) matches this OS."""
    return skill_matches_platform_list(frontmatter.get("platforms"))


def skill_matches_environment(frontmatter: Dict[str, Any], _detect_environment) -> bool:
    """True when ANY declared ``environments:`` tag is active (absent = all;
    unknown tags fail open). Offer-time filter only."""
    environments = frontmatter.get("environments")
    if not environments:
        return True
    tags = [str(env).lower().strip() for env in (environments if isinstance(environments, list) else [environments])]
    return any(_detect_environment(tag) for tag in tags if tag)


def parse_config_string_list(value) -> List[str]:
    """Normalize a config value that may hold a JSON-array string into a list.
    ``hermes config set`` stores lists as quoted JSON/Python-literal strings;
    treating one as a single name would silently filter nothing. A scalar
    string still means one name.

    See #13026, #86661.
    """
    if isinstance(value, str):
        if value.strip().startswith("["):
            try:
                parsed = ast.literal_eval(value.strip())
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        return [value]
    return [str(item) for item in value] if isinstance(value, (list, tuple, set, frozenset)) else []


def _normalize_string_set(values) -> Set[str]:
    return {name.strip() for name in parse_config_string_list(values) if name.strip()}


def _hermes_metadata(frontmatter: Dict[str, Any]) -> Dict[str, Any]:
    """``metadata.hermes`` mapping from frontmatter, or ``{}`` when malformed."""
    metadata = frontmatter.get("metadata")
    hermes = metadata.get("hermes") if isinstance(metadata, dict) else None
    return hermes if isinstance(hermes, dict) else {}


_CONDITION_KEYS = ("fallback_for_toolsets", "requires_toolsets", "fallback_for_tools", "requires_tools", "session_platforms")


def extract_skill_conditions(frontmatter: Dict[str, Any]) -> Dict[str, List]:
    """Extract conditional activation fields from parsed frontmatter (absent = ``[]``)."""
    hermes = _hermes_metadata(frontmatter)
    return {key: hermes.get(key, []) for key in _CONDITION_KEYS}


def extract_skill_config_vars(frontmatter: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract ``metadata.hermes.config`` declarations (key/description/default/prompt).
    Entries missing ``key`` or ``description`` are skipped; ``prompt`` defaults to the description."""
    raw = _hermes_metadata(frontmatter).get("config")
    if isinstance(raw, dict):
        raw = [raw]
    if not raw or not isinstance(raw, list):
        return []
    result: Dict[str, Dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        desc = str(item.get("description", "")).strip()
        if not key or key in result or not desc:
            continue
        entry: Dict[str, Any] = {"key": key, "description": desc}
        if item.get("default") is not None:
            entry["default"] = item["default"]
        prompt_text = item.get("prompt")
        entry["prompt"] = prompt_text.strip() if isinstance(prompt_text, str) and prompt_text.strip() else desc
        result[key] = entry
    return list(result.values())


SKILL_CONFIG_PREFIX = "skills.config"


def _resolve_dotpath(config: Dict[str, Any], dotted_key: str):
    """Walk a nested dict following a dotted key; None if any part is missing."""
    current = config
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def resolve_skill_config_values(config_vars: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    """Map logical skill config keys to current values (or declared defaults);
    path-like string values are ``~``/``${VAR}`` expanded."""
    resolved: Dict[str, Any] = {}
    for var in config_vars:
        value = _resolve_dotpath(config, f"{SKILL_CONFIG_PREFIX}.{var['key']}")
        if value is None or (isinstance(value, str) and not value.strip()):
            value = var.get("default", "")
        if isinstance(value, str) and ("~" in value or "${" in value):
            value = os.path.expanduser(os.path.expandvars(value))
        resolved[var["key"]] = value
    return resolved


SKILL_PROMPT_DESC_LIMIT = 60


def _normalize_skill_description(frontmatter: Dict[str, Any]) -> str:
    """Normalize a skill's description field for comparison/truncation."""
    raw_desc = frontmatter.get("description", "")
    return str(raw_desc).strip().strip("'\"") if raw_desc else ""


def extract_skill_description(frontmatter: Dict[str, Any]) -> str:
    """Extract a system-prompt-length description from parsed frontmatter."""
    desc = _normalize_skill_description(frontmatter)
    return desc[:SKILL_PROMPT_DESC_LIMIT - 3] + "..." if len(desc) > SKILL_PROMPT_DESC_LIMIT else desc


def is_skill_description_truncated_for_prompt(frontmatter: Dict[str, Any]) -> bool:
    """True when the description will be truncated in the system prompt skill index."""
    return len(_normalize_skill_description(frontmatter)) > SKILL_PROMPT_DESC_LIMIT


_NAMESPACE_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def parse_qualified_name(name: str) -> Tuple[Optional[str], str]:
    """Split ``'namespace:skill-name'`` into ``(namespace, bare_name)``; ``(None, name)`` without ``':'``."""
    namespace, sep, bare = name.partition(":")
    return (namespace, bare) if sep else (None, name)


def is_valid_namespace(candidate: Optional[str]) -> bool:
    """Check whether *candidate* is a valid namespace (``[a-zA-Z0-9_-]+``)."""
    return bool(candidate) and bool(_NAMESPACE_RE.match(candidate))


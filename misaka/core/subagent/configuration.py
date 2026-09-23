"""MISAKA session adapters for CCB settings-scoped agent permissions and hooks."""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def project_settings(cwd: str) -> Mapping[str, Any]:
    """Read only the explicitly trusted target project's native settings file."""
    from misaka.config import home

    project_dir = home.project_dir(cwd)
    if project_dir is None:
        return {}
    try:
        value = json.loads((project_dir / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def settings_layers(session: Any, *, cwd: str | None = None, include_project: bool = True) -> list[Mapping[str, Any]]:
    manager = getattr(session, "settingsManager", None)
    if manager is None:
        return []
    trusted = getattr(manager, "isProjectTrusted", lambda: include_project)()
    session_cwd = getattr(session, "cwd", None)
    same_project = cwd is None or session_cwd is None or Path(cwd).resolve() == Path(session_cwd).resolve()
    names = ['getGlobalSettings']
    if include_project and trusted and same_project:
        names.append('getProjectSettings')
    names.append('getManagedSettings')
    layers = [value for name in names if callable(getter := getattr(manager, name, None))
              and isinstance(value := getter(), Mapping)]
    if cwd is not None and include_project and not same_project:
        # Parent trust never grants another directory. Caller supplies the
        # independently resolved target trust bit.
        layers.insert(1, project_settings(cwd))
    return layers


def denied_agent_types(session: Any, *, cwd: str | None = None, include_project: bool = True) -> set[str]:
    # CCB filterDeniedAgents: exact Agent(type) rule content, not a glob match.
    from misaka.core.subagent.policy import split_rule

    denied = set()
    for layer in settings_layers(session, cwd=cwd, include_project=include_project):
        permissions = layer.get('permissions') or {}
        for rule in permissions.get('deny', []) if isinstance(permissions, Mapping) else []:
            if isinstance(rule, str):
                tool, content = split_rule(rule)
                if tool in {'agent', 'task'} and content is not None:
                    denied.add(content)
    return denied



def hook_controls(session: Any, *, cwd: str | None = None, include_project: bool = True) -> tuple[bool, bool]:
    layers = settings_layers(session, cwd=cwd, include_project=include_project)
    disabled = any(layer.get("disableAllHooks") is True for layer in layers)
    manager = getattr(session, "settingsManager", None)
    managed_getter = getattr(manager, "getManagedSettings", None)
    managed = managed_getter() if callable(managed_getter) else {}
    managed_only = isinstance(managed, Mapping) and managed.get("allowManagedHooksOnly") is True
    return disabled, managed_only


def configured_hooks(session: Any, *, cwd: str | None = None, include_project: bool = True) -> dict[str, Any]:
    from misaka.core.subagent.hooks import validate_hooks

    layers = settings_layers(session, cwd=cwd, include_project=include_project)
    disabled, managed_only = hook_controls(session, cwd=cwd, include_project=include_project)
    if disabled or os.environ.get("MISAKA_SUBAGENT_HOOKS_DISABLED") == "1":
        return {}
    inherited_managed_only = os.environ.get("MISAKA_SUBAGENT_MANAGED_HOOKS_ONLY") == "1"
    if managed_only or inherited_managed_only:
        manager = getattr(session, "settingsManager", None)
        getter = getattr(manager, "getManagedSettings", None)
        layers = [getter()] if callable(getter) else []
        if inherited_managed_only:
            # Only the parent's filtered managed snapshot crosses this fence.
            # Re-reading ordinary user/project hooks here would undo the policy,
            # including for VCS lifecycle hooks and recursively launched children.
            inherited = json.loads(os.environ.get("MISAKA_SUBAGENT_HOST_HOOKS", "{}"))
            layers.insert(0, {"hooks": inherited})
    merged: dict[str, list[Any]] = {}
    for layer in layers:
        hooks = layer.get('hooks', {})
        hooks = validate_hooks(hooks)
        for name, matchers in hooks.items():
            merged.setdefault(name, []).extend(matchers)
    return merged


def permission_settings(session: Any, *, cwd: str | None = None, include_project: bool = True) -> list[dict[str, Any]]:
    """Normalize additionalDirectories before crossing a cwd/process boundary."""
    root = Path(cwd or getattr(session, 'cwd', None) or os.getcwd()).expanduser().resolve()
    result = []
    for layer in settings_layers(session, cwd=cwd, include_project=include_project):
        permissions = layer.get('permissions', {})
        if not isinstance(permissions, Mapping):
            continue
        value = dict(permissions)
        directories = value.get('additionalDirectories', [])
        if not isinstance(directories, list) or any(not isinstance(path, str) for path in directories):
            raise ValueError('permissions.additionalDirectories must be a list of paths')
        if 'additionalDirectories' in value:
            value['additionalDirectories'] = [str((root / Path(path).expanduser()).resolve()) for path in directories]
        result.append(value)
    trusted = getattr(getattr(session, "settingsManager", None), "isProjectTrusted", lambda: include_project)()
    if include_project and trusted and (cwd is None or root == Path(getattr(session, "cwd", None) or root).resolve()):
        for part in getattr(getattr(session, "moments", None), "parts", ()):
            getter = getattr(part, "get_permission_settings", None)
            if callable(getter) and (grant := getter()):
                result.append(grant)
    return result


def inherited_permissions() -> list[dict[str, Any]]:
    value = json.loads(os.environ.get('MISAKA_SUBAGENT_PERMISSION_SETTINGS', '[]'))
    if not isinstance(value, list) or any(not isinstance(layer, dict) for layer in value):
        raise ValueError('Inherited permission settings must be a list of objects')
    for layer in value:
        for key in ('allow', 'ask', 'deny', 'additionalDirectories'):
            if key in layer and (not isinstance(layer[key], list) or any(not isinstance(item, str) for item in layer[key])):
                raise ValueError(f'Inherited permission {key} must be a list of strings')
        scopes = layer.get("writeDirectories", {})
        if (not isinstance(scopes, dict) or any(
            tool not in {"write", "edit"} or not isinstance(paths, list)
            or any(not isinstance(path, str) or not Path(path).is_absolute() for path in paths)
            for tool, paths in scopes.items()
        )):
            raise ValueError("Inherited writeDirectories must map write/edit to absolute directory lists")
    return value


def permission_decision(
    permissions: list[dict[str, Any]], name: str, tool_input: Mapping[str, Any], *, workspace: str | None = None,
) -> str | None:
    """Source rule ordering, shared by workers and model-based hook verifiers."""
    from misaka.core.subagent.policy import _resolved_path, rule_matches

    candidate = None
    inputs = [tool_input]
    if workspace and name in {"write", "edit"}:
        raw = tool_input.get("path") or tool_input.get("file_path")
        candidate = _resolved_path(raw, workspace) if isinstance(raw, str) and raw.strip() else None
        if candidate is not None:
            # A configured deny/ask applies to either spelling of the same file.
            inputs.append({**tool_input, "path": str(candidate)})
            root = Path(workspace).resolve()
            if candidate.is_relative_to(root):
                inputs.append({**tool_input, "path": str(candidate.relative_to(root))})

    for behavior in ('deny', 'ask', 'allow'):
        for layer in permissions:
            entries = layer.get(behavior, ())
            if isinstance(entries, (list, tuple)) and any(
                isinstance(rule, str) and any(rule_matches(rule, name, value) for value in inputs)
                for rule in entries
            ):
                return behavior
    # Card output grants use resolved paths, not globs over raw model arguments:
    # ../ and symlinks must not turn a subtree grant into a whole-project grant.
    if candidate is not None and any(
        candidate.is_relative_to(Path(directory))
        for layer in permissions for directory in layer.get("writeDirectories", {}).get(name, ())
    ):
        return "allow"
    return None


PERMISSION_MODES = frozenset({"default", "plan", "acceptEdits", "bypassPermissions", "dontAsk", "auto", "bubble"})


def validate_permission_mode(mode):
    if not isinstance(mode, str) or mode not in PERMISSION_MODES:
        raise ValueError("Invalid permission mode")
    return mode


def current_permission_mode(session, fallback=None):
    """Native live session state, plus the parent's just-refreshed child snapshot."""
    from misaka.core.subagent import policy

    if policy._permission_settings_provider is not None:
        return validate_permission_mode(os.environ.get("MISAKA_SUBAGENT_PERMISSION_MODE", fallback or "default"))
    getter = getattr(session, "getPermissionMode", None)
    mode = getter() if callable(getter) else None
    return validate_permission_mode(mode if mode is not None else fallback or "default")


def child_permission_mode(parent, requested):
    """Source runAgent.agentGetAppState resolves this on every access, not launch."""
    parent = validate_permission_mode(parent or "default")
    if requested is not None:
        validate_permission_mode(requested)
    return requested if requested and parent not in {"bypassPermissions", "acceptEdits", "auto"} else parent

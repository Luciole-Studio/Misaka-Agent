"""Translated from pi's ``packages/coding-agent/src/core/project-trust.ts`` (v1.3.0).

MISAKA forks:
- No RPC mode.
- The prompt names what trusting loads, since MISAKA never installs project packages or runs project extensions.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, TypedDict

from misaka.config import APP_NAME, home
from misaka.core.settings_defaults import SETTINGS_DEFAULTS
from misaka.core.trust_manager import (
    ProjectTrustDecision,
    ProjectTrustOption,
    ProjectTrustStore,
    ProjectTrustStoreEntry,
    ProjectTrustUpdate,
    getProjectTrustOptions,
    hasTrustRequiringProjectResources,
)

# MISAKA fork: no RPC mode.
type AppMode = Literal["interactive", "print", "json"]


class ResolveProjectTrustedOptions(TypedDict, total=False):
    cwd: str
    trustStore: ProjectTrustStore
    trustOverride: bool
    defaultProjectTrust: Literal["ask", "always", "never"]
    extensionsResult: Any
    projectTrustContext: Any
    onExtensionError: Callable[[str], None]


def formatProjectTrustPrompt(cwd: str) -> str:
    # MISAKA fork: what trusting loads (see trust_manager.hasTrustRequiringProjectResources).
    return (
        f"Trust project folder?\n{cwd}\n\n"
        f"This allows {APP_NAME} to load {home.DIR_NAME}/settings.json, {home.DIR_NAME}/mcp.json, "
        f"{home.DIR_NAME}/prompts, {home.DIR_NAME}/themes, and Sisters project "
        f"agent definitions from {home.DIR_NAME}/{home.SUBAGENTS_DIR}."
    )


def _member(source: Any, name: str) -> Any:
    """``source.name`` for an object or a mapping."""
    return source[name] if isinstance(source, Mapping) else getattr(source, name)


async def selectProjectTrustOption(cwd: str, ctx: Any) -> ProjectTrustOption | None:
    options = getProjectTrustOptions(cwd, {"includeSessionOnly": True})
    selected = await _member(_member(ctx, "ui"), "select")(
        formatProjectTrustPrompt(cwd),
        [option.label for option in options],
    )
    return next((option for option in options if option.label == selected), None)


def saveProjectTrustPromptResult(trustStore: ProjectTrustStore, result: ProjectTrustOption) -> None:
    if len(result.updates) > 0:
        trustStore.setMany(result.updates)


async def resolveProjectTrusted(options: ResolveProjectTrustedOptions) -> bool:
    if options.get("trustOverride") is not None:
        return options["trustOverride"]
    if not hasTrustRequiringProjectResources(options["cwd"]):
        return True

    if options.get("extensionsResult"):
        from misaka.core.extensions.runner import emitProjectTrustEvent

        emitted = await emitProjectTrustEvent(
            options["extensionsResult"],
            {"type": "project_trust", "cwd": options["cwd"]},
            options["projectTrustContext"],
        )
        result, errors = emitted.get("result"), emitted["errors"]
        for error in errors:
            onExtensionError = options.get("onExtensionError")
            if onExtensionError is not None:
                onExtensionError(f'Extension "{error.extensionPath}" project_trust error: {error.error}')
        if result:
            trusted = result["trusted"] == "yes"
            if result.get("remember") is True:
                options["trustStore"].set(options["cwd"], trusted)
            return trusted

    decision = options["trustStore"].get(options["cwd"])
    if decision is not None:
        return decision

    match options.get("defaultProjectTrust") or SETTINGS_DEFAULTS["defaultProjectTrust"]:
        case "always":
            return True
        case "never":
            return False
        case "ask":
            pass

    if not _member(options["projectTrustContext"], "hasUI"):
        return False

    selected = await selectProjectTrustOption(options["cwd"], options["projectTrustContext"])
    if selected is not None:
        saveProjectTrustPromptResult(options["trustStore"], selected)
        return selected.trusted
    return False


__all__ = [
    "AppMode",
    "ProjectTrustDecision",
    "ProjectTrustOption",
    "ProjectTrustStore",
    "ProjectTrustStoreEntry",
    "ProjectTrustUpdate",
    "ResolveProjectTrustedOptions",
    "hasTrustRequiringProjectResources",
    "resolveProjectTrusted",
]

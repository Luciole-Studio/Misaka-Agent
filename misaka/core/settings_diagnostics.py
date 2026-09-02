"""Settings load diagnostics shared by startup and runtime creation."""

from __future__ import annotations

from collections.abc import Sequence

from misaka.core.agent_session_services import AgentSessionRuntimeDiagnostic
from misaka.core.settings_manager import SettingsManager


def collect_settings_diagnostics(
    settings_manager: SettingsManager,
) -> list[AgentSessionRuntimeDiagnostic]:
    return [
        AgentSessionRuntimeDiagnostic(
            type="warning",
            message=(
                f"Invalid settings file {item.path}: {item.error}"
                if item.path
                else f"Invalid {item.scope} settings: {item.error}"
            ),
        )
        for item in settings_manager.drainErrors()
    ]


def deduplicate_diagnostics(
    diagnostics: Sequence[AgentSessionRuntimeDiagnostic],
) -> list[AgentSessionRuntimeDiagnostic]:
    seen: set[tuple[str, str]] = set()
    result: list[AgentSessionRuntimeDiagnostic] = []
    for diagnostic in diagnostics:
        key = (diagnostic.type, diagnostic.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(diagnostic)
    return result


__all__ = ["collect_settings_diagnostics", "deduplicate_diagnostics"]

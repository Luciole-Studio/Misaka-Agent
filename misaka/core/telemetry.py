"""Telemetry feature-flag helpers for coding-agent integrations."""

from __future__ import annotations

import os
from typing import Protocol


class _SettingsManagerLike(Protocol):
    def getEnableInstallTelemetry(self) -> bool: ...


_UNSET = object()


def _is_truthy_env_flag(value: str | None) -> bool:
    if not value:
        return False
    return value == "1" or value.lower() in {"true", "yes"}


def is_install_telemetry_enabled(
    settings_manager: _SettingsManagerLike,
    telemetry_env: str | None | object = _UNSET,
) -> bool:
    resolved_env = os.environ.get("MISAKA_TELEMETRY") if telemetry_env is _UNSET else telemetry_env
    if resolved_env is not None:
        return _is_truthy_env_flag(resolved_env)
    return settings_manager.getEnableInstallTelemetry()


isInstallTelemetryEnabled = is_install_telemetry_enabled

__all__ = [
    "isInstallTelemetryEnabled",
]

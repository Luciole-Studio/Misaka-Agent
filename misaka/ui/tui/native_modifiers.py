"""Translated from pi-tui's ``packages/tui/src/native-modifiers.ts`` (v1.1.0)."""

from __future__ import annotations

from .native_platform import ModifierKey, getNativePlatformHelper


def isNativeModifierPressed(key: ModifierKey) -> bool:
    helper = getNativePlatformHelper()
    isModifierPressed = getattr(helper, "isModifierPressed", None)
    if not isModifierPressed:
        return False
    try:
        return isModifierPressed(key) is True
    except Exception:  # noqa: BLE001 - upstream `catch { return false }`: the native layer is best-effort
        return False


__all__ = ["ModifierKey", "isNativeModifierPressed"]

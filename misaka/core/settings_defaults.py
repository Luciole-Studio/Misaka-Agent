"""Translated from pi's ``packages/coding-agent/src/core/settings-defaults.ts`` (v1.3.0).

MISAKA fork: upstream's ``as const`` object is a read-only mapping, nested the same way and read as
``SETTINGS_DEFAULTS["compaction"]["enabled"]``. misaka has no analytics (``enableAnalytics``) and no fullscreen renderer
(``tuiMode``, ``fullscreenExitOutput``, ``fullscreenScrollbar``, ``fullscreenCopyOnSelect``,
``fullscreenWheelScrollLines``), so those defaults are absent, and ``enableInstallTelemetry`` keeps MISAKA's own default.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from misaka.ai.utils.retry import DEFAULT_MAX_AGENT_RETRY_DELAY_MS


def _readOnly(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """MISAKA fork: ``as const`` -- the mapping and every mapping in it are read-only."""
    return MappingProxyType({key: _readOnly(child) if isinstance(child, Mapping) else child for key, child in value.items()})


# Runtime defaults shared by settings accessors and the published JSON Schema.
SETTINGS_DEFAULTS: Mapping[str, Any] = _readOnly({
    "transport": "auto",
    "sessionShareDestination": "auto",
    "steeringMode": "one-at-a-time",
    "followUpMode": "one-at-a-time",
    "compaction": {
        "enabled": True,
        "reserveTokens": 16384,
        "keepRecentTokens": 20000,
    },
    "branchSummary": {
        "reserveTokens": 16384,
        "skipPrompt": False,
    },
    "retry": {
        "enabled": True,
        "maxRetries": 3,
        "baseDelayMs": 2000,
        "maxAgentDelayMs": DEFAULT_MAX_AGENT_RETRY_DELAY_MS,
        "provider": {
            "maxRetryDelayMs": 60000,
        },
    },
    "hideThinkingBlock": False,
    "showCacheMissNotices": False,
    "quietStartup": False,
    "defaultProjectTrust": "ask",
    "collapseChangelog": False,
    # MISAKA fork: off. The setting answers "may this install be identified to a service"; all that rides on it is
    # `core/provider_attribution.py` naming the client "misaka" to aggregator gateways. Upstream defaults its install
    # ping on; being named is something a MISAKA user turns on rather than something they find.
    "enableInstallTelemetry": False,
    "enableSkillCommands": True,
    "terminal": {
        "showImages": True,
        "imageWidthCells": 60,
        "clearOnShrink": False,
        "showTerminalProgress": False,
    },
    "images": {
        "autoResize": True,
        "blockImages": False,
    },
    "doubleEscapeAction": "tree",
    "treeFilterMode": "default",
    "editorPaddingX": 0,
    "outputPad": 1,
    "autocompleteMaxVisible": 5,
    "markdown": {
        "codeBlockIndent": "  ",
        "mermaid": "streaming",
    },
    "warnings": {
        "anthropicExtraUsage": True,
    },
    "codemode": {
        "mode": "on",
        "inlineBudget": 3000,
    },
    "cacheWarming": "streaming",
})


__all__ = ["SETTINGS_DEFAULTS"]

"""Interactive mode exports for misaka."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "ANTHROPIC_SUBSCRIPTION_AUTH_WARNING",
    "InteractiveMode",
    "is_anthropic_subscription_auth_key",
]

if TYPE_CHECKING:
    from misaka.ui.tui.interactive.interactive_mode import (
        ANTHROPIC_SUBSCRIPTION_AUTH_WARNING,
        InteractiveMode,
        is_anthropic_subscription_auth_key,
    )


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)

    from misaka.ui.tui.interactive.interactive_mode import (
        ANTHROPIC_SUBSCRIPTION_AUTH_WARNING,
        InteractiveMode,
        is_anthropic_subscription_auth_key,
    )

    exports = {
        "ANTHROPIC_SUBSCRIPTION_AUTH_WARNING": ANTHROPIC_SUBSCRIPTION_AUTH_WARNING,
        "InteractiveMode": InteractiveMode,
        "is_anthropic_subscription_auth_key": is_anthropic_subscription_auth_key,
    }
    return exports[name]


def __dir__() -> list[str]:
    return sorted(__all__)

"""CCB AgentTool background feature controls, with MISAKA environment names."""
import os

from misaka.core.prompt_templates import _ECMASCRIPT_WHITESPACE

_SPACE = "".join(_ECMASCRIPT_WHITESPACE)


def _truthy(value: str | bool | None) -> bool:
    """CCB envUtils.isEnvTruthy, including its ECMAScript trim semantics."""
    if isinstance(value, bool):
        return value
    return (value or "").lower().strip(_SPACE) in {"1", "true", "yes", "on"}


def _defined_falsy(value: str | bool | None) -> bool:
    """CCB envUtils.isEnvDefinedFalsy; absent/empty is not an override."""
    if isinstance(value, bool):
        return not value
    return (value or "").lower().strip(_SPACE) in {"0", "false", "no", "off"}


def background_disabled() -> bool:
    from misaka.config.product import setting

    return not setting("subagents", "background_tasks", True, bool)


def auto_background_seconds() -> float:
    # CCB getAutoBackgroundMs: 120_000 ms when enabled, no timer otherwise.
    from misaka.config.product import setting

    return 120.0 if setting("subagents", "auto_background_tasks", False, bool) else 0.0


def default_bash_timeout_seconds() -> float:
    """CCB getDefaultBashTimeoutMs, adapted to native Bash's seconds unit.

    Preserve JS parseInt(decimal) and Node's 1 ms overflowing-timer behavior.
    BASH_MAX_TIMEOUT_MS is upstream prompt text, not an execution clamp.
    """
    from misaka.core.subagent.model import parse_decimal_prefix

    parsed = parse_decimal_prefix(os.environ.get("BASH_DEFAULT_TIMEOUT_MS", ""))
    if parsed is None or parsed <= 0:
        return 120.0
    return .001 if parsed > 2_147_483_647 else parsed / 1000

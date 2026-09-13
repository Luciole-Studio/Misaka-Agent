"""Original auxiliary reasoning-level parser."""

VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def parse_reasoning_effort(effort) -> dict | None:
    """Parse a reasoning effort level into a config dict.

    ``None`` for empty/unrecognized input (caller uses the default); ``{"enabled": False}`` for
    "none"/"false"/"disabled"/YAML False — ``reasoning_effort: false`` must mean disabled.
    """
    if effort is None or effort is True:
        return None
    effort = str(effort).strip().lower()  # False -> "false" -> disabled; "" matches neither set
    if effort in {"none", "false", "disabled"}:
        return {"enabled": False}
    if effort in VALID_REASONING_EFFORTS:
        return {"enabled": True, "effort": effort}
    return None

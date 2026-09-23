"""Whether this install may be identified to an outside service.

Ported from pi's ``src/core/telemetry.ts``. It is only the question, never the answer:
the module decides nothing and sends nothing. Upstream reads it in two places -- before
attaching provider attribution headers (``provider-attribution.ts``) and before the
post-update version ping (``interactive-mode.ts``) -- so that both honour one switch
instead of two.

MISAKA asks it in one place: ``core/provider_attribution.py``, before naming the client
"misaka" to an aggregator gateway. There is no version ping. The switch is also here so an
extension that wants to identify the install has somewhere to ask, and because a setting
that appears only once someone starts reporting is a setting nobody was ever offered.

Precedence follows upstream: the environment variable wins when it is set at all, even
set to a falsy value, so ``MISAKA_TELEMETRY=0`` turns it off regardless of the settings
file. Absent the variable, the stored setting decides. Upstream defaults that on; MISAKA
defaults it off (``SettingsManager.getEnableInstallTelemetry``), so being named to a
gateway is something the user turns on rather than something they find.
"""

from __future__ import annotations

import os
from typing import Any


def _isTruthyEnvFlag(value: str | None) -> bool:
    if not value:
        return False
    return value == "1" or value.lower() in ("true", "yes")


def isInstallTelemetryEnabled(
    settingsManager: Any,
    telemetryEnv: str | None = None,
) -> bool:
    """``True`` when this install may be identified to an outside service.

    ``telemetryEnv`` defaults to ``MISAKA_TELEMETRY``. It is read here rather than as a
    default argument value so a test -- or a caller that has its own source for it --
    can pass the variable in without touching the process environment.
    """
    if telemetryEnv is None:
        telemetryEnv = os.environ.get("MISAKA_TELEMETRY")
    if telemetryEnv is not None:
        return _isTruthyEnvFlag(telemetryEnv)
    return bool(settingsManager.getEnableInstallTelemetry())


__all__ = ["isInstallTelemetryEnabled"]

"""A model or thinking-level switch during a compaction voids the compaction when it lands
(`_checkCompactionSource` sees the model and the appended entries changed). With LCM a
compaction can take minutes; on 2026-09-24 a Sister at 1.19M tokens lost a five-minute one to
three `/model` presses and overflowed. The switch entry points now wait, as `/reload`
already does; an idle session is unchanged."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from misaka.modes.interactive.interactive_mode import InteractiveMode


def _mode(compacting):
    warnings, selector = [], []
    mode = SimpleNamespace(
        session=SimpleNamespace(
            isCompacting=compacting,
            cycleModel=AsyncMock(return_value=None),
            cycleThinkingLevel=Mock(return_value=None),
            scopedModels=[],
        ),
        showWarning=warnings.append,
        showStatus=lambda text: None,
        showError=lambda text: None,
        showModelSelector=lambda *args, **kwargs: selector.append(args),
    )
    mode._refuseSwitchDuringCompaction = lambda what: InteractiveMode._refuseSwitchDuringCompaction(mode, what)
    return mode, warnings, selector


async def test_every_switch_waits_while_compacting():
    mode, warnings, selector = _mode(compacting=True)
    await InteractiveMode.handleModelCommand(mode, None)
    await InteractiveMode.cycleModel(mode, "forward")
    InteractiveMode.handleThinkingCommand(mode, "high")
    InteractiveMode.cycleThinkingLevel(mode)
    assert len(warnings) == 4 and all("compaction" in text for text in warnings)
    assert selector == [] and not mode.session.cycleModel.await_count
    mode.session.cycleThinkingLevel.assert_not_called()


@pytest.mark.parametrize("entry", ["command", "cycle"])
async def test_an_idle_session_switches_as_before(entry):
    mode, warnings, selector = _mode(compacting=False)
    if entry == "command":
        await InteractiveMode.handleModelCommand(mode, None)
        assert selector == [()]
    else:
        await InteractiveMode.cycleModel(mode, "forward")
        assert mode.session.cycleModel.await_count == 1
    assert warnings == []

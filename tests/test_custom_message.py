"""Translated from pi's ``coding-agent/test/custom-message.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time
from typing import Any

from misaka.core.messages import CustomMessage
from misaka.modes.interactive.components.custom_message import CustomMessageComponent
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import Text
from misaka.utils.ansi import stripAnsi


def test_provides_output_padding_to_custom_renderers_and_updates_it():
    initTheme("dark")
    optionsSeen: list[Any] = []

    def renderer(_message: Any, options: Any, _theme: Any) -> Text:
        optionsSeen.append(dict(options))
        return Text("custom", options["outputPad"], 0)

    message = CustomMessage(customType="test", content="custom", display=True, timestamp=int(time.time() * 1000))
    component = CustomMessageComponent(message, renderer, None, 1)

    assert optionsSeen == [{"expanded": False, "outputPad": 1}]
    assert any(line.startswith(" custom") for line in map(stripAnsi, component.render(40)))

    component.setOutputPad(0)

    assert optionsSeen[-1] == {"expanded": False, "outputPad": 0}
    assert any(line.startswith("custom") for line in map(stripAnsi, component.render(40)))

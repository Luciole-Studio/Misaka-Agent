"""ExtensionSelectorComponent options and the Radius `/login` menu built on it."""

from __future__ import annotations

import pytest

from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components.extension_selector import (
    ExtensionSelectorComponent,
)
from misaka.modes.interactive.components.radius_login_selector import (
    RadiusOption,
    createLoginMenuSelector,
    radiusShimmer,
)
from misaka.ui.tui import keybindings as tui_keybindings
from misaka.utils.ansi import stripAnsi


@pytest.fixture(autouse=True)
def _keybindings(monkeypatch):
    monkeypatch.setattr(tui_keybindings, "globalKeybindings", KeybindingsManager())


class _Tui:
    def __init__(self) -> None:
        self.renders = 0

    def requestRender(self, *_args) -> None:
        self.renders += 1


def test_the_selector_shows_a_description_under_the_title_when_given_one():
    plain = ExtensionSelectorComponent("Pick one:", ["a", "b"], lambda _option: None, lambda: None)
    described = ExtensionSelectorComponent(
        "Pick one:", ["a", "b"], lambda _option: None, lambda: None, {"description": "Radius is a service."}
    )

    assert "Radius is a service." not in stripAnsi("\n".join(plain.render(80)))
    assert "Radius is a service." in stripAnsi("\n".join(described.render(80)))
    assert len(described.render(80)) == len(plain.render(80)) + 2


def test_the_shimmer_colours_every_character_and_resets_the_foreground():
    shimmer = radiusShimmer("Sign in with Radius", 0)

    assert stripAnsi(shimmer) == "Sign in with Radius"
    assert shimmer.endswith("\x1b[39m")
    assert shimmer.count("\x1b[38;") == len("Sign in with Radius")
    # The bands flow: a later frame colours the first character differently.
    assert radiusShimmer("Sign in with Radius", 250) != shimmer


async def test_the_login_menu_animates_only_while_the_radius_option_is_selected():
    tui = _Tui()
    label = "Sign in with Radius • not configured"
    options = ["Sign in with an account", "Sign in with an API key", label]
    selected: list[str] = []
    selector = createLoginMenuSelector(
        tui, "Select authentication method:", options, RadiusOption(label=label, text="Sign in with Radius"),
        selected.append, lambda: None,
    )

    first = selector.render(80)
    assert selector.animating is False
    assert stripAnsi("\n".join(first)).count(label) == 1

    selector.handleInput("j")
    selector.handleInput("j")
    animated = selector.render(80)
    assert selector.animating is True
    assert stripAnsi("\n".join(animated)) == stripAnsi("\n".join(first)).replace(
        f"→ {options[0]}", f"  {options[0]}"
    ).replace(f"  {label}", f"→ {label}")
    # The selected Radius row is coloured character by character, unlike a plain selector's row.
    plain = ExtensionSelectorComponent("Select authentication method:", options, lambda _option: None, lambda: None)
    plain.handleInput("j")
    plain.handleInput("j")
    [plain_row] = [line for line in plain.render(80) if "Radius" in stripAnsi(line)]
    [animated_row] = [line for line in animated if "Radius" in stripAnsi(line)]
    assert animated_row != plain_row
    assert animated_row.count("\x1b[38;") >= len("Sign in with Radius")
    assert plain_row.count("\x1b[38;") < len("Sign in with Radius")

    selector.handleInput("\n")
    assert selected == [label]
    assert selector.animationTimer is None and selector.animating is False
    selector.dispose()

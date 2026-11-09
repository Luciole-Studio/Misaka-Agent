"""MISAKA regressions for the small TUI components, for behaviour upstream has no direct case for."""

from __future__ import annotations

import asyncio

from misaka.ui.tui.components.box import Box
from misaka.ui.tui.components.loader import Loader, LoaderIndicatorOptions
from misaka.ui.tui.components.settings_list import (
    SettingItem,
    SettingsList,
    SettingsListTheme,
)
from misaka.ui.tui.components.text import Text
from misaka.ui.tui.utils import visibleWidth


class _Ui:
    def __init__(self) -> None:
        self.renders = 0

    def requestRender(self, _force: bool = False) -> None:
        self.renders += 1


THEME = SettingsListTheme(
    label=lambda text, _selected: text,
    value=lambda text, _selected: text,
    description=lambda text: text,
    cursor="> ",
    hint=lambda text: text,
)


def test_text_narrows_its_margins_to_fit_the_width():
    assert [visibleWidth(line) for line in Text("x", 1, 0).render(2)] == [2]
    assert [visibleWidth(line) for line in Text("abc", 2, 0).render(3)] == [3, 3, 3]


def test_set_padding_x_rerenders_text_and_box():
    text = Text("hi", 1, 0)
    assert text.render(10) == [" hi       "]
    text.setPaddingX(3)
    assert text.render(10) == ["   hi     "]

    box = Box(1, 0)
    box.addChild(Text("hi", 0, 0))
    assert box.render(6) == [" hi   "]
    box.setPaddingX(2)
    assert box.render(6) == ["  hi  "]


def test_box_remove_child_and_clear_follow_identity_and_rebinding():
    box = Box()
    first, second = Text("a"), Text("b")
    box.addChild(first)
    box.addChild(second)
    held = box.children
    box.removeChild(Text("a"))
    assert box.children == [first, second]
    box.clear()
    assert box.children == [] and held == [first, second]


def test_loader_invalidate_recolours_and_requests_a_render():
    ui = _Ui()
    colour = {"tag": "a"}
    loader = Loader(ui, lambda text: text, lambda text: f"<{colour['tag']}>{text}", "msg", LoaderIndicatorOptions(frames=[]))
    try:
        assert loader.text == "<a>msg"
        colour["tag"] = "b"
        before = ui.renders
        loader.invalidate()
        assert loader.text == "<b>msg" and ui.renders == before + 1
    finally:
        loader.stop()


def test_loader_tests_the_rendered_indicator_not_the_raw_frame():
    ui = _Ui()
    hidden = Loader(ui, lambda _text: "", lambda text: text, "m")
    coloured = Loader(ui, lambda text: f"<>{text}", lambda text: text, "m", None)
    coloured.setIndicator(LoaderIndicatorOptions(frames=[""]))
    try:
        assert hidden.text == "m"
        assert coloured.text == "m"
        coloured.renderIndicatorVerbatim = False
        coloured.setMessage("m")
        assert coloured.text == "<> m"
    finally:
        hidden.stop()
        coloured.stop()


async def test_loader_ticks_on_the_running_event_loop():
    ui = _Ui()
    loader = Loader(ui, lambda text: text, lambda text: text, "m", LoaderIndicatorOptions(frames=["a", "b"], intervalMs=1))
    try:
        assert isinstance(loader.intervalId, asyncio.TimerHandle)
        await asyncio.sleep(0.05)
        assert loader.currentFrame in (0, 1) and ui.renders > 2
    finally:
        loader.stop()
    assert loader.intervalId is None


def test_settings_list_navigate_to_opens_the_target_submenu_after_close():
    opened: list[str] = []
    dones = {}

    class _Submenu:
        def render(self, _width: int) -> list[str]:
            return ["submenu"]

        def invalidate(self) -> None:
            pass

    def submenu(itemId):
        def factory(_value, done):
            opened.append(itemId)
            dones[itemId] = done
            return _Submenu()

        return factory

    settings = SettingsList(
        [
            SettingItem(id="a", label="A", currentValue="1", submenu=submenu("a")),
            SettingItem(id="b", label="B", currentValue="2", submenu=submenu("b")),
        ],
        10,
        THEME,
        lambda _id, _value: None,
        lambda: None,
    )
    settings.handleInput("\r")
    assert opened == ["a"]
    dones["a"](None, {"navigateTo": "b"})
    assert opened == ["a", "b"] and settings.selectedIndex == 1
    dones["b"]()
    assert settings.submenuComponent is None and settings.selectedIndex == 1


def test_settings_list_aligns_labels_up_to_36_columns():
    label = "x" * 34
    settings = SettingsList(
        [SettingItem(id="a", label=label, currentValue="v"), SettingItem(id="b", label="y", currentValue="w")],
        10,
        THEME,
        lambda _id, _value: None,
        lambda: None,
    )
    lines = settings.render(80)
    assert lines[0] == f"> {label}  v"
    assert lines[1] == "  y" + " " * 33 + "  w"

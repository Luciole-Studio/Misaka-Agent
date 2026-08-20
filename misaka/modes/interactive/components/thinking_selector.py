"""Bordered selector for interactive thinking-level changes."""

from __future__ import annotations

from collections.abc import Callable

from misaka.agent.types import ThinkingLevel
from misaka.tui import Container, SelectItem, SelectList, SelectListLayoutOptions

from misaka.modes.interactive.components.dynamic_border import DynamicBorder
from misaka.modes.interactive.theme.theme import get_select_list_theme

THINKING_SELECT_LIST_LAYOUT = SelectListLayoutOptions(minPrimaryColumnWidth=12, maxPrimaryColumnWidth=32)

# budget 线（thinking.budget_tokens）：token 数是真的——1024/2048/8192/16384。
# xhigh 上游原文写 ~32k，但 ThinkingBudgets 没有 xhigh 档，实际 clamp 到 high。
LEVEL_DESCRIPTIONS: dict[ThinkingLevel, str] = {
    "off": "No reasoning",
    "minimal": "Very brief reasoning (~1k tokens)",
    "low": "Light reasoning (~2k tokens)",
    "medium": "Moderate reasoning (~8k tokens)",
    "high": "Deep reasoning (~16k tokens)",
    "xhigh": "Maximum reasoning (budget clamps to high, ~16k)",
}

# adaptive 线（forceAdaptiveThinking 的模型，如 claude-5 系）：线上发的是
# output_config.effort 关键字，不发 budget——token 数描述在这条线上是假的，
# 所以单列一张诚实的表（minimal/low 同映射 effort=low，如实标注）。
ADAPTIVE_LEVEL_DESCRIPTIONS: dict[ThinkingLevel, str] = {
    "off": "No reasoning",
    "minimal": 'Adaptive effort "low" (same as low)',
    "low": 'Adaptive effort "low"',
    "medium": 'Adaptive effort "medium"',
    "high": 'Adaptive effort "high"',
    "xhigh": "Adaptive effort from model map",
}


class ThinkingSelectorComponent(Container):
    def __init__(
        self,
        currentLevel: ThinkingLevel,
        availableLevels: list[ThinkingLevel],
        onSelect: Callable[[ThinkingLevel], None],
        onCancel: Callable[[], None],
        descriptions: dict[ThinkingLevel, str] | None = None,
    ) -> None:
        super().__init__()

        descriptions = descriptions or LEVEL_DESCRIPTIONS
        items = [
            SelectItem(value=level, label=level, description=descriptions[level])
            for level in availableLevels
        ]

        self.addChild(DynamicBorder())
        self.selectList = SelectList(items, len(items), get_select_list_theme(), THINKING_SELECT_LIST_LAYOUT)
        current_index = next((index for index, item in enumerate(items) if item.value == currentLevel), -1)
        if current_index >= 0:
            self.selectList.setSelectedIndex(current_index)
        self.selectList.onSelect = lambda item: onSelect(item.value)
        self.selectList.onCancel = onCancel
        self.addChild(self.selectList)
        self.addChild(DynamicBorder())

    def handleInput(self, data: str) -> None:
        self.selectList.handleInput(data)

    def getSelectList(self) -> SelectList:
        return self.selectList


__all__ = [
    "ThinkingSelectorComponent",
]


if __name__ == "__main__":
    assert set(ADAPTIVE_LEVEL_DESCRIPTIONS) == set(LEVEL_DESCRIPTIONS), \
        "两张描述表必须同键——缺键的档位会让选择器开屏 KeyError"
    assert all("tokens" not in v for v in ADAPTIVE_LEVEL_DESCRIPTIONS.values()), \
        "adaptive 线不发 budget，描述里不许再出现 token 数"
    assert "clamp" in LEVEL_DESCRIPTIONS["xhigh"], "budget 线 xhigh 实为 clamp 到 high，文案要如实"
    print("thinking_selector selfcheck ok — 双表同键＋adaptive 无假 token 数＋xhigh 如实")

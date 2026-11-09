"""Translated from pi-tui's ``test/test-themes.ts`` (v1.1.0): default themes for TUI tests using chalk.

MISAKA fork: ``chalk`` (level 3) is ``misaka.utils.chalk``.
"""

from __future__ import annotations

from collections.abc import Callable

from misaka.ui.tui.components.editor import EditorTheme
from misaka.ui.tui.components.markdown import MarkdownTheme
from misaka.ui.tui.components.select_list import SelectListTheme
from misaka.utils.chalk import Chalk, chalk


def _styled(style: Chalk) -> Callable[[str], str]:
    return lambda text: style(text)


defaultSelectListTheme = SelectListTheme(
    selectedPrefix=_styled(chalk.blue),
    selectedText=_styled(chalk.bold),
    description=_styled(chalk.dim),
    scrollInfo=_styled(chalk.dim),
    noMatch=_styled(chalk.dim),
)

defaultMarkdownTheme = MarkdownTheme(
    heading=_styled(chalk.bold.cyan),
    link=_styled(chalk.blue),
    linkUrl=_styled(chalk.dim),
    code=_styled(chalk.yellow),
    codeBlock=_styled(chalk.green),
    codeBlockBorder=_styled(chalk.dim),
    quote=_styled(chalk.italic),
    quoteBorder=_styled(chalk.dim),
    hr=_styled(chalk.dim),
    listBullet=_styled(chalk.cyan),
    bold=_styled(chalk.bold),
    italic=_styled(chalk.italic),
    strikethrough=_styled(chalk.strikethrough),
    underline=_styled(chalk.underline),
)

defaultEditorTheme = EditorTheme(
    borderColor=_styled(chalk.dim),
    selectList=defaultSelectListTheme,
)

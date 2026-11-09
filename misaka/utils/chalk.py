"""The part of chalk 5 (https://github.com/chalk/chalk, MIT) that pi's interactive mode calls: chained styles
(``chalk.bold.cyan(text)``) and chalk's ``applyStyle``. A style closes before each line break and reopens after
it, and reopens after an inner close code of the same style, so nested styles survive.

MISAKA fork: no color-level detection. chalk's default instance asks supports-color and returns plain text at
level 0; MISAKA's TUI always draws into a color terminal, so styles are always applied.
"""

from __future__ import annotations

# ansi-styles: name -> (open, close) SGR parameters.
_STYLES: dict[str, tuple[int, int]] = {
    "reset": (0, 0),
    "bold": (1, 22),
    "dim": (2, 22),
    "italic": (3, 23),
    "underline": (4, 24),
    "overline": (53, 55),
    "inverse": (7, 27),
    "hidden": (8, 28),
    "strikethrough": (9, 29),
    "black": (30, 39),
    "red": (31, 39),
    "green": (32, 39),
    "yellow": (33, 39),
    "blue": (34, 39),
    "magenta": (35, 39),
    "cyan": (36, 39),
    "white": (37, 39),
    "blackBright": (90, 39),
    "gray": (90, 39),
    "grey": (90, 39),
    "redBright": (91, 39),
    "greenBright": (92, 39),
    "yellowBright": (93, 39),
    "blueBright": (94, 39),
    "magentaBright": (95, 39),
    "cyanBright": (96, 39),
    "whiteBright": (97, 39),
    "bgBlack": (40, 49),
    "bgRed": (41, 49),
    "bgGreen": (42, 49),
    "bgYellow": (43, 49),
    "bgBlue": (44, 49),
    "bgMagenta": (45, 49),
    "bgCyan": (46, 49),
    "bgWhite": (47, 49),
    "bgBlackBright": (100, 49),
    "bgGray": (100, 49),
    "bgGrey": (100, 49),
    "bgRedBright": (101, 49),
    "bgGreenBright": (102, 49),
    "bgYellowBright": (103, 49),
    "bgBlueBright": (104, 49),
    "bgMagentaBright": (105, 49),
    "bgCyanBright": (106, 49),
    "bgWhiteBright": (107, 49),
}


def _stringReplaceAll(string: str, substring: str, replacer: str) -> str:
    index = string.find(substring)
    if index == -1:
        return string

    substringLength = len(substring)
    endIndex = 0
    returnValue = ""
    while True:
        returnValue += string[endIndex:index] + substring + replacer
        endIndex = index + substringLength
        index = string.find(substring, endIndex)
        if index == -1:
            break

    returnValue += string[endIndex:]
    return returnValue


def _stringEncaseCRLFWithFirstIndex(string: str, prefix: str, postfix: str, index: int) -> str:
    endIndex = 0
    returnValue = ""
    while True:
        gotCR = index > 0 and string[index - 1] == "\r"
        returnValue += string[endIndex : (index - 1 if gotCR else index)] + prefix + ("\r\n" if gotCR else "\n") + postfix
        endIndex = index + 1
        index = string.find("\n", endIndex)
        if index == -1:
            break

    returnValue += string[endIndex:]
    return returnValue


class Chalk:
    """A chain of styles: ``chalk.bold.cyan(text)`` applies bold, then cyan inside it."""

    def __init__(self, stylers: tuple[tuple[str, str], ...] = ()) -> None:
        self._stylers = stylers

    def __getattr__(self, name: str) -> Chalk:
        style = _STYLES.get(name)
        if style is None:
            raise AttributeError(name)
        open_, close = style
        return Chalk((*self._stylers, (f"\x1b[{open_}m", f"\x1b[{close}m")))

    def __call__(self, text: str) -> str:
        """chalk's ``applyStyle``."""
        if not text:
            return text
        if not self._stylers:
            return text

        openAll = "".join(open_ for open_, _close in self._stylers)
        closeAll = "".join(close for _open, close in reversed(self._stylers))
        if "\x1b" in text:
            # Replace any instances already present with a re-opening code, otherwise only the part of the
            # string until said closing code will be colored, and the rest will simply be 'plain'.
            for open_, close in reversed(self._stylers):
                text = _stringReplaceAll(text, close, open_)

        # Close the styling before a linebreak and reopen after next line to fix a bleed issue on macOS.
        lfIndex = text.find("\n")
        if lfIndex != -1:
            text = _stringEncaseCRLFWithFirstIndex(text, closeAll, openAll, lfIndex)

        return openAll + text + closeAll


chalk = Chalk()

__all__ = ["Chalk", "chalk"]

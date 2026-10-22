"""Translated from pi's ``modes/interactive/theme/theme.ts`` (v1.3.0).

MISAKA forks, each marked where it happens:

- No system theme (D11): ``SYSTEM_THEME_NAME``, ``createSystemTheme``, faint ("dim") tokens, the terminal color
  query state (``setTerminalColors``, ``setTerminalColorScheme``, ``markTerminalColorsPending``) and ``okhsl()``/
  ``oklch()`` values are not ported. Where upstream falls back to or defaults to the system theme, MISAKA uses the
  built-in theme matching the terminal (``getTerminalTheme()``), and ``MISAKA_THEME`` (set by the panel for its panes)
  pins that choice.
- ``theme`` is a real object, loaded at import, and ``setGlobalTheme`` copies a new theme into it: components hold
  ``theme`` directly, so rebinding the module name alone would leave them on the old palette.
- Unknown tokens raise ``KeyError`` (upstream ``Error``) with the upstream message.
- ``THEME_TOKENS`` (``theme_tokens.py``) also has MISAKA's optional ``sectionTitle``, ``appTitle`` and
  ``appTitleOnLight`` tokens.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, NotRequired, TypedDict, cast

from misaka.agent.types import ThinkingLevel
from misaka.config import get_custom_themes_dir, get_themes_dir
from misaka.core.source_info import SourceInfo
from misaka.ui.tui.colors import (
    Color,
    TerminalColorMode,
    TextAttributes,
    backgroundAnsi,
    colorToHex,
    colorToOklch,
    foregroundAnsi,
    indexedColor,
    parseColor,
    rgbColor,
    styleTextWithAnsi,
)
from misaka.ui.tui.components.editor import EditorTheme
from misaka.ui.tui.components.markdown import MarkdownTheme
from misaka.ui.tui.components.select_list import SelectListTheme
from misaka.ui.tui.components.settings_list import SettingsListTheme
from misaka.ui.tui.terminal_colors import RgbColor, TerminalColors
from misaka.ui.tui.terminal_image import getTerminalColorMode
from misaka.utils.chalk import chalk
from misaka.utils.collation import localeCompare
from misaka.utils.fs_watch import FSWatcher, closeWatcher, watchWithErrorHandler
from misaka.utils.js import Timeout, clearTimeout, jsonParse, jsTrim, setTimeout
from misaka.utils.js_regexp import RegExp
from misaka.utils.syntax_highlight import highlight, supportsLanguage

# The schema that validates this shape lives in `theme_schema.py`; importing the type is free.
from .theme_schema import ThemeColorValue as ColorValue
from .theme_schema import ValidatedThemeJson as ThemeJson
from .theme_tokens import (
    THEME_TOKENS,
    ResolvedThemeColorValues,
    ThemeBg,
    ThemeColor,
    ThemeColorValues,
    ThemeToken,
)

# ============================================================================
# Types & Schema
# ============================================================================


class ThemeStyle(TextAttributes, total=False):
    """Tokens are only accepted in their own slot, because "" (terminal default) means the default foreground
    or background depending on the slot. Use ``theme.colors[token]`` to use a token's color in the other slot.
    """

    fg: ThemeColor | Color
    bg: ThemeBg | Color


type ThemeJsonValidator = Callable[[str, Any], ThemeJson]

themeJsonValidator: ThemeJsonValidator | None = None


def setThemeJsonValidator(validator: ThemeJsonValidator) -> None:
    """Install full theme validation. Without it, documents are accepted as-is, which is what built-in
    themes already do: validating user-authored JSON needs typebox, and a presentation that only uses
    built-in themes should not pay ~17 MB of module graph for it.
    """
    global themeJsonValidator
    themeJsonValidator = validator


# ============================================================================
# Color Utilities
# ============================================================================


def resolveVarRefs(
    value: ColorValue,
    vars: Mapping[str, ColorValue],
    visited: set[str] | None = None,
) -> str | int:
    if visited is None:
        visited = set()
    # MISAKA fork: no oklch() / okhsl() values (D11).
    if not isinstance(value, str) or value == "" or value.startswith("#"):
        return value
    if value in visited:
        raise ValueError(f"Circular variable reference detected: {value}")
    if value not in vars:
        raise ValueError(f"Variable reference not found: {value}")
    visited.add(value)
    return resolveVarRefs(vars[value], vars, visited)


def resolveThemeColors(
    colors: Mapping[str, ColorValue],
    vars: Mapping[str, ColorValue] | None = None,
) -> dict[str, str | int]:
    if vars is None:
        vars = {}
    resolved: dict[str, str | int] = {}
    for key, value in colors.items():
        resolved[key] = resolveVarRefs(value, vars)
    return resolved


def withThemeTokenFallbacks[Value](values: ThemeColorValues[Value]) -> ResolvedThemeColorValues[Value]:
    resolved: dict[str, Value] = {**values}
    for name, descriptor in THEME_TOKENS.items():
        if "fallback" not in descriptor:
            continue
        if name in resolved:
            continue
        fallback = descriptor["fallback"]
        if fallback not in resolved:
            raise ValueError(f"Theme token {name} has unresolved fallback {descriptor['fallback']}")
        resolved[name] = resolved[fallback]
    return cast(ResolvedThemeColorValues[Value], resolved)


def _nullish(value: Any, fallback: Any) -> Any:
    """MISAKA fork: ``value ?? fallback``."""
    return fallback if value is None else value

# The background a theme is designed for.
type ThemeAppearance = TerminalTheme

# The terminal's reported colors. Replaced (never mutated) on update, so themes can cache resolved colors
# by identity.
# MISAKA fork: never replaced. Its only writer, `setTerminalColors()`, serves the system theme (D11).
terminalColors: TerminalColors = {}


class _DefaultColors(TypedDict):
    foreground: Color
    background: Color


# Assumed terminal default colors when the terminal does not report them.
GUESSED_DEFAULT_COLORS: dict[ThemeAppearance, _DefaultColors] = {
    "dark": {"foreground": parseColor("#e5e5e7"), "background": parseColor("#000000")},
    "light": {"foreground": parseColor("#000000"), "background": parseColor("#ffffff")},
}


def averageLightness(colors: list[Color]) -> float | None:
    # Palette colors 0-15 follow the user's terminal palette, so they say nothing about the theme.
    fixed = [color for color in colors if color.kind != "indexed" or color.index >= 16]
    if len(fixed) == 0:
        return None
    return sum(colorToOklch(color).l for color in fixed) / len(fixed)


def detectAppearance(foregrounds: list[Color], backgrounds: list[Color]) -> ThemeAppearance | None:
    """Detect the background a theme is designed for from the lightness of its own colors."""
    fg = averageLightness(foregrounds)
    bg = averageLightness(backgrounds)
    if fg is not None and bg is not None:
        return "dark" if bg < fg else "light"
    if bg is not None:
        return "dark" if bg < 0.5 else "light"
    if fg is not None:
        return "dark" if fg > 0.5 else "light"
    return None


# ============================================================================
# Theme Class
# ============================================================================


class ThemeOptions(TypedDict, total=False):
    """MISAKA fork: a name for the inline type of ``Theme``'s ``options``. No ``dim`` (D11)."""

    name: str
    sourcePath: str
    sourceInfo: SourceInfo
    appearance: ThemeAppearance


class _ResolvedColors(TypedDict):
    terminal: TerminalColors
    colors: Mapping[ThemeToken, Color]


class Theme:
    def __init__(
        self,
        fgColors: Mapping[ThemeColor, str | int],
        bgColors: Mapping[ThemeBg, str | int],
        mode: TerminalColorMode,
        options: ThemeOptions | None = None,
    ) -> None:
        if options is None:
            options = {}
        self.name: str | None = options.get("name")
        self.sourcePath: str | None = options.get("sourcePath")
        self.sourceInfo: SourceInfo | None = options.get("sourceInfo")
        self.mode = mode
        # Precomputed escape sequences keep fg()/bg() on the render hot path to a lookup and concat.
        self.fgAnsi: dict[ThemeColor, str] = {}
        self.bgAnsi: dict[ThemeBg, str] = {}
        # Tokens set to "" have no color of their own; `colors` fills them from the terminal defaults.
        self.concreteColors: dict[ThemeToken, Color] = {}
        self.defaultForegroundTokens: list[ThemeToken] = []
        self.defaultBackgroundTokens: list[ThemeToken] = []
        self.resolvedColors: _ResolvedColors | None = None
        split = splitThemeColors(withThemeTokenFallbacks({**fgColors, **bgColors}))
        foregrounds = split["fgColors"]
        backgrounds = split["bgColors"]
        concreteForegrounds: list[Color] = []
        concreteBackgrounds: list[Color] = []

        # Returns the escape sequence for the token's own slot.
        def addToken(token: ThemeToken, value: str | int, isBackground: bool) -> str:
            if value == "":
                (self.defaultBackgroundTokens if isBackground else self.defaultForegroundTokens).append(token)
                return "\x1b[49m" if isBackground else "\x1b[39m"
            color = parseColor(value)
            self.concreteColors[token] = color
            (concreteBackgrounds if isBackground else concreteForegrounds).append(color)
            return backgroundAnsi(color, mode) if isBackground else foregroundAnsi(color, mode)

        for token, value in foregrounds.items():
            self.fgAnsi[token] = addToken(token, value, False)
        for bgToken, bgValue in backgrounds.items():
            self.bgAnsi[bgToken] = addToken(bgToken, bgValue, True)
        self.ownAppearance: ThemeAppearance | None = _nullish(
            options.get("appearance"), detectAppearance(concreteForegrounds, concreteBackgrounds)
        )

    @property
    def appearance(self) -> ThemeAppearance:
        """The background the theme is designed for: declared in the theme JSON, detected from its colors,
        or, for themes without usable colors, the terminal's appearance.
        """
        return _nullish(self.ownAppearance, getTerminalTheme())

    @property
    def colors(self) -> Mapping[ThemeToken, Color]:
        """Concrete colors for all tokens. Tokens set to "" (terminal default) use the terminal's reported
        default colors, or a guess based on ``appearance`` when the terminal did not report them.
        """
        terminal = terminalColors
        if self.resolvedColors is None or self.resolvedColors["terminal"] is not terminal:
            guess = GUESSED_DEFAULT_COLORS[self.appearance]

            def toColor(rgb: RgbColor | None, fallback: Color) -> Color:
                return rgbColor(rgb.r, rgb.g, rgb.b) if rgb else fallback

            foreground = toColor(terminal.get("foreground"), guess["foreground"])
            background = toColor(terminal.get("background"), guess["background"])
            colors = {**self.concreteColors}
            for token in self.defaultForegroundTokens:
                colors[token] = foreground
            for token in self.defaultBackgroundTokens:
                colors[token] = background
            # MISAKA fork: no faint tokens to mix toward the background (D11).
            self.resolvedColors = {"terminal": terminal, "colors": MappingProxyType(colors)}
        return self.resolvedColors["colors"]

    def style(self, text: str, options: ThemeStyle) -> str:
        fg = options.get("fg")
        bg = options.get("bg")
        # MISAKA fork: no faint tokens (D11).
        return styleTextWithAnsi(
            text,
            None
            if fg is None
            else self.tokenAnsi(self.fgAnsi, cast(ThemeColor, fg))
            if isinstance(fg, str)
            else foregroundAnsi(fg, self.mode),
            None
            if bg is None
            else self.tokenAnsi(self.bgAnsi, cast(ThemeBg, bg))
            if isinstance(bg, str)
            else backgroundAnsi(bg, self.mode),
            options,
        )

    def fg(self, color: ThemeColor, text: str) -> str:
        ansi = self.tokenAnsi(self.fgAnsi, color)
        return f"{ansi}{text}\x1b[39m"

    def bg(self, color: ThemeBg, text: str) -> str:
        ansi = self.tokenAnsi(self.bgAnsi, color)
        return f"{ansi}{text}\x1b[49m"

    def tokenAnsi[T: str](self, ansi: Mapping[T, str], token: T) -> str:
        value = ansi.get(token)
        if value is None:
            raise KeyError(f"Unknown theme color: {token}")
        return value

    def bold(self, text: str) -> str:
        return chalk.bold(text)

    def italic(self, text: str) -> str:
        return chalk.italic(text)

    def underline(self, text: str) -> str:
        return chalk.underline(text)

    def inverse(self, text: str) -> str:
        return chalk.inverse(text)

    def strikethrough(self, text: str) -> str:
        return chalk.strikethrough(text)

    def getFgAnsi(self, color: ThemeColor) -> str:
        """Opening escape sequence for a foreground token."""
        return self.tokenAnsi(self.fgAnsi, color)

    def getBgAnsi(self, color: ThemeBg) -> str:
        return self.tokenAnsi(self.bgAnsi, color)

    def getColorMode(self) -> TerminalColorMode:
        return self.mode

    def getThinkingBorderColor(self, level: ThinkingLevel) -> Callable[[str], str]:
        # Map thinking levels to dedicated theme colors
        match level:
            case "off":
                return lambda str: self.fg("thinkingOff", str)
            case "minimal":
                return lambda str: self.fg("thinkingMinimal", str)
            case "low":
                return lambda str: self.fg("thinkingLow", str)
            case "medium":
                return lambda str: self.fg("thinkingMedium", str)
            case "high":
                return lambda str: self.fg("thinkingHigh", str)
            case "xhigh":
                return lambda str: self.fg("thinkingXhigh", str)
            case "max":
                return lambda str: self.fg("thinkingMax", str)
            case _:
                return lambda str: self.fg("thinkingOff", str)

    def getBashModeBorderColor(self) -> Callable[[str], str]:
        return lambda str: self.fg("bashMode", str)


# ============================================================================
# Theme Loading
# ============================================================================

BUILTIN_THEMES: dict[str, ThemeJson] | None = None


def _readJsonFile(path: str) -> Any:
    """MISAKA fork: ``JSON.parse(stripBom(fs.readFileSync(path, "utf-8")))``."""
    return jsonParse(Path(path).read_text(encoding="utf-8", errors="replace").removeprefix("﻿"))


def getBuiltinThemes() -> dict[str, ThemeJson]:
    global BUILTIN_THEMES
    if not BUILTIN_THEMES:
        themesDir = get_themes_dir()
        darkPath = os.path.join(themesDir, "dark.json")
        lightPath = os.path.join(themesDir, "light.json")
        BUILTIN_THEMES = {
            "dark": cast(ThemeJson, _readJsonFile(darkPath)),
            "light": cast(ThemeJson, _readJsonFile(lightPath)),
        }
    return BUILTIN_THEMES


def getAvailableThemes() -> list[str]:
    return [themeInfo["name"] for themeInfo in getAvailableThemesWithPaths()]


class ThemeInfo(TypedDict):
    name: str
    path: str | None


def getAvailableThemesWithPaths() -> list[ThemeInfo]:
    themesDir = get_themes_dir()
    result: list[ThemeInfo] = []
    seen: set[str] = set()

    def addTheme(themeInfo: ThemeInfo) -> None:
        if themeInfo["name"] in seen:
            return
        seen.add(themeInfo["name"])
        result.append(themeInfo)

    # Built-in themes.
    # MISAKA fork: no system theme (D11).
    for name in getBuiltinThemes():
        addTheme({"name": name, "path": os.path.join(themesDir, f"{name}.json")})

    # Custom themes
    for themeInfo in getCustomThemeInfos():
        addTheme(themeInfo)

    for name, registered in registeredThemes.items():
        addTheme({"name": name, "path": registered.sourcePath})

    def compare(a: ThemeInfo, b: ThemeInfo) -> int:
        return localeCompare(a["name"], b["name"])

    return sorted(result, key=functools.cmp_to_key(compare))


def getCustomThemeInfos() -> list[ThemeInfo]:
    customThemesDir = get_custom_themes_dir()
    result: list[ThemeInfo] = []
    if not os.path.exists(customThemesDir):
        return result

    for file in os.listdir(customThemesDir):
        if not file.endswith(".json"):
            continue
        themePath = os.path.join(customThemesDir, file)
        try:
            customTheme = loadThemeFromPath(themePath)
            if customTheme.name:
                result.append({"name": customTheme.name, "path": themePath})
        except Exception:  # noqa: BLE001, S112 - invalid themes are ignored here; the resource loader reports them during normal startup/reload
            continue
    return result


def assertThemeNameIsValid(name: str) -> None:
    if "/" in name:
        raise ValueError(
            f'Invalid theme name "{name}": theme names cannot contain "/" because it is reserved for automatic '
            "light/dark theme settings."
        )


def parseThemeJson(label: str, json: Any) -> ThemeJson:
    if themeJsonValidator:
        return themeJsonValidator(label, json)
    if not isinstance(json, dict) or "colors" not in json:
        raise ValueError(f'Invalid theme "{label}": expected an object with a "colors" map.')
    return cast(ThemeJson, json)


def parseThemeJsonContent(label: str, content: str) -> ThemeJson:
    try:
        json = jsonParse(content.removeprefix("﻿"))
    except Exception as error:
        raise ValueError(f"Failed to parse theme {label}: {error}") from error
    return parseThemeJson(label, json)


def loadThemeJson(name: str) -> ThemeJson:
    builtinThemes = getBuiltinThemes()
    if name in builtinThemes:
        return builtinThemes[name]
    registeredTheme = registeredThemes.get(name)
    if registeredTheme is not None and registeredTheme.sourcePath:
        content = Path(registeredTheme.sourcePath).read_text(encoding="utf-8", errors="replace")
        return parseThemeJsonContent(registeredTheme.sourcePath, content)
    if registeredTheme is not None:
        raise ValueError(f'Theme "{name}" does not have a source path for export')
    customThemesDir = get_custom_themes_dir()
    themePath = os.path.join(customThemesDir, f"{name}.json")
    if not os.path.exists(themePath):
        raise ValueError(f"Theme not found: {name}")
    content = Path(themePath).read_text(encoding="utf-8", errors="replace")
    return parseThemeJsonContent(name, content)


class _SplitThemeColors(TypedDict):
    fgColors: dict[ThemeColor, str | int]
    bgColors: dict[ThemeBg, str | int]


def splitThemeColors(colors: ResolvedThemeColorValues[str | int]) -> _SplitThemeColors:
    fgColors: dict[ThemeColor, str | int] = {}
    bgColors: dict[ThemeBg, str | int] = {}
    for key, value in colors.items():
        if THEME_TOKENS[key]["slot"] == "background":
            bgColors[cast(ThemeBg, key)] = value
        else:
            fgColors[cast(ThemeColor, key)] = value
    return {"fgColors": fgColors, "bgColors": bgColors}


def createTheme(themeJson: ThemeJson, mode: TerminalColorMode | None = None, sourcePath: str | None = None) -> Theme:
    colorMode = _nullish(mode, getTerminalColorMode())
    resolvedColors = withThemeTokenFallbacks(resolveThemeColors(themeJson["colors"], themeJson.get("vars")))
    split = splitThemeColors(resolvedColors)
    options: ThemeOptions = {"name": themeJson["name"]}
    if sourcePath is not None:
        options["sourcePath"] = sourcePath
    appearance = themeJson.get("appearance")
    if appearance is not None:
        options["appearance"] = appearance
    return Theme(split["fgColors"], split["bgColors"], colorMode, options)


def loadThemeFromPath(themePath: str, mode: TerminalColorMode | None = None) -> Theme:
    content = Path(themePath).read_text(encoding="utf-8", errors="replace")
    themeJson = parseThemeJsonContent(themePath, content)
    return createTheme(themeJson, mode, themePath)


def loadTheme(name: str, mode: TerminalColorMode | None = None) -> Theme:
    # MISAKA fork: no system theme (D11).
    registeredTheme = registeredThemes.get(name)
    if registeredTheme is not None:
        return registeredTheme
    themeJson = loadThemeJson(name)
    return createTheme(themeJson, mode)


def getThemeByName(name: str) -> Theme | None:
    try:
        return loadTheme(name)
    except Exception:  # noqa: BLE001 - no theme by that name
        return None


class _AutoThemeSetting(TypedDict):
    lightTheme: str
    darkTheme: str


# ============================================================================
# Appearance & Terminal Default Colors
# ============================================================================

type TerminalTheme = Literal["dark", "light"]


def parseAutoThemeSetting(themeSetting: str | None) -> _AutoThemeSetting | None:
    if not themeSetting:
        return None
    slashIndex = themeSetting.find("/")
    if slashIndex == -1 or themeSetting.find("/", slashIndex + 1) != -1:
        return None

    lightTheme = jsTrim(themeSetting[0:slashIndex])
    darkTheme = jsTrim(themeSetting[slashIndex + 1 :])
    if not lightTheme or not darkTheme:
        return None
    return {"lightTheme": lightTheme, "darkTheme": darkTheme}


def resolveThemeSetting(themeSetting: str | None, terminalTheme: TerminalTheme) -> str | None:
    autoTheme = parseAutoThemeSetting(themeSetting)
    if autoTheme:
        return autoTheme["lightTheme"] if terminalTheme == "light" else autoTheme["darkTheme"]
    if themeSetting is not None and "/" in themeSetting:
        return None
    if isinstance(themeSetting, str):
        return themeSetting
    return None


_COLORFGBG_INDEX = RegExp(r"^\d{1,2}$")


def detectColorFgBgTheme(env: Mapping[str, str] | None = None) -> TerminalTheme | None:
    """Dark or light from the ``COLORFGBG`` environment variable some terminals set, or None without a
    usable background index. The value is ``fg;bg`` or ``fg;xpm;bg`` (rxvt), where a field is an ANSI color
    index or ``default`` when the color is not in the palette. The index refers to the terminal's own palette,
    whose colors are unknown here, so it is classified by index like Vim does: 0-6 and 8 (bright black, e.g.
    Solarized Dark's background) are dark, 7 and 9-15 are light.
    """
    if env is None:
        env = os.environ
    colorFgBg = env.get("COLORFGBG")
    bg = jsTrim(colorFgBg.split(";")[-1]) if colorFgBg is not None else None
    if not bg or not _COLORFGBG_INDEX.test(bg):
        return None
    index = int(bg)
    if index > 15:
        return None
    return "dark" if index <= 6 or index == 8 else "light"


def detectTerminalTheme(
    colors: TerminalColors | None = None,
    reportedScheme: TerminalTheme | None = None,
    env: Mapping[str, str] | None = None,
) -> TerminalTheme:
    """Whether the terminal is dark or light: the terminal's light/dark report, then COLORFGBG, then dark.

    MISAKA forks: ``MISAKA_THEME`` (``dark``/``light``, set by the panel for its panes) decides first. The terminal's
    reported background, which decides first upstream, is never reported here (D11).
    """
    if env is None:
        env = os.environ
    pinned = env.get("MISAKA_THEME")
    if pinned == "dark":
        return "dark"
    if pinned == "light":
        return "light"
    return _nullish(reportedScheme, _nullish(detectColorFgBgTheme(env), "dark"))


def getTerminalTheme() -> TerminalTheme:
    """Whether the terminal is dark or light, from everything it reported so far. See ``detectTerminalTheme()``."""
    # MISAKA fork: no mode-2031 report is recorded (D11).
    return detectTerminalTheme(terminalColors, None)


# ============================================================================
# Global Theme Instance
# ============================================================================


def setGlobalTheme(t: Theme) -> None:
    # MISAKA fork: copy the new theme into the shared object (see the module docstring).
    global theme
    prior = globals().get("theme")
    if isinstance(prior, Theme) and prior is not t:
        prior.__dict__.update(t.__dict__)
        return
    theme = t


currentThemeName: str | None = None
themeWatcher: FSWatcher | None = None
themeReloadTimer: Timeout | None = None
onThemeChangeCallback: Callable[[], None] | None = None
registeredThemes: dict[str, Theme] = {}


def setRegisteredThemes(themes: list[Theme]) -> None:
    registeredThemes.clear()
    for t in themes:
        if t.name:
            assertThemeNameIsValid(t.name)
            registeredThemes[t.name] = t


def _loadFallbackTheme(error: Exception) -> None:
    """MISAKA fork: the fallback is the built-in theme matching the terminal (D11), which is a file and can fail to
    load, unlike the generated system theme. Say where it was looked for."""
    global currentThemeName
    currentThemeName = getTerminalTheme()
    try:
        setGlobalTheme(loadTheme(currentThemeName))
    except Exception as fallbackError:
        raise RuntimeError(
            f"Cannot load the built-in '{currentThemeName}' theme from {get_themes_dir()} "
            f"(requested theme failed first: {error})"
        ) from fallbackError


def initTheme(themeName: str | None = None, enableWatcher: bool = False) -> None:
    global currentThemeName
    # MISAKA fork: the default is the built-in theme matching the terminal (D11).
    name = _nullish(themeName, getTerminalTheme())
    currentThemeName = name
    try:
        setGlobalTheme(loadTheme(name))
        if enableWatcher:
            startThemeWatcher()
    except Exception as error:  # noqa: BLE001 - an invalid theme falls back silently
        # Theme is invalid - fall back to the default theme silently
        _loadFallbackTheme(error)
        # Don't start watcher for fallback theme


class SetThemeResult(TypedDict):
    """MISAKA fork: a name for the inline result type of ``setTheme``."""

    success: bool
    error: NotRequired[str]


def setTheme(name: str, enableWatcher: bool = False) -> SetThemeResult:
    global currentThemeName
    currentThemeName = name
    try:
        setGlobalTheme(loadTheme(name))
        if enableWatcher:
            startThemeWatcher()
        if onThemeChangeCallback:
            onThemeChangeCallback()
        return {"success": True}
    except Exception as error:  # noqa: BLE001 - an invalid theme falls back
        # Theme is invalid - fall back to the default theme
        _loadFallbackTheme(error)
        # Don't start watcher for fallback theme
        return {"success": False, "error": str(error)}


def setThemeInstance(themeInstance: Theme) -> None:
    global currentThemeName
    setGlobalTheme(themeInstance)
    currentThemeName = "<in-memory>"
    stopThemeWatcher()  # Can't watch a direct instance
    if onThemeChangeCallback:
        onThemeChangeCallback()


def onThemeChange(callback: Callable[[], None]) -> None:
    global onThemeChangeCallback
    onThemeChangeCallback = callback


def startThemeWatcher() -> None:
    global themeWatcher
    stopThemeWatcher()

    # Only watch if it's a custom theme (not built-in)
    # MISAKA fork: there is no system theme to skip (D11).
    if not currentThemeName or currentThemeName == "dark" or currentThemeName == "light":
        return

    customThemesDir = get_custom_themes_dir()
    watchedThemeName = currentThemeName
    watchedFileName = f"{watchedThemeName}.json"
    themeFile = os.path.join(customThemesDir, watchedFileName)

    # Only watch if the file exists
    if not os.path.exists(themeFile):
        return

    def scheduleReload() -> None:
        global themeReloadTimer
        if themeReloadTimer:
            clearTimeout(themeReloadTimer)

        def reload() -> None:
            global themeReloadTimer
            themeReloadTimer = None

            # Ignore stale timers after switching themes or stopping the watcher
            if currentThemeName != watchedThemeName:
                return

            # Keep the last successfully loaded theme active if the file is temporarily missing
            if not os.path.exists(themeFile):
                return

            try:
                # Reload the theme from disk and refresh the registry cache
                reloadedTheme = loadThemeFromPath(themeFile)
                registeredThemes[watchedThemeName] = reloadedTheme
                setGlobalTheme(reloadedTheme)
                # Notify callback (to invalidate UI)
                if onThemeChangeCallback:
                    onThemeChangeCallback()
            except Exception:  # noqa: BLE001, S110 - the file might be in an invalid state while being edited
                pass

        themeReloadTimer = setTimeout(reload, 100)

    def onChange(_eventType: str, filename: str | None) -> None:
        if currentThemeName != watchedThemeName:
            return
        if not filename:
            scheduleReload()
            return
        if filename != watchedFileName:
            return
        scheduleReload()

    def onError() -> None:
        global themeWatcher
        closeWatcher(themeWatcher)
        themeWatcher = None

    themeWatcher = watchWithErrorHandler(customThemesDir, onChange, onError)


def stopThemeWatcher() -> None:
    global themeWatcher, themeReloadTimer
    if themeReloadTimer:
        clearTimeout(themeReloadTimer)
        themeReloadTimer = None
    closeWatcher(themeWatcher)
    themeWatcher = None


# ============================================================================
# HTML Export Helpers
# ============================================================================


def getResolvedThemeColors(themeName: str | None = None) -> dict[str, str]:
    """Get resolved theme colors as CSS-compatible hex strings.
    Used by HTML export to generate CSS custom properties.
    """
    colors = loadTheme(_nullish(themeName, _nullish(currentThemeName, getTerminalTheme()))).colors
    return {token: colorToHex(color) for token, color in colors.items()}


def isLightTheme(themeName: str | None = None) -> bool:
    """Check if a theme is a "light" theme (for CSS that needs light/dark variants)."""
    return loadTheme(_nullish(themeName, _nullish(currentThemeName, getTerminalTheme()))).appearance == "light"


class ThemeExportColors(TypedDict, total=False):
    pageBg: str | None
    cardBg: str | None
    infoBg: str | None


def getThemeExportColors(themeName: str | None = None) -> ThemeExportColors:
    """Get explicit export colors from theme JSON, if specified.
    Returns None for each color that isn't explicitly set.
    """
    name = _nullish(themeName, _nullish(currentThemeName, getTerminalTheme()))
    try:
        themeJson = loadThemeJson(name)
        exportSection = themeJson.get("export")
        if not exportSection:
            return {}

        vars = themeJson.get("vars") or {}

        # Export colors end up in CSS, which understands hex values directly.
        def resolve(value: ColorValue | None) -> str | None:
            if value is None:
                return None
            resolved = resolveVarRefs(value, vars)
            if isinstance(resolved, int):
                return colorToHex(indexedColor(resolved))
            if resolved == "":
                return None
            # MISAKA fork: no okhsl() values to convert (D11).
            return resolved

        return {
            "pageBg": resolve(exportSection.get("pageBg")),
            "cardBg": resolve(exportSection.get("cardBg")),
            "infoBg": resolve(exportSection.get("infoBg")),
        }
    except Exception:  # noqa: BLE001 - export colors are optional
        return {}


# ============================================================================
# TUI Helpers
# ============================================================================

type CliHighlightTheme = dict[str, Callable[[str], str]]

cachedHighlightThemeFor: Theme | None = None
cachedCliHighlightTheme: CliHighlightTheme | None = None


def buildCliHighlightTheme(t: Theme) -> CliHighlightTheme:
    return {
        "keyword": lambda s: t.fg("syntaxKeyword", s),
        "built_in": lambda s: t.fg("syntaxType", s),
        "literal": lambda s: t.fg("syntaxNumber", s),
        "number": lambda s: t.fg("syntaxNumber", s),
        "regexp": lambda s: t.fg("syntaxString", s),
        "string": lambda s: t.fg("syntaxString", s),
        "subst": lambda s: t.fg("text", s),
        "comment": lambda s: t.fg("syntaxComment", s),
        "doctag": lambda s: t.fg("syntaxComment", s),
        "meta": lambda s: t.fg("muted", s),
        "function": lambda s: t.fg("syntaxFunction", s),
        "title": lambda s: t.fg("syntaxFunction", s),
        "class": lambda s: t.fg("syntaxType", s),
        "type": lambda s: t.fg("syntaxType", s),
        "tag": lambda s: t.fg("syntaxPunctuation", s),
        "name": lambda s: t.fg("syntaxKeyword", s),
        "attr": lambda s: t.fg("syntaxVariable", s),
        "variable": lambda s: t.fg("syntaxVariable", s),
        "params": lambda s: t.fg("syntaxVariable", s),
        "operator": lambda s: t.fg("syntaxOperator", s),
        "punctuation": lambda s: t.fg("syntaxPunctuation", s),
        "emphasis": lambda s: t.italic(s),
        "strong": lambda s: t.bold(s),
        "link": lambda s: t.underline(s),
        "addition": lambda s: t.fg("toolDiffAdded", s),
        "deletion": lambda s: t.fg("toolDiffRemoved", s),
    }


def getCliHighlightTheme(t: Theme) -> CliHighlightTheme:
    global cachedHighlightThemeFor, cachedCliHighlightTheme
    if cachedHighlightThemeFor is not t or not cachedCliHighlightTheme:
        cachedHighlightThemeFor = t
        cachedCliHighlightTheme = buildCliHighlightTheme(t)
    return cachedCliHighlightTheme


def highlightCode(code: str, lang: str | None = None) -> list[str]:
    """Highlight code with syntax coloring based on file extension or language.
    Returns array of highlighted lines.
    """
    # Validate language before highlighting to avoid stderr spam from cli-highlight
    validLang = lang if lang and supportsLanguage(lang) else None
    # Skip highlighting when no valid language is specified. cli-highlight's
    # auto-detection is unreliable and can misidentify prose as AppleScript,
    # LiveCodeServer, etc., coloring random English words as keywords.
    if not validLang:
        return [theme.fg("mdCodeBlock", line) for line in code.split("\n")]
    opts = {
        "language": validLang,
        "ignoreIllegals": True,
        "theme": getCliHighlightTheme(theme),
    }
    try:
        return highlight(code, opts).split("\n")
    except Exception:  # noqa: BLE001 - highlighting is cosmetic; the plain code is shown
        return code.split("\n")


def getLanguageFromPath(filePath: str) -> str | None:
    """Get language identifier from file path extension."""
    ext = filePath.split(".")[-1].lower()
    if not ext:
        return None

    extToLang: dict[str, str] = {
        "ts": "typescript",
        "tsx": "typescript",
        "js": "javascript",
        "jsx": "javascript",
        "mjs": "javascript",
        "cjs": "javascript",
        "py": "python",
        "rb": "ruby",
        "rs": "rust",
        "go": "go",
        "java": "java",
        "kt": "kotlin",
        "swift": "swift",
        "c": "c",
        "h": "c",
        "cpp": "cpp",
        "cc": "cpp",
        "cxx": "cpp",
        "hpp": "cpp",
        "cs": "csharp",
        "php": "php",
        "sh": "bash",
        "bash": "bash",
        "zsh": "bash",
        "fish": "fish",
        "ps1": "powershell",
        "sql": "sql",
        "html": "html",
        "htm": "html",
        "css": "css",
        "scss": "scss",
        "sass": "sass",
        "less": "less",
        "json": "json",
        "yaml": "yaml",
        "yml": "yaml",
        "toml": "toml",
        "xml": "xml",
        "md": "markdown",
        "markdown": "markdown",
        "dockerfile": "dockerfile",
        "makefile": "makefile",
        "cmake": "cmake",
        "lua": "lua",
        "perl": "perl",
        "r": "r",
        "scala": "scala",
        "clj": "clojure",
        "ex": "elixir",
        "exs": "elixir",
        "erl": "erlang",
        "hs": "haskell",
        "ml": "ocaml",
        "vim": "vim",
        "graphql": "graphql",
        "proto": "protobuf",
        "tf": "hcl",
        "hcl": "hcl",
    }

    return extToLang.get(ext)


def getMarkdownTheme() -> MarkdownTheme:
    def highlightCode(code: str, lang: str | None = None) -> list[str]:
        # Validate language before highlighting to avoid stderr spam from cli-highlight
        validLang = lang if lang and supportsLanguage(lang) else None
        # Skip highlighting when no valid language is specified. cli-highlight's
        # auto-detection is unreliable and can misidentify prose as AppleScript,
        # LiveCodeServer, etc., coloring random English words as keywords.
        if not validLang:
            return [theme.fg("mdCodeBlock", line) for line in code.split("\n")]
        opts = {
            "language": validLang,
            "ignoreIllegals": True,
            "theme": getCliHighlightTheme(theme),
        }
        try:
            return highlight(code, opts).split("\n")
        except Exception:  # noqa: BLE001 - highlighting is cosmetic; the plain code is shown
            return [theme.fg("mdCodeBlock", line) for line in code.split("\n")]

    return MarkdownTheme(
        heading=lambda text: theme.fg("mdHeading", text),
        link=lambda text: theme.fg("mdLink", text),
        linkUrl=lambda text: theme.fg("mdLinkUrl", text),
        code=lambda text: theme.fg("mdCode", text),
        codeBlock=lambda text: theme.fg("mdCodeBlock", text),
        codeBlockBorder=lambda text: theme.fg("mdCodeBlockBorder", text),
        quote=lambda text: theme.fg("mdQuote", text),
        quoteBorder=lambda text: theme.fg("mdQuoteBorder", text),
        hr=lambda text: theme.fg("mdHr", text),
        listBullet=lambda text: theme.fg("mdListBullet", text),
        bold=lambda text: theme.bold(text),
        italic=lambda text: theme.italic(text),
        underline=lambda text: theme.underline(text),
        strikethrough=lambda text: theme.strikethrough(text),
        highlightCode=highlightCode,
    )


def getSelectListTheme() -> SelectListTheme:
    return SelectListTheme(
        selectedPrefix=lambda text: theme.fg("accent", text),
        selectedText=lambda text: theme.fg("accent", text),
        description=lambda text: theme.fg("muted", text),
        scrollInfo=lambda text: theme.fg("muted", text),
        noMatch=lambda text: theme.fg("muted", text),
    )


def getEditorTheme() -> EditorTheme:
    return EditorTheme(
        borderColor=lambda text: theme.fg("borderMuted", text),
        selectList=getSelectListTheme(),
    )


def getSettingsListTheme() -> SettingsListTheme:
    return SettingsListTheme(
        label=lambda text, selected: theme.fg("accent", text) if selected else text,
        value=lambda text, selected: theme.fg("accent", text) if selected else theme.fg("muted", text),
        description=lambda text: theme.fg("dim", text),
        cursor=theme.fg("accent", "→ "),
        hint=lambda text: theme.fg("dim", text),
    )


def _defaultTerminalTheme() -> Theme:
    """MISAKA fork: ``theme`` exists from import on (upstream throws "Theme not initialized" until ``initTheme()``).
    A broken built-in theme leaves every token at the terminal default."""
    try:
        return loadTheme(getTerminalTheme())
    except Exception:  # noqa: BLE001 - the module must import even with a broken built-in theme
        split = splitThemeColors(dict.fromkeys(THEME_TOKENS, ""))
        return Theme(split["fgColors"], split["bgColors"], "256color", {"name": "uninitialized"})


theme: Theme = _defaultTerminalTheme()
currentThemeName = theme.name

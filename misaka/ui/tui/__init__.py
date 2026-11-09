"""Translated from pi-tui's ``index.ts`` (v1.3.0): the package's public names.

MISAKA fork: not re-exported, because they are not ported: the alt screen and everything only it uses (D1:
``TuiAltScreen``, ``TuiMainScreenRenderState``, ``isViewportTUI``/``ViewportTUI``, ``TuiMode``, the ``TuiMouse*``
types, ``ScrollView``, ``HStack``/``VStack`` and the stack types, ``deleteAllKittyImages``, ``getOsc8LinkAtColumn``,
``WheelScrollLines``), and the OKLCH/OKHSL part of the colour library behind the system
theme (D11: ``colorToOkhsl``, ``OkhslChannels``, ``OklchColorValue``, ``okhslColor``, ``oklchColor`` and
``oklabToOkhslLightness``).
"""

# Autocomplete support
from misaka.ui.tui.autocomplete import (
    AutocompleteItem,
    AutocompleteProvider,
    AutocompleteSuggestions,
    CombinedAutocompleteProvider,
    SlashCommand,
)

# Colors and styling
from misaka.ui.tui.colors import (
    Color,
    ColorMixSpace,
    IndexedColor,
    OklchChannels,
    RgbColorValue,
    TerminalColorMode,
    TextAttributes,
    TextStyle,
    backgroundAnsi,
    colorToHex,
    colorToOklch,
    colorToRgb,
    foregroundAnsi,
    indexedColor,
    mixColors,
    parseColor,
    rgbColor,
    styleText,
    styleTextWithAnsi,
)

# Components
from misaka.ui.tui.components.box import Box
from misaka.ui.tui.components.cancellable_loader import CancellableLoader
from misaka.ui.tui.components.editor import Editor, EditorOptions, EditorTheme
from misaka.ui.tui.components.image import (
    Image,
    ImageOptions,
    ImageTheme,
    ImageTranscoder,
    setImageTranscoder,
)
from misaka.ui.tui.components.input import Input
from misaka.ui.tui.components.loader import Loader, LoaderIndicatorOptions
from misaka.ui.tui.components.markdown import (
    DefaultTextStyle,
    Markdown,
    MarkdownOptions,
    MarkdownTheme,
)
from misaka.ui.tui.components.mouse_region import MouseRegion, MouseRegionHandler
from misaka.ui.tui.components.select_list import (
    SelectItem,
    SelectList,
    SelectListLayoutOptions,
    SelectListTheme,
    SelectListTruncatePrimaryContext,
)
from misaka.ui.tui.components.settings_list import (
    SettingItem,
    SettingsList,
    SettingsListTheme,
)
from misaka.ui.tui.components.spacer import Spacer
from misaka.ui.tui.components.text import Text
from misaka.ui.tui.components.truncated_text import TruncatedText

# Editor component interface (for custom editors)
from misaka.ui.tui.editor_component import EditorComponent

# Fuzzy matching
from misaka.ui.tui.fuzzy import FuzzyMatch, fuzzyFilter, fuzzyMatch

# Keybindings
from misaka.ui.tui.keybindings import (
    TUI_KEYBINDINGS,
    Keybinding,
    KeybindingConflict,
    KeybindingDefinition,
    KeybindingDefinitions,
    Keybindings,
    KeybindingsConfig,
    KeybindingsManager,
    getKeybindings,
    setKeybindings,
)

# Keyboard input handling
from misaka.ui.tui.keys import (
    Key,
    KeyEventType,
    KeyId,
    decodeKittyPrintable,
    isKeyRelease,
    isKeyRepeat,
    isKittyProtocolActive,
    matchesKey,
    parseKey,
    setKittyProtocolActive,
)

# LaTeX rendering
from misaka.ui.tui.latex import RenderLatexOptions, renderLatex

# Native platform integration
from misaka.ui.tui.native_platform import NativeClipboard, getNativeClipboard

# Program status reporting (OSC 7501)
from misaka.ui.tui.program_status import ProgramStatus, formatProgramStatus

# Input buffering for batch splitting
from misaka.ui.tui.stdin_buffer import (
    StdinBuffer,
    StdinBufferEventMap,
    StdinBufferOptions,
)

# Terminal interface and implementations
from misaka.ui.tui.terminal import ProcessTerminal, Terminal, isAppleTerminalSession

# Terminal colors
from misaka.ui.tui.terminal_colors import (
    RgbColor,
    TerminalColors,
    TerminalColorScheme,
    parseTerminalColorSchemeReport,
)

# Terminal image support
from misaka.ui.tui.terminal_image import (
    CellDimensions,
    ImageDimensions,
    ImageProtocol,
    ImageRenderOptions,
    TerminalCapabilities,
    allocateImageId,
    calculateImageRows,
    deleteKittyImage,
    detectCapabilities,
    encodeITerm2,
    encodeKitty,
    getCapabilities,
    getCellDimensions,
    getGifDimensions,
    getImageDimensions,
    getJpegDimensions,
    getPngDimensions,
    getTerminalColorMode,
    getWebpDimensions,
    hyperlink,
    imageFallback,
    renderImage,
    resetCapabilitiesCache,
    setCapabilities,
    setCapabilityOverrides,
    setCellDimensions,
)
from misaka.ui.tui.tui import (
    CURSOR_MARKER,
    TUI,
    Component,
    Container,
    Focusable,
    OverlayAnchor,
    OverlayBounds,
    OverlayHandle,
    OverlayMargin,
    OverlayOptions,
    OverlayUnfocusOptions,
    SizeValue,
    TuiInputListener,
    TuiInputListenerResult,
    TuiStopOptions,
    compositeTuiLine,
    isFocusable,
    renderFakeCursor,
)
from misaka.ui.tui.tui_main_screen import TuiMainScreen

# Utilities
from misaka.ui.tui.utils import (
    sliceByColumn,
    stripTerminalSequences,
    truncateToWidth,
    visibleWidth,
    wrapTextWithAnsi,
)

# Core TUI interfaces and classes (marked, upstream's first export; isort places it last)
from misaka.ui.tui.vendor.marked import Marked, Token

__all__ = [
    "CURSOR_MARKER",
    "TUI",
    "TUI_KEYBINDINGS",
    "AutocompleteItem",
    "AutocompleteProvider",
    "AutocompleteSuggestions",
    "Box",
    "CancellableLoader",
    "CellDimensions",
    "Color",
    "ColorMixSpace",
    "CombinedAutocompleteProvider",
    "Component",
    "Container",
    "DefaultTextStyle",
    "Editor",
    "EditorComponent",
    "EditorOptions",
    "EditorTheme",
    "Focusable",
    "FuzzyMatch",
    "Image",
    "ImageDimensions",
    "ImageOptions",
    "ImageProtocol",
    "ImageRenderOptions",
    "ImageTheme",
    "ImageTranscoder",
    "IndexedColor",
    "Input",
    "Key",
    "KeyEventType",
    "KeyId",
    "Keybinding",
    "KeybindingConflict",
    "KeybindingDefinition",
    "KeybindingDefinitions",
    "Keybindings",
    "KeybindingsConfig",
    "KeybindingsManager",
    "Loader",
    "LoaderIndicatorOptions",
    "Markdown",
    "MarkdownOptions",
    "MarkdownTheme",
    "Marked",
    "MouseRegion",
    "MouseRegionHandler",
    "NativeClipboard",
    "OklchChannels",
    "OverlayAnchor",
    "OverlayBounds",
    "OverlayHandle",
    "OverlayMargin",
    "OverlayOptions",
    "OverlayUnfocusOptions",
    "ProcessTerminal",
    "ProgramStatus",
    "RenderLatexOptions",
    "RgbColor",
    "RgbColorValue",
    "SelectItem",
    "SelectList",
    "SelectListLayoutOptions",
    "SelectListTheme",
    "SelectListTruncatePrimaryContext",
    "SettingItem",
    "SettingsList",
    "SettingsListTheme",
    "SizeValue",
    "SlashCommand",
    "Spacer",
    "StdinBuffer",
    "StdinBufferEventMap",
    "StdinBufferOptions",
    "Terminal",
    "TerminalCapabilities",
    "TerminalColorMode",
    "TerminalColorScheme",
    "TerminalColors",
    "Text",
    "TextAttributes",
    "TextStyle",
    "Token",
    "TruncatedText",
    "TuiInputListener",
    "TuiInputListenerResult",
    "TuiMainScreen",
    "TuiStopOptions",
    "allocateImageId",
    "backgroundAnsi",
    "calculateImageRows",
    "colorToHex",
    "colorToOklch",
    "colorToRgb",
    "compositeTuiLine",
    "decodeKittyPrintable",
    "deleteKittyImage",
    "detectCapabilities",
    "encodeITerm2",
    "encodeKitty",
    "foregroundAnsi",
    "formatProgramStatus",
    "fuzzyFilter",
    "fuzzyMatch",
    "getCapabilities",
    "getCellDimensions",
    "getGifDimensions",
    "getImageDimensions",
    "getJpegDimensions",
    "getKeybindings",
    "getNativeClipboard",
    "getPngDimensions",
    "getTerminalColorMode",
    "getWebpDimensions",
    "hyperlink",
    "imageFallback",
    "indexedColor",
    "isAppleTerminalSession",
    "isFocusable",
    "isKeyRelease",
    "isKeyRepeat",
    "isKittyProtocolActive",
    "matchesKey",
    "mixColors",
    "parseColor",
    "parseKey",
    "parseTerminalColorSchemeReport",
    "renderFakeCursor",
    "renderImage",
    "renderLatex",
    "resetCapabilitiesCache",
    "rgbColor",
    "setCapabilities",
    "setCapabilityOverrides",
    "setCellDimensions",
    "setImageTranscoder",
    "setKeybindings",
    "setKittyProtocolActive",
    "sliceByColumn",
    "stripTerminalSequences",
    "styleText",
    "styleTextWithAnsi",
    "truncateToWidth",
    "visibleWidth",
    "wrapTextWithAnsi",
]

"""Translated from pi's ``modes/interactive/interactive-mode.ts`` (v1.3.0).

Interactive mode for the coding agent.
Handles TUI rendering and user interaction, delegating business logic to AgentSession.

MISAKA forks that shape the whole file:
- (D1) The fullscreen renderer is not ported: no ``TuiAltScreen``, ``switchTuiMode``, chat viewport, ``tuiMode`` option or
  setting, right-click paste, fullscreen copy-on-select and wheel settings, and no clickable header logo.
- (D11) No system theme: the theme controller resolves settings against the terminal's light/dark appearance once and
  falls back to the built-in theme matching the terminal.
- MISAKA's read-only/attach session viewer (``misaka chat --read-only/--attach``) draws with its own ``Conversation``
  screen and ``TranscriptRenderer``; this file does not use either.
- MISAKA has no ``/bug`` (pi's reports go to the Pi developers), no ``/changelog`` and changelog notices (no changelog
  ships), no package sources or package updates, no pi version check, no install telemetry ping, and no managed fd/rg
  download; the easter eggs are not ported.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import errno
import functools
import inspect
import json
import os
import posixpath
import re
import shlex
import signal
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from dataclasses import replace as dataclasses_replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal, NoReturn, Protocol, TypedDict, TypeGuard

from misaka.agent.types import ThinkingLevel
from misaka.ai.auth.types import AuthCheck, AuthOperationOptions, LoginOptions
from misaka.ai.models import ModelsRefreshOptions
from misaka.ai.types import ImageContent, Model
from misaka.ai.utils.retry import isRetryableAssistantError
from misaka.config import (
    APP_NAME,
    APP_TITLE,
    VERSION,
    get_agent_dir,
    get_auth_path,
    get_debug_log_path,
    home,
    profiles,
)
from misaka.config.engine import detectInstallChange
from misaka.core.agent_session import parseSkillBlock
from misaka.core.agent_session_runtime import (
    ContextCarryUnsupportedError,
    SessionImportFileNotFoundError,
)
from misaka.core.auth_storage import oauth_account_key
from misaka.core.bash_executor import BashResult
from misaka.core.cache_stats import (
    CACHE_TTL_MS,
    CacheMiss,
    collectCacheMisses,
    computeCacheWaste,
    detectCacheMiss,
)
from misaka.core.cache_warmer import (
    formatCacheWarmingStatus,
    formatCacheWarmingUsage,
)
from misaka.core.crash_log import findExtensionStackMatches
from misaka.core.defaults import DEFAULT_THINKING_LEVEL, THINKING_LEVEL_OPTIONS
from misaka.core.extensions import startup_sections
from misaka.core.extensions.types import (
    MarkdownTransformer,
    ProjectTrustContext,
    WorkingIndicatorOptions,
)
from misaka.core.footer_data_provider import FooterDataProvider
from misaka.core.http_dispatcher import configureHttpDispatcher, formatHttpIdleTimeoutMs
from misaka.core.keybindings import KeybindingsManager
from misaka.core.messages import createCompactionSummaryMessage, createCustomMessage
from misaka.core.model_resolver import (
    defaultModelPerProvider,
    findExactModelReferenceMatch,
    resolveModelScopeFromModels,
)
from misaka.core.model_runtime import CredentialSynchronizationError
from misaka.core.prompt_templates import parse_prompt_template_invocation
from misaka.core.provider_attribution import CLIENT_NAME
from misaka.core.radius import RADIUS_MCP_URL, RADIUS_PROVIDER_ID
from misaka.core.session_cwd import (
    MissingSessionCwdError,
    formatMissingSessionCwdPrompt,
)
from misaka.core.session_manager import (
    InvalidSessionFileError,
    SessionManager,
    session_entry_to_display_messages,
    sessions_root_of,
)
from misaka.core.settings_manager import MermaidRenderingMode
from misaka.core.slash_commands import (
    _LOCAL_ALIAS_SLASH_COMMANDS,
    BUILTIN_SLASH_COMMANDS,
)
from misaka.core.tools.renderers import withBuiltInRenderers
from misaka.core.tools.truncate import TruncationResult
from misaka.core.trust_manager import (
    ProjectTrustStore,
    hasTrustRequiringProjectResources,
)
from misaka.core.usage_totals import getUsageCostBreakdown
from misaka.ui.tui import (
    AutocompleteProvider,
    CombinedAutocompleteProvider,
    Container,
    LoaderIndicatorOptions,
    Markdown,
    SlashCommand,
    Spacer,
    Text,
    TruncatedText,
    fuzzyFilter,
    matchesKey,
    setCapabilityOverrides,
    setKeybindings,
    stripTerminalSequences,
    visibleWidth,
)
from misaka.ui.tui.autocomplete import AutocompleteItem
from misaka.utils import loop_watchdog
from misaka.utils._abort_signal import TimeoutAbortSignal
from misaka.utils.abort import raceWithAbortSignal
from misaka.utils.chalk import chalk
from misaka.utils.clipboard import (
    copyToClipboard,
    readClipboardFilePaths,
    readClipboardText,
)
from misaka.utils.clipboard_image import (
    extensionForImageMimeType,
    readClipboardImage,
)
from misaka.utils.collation import localeCompare, localeSortKey
from misaka.utils.image_convert import ensurePngTranscoder
from misaka.utils.js import (
    ECMASCRIPT_WHITESPACE,
    UNDEFINED,
    AbortController,
    clearTimeout,
    errorMessage,
    jsIndex,
    jsonStringify,
    jsRound,
    jsTrim,
    jsTrimStart,
    setTimeout,
    spawn,
    toFixed,
)
from misaka.utils.paths import getCwdRelativePath
from misaka.utils.shell import killTrackedDetachedChildren
from misaka.utils.tools_manager import ToolStatus, ensureTool
from misaka.utils.values import maybe_await, read_field, signal_aborted

from .components.assistant_message import AssistantMessageComponent
from .components.bash_execution import BashExecutionComponent
from .components.branch_summary_message import BranchSummaryMessageComponent
from .components.compaction_summary_message import CompactionSummaryMessageComponent
from .components.custom_editor import CustomEditor, CustomEditorOptions
from .components.custom_entry import CustomEntryComponent
from .components.custom_message import CustomMessageComponent
from .components.dynamic_border import DynamicBorder
from .components.extension_editor import ExtensionEditorComponent
from .components.extension_input import ExtensionInputComponent
from .components.extension_selector import ExtensionSelectorComponent
from .components.footer import FooterComponent, formatTokens
from .components.keybinding_hints import (
    KeyTextFormatOptions,
    formatKeyText,
    keyDisplayText,
    keyHint,
    keyText,
    rawKeyHint,
)
from .components.login_dialog import LoginDialogComponent
from .components.mermaid import (
    MermaidTransformerOptions,
    createMermaidMarkdownTransformer,
)
from .components.model_selector import (
    DefaultModelReference,
    ModelSelectorComponent,
    ScopedModelItem,
)
from .components.oauth_selector import (
    AuthSelectorProvider,
    OAuthSelectorComponent,
    formatAuthSelectorProviderStatus,
    formatAuthSelectorProviderType,
)
from .components.radius_login_selector import RadiusOption, createLoginMenuSelector
from .components.scoped_models_selector import (
    ModelsCallbacks,
    ModelsConfig,
    ScopedModelsSelectorComponent,
)
from .components.session_selector import SessionSelectorComponent
from .components.settings_selector import (
    SettingsCallbacks,
    SettingsConfig,
    SettingsSelectorComponent,
)
from .components.skill_invocation_message import SkillInvocationMessageComponent
from .components.status_indicator import (
    BranchSummaryStatusIndicator,
    CompactionStatusIndicator,
    IdleStatus,
    RetryStatusIndicator,
    StatusIndicator,
    WorkingStatusIndicator,
)
from .components.themed_text import ThemedText
from .components.thinking_selector import ThinkingSelectorComponent
from .components.tool_execution import ToolExecutionComponent
from .components.tree_selector import TreeSelectorComponent
from .components.trust_selector import TrustSelectorComponent, TrustSelectorOptions
from .components.user_message import UserMessageComponent
from .components.user_message_selector import (
    UserMessageItem,
    UserMessageSelectorComponent,
)
from .external_editor import editInExternalEditor
from .model_catalog_refresh import refreshModelCatalogs
from .model_search import ModelSearchItem, getModelSearchText
from .program_status_reporter import BlockedStatus, ProgramStatusReporter
from .session_share import SessionShareContext, shareSession
from .theme.theme import (
    Theme,
    ThemeColor,
    getAvailableThemes,
    getAvailableThemesWithPaths,
    getEditorTheme,
    getMarkdownTheme,
    getThemeByName,
    onThemeChange,
    setRegisteredThemes,
    stopThemeWatcher,
    theme,
)
from .theme.theme_controller import InteractiveThemeController
from .tui_renderer import InteractiveTuiOptions, createInteractiveTui


class Expandable(Protocol):
    """Interface for components that can be expanded/collapsed"""

    def setExpanded(self, expanded: bool) -> None: ...


class WorkingStatusEditor(Protocol):
    embedWorkingStatus: bool

    def setWorkingStatusIndicator(self, indicator: StatusIndicator | None) -> None: ...


def isEmbeddedStatus(editor: Any) -> TypeGuard[WorkingStatusEditor]:
    return getattr(editor, "embedWorkingStatus", None) is True and callable(
        getattr(editor, "setWorkingStatusIndicator", None)
    )


def isExpandable(obj: Any) -> TypeGuard[Expandable]:
    return obj is not None and callable(getattr(obj, "setExpanded", None))


class ExpandableText(ThemedText):
    def __init__(
        self,
        getCollapsedText: Callable[[], str],
        getExpandedText: Callable[[], str],
        expanded: bool = False,
        paddingX: int = 0,
        paddingY: int = 0,
    ) -> None:
        state = {"expanded": expanded}
        super().__init__(lambda: getExpandedText() if state["expanded"] else getCollapsedText(), paddingX, paddingY)
        self.state = state

    def setExpanded(self, expanded: bool) -> None:
        self.state["expanded"] = expanded
        self.invalidate()


# MISAKA fork (D1): pi's `BuiltInHeader` only adds the logo click that plays an easter egg in fullscreen mode; the
# header is an `ExpandableText`.


class CompactionQueuedMessage(TypedDict):
    text: str
    mode: Literal["steer", "followUp"]


class CompactionCostNotice(TypedDict):
    type: Literal["compaction_cost"]
    kind: Literal["compaction", "branch_summary"]
    usage: Any


def isCustomSessionEntry(item: Any) -> bool:
    return read_field(item, "type") == "custom"


def isCompactionCostNotice(item: Any) -> bool:
    return read_field(item, "type") == "compaction_cost"


def isUsageSessionEntry(item: Any) -> bool:
    return read_field(item, "type") == "usage"


# EIO: tty reads/ioctls from an orphaned background process group, or writes after hangup.
# ENOTTY: the tty was revoked (macOS) and stdin is no longer a terminal.
DEAD_TERMINAL_ERROR_CODES = {"EIO", "EPIPE", "ENOTCONN", "ENOTTY"}


def isDeadTerminalError(error: Any) -> bool:
    # MISAKA fork: Node names the errno in `error.code`; Python carries the number in `OSError.errno`.
    if not isinstance(error, OSError) or error.errno is None:
        return False
    code = errno.errorcode.get(error.errno)
    return code is not None and code in DEAD_TERMINAL_ERROR_CODES


def formatCrashExtensionHint(extensionMatches: Sequence[str] | None) -> str | None:
    matches = (
        [match for match in extensionMatches if isinstance(match, str) and len(match) > 0]
        if isinstance(extensionMatches, (list, tuple))
        else []
    )
    if len(matches) == 0:
        return None
    quoted = [f"`{match}`" for match in matches]
    labels = (
        quoted[0]
        if len(quoted) == 1
        else " and ".join(quoted)
        if len(quoted) == 2
        else f"{', '.join(quoted[:-1])}, and {quoted[-1]}"
    )
    noun = "extension" if len(matches) == 1 else "extensions"
    # MISAKA fork: MISAKA has no `config` command to disable an extension with, so only the `-ne` check is offered.
    return f"A stack frame came from loaded {noun} {labels}, which may be involved. Run `{APP_NAME} -ne` to confirm."


ANTHROPIC_SUBSCRIPTION_AUTH_WARNING = (
    "Anthropic subscription auth is active. Third-party harness usage draws from extra usage and is billed per token, "
    "not your Claude plan limits. Manage extra usage at https://claude.ai/settings/usage. Disable this warning in "
    "/settings."
)


def isAnthropicSubscriptionAuthKey(apiKey: str | None) -> bool:
    return isinstance(apiKey, str) and apiKey.startswith("sk-ant-oat")


def isUnknownModel(model: Any) -> bool:
    return (
        bool(model)
        and read_field(model, "provider") == "unknown"
        and read_field(model, "id") == "unknown"
        and read_field(model, "api") == "unknown"
    )


_SHELL_SAFE = re.compile(r"[^a-zA-Z0-9_\-./~:@]")


def quoteIfNeeded(value: str) -> str:
    if len(value) > 0 and not _SHELL_SAFE.search(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def formatResumeCommand(sessionManager: Any) -> str | None:
    if not sys.stdout.isatty():
        return None
    if not sessionManager.isPersisted():
        return None

    sessionFile = sessionManager.getSessionFile()
    if not sessionFile or not os.path.exists(sessionFile):
        return None

    # MISAKA fork: MISAKA resumes a conversation through `misaka chat`, which finds the session in the window's
    # own store (`--as <Sister>` outside Last Order); there is no `--session-dir` to name.
    args = [APP_NAME, "chat"]
    who = os.environ.get("MISAKA_WHO")
    if who and who != "last-order":
        args.extend(["--as", quoteIfNeeded(who)])
    args.extend(["--session", sessionManager.getSessionId()])
    return " ".join(args)


def _byProviderName(a: AuthSelectorProvider, b: AuthSelectorProvider) -> int:
    """MISAKA fork: ``(a, b) => a.name.localeCompare(b.name)``."""
    return localeCompare(a.name, b.name)


def hasDefaultModelProvider(providerId: str) -> bool:
    return providerId in defaultModelPerProvider


def llamaCppPostLoginGuidance(actionLabel: str, loadedModelCount: int) -> str:
    return (
        f"{actionLabel}. No llama.cpp models are loaded. Use /llama to load a model, then /model to select it."
        if loadedModelCount == 0
        else f"{actionLabel}. Use /model to select a loaded llama.cpp model, or /llama to manage models."
    )


@dataclass(slots=True)
class LoginProviderCompletionOption:
    id: str
    name: str
    authTypes: list[str]
    subscription: bool | None = None


RADIUS_LOGIN_INTRO = "Radius is a service crafted for Pi by the builders of Pi, Earendil Works"

AUTH_TYPE_ORDER = {"oauth": 0, "api_key": 1}


def createFuzzyAutocompleteItems[T](
    items: list[T],
    prefix: str,
    getSearchText: Callable[[T], str],
    toAutocompleteItem: Callable[[T], AutocompleteItem],
) -> list[AutocompleteItem] | None:
    filtered = fuzzyFilter(items, prefix, getSearchText)
    if len(filtered) == 0:
        return None
    return [toAutocompleteItem(item) for item in filtered]


def getLoginProviderCompletionOptions(providerOptions: list[AuthSelectorProvider]) -> list[LoginProviderCompletionOption]:
    byId: dict[str, LoginProviderCompletionOption] = {}
    for provider in providerOptions:
        existing = byId.get(provider.id)
        if existing:
            if provider.authType not in existing.authTypes:
                existing.authTypes.append(provider.authType)
                existing.authTypes.sort(key=lambda authType: AUTH_TYPE_ORDER[authType])
            continue
        byId[provider.id] = LoginProviderCompletionOption(
            id=provider.id,
            name=provider.name,
            authTypes=[provider.authType],
            subscription=provider.subscription,
        )
    return sorted(byId.values(), key=lambda option: localeSortKey(option.name))


def getLoginProviderSearchText(provider: LoginProviderCompletionOption) -> str:
    authTypes = " ".join(
        f"{authType} {formatAuthSelectorProviderType(authType, provider.subscription)}" for authType in provider.authTypes
    )
    return f"{provider.id} {provider.name} {authTypes}"


def formatLoginProviderCompletionDescription(provider: LoginProviderCompletionOption) -> str:
    authTypes = "/".join(formatAuthSelectorProviderType(authType, provider.subscription) for authType in provider.authTypes)
    return authTypes if provider.name == provider.id else f"{provider.name} · {authTypes}"


@dataclass(slots=True, kw_only=True)
class InteractiveModeOptions:
    """Options for InteractiveMode initialization.

    MISAKA forks: no ``migratedProviders`` (MISAKA migrates no credentials) and no ``tuiMode`` (D1).
    """

    # Diagnostics collected before the interactive TUI was initialized.
    startupDiagnostics: list[Any] | None = None
    # Warning message if session model couldn't be restored
    modelFallbackMessage: str | None = None
    # Cwd to trust after reload if it gained a .pi directory during this implicitly trusted session.
    autoTrustOnReloadCwd: str | None = None
    # Initial message to send on startup (can include @file content)
    initialMessage: str | None = None
    # Images to attach to the initial message
    initialImages: list[ImageContent] | None = None
    # Additional messages to send after the initial message
    initialMessages: list[str] | None = None
    # Force verbose startup (overrides quietStartup setting)
    verbose: bool | None = None
    # Initial interactive theme setting for this invocation.
    initialThemeSetting: str | None = None
    # Terminal implementation. Defaults to the current process terminal.
    terminal: Any = None


# --- MISAKA forks: Python helpers for JavaScript idioms the interactive mode uses ---


def _spread_tool_result(raw: Any, isError: bool, durationMs: float | None = None) -> dict[str, Any]:
    """MISAKA fork: ``{ ...event.result, isError, durationMs }`` for a tool result of either shape (a dataclass result
    has no JavaScript spread)."""
    return {
        "content": list(read_field(raw, "content", []) or []),
        "details": read_field(raw, "details"),
        "isError": isError,
        "durationMs": durationMs,
    }


def _set_field(target: Any, name: str, value: Any) -> None:
    """MISAKA fork: ``target.name = value`` for a message that is a dict or an object."""
    if isinstance(target, dict):
        target[name] = value
    else:
        setattr(target, name, value)


# JavaScript's `\s`: Python's differs at U+001C-U+001F and U+0085 (spaces there, not in JavaScript)
# and at U+FEFF (the other way round).
_JS_WHITESPACE = re.compile(f"[{re.escape(ECMASCRIPT_WHITESPACE)}]")


def _coerce_bash_result(result: Any) -> BashResult:
    """MISAKA fork: Python extensions answer `user_bash` with dicts; this reads one as a `BashResult`."""
    if isinstance(result, BashResult):
        return result

    exitCode = read_field(result, "exitCode", read_field(result, "exit_code"))
    try:
        resolvedExitCode = int(exitCode) if exitCode is not None else None
    except (TypeError, ValueError):
        resolvedExitCode = None

    fullOutputPath = read_field(result, "fullOutputPath", read_field(result, "full_output_path"))
    return BashResult(
        output=str(read_field(result, "output", "") or ""),
        exitCode=resolvedExitCode,
        cancelled=bool(read_field(result, "cancelled", False)),
        truncated=bool(read_field(result, "truncated", False)),
        fullOutputPath=str(fullOutputPath) if fullOutputPath is not None else None,
    )


def _make_bash_truncation_result(content: str) -> TruncationResult:
    """MISAKA fork: upstream casts `{ truncated: true, content }` to a TruncationResult; Python builds a whole one."""
    encoded = content.encode("utf-8")
    lines = content.split("\n") if content else []
    if content.endswith("\n") and lines:
        lines.pop()
    totalLines = len(lines)
    totalBytes = len(encoded)
    return TruncationResult(
        content=content,
        truncated=True,
        truncatedBy="bytes",
        totalLines=totalLines,
        totalBytes=totalBytes,
        outputLines=totalLines,
        outputBytes=totalBytes,
        lastLinePartial=False,
        firstLineExceedsLimit=False,
        maxLines=0,
        maxBytes=0,
    )


def _register_abort_handler(signal_: Any, callback: Callable[[], None]) -> Callable[[], None]:
    """MISAKA fork: ``signal.addEventListener("abort", callback, { once: true })`` for both of MISAKA's signal shapes
    (pi-style signals with listeners and flag-style controllers with ``wait()``). Returns the remover."""
    if signal_ is None:
        return lambda: None

    if signal_aborted(signal_):
        callback()
        return lambda: None

    addListener = getattr(signal_, "addEventListener", None)
    removeListener = getattr(signal_, "removeEventListener", None)
    if callable(addListener):
        addListener("abort", callback, {"once": True})

        def removeEventListener() -> None:
            if callable(removeListener):
                removeListener("abort", callback)

        return removeEventListener

    wait = getattr(signal_, "wait", None)
    if callable(wait):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return lambda: None

        async def watchAbort() -> None:
            await wait()
            callback()

        task = loop.create_task(watchAbort())

        def cancel() -> None:
            task.cancel()

        return cancel

    return lambda: None


def _toLoaderIndicatorOptions(options: WorkingIndicatorOptions | None) -> LoaderIndicatorOptions | None:
    """MISAKA fork: the extension API's `WorkingIndicatorOptions` (a TypedDict) as the TUI loader's options."""
    if options is None:
        return None
    if isinstance(options, LoaderIndicatorOptions):
        return options
    return LoaderIndicatorOptions(frames=read_field(options, "frames"), intervalMs=read_field(options, "intervalMs"))


@dataclass(slots=True)
class DefaultFlagArgs:
    """MISAKA fork: parsed arguments of `/thinking --default <level>` (from pi 496185f6, which no release carries;
    MISAKA documents it)."""

    persist: bool = False
    searchTerm: str | None = None
    error: str | None = None


def parse_default_flag_args(commandName: str, args: str | None) -> DefaultFlagArgs:
    """Accept only ``--default``; any other ``--x`` option is an error."""
    tokens = (args or "").strip().split()
    persist = False
    rest: list[str] = []
    for index, token in enumerate(tokens):
        if token.startswith("--") and not rest:
            if token == "--default":
                persist = True
                continue
            return DefaultFlagArgs(error=f'Unknown /{commandName} option "{token}". Supported option: --default.')
        rest = tokens[index:]
        break
    return DefaultFlagArgs(persist=persist, searchTerm=" ".join(rest) or None)


class _ExtensionUIContext:
    """MISAKA fork: the object ``createExtensionUIContext`` returns. Upstream builds an object literal with a ``theme``
    getter; Python has no literal with a getter, so the members live on this class, in upstream order."""

    def __init__(self, mode: InteractiveMode) -> None:
        self._mode = mode

    def select(self, title: str, options: list[str], opts: Any = None) -> Awaitable[str | None]:
        return self._mode.showExtensionSelector(title, options, opts)

    def confirm(self, title: str, message: str, opts: Any = None) -> Awaitable[bool]:
        return self._mode.showExtensionConfirm(title, message, opts)

    def input(self, title: str, placeholder: str | None = None, opts: Any = None) -> Awaitable[str | None]:
        return self._mode.showExtensionInput(title, placeholder, opts)

    def notify(self, message: str, type: str | None = None) -> None:
        self._mode.showExtensionNotify(message, type)

    def onTerminalInput(self, handler: Any) -> Callable[[], None]:
        return self._mode.addExtensionTerminalInputListener(handler)

    def setStatus(self, key: str, text: str | None = None) -> None:
        self._mode.setExtensionStatus(key, text)

    def setWorkingMessage(self, message: str | None = None) -> None:
        self._mode.workingMessage = message
        indicator = self._mode.activeStatusIndicator
        if indicator is not None and indicator.kind == "working":
            indicator.setMessage(message if message is not None else self._mode.defaultWorkingMessage)

    def setWorkingVisible(self, visible: bool) -> None:
        self._mode.setWorkingVisible(visible)

    def setWorkingIndicator(self, options: WorkingIndicatorOptions | None = None) -> None:
        self._mode.setWorkingIndicator(options)

    def setHiddenThinkingLabel(self, label: str | None = None) -> None:
        self._mode.setHiddenThinkingLabel(label)

    def setWidget(self, key: str, content: Any, options: Any = None) -> None:
        self._mode.setExtensionWidget(key, content, options)

    def setFooter(self, factory: Any) -> None:
        self._mode.setExtensionFooter(factory)

    def setHeader(self, factory: Any) -> None:
        self._mode.setExtensionHeader(factory)

    def setTitle(self, title: str) -> None:
        self._mode.ui.terminal.setTitle(title)

    def custom(self, factory: Any, options: Any = None) -> Awaitable[Any]:
        return self._mode.showExtensionCustom(factory, options)

    def pasteToEditor(self, text: str) -> None:
        self._mode.editor.handleInput(f"\x1b[200~{text}\x1b[201~")

    def setEditorText(self, text: str) -> None:
        self._mode.editor.setText(text)

    def getEditorText(self) -> str:
        return self._mode.getEditorExpandedText()

    def editor(self, title: str, prefill: str | None = None) -> Awaitable[str | None]:
        return self._mode.showExtensionEditor(title, prefill)

    def addAutocompleteProvider(self, factory: Callable[[AutocompleteProvider], AutocompleteProvider]) -> None:
        self._mode.autocompleteProviderWrappers.append(factory)
        self._mode.setupAutocompleteProvider()

    def setEditorComponent(self, factory: Any) -> None:
        self._mode.setCustomEditorComponent(factory)

    def getEditorComponent(self) -> Any:
        return self._mode.editorComponentFactory

    @property
    def theme(self) -> Theme:
        return theme

    def getAllThemes(self) -> list[Any]:
        return getAvailableThemesWithPaths()

    def getTheme(self, name: str) -> Theme | None:
        return getThemeByName(name)

    def setTheme(self, themeOrName: str | Theme) -> Any:
        if isinstance(themeOrName, Theme):
            return self._mode.themeController.setThemeInstance(themeOrName)
        result = self._mode.themeController.setThemeName(themeOrName)
        if result["success"]:  # noqa: SIM102 - upstream nesting
            if self._mode.settingsManager.getTheme() != themeOrName:
                self._mode.settingsManager.setTheme(themeOrName)
        return result

    def getToolsExpanded(self) -> bool:
        return self._mode.toolOutputExpanded

    def setToolsExpanded(self, expanded: bool) -> None:
        self._mode.setToolsExpanded(expanded)


class InteractiveMode:
    # Maximum total widget lines to prevent viewport overflow
    MAX_WIDGET_LINES = 10

    # Convenience accessors
    @property
    def session(self) -> Any:
        return self.runtimeHost.session

    @property
    def agent(self) -> Any:
        return self.session.agent

    @property
    def sessionManager(self) -> Any:
        return self.session.sessionManager

    @property
    def settingsManager(self) -> Any:
        return self.session.settingsManager

    def __init__(self, runtimeHost: Any, options: InteractiveModeOptions | dict[str, Any] | None = None) -> None:
        if options is None:
            options = InteractiveModeOptions()
        elif isinstance(options, dict):
            # MISAKA fork: the engine passes the options as a dict.
            options = InteractiveModeOptions(**options)

        self.autocompleteProviderWrappers: list[Callable[[AutocompleteProvider], AutocompleteProvider]] = []
        self.editorComponentFactory: Any = None
        self.autocompleteProvider: AutocompleteProvider | None = None
        self.fdPath: str | None = None
        self.activeSelectorToken: object | None = None
        self.activeSelectorDispose: Callable[[], None] | None = None
        self.isInitialized = False
        self.onInputCallback: Callable[[str], None] | None = None
        self.pendingUserInputs: list[str] = []
        self.activeStatusIndicator: StatusIndicator | None = None
        self.activeWorkingIndicatorEmbedded = False
        self.idleStatus = IdleStatus()
        self.workingMessage: str | None = None
        self.workingVisible = True
        self.workingIndicatorOptions: WorkingIndicatorOptions | None = None
        self.defaultWorkingMessage = "Working"
        self.defaultHiddenThinkingLabel = "Thinking..."
        self.hiddenThinkingLabel = self.defaultHiddenThinkingLabel

        self.lastSigintTime: float = 0
        self.lastEscapeTime: float = 0
        self.anthropicSubscriptionWarningShown = False

        # Status line tracking (for mutating immediately-sequential status updates)
        self.lastStatusSpacer: Spacer | None = None
        self.lastStatusText: ThemedText | None = None
        self.lastStatusMessage = ""
        self.managedToolStatusStarted = False

        # Streaming message tracking
        self.streamingComponent: AssistantMessageComponent | None = None
        self.entriesRenderedByBoundaryCompaction: set[str] = set()
        self.streamingMessage: Any = None

        # Tool execution tracking: toolCallId -> component
        self.pendingTools: dict[str, ToolExecutionComponent] = {}

        # Tool output expansion state
        self.toolOutputExpanded = False

        # Thinking block visibility state
        self.hideThinkingBlock = False
        self.outputPad = 1
        self.mermaidMarkdownTransformer: MarkdownTransformer = createMermaidMarkdownTransformer(
            MermaidTransformerOptions(getMode=lambda: self.settingsManager.getMermaidRenderingMode(), theme=theme)
        )

        # Agent subscription unsubscribe function
        self.unsubscribe: Callable[[], None] | None = None
        self.signalCleanupHandlers: list[Callable[[], None]] = []

        # Track if editor is in bash mode (text starts with !)
        self.isBashMode = False

        # Track current bash execution component
        self.bashComponent: BashExecutionComponent | None = None

        # Track pending bash components (shown in pending area, moved to chat on submit)
        self.pendingBashComponents: list[BashExecutionComponent] = []

        # Auto-compaction state
        self.autoCompactionEscapeHandler: Callable[[], None] | None = None

        # Auto-retry state
        self.retryEscapeHandler: Callable[[], None] | None = None

        # Messages queued while compaction is running
        self.compactionQueuedMessages: list[CompactionQueuedMessage] = []

        # Shutdown state
        self.shutdownRequested = False
        self.isShuttingDown = False

        # Reports working, blocked, done, and error states to terminals that support OSC 7501.
        self.programStatus = ProgramStatusReporter(
            lambda: self.ui.terminal,
            lambda: self.sessionManager.getSessionName(),
        )

        self.installChangeWarningShown = False

        # Extension UI state
        self.extensionSelector: ExtensionSelectorComponent | None = None
        self.extensionInput: ExtensionInputComponent | None = None
        self.extensionEditor: ExtensionEditorComponent | None = None
        self.extensionTerminalInputSubscriptions: list[dict[str, Any]] = []

        # Extension widgets (components rendered above/below the editor)
        self.extensionWidgetsAbove: dict[str, Any] = {}
        self.extensionWidgetsBelow: dict[str, Any] = {}

        # Custom footer from extension (undefined = use built-in footer)
        self.customFooter: Any = None

        # Built-in header (logo + keybinding hints + changelog)
        self.builtInHeader: Any = None

        # Custom header from extension (undefined = use built-in header)
        self.customHeader: Any = None

        # MISAKA fork: extension dialogs run one at a time; a second waits for the first, a dialog opened from inside
        # another is refused, and resetting the extension UI cancels the waiting one. Sisters, research and extension
        # parts can all ask at once, and upstream's displacement would strand the displaced dialog's caller.
        self._extensionPromptLock = asyncio.Lock()
        self._extensionPromptOwner: object | None = None
        self._extensionPromptTask: asyncio.Task[Any] | None = None
        self._extensionPromptCancel: Callable[[], None] | None = None
        self._extensionPromptGeneration = 0
        self._extensionPromptContext: contextvars.ContextVar[object | None] = contextvars.ContextVar(
            f"misaka_extension_prompt_{id(self)}", default=None
        )
        self._extensionPromptContextToken: contextvars.Token[object | None] | None = None
        # MISAKA fork: `run` returns the exit code through this future (the engine awaits it), and `void` promises are
        # tasks held here until they finish (asyncio holds tasks weakly).
        self._shutdownFuture: asyncio.Future[int] | None = None
        self._pendingUserInputFuture: asyncio.Future[str] | None = None
        self._backgroundTasks: set[asyncio.Task[Any]] = set()

        self.runtimeHost = runtimeHost
        setCapabilityOverrides(self.settingsManager.getTerminalCapabilityOverrides())
        self.options = options
        self.autoTrustOnReloadCwd = options.autoTrustOnReloadCwd
        self.runtimeHost.setBeforeSessionInvalidate(lambda: self.resetExtensionUI())

        async def rebindSession(_session: Any = None) -> None:
            await self.rebindCurrentSession({"renderBeforeBind": True})
            self.themeController.applyFromSettings()

        # MISAKA fork: the runtime passes the new session; the bound accessors read it from the runtime anyway.
        self.runtimeHost.setRebindSession(rebindSession)
        self.version = VERSION
        # MISAKA fork (D1): the main-screen renderer only, so no renderer reference is needed.
        self.ui = createInteractiveTui(
            InteractiveTuiOptions(
                showHardwareCursor=self.settingsManager.getShowHardwareCursor(),
                logDirectory=get_agent_dir(),
                terminal=options.terminal,
            )
        )
        self.ui.setClearOnShrink(self.settingsManager.getClearOnShrink())
        self.headerContainer = Container()
        self.loadedResourcesContainer = Container()
        self.chatContainer = Container()
        self.documentContainer = Container()
        self.documentContainer.addChild(self.headerContainer)
        self.documentContainer.addChild(self.loadedResourcesContainer)
        self.documentContainer.addChild(self.chatContainer)
        self.pendingMessagesContainer = Container()
        self.statusContainer = Container()
        self.widgetContainerAbove = Container()
        self.widgetContainerBelow = Container()
        self.keybindings = KeybindingsManager.create()
        setKeybindings(self.keybindings)
        editorPaddingX = self.settingsManager.getEditorPaddingX()
        autocompleteMaxVisible = self.settingsManager.getAutocompleteMaxVisible()
        self.defaultEditor = CustomEditor(
            self.ui,
            getEditorTheme(),
            self.keybindings,
            CustomEditorOptions(
                paddingX=editorPaddingX,
                autocompleteMaxVisible=autocompleteMaxVisible,
                embedWorkingStatus=True,
            ),
        )
        self.editor: Any = self.defaultEditor
        self.editorContainer = Container()
        self.editorContainer.addChild(self.editor)
        self.footerDataProvider = FooterDataProvider(self.sessionManager.getCwd())
        self.footer = FooterComponent(self.session, self.footerDataProvider)
        self.footer.setAutoCompactEnabled(self.session.autoCompactionEnabled)
        self.footerContainer = Container()
        self.footerContainer.addChild(self.footer)

        # Load hide thinking block setting
        self.hideThinkingBlock = self.settingsManager.getHideThinkingBlock()
        self.outputPad = self.settingsManager.getOutputPad()

        # Register themes from resource loader and initialize
        setRegisteredThemes(read_field(self.session.resourceLoader.getThemes(), "themes", []))
        self.themeController = InteractiveThemeController(self.ui, {"getSettingsManager": lambda: self.settingsManager, "showError": lambda message: self.showError(message), "onChanged": lambda: self.updateEditorBorderColor(), "initialThemeSetting": options.initialThemeSetting})

    def getAutocompleteSourceTag(self, sourceInfo: Any = None) -> str | None:
        # Built-in extension commands are untagged, like built-in commands.
        if not sourceInfo or read_field(sourceInfo, "source") == "builtin":
            return None

        scope = read_field(sourceInfo, "scope")
        scopePrefix = "u" if scope == "user" else "p" if scope == "project" else "t"
        source = jsTrim(str(read_field(sourceInfo, "source", "")))

        if source == "auto" or source == "local" or source == "cli":
            return scopePrefix

        # MISAKA fork: MISAKA has no package sources (npm:/git:), so every other source is tagged by its scope.
        return scopePrefix

    def prefixAutocompleteDescription(self, description: str | None, sourceInfo: Any = None) -> str | None:
        sourceTag = self.getAutocompleteSourceTag(sourceInfo)
        if not sourceTag:
            return description
        return f"[{sourceTag}] {description}" if description else f"[{sourceTag}]"

    def getBuiltInCommandConflictDiagnostics(self, extensionRunner: Any) -> list[dict[str, Any]]:
        builtinNames = {command.name for command in BUILTIN_SLASH_COMMANDS}
        return [
            {
                "type": "warning",
                "message": (
                    f"Extension command '/{command.name}' conflicts with built-in interactive command. Skipping in "
                    "autocomplete."
                    if command.invocationName == command.name
                    else f"Extension command '/{command.name}' conflicts with built-in interactive command. Available "
                    f"as '/{command.invocationName}'."
                ),
                "path": read_field(command.sourceInfo, "path"),
            }
            for command in extensionRunner.getRegisteredCommands()
            if command.name in builtinNames
        ]

    def createBaseAutocompleteProvider(self) -> AutocompleteProvider:
        # Define commands for autocomplete
        slashCommands = [
            SlashCommand(name=command.name, description=command.description, argumentHint=command.argumentHint)
            for command in BUILTIN_SLASH_COMMANDS
        ]
        # MISAKA fork: MISAKA's own commands (`/clear`, `/board`).
        slashCommands.extend(
            SlashCommand(name=command.name, description=command.description, argumentHint=command.argumentHint)
            for command in _LOCAL_ALIAS_SLASH_COMMANDS
        )

        modelCommand = next((command for command in slashCommands if command.name == "model"), None)
        if modelCommand:

            def modelCompletions(prefix: str) -> list[AutocompleteItem] | None:
                models = (
                    [read_field(scoped, "model") for scoped in self.session.scopedModels]
                    if len(self.session.scopedModels) > 0
                    else self.session.modelRuntime.getAvailableSnapshot()
                )

                if len(models) == 0:
                    return None

                # Create items with provider/id format
                items = [
                    ModelSearchItem(id=m.id, provider=m.provider, name=getattr(m, "name", None)) for m in models
                ]

                return createFuzzyAutocompleteItems(
                    items,
                    prefix,
                    getModelSearchText,
                    lambda item: AutocompleteItem(
                        value=f"{item.provider}/{item.id}", label=item.id, description=item.provider
                    ),
                )

            modelCommand.getArgumentCompletions = modelCompletions  # type: ignore[assignment]

        thinkingCommand = next((command for command in slashCommands if command.name == "thinking"), None)
        if thinkingCommand:

            def thinkingCompletions(prefix: str) -> list[AutocompleteItem] | None:
                return createFuzzyAutocompleteItems(
                    list(self.session.getAvailableThinkingLevels()),
                    prefix,
                    lambda level: level,
                    lambda level: AutocompleteItem(value=level, label=level),
                )

            thinkingCommand.getArgumentCompletions = thinkingCompletions  # type: ignore[assignment]

        loginCommand = next((command for command in slashCommands if command.name == "login"), None)
        if loginCommand:

            def loginCompletions(prefix: str) -> list[AutocompleteItem] | None:
                providers = getLoginProviderCompletionOptions(self.getLoginProviderOptions())
                return createFuzzyAutocompleteItems(
                    providers,
                    prefix,
                    getLoginProviderSearchText,
                    lambda provider: AutocompleteItem(
                        value=provider.id,
                        label=provider.id,
                        description=formatLoginProviderCompletionDescription(provider),
                    ),
                )

            loginCommand.getArgumentCompletions = loginCompletions  # type: ignore[assignment]

        # Convert prompt templates to SlashCommand format for autocomplete
        templateCommands = [
            SlashCommand(
                name=cmd.name,
                description=self.prefixAutocompleteDescription(cmd.description, cmd.sourceInfo),
                argumentHint=cmd.argumentHint,
            )
            for cmd in self.session.promptTemplates
        ]

        # Convert extension commands to SlashCommand format
        builtinCommandNames = {c.name for c in slashCommands}
        extensionCommands = [
            SlashCommand(
                name=cmd.invocationName,
                description=self.prefixAutocompleteDescription(cmd.description, cmd.sourceInfo),
                getArgumentCompletions=cmd.getArgumentCompletions,  # type: ignore[arg-type]
            )
            for cmd in self.session.extensionRunner.getRegisteredCommands()
            if cmd.name not in builtinCommandNames
        ]

        # MISAKA fork: skills are MISAKA core commands (the parts' moments), not pi's `skill:` commands; they and the
        # other core commands follow the extension commands.
        knownNames = {command.name for command in [*slashCommands, *templateCommands, *extensionCommands]}
        coreCommandList = [
            SlashCommand(
                name=command.name,
                description=self.prefixAutocompleteDescription(command.description, None),
            )
            for command in self.session.moments.commands()
            if command.name not in knownNames
        ]

        return CombinedAutocompleteProvider(
            [*slashCommands, *templateCommands, *extensionCommands, *coreCommandList],
            self.sessionManager.getCwd(),
            self.fdPath,
        )

    def setupAutocompleteProvider(self) -> None:
        provider = self.createBaseAutocompleteProvider()
        triggerCharacters: list[str] = []
        for wrapProvider in self.autocompleteProviderWrappers:
            provider = wrapProvider(provider)
            triggerCharacters.extend(getattr(provider, "triggerCharacters", None) or [])
        if len(triggerCharacters) > 0:
            provider.triggerCharacters = list(dict.fromkeys(triggerCharacters))  # type: ignore[attr-defined]

        self.autocompleteProvider = provider
        self.defaultEditor.setAutocompleteProvider(provider)
        if self.editor is not self.defaultEditor:
            setAutocompleteProvider = getattr(self.editor, "setAutocompleteProvider", None)
            if setAutocompleteProvider is not None:
                setAutocompleteProvider(provider)

    # MISAKA fork: no `showStartupNoticesIfNeeded`: it shows only the changelog ("What's New"), which MISAKA does not
    # ship.

    def mountInteractiveTui(self, tui: Any, components: list[Any]) -> None:
        for component in components:
            tui.addChild(component)

    def stopInteractiveTui(self) -> None:
        # MISAKA fork (D1): the fullscreen exit output and `preserveScreen` belong to the fullscreen renderer.
        self.ui.stop()

    # MISAKA fork (D1): no `switchTuiMode`.

    async def init(self) -> None:
        if self.isInitialized:
            return

        self.registerSignalHandlers()

        # MISAKA fork: no changelog is loaded (MISAKA ships none).

        if len(self.session.scopedModels) > 0 and self.shouldShowStartupDetails():
            modelList = ", ".join(
                f"{sm.model.id}{f':{sm.thinkingLevel}' if sm.thinkingLevel else ''}" for sm in self.session.scopedModels
            )
            cycleKeys = self.keybindings.getKeys("app.model.cycleForward")
            cycleHint = (
                theme.fg("muted", f" ({formatKeyText('/'.join(cycleKeys), KeyTextFormatOptions(capitalize=True))} to cycle)")
                if len(cycleKeys) > 0
                else ""
            )
            print(theme.fg("dim", f"Model scope: {modelList}{cycleHint}"))

        # Keep one component tree and remount it when changing renderers.
        self.renderWidgets()  # Initialize with default spacer
        # MISAKA fork (D1): no fullscreen chat viewport.
        self.mountInteractiveTui(
            self.ui,
            [
                self.documentContainer,
                self.pendingMessagesContainer,
                self.statusContainer,
                self.widgetContainerAbove,
                self.editorContainer,
                self.widgetContainerBelow,
                self.footerContainer,
            ],
        )
        # Accept text while startup completes, but only enable interrupt, exit, and submission feedback.
        self.defaultEditor.onAction("app.clear", lambda: self.handleCtrlC())
        self.defaultEditor.onCtrlD = lambda: self.handleCtrlD()
        self.defaultEditor.onSubmit = lambda text: self.handleStartupSubmit(text)
        self.ui.setFocus(self.editor)

        # MISAKA fork: the header title's agent part follows the terminal background (light or dark), from the
        # terminal's OSC 11 answer and its mode-2031 light/dark notifications, and is redrawn when that changes.
        self._terminalBgIsLight = False
        self.ui.setTerminalColorSchemeNotifications(True)

        def applyBackgroundLightness(isLight: bool) -> None:
            if self._terminalBgIsLight == isLight:
                return
            self._terminalBgIsLight = isLight
            if self.builtInHeader is not None:
                self.builtInHeader.invalidate()
            self.ui.requestRender(True)

        self.ui.onTerminalColorSchemeChange(lambda scheme: applyBackgroundLightness(scheme == "light"))

        # Start the UI before initializing extensions so session_start handlers can use interactive dialogs
        self.ui.start()
        self.isInitialized = True
        self.programStatus.report()
        self.ensurePngTranscoder()

        self.themeController.applyFromSettings()

        async def probeTerminalBackground() -> None:
            try:
                rgb = await self.ui.queryTerminalBackgroundColor(timeoutMs=1000)
            except Exception:  # noqa: BLE001 - no background colour answer within the timeout
                return
            if rgb is None:
                return
            from misaka.ui.tui.terminal_colors import getThemeForRgbColor

            applyBackgroundLightness(getThemeForRgbColor(rgb) == "light")

        self._schedule_task(probeTerminalBackground())

        # Add header with keybindings from config (unless silenced)
        if self.shouldShowStartupHeader():
            showDetails = self.shouldShowStartupDetails()
            # MISAKA fork: MISAKA's title in place of the Pi logo. `MISAKA_APP_TITLE` ("MISAKA · <agent>") overrides
            # only the displayed title; the brand keeps the accent colour and the agent part takes `appTitleOnLight`
            # on light terminals and `appTitle` otherwise.
            titleText = os.environ.get("MISAKA_APP_TITLE") or APP_NAME

            def withLogo(hints: str) -> str:
                agentSlot: Any = "appTitleOnLight" if self._terminalBgIsLight else "appTitle"
                if " · " in titleText:
                    brand, agent = titleText.split(" · ", 1)
                    title = theme.fg("accent", brand + " · ") + theme.fg(agentSlot, agent)
                else:
                    title = theme.fg("accent", titleText)
                return f"{theme.bold(title)}{theme.fg('dim', f' v{self.version}')}\n{hints}"

            # Build startup instructions using keybinding hint helpers
            def hint(keybinding: str, description: str) -> str:
                return keyHint(keybinding, description)

            def expandedInstructions() -> str:
                return "\n".join(
                    [
                        hint("app.interrupt", "to interrupt"),
                        hint("app.clear", "to clear"),
                        rawKeyHint(f"{keyText('app.clear')} twice", "to exit"),
                        hint("app.exit", "to exit (empty)"),
                        hint("app.suspend", "to suspend"),
                        keyHint("tui.editor.deleteToLineEnd", "to delete to end"),
                        hint("app.thinking.cycle", "to cycle thinking level"),
                        rawKeyHint(
                            f"{keyText('app.model.cycleForward')}/{keyText('app.model.cycleBackward')}",
                            "to cycle models",
                        ),
                        hint("app.model.select", "to select model"),
                        hint("app.tools.expand", "to expand tools"),
                        hint("app.thinking.toggle", "to expand thinking"),
                        hint("app.editor.external", "for external editor"),
                        rawKeyHint("/", "for commands"),
                        rawKeyHint("!", "to run bash"),
                        rawKeyHint("!!", "to run bash (no context)"),
                        hint("app.message.followUp", "to queue follow-up"),
                        hint("app.message.dequeue", "to edit all queued messages"),
                        hint("app.clipboard.pasteImage", "to paste files on macOS, images, or text"),
                        rawKeyHint("drop files", "to attach"),
                    ]
                )

            def compactInstructions() -> str:
                return theme.fg("muted", " · ").join(
                    [
                        hint("app.interrupt", "interrupt"),
                        rawKeyHint(f"{keyText('app.clear')}/{keyText('app.exit')}", "clear/exit"),
                        rawKeyHint("/", "commands"),
                        rawKeyHint("!", "bash"),
                        hint("app.tools.expand", "more"),
                    ]
                )

            def compactOnboarding() -> str:
                return theme.fg(
                    "dim",
                    f"Press {keyText('app.tools.expand')} to show full startup help"
                    f"{' and loaded resources' if showDetails else ''}.",
                )

            def onboarding() -> str:
                # MISAKA fork: the window's tagline (`MISAKA_TAGLINE`) in place of pi's self-help note.
                return theme.fg("dim", os.environ.get("MISAKA_TAGLINE") or "MISAKA research system ready.")

            header = ExpandableText(
                lambda: f"{withLogo(compactInstructions())}\n{compactOnboarding()}\n\n{onboarding()}",
                lambda: f"{withLogo(expandedInstructions())}\n\n{onboarding()}",
                self.getStartupExpansionState(),
                1,
                0,
            )
            self.builtInHeader = header

            # Setup UI layout
            self.headerContainer.addChild(Spacer(1))
            self.headerContainer.addChild(self.builtInHeader)
            self.headerContainer.addChild(Spacer(1))
        else:
            # Minimal header when silenced
            self.builtInHeader = Text("", 0, 0)
            self.headerContainer.addChild(self.builtInHeader)
        self.ui.requestRender()

        # Ensure fd and rg are available after mounting the TUI.
        # Both are needed: fd for autocomplete, rg for grep tool and bash commands.
        # MISAKA fork: never downloaded (see tools_manager); a missing one is reported.
        fdPath, _rgPath = await asyncio.gather(
            ensureTool("fd", lambda status: self.showManagedToolStatus(status)),
            ensureTool("rg", lambda status: self.showManagedToolStatus(status)),
        )
        self.fdPath = fdPath

        # Enable the remaining input handlers only after managed-tool setup completes.
        self.setupKeyHandlers()
        self.setupEditorSubmitHandler()
        self.ui.requestRender()

        # Initialize extensions first so resources are shown before messages
        await self.rebindCurrentSession()

        # Render initial messages AFTER showing loaded resources
        self.renderInitialMessages()

        # Set up theme file watcher
        def themeChanged() -> None:
            self.ui.invalidate()
            self.updateEditorBorderColor()
            self.ui.requestRender()

        onThemeChange(themeChanged)

        # Set up git branch watcher (uses provider instead of footer)
        self.footerDataProvider.onBranchChange(lambda: self.ui.requestRender())

        # Initialize available provider count for footer display
        self.updateAvailableProviderCount()

        # Flush the completed startup state.
        self.ui.renderNow()

    def updateTerminalTitle(self) -> None:
        """Update terminal title with session name and cwd."""
        cwdBasename = os.path.basename(self.sessionManager.getCwd())
        sessionName = self.sessionManager.getSessionName()
        if sessionName:
            self.ui.terminal.setTitle(f"{APP_TITLE} - {sessionName} - {cwdBasename}")
        else:
            self.ui.terminal.setTitle(f"{APP_TITLE} - {cwdBasename}")

    async def run(self) -> int:
        """Run the interactive mode. This is the main entry point.
        Initializes the UI, shows warnings, processes initial messages, and starts the interactive loop.

        MISAKA fork: returns the exit code (the engine exits with it) once `shutdown` finishes, instead of exiting the
        process from inside.
        """
        await self.init()
        self._shutdownFuture = asyncio.get_running_loop().create_future()
        if self.shutdownRequested:
            await self.checkShutdownRequested()
            return await self._shutdownFuture

        # MISAKA fork: offline mode is `MISAKA_OFFLINE`, where upstream reads `PI_OFFLINE`.
        if not os.environ.get("MISAKA_OFFLINE"):
            controller = AbortController()
            timeout = setTimeout(lambda: controller.abort(), 15_000)

            async def refreshCatalogs() -> None:
                try:
                    await refreshModelCatalogs(self.session.modelRuntime, controller.signal)
                    self.updateAvailableProviderCount()
                except Exception:  # noqa: BLE001, S110 - upstream `.catch(() => {})`
                    pass
                finally:
                    timeout.cancel()

            self._schedule_task(refreshCatalogs())

        # MISAKA fork: no pi version check and no package update check (MISAKA updates through `misaka update` and has
        # no package sources).

        # Check tmux keyboard setup asynchronously
        async def tmuxCheck() -> None:
            warning = await self.checkTmuxKeyboardSetup()
            if warning:
                self.showWarning(warning)

        self._schedule_task(tmuxCheck())

        # Show startup warnings
        startupDiagnostics = self.options.startupDiagnostics
        modelFallbackMessage = self.options.modelFallbackMessage

        for diagnostic in startupDiagnostics or []:
            diagnosticType = read_field(diagnostic, "type")
            message = read_field(diagnostic, "message")
            if diagnosticType == "error":
                self.showError(message)
            elif diagnosticType == "warning":
                self.showWarning(message)
            else:
                self.showStatus(message)

        modelsJsonError = self.session.modelRuntime.getError()
        if modelsJsonError:
            self.showError(f"models.json error: {modelsJsonError}")

        if modelFallbackMessage:
            self.showWarning(modelFallbackMessage)

        # MISAKA fork: no crash notice (MISAKA keeps no crash log for `/bug`).

        self._schedule_task(self.maybeWarnAboutAnthropicSubscriptionAuth())

        # Process initial messages
        await self._promptInitialMessages()

        # Main interactive loop
        self._schedule_task(self._runInputLoop())

        return await self._shutdownFuture

    async def _promptInitialMessages(self) -> None:
        """MISAKA fork: pi sends these bare, and nothing else starts a turn during its startup. Here a session's
        inbox may start one with ``triggerTurn`` at bind (a card's waits for this message), so a message could arrive
        mid-turn; ``prompt()`` then refused it ("Agent is already processing") and it was lost (2026-09-23).
        ``steer`` puts it in at that run's next tool boundary. ``followUp`` held it until the whole run was over,
        which for a card was after she had submitted (2026-10-24, R14). An idle session is unchanged."""
        initialMessage = self.options.initialMessage
        if initialMessage:
            try:
                await self.session.prompt(
                    initialMessage, {"images": self.options.initialImages, "streamingBehavior": "steer"}
                )
            except Exception as error:  # noqa: BLE001 - upstream `catch (error)` shows any failure
                self.showError(errorMessage(error))

        for message in self.options.initialMessages or []:
            try:
                await self.session.prompt(message, {"streamingBehavior": "steer"})
            except Exception as error:  # noqa: BLE001 - upstream `catch (error)` shows any failure
                self.showError(errorMessage(error))

    async def _runInputLoop(self) -> None:
        """MISAKA fork: the main loop as a task, so `run` can return the exit code."""
        while self._shutdownFuture is not None and not self._shutdownFuture.done():
            try:
                userInput = await self.getUserInput()
            except asyncio.CancelledError:
                return
            if self._shutdownFuture is not None and self._shutdownFuture.done():
                return
            try:
                # MISAKA fork: the input was taken while the agent was idle, but a research driver or a notification
                # can start a turn in between (pi has no such turn starters). Without a behaviour, prompt() raised
                # "Agent is already processing" and the typed message was lost; steer is what Enter would have queued
                # had the turn started a moment earlier. Idle is unchanged.
                await self.session.prompt(userInput, {"streamingBehavior": "steer"})
            except Exception as error:  # noqa: BLE001 - upstream `catch (error)` shows any failure
                self.showError(errorMessage(error))

    # MISAKA fork: no `checkForPackageUpdates` (MISAKA has no package sources).

    async def checkTmuxKeyboardSetup(self) -> str | None:
        if not os.environ.get("TMUX"):
            return None

        async def runTmuxShow(option: str) -> str | None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "tmux",
                    "show",
                    "-gv",
                    option,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError:
                return None

            try:
                stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return None

            return jsTrim(stdout.decode("utf-8", errors="replace")) if proc.returncode == 0 else None

        extendedKeys, extendedKeysFormat = await asyncio.gather(
            runTmuxShow("extended-keys"),
            runTmuxShow("extended-keys-format"),
        )

        # If we couldn't query tmux (timeout, sandbox, etc.), don't warn
        if extendedKeys is None:
            return None

        if extendedKeys != "on" and extendedKeys != "always":
            return (
                "tmux extended-keys is off. Modified Enter keys may not work. Add `set -g extended-keys on` to "
                "~/.tmux.conf and restart tmux."
            )

        if extendedKeysFormat == "xterm":
            # MISAKA fork: the product name.
            return (
                "tmux extended-keys-format is xterm. MISAKA works best with csi-u. Add `set -g extended-keys-format "
                "csi-u` to ~/.tmux.conf and restart tmux."
            )

        return None

    # MISAKA fork: no `getChangelogForDisplay` or `reportInstallTelemetry` (no changelog ships, and MISAKA pings
    # nothing).

    def getMarkdownThemeWithSettings(self) -> Any:
        return dataclasses_replace(getMarkdownTheme(), codeBlockIndent=self.settingsManager.getCodeBlockIndent())

    # =========================================================================
    # Extension System
    # =========================================================================

    def formatDisplayPath(self, p: str) -> str:
        homeDir = os.path.expanduser("~")
        result = p

        # Replace home directory with ~
        if result.startswith(homeDir):
            result = f"~{result[len(homeDir):]}"

        return result

    def formatExtensionDisplayPath(self, path: str) -> str:
        result = self.formatDisplayPath(path)
        # MISAKA fork: MISAKA extensions are Python packages, so `/index.py` is stripped as well.
        for suffix in ("/index.ts", "/index.js", "/index.py"):
            result = result.removesuffix(suffix)
        return result

    def formatContextPath(self, p: str) -> str:
        cwd = os.path.abspath(self.sessionManager.getCwd())
        absolutePath = os.path.abspath(p) if os.path.isabs(p) else os.path.abspath(os.path.join(cwd, p))
        relativePath = getCwdRelativePath(absolutePath, cwd)
        if relativePath is not None:
            return relativePath

        return self.formatDisplayPath(absolutePath)

    def getStartupExpansionState(self) -> bool:
        return bool(self.options.verbose or self.toolOutputExpanded)

    def shouldShowStartupHeader(self) -> bool:
        """Startup header (logo, version, key hints). Hidden only by quietStartup: true."""
        return self.options.verbose is True or self.settingsManager.getQuietStartup() is not True

    def shouldShowStartupDetails(self) -> bool:
        """Startup details (model scope, loaded resources). Hidden by quietStartup: true or "header"."""
        return self.options.verbose is True or self.settingsManager.getQuietStartup() is False

    def getShortPath(self, fullPath: str, sourceInfo: Any = None) -> str:
        """Get a short path relative to the package root for display."""
        normalizedFullPath = fullPath.replace("\\", "/")
        baseDir = read_field(sourceInfo, "baseDir")
        if baseDir and self.isPackageSource(sourceInfo):
            normalizedBaseDir = str(baseDir).replace("\\", "/")
            npmRootMatch = re.match(r"^(.*/node_modules)/(@?[^/]+(?:/[^/]+)?)$", normalizedBaseDir)
            # If fullPath is under the same node_modules root as baseDir, preserve that relative topology.
            if npmRootMatch and npmRootMatch.group(1) and normalizedFullPath.startswith(f"{npmRootMatch.group(1)}/"):
                return posixpath.relpath(normalizedFullPath, normalizedBaseDir)

            try:
                relativePath = os.path.relpath(os.path.abspath(fullPath), os.path.abspath(str(baseDir)))
            except ValueError:
                relativePath = ""
            if (
                relativePath
                and relativePath != "."
                and not relativePath.startswith("..")
                and not relativePath.startswith(f"..{os.sep}")
                and not os.path.isabs(relativePath)
            ):
                return relativePath.replace("\\", "/")

        source = str(read_field(sourceInfo, "source", ""))
        npmMatch = re.search(r"node_modules/(@?[^/]+(?:/[^/]+)?)/(.*)", normalizedFullPath)
        if npmMatch and source.startswith("npm:"):
            return npmMatch.group(2)

        gitMatch = re.search(r"git/[^/]+/[^/]+/(.*)", normalizedFullPath)
        if gitMatch and source.startswith("git:"):
            return gitMatch.group(1)

        return self.formatDisplayPath(fullPath)

    def getCompactPathLabel(self, resourcePath: str, sourceInfo: Any = None) -> str:
        shortPath = self.getShortPath(resourcePath, sourceInfo)
        normalizedPath = shortPath.replace("\\", "/")
        segments = [segment for segment in normalizedPath.split("/") if len(segment) > 0 and segment != "~"]
        if len(segments) > 0:
            return segments[-1]
        return shortPath

    def getCompactPackageSourceLabel(self, sourceInfo: Any = None) -> str:
        source = str(read_field(sourceInfo, "source", ""))
        if source.startswith("npm:"):
            return source[len("npm:") :] or source

        # MISAKA fork: no git URL parsing (MISAKA has no git package sources).
        return source

    def getCompactExtensionLabel(self, resourcePath: str, sourceInfo: Any = None) -> str:
        if not self.isPackageSource(sourceInfo):
            return self.getCompactPathLabel(resourcePath, sourceInfo)

        sourceLabel = self.getCompactPackageSourceLabel(sourceInfo)
        if not sourceLabel:
            return self.getCompactPathLabel(resourcePath, sourceInfo)

        shortPath = self.getShortPath(resourcePath, sourceInfo).replace("\\", "/")
        packagePath = shortPath.removeprefix("extensions/")
        parsedDir = posixpath.dirname(packagePath)
        parsedName = posixpath.splitext(posixpath.basename(packagePath))[0]

        if parsedName == "index":
            return sourceLabel if not parsedDir or parsedDir == "." else f"{sourceLabel}:{parsedDir}"

        return f"{sourceLabel}:{packagePath}"

    def getCompactDisplayPathSegments(self, resourcePath: str) -> list[str]:
        return [
            segment
            for segment in self.formatDisplayPath(resourcePath).replace("\\", "/").split("/")
            if len(segment) > 0 and segment != "~"
        ]

    def getCompactNonPackageExtensionLabel(self, resourcePath: str, index: int, allPaths: list[dict[str, Any]]) -> str:
        item = jsIndex(allPaths, index)
        segments = item["segments"] if item else None
        if not segments or len(segments) == 0:
            return self.getCompactPathLabel(resourcePath)

        for segmentCount in range(1, len(segments) + 1):
            candidate = "/".join(segments[-segmentCount:])
            isUnique = all(
                itemIndex == index or "/".join(other["segments"][-segmentCount:]) != candidate
                for itemIndex, other in enumerate(allPaths)
            )

            if isUnique:
                return candidate

        return "/".join(segments)

    def getCompactExtensionLabels(self, extensions: list[dict[str, Any]]) -> list[str]:
        nonPackageExtensions = []
        for extension in extensions:
            segments = self.getCompactDisplayPathSegments(extension["path"])
            lastSegment = segments[-1] if segments else None
            # MISAKA fork: `index.py` too.
            if len(segments) > 1 and lastSegment in ("index.ts", "index.js", "index.py"):
                segments.pop()
            if not self.isPackageSource(extension.get("sourceInfo")):
                nonPackageExtensions.append(
                    {"path": extension["path"], "sourceInfo": extension.get("sourceInfo"), "segments": segments}
                )

        labels = []
        for extension in extensions:
            if self.isPackageSource(extension.get("sourceInfo")):
                labels.append(self.getCompactExtensionLabel(extension["path"], extension.get("sourceInfo")))
                continue

            nonPackageIndex = next(
                (index for index, item in enumerate(nonPackageExtensions) if item["path"] == extension["path"]), -1
            )
            if nonPackageIndex == -1:
                labels.append(self.getCompactPathLabel(extension["path"], extension.get("sourceInfo")))
                continue

            labels.append(
                self.getCompactNonPackageExtensionLabel(extension["path"], nonPackageIndex, nonPackageExtensions)
            )
        return labels

    def getDisplaySourceInfo(self, sourceInfo: Any = None) -> dict[str, Any]:
        source = read_field(sourceInfo, "source", "local")
        scope = read_field(sourceInfo, "scope", "project")
        if source == "local":
            if scope == "user":
                return {"label": "user", "color": "muted"}
            if scope == "project":
                return {"label": "project", "color": "muted"}
            if scope == "temporary":
                return {"label": "path", "scopeLabel": "temp", "color": "muted"}
            return {"label": "path", "color": "muted"}

        if source == "cli":
            return {"label": "path", "scopeLabel": "temp" if scope == "temporary" else None, "color": "muted"}

        scopeLabel = (
            "user" if scope == "user" else "project" if scope == "project" else "temp" if scope == "temporary" else None
        )
        return {"label": source, "scopeLabel": scopeLabel, "color": "accent"}

    def getScopeGroup(self, sourceInfo: Any = None) -> Literal["user", "project", "path"]:
        source = read_field(sourceInfo, "source", "local")
        scope = read_field(sourceInfo, "scope", "project")
        if source == "cli" or scope == "temporary":
            return "path"
        if scope == "user":
            return "user"
        if scope == "project":
            return "project"
        return "path"

    def isPackageSource(self, sourceInfo: Any = None) -> bool:
        source = str(read_field(sourceInfo, "source", ""))
        return source.startswith(("npm:", "git:"))

    def buildScopeGroups(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {
            "user": {"scope": "user", "paths": [], "packages": {}},
            "project": {"scope": "project", "paths": [], "packages": {}},
            "path": {"scope": "path", "paths": [], "packages": {}},
        }

        for item in items:
            groupKey = self.getScopeGroup(item.get("sourceInfo"))
            group = groups[groupKey]
            source = read_field(item.get("sourceInfo"), "source", "local")

            if self.isPackageSource(item.get("sourceInfo")):
                group["packages"].setdefault(source, []).append(item)
            else:
                group["paths"].append(item)

        return [
            group
            for group in (groups["project"], groups["user"], groups["path"])
            if len(group["paths"]) > 0 or len(group["packages"]) > 0
        ]

    def formatScopeGroups(self, groups: list[dict[str, Any]], options: dict[str, Callable[..., str]]) -> str:
        lines: list[str] = []

        def byPath(a: dict[str, Any], b: dict[str, Any]) -> int:
            return localeCompare(a["path"], b["path"])

        for group in groups:
            lines.append(f"  {theme.fg('accent', group['scope'])}")

            sortedPaths = sorted(group["paths"], key=functools.cmp_to_key(byPath))
            for item in sortedPaths:
                lines.append(theme.fg("dim", f"    {options['formatPath'](item)}"))

            def bySource(a: tuple[str, Any], b: tuple[str, Any]) -> int:
                return localeCompare(a[0], b[0])

            sortedPackages = sorted(group["packages"].items(), key=functools.cmp_to_key(bySource))
            for source, packageItems in sortedPackages:
                lines.append(f"    {theme.fg('mdLink', source)}")
                for item in sorted(packageItems, key=functools.cmp_to_key(byPath)):
                    lines.append(theme.fg("dim", f"      {options['formatPackagePath'](item, source)}"))

        return "\n".join(lines)

    def findSourceInfoForPath(self, p: str, sourceInfos: dict[str, Any]) -> Any:
        exact = sourceInfos.get(p)
        if exact:
            return exact

        current = p
        while "/" in current:
            current = current[: current.rfind("/")]
            parent = sourceInfos.get(current)
            if parent:
                return parent

        return None

    def formatPathWithSource(self, p: str, sourceInfo: Any = None) -> str:
        if sourceInfo:
            shortPath = self.getShortPath(p, sourceInfo)
            info = self.getDisplaySourceInfo(sourceInfo)
            scopeLabel = info.get("scopeLabel")
            labelText = f"{info['label']} ({scopeLabel})" if scopeLabel else info["label"]
            return f"{labelText} {shortPath}"
        return self.formatDisplayPath(p)

    def formatDiagnostics(self, diagnostics: list[Any], sourceInfos: dict[str, Any]) -> str:
        lines: list[str] = []

        # Group collision diagnostics by name
        collisions: dict[str, list[Any]] = {}
        otherDiagnostics: list[Any] = []

        for d in diagnostics:
            collision = read_field(d, "collision")
            if read_field(d, "type") == "collision" and collision:
                collisions.setdefault(read_field(collision, "name"), []).append(d)
            else:
                otherDiagnostics.append(d)

        # Format collision diagnostics grouped by name
        for name, collisionList in collisions.items():
            first = read_field(collisionList[0], "collision") if collisionList else None
            if not first:
                continue
            winnerPath = read_field(first, "winnerPath")
            lines.append(theme.fg("warning", f'  "{name}" collision:'))
            lines.append(
                theme.fg(
                    "dim",
                    f"    {theme.fg('success', '✓')} "
                    f"{self.formatPathWithSource(winnerPath, self.findSourceInfoForPath(winnerPath, sourceInfos))}",
                )
            )
            for d in collisionList:
                collision = read_field(d, "collision")
                if collision:
                    loserPath = read_field(collision, "loserPath")
                    lines.append(
                        theme.fg(
                            "dim",
                            f"    {theme.fg('warning', '✗')} "
                            f"{self.formatPathWithSource(loserPath, self.findSourceInfoForPath(loserPath, sourceInfos))}"
                            " (skipped)",
                        )
                    )

        for d in otherDiagnostics:
            color: ThemeColor = "error" if read_field(d, "type") == "error" else "warning"
            path = read_field(d, "path")
            if path:
                formattedPath = self.formatPathWithSource(path, self.findSourceInfoForPath(path, sourceInfos))
                lines.append(theme.fg(color, f"  {formattedPath}"))
                lines.append(theme.fg(color, f"    {read_field(d, 'message')}"))
            else:
                lines.append(theme.fg(color, f"  {read_field(d, 'message')}"))

        return "\n".join(lines)

    def showLoadedResources(self, options: dict[str, Any] | None = None) -> None:
        # Resource rendering is idempotent; chat clears no longer clear this separate container.
        self.loadedResourcesContainer.clear()

        showListing = bool(read_field(options, "force")) or self.shouldShowStartupDetails()
        showDiagnostics = showListing or read_field(options, "showDiagnosticsWhenQuiet") is True
        if not showListing and not showDiagnostics:
            return

        def sectionHeader(name: str, color: str = "mdHeading") -> str:
            # MISAKA fork: section titles use MISAKA's `sectionTitle` theme token (falls back to accent).
            return theme.fg("sectionTitle", f"[{name}]")

        def formatCompactList(items: list[str], sort: bool = True) -> str:
            labels = [label for label in (jsTrim(item) for item in items) if len(label) > 0]
            if sort:
                labels.sort(key=functools.cmp_to_key(localeCompare))
            return theme.fg("dim", f"  {', '.join(labels)}")

        # Bodies are built on demand so the listing follows theme changes.
        def addLoadedSection(
            name: str,
            collapsedBody: Callable[[], str],
            expandedBody: Callable[[], str] | None = None,
            color: str = "mdHeading",
        ) -> None:
            expanded = expandedBody if expandedBody is not None else collapsedBody
            section = ExpandableText(
                lambda: f"{sectionHeader(name, color)}\n{collapsedBody()}",
                lambda: f"{sectionHeader(name, color)}\n{expanded()}",
                self.getStartupExpansionState(),
                0,
                0,
            )
            self.loadedResourcesContainer.addChild(section)
            self.loadedResourcesContainer.addChild(Spacer(1))

        # MISAKA fork: no skills from the resource loader; MISAKA's skills are core commands and list themselves through
        # `startup_sections`.
        promptsResult = self.session.resourceLoader.getPrompts()
        themesResult = self.session.resourceLoader.getThemes()
        extensions = read_field(options, "extensions")
        if extensions is None:
            extensions = [
                {"path": extension.path, "sourceInfo": extension.sourceInfo}
                for extension in self.session.resourceLoader.getExtensions().extensions
                if not getattr(extension, "hidden", False)
            ]
        sourceInfos: dict[str, Any] = {}
        for extension in extensions:
            if extension.get("sourceInfo"):
                sourceInfos[extension["path"]] = extension["sourceInfo"]
        for prompt in read_field(promptsResult, "prompts", []):
            if prompt.sourceInfo:
                sourceInfos[prompt.filePath] = prompt.sourceInfo
        for loadedTheme in read_field(themesResult, "themes", []):
            sourcePath = getattr(loadedTheme, "sourcePath", None)
            sourceInfo = getattr(loadedTheme, "sourceInfo", None)
            if sourcePath and sourceInfo:
                sourceInfos[sourcePath] = sourceInfo

        if showListing:
            systemPromptSource = self.session.resourceLoader.getSystemPromptSource()
            contextFiles = [
                *([systemPromptSource] if systemPromptSource else []),
                *self.session.resourceLoader.getAppendSystemPromptSources(),
                *read_field(self.session.resourceLoader.getAgentsFiles(), "agentsFiles", []),
            ]
            if len(contextFiles) > 0:
                self.loadedResourcesContainer.addChild(Spacer(1))

                def contextList() -> str:
                    return "\n".join(
                        theme.fg("dim", f"  {self.formatDisplayPath(read_field(f, 'path'))}") for f in contextFiles
                    )

                def contextCompactList() -> str:
                    return formatCompactList(
                        [self.formatContextPath(read_field(contextFile, "path")) for contextFile in contextFiles],
                        sort=False,
                    )

                addLoadedSection("Context", contextCompactList, contextList)

            templates = self.session.promptTemplates
            if len(templates) > 0:
                templateGroups = self.buildScopeGroups(
                    [{"path": template.filePath, "sourceInfo": template.sourceInfo} for template in templates]
                )
                templateByPath = {t.filePath: t for t in templates}

                def formatTemplatePath(item: dict[str, Any], _source: str | None = None) -> str:
                    template = templateByPath.get(item["path"])
                    return f"/{template.name}" if template else self.formatDisplayPath(item["path"])

                def templateList() -> str:
                    return self.formatScopeGroups(
                        templateGroups, {"formatPath": formatTemplatePath, "formatPackagePath": formatTemplatePath}
                    )

                def promptCompactList() -> str:
                    return formatCompactList([f"/{template.name}" for template in templates])

                addLoadedSection("Prompts", promptCompactList, templateList)

            if len(extensions) > 0:
                extensionGroups = self.buildScopeGroups(extensions)

                def extList() -> str:
                    return self.formatScopeGroups(
                        extensionGroups,
                        {
                            "formatPath": lambda item: self.formatExtensionDisplayPath(item["path"]),
                            "formatPackagePath": lambda item, _source: self.formatExtensionDisplayPath(
                                self.getShortPath(item["path"], item.get("sourceInfo"))
                            ),
                        },
                    )

                extensionLabels = self.getCompactExtensionLabels(extensions)
                addLoadedSection("Extensions", lambda: formatCompactList(extensionLabels), extList, "mdHeading")

            # MISAKA fork: sections registered by extensions and core parts (MISAKA's [Skills] among them). Their text
            # may be a callable, evaluated on every render, so an asynchronously loaded resource updates by itself.
            for registered in list(startup_sections.SECTIONS):
                registeredName = str(registered.get("name") or "")
                if not registeredName:
                    continue

                def collapsedSection(section: dict[str, Any] = registered) -> str:
                    return startup_sections.resolve(section.get("collapsed"))

                def expandedSection(section: dict[str, Any] = registered) -> str:
                    return startup_sections.resolve(section.get("expanded") or section.get("collapsed"))

                addLoadedSection(registeredName, collapsedSection, expandedSection)

        if showDiagnostics:
            # MISAKA fork: no [Skill conflicts] (no skill resources in the loader).
            promptDiagnostics = read_field(promptsResult, "diagnostics", [])
            if len(promptDiagnostics) > 0:
                self.loadedResourcesContainer.addChild(
                    ThemedText(
                        lambda: f"{theme.fg('warning', '[Prompt conflicts]')}\n"
                        f"{self.formatDiagnostics(promptDiagnostics, sourceInfos)}",
                        0,
                        0,
                    )
                )
                self.loadedResourcesContainer.addChild(Spacer(1))

            extensionDiagnostics: list[Any] = []
            extensionsResult = self.session.resourceLoader.getExtensions()
            for error in extensionsResult.errors:
                extensionDiagnostics.append(
                    {"type": "error", "message": read_field(error, "error"), "path": read_field(error, "path")}
                )
            for warning in extensionsResult.warnings or []:
                extensionDiagnostics.append(
                    {"type": "warning", "message": read_field(warning, "warning"), "path": read_field(warning, "path")}
                )

            commandDiagnostics = self.session.extensionRunner.getCommandDiagnostics()
            extensionDiagnostics.extend(commandDiagnostics)
            extensionDiagnostics.extend(self.getBuiltInCommandConflictDiagnostics(self.session.extensionRunner))

            shortcutDiagnostics = self.session.extensionRunner.getShortcutDiagnostics()
            extensionDiagnostics.extend(shortcutDiagnostics)

            if len(extensionDiagnostics) > 0:
                self.loadedResourcesContainer.addChild(
                    ThemedText(
                        lambda: f"{theme.fg('warning', '[Extension issues]')}\n"
                        f"{self.formatDiagnostics(extensionDiagnostics, sourceInfos)}",
                        0,
                        0,
                    )
                )
                self.loadedResourcesContainer.addChild(Spacer(1))

            themeDiagnostics = read_field(themesResult, "diagnostics", [])
            if len(themeDiagnostics) > 0:
                self.loadedResourcesContainer.addChild(
                    ThemedText(
                        lambda: f"{theme.fg('warning', '[Theme conflicts]')}\n"
                        f"{self.formatDiagnostics(themeDiagnostics, sourceInfos)}",
                        0,
                        0,
                    )
                )
                self.loadedResourcesContainer.addChild(Spacer(1))

    async def bindCurrentSessionExtensions(self) -> None:
        """Initialize the extension system with TUI-based UI context."""
        uiContext = self.createExtensionUIContext()

        async def newSession(options: Any = None) -> Any:
            self.clearStatusIndicator()
            try:
                return await self.runtimeHost.newSession(options)
            except ContextCarryUnsupportedError as error:
                # MISAKA fork: an extension asked to carry the context over (`carryOverContext`) and the context
                # engine refused; the current session is intact, so this is reported rather than fatal.
                self.showError(str(error))
                return {"cancelled": True}
            except Exception as error:  # noqa: BLE001 - upstream catches everything
                return await self.handleFatalRuntimeError("Failed to create session", error)

        async def fork(entryId: str, options: Any = None) -> Any:
            try:
                result = await self.runtimeHost.fork(entryId, options)
                if not read_field(result, "cancelled"):
                    self.editor.setText(read_field(result, "selectedText") or "")
                    self.showStatus("Forked to new session")
                return {"cancelled": read_field(result, "cancelled")}
            except Exception as error:  # noqa: BLE001 - upstream catches everything
                return await self.handleFatalRuntimeError("Failed to fork session", error)

        async def navigateTree(targetId: str, options: Any = None) -> Any:
            result = await self.session.navigateTree(
                targetId,
                {
                    "summarize": read_field(options, "summarize"),
                    "customInstructions": read_field(options, "customInstructions"),
                    "replaceInstructions": read_field(options, "replaceInstructions"),
                    "label": read_field(options, "label"),
                },
            )
            if read_field(result, "cancelled"):
                return {"cancelled": True}

            self.chatContainer.clear()
            self.renderInitialMessages()
            editorText = read_field(result, "editorText")
            if editorText and not jsTrim(self.editor.getText()):
                self.editor.setText(editorText)
            self.showStatus("Navigated to selected point")
            self._schedule_task(self.flushCompactionQueue({"willRetry": False}))
            return {"cancelled": False}

        async def switchSession(sessionPath: str, options: Any = None) -> Any:
            return await self.handleResumeSession(sessionPath, options)

        async def reload() -> None:
            await self.handleReloadCommand()

        def shutdownHandler() -> None:
            self.shutdownRequested = True
            # MISAKA fork: until `run` has created the exit-code future, `run` performs the requested shutdown itself.
            if self.session.isIdle and self._shutdownFuture is not None:
                self._schedule_task(self.shutdown())

        await self.session.bindExtensions(
            {
                "uiContext": uiContext,
                "mode": "tui",
                "abortHandler": lambda: self.restoreQueuedMessagesToEditor({"abort": True}),
                "commandContextActions": {
                    "waitForIdle": lambda: self.session.waitForIdle(),
                    "newSession": newSession,
                    "fork": fork,
                    "navigateTree": navigateTree,
                    "switchSession": switchSession,
                    "reload": reload,
                },
                "shutdownHandler": shutdownHandler,
                "onError": lambda error: self.showExtensionError(
                    read_field(error, "extensionPath"), read_field(error, "error"), read_field(error, "stack")
                ),
            }
        )

        setRegisteredThemes(read_field(self.session.resourceLoader.getThemes(), "themes", []))
        self.setupAutocompleteProvider()

        extensionRunner = self.session.extensionRunner
        self.setupExtensionShortcuts(extensionRunner)
        self.showLoadedResources({"force": False, "showDiagnosticsWhenQuiet": True})
        # MISAKA fork: no `showStartupNoticesIfNeeded` (no changelog ships).

    # MISAKA fork (D1): no `applyFullscreenScrollbarSetting`.

    def ensurePngTranscoder(self) -> None:
        """Lets extension images use the PNG transcoder; tool results register it themselves."""

        def onRegistered() -> None:
            self.ui.invalidate()
            self.ui.requestRender()

        ensurePngTranscoder(onRegistered)

    def applyRuntimeSettings(self) -> None:
        setCapabilityOverrides(self.settingsManager.getTerminalCapabilityOverrides())
        self.ensurePngTranscoder()
        configureHttpDispatcher(self.settingsManager.getHttpIdleTimeoutMs())
        self.footer.setSession(self.session)
        self.footer.setAutoCompactEnabled(self.session.autoCompactionEnabled)
        self.footerDataProvider.setCwd(self.sessionManager.getCwd())
        self.hideThinkingBlock = self.settingsManager.getHideThinkingBlock()
        self.outputPad = self.settingsManager.getOutputPad()
        self.ui.setShowHardwareCursor(self.settingsManager.getShowHardwareCursor())
        clearOnShrink = self.settingsManager.getClearOnShrink()
        self.ui.setClearOnShrink(clearOnShrink)
        if not clearOnShrink and not self.activeStatusIndicator:
            self.statusContainer.clear()
        editorPaddingX = self.settingsManager.getEditorPaddingX()
        autocompleteMaxVisible = self.settingsManager.getAutocompleteMaxVisible()
        self.defaultEditor.setPaddingX(editorPaddingX)
        self.defaultEditor.setAutocompleteMaxVisible(autocompleteMaxVisible)
        if self.editor is not self.defaultEditor:
            setPaddingX = getattr(self.editor, "setPaddingX", None)
            if callable(setPaddingX):
                setPaddingX(editorPaddingX)
            setAutocompleteMaxVisible = getattr(self.editor, "setAutocompleteMaxVisible", None)
            if callable(setAutocompleteMaxVisible):
                setAutocompleteMaxVisible(autocompleteMaxVisible)

    async def rebindCurrentSession(self, options: dict[str, Any] | None = None) -> None:
        if options is None:
            options = {}
        session = self.session

        if self.unsubscribe is not None:
            self.unsubscribe()
        self.unsubscribe = None
        self.programStatus.reset()
        self.applyRuntimeSettings()

        if options.get("renderBeforeBind"):
            self.renderCurrentSessionState()
            self.subscribeToAgent()

        await self.bindCurrentSessionExtensions()

        if self.session is not session:
            return

        if not options.get("renderBeforeBind"):
            self.subscribeToAgent()

        self.updateAvailableProviderCount()
        self.updateEditorBorderColor()
        self.updateTerminalTitle()

    async def handleFatalRuntimeError(self, prefix: str, error: Any) -> NoReturn:
        message = errorMessage(error)
        self.showError(f"{prefix}: {message}")
        extensionHint = self.getCrashExtensionHint(error)
        if extensionHint:
            self.chatContainer.addChild(ThemedText(lambda: theme.fg("warning", extensionHint), self.outputPad, 0))
        # MISAKA fork: no crash record or `/bug` instructions (MISAKA has no `/bug`).
        stopThemeWatcher()
        self.stop()
        # MISAKA fork: `SystemExit` in place of `process.exit(1)`.
        raise SystemExit(1)

    def getCrashExtensionHint(self, error: Any) -> str | None:
        try:
            # MISAKA fork: the stack is the Python traceback.
            stack = (
                "".join(traceback.format_exception(error)) if isinstance(error, BaseException) else None
            )
            return formatCrashExtensionHint(
                findExtensionStackMatches(stack, self.session.resourceLoader.getExtensions().extensions)
            )
        except Exception:  # noqa: BLE001 - upstream `catch { return undefined }`
            return None

    # MISAKA fork: no `recordCrash`, `crashReportInstructions` or `suggestBugReport` (MISAKA has no `/bug`).

    def maybeSuggestBugReport(self, message: Any) -> None:
        """MISAKA fork: with no `/bug` to suggest, an unexpected error only checks whether the install changed."""
        if read_field(message, "stopReason") != "error" or isRetryableAssistantError(message):
            return
        if re.search(r"\b(?:abort(?:ed)?|cancel(?:l?ed)?)\b", read_field(message, "errorMessage") or "", re.IGNORECASE):
            return
        self.maybeShowInstallChangeWarning()

    def maybeShowInstallChangeWarning(self) -> bool:
        """After an error, check whether an update replaced or removed this install while the session ran.
        Code loaded on demand then fails with missing modules until restart (#10439). Returns true when
        the install changed.
        """
        if self.installChangeWarningShown:
            return True
        change = detectInstallChange()
        if not change:
            return False
        self.installChangeWarningShown = True
        cause = (
            f"{APP_NAME} was updated to {change['version']} while this session was running ({VERSION})"
            if change["kind"] == "updated"
            else f"The {APP_NAME} installation this session runs from was removed or replaced"
        )
        resumeCommand = formatResumeCommand(self.sessionManager)
        restart = f"Restart with `{resumeCommand}` to continue this session." if resumeCommand else f"Restart {APP_NAME}."
        self.showWarning(f"{cause}. Features that load code on demand can fail until restart. {restart}")
        return True

    def renderCurrentSessionState(self) -> None:
        self.loadedResourcesContainer.clear()
        self.chatContainer.clear()
        self.pendingMessagesContainer.clear()
        self.compactionQueuedMessages = []
        # MISAKA fork: a `!` command run while the agent streams keeps its block in the pending area, and its message
        # waits in the session (not in the transcript redrawn below) until the run settles. `/reload` and the turn-end
        # custom-message flush redraw mid-run, so the blocks go back into the pending area; once the session has
        # nothing pending, the session itself changed (`/new`, `/resume`, fork) and they are dropped.
        if self.session.hasPendingBashMessages:
            for component in self.pendingBashComponents:
                self.pendingMessagesContainer.addChild(component)
        else:
            self.pendingBashComponents = []
        self.streamingComponent = None
        self.streamingMessage = None
        self.pendingTools.clear()
        self.renderInitialMessages()

    def getRegisteredToolDefinition(self, toolName: str) -> Any:
        """Extension-registered definition, falling back to the built-in one. The renderer components take
        whatever this returns, so they never reach into the tool registry themselves.
        """
        return self.session.extensionRunner.resolveToolRenderers(
            toolName, lambda: withBuiltInRenderers(toolName, self.session.getToolDefinition(toolName))
        )

    def getMarkdownTransformers(self) -> list[MarkdownTransformer]:
        return [self.mermaidMarkdownTransformer, *self.session.extensionRunner.getMarkdownTransformers()]

    def setupExtensionShortcuts(self, extensionRunner: Any) -> None:
        """Set up keyboard shortcuts registered by extensions."""
        shortcuts = extensionRunner.getShortcuts(self.keybindings.getEffectiveConfig())
        if len(shortcuts) == 0:
            return

        # Create a context for shortcut handlers
        def createContext() -> Any:
            def shutdown() -> None:
                self.shutdownRequested = True

            def compact(options: Any = None) -> None:
                async def run() -> None:
                    try:
                        result = await self.session.compact(read_field(options, "customInstructions"))
                        onComplete = read_field(options, "onComplete")
                        if onComplete:
                            onComplete(result)
                    except Exception as error:  # noqa: BLE001 - reported to the caller
                        onError = read_field(options, "onError")
                        if onError:
                            onError(error)

                self._schedule_task(run())

            # MISAKA fork: a namespace stands in for the object literal.
            return SimpleNamespace(
                ui=self.createExtensionUIContext(),
                mode="tui",
                hasUI=True,
                cwd=self.sessionManager.getCwd(),
                sessionManager=self.sessionManager,
                modelRegistry=extensionRunner.modelRegistry,
                model=self.session.model,
                scopedModels=self.session.scopedModels,
                thinkingLevel=self.session.thinkingLevel,
                isIdle=lambda: self.session.isIdle,
                isProjectTrusted=lambda: self.settingsManager.isProjectTrusted(),
                signal=self.session.agent.signal,
                abort=lambda: self.restoreQueuedMessagesToEditor({"abort": True}),
                hasPendingMessages=lambda: self.session.pendingMessageCount > 0,
                shutdown=shutdown,
                getContextUsage=lambda: self.session.getContextUsage(),
                compact=compact,
                getSystemPrompt=lambda: self.session.systemPrompt,
            )

        async def runHandler(handler: Any) -> None:
            try:
                await maybe_await(handler(createContext()))
            except Exception as err:  # noqa: BLE001 - shown to the user
                self.showError(f"Shortcut handler error: {errorMessage(err)}")

        # Set up the extension shortcut handler on the default editor
        def onExtensionShortcut(data: str) -> bool:
            for shortcutStr, shortcut in shortcuts.items():
                # Cast to KeyId - extension shortcuts use the same format
                if matchesKey(data, shortcutStr):
                    # Run handler async, don't block input
                    self._schedule_task(runHandler(shortcut.handler))
                    return True
            return False

        self.defaultEditor.onExtensionShortcut = onExtensionShortcut

    def setExtensionStatus(self, key: str, text: str | None) -> None:
        """Set extension status text in the footer."""
        self.footerDataProvider.setExtensionStatus(key, text)
        self.ui.requestRender()

    def setEditorWorkingStatusIndicator(self, indicator: StatusIndicator | None) -> bool:
        self.defaultEditor.setWorkingStatusIndicator(None)
        if not isEmbeddedStatus(self.editor):
            return False
        self.editor.setWorkingStatusIndicator(indicator)
        return True

    def showStatusIndicator(self, indicator: StatusIndicator) -> None:
        if self.activeStatusIndicator is not None:
            self.activeStatusIndicator.dispose()
        self.activeStatusIndicator = indicator
        self.activeWorkingIndicatorEmbedded = False
        self.statusContainer.clear()
        self.setEditorWorkingStatusIndicator(None)
        if self.setEditorWorkingStatusIndicator(indicator):
            self.activeWorkingIndicatorEmbedded = True
            return
        self.statusContainer.addChild(indicator)

    def clearStatusIndicator(self, kind: str | None = None) -> None:
        if kind and (self.activeStatusIndicator.kind if self.activeStatusIndicator else None) != kind:
            return
        clearedIndicator = self.activeStatusIndicator
        clearedIndicatorWasEmbedded = self.activeWorkingIndicatorEmbedded
        if clearedIndicator is not None:
            clearedIndicator.dispose()
        self.activeStatusIndicator = None
        self.activeWorkingIndicatorEmbedded = False
        self.statusContainer.clear()
        self.setEditorWorkingStatusIndicator(None)
        # MISAKA fork (D1): the main screen is the only renderer, so pi's `tuiMode === "regular"` test always holds.
        if clearedIndicator and not clearedIndicatorWasEmbedded and self.ui.getClearOnShrink():
            self.statusContainer.addChild(self.idleStatus)

    def showWorkingStatusIndicator(self) -> None:
        colorFn = (
            (
                lambda text: (
                    getattr(self.editor, "borderColor", None)
                    or theme.getThinkingBorderColor(self.session.thinkingLevel or "off")
                )(text)
            )
            if isEmbeddedStatus(self.editor)
            else None
        )
        self.showStatusIndicator(
            WorkingStatusIndicator(
                self.ui,
                self.workingMessage if self.workingMessage is not None else self.defaultWorkingMessage,
                _toLoaderIndicatorOptions(self.workingIndicatorOptions),
                colorFn,
            )
        )

    def setWorkingVisible(self, visible: bool) -> None:
        self.workingVisible = visible
        if not visible:
            self.clearStatusIndicator("working")
            self.ui.requestRender()
            return
        if self.session.isStreaming and (self.activeStatusIndicator.kind if self.activeStatusIndicator else None) != (
            "working"
        ):
            self.showWorkingStatusIndicator()
        self.ui.requestRender()

    def setWorkingIndicator(self, options: WorkingIndicatorOptions | None = None) -> None:
        self.workingIndicatorOptions = options
        if self.activeStatusIndicator is not None and self.activeStatusIndicator.kind == "working":
            self.activeStatusIndicator.setIndicator(_toLoaderIndicatorOptions(options))
        self.ui.requestRender()

    def setHiddenThinkingLabel(self, label: str | None = None) -> None:
        self.hiddenThinkingLabel = label if label is not None else self.defaultHiddenThinkingLabel
        for child in self.chatContainer.children:
            if isinstance(child, AssistantMessageComponent):
                child.setHiddenThinkingLabel(self.hiddenThinkingLabel)
        if self.streamingComponent:
            self.streamingComponent.setHiddenThinkingLabel(self.hiddenThinkingLabel)
        self.ui.requestRender()

    def setExtensionWidget(self, key: str, content: Any, options: Any = None) -> None:
        """Set an extension widget (string array or custom component)."""
        placement = read_field(options, "placement") or "aboveEditor"

        def removeExisting(widgets: dict[str, Any]) -> None:
            existing = widgets.get(key)
            dispose = getattr(existing, "dispose", None)
            if callable(dispose):
                dispose()
            widgets.pop(key, None)

        removeExisting(self.extensionWidgetsAbove)
        removeExisting(self.extensionWidgetsBelow)

        if content is None:
            self.renderWidgets()
            return

        if isinstance(content, list):
            # Wrap string array in a Container with Text components
            component: Any = Container()
            for line in content[: InteractiveMode.MAX_WIDGET_LINES]:
                component.addChild(Text(line, 1, 0))
            if len(content) > InteractiveMode.MAX_WIDGET_LINES:
                component.addChild(ThemedText(lambda: theme.fg("muted", "... (widget truncated)"), 1, 0))
        else:
            # Factory function - create component
            component = content(self.ui, theme)

        targetMap = self.extensionWidgetsBelow if placement == "belowEditor" else self.extensionWidgetsAbove
        targetMap[key] = component
        self.renderWidgets()

    def clearExtensionWidgets(self) -> None:
        for widget in self.extensionWidgetsAbove.values():
            dispose = getattr(widget, "dispose", None)
            if callable(dispose):
                dispose()
        for widget in self.extensionWidgetsBelow.values():
            dispose = getattr(widget, "dispose", None)
            if callable(dispose):
                dispose()
        self.extensionWidgetsAbove.clear()
        self.extensionWidgetsBelow.clear()
        self.renderWidgets()

    def resetExtensionUI(self) -> None:
        # MISAKA fork: cancels the dialog that waits for its turn as well as the open one.
        self._resetExtensionPrompts()
        if self.extensionSelector:
            self.hideExtensionSelector()
        if self.extensionInput:
            self.hideExtensionInput()
        if self.extensionEditor:
            self.hideExtensionEditor()
        self.ui.hideOverlay()
        self.clearExtensionTerminalInputListeners()
        self.setExtensionFooter(None)
        self.setExtensionHeader(None)
        self.clearExtensionWidgets()
        self.footerDataProvider.clearExtensionStatuses()
        self.footer.invalidate()
        self.autocompleteProviderWrappers = []
        self.setCustomEditorComponent(None)
        self.setupAutocompleteProvider()
        self.defaultEditor.onExtensionShortcut = None
        self.updateTerminalTitle()
        self.workingMessage = None
        self.workingVisible = True
        self.setWorkingIndicator()
        if self.activeStatusIndicator is not None and self.activeStatusIndicator.kind == "working":
            self.activeStatusIndicator.setMessage(
                f"{self.defaultWorkingMessage} ({keyText('app.interrupt')} to interrupt)"
            )
        self.setHiddenThinkingLabel()

    def renderWidgets(self) -> None:
        """Render all extension widgets to the widget container."""
        if not self.widgetContainerAbove or not self.widgetContainerBelow:
            return
        self.renderWidgetContainer(self.widgetContainerAbove, self.extensionWidgetsAbove, True, True)
        self.renderWidgetContainer(self.widgetContainerBelow, self.extensionWidgetsBelow, False, False)
        self.ui.requestRender()

    def renderWidgetContainer(
        self,
        container: Container,
        widgets: dict[str, Any],
        spacerWhenEmpty: bool,
        leadingSpacer: bool,
    ) -> None:
        container.clear()

        if len(widgets) == 0:
            if spacerWhenEmpty:
                container.addChild(Spacer(1))
            return

        if leadingSpacer:
            container.addChild(Spacer(1))
        for component in widgets.values():
            container.addChild(component)

    def setExtensionFooter(self, factory: Any) -> None:
        """Set a custom footer component, or restore the built-in footer."""
        # Dispose existing custom footer
        dispose = getattr(self.customFooter, "dispose", None)
        if callable(dispose):
            dispose()

        self.footerContainer.clear()
        if factory:
            # Create and add custom footer, passing the data provider
            self.customFooter = factory(self.ui, theme, self.footerDataProvider)
            self.footerContainer.addChild(self.customFooter)
        else:
            # Restore built-in footer
            self.customFooter = None
            self.footerContainer.addChild(self.footer)

        self.ui.requestRender()

    def setExtensionHeader(self, factory: Any) -> None:
        """Set a custom header component, or restore the built-in header."""
        # Header may not be initialized yet if called during early initialization
        if not self.builtInHeader:
            return

        # Dispose existing custom header
        dispose = getattr(self.customHeader, "dispose", None)
        if callable(dispose):
            dispose()

        # Find the index of the current header in the header container
        currentHeader = self.customHeader or self.builtInHeader
        index = next(
            (position for position, child in enumerate(self.headerContainer.children) if child is currentHeader), -1
        )

        if factory:
            # Create and add custom header
            self.customHeader = factory(self.ui, theme)
            customHeader = self.customHeader
            if isExpandable(customHeader):
                customHeader.setExpanded(self.toolOutputExpanded)
            if index != -1:
                self.headerContainer.children[index] = self.customHeader
            else:
                # If not found (e.g. builtInHeader was never added), add at the top
                self.headerContainer.children.insert(0, self.customHeader)
        else:
            # Restore built-in header
            self.customHeader = None
            builtInHeader = self.builtInHeader
            if isExpandable(builtInHeader):
                builtInHeader.setExpanded(self.toolOutputExpanded)
            if index != -1:
                self.headerContainer.children[index] = self.builtInHeader

        self.ui.requestRender()

    def addExtensionTerminalInputListener(self, handler: Any) -> Callable[[], None]:
        subscription = {"handler": handler, "unsubscribe": self.ui.addInputListener(handler)}
        self.extensionTerminalInputSubscriptions.append(subscription)

        def unsubscribe() -> None:
            subscription["unsubscribe"]()
            if subscription in self.extensionTerminalInputSubscriptions:
                self.extensionTerminalInputSubscriptions.remove(subscription)

        return unsubscribe

    # MISAKA fork (D1): no `rebindExtensionTerminalInputListeners`; the renderer is never replaced.

    def clearExtensionTerminalInputListeners(self) -> None:
        for subscription in self.extensionTerminalInputSubscriptions:
            subscription["unsubscribe"]()
        self.extensionTerminalInputSubscriptions.clear()

    def createProjectTrustContext(self, cwd: str) -> ProjectTrustContext:
        ui = self.createExtensionUIContext()
        return {
            "cwd": cwd,
            "mode": "tui",
            "hasUI": True,
            "ui": {
                "select": ui.select,
                "confirm": ui.confirm,
                "input": ui.input,
                "notify": ui.notify,
            },
        }

    def createExtensionUIContext(self) -> _ExtensionUIContext:
        """Create the ExtensionUIContext for extensions."""
        return _ExtensionUIContext(self)

    # MISAKA fork: extension dialogs (selector, input, editor and custom components) run one at a time. Sisters,
    # research and extension parts can all ask at once; pi lets a new dialog displace the open one, which strands the
    # displaced caller's promise. Here a second dialog waits for the first, a dialog opened from inside another is
    # refused, and resetting the extension UI cancels both the open and the waiting one.

    async def _acquireExtensionPrompt(self, signal: Any = None) -> object | None:
        task = asyncio.current_task()
        if task is self._extensionPromptTask or self._extensionPromptContext.get() is not None:
            raise RuntimeError("Extension UI prompts cannot be nested")
        generation = self._extensionPromptGeneration
        if signal is None:
            await self._extensionPromptLock.acquire()
        else:
            loop = asyncio.get_running_loop()
            aborted: asyncio.Future[None] = loop.create_future()

            def abort() -> None:
                def settle() -> None:
                    if not aborted.done():
                        aborted.set_result(None)

                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(settle)

            unregisterAbort = _register_abort_handler(signal, abort)
            acquire = asyncio.create_task(self._extensionPromptLock.acquire())
            acquired = False
            try:
                waiters: set[asyncio.Future[Any]] = {acquire, aborted}
                completed, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if aborted in completed:
                    if not acquire.done():
                        acquire.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await acquire
                    elif not acquire.cancelled() and acquire.exception() is None:
                        self._extensionPromptLock.release()
                    return None
                acquired = await acquire
            except BaseException:
                if not acquire.done():
                    acquire.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await acquire
                elif not acquired and not acquire.cancelled() and acquire.exception() is None:
                    self._extensionPromptLock.release()
                raise
            finally:
                with contextlib.suppress(Exception):
                    unregisterAbort()
        if generation != self._extensionPromptGeneration:
            self._extensionPromptLock.release()
            return None
        owner = object()
        self._extensionPromptOwner = owner
        self._extensionPromptTask = task
        self._extensionPromptContextToken = self._extensionPromptContext.set(owner)
        return owner

    def _releaseExtensionPrompt(self, owner: object) -> None:
        token = self._extensionPromptContextToken
        if token is not None:
            self._extensionPromptContext.reset(token)
            self._extensionPromptContextToken = None
        if self._extensionPromptOwner is owner:
            self._extensionPromptOwner = None
            self._extensionPromptTask = None
            self._extensionPromptCancel = None
        self._extensionPromptLock.release()

    def _resetExtensionPrompts(self) -> None:
        self._extensionPromptGeneration += 1
        cancel = self._extensionPromptCancel
        if cancel is not None:
            with contextlib.suppress(Exception):
                cancel()

    async def _showExtensionDialog(
        self,
        slot: Literal["extensionSelector", "extensionInput", "extensionEditor"],
        create: Callable[[Callable[[Any], None]], Any],
        blocked: BlockedStatus,
        signal: Any = None,
    ) -> Any:
        """Mounts the dialog `create` builds in the editor slot and waits for its value; `None` when it is
        cancelled, aborted or displaced by a reset."""
        if signal_aborted(signal):
            return None
        owner = await self._acquireExtensionPrompt(signal)
        if owner is None:
            return None
        if signal_aborted(signal):
            self._releaseExtensionPrompt(owner)
            return None

        hide = {
            "extensionSelector": self.hideExtensionSelector,
            "extensionInput": self.hideExtensionInput,
            "extensionEditor": self.hideExtensionEditor,
        }[slot]
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        component: Any = None
        unregisterAbort: Callable[[], None] = lambda: None
        closed = False

        def finish(value: Any) -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            with contextlib.suppress(Exception):
                unregisterAbort()
            if self._extensionPromptOwner is owner:
                if component is not None and getattr(self, slot) is component:
                    hide()
                self._extensionPromptCancel = None
            if not future.done():
                future.set_result(value)

        try:
            self._extensionPromptCancel = lambda: finish(None)
            unregisterAbort = _register_abort_handler(signal, lambda: finish(None))
            if not closed:
                component = create(finish)
                setattr(self, slot, component)
                self.disposeActiveSelector()
                self.editorContainer.clear()
                self.editorContainer.addChild(component)
                self.ui.setFocus(component)
                # Extension dialogs share the editor slot: opening one replaces the status of a displaced one.
                self.programStatus.setBlocked("extension-dialog", blocked)
                self.ui.requestRender()
            return await future
        finally:
            finish(None)
            self._releaseExtensionPrompt(owner)

    async def showExtensionSelector(
        self,
        title: str,
        options: list[str],
        opts: Any = None,
        blocked: BlockedStatus | None = None,
    ) -> str | None:
        """Show a selector for extensions."""
        if blocked is None:
            blocked = {"kind": "question", "message": title}
        return await self._showExtensionDialog(
            "extensionSelector",
            lambda finish: ExtensionSelectorComponent(
                title,
                options,
                finish,
                lambda: finish(None),
                {
                    "tui": self.ui,
                    "timeout": read_field(opts, "timeout"),
                    "onToggleToolsExpanded": lambda: self.toggleToolOutputExpansion(),
                },
            ),
            blocked,
            read_field(opts, "signal"),
        )

    def hideExtensionSelector(self) -> None:
        """Hide the extension selector."""
        if self.extensionSelector is not None:
            self.extensionSelector.dispose()
        self.editorContainer.clear()
        self.editorContainer.addChild(self.editor)
        self.extensionSelector = None
        self.programStatus.setBlocked("extension-dialog", None)
        self.ui.setFocus(self.editor)
        self.ui.requestRender()

    async def showExtensionConfirm(self, title: str, message: str, opts: Any = None) -> bool:
        """Show a confirmation dialog for extensions."""
        result = await self.showExtensionSelector(
            f"{title}\n{message}", ["Yes", "No"], opts, {"kind": "permission", "message": title}
        )
        return result == "Yes"

    async def promptForMissingSessionCwd(self, error: MissingSessionCwdError) -> str | None:
        confirmed = await self.showExtensionConfirm(
            "Session cwd not found", formatMissingSessionCwdPrompt(error.issue)
        )
        return error.issue.fallbackCwd if confirmed else None

    async def showExtensionInput(self, title: str, placeholder: str | None = None, opts: Any = None) -> str | None:
        """Show a text input for extensions."""
        return await self._showExtensionDialog(
            "extensionInput",
            lambda finish: ExtensionInputComponent(
                title,
                placeholder,
                finish,
                lambda: finish(None),
                {"tui": self.ui, "timeout": read_field(opts, "timeout")},
            ),
            {"kind": "question", "message": title},
            read_field(opts, "signal"),
        )

    def hideExtensionInput(self) -> None:
        """Hide the extension input."""
        if self.extensionInput is not None:
            self.extensionInput.dispose()
        self.editorContainer.clear()
        self.editorContainer.addChild(self.editor)
        self.extensionInput = None
        self.programStatus.setBlocked("extension-dialog", None)
        self.ui.setFocus(self.editor)
        self.ui.requestRender()

    async def showExtensionEditor(self, title: str, prefill: str | None = None) -> str | None:
        """Show a multi-line editor for extensions (with Ctrl+G support)."""
        return await self._showExtensionDialog(
            "extensionEditor",
            lambda finish: ExtensionEditorComponent(
                self.ui,
                self.keybindings,
                title,
                prefill,
                finish,
                lambda: finish(None),
                None,
                self.settingsManager.getExternalEditorCommand(),
            ),
            {"kind": "question", "message": title},
        )

    def hideExtensionEditor(self) -> None:
        """Hide the extension editor."""
        self.editorContainer.clear()
        self.editorContainer.addChild(self.editor)
        self.extensionEditor = None
        self.programStatus.setBlocked("extension-dialog", None)
        self.ui.setFocus(self.editor)
        self.ui.requestRender()

    def setCustomEditorComponent(self, factory: Any) -> None:
        """Set a custom editor component from an extension.
        Pass undefined to restore the default editor.
        """
        self.editorComponentFactory = factory

        # Save text from current editor before switching
        currentText = self.editor.getText()

        self.disposeActiveSelector()
        self.editorContainer.clear()

        if factory:
            # Create the custom editor with tui, theme, and keybindings
            newEditor = factory(self.ui, getEditorTheme(), self.keybindings)

            # Wire up callbacks from the default editor
            newEditor.onSubmit = self.defaultEditor.onSubmit
            newEditor.onChange = self.defaultEditor.onChange

            # Copy text from previous editor
            newEditor.setText(currentText)

            # Copy appearance settings if supported
            if getattr(newEditor, "borderColor", None) is not None:
                newEditor.borderColor = self.defaultEditor.borderColor
            if getattr(newEditor, "setPaddingX", None) is not None:
                newEditor.setPaddingX(self.defaultEditor.getPaddingX())
            if getattr(newEditor, "setAutocompleteMaxVisible", None) is not None:
                newEditor.setAutocompleteMaxVisible(self.defaultEditor.getAutocompleteMaxVisible())

            # Set autocomplete if supported
            if getattr(newEditor, "setAutocompleteProvider", None) and self.autocompleteProvider:
                newEditor.setAutocompleteProvider(self.autocompleteProvider)

            # If extending CustomEditor, copy app-level handlers
            # Use duck typing since instanceof fails across jiti module boundaries
            customEditor = newEditor
            if isinstance(getattr(customEditor, "actionHandlers", None), dict):
                if not getattr(customEditor, "onEscape", None):
                    customEditor.onEscape = lambda: self.defaultEditor.onEscape and self.defaultEditor.onEscape()
                if not getattr(customEditor, "onCtrlD", None):
                    customEditor.onCtrlD = lambda: self.defaultEditor.onCtrlD and self.defaultEditor.onCtrlD()
                if not getattr(customEditor, "onPasteImage", None):
                    customEditor.onPasteImage = (
                        lambda: self.defaultEditor.onPasteImage and self.defaultEditor.onPasteImage()
                    )
                if not getattr(customEditor, "onExtensionShortcut", None):
                    customEditor.onExtensionShortcut = (
                        lambda data: self.defaultEditor.onExtensionShortcut is not None
                        and self.defaultEditor.onExtensionShortcut(data)
                    )
                # Copy action handlers (clear, suspend, model switching, etc.)
                for action, handler in self.defaultEditor.actionHandlers.items():
                    customEditor.actionHandlers[action] = handler

            self.editor = newEditor
        else:
            # Restore default editor with text from custom editor
            self.defaultEditor.setText(currentText)
            self.editor = self.defaultEditor

        self.editorContainer.addChild(self.editor)
        if self.activeStatusIndicator:
            self.statusContainer.clear()
            self.activeWorkingIndicatorEmbedded = self.setEditorWorkingStatusIndicator(self.activeStatusIndicator)
            if not self.activeWorkingIndicatorEmbedded:
                self.statusContainer.addChild(self.activeStatusIndicator)
        self.ui.setFocus(self.editor)
        self.ui.requestRender()

    def showExtensionNotify(self, message: str, type: str | None = None) -> None:
        """Show a notification for extensions."""
        if type == "error":
            self.showError(message)
        elif type == "warning":
            self.showWarning(message)
        else:
            self.showStatus(message)

    async def showExtensionCustom(self, factory: Any, options: Any = None) -> Any:
        """Show a custom component with keyboard focus. Overlay mode renders on top of existing content."""
        # MISAKA fork: one extension dialog at a time (see `_acquireExtensionPrompt`).
        owner = await self._acquireExtensionPrompt()
        if owner is None:
            return None
        try:
            return await self._showExtensionCustom(owner, factory, options)
        finally:
            self._releaseExtensionPrompt(owner)

    async def _showExtensionCustom(self, owner: object, factory: Any, options: Any) -> Any:
        savedText = self.editor.getText()
        isOverlay = read_field(options, "overlay") or False
        # MISAKA fork: a component may name the dialog status to report while it waits (AskUserQuestion does).
        blocked: BlockedStatus | None = read_field(options, "blocked")

        def restoreEditor() -> None:
            self.editorContainer.clear()
            self.editorContainer.addChild(self.editor)
            self.editor.setText(savedText)
            self.ui.setFocus(self.editor)
            self.ui.requestRender()

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        component: Any = None
        overlayHandle: Any = None
        closed = False

        def close(result: Any) -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            # Remove this overlay, not the topmost one: another overlay may have opened above it.
            if isOverlay:
                if overlayHandle is not None:
                    overlayHandle.hide()
            else:
                restoreEditor()
            # Note: both branches above already call requestRender
            if blocked:
                self.programStatus.setBlocked("extension-dialog", None)
            if self._extensionPromptOwner is owner:
                self._extensionPromptCancel = None
            if not future.done():
                future.set_result(result)
            try:
                dispose = getattr(component, "dispose", None)
                if callable(dispose):
                    dispose()
            except Exception:  # noqa: BLE001, S110 - ignore dispose errors
                pass

        self._extensionPromptCancel = lambda: close(None)
        factoryTask: asyncio.Future[Any] | None = None
        try:
            try:
                created = factory(self.ui, theme, self.keybindings, close)
                if inspect.isawaitable(created):
                    factoryTask = asyncio.ensure_future(created)
                    completed, _pending = await asyncio.wait({factoryTask, future}, return_when=asyncio.FIRST_COMPLETED)
                    if factoryTask not in completed:
                        # MISAKA fork: closed before its component was built, so the build is cancelled.
                        return await future
                    c = factoryTask.result()
                else:
                    c = created
            except Exception:
                if closed:
                    return await future
                closed = True
                if not isOverlay:
                    restoreEditor()
                raise
            if closed:
                return await future
            component = c
            if isOverlay:
                # Resolve overlay options - can be static or dynamic function
                def resolveOptions() -> Any:
                    overlayOptions = read_field(options, "overlayOptions")
                    if overlayOptions is not None:
                        return overlayOptions() if callable(overlayOptions) else overlayOptions
                    # Fallback: use component's width property if available
                    w = getattr(component, "width", None)
                    return {"width": w} if w else None

                overlayHandle = self.ui.showOverlay(component, resolveOptions())
                # Expose handle to caller for visibility control
                onHandle = read_field(options, "onHandle")
                if onHandle:
                    onHandle(overlayHandle)
            else:
                self.disposeActiveSelector()
                self.editorContainer.clear()
                self.editorContainer.addChild(component)
                self.ui.setFocus(component)
                self.ui.requestRender()
            if blocked:
                self.programStatus.setBlocked("extension-dialog", blocked)
            return await future
        finally:
            if factoryTask is not None and not factoryTask.done():
                factoryTask.cancel()
            # MISAKA fork: a caller that stops waiting (its task is cancelled) closes the component, so the editor
            # is not left behind it.
            close(None)

    def showExtensionError(self, extensionPath: str, error: str, stack: str | None = None) -> None:
        """Show an extension error in the UI."""
        errorMsg = f'Extension "{extensionPath}" error: {error}'
        errorText = ThemedText(lambda: theme.fg("error", errorMsg), 1, 0)
        self.chatContainer.addChild(errorText)
        if stack:
            # Show stack trace in dim color, indented
            stackLines = stack.split("\n")[1:]  # Skip first line (duplicates error message)
            if len(stackLines) > 0:

                def renderStack() -> str:
                    return "\n".join(theme.fg("dim", f"  {jsTrim(line)}") for line in stackLines)

                self.chatContainer.addChild(ThemedText(renderStack, 1, 0))
        self.ui.requestRender()

    # =========================================================================
    # Key Handlers
    # =========================================================================

    def setupKeyHandlers(self) -> None:
        # Set up handlers on defaultEditor - they use this.editor for text access
        # so they work correctly regardless of which editor is active
        def onEscape() -> None:
            if self.session.isStreaming:
                self.restoreQueuedMessagesToEditor({"abort": True})
            elif self.session.isBashRunning:
                self.session.abortBash()
            elif self.isBashMode:
                self.editor.setText("")
                self.isBashMode = False
                self.updateEditorBorderColor()
            elif not jsTrim(self.editor.getText()):
                # Double-escape with empty editor triggers /tree, /fork, or nothing based on setting
                action = self.settingsManager.getDoubleEscapeAction()
                if action != "none":
                    now = time.time() * 1000
                    if now - self.lastEscapeTime < 500:
                        if action == "tree":
                            self.showTreeSelector()
                        else:
                            self.showUserMessageSelector()
                        self.lastEscapeTime = 0
                    else:
                        self.lastEscapeTime = now

        self.defaultEditor.onEscape = onEscape

        # Register app action handlers
        self.defaultEditor.onAction("app.clear", lambda: self.handleCtrlC())
        self.defaultEditor.onCtrlD = lambda: self.handleCtrlD()
        self.defaultEditor.onAction("app.suspend", lambda: self.handleCtrlZ())
        self.defaultEditor.onAction("app.thinking.cycle", lambda: self.cycleThinkingLevel())
        self.defaultEditor.onAction("app.model.cycleForward", lambda: self._schedule_task(self.cycleModel("forward")))
        self.defaultEditor.onAction(
            "app.model.cycleBackward", lambda: self._schedule_task(self.cycleModel("backward"))
        )

        # Global debug handler on TUI (works regardless of focus)
        self.ui.onDebug = lambda: self.handleDebugCommand()
        self.defaultEditor.onAction("app.model.select", lambda: self.showModelSelector())
        self.defaultEditor.onAction("app.tools.expand", lambda: self.toggleToolOutputExpansion())
        self.defaultEditor.onAction("app.thinking.toggle", lambda: self.toggleThinkingBlockVisibility())
        self.defaultEditor.onAction("app.editor.external", lambda: self._schedule_task(self.handleOpenExternalEditor()))
        self.defaultEditor.onAction(
            "app.message.copy",
            lambda: self._schedule_task(self.handleCopyCommand({"flashConfirmation": True, "preferSelection": True})),
        )
        self.defaultEditor.onAction("app.message.followUp", lambda: self._schedule_task(self.handleFollowUp()))
        self.defaultEditor.onAction("app.message.dequeue", lambda: self.handleDequeue())
        self.defaultEditor.onAction("app.session.new", lambda: self._schedule_task(self.handleClearCommand()))
        self.defaultEditor.onAction("app.session.tree", lambda: self.showTreeSelector())
        self.defaultEditor.onAction("app.session.fork", lambda: self.showUserMessageSelector())
        self.defaultEditor.onAction("app.session.resume", lambda: self.showSessionSelector())

        def onChange(text: str) -> None:
            wasBashMode = self.isBashMode
            self.isBashMode = jsTrimStart(text).startswith("!")
            if wasBashMode != self.isBashMode:
                self.updateEditorBorderColor()

        self.defaultEditor.onChange = onChange

        # Handle clipboard paste (triggered on Ctrl+V). Copied files use their original paths,
        # images are attached via temporary files, and plain text is the final fallback.
        self.defaultEditor.onPasteImage = lambda: self._schedule_task(self.handleClipboardPaste())

    # MISAKA fork (D1): no `handleRightClickPaste` (right-click paste is a fullscreen feature).

    async def handleClipboardPaste(self) -> None:
        insertTextAtCursor = getattr(self.editor, "insertTextAtCursor", None)
        try:
            filePaths = await readClipboardFilePaths()
            if filePaths:
                if any(unicodedata.category(character) == "Cc" for filePath in filePaths for character in filePath):
                    raise RuntimeError("Clipboard file path contains control characters")
                paths = " ".join(quoteIfNeeded(filePath) for filePath in filePaths) if self.isBashMode else "\n".join(
                    filePaths
                )
                getCursor = getattr(self.editor, "getCursor", None)
                cursor = getCursor() if callable(getCursor) else None
                currentLine = (jsIndex(self.editor.getText().split("\n"), cursor["line"]) or "") if cursor else ""
                characterBeforeCursor = jsIndex(currentLine, cursor["col"] - 1) if cursor and cursor["col"] > 0 else ""
                characterAfterCursor = jsIndex(currentLine, cursor["col"]) if cursor else ""
                leadingSpace = (
                    " " if characterBeforeCursor and _JS_WHITESPACE.match(characterBeforeCursor) is None else ""
                )
                trailingSpace = " " if characterAfterCursor and _JS_WHITESPACE.match(characterAfterCursor) is None else ""
                if callable(insertTextAtCursor):
                    insertTextAtCursor(f"{leadingSpace}{paths}{trailingSpace}")
                self.ui.requestRender()
                return

            image = await readClipboardImage()
            if image:
                tmpDir = tempfile.gettempdir()
                ext = extensionForImageMimeType(image.mimeType) or "png"
                # MISAKA fork: the product name in the file name.
                fileName = f"misaka-clipboard-{uuid.uuid4()}.{ext}"
                filePath = os.path.join(tmpDir, fileName)
                with open(filePath, "wb") as file:  # noqa: ASYNC230 - upstream writes synchronously
                    file.write(bytes(image.bytes))

                if callable(insertTextAtCursor):
                    insertTextAtCursor(filePath)
                self.ui.requestRender()
                return

            text = await readClipboardText()
            if text:
                if callable(insertTextAtCursor):
                    insertTextAtCursor(text)
                self.ui.requestRender()
        except Exception as error:  # noqa: BLE001 - shown to the user
            self.showError(f"Failed to paste from clipboard: {errorMessage(error)}")

    def handleStartupSubmit(self, text: str) -> None:
        self.editor.setText(text)
        self.showStatus("Startup is still in progress")

    def setupEditorSubmitHandler(self) -> None:
        async def onSubmit(text: str) -> None:
            text = jsTrim(text)
            if not text:
                return

            # Handle commands
            if text == "/settings":
                self.showSettingsSelector()
                self.editor.setText("")
                return
            if text == "/scoped-models":
                self.editor.setText("")
                self.showModelsSelector()
                return
            if text == "/model" or text.startswith("/model "):
                searchTerm = jsTrim(text[7:]) if text.startswith("/model ") else None
                self.editor.setText("")
                await self.handleModelCommand(searchTerm)
                return
            if text == "/thinking" or text.startswith("/thinking "):
                searchTerm = jsTrim(text[10:]) if text.startswith("/thinking ") else None
                self.editor.setText("")
                self.handleThinkingCommand(searchTerm)
                return
            if text == "/export" or text.startswith("/export "):
                await self.handleExportCommand(text)
                self.editor.setText("")
                return
            if text == "/import" or text.startswith("/import "):
                await self.handleImportCommand(text)
                self.editor.setText("")
                return
            if text == "/share":
                await self.handleShareCommand()
                self.editor.setText("")
                return
            # MISAKA fork: no `/bug` (pi's reports go to the Pi developers).
            if text == "/copy":
                await self.handleCopyCommand()
                self.editor.setText("")
                return
            if text == "/name" or text.startswith("/name "):
                self.handleNameCommand(text)
                self.editor.setText("")
                return
            if text == "/session":
                self.handleSessionCommand()
                self.editor.setText("")
                return
            # MISAKA fork: no `/changelog` (no changelog ships).
            if text == "/hotkeys":
                self.handleHotkeysCommand()
                self.editor.setText("")
                return
            if text == "/fork":
                self.showUserMessageSelector()
                self.editor.setText("")
                return
            if text == "/clone":
                self.editor.setText("")
                await self.handleCloneCommand()
                return
            if text == "/tree":
                self.showTreeSelector()
                self.editor.setText("")
                return
            if text == "/trust":
                self.showTrustSelector()
                self.editor.setText("")
                return
            if text == "/login" or text.startswith("/login "):
                providerRef = jsTrim(text[7:]) if text.startswith("/login ") else None
                self.editor.setText("")
                await self.handleLoginCommand(providerRef)
                return
            if text == "/logout":
                self._schedule_task(self.showOAuthSelector("logout"))
                self.editor.setText("")
                return
            # MISAKA fork: `/new --carry` hands the context to the new session through the context engine.
            if text == "/new" or text == "/new --carry":
                self.editor.setText("")
                await self.handleClearCommand(carry=text == "/new --carry")
                return
            # MISAKA fork: `/clear` empties the chat and the context; `/board` opens the team board.
            if text == "/clear":
                self.editor.setText("")
                await self.handleTrueClearCommand()
                return
            if text == "/board":
                self.editor.setText("")
                await self.handleBoardCommand()
                return
            if text == "/compact" or text.startswith("/compact "):
                customInstructions = jsTrim(text[9:]) if text.startswith("/compact ") else None
                self.editor.setText("")
                await self.handleCompactCommand(customInstructions)
                return
            if text == "/reload":
                self.editor.setText("")
                await self.handleReloadCommand()
                return
            if text == "/debug":
                self.handleDebugCommand()
                self.editor.setText("")
                return
            # MISAKA fork: no `/arminsayshi` or `/dementedelves` (easter eggs).
            if text == "/resume":
                self.showSessionSelector()
                self.editor.setText("")
                return
            if text == "/quit":
                self.editor.setText("")
                await self.shutdown()
                return

            # MISAKA fork: an unknown slash command fails loudly instead of going to the model as text, which showed
            # only the working indicator and looked as though the command had run. Prompt templates, extension
            # commands and core commands pass through; `//name` sends the text as is.
            if (
                text.startswith("/")
                and not text.startswith("//")
                and not self.isExtensionCommand(text)
                and not self.isCorePromptCommand(text)
                and not self.isPromptTemplate(text)
            ):
                name = text[1:].split(" ", 1)[0]
                if name and not name[0].isdigit():
                    self.showError(
                        f"Unknown command /{name}. Type / to list commands, or //{name} to send it as plain text."
                    )
                    return

            # Handle bash command (! for normal, !! for excluded from context)
            if text.startswith("!"):
                isExcluded = text.startswith("!!")
                command = jsTrim(text[2:]) if isExcluded else jsTrim(text[1:])
                if command:
                    if self.session.isBashRunning:
                        self.showWarning("A bash command is already running. Press Esc to cancel it first.")
                        self.editor.setText(text)
                        return
                    addToHistory = getattr(self.editor, "addToHistory", None)
                    if callable(addToHistory):
                        addToHistory(text)
                    await self.handleBashCommand(command, isExcluded)
                    self.isBashMode = False
                    self.updateEditorBorderColor()
                    return

            # Queue input during compaction (extension commands execute immediately)
            if self.session.isCompacting:
                if self.isExtensionCommand(text):
                    addToHistory = getattr(self.editor, "addToHistory", None)
                    if callable(addToHistory):
                        addToHistory(text)
                    self.editor.setText("")
                    await self.session.prompt(text)
                else:
                    self.queueCompactionMessage(text, "steer")
                return

            # If streaming, use prompt() with steer behavior
            # This handles extension commands (execute immediately), prompt template expansion, and queueing
            if self.session.isStreaming:
                addToHistory = getattr(self.editor, "addToHistory", None)
                if callable(addToHistory):
                    addToHistory(text)
                self.editor.setText("")
                await self.session.prompt(text, {"streamingBehavior": "steer"})
                self.updatePendingMessagesDisplay()
                self.ui.requestRender()
                return

            # Normal message submission
            # First, move any pending bash components to chat
            self.flushPendingBashComponents()

            if self.onInputCallback:
                self.onInputCallback(text)
            else:
                self.pendingUserInputs.append(text)
            addToHistory = getattr(self.editor, "addToHistory", None)
            if callable(addToHistory):
                addToHistory(text)

        # MISAKA fork: the editor calls `onSubmit` synchronously, so the handler runs as a task.
        self.defaultEditor.onSubmit = lambda text: self._schedule_task(onSubmit(text))

    def subscribeToAgent(self) -> None:
        def listener(event: Any) -> None:
            # MISAKA fork: the session calls listeners synchronously; the handler starts at once, as a JavaScript
            # async listener runs up to its first await, so compaction-end queue delivery can beat the agent loop's
            # post-compaction steering poll.
            self._schedule_task(self.handleEvent(event))

        self.unsubscribe = self.session.subscribe(listener)

    async def handleEvent(self, event: Any) -> None:
        if not self.isInitialized:
            await self.init()

        self.footer.invalidate()
        self.programStatus.handleEvent(event)

        match read_field(event, "type"):
            case "agent_start":
                self.pendingTools.clear()
                # Restore main escape handler if retry handler is still active
                # (retry success event fires later, but we need main handler now)
                if self.retryEscapeHandler:
                    self.defaultEditor.onEscape = self.retryEscapeHandler
                    self.retryEscapeHandler = None

            case "turn_start":
                if self.settingsManager.getShowTerminalProgress():
                    self.ui.terminal.setProgress(True)
                if self.workingVisible:
                    if (self.activeStatusIndicator.kind if self.activeStatusIndicator else None) != "working":
                        self.showWorkingStatusIndicator()
                else:
                    self.clearStatusIndicator()
                self.ui.requestRender()

            case "queue_update":
                self.updatePendingMessagesDisplay()
                self.ui.requestRender()

            case "entry_appended":
                entry = read_field(event, "entry")
                entryId = read_field(entry, "id")
                if entryId in self.entriesRenderedByBoundaryCompaction:
                    self.entriesRenderedByBoundaryCompaction.discard(entryId)
                    return
                entryType = read_field(entry, "type")
                if entryType == "custom":
                    self.addCustomEntryToChat(entry)
                    self.ui.requestRender()
                elif entryType == "usage" and read_field(entry, "kind") == "cache_warm":
                    self.addCacheWarmingUsage(entry)
                    self.ui.requestRender()
                elif entryType == "custom_message" and read_field(entry, "display"):
                    self.addMessageToChat(
                        createCustomMessage(
                            read_field(entry, "customType"),
                            read_field(entry, "content"),
                            read_field(entry, "display"),
                            read_field(entry, "details"),
                            read_field(entry, "timestamp"),
                        )
                    )
                    self.ui.requestRender()
                elif entryType == "compaction":
                    entries = self.sessionManager.buildContextEntries()
                    if read_field(jsIndex(entries, 0), "id") != entryId:
                        return
                    self.chatContainer.clear()
                    branch = self.sessionManager.getBranch()
                    compactionIndex = next(
                        (index for index, branchEntry in enumerate(branch) if read_field(branchEntry, "id") == entryId),
                        -1,
                    )
                    entriesAfterCompaction = {read_field(branchEntry, "id") for branchEntry in branch[compactionIndex + 1 :]}
                    retainedEntries = entries[1:]
                    if read_field(entry, "contextMessages") is not None:
                        # MISAKA fork: a context engine's compaction carries the context it keeps, which is drawn in
                        # context order in place of the summary.
                        self.renderSessionEntries(entries)
                    else:
                        self.renderSessionEntries(
                            [retained for retained in retainedEntries if read_field(retained, "id") not in entriesAfterCompaction]
                        )
                        self.addMessageToChat(
                            createCompactionSummaryMessage(
                                read_field(entry, "summary"), read_field(entry, "tokensBefore"), read_field(entry, "timestamp")
                            )
                        )
                        if read_field(entry, "usage"):
                            self.addCompactionCostNotice(
                                {"type": "compaction_cost", "kind": "compaction", "usage": read_field(entry, "usage")}
                            )
                        self.renderSessionEntries(
                            [retained for retained in retainedEntries if read_field(retained, "id") in entriesAfterCompaction]
                        )
                    for afterId in entriesAfterCompaction:
                        self.entriesRenderedByBoundaryCompaction.add(afterId)
                    self.footer.invalidate()
                    self.ui.requestRender()

            case "session_info_changed":
                self.updateTerminalTitle()
                self.footer.invalidate()
                self.ui.requestRender()

            case "thinking_level_changed":
                self.footer.invalidate()
                self.updateEditorBorderColor()

            case "message_start":
                message = read_field(event, "message")
                role = read_field(message, "role")
                if role == "custom":
                    self.addMessageToChat(message)
                    self.ui.requestRender()
                elif role == "user":
                    self.addMessageToChat(message)
                    self.updatePendingMessagesDisplay()
                    self.ui.requestRender()
                elif role == "assistant":
                    self.streamingComponent = AssistantMessageComponent(
                        None,
                        self.hideThinkingBlock,
                        self.getMarkdownThemeWithSettings(),
                        self.hiddenThinkingLabel,
                        self.outputPad,
                        self.getMarkdownTransformers(),
                    )
                    self.streamingMessage = message
                    self.chatContainer.addChild(self.streamingComponent)
                    self.streamingComponent.updateContent(self.streamingMessage, True)
                    self.ui.requestRender()

            case "message_update":
                message = read_field(event, "message")
                if self.streamingComponent and read_field(message, "role") == "assistant":
                    self.streamingMessage = message
                    self.streamingComponent.updateContent(self.streamingMessage, True)

                    for content in read_field(self.streamingMessage, "content", []):
                        if read_field(content, "type") == "toolCall":
                            contentId = read_field(content, "id")
                            if contentId not in self.pendingTools:
                                newComponent = ToolExecutionComponent(
                                    read_field(content, "name"),
                                    contentId,
                                    read_field(content, "arguments"),
                                    {
                                        "showImages": self.settingsManager.getShowImages(),
                                        "imageWidthCells": self.settingsManager.getImageWidthCells(),
                                        "outputPad": self.outputPad,
                                    },
                                    self.getRegisteredToolDefinition(read_field(content, "name")),
                                    self.ui,
                                    self.sessionManager.getCwd(),
                                )
                                newComponent.setExpanded(self.toolOutputExpanded)
                                self.chatContainer.addChild(newComponent)
                                self.pendingTools[contentId] = newComponent
                            else:
                                existing = self.pendingTools.get(contentId)
                                if existing:
                                    existing.updateArgs(read_field(content, "arguments"))
                    self.ui.requestRender()

            case "message_end":
                message = read_field(event, "message")
                role = read_field(message, "role")
                if role == "user":
                    return
                if self.streamingComponent and role == "assistant":
                    self.streamingMessage = message
                    errorMessage: str | None = None
                    if read_field(self.streamingMessage, "stopReason") == "aborted":
                        retryAttempt = self.session.retryAttempt
                        errorMessage = (
                            f"Aborted after {retryAttempt} retry attempt{'s' if retryAttempt > 1 else ''}"
                            if retryAttempt > 0
                            else "Operation aborted"
                        )
                        _set_field(self.streamingMessage, "errorMessage", errorMessage)
                    self.streamingComponent.updateContent(self.streamingMessage, False)

                    if read_field(self.streamingMessage, "stopReason") in ("aborted", "error"):
                        if not errorMessage:
                            errorMessage = read_field(self.streamingMessage, "errorMessage") or "Error"
                        for pendingComponent in self.pendingTools.values():
                            pendingComponent.updateResult(
                                {"content": [{"type": "text", "text": errorMessage}], "isError": True}
                            )
                        self.pendingTools.clear()
                        self.maybeSuggestBugReport(self.streamingMessage)
                    else:
                        # Args are now complete - trigger diff computation for edit tools
                        for pendingComponent in self.pendingTools.values():
                            pendingComponent.setArgsComplete()
                        self.maybeShowThinkingDropNotice(self.streamingMessage)
                        self.maybeShowCacheMissNotice(self.streamingMessage)
                    self.streamingComponent = None
                    self.streamingMessage = None
                    self.footer.invalidate()
                elif role == "custom" and self._customMessageIsInTranscript(message):
                    # MISAKA fork: a custom message the session defers until its entry exists is placed by redrawing
                    # the session (see `_customMessageIsInTranscript`).
                    self.renderCurrentSessionState()
                self.ui.requestRender()

            case "bash_execution_update":
                # The bash execution callback handles TUI output rendering.
                pass

            case "tool_execution_start":
                # Nested calls (from codemode scripts) are shown inside their parent's row.
                if read_field(event, "parentToolCallId"):
                    return
                toolCallId = read_field(event, "toolCallId")
                component = self.pendingTools.get(toolCallId)
                if not component:
                    component = ToolExecutionComponent(
                        read_field(event, "toolName"),
                        toolCallId,
                        read_field(event, "args"),
                        {
                            "showImages": self.settingsManager.getShowImages(),
                            "imageWidthCells": self.settingsManager.getImageWidthCells(),
                            "outputPad": self.outputPad,
                        },
                        self.getRegisteredToolDefinition(read_field(event, "toolName")),
                        self.ui,
                        self.sessionManager.getCwd(),
                    )
                    component.setExpanded(self.toolOutputExpanded)
                    self.chatContainer.addChild(component)
                    self.pendingTools[toolCallId] = component
                component.markExecutionStarted()
                self.ui.requestRender()

            case "tool_execution_update":
                component = self.pendingTools.get(read_field(event, "toolCallId"))
                if component:
                    component.updateResult(_spread_tool_result(read_field(event, "partialResult"), False), True)
                    self.ui.requestRender()

            case "tool_execution_end":
                if read_field(event, "isError"):
                    self.maybeShowInstallChangeWarning()
                toolCallId = read_field(event, "toolCallId")
                component = self.pendingTools.get(toolCallId)
                if component:
                    component.updateResult(
                        _spread_tool_result(
                            read_field(event, "result"), read_field(event, "isError"), read_field(event, "durationMs")
                        )
                    )
                    self.pendingTools.pop(toolCallId, None)
                    self.ui.requestRender()

            case "agent_end":
                if self.settingsManager.getShowTerminalProgress():
                    self.ui.terminal.setProgress(False)
                self.clearStatusIndicator("working")
                if self.streamingComponent:
                    self.chatContainer.removeChild(self.streamingComponent)
                    self.streamingComponent = None
                    self.streamingMessage = None
                self.pendingTools.clear()

                self.ui.requestRender()

            case "agent_settled":
                await self.checkShutdownRequested()

            case "compaction_start":
                if self.settingsManager.getShowTerminalProgress():
                    self.ui.terminal.setProgress(True)
                # Keep editor active; submissions are queued during compaction.
                self.autoCompactionEscapeHandler = self.defaultEditor.onEscape
                self.defaultEditor.onEscape = lambda: self.session.abortCompaction()
                colorFn = (
                    (
                        lambda text: (
                            getattr(self.editor, "borderColor", None)
                            or theme.getThinkingBorderColor(self.session.thinkingLevel or "off")
                        )(text)
                    )
                    if isEmbeddedStatus(self.editor)
                    else None
                )
                self.showStatusIndicator(CompactionStatusIndicator(self.ui, read_field(event, "reason"), colorFn))
                self.ui.requestRender()

            case "compaction_end":
                if self.settingsManager.getShowTerminalProgress():
                    self.ui.terminal.setProgress(False)
                if self.autoCompactionEscapeHandler:
                    self.defaultEditor.onEscape = self.autoCompactionEscapeHandler
                    self.autoCompactionEscapeHandler = None
                self.clearStatusIndicator("compaction")
                result = read_field(event, "result")
                if read_field(event, "aborted"):
                    if read_field(event, "reason") == "manual":
                        self.showError("Compaction cancelled")
                    else:
                        self.showStatus("Auto-compaction cancelled")
                elif result:
                    entries = self.sessionManager.buildContextEntries()
                    if read_field(jsIndex(entries, 0), "type") != "compaction":
                        raise RuntimeError("Completed compaction is missing from the session context")
                    self.chatContainer.clear()
                    if read_field(entries[0], "contextMessages") is not None:
                        # MISAKA fork: a context engine's compaction carries the context it keeps, which is drawn in
                        # place of the summary.
                        self.renderSessionEntries(entries)
                    else:
                        # The latest compaction is prepended for model context; append it below at its chronological
                        # position.
                        self.renderSessionEntries(entries[1:])
                        self.addMessageToChat(
                            createCompactionSummaryMessage(
                                read_field(result, "summary"),
                                read_field(result, "tokensBefore"),
                                datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                            )
                        )
                        if read_field(result, "usage"):
                            self.addCompactionCostNotice(
                                {"type": "compaction_cost", "kind": "compaction", "usage": read_field(result, "usage")}
                            )
                    self.footer.invalidate()
                elif read_field(event, "errorMessage"):
                    if read_field(event, "reason") == "manual":
                        self.showError(read_field(event, "errorMessage"))
                    else:
                        self.chatContainer.addChild(Spacer(1))
                        compactionError = read_field(event, "errorMessage")
                        self.chatContainer.addChild(ThemedText(lambda: theme.fg("error", compactionError), 1, 0))
                self._schedule_task(self.flushCompactionQueue({"willRetry": read_field(event, "willRetry")}))
                self.ui.requestRender()

            case "auto_retry_start":
                # Set up escape to abort retry
                self.retryEscapeHandler = self.defaultEditor.onEscape
                self.defaultEditor.onEscape = lambda: self.session.abortRetry()
                self.showStatusIndicator(
                    RetryStatusIndicator(
                        self.ui, read_field(event, "attempt"), read_field(event, "maxAttempts"), read_field(event, "delayMs")
                    )
                )
                self.ui.requestRender()

            case "auto_retry_end":
                # Restore escape handler
                if self.retryEscapeHandler:
                    self.defaultEditor.onEscape = self.retryEscapeHandler
                    self.retryEscapeHandler = None
                self.clearStatusIndicator("retry")
                # Show error only on final failure (success shows normal response)
                if not read_field(event, "success"):
                    self.showError(
                        f"Retry failed after {read_field(event, 'attempt')} attempts: "
                        f"{read_field(event, 'finalError') or 'Unknown error'}"
                    )
                self.ui.requestRender()

            case "summarization_retry_scheduled":
                self.showError(read_field(event, "errorMessage"))
                self.showStatusIndicator(
                    RetryStatusIndicator(
                        self.ui, read_field(event, "attempt"), read_field(event, "maxAttempts"), read_field(event, "delayMs")
                    )
                )
                self.ui.requestRender()

            case "summarization_retry_attempt_start":
                self.clearStatusIndicator("retry")
                if read_field(event, "source") == "branchSummary":
                    self.showStatusIndicator(BranchSummaryStatusIndicator(self.ui))
                else:
                    colorFn = (
                        (
                            lambda text: (
                                getattr(self.editor, "borderColor", None)
                                or theme.getThinkingBorderColor(self.session.thinkingLevel or "off")
                            )(text)
                        )
                        if isEmbeddedStatus(self.editor)
                        else None
                    )
                    self.showStatusIndicator(CompactionStatusIndicator(self.ui, read_field(event, "reason"), colorFn))
                self.ui.requestRender()

            case "summarization_retry_finished":
                self.clearStatusIndicator("retry")
                self.ui.requestRender()

    def _customMessageIsInTranscript(self, message: Any) -> bool:
        """MISAKA fork: has the custom message just announced already been written to the session?

        The session emits ``message_end`` before it persists the message, so a custom message delivered by the agent
        loop (``followUp``/``steer``/``prompt``, which is how a Sisters task notification reaches the chat) is announced
        while the transcript still lacks its entry, and redrawing from it would erase the card ``message_start`` drew.
        Counting rather than looking for a match keeps two identical cards apart: the second is unpersisted exactly
        when the chat shows more copies than the transcript holds.
        """
        customType = read_field(message, "customType")
        content = read_field(message, "content")

        def matches(item: Any) -> bool:
            return read_field(item, "customType") == customType and read_field(item, "content") == content

        persisted = sum(
            1
            for entry in self.sessionManager.buildContextEntries()
            if read_field(entry, "type") in ("custom_message", "custom") and matches(entry)
        )
        if persisted == 0:
            return False
        drawn = sum(
            1
            for child in self.chatContainer.children
            if isinstance(child, CustomMessageComponent) and matches(child.message)
        )
        return persisted >= drawn

    def getUserMessageText(self, message: Any) -> str:
        """Extract text content from a user message"""
        if read_field(message, "role") != "user":
            return ""
        content = read_field(message, "content")
        textBlocks = (
            [{"type": "text", "text": content}]
            if isinstance(content, str)
            else [c for c in content or [] if read_field(c, "type") == "text"]
        )
        return "".join(read_field(c, "text") for c in textBlocks)

    def showManagedToolStatus(self, status: ToolStatus) -> None:
        """Show a managed-tool status update in the chat."""
        if not self.managedToolStatusStarted:
            self.chatContainer.addChild(Spacer(1))
            self.managedToolStatusStarted = True
        message = f"Warning: {status.message}" if status.type == "warning" else status.message
        color = "warning" if status.type == "warning" else "dim"
        self.chatContainer.addChild(ThemedText(lambda: theme.fg(color, message), 1, 0))
        self.lastStatusSpacer = None
        self.lastStatusText = None
        self.ui.requestRender()

    def showStatus(self, message: str) -> None:
        """Show a status message in the chat.

        If multiple status messages are emitted back-to-back (without anything else being added to the chat),
        we update the previous status line instead of appending new ones to avoid log spam.
        """
        children = self.chatContainer.children
        last = children[-1] if len(children) > 0 else None
        secondLast = children[-2] if len(children) > 1 else None

        if last and secondLast and last is self.lastStatusText and secondLast is self.lastStatusSpacer:
            self.lastStatusMessage = message
            self.lastStatusText.invalidate()
            self.ui.requestRender()
            return

        spacer = Spacer(1)
        self.lastStatusMessage = message
        text = ThemedText(lambda: theme.fg("dim", self.lastStatusMessage), 1, 0)
        self.chatContainer.addChild(spacer)
        self.chatContainer.addChild(text)
        self.lastStatusSpacer = spacer
        self.lastStatusText = text
        self.ui.requestRender()

    def addCustomEntryToChat(self, entry: Any) -> None:
        renderer = self.session.extensionRunner.getEntryRenderer(read_field(entry, "customType"))
        if not renderer:
            return
        component = CustomEntryComponent(entry, renderer, self.outputPad)
        component.setExpanded(self.toolOutputExpanded)
        if not component.hasContent():
            return

        if self.streamingComponent:
            streamingIndex = next(
                (index for index, child in enumerate(self.chatContainer.children) if child is self.streamingComponent),
                -1,
            )
            if streamingIndex >= 0:
                self.chatContainer.children.insert(streamingIndex, component)
                return

        self.chatContainer.addChild(component)

    def addMessageToChat(self, message: Any, options: dict[str, Any] | None = None) -> None:
        match read_field(message, "role"):
            case "bashExecution":
                bashComponent = BashExecutionComponent(
                    read_field(message, "command"),
                    self.ui,
                    read_field(message, "excludeFromContext"),
                    self.outputPad,
                )
                output = read_field(message, "output")
                if output:
                    bashComponent.appendOutput(output)
                bashComponent.setComplete(
                    read_field(message, "exitCode"),
                    read_field(message, "cancelled"),
                    _make_bash_truncation_result(output or "") if read_field(message, "truncated") else None,
                    read_field(message, "fullOutputPath"),
                )
                self.chatContainer.addChild(bashComponent)
            case "custom":
                if read_field(message, "display"):
                    renderer = self.session.extensionRunner.getMessageRenderer(read_field(message, "customType"))
                    customComponent = CustomMessageComponent(
                        message,
                        renderer,
                        self.getMarkdownThemeWithSettings(),
                        self.outputPad,
                    )
                    customComponent.setExpanded(self.toolOutputExpanded)
                    self.chatContainer.addChild(customComponent)
            case "compactionSummary":
                self.chatContainer.addChild(Spacer(1))
                compactionComponent = CompactionSummaryMessageComponent(
                    message,
                    self.getMarkdownThemeWithSettings(),
                    self.outputPad,
                )
                compactionComponent.setExpanded(self.toolOutputExpanded)
                self.chatContainer.addChild(compactionComponent)
            case "branchSummary":
                self.chatContainer.addChild(Spacer(1))
                branchComponent = BranchSummaryMessageComponent(
                    message,
                    self.getMarkdownThemeWithSettings(),
                    self.outputPad,
                )
                branchComponent.setExpanded(self.toolOutputExpanded)
                self.chatContainer.addChild(branchComponent)
            case "system":
                pass
            case "user":
                textContent = self.getUserMessageText(message)
                if textContent:
                    if len(self.chatContainer.children) > 0:
                        self.chatContainer.addChild(Spacer(1))
                    skillBlock = parseSkillBlock(textContent)
                    if skillBlock:
                        # Render skill block (collapsible)
                        skillComponent = SkillInvocationMessageComponent(
                            skillBlock,
                            self.getMarkdownThemeWithSettings(),
                            self.outputPad,
                        )
                        skillComponent.setExpanded(self.toolOutputExpanded)
                        self.chatContainer.addChild(skillComponent)
                        # Render user message separately if present
                        if read_field(skillBlock, "userMessage"):
                            self.chatContainer.addChild(Spacer(1))
                            self.chatContainer.addChild(
                                UserMessageComponent(
                                    read_field(skillBlock, "userMessage"),
                                    self.getMarkdownThemeWithSettings(),
                                    self.outputPad,
                                    self.getMarkdownTransformers(),
                                )
                            )
                    else:
                        self.chatContainer.addChild(
                            UserMessageComponent(
                                textContent,
                                self.getMarkdownThemeWithSettings(),
                                self.outputPad,
                                self.getMarkdownTransformers(),
                            )
                        )
                    # MISAKA fork: pi's editor history lives in memory, so every render of a session refills it from
                    # the transcript. An editor whose history is a file already holds what was typed, and refilling it
                    # appended the whole session again on every open, resume and reload.
                    if read_field(options, "populateHistory") and not getattr(self.editor, "historyFile", None):
                        addToHistory = getattr(self.editor, "addToHistory", None)
                        if callable(addToHistory):
                            addToHistory(textContent)
            case "assistant":
                self.chatContainer.addChild(
                    AssistantMessageComponent(
                        message,
                        self.hideThinkingBlock,
                        self.getMarkdownThemeWithSettings(),
                        self.hiddenThinkingLabel,
                        self.outputPad,
                        self.getMarkdownTransformers(),
                    )
                )
            case "toolResult":
                # Tool results are rendered inline with tool calls, handled separately
                pass

    def renderSessionItems(self, items: list[Any], options: dict[str, Any] | None = None) -> None:
        if options is None:
            options = {}
        self.pendingTools.clear()
        renderedPendingTools: dict[str, ToolExecutionComponent] = {}
        # Cache misses are not persisted, unlike successful cache-warming usage.
        # Re-derive them and inject them after the assistant messages that paid for them.
        cacheMisses: dict[int, CacheMiss] = (
            collectCacheMisses(self.sessionManager.getEntries(), self.session.modelRuntime)
            if self.settingsManager.getShowCacheMissNotices()
            else {}
        )

        if options.get("updateFooter"):
            self.footer.invalidate()
            self.updateEditorBorderColor()

        for item in items:
            if isCustomSessionEntry(item):
                self.addCustomEntryToChat(item)
                continue
            if isUsageSessionEntry(item):
                self.addCacheWarmingUsage(item)
                continue
            if isCompactionCostNotice(item):
                self.addCompactionCostNotice(item)
                continue

            message = item
            role = read_field(message, "role")
            stopReason = read_field(message, "stopReason")
            # Assistant messages need special handling for tool calls
            if role == "assistant":
                self.addMessageToChat(message)
                # Render tool call components
                for content in read_field(message, "content", []):
                    if read_field(content, "type") == "toolCall":
                        toolComponent = ToolExecutionComponent(
                            read_field(content, "name"),
                            read_field(content, "id"),
                            read_field(content, "arguments"),
                            {
                                "showImages": self.settingsManager.getShowImages(),
                                "imageWidthCells": self.settingsManager.getImageWidthCells(),
                                "outputPad": self.outputPad,
                            },
                            self.getRegisteredToolDefinition(read_field(content, "name")),
                            self.ui,
                            self.sessionManager.getCwd(),
                        )
                        toolComponent.setExpanded(self.toolOutputExpanded)
                        self.chatContainer.addChild(toolComponent)

                        if stopReason == "aborted" or stopReason == "error":
                            if stopReason == "aborted":
                                retryAttempt = self.session.retryAttempt
                                errorMessage = (
                                    f"Aborted after {retryAttempt} retry attempt{'s' if retryAttempt > 1 else ''}"
                                    if retryAttempt > 0
                                    else "Operation aborted"
                                )
                            else:
                                errorMessage = read_field(message, "errorMessage") or "Error"
                            toolComponent.updateResult(
                                {"content": [{"type": "text", "text": errorMessage}], "isError": True}
                            )
                        else:
                            renderedPendingTools[read_field(content, "id")] = toolComponent
                if stopReason != "aborted" and stopReason != "error":
                    miss = cacheMisses.get(id(message))
                    if miss:
                        self.addCacheMissNotice(miss)
            elif role == "toolResult":
                # Match tool results to pending tool components
                toolCallId = read_field(message, "toolCallId")
                component = renderedPendingTools.get(toolCallId)
                if component:
                    component.updateResult(message)
                    renderedPendingTools.pop(toolCallId, None)
            else:
                # All other messages use standard rendering
                self.addMessageToChat(message, options)

        for toolCallId, component in renderedPendingTools.items():
            self.pendingTools[toolCallId] = component
        self.ui.requestRender()

    def renderSessionEntries(self, entries: list[Any], options: dict[str, Any] | None = None) -> None:
        """Render session entries to chat. Used for initial load and rebuild after compaction.
        @param entries Compaction-aware session entries to render
        @param options.updateFooter Update footer state
        @param options.populateHistory Add user messages to editor history
        """
        # MISAKA fork (D1): no text selection to reset (`TuiAltScreen.resetTextSelection`).
        items: list[Any] = []
        for entry in entries:
            entryType = read_field(entry, "type")
            if entryType == "custom" or (entryType == "usage" and read_field(entry, "kind") == "cache_warm"):
                items.append(entry)
                continue
            # MISAKA fork: a context engine's compaction shows its summaries as compaction summaries, never as
            # user turns (`session_entry_to_display_messages`).
            messages = session_entry_to_display_messages(entry)
            if entryType in ("compaction", "branch_summary") and read_field(entry, "usage") and len(messages) > 0:
                items.extend([*messages, {"type": "compaction_cost", "kind": entryType, "usage": read_field(entry, "usage")}])
                continue
            items.extend(messages)
        self.renderSessionItems(items, options)

    def addCacheWarmingUsage(self, entry: Any) -> None:
        if not self.settingsManager.getShowCacheMissNotices():
            return
        self.chatContainer.addChild(Spacer(1))
        usage = formatCacheWarmingUsage(entry)
        self.chatContainer.addChild(ThemedText(lambda: theme.fg("dim", usage), 1, 0))

    def addCompactionCostNotice(self, notice: CompactionCostNotice) -> None:
        """Render billing usage for a compaction or branch summary. The notice is derived
        from persisted summary usage and is not stored as a separate session entry.
        """
        if not self.settingsManager.getShowCacheMissNotices():
            return

        usage = notice["usage"]
        tokens = (
            read_field(usage, "input")
            + read_field(usage, "output")
            + read_field(usage, "cacheRead")
            + read_field(usage, "cacheWrite")
        )
        total = read_field(read_field(usage, "cost"), "total")
        cost = f" (~${toFixed(total, 2)})" if total >= 0.01 else ""
        label = "Compaction" if notice["kind"] == "compaction" else "Branch summary"
        self.chatContainer.addChild(Spacer(1))
        self.chatContainer.addChild(
            ThemedText(lambda: theme.fg("warning", f"{label}: {formatTokens(tokens)} tokens billed{cost}"), 1, 0)
        )

    @staticmethod
    def countDroppedThinkingBlocks(message: Any) -> int:
        count = 0
        for diagnostic in read_field(message, "diagnostics") or []:
            if read_field(diagnostic, "type") != "anthropic_input_transformations":
                continue
            transformations = read_field(read_field(diagnostic, "details"), "transformations")
            if not isinstance(transformations, list):
                continue
            count += len(
                [
                    transformation
                    for transformation in transformations
                    if isinstance(transformation, dict) and transformation.get("type") == "thinking_dropped"
                ]
            )
        return count

    def maybeShowThinkingDropNotice(self, message: Any) -> None:
        if not self.settingsManager.getShowCacheMissNotices():
            return

        droppedCount = InteractiveMode.countDroppedThinkingBlocks(message)
        if droppedCount == 0:
            return

        previousDroppedCount = 0
        # message_end reaches the UI before the current message is persisted,
        # so the branch's last assistant message is the previous response.
        branch = self.sessionManager.getBranch()
        for entry in reversed(branch):
            if read_field(entry, "type") == "message" and read_field(read_field(entry, "message"), "role") == "assistant":
                previousDroppedCount = InteractiveMode.countDroppedThinkingBlocks(read_field(entry, "message"))
                break
        if droppedCount <= previousDroppedCount:
            return

        noun = "thinking block" if droppedCount == 1 else "thinking blocks"
        self.chatContainer.addChild(Spacer(1))
        self.chatContainer.addChild(
            ThemedText(
                lambda: theme.fg("warning", f"Anthropic dropped {droppedCount} {noun} (details in session)"),
                1,
                0,
            )
        )

    def maybeShowCacheMissNotice(self, message: Any) -> None:
        """Show a transcript notice when a completed assistant message paid for a
        significant cache miss. Only states observable facts: the miss itself,
        a model switch, or an idle gap past the cache TTL.
        """
        if not self.settingsManager.getShowCacheMissNotices():
            return

        # Entries don't contain `message` yet: message_end fires before persistence.
        miss = detectCacheMiss(self.sessionManager.getEntries(), message, self.session.modelRuntime)
        if miss:
            self.addCacheMissNotice(miss)

    def addCacheMissNotice(self, miss: CacheMiss) -> None:
        if miss.missedTokens < 20_000 and miss.missedCost < 0.1:
            return

        cost = f" (~${toFixed(miss.missedCost, 2)})" if miss.missedCost >= 0.01 else ""
        reBilled = f"{formatTokens(miss.missedTokens)} tokens re-billed{cost}"
        label = "Cache miss"
        if miss.modelChanged:
            label = "Cache miss after model switch"
        elif miss.idleMs >= CACHE_TTL_MS:
            label = f"Cache miss after {jsRound(miss.idleMs / 60_000)}m idle"
        self.chatContainer.addChild(Spacer(1))
        self.chatContainer.addChild(ThemedText(lambda: theme.fg("warning", f"{label}: {reBilled}"), 1, 0))

    def renderInitialMessages(self) -> None:
        entries = self.sessionManager.buildContextEntries()
        self.renderSessionEntries(entries, {"updateFooter": True, "populateHistory": True})
        self.renderProjectTrustWarningIfNeeded()

        # Show compaction info if session was compacted
        allEntries = self.sessionManager.getEntries()
        compactionCount = len([e for e in allEntries if read_field(e, "type") == "compaction"])
        if compactionCount > 0:
            times = "1 time" if compactionCount == 1 else f"{compactionCount} times"
            self.showStatus(f"Session compacted {times}")

    def renderProjectTrustWarningIfNeeded(self) -> None:
        if self.settingsManager.isProjectTrusted() or not hasTrustRequiringProjectResources(
            self.sessionManager.getCwd()
        ):
            return

        if len(self.chatContainer.children) > 0:
            self.chatContainer.addChild(Spacer(1))
        # MISAKA fork: names what an untrusted project loses in MISAKA (settings, prompts, themes and Sisters agent
        # definitions) and the product to restart.
        self.chatContainer.addChild(
            ThemedText(
                lambda: theme.fg(
                    "warning",
                    f"This project is not trusted. Project {home.DIR_NAME}/settings.json, {home.DIR_NAME}/prompts, "
                    f"{home.DIR_NAME}/themes, and Sisters project agent definitions in "
                    f"{home.DIR_NAME}/{home.SUBAGENTS_DIR} are ignored. Use /trust to save a trust decision, then "
                    f"restart {APP_NAME}.",
                ),
                1,
                0,
            )
        )

    async def getUserInput(self) -> str:
        if self.pendingUserInputs:
            return self.pendingUserInputs.pop(0)

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        # MISAKA fork: `shutdown` cancels the waiting input, since the process is not exited from inside.
        self._pendingUserInputFuture = future

        def onInputCallback(text: str) -> None:
            self.onInputCallback = None
            if not future.done():
                future.set_result(text)

        self.onInputCallback = onInputCallback
        try:
            return await future
        finally:
            if self.onInputCallback is onInputCallback:
                self.onInputCallback = None
            if self._pendingUserInputFuture is future:
                self._pendingUserInputFuture = None

    def rebuildChatFromMessages(self) -> None:
        self.chatContainer.clear()
        self.renderSessionEntries(self.sessionManager.buildContextEntries())

    # =========================================================================
    # Key handlers
    # =========================================================================

    def handleCtrlC(self) -> None:
        now = time.time() * 1000
        if now - self.lastSigintTime < 500:
            self._schedule_task(self.shutdown())
            return
        self.lastSigintTime = now
        # MISAKA fork: Ctrl+C while busy interrupts through the same abort path as Escape (pi only clears the line);
        # a double press still exits and an idle press still clears.
        if self.session.isStreaming:
            self.restoreQueuedMessagesToEditor({"abort": True})
            return
        if self.session.isBashRunning:
            self.session.abortBash()
            return
        self.clearEditor()

    def handleCtrlD(self) -> None:
        # Only called when editor is empty (enforced by CustomEditor)
        self._schedule_task(self.shutdown())

    async def shutdown(self, options: dict[str, Any] | None = None) -> int:
        """Gracefully shutdown the agent.
        Stops the TUI before emitting shutdown events so extension UI cleanup cannot
        repaint the final frame while the process is exiting.

        MISAKA fork: returns the exit code, which `run` hands to the engine, instead of exiting the process.
        """
        if self.isShuttingDown:
            return await asyncio.shield(self._shutdownFuture) if self._shutdownFuture is not None else 0
        self.isShuttingDown = True
        # Keep signal handlers registered until terminal cleanup has completed.

        # MISAKA fork: the input loop stops waiting for input.
        if self._pendingUserInputFuture is not None and not self._pendingUserInputFuture.done():
            self._pendingUserInputFuture.cancel()
        self.onInputCallback = None

        if read_field(options, "fromSignal"):
            # Signal-triggered shutdown (SIGTERM/SIGHUP). Emit extension cleanup
            # (session_shutdown) BEFORE touching the terminal. Extension teardown
            # such as removing sockets does not write to the tty, so it must not be
            # skipped if a later terminal-restore write fails on a dead or stalled
            # terminal.
            await self.runtimeHost.dispose()
            # MISAKA fork (D11): no theme auto-sync to disable.
            await self.ui.terminal.drainInput(1000)
            self.stop()
            return self._finishShutdown()

        # Interactive quit (Ctrl+D, Ctrl+C, /quit, extension shutdown()). Stop the
        # TUI before emitting shutdown events so extension UI cleanup cannot repaint
        # the final frame while the process is exiting.
        # Drain any in-flight Kitty key release events before stopping.
        # This prevents escape sequences from leaking to the parent shell over slow SSH.
        await self.ui.terminal.drainInput(1000)

        self.stop()
        await self.runtimeHost.dispose()

        resumeCommand = formatResumeCommand(self.sessionManager)
        if resumeCommand:
            sys.stdout.write(f"{chalk.dim('To resume this session:')} {resumeCommand}\n")

        return self._finishShutdown()

    def _finishShutdown(self) -> int:
        """MISAKA fork: `process.exit(0)` resolves the exit-code future `run` waits on."""
        if self._shutdownFuture is not None and not self._shutdownFuture.done():
            self._shutdownFuture.set_result(0)
        return 0

    def emergencyTerminalExit(self) -> NoReturn:
        self.isShuttingDown = True
        self.unregisterSignalHandlers()
        killTrackedDetachedChildren()
        # The terminal is gone. Do not run normal shutdown because TUI and
        # extension cleanup can write restore sequences and re-trigger EIO.
        # MISAKA fork: `SystemExit` in place of `process.exit(129)`.
        raise SystemExit(129)

    def uncaughtCrash(self, error: BaseException) -> NoReturn:
        """Last-resort handler for uncaught exceptions. The TUI puts stdin into raw
        mode and hides the cursor; without this handler, an uncaught throw from
        anywhere (e.g. an extension callback) tears down the process while leaving
        the terminal in raw mode with no cursor, requiring `stty sane && reset` to
        recover.

        Unlike emergencyTerminalExit, the terminal is still alive here, so we
        call ui.stop() to restore cooked mode, the cursor, and disable bracketed
        paste / Kitty / modifyOtherKeys sequences.
        """
        # A dead terminal is not a crash. Do not try to restore it.
        if isDeadTerminalError(error):
            self.emergencyTerminalExit()
        if self.isShuttingDown:
            raise SystemExit(1)
        self.isShuttingDown = True
        with contextlib.suppress(Exception):
            self.unregisterSignalHandlers()
        with contextlib.suppress(Exception):
            killTrackedDetachedChildren()
        with contextlib.suppress(Exception):
            self.ui.stop()
        print(f"{APP_NAME} exiting due to uncaughtException:", file=sys.stderr)
        traceback.print_exception(error, file=sys.stderr)
        extensionHint = self.getCrashExtensionHint(error)
        if extensionHint:
            print(f"\n{extensionHint}", file=sys.stderr)
        # MISAKA fork: no crash record or `/bug` instructions (MISAKA has no `/bug`).
        raise SystemExit(1)

    async def checkShutdownRequested(self) -> None:
        """Check if shutdown was requested and perform shutdown if so."""
        if not self.shutdownRequested:
            return
        await self.shutdown()

    def registerSignalHandlers(self) -> None:
        self.unregisterSignalHandlers()

        signals = [signal.SIGTERM]
        if sys.platform != "win32":
            signals.append(signal.SIGHUP)  # windows-footgun: ok - guarded above

        for signum in signals:

            def handler(_signum: int, _frame: Any) -> None:
                # SIGHUP no longer hard-exits: graceful shutdown emits session_shutdown
                # first, then attempts terminal restore. A genuinely dead terminal
                # surfaces as an EIO on the restore writes, which ends in
                # emergencyTerminalExit.
                killTrackedDetachedChildren()
                self._schedule_task(self.shutdown({"fromSignal": True}))

            try:
                previous = signal.getsignal(signum)
                signal.signal(signum, handler)
            except (AttributeError, ValueError):
                continue

            def restore(signum: signal.Signals = signum, previous: Any = previous) -> None:
                signal.signal(signum, previous)

            self.signalCleanupHandlers.append(restore)

        # MISAKA fork: pi also listens for stream errors on stdin/stdout/stderr and turns a dead terminal's EIO/ENOTTY
        # into emergencyTerminalExit. Python's standard streams have no error events; a dead terminal's OSError
        # reaches `uncaughtCrash` below, which exits the same way.

        # Restore the terminal before the process dies on any uncaught throw.
        # Without this, an unhandled exception from extension code (or anywhere
        # in the app) leaves the terminal in raw mode with no cursor.
        previousExcepthook = sys.excepthook

        def uncaughtExceptionHandler(_type: type[BaseException], error: BaseException, _traceback: Any) -> None:
            self.uncaughtCrash(error)

        sys.excepthook = uncaughtExceptionHandler
        self.signalCleanupHandlers.append(lambda: setattr(sys, "excepthook", previousExcepthook))

        # MISAKA fork: an exception that escapes a helper thread is handled on the UI thread, which owns the terminal
        # and the signal handlers (Node has one thread).
        previousThreadingExcepthook = threading.excepthook
        try:
            ownerLoop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            ownerLoop = None

        def threadingExcepthook(args: Any) -> None:
            if isinstance(args.exc_value, BaseException) and ownerLoop is not None and not ownerLoop.is_closed():
                try:
                    ownerLoop.call_soon_threadsafe(self.uncaughtCrash, args.exc_value)
                    return
                except RuntimeError:
                    pass
            previousThreadingExcepthook(args)

        threading.excepthook = threadingExcepthook
        self.signalCleanupHandlers.append(lambda: setattr(threading, "excepthook", previousThreadingExcepthook))

        # MISAKA fork: an exception raised in an event-loop callback, or a task failure nobody awaited, is what Node
        # reports as an uncaught exception or an unhandled rejection; asyncio hands both to the loop's handler.
        if ownerLoop is not None:
            previousLoopHandler = ownerLoop.get_exception_handler()

            def loopExceptionHandler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
                error = context.get("exception")
                if isinstance(error, Exception):
                    self.uncaughtCrash(error)
                    return
                if previousLoopHandler is not None:
                    previousLoopHandler(loop, context)
                else:
                    loop.default_exception_handler(context)

            ownerLoop.set_exception_handler(loopExceptionHandler)
            self.signalCleanupHandlers.append(lambda: ownerLoop.set_exception_handler(previousLoopHandler))

    def unregisterSignalHandlers(self) -> None:
        for cleanup in self.signalCleanupHandlers:
            cleanup()
        self.signalCleanupHandlers = []

    def handleCtrlZ(self) -> None:
        if sys.platform == "win32":
            self.showStatus("Suspend to background is not supported on Windows")
            return

        # MISAKA fork: Python keeps running while suspended, so no keep-alive timer is needed.

        # Ignore SIGINT while suspended so Ctrl+C in the terminal does not
        # kill the backgrounded process. The handler is removed on resume.
        previousSigint = signal.getsignal(signal.SIGINT)
        previousSigcont = signal.getsignal(signal.SIGCONT)  # windows-footgun: ok - guarded above

        def ignoreSigint(_signum: int, _frame: Any) -> None:
            return None

        # Set up handler to restore TUI when resumed
        def onSigcont(_signum: int, _frame: Any) -> None:
            signal.signal(signal.SIGCONT, previousSigcont)  # windows-footgun: ok - guarded above
            signal.signal(signal.SIGINT, previousSigint)
            self.ui.start()
            self.ui.requestRender(True)

        signal.signal(signal.SIGINT, ignoreSigint)
        signal.signal(signal.SIGCONT, onSigcont)  # windows-footgun: ok - guarded above

        try:
            # Stop the TUI (restore terminal to normal mode)
            self.ui.stop()

            # MISAKA fork: a stopped process is not a stalled event loop.
            loop_watchdog.suspend()
            # Send SIGTSTP to process group (pid=0 means all processes in group)
            os.kill(0, signal.SIGTSTP)  # windows-footgun: ok - guarded above
        except BaseException:
            signal.signal(signal.SIGINT, previousSigint)
            signal.signal(signal.SIGCONT, previousSigcont)  # windows-footgun: ok - guarded above
            raise

    async def handleFollowUp(self) -> None:
        text = jsTrim(self.getEditorExpandedText())
        if not text:
            return

        # Queue input during compaction (extension commands execute immediately)
        if self.session.isCompacting:
            if self.isExtensionCommand(text):
                addToHistory = getattr(self.editor, "addToHistory", None)
                if callable(addToHistory):
                    addToHistory(text)
                self.editor.setText("")
                await self.session.prompt(text)
            else:
                self.queueCompactionMessage(text, "followUp")
            return

        # Alt+Enter queues a follow-up message (waits until agent finishes)
        # This handles extension commands (execute immediately), prompt template expansion, and queueing
        if self.session.isStreaming:
            addToHistory = getattr(self.editor, "addToHistory", None)
            if callable(addToHistory):
                addToHistory(text)
            self.editor.setText("")
            await self.session.prompt(text, {"streamingBehavior": "followUp"})
            self.updatePendingMessagesDisplay()
            self.ui.requestRender()
        # If not streaming, Alt+Enter acts like regular Enter (trigger onSubmit)
        elif self.editor.onSubmit:
            self.editor.setText("")
            self.editor.onSubmit(text)

    def getEditorExpandedText(self) -> str:
        """MISAKA fork: ``this.editor.getExpandedText?.() ?? this.editor.getText()``."""
        getExpandedText = getattr(self.editor, "getExpandedText", None)
        return getExpandedText() if callable(getExpandedText) else self.editor.getText()

    def handleDequeue(self) -> None:
        restored = self.restoreQueuedMessagesToEditor()
        if restored == 0:
            self.showStatus("No queued messages to restore")
        else:
            self.showStatus(f"Restored {restored} queued message{'s' if restored > 1 else ''} to editor")

    def updateEditorBorderColor(self) -> None:
        if self.isBashMode:
            self.editor.borderColor = theme.getBashModeBorderColor()
        else:
            level: ThinkingLevel = self.session.thinkingLevel or "off"
            self.editor.borderColor = theme.getThinkingBorderColor(level)
        if self.activeStatusIndicator is not None:
            self.activeStatusIndicator.invalidate()
        self.ui.requestRender()

    def _refuseSwitchDuringCompaction(self, what: str) -> bool:
        """MISAKA fork: pi lets the model or thinking level change while a compaction runs, and the session then
        discards the compaction when it lands. A context engine's compaction can take minutes (a 1.19M-token Sister
        lost a five-minute one to three switches and overflowed), so the switch waits, as `/reload` does."""
        if self.session.isCompacting:
            self.showWarning(f"Wait for compaction to finish before changing the {what}.")
            return True
        return False

    def cycleThinkingLevel(self) -> None:
        if self._refuseSwitchDuringCompaction("thinking level"):
            return
        newLevel = self.session.cycleThinkingLevel()
        if newLevel is None:
            self.showStatus("Current model does not support thinking")
        else:
            self.footer.invalidate()
            self.updateEditorBorderColor()
            self.showStatus(f"Thinking level: {newLevel}")

    async def cycleModel(self, direction: Literal["forward", "backward"]) -> None:
        if self._refuseSwitchDuringCompaction("model"):
            return
        try:
            result = await self.session.cycleModel(direction)
            if result is None:
                msg = "Only one model in scope" if len(self.session.scopedModels) > 0 else "Only one model available"
                self.showStatus(msg)
            else:
                self.footer.invalidate()
                self.updateEditorBorderColor()
                model = read_field(result, "model")
                thinkingLevel = read_field(result, "thinkingLevel")
                thinkingStr = (
                    f" (thinking: {thinkingLevel})" if read_field(model, "reasoning") and thinkingLevel != "off" else ""
                )
                self.showStatus(f"Switched to {read_field(model, 'name') or read_field(model, 'id')}{thinkingStr}")
                self._schedule_task(self.maybeWarnAboutAnthropicSubscriptionAuth(model))
        except Exception as error:  # noqa: BLE001 - shown to the user
            self.showError(errorMessage(error))

    def toggleToolOutputExpansion(self) -> None:
        self.setToolsExpanded(not self.toolOutputExpanded)

    def setToolsExpanded(self, expanded: bool) -> None:
        if expanded == self.toolOutputExpanded:
            return

        self.toolOutputExpanded = expanded
        activeHeader = self.customHeader if self.customHeader is not None else self.builtInHeader
        if isExpandable(activeHeader):
            activeHeader.setExpanded(expanded)
        for container in (self.loadedResourcesContainer, self.chatContainer):
            for child in container.children:
                if isExpandable(child):
                    child.setExpanded(expanded)
        self.showStatus(f"Tool output: {'expanded' if expanded else 'collapsed'}")

    def updateThinkingBlockVisibility(self) -> None:
        """Update rendered assistant messages without rebuilding live tool components."""
        for child in self.chatContainer.children:
            if isinstance(child, AssistantMessageComponent):
                child.setHideThinkingBlock(self.hideThinkingBlock)
        self.ui.requestRender()

    def toggleThinkingBlockVisibility(self) -> None:
        self.hideThinkingBlock = not self.hideThinkingBlock
        self.settingsManager.setHideThinkingBlock(self.hideThinkingBlock)
        self.updateThinkingBlockVisibility()
        self.showStatus(f"Thinking blocks: {'hidden' if self.hideThinkingBlock else 'visible'}")

    async def handleOpenExternalEditor(self) -> None:
        editorCmd = self.settingsManager.getExternalEditorCommand()
        content = self.getEditorExpandedText()
        self.ui.stop()
        try:
            result = await editInExternalEditor({"command": editorCmd, "content": content})
            if result["status"] == "complete":
                self.editor.setText(result["content"])
        finally:
            self.ui.start()
            self.ui.requestRender(True)

    # =========================================================================
    # UI helpers
    # =========================================================================

    def clearEditor(self) -> None:
        self.editor.setText("")
        self.ui.requestRender()

    def showError(self, errorMessage: str) -> None:
        self.chatContainer.addChild(Spacer(1))
        self.chatContainer.addChild(ThemedText(lambda: theme.fg("error", f"Error: {errorMessage}"), self.outputPad, 0))
        self.ui.requestRender()

    def showWarning(self, warningMessage: str) -> None:
        self.chatContainer.addChild(Spacer(1))
        self.chatContainer.addChild(ThemedText(lambda: theme.fg("warning", f"Warning: {warningMessage}"), 1, 0))
        self.ui.requestRender()

    # MISAKA fork: no `showNewVersionNotification` or `showPackageUpdateNotification` (no pi version check and no
    # package sources).

    def getAllQueuedMessages(self) -> dict[str, list[str]]:
        """Get all queued messages (read-only).
        Combines session queue and compaction queue.
        """
        return {
            "steering": [
                *self.session.getSteeringMessages(),
                *[msg["text"] for msg in self.compactionQueuedMessages if msg["mode"] == "steer"],
            ],
            "followUp": [
                *self.session.getFollowUpMessages(),
                *[msg["text"] for msg in self.compactionQueuedMessages if msg["mode"] == "followUp"],
            ],
        }

    def clearAllQueues(self) -> dict[str, list[str]]:
        """Clear all queued messages and return their contents.
        Clears both session queue and compaction queue.
        """
        cleared = self.session.clearQueue()
        steering = read_field(cleared, "steering")
        followUp = read_field(cleared, "followUp")
        compactionSteering = [msg["text"] for msg in self.compactionQueuedMessages if msg["mode"] == "steer"]
        compactionFollowUp = [msg["text"] for msg in self.compactionQueuedMessages if msg["mode"] == "followUp"]
        self.compactionQueuedMessages = []
        return {
            "steering": [*steering, *compactionSteering],
            "followUp": [*followUp, *compactionFollowUp],
        }

    def updatePendingMessagesDisplay(self) -> None:
        self.pendingMessagesContainer.clear()
        queued = self.getAllQueuedMessages()
        steeringMessages = queued["steering"]
        followUpMessages = queued["followUp"]
        if len(steeringMessages) > 0 or len(followUpMessages) > 0:
            self.pendingMessagesContainer.addChild(Spacer(1))
            for message in steeringMessages:
                text = theme.fg("dim", f"Steering: {message}")
                self.pendingMessagesContainer.addChild(TruncatedText(text, 1, 0))
            for message in followUpMessages:
                text = theme.fg("dim", f"Follow-up: {message}")
                self.pendingMessagesContainer.addChild(TruncatedText(text, 1, 0))
            dequeueHint = self.getAppKeyDisplay("app.message.dequeue")
            hintText = theme.fg("dim", f"↳ {dequeueHint} to edit all queued messages")
            self.pendingMessagesContainer.addChild(TruncatedText(hintText, 1, 0))

    def restoreQueuedMessagesToEditor(self, options: dict[str, Any] | None = None) -> int:
        cleared = self.clearAllQueues()
        allQueued = [*cleared["steering"], *cleared["followUp"]]
        if len(allQueued) == 0:
            self.updatePendingMessagesDisplay()
            if read_field(options, "abort"):
                self._schedule_task(self.session.abort())
            return 0
        queuedText = "\n\n".join(allQueued)
        currentText = read_field(options, "currentText")
        if currentText is None:
            currentText = self.editor.getText()
        combinedText = "\n\n".join(t for t in (queuedText, currentText) if jsTrim(t))
        self.editor.setText(combinedText)
        self.updatePendingMessagesDisplay()
        if read_field(options, "abort"):
            self._schedule_task(self.session.abort())
        return len(allQueued)

    def queueCompactionMessage(self, text: str, mode: Literal["steer", "followUp"]) -> None:
        self.compactionQueuedMessages.append({"text": text, "mode": mode})
        addToHistory = getattr(self.editor, "addToHistory", None)
        if callable(addToHistory):
            addToHistory(text)
        self.editor.setText("")
        self.updatePendingMessagesDisplay()
        self.showStatus("Queued message for after compaction")

    def isExtensionCommand(self, text: str) -> bool:
        if not text.startswith("/"):
            return False
        # MISAKA fork: a core prompt command (a skill) is prepared and sent as a prompt, not run as a command.
        if self.isCorePromptCommand(text):
            return False

        extensionRunner = self.session.extensionRunner

        spaceIndex = text.find(" ")
        commandName = text[1:] if spaceIndex == -1 else text[1:spaceIndex]
        # MISAKA fork: a core part's command runs the same way as an extension's.
        return bool(extensionRunner.getCommand(commandName)) or self.session.moments.command(commandName) is not None

    def isCorePromptCommand(self, text: str) -> bool:
        """MISAKA fork: whether the text invokes a core prompt command (a skill)."""
        return self.session.getCorePromptCommand(text) is not None

    def isPromptTemplate(self, text: str) -> bool:
        """MISAKA fork: whether the text names a prompt template, which the menu offers and `prompt()` expands."""
        invocation = parse_prompt_template_invocation(text)
        if invocation is None:
            return False
        name, _args = invocation
        return any(template.name == name for template in self.session.promptTemplates)

    async def flushCompactionQueue(self, options: dict[str, Any] | None = None) -> None:
        if len(self.compactionQueuedMessages) == 0:
            return

        queuedMessages = list(self.compactionQueuedMessages)
        self.compactionQueuedMessages = []
        self.updatePendingMessagesDisplay()

        # MISAKA fork: a skill's preparation can outlast even a compaction retry, so a failure restores only the
        # messages the session has not accepted yet: already accepted steering and follow-ups are never cleared, and
        # a prompt that reached its preflight acceptance is never sent twice. pi clears the session queue and restores
        # every message.
        pending = list(queuedMessages)
        restored = False
        promptTask: asyncio.Task[Any] | None = None

        def accepted(message: CompactionQueuedMessage) -> None:
            pending[:] = [item for item in pending if item is not message]
            if restored:
                self.compactionQueuedMessages = [item for item in self.compactionQueuedMessages if item is not message]
                self.updatePendingMessagesDisplay()

        def restoreQueue(error: Any) -> None:
            nonlocal restored
            if restored:
                return
            restored = True
            self.compactionQueuedMessages = [*pending, *self.compactionQueuedMessages]
            self.updatePendingMessagesDisplay()
            self.showError(f"Failed to send queued message{'s' if len(pending) > 1 else ''}: {errorMessage(error)}")

        async def dispatch(message: CompactionQueuedMessage, forceQueue: bool = False) -> None:
            if restored:
                return
            if self.isExtensionCommand(message["text"]):
                await self.session.prompt(message["text"])
            elif self.isCorePromptCommand(message["text"]) or (not forceQueue and not self.session.isStreaming):
                # MISAKA fork: prompt() decides whether to queue or start once the skill is prepared.
                await self.session.prompt(
                    message["text"],
                    {"streamingBehavior": message["mode"], "preflightResult": lambda _disposition: accepted(message)},
                )
            elif message["mode"] == "followUp":
                await self.session.followUp(message["text"])
            else:
                await self.session.steer(message["text"])
            accepted(message)

        try:
            if read_field(options, "willRetry"):
                # When retry is pending, queue messages for the retry turn
                for message in queuedMessages:
                    await dispatch(message, True)
                self.updatePendingMessagesDisplay()
                return

            # Find first non-extension-command message to use as prompt
            firstPromptIndex = next(
                (index for index, message in enumerate(queuedMessages) if not self.isExtensionCommand(message["text"])),
                -1,
            )
            if firstPromptIndex == -1:
                # All extension commands - execute them all
                for message in queuedMessages:
                    await dispatch(message)
                return

            # Execute any extension commands before the first prompt
            preCommands = queuedMessages[:firstPromptIndex]
            firstPrompt = queuedMessages[firstPromptIndex]
            rest = queuedMessages[firstPromptIndex + 1 :]

            for message in preCommands:
                await dispatch(message)

            # Start a prompt when idle, or queue it into a run still finishing compaction.
            preflight: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

            def reportPreflight(success: bool) -> None:
                if success:
                    accepted(firstPrompt)
                if not preflight.done():
                    preflight.set_result(success)

            async def promptFirst() -> None:
                try:
                    await self.session.prompt(
                        firstPrompt["text"],
                        {
                            "streamingBehavior": firstPrompt["mode"],
                            "preflightResult": lambda _disposition: reportPreflight(True),
                        },
                    )
                except asyncio.CancelledError:
                    if any(item is firstPrompt for item in pending):
                        restoreQueue("Queued prompt cancelled before submission")
                    raise
                except Exception as error:  # noqa: BLE001 - upstream `.catch(restoreQueue)`
                    restoreQueue(error)
                else:
                    reportPreflight(True)
                finally:
                    if not preflight.done():
                        preflight.set_result(False)

            promptTask = spawn(promptFirst(), self._backgroundTasks)
            # MISAKA fork: later messages wait until the first prompt is accepted, so its failure cannot restore
            # messages they already sent.
            if not await asyncio.shield(preflight):
                return

            # Queue remaining messages
            for message in rest:
                await dispatch(message)
            self.updatePendingMessagesDisplay()
        except asyncio.CancelledError:
            if promptTask is not None and not promptTask.done():
                promptTask.cancel()
                await asyncio.gather(promptTask, return_exceptions=True)
            restoreQueue("Queued submission cancelled")
            raise
        except Exception as error:  # noqa: BLE001 - restored and shown
            restoreQueue(error)

    def flushPendingBashComponents(self) -> None:
        """Move pending bash components from pending area to chat"""
        for component in self.pendingBashComponents:
            self.pendingMessagesContainer.removeChild(component)
            self.chatContainer.addChild(component)
        self.pendingBashComponents = []

    # =========================================================================
    # Selectors
    # =========================================================================

    def disposeActiveSelector(self) -> None:
        dispose = self.activeSelectorDispose
        self.activeSelectorToken = None
        self.activeSelectorDispose = None
        if dispose:
            dispose()

    def showSelector(self, create: Callable[[Callable[[], None]], dict[str, Any]]) -> None:
        """Shows a selector component in place of the editor.
        @param create Factory that receives a `done` callback and returns the component and focus target
        """
        token = object()
        dispose: Callable[[], None] | None = None

        def done() -> None:
            if dispose:
                dispose()
            if self.activeSelectorToken is not token:
                return
            self.activeSelectorToken = None
            self.activeSelectorDispose = None
            self.editorContainer.clear()
            self.editorContainer.addChild(self.editor)
            self.ui.setFocus(self.editor)

        created = create(done)
        dispose = created.get("dispose")
        self.disposeActiveSelector()
        self.activeSelectorToken = token
        self.activeSelectorDispose = dispose
        self.editorContainer.clear()
        self.editorContainer.addChild(created["component"])
        self.ui.setFocus(created["focus"])
        self.ui.requestRender()

    def showSettingsSelector(self) -> None:
        def create(done: Callable[[], None]) -> dict[str, Any]:
            defaultProvider = self.settingsManager.getDefaultProvider()
            defaultModelId = self.settingsManager.getDefaultModel()
            defaultModel = f"{defaultProvider}/{defaultModelId}" if defaultProvider and defaultModelId else "not set"

            def onAutoCompactChange(enabled: bool) -> None:
                self.session.setAutoCompactionEnabled(enabled)
                self.footer.setAutoCompactEnabled(enabled)

            def onShowImagesChange(enabled: bool) -> None:
                self.settingsManager.setShowImages(enabled)
                for child in self.chatContainer.children:
                    if isinstance(child, ToolExecutionComponent):
                        child.setShowImages(enabled)

            def onImageWidthCellsChange(width: int) -> None:
                self.settingsManager.setImageWidthCells(width)
                for child in self.chatContainer.children:
                    if isinstance(child, ToolExecutionComponent):
                        child.setImageWidthCells(width)

            def onEnableSkillCommandsChange(enabled: bool) -> None:
                self.settingsManager.setEnableSkillCommands(enabled)
                self.setupAutocompleteProvider()

            def onTransportChange(transport: Any) -> None:
                self.settingsManager.setTransport(transport)
                self.session.agent.transport = transport

            def onSessionShareDestinationChange(destination: Any) -> None:
                self.settingsManager.setSessionShareDestination(destination)

            def onHttpIdleTimeoutMsChange(timeoutMs: int) -> None:
                self.settingsManager.setHttpIdleTimeoutMs(timeoutMs)
                configureHttpDispatcher(timeoutMs)
                self.showStatus(f"HTTP idle timeout: {formatHttpIdleTimeoutMs(timeoutMs)}")

            def onCacheWarmingModeChange(mode: Any) -> None:
                self.session.setCacheWarmingMode(mode)
                self.showStatus(f"Cache warming: {mode}")

            def onModelThinkingLevelChange(provider: str, modelId: str, level: ThinkingLevel) -> None:
                self.settingsManager.setModelThinkingLevel(provider, modelId, level)
                # If the override is for the current model, apply it to the session too
                current = self.session.model
                if current and current.provider == provider and current.id == modelId:
                    self.session.setThinkingLevel(level)
                    self.footer.invalidate()
                    self.updateEditorBorderColor()

            def onModelThinkingLevelRemove(provider: str, modelId: str) -> None:
                self.settingsManager.removeModelThinkingLevel(provider, modelId)
                # If the override was for the current model, revert to global default
                current = self.session.model
                if current and current.provider == provider and current.id == modelId:
                    globalDefault = self.settingsManager.getDefaultThinkingLevel() or DEFAULT_THINKING_LEVEL
                    self.session.setThinkingLevel(globalDefault)
                    self.footer.invalidate()
                    self.updateEditorBorderColor()

            def onThemeChange(themeSetting: str) -> None:
                self.settingsManager.setTheme(themeSetting)
                self.themeController.setThemeSetting(themeSetting)

            def onHideThinkingBlockChange(hidden: bool) -> None:
                self.hideThinkingBlock = hidden
                self.settingsManager.setHideThinkingBlock(hidden)
                self.updateThinkingBlockVisibility()

            def onMermaidRenderingModeChange(mode: MermaidRenderingMode) -> None:
                self.settingsManager.setMermaidRenderingMode(mode)
                self.chatContainer.invalidate()
                self.ui.requestRender()

            def onShowCacheMissNoticesChange(shown: bool) -> None:
                self.settingsManager.setShowCacheMissNotices(shown)
                self.rebuildChatFromMessages()

            def onShowHardwareCursorChange(enabled: bool) -> None:
                self.settingsManager.setShowHardwareCursor(enabled)
                self.ui.setShowHardwareCursor(enabled)

            def onEditorPaddingXChange(padding: int) -> None:
                self.settingsManager.setEditorPaddingX(padding)
                self.defaultEditor.setPaddingX(padding)
                if self.editor is not self.defaultEditor and getattr(self.editor, "setPaddingX", None) is not None:
                    self.editor.setPaddingX(padding)

            def onOutputPadChange(padding: Literal[0, 1]) -> None:
                self.settingsManager.setOutputPad(padding)
                self.outputPad = padding
                for container in (self.chatContainer, self.pendingMessagesContainer):
                    for child in container.children:
                        setOutputPad = getattr(child, "setOutputPad", None)
                        if callable(setOutputPad):
                            setOutputPad(padding)
                self.ui.requestRender()

            def onAutocompleteMaxVisibleChange(maxVisible: int) -> None:
                self.settingsManager.setAutocompleteMaxVisible(maxVisible)
                self.defaultEditor.setAutocompleteMaxVisible(maxVisible)
                if (
                    self.editor is not self.defaultEditor
                    and getattr(self.editor, "setAutocompleteMaxVisible", None) is not None
                ):
                    self.editor.setAutocompleteMaxVisible(maxVisible)

            def onClearOnShrinkChange(enabled: bool) -> None:
                self.settingsManager.setClearOnShrink(enabled)
                self.ui.setClearOnShrink(enabled)
                if not enabled and not self.activeStatusIndicator:
                    self.statusContainer.clear()

            def onCancel() -> None:
                done()
                self.ui.requestRender()

            selector = SettingsSelectorComponent(
                SettingsConfig(
                    autoCompact=self.session.autoCompactionEnabled,
                    defaultModel=defaultModel,
                    currentModel=self.session.model,
                    availableDefaultModels=self.session.modelRuntime.getAvailableSnapshot(),
                    showImages=self.settingsManager.getShowImages(),
                    imageWidthCells=self.settingsManager.getImageWidthCells(),
                    autoResizeImages=self.settingsManager.getImageAutoResize(),
                    blockImages=self.settingsManager.getBlockImages(),
                    enableSkillCommands=self.settingsManager.getEnableSkillCommands(),
                    steeringMode=self.session.steeringMode,
                    followUpMode=self.session.followUpMode,
                    transport=self.settingsManager.getTransport(),
                    sessionShareDestination=self.settingsManager.getSessionShareDestination(),
                    httpIdleTimeoutMs=self.settingsManager.getHttpIdleTimeoutMs(),
                    cacheWarmingMode=self.settingsManager.getCacheWarmingMode(),
                    thinkingLevel=self.settingsManager.getDefaultThinkingLevel() or DEFAULT_THINKING_LEVEL,
                    availableThinkingLevels=[*THINKING_LEVEL_OPTIONS],
                    modelThinkingLevels=self.settingsManager.getAllModelThinkingLevels(),
                    # MISAKA fork (D11): without a saved selection, the built-in theme matching the terminal.
                    currentTheme=self.themeController.getThemeSelection() or self.themeController.resolveThemeName(),
                    terminalTheme=self.themeController.getTerminalTheme(),
                    availableThemes=getAvailableThemes(),
                    hideThinkingBlock=self.hideThinkingBlock,
                    mermaidRenderingMode=self.settingsManager.getMermaidRenderingMode(),
                    collapseChangelog=self.settingsManager.getCollapseChangelog(),
                    enableInstallTelemetry=self.settingsManager.getEnableInstallTelemetry(),
                    doubleEscapeAction=self.settingsManager.getDoubleEscapeAction(),
                    treeFilterMode=self.settingsManager.getTreeFilterMode(),
                    showHardwareCursor=self.settingsManager.getShowHardwareCursor(),
                    showCacheMissNotices=self.settingsManager.getShowCacheMissNotices(),
                    defaultProjectTrust=self.settingsManager.getDefaultProjectTrust(),
                    editorPaddingX=self.settingsManager.getEditorPaddingX(),
                    outputPad=self.settingsManager.getOutputPad(),
                    autocompleteMaxVisible=self.settingsManager.getAutocompleteMaxVisible(),
                    quietStartup=self.settingsManager.getQuietStartup(),
                    clearOnShrink=self.settingsManager.getClearOnShrink(),
                    showTerminalProgress=self.settingsManager.getShowTerminalProgress(),
                    # MISAKA fork (D1): no `tuiMode` or fullscreen settings.
                    warnings=self.settingsManager.getWarnings(),
                ),
                SettingsCallbacks(
                    onAutoCompactChange=onAutoCompactChange,
                    onShowImagesChange=onShowImagesChange,
                    onImageWidthCellsChange=onImageWidthCellsChange,
                    onAutoResizeImagesChange=lambda enabled: self.settingsManager.setImageAutoResize(enabled),
                    onBlockImagesChange=lambda blocked: self.settingsManager.setBlockImages(blocked),
                    onEnableSkillCommandsChange=onEnableSkillCommandsChange,
                    onSteeringModeChange=lambda mode: self.session.setSteeringMode(mode),
                    onFollowUpModeChange=lambda mode: self.session.setFollowUpMode(mode),
                    onTransportChange=onTransportChange,
                    onSessionShareDestinationChange=onSessionShareDestinationChange,
                    onHttpIdleTimeoutMsChange=onHttpIdleTimeoutMsChange,
                    onCacheWarmingModeChange=onCacheWarmingModeChange,
                    # MISAKA fork: the panel's thinking level row saves the default (this agent's own in a Last Order
                    # or Sister window), like every other row of the panel.
                    onThinkingLevelChange=lambda level: self.selectThinkingLevel(level, True),
                    onModelThinkingLevelChange=onModelThinkingLevelChange,
                    onModelThinkingLevelRemove=onModelThinkingLevelRemove,
                    onThemeChange=onThemeChange,
                    onThemePreview=lambda themeName: self.themeController.preview(themeName),
                    onHideThinkingBlockChange=onHideThinkingBlockChange,
                    onMermaidRenderingModeChange=onMermaidRenderingModeChange,
                    onShowCacheMissNoticesChange=onShowCacheMissNoticesChange,
                    onCollapseChangelogChange=lambda collapsed: self.settingsManager.setCollapseChangelog(collapsed),
                    onEnableInstallTelemetryChange=lambda enabled: self.settingsManager.setEnableInstallTelemetry(
                        enabled
                    ),
                    onQuietStartupChange=lambda quiet: self.settingsManager.setQuietStartup(quiet),
                    onDefaultProjectTrustChange=lambda defaultProjectTrust: self.settingsManager.setDefaultProjectTrust(
                        defaultProjectTrust
                    ),
                    onDoubleEscapeActionChange=lambda action: self.settingsManager.setDoubleEscapeAction(action),
                    onTreeFilterModeChange=lambda mode: self.settingsManager.setTreeFilterMode(mode),
                    onShowHardwareCursorChange=onShowHardwareCursorChange,
                    onEditorPaddingXChange=onEditorPaddingXChange,
                    onOutputPadChange=onOutputPadChange,
                    onAutocompleteMaxVisibleChange=onAutocompleteMaxVisibleChange,
                    onClearOnShrinkChange=onClearOnShrinkChange,
                    onShowTerminalProgressChange=lambda enabled: self.settingsManager.setShowTerminalProgress(enabled),
                    onWarningsChange=lambda warnings: self.settingsManager.setWarnings(warnings),
                    onCancel=onCancel,
                ),
            )
            return {"component": selector, "focus": selector.getSettingsList()}

        self.showSelector(create)

    def handleThinkingCommand(self, searchTerm: str | None = None) -> None:
        # MISAKA fork: a model or thinking level switch waits for a running compaction.
        if self._refuseSwitchDuringCompaction("thinking level"):
            return
        # MISAKA fork: `/thinking --default [level]` also saves the level as the startup default (and `--default`
        # alone opens the selector in that mode).
        parsed = parse_default_flag_args("thinking", searchTerm)
        if parsed.error:
            self.showError(parsed.error)
            return
        searchTerm = parsed.searchTerm
        availableLevels = self.session.getAvailableThinkingLevels()
        if not searchTerm:
            self.showThinkingSelector(parsed.persist)
            return

        normalized = jsTrim(searchTerm).lower()
        level = next((candidate for candidate in availableLevels if candidate.lower() == normalized), None)
        if not level:
            self.showError(f'Unknown thinking level "{searchTerm}". Available levels: {", ".join(availableLevels)}.')
            return

        self.selectThinkingLevel(level, parsed.persist)

    def selectThinkingLevel(self, level: ThinkingLevel, persist: bool) -> None:
        try:
            # MISAKA fork: the default is saved through the settings first, which in a Last Order or Sister window
            # is the agent's own role file; a role file that refuses the write leaves the session level as it was.
            if persist:
                try:
                    self.settingsManager.setDefaultThinkingLevel(level)
                except (OSError, ValueError) as error:
                    self.showError(f"Thinking level not saved: {errorMessage(error)}")
                    return
            self.session.setThinkingLevel(level)
            self.footer.invalidate()
            self.updateEditorBorderColor()
            # MISAKA fork: an agent's own default names itself.
            whose = " for this agent" if self.settingsManager.getModelProfile() else ""
            self.showStatus(f"Default thinking level{whose}: {level}" if persist else f"Thinking level: {level}")
        except Exception as error:  # noqa: BLE001 - shown to the user
            self.showError(errorMessage(error))

    def showThinkingSelector(self, persist: bool = False) -> None:
        def create(done: Callable[[], None]) -> dict[str, Any]:
            def selectLevel(level: ThinkingLevel, persistLevel: bool) -> None:
                self.selectThinkingLevel(level, persistLevel)
                done()

            def onCancel() -> None:
                done()
                self.ui.requestRender()

            selector = ThinkingSelectorComponent(
                self.session.thinkingLevel or DEFAULT_THINKING_LEVEL,
                self.session.getAvailableThinkingLevels(),
                # MISAKA fork: `/thinking --default` opens the selector in save-as-default mode.
                lambda level: selectLevel(level, persist),
                onCancel,
                lambda level: selectLevel(level, True),
                self.settingsManager.getDefaultThinkingLevel() or DEFAULT_THINKING_LEVEL,
            )
            return {"component": selector, "focus": selector}

        self.showSelector(create)

    async def handleModelCommand(self, searchTerm: str | None = None) -> None:
        # MISAKA fork: a model or thinking level switch waits for a running compaction.
        if self._refuseSwitchDuringCompaction("model"):
            return
        if not searchTerm:
            self.showModelSelector()
            return

        model = await self.findExactModelMatch(searchTerm)
        if model:
            try:
                await self.session.setModel(model, {"persist": False})
                self.footer.invalidate()
                self.updateEditorBorderColor()
                self.showStatus(f"Model: {model.id}")
                self._schedule_task(self.maybeWarnAboutAnthropicSubscriptionAuth(model))
            except Exception as error:  # noqa: BLE001 - shown to the user
                self.showError(errorMessage(error))
            return

        self.showModelSelector(searchTerm)

    async def findExactModelMatch(self, searchTerm: str) -> Model | None:
        cachedModels = (
            [read_field(scoped, "model") for scoped in self.session.scopedModels]
            if len(self.session.scopedModels) > 0
            else [*self.session.modelRuntime.getAvailableSnapshot()]
        )
        cachedMatch = findExactModelReferenceMatch(searchTerm, cachedModels)
        if cachedMatch or len(self.session.scopedModels) > 0:
            return cachedMatch

        self.showStatus("Refreshing model catalogs…")
        controller = AbortController()
        timedOut = False

        def onTimeout() -> None:
            nonlocal timedOut
            timedOut = True
            controller.abort()

        timeout = setTimeout(onTimeout, 15_000)
        try:
            result = await refreshModelCatalogs(self.session.modelRuntime, controller.signal)
            if result.aborted and timedOut:
                self.showWarning("Model refresh timed out; searching cached models.")
            elif len(result.errors) > 0:
                self.showWarning(f"Could not refresh {', '.join(result.errors.keys())}; searching cached models.")
        except Exception as error:  # noqa: BLE001 - a failed refresh falls back to the cached models
            self.showWarning(
                "Model refresh timed out; searching cached models."
                if timedOut
                else f"Could not refresh model catalogs: {errorMessage(error)}"
            )
        finally:
            clearTimeout(timeout)
        return findExactModelReferenceMatch(searchTerm, [*self.session.modelRuntime.getAvailableSnapshot()])

    def updateAvailableProviderCount(self) -> None:
        """Update the footer's available provider count from the current snapshot without refreshing catalogs."""
        models = (
            [read_field(scoped, "model") for scoped in self.session.scopedModels]
            if len(self.session.scopedModels) > 0
            else self.session.modelRuntime.getAvailableSnapshot()
        )
        uniqueProviders = {model.provider for model in models}
        self.footerDataProvider.setAvailableProviderCount(len(uniqueProviders))

    async def maybeWarnAboutAnthropicSubscriptionAuth(self, model: Any = UNDEFINED) -> None:
        if model is UNDEFINED:
            model = self.session.model
        if self.settingsManager.getWarnings().get("anthropicExtraUsage") is False:
            return
        if self.anthropicSubscriptionWarningShown:
            return
        if not model or model.provider != "anthropic":
            return

        try:
            if read_field(await self.session.modelRuntime.checkAuth("anthropic"), "type") == "oauth":
                self.anthropicSubscriptionWarningShown = True
                self.showWarning(ANTHROPIC_SUBSCRIPTION_AUTH_WARNING)
                return
            resolution = await self.session.modelRuntime.getAuth(model.provider)
            apiKey = resolution.auth.apiKey if resolution else None
            if not isAnthropicSubscriptionAuthKey(apiKey):
                return
            self.anthropicSubscriptionWarningShown = True
            self.showWarning(ANTHROPIC_SUBSCRIPTION_AUTH_WARNING)
        except Exception:  # noqa: BLE001, S110 - Ignore auth lookup failures for warning-only checks.
            pass

    def maybeSaveImplicitProjectTrustAfterReload(self) -> bool:
        cwd = self.sessionManager.getCwd()
        if self.autoTrustOnReloadCwd != cwd:
            return False
        if not self.settingsManager.isProjectTrusted() or not hasTrustRequiringProjectResources(cwd):
            return False

        trustStore = ProjectTrustStore(self.runtimeHost.services.agentDir)
        try:
            if trustStore.get(cwd) is not None:
                self.autoTrustOnReloadCwd = None
                return False
            trustStore.set(cwd, True)
            self.autoTrustOnReloadCwd = None
            return True
        except Exception as error:  # noqa: BLE001 - warned; retried on the next reload
            self.showWarning(f"Could not save project trust after reload: {errorMessage(error)}")
            return False

    def showTrustSelector(self) -> None:
        cwd = self.sessionManager.getCwd()
        trustStore = ProjectTrustStore(self.runtimeHost.services.agentDir)
        savedDecision = trustStore.getEntry(cwd)

        def create(done: Callable[[], None]) -> dict[str, Any]:
            def onSelect(selection: Any) -> None:
                trustStore.setMany(selection.updates)
                done()
                self.showStatus(
                    f"Saved trust decision: {'trusted' if selection.trusted else 'untrusted'}. "
                    f"Restart {APP_NAME} for this to take effect."
                )

            def onCancel() -> None:
                done()
                self.ui.requestRender()

            selector = TrustSelectorComponent(
                TrustSelectorOptions(
                    cwd=cwd,
                    savedDecision=savedDecision,
                    projectTrusted=self.settingsManager.isProjectTrusted(),
                    onSelect=onSelect,
                    onCancel=onCancel,
                )
            )
            return {"component": selector, "focus": selector}

        self.showSelector(create)

    def showModelSelector(self, initialSearchInput: str | None = None) -> None:
        def create(done: Callable[[], None]) -> dict[str, Any]:
            async def selectModel(model: Model, persist: bool) -> None:
                try:
                    await self.session.setModel(model, {"persist": persist})
                    self.updateAvailableProviderCount()
                    self.footer.invalidate()
                    self.updateEditorBorderColor()
                    done()
                    self.showStatus(f"Default model: {model.provider}/{model.id}" if persist else f"Model: {model.id}")
                    self._schedule_task(self.maybeWarnAboutAnthropicSubscriptionAuth(model))
                except Exception as error:  # noqa: BLE001 - shown to the user
                    done()
                    self.showError(errorMessage(error))

            def onCancel() -> None:
                done()
                self.ui.requestRender()

            defaultProvider = self.settingsManager.getDefaultProvider()
            defaultModel = self.settingsManager.getDefaultModel()
            selector = ModelSelectorComponent(
                self.ui,
                self.session.model,
                self.session.modelRuntime,
                # MISAKA fork: the session keeps its scoped models as dicts.
                [
                    ScopedModelItem(model=read_field(scoped, "model"), thinkingLevel=read_field(scoped, "thinkingLevel"))
                    for scoped in self.session.scopedModels
                ],
                lambda model: self._schedule_task(selectModel(model, False)),
                onCancel,
                initialSearchInput,
                lambda model: self._schedule_task(selectModel(model, True)),
                DefaultModelReference(provider=defaultProvider, id=defaultModel)
                if defaultProvider and defaultModel
                else None,
            )
            return {"component": selector, "focus": selector, "dispose": lambda: selector.dispose()}

        self.showSelector(create)

    def showModelsSelector(self) -> None:
        availableModels = [*self.session.modelRuntime.getAvailableSnapshot()]
        availableModelIds = {f"{model.provider}/{model.id}" for model in availableModels}
        configuredPatterns = self.settingsManager.getEnabledModels()
        sessionScopedModels = self.session.scopedModels

        def configuredEnabledIds(models: list[Model]) -> list[str] | None:
            if not configuredPatterns:
                return None
            resolved = resolveModelScopeFromModels(configuredPatterns, models)
            ids = [f"{scoped.model.provider}/{scoped.model.id}" for scoped in resolved.scopedModels]
            for diagnostic in resolved.diagnostics:
                if diagnostic.code == "no-match" and diagnostic.pattern not in ids:
                    ids.append(diagnostic.pattern)
            return ids

        currentEnabledIds: list[str] | None = (
            [
                f"{read_field(scoped, 'model').provider}/{read_field(scoped, 'model').id}"
                for scoped in sessionScopedModels
            ]
            if len(sessionScopedModels) > 0
            else configuredEnabledIds(availableModels)
        )
        selectionChanged = False

        def updateSessionModels(enabledIds: list[str] | None) -> None:
            nonlocal currentEnabledIds
            currentEnabledIds = None if enabledIds is None else [*enabledIds]
            hasEnabledAvailableModel = any(id in availableModelIds for id in enabledIds) if enabledIds else False
            allAvailableModelsEnabled = enabledIds is not None and all(id in enabledIds for id in availableModelIds)
            if enabledIds and hasEnabledAvailableModel and not allAvailableModelsEnabled:
                newScopedModels = resolveModelScopeFromModels(enabledIds, availableModels).scopedModels
                self.session.setScopedModels(
                    [{"model": scoped.model, "thinkingLevel": scoped.thinkingLevel} for scoped in newScopedModels]
                )
            else:
                self.session.setScopedModels([])
            self.updateAvailableProviderCount()
            self.ui.requestRender()

        def create(done: Callable[[], None]) -> dict[str, Any]:
            disposed = False
            timedOut = False
            controller = AbortController()

            def onTimeout() -> None:
                nonlocal timedOut
                timedOut = True
                controller.abort()

            timeout = setTimeout(onTimeout, 15_000)

            def onChange(enabledIds: list[str] | None) -> None:
                nonlocal selectionChanged
                selectionChanged = True
                updateSessionModels(enabledIds)

            def onPersist(enabledIds: list[str] | None) -> None:
                allEnabled = (
                    enabledIds is not None
                    and len(enabledIds) == len(availableModels)
                    and all(id in availableModelIds for id in enabledIds)
                )
                newPatterns = None if enabledIds is None or allEnabled else enabledIds
                self.settingsManager.setEnabledModels([*newPatterns] if newPatterns is not None else None)
                self.showStatus("Model selection saved to settings")

            def onCancel() -> None:
                done()
                self.ui.requestRender()

            selector = ScopedModelsSelectorComponent(
                ModelsConfig(
                    allModels=availableModels,
                    enabledModelIds=currentEnabledIds,
                    refreshStatus="Refreshing model catalogs…",
                ),
                ModelsCallbacks(onChange=onChange, onPersist=onPersist, onCancel=onCancel),
            )

            async def refresh() -> None:
                nonlocal availableModels, availableModelIds, currentEnabledIds
                try:
                    result = await refreshModelCatalogs(self.session.modelRuntime, controller.signal)
                    if disposed:
                        return
                    availableModels = [*self.session.modelRuntime.getAvailableSnapshot()]
                    availableModelIds = {f"{model.provider}/{model.id}" for model in availableModels}
                    if not selectionChanged and len(sessionScopedModels) == 0:
                        currentEnabledIds = configuredEnabledIds(availableModels)
                        selector.updateModels(availableModels, currentEnabledIds)
                    else:
                        selector.updateModels(availableModels)
                    if currentEnabledIds is not None:
                        updateSessionModels(currentEnabledIds)
                    if result.aborted and timedOut:
                        selector.setRefreshStatus("Model refresh timed out; showing cached models.", "warning")
                    elif len(result.errors) > 0:
                        selector.setRefreshStatus(
                            f"Could not refresh {', '.join(result.errors.keys())}; showing cached models.",
                            "warning",
                        )
                    else:
                        selector.setRefreshStatus("Model catalogs refreshed.", "success")
                    self.ui.requestRender()
                except Exception as error:  # noqa: BLE001 - reported on the selector
                    if disposed:
                        return
                    selector.setRefreshStatus(
                        "Model refresh timed out; showing cached models."
                        if timedOut
                        else f"Could not refresh model catalogs: {errorMessage(error)}",
                        "warning",
                    )
                    self.ui.requestRender()
                finally:
                    clearTimeout(timeout)

            self._schedule_task(refresh())

            def dispose() -> None:
                nonlocal disposed
                disposed = True
                clearTimeout(timeout)
                controller.abort()

            return {"component": selector, "focus": selector, "dispose": dispose}

        self.showSelector(create)

    def showUserMessageSelector(self) -> None:
        userMessages = self.session.getUserMessagesForForking()

        if len(userMessages) == 0:
            self.showStatus("No messages to fork from")
            return

        initialSelectedId = read_field(jsIndex(userMessages, len(userMessages) - 1), "entryId")

        def create(done: Callable[[], None]) -> dict[str, Any]:
            async def onSelect(entryId: str) -> None:
                done()
                try:
                    result = await self.runtimeHost.fork(entryId)
                    if read_field(result, "cancelled"):
                        self.ui.requestRender()
                        return

                    self.editor.setText(read_field(result, "selectedText") or "")
                    self.showStatus("Forked to new session")
                except Exception as error:  # noqa: BLE001 - shown to the user
                    self.showError(errorMessage(error))

            def onCancel() -> None:
                done()
                self.ui.requestRender()

            selector = UserMessageSelectorComponent(
                [UserMessageItem(id=read_field(m, "entryId"), text=read_field(m, "text")) for m in userMessages],
                lambda entryId: self._schedule_task(onSelect(entryId)),
                onCancel,
                initialSelectedId,
            )
            return {"component": selector, "focus": selector.getMessageList()}

        self.showSelector(create)

    async def handleCloneCommand(self) -> None:
        leafId = self.sessionManager.getLeafId()
        if not leafId:
            self.showStatus("Nothing to clone yet")
            return

        try:
            result = await self.runtimeHost.fork(leafId, {"position": "at"})
            if read_field(result, "cancelled"):
                self.ui.requestRender()
                return

            self.editor.setText("")
            self.showStatus("Cloned to new session")
        except Exception as error:  # noqa: BLE001 - shown to the user
            self.showError(errorMessage(error))

    def showTreeSelector(self, initialSelectedId: str | None = None) -> None:
        tree = self.sessionManager.getTree()
        realLeafId = self.sessionManager.getLeafId()
        initialFilterMode = self.settingsManager.getTreeFilterMode()

        if len(tree) == 0:
            self.showStatus("No entries in session")
            return

        def create(done: Callable[[], None]) -> dict[str, Any]:
            async def onSelect(entryId: str) -> None:
                # Selecting the current leaf is a no-op (already there)
                if entryId == self.sessionManager.getLeafId():
                    done()
                    self.showStatus("Already at this point")
                    return

                # Ask about summarization
                done()  # Close selector first

                # Loop until user makes a complete choice or cancels to tree
                wantsSummary = False
                customInstructions: str | None = None

                # Check if we should skip the prompt (user preference to always default to no summary)
                if not self.settingsManager.getBranchSummarySkipPrompt():
                    while True:
                        summaryChoice = await self.showExtensionSelector(
                            "Summarize branch?",
                            ["No summary", "Summarize", "Summarize with custom prompt"],
                        )

                        if summaryChoice is None:
                            # User pressed escape - re-show tree selector with same selection
                            self.showTreeSelector(entryId)
                            return

                        wantsSummary = summaryChoice != "No summary"

                        if summaryChoice == "Summarize with custom prompt":
                            customInstructions = await self.showExtensionEditor("Custom summarization instructions")
                            if customInstructions is None:
                                # User cancelled - loop back to summary selector
                                continue

                        # User made a complete choice
                        break

                # The user committed to navigating: stop the active response first.
                if self.session.isStreaming:
                    self.restoreQueuedMessagesToEditor()
                    await self.session.abort()

                # Recheck after the dialogs and streaming abort, before replacing another operation's UI.
                if self.session.isCompacting:
                    self.showError(
                        "Wait for the current compaction or tree navigation to finish before navigating the session tree."
                    )
                    return

                # Set up escape handler and status indicator if summarizing
                showingSummaryIndicator = False
                originalOnEscape = self.defaultEditor.onEscape

                if wantsSummary:
                    self.defaultEditor.onEscape = lambda: self.session.abortBranchSummary()
                    self.chatContainer.addChild(Spacer(1))
                    self.showStatusIndicator(BranchSummaryStatusIndicator(self.ui))
                    showingSummaryIndicator = True
                    self.ui.requestRender()

                try:
                    result = await self.session.navigateTree(
                        entryId, {"summarize": wantsSummary, "customInstructions": customInstructions}
                    )

                    if read_field(result, "aborted"):
                        # Summarization aborted - re-show tree selector with same selection
                        self.showStatus("Branch summarization cancelled")
                        self.showTreeSelector(entryId)
                        return
                    if read_field(result, "cancelled"):
                        self.showStatus("Navigation cancelled")
                        return

                    # Update UI
                    self.chatContainer.clear()
                    self.renderInitialMessages()
                    editorText = read_field(result, "editorText")
                    if editorText and not jsTrim(self.editor.getText()):
                        self.editor.setText(editorText)
                    self.showStatus("Navigated to selected point")
                    self._schedule_task(self.flushCompactionQueue({"willRetry": False}))
                except Exception as error:  # noqa: BLE001 - shown to the user
                    self.showError(errorMessage(error))
                finally:
                    if showingSummaryIndicator:
                        self.clearStatusIndicator("branchSummary")
                    self.defaultEditor.onEscape = originalOnEscape

            def onCancel() -> None:
                done()
                self.ui.requestRender()

            def onLabelChange(entryId: str, label: str | None) -> None:
                self.sessionManager.appendLabelChange(entryId, label)
                self.ui.requestRender()

            selector = TreeSelectorComponent(
                tree,
                realLeafId,
                self.ui.terminal.rows,
                lambda entryId: self._schedule_task(onSelect(entryId)),
                onCancel,
                onLabelChange,
                initialSelectedId,
                initialFilterMode,
            )

            async def onCopy(text: str | None) -> None:
                if not text:
                    self.showError("Selected entry has no text to copy")
                    return
                try:
                    await copyToClipboard(text)
                    self.showStatus("Copied selected message to clipboard")
                except Exception as error:  # noqa: BLE001 - shown to the user
                    self.showError(errorMessage(error))

            selector.onCopy = lambda text: self._schedule_task(onCopy(text))
            return {"component": selector, "focus": selector}

        self.showSelector(create)

    def showSessionSelector(self) -> None:
        def create(done: Callable[[], None]) -> dict[str, Any]:
            async def onSelect(sessionPath: str) -> None:
                done()
                await self.handleResumeSession(sessionPath)

            def onCancel() -> None:
                done()
                self.ui.requestRender()

            async def renameSession(sessionFilePath: str, nextName: str | None) -> None:
                next = jsTrim(nextName if nextName is not None else "")
                if not next:
                    return
                mgr = SessionManager.open(sessionFilePath)
                mgr.appendSessionInfo(next)

            def currentSessionsLoader(onProgress: Any = None, signal: Any = None) -> Any:
                return SessionManager.list(
                    self.sessionManager.getCwd(),
                    self.sessionManager.getSessionDir(),
                    onProgress,
                    signal,
                )

            # MISAKA fork: every window keeps its sessions in a store of its own (Last Order's, a Sister's, a DM's),
            # so the all-sessions tab lists that store's root, as `misaka chat -r` does; the engine's default root
            # would show another window's sessions or none.
            def allSessionsLoader(onProgress: Any = None, signal: Any = None) -> Any:
                return SessionManager.listAll(sessions_root_of(self.sessionManager.getSessionDir()), onProgress, signal)

            selector = SessionSelectorComponent(
                currentSessionsLoader,
                allSessionsLoader,
                lambda sessionPath: self._schedule_task(onSelect(sessionPath)),
                onCancel,
                lambda: self._schedule_task(self.shutdown()),
                lambda: self.ui.requestRender(),
                {
                    "renameSession": renameSession,
                    "showRenameHint": True,
                    "keybindings": self.keybindings,
                },
                self.sessionManager.getSessionFile(),
            )
            return {"component": selector, "focus": selector}

        self.showSelector(create)

    async def handleResumeSession(self, sessionPath: str, options: Any = None) -> dict[str, bool]:
        self.clearStatusIndicator()
        try:
            result = await self.runtimeHost.switchSession(
                sessionPath,
                {
                    "withSession": read_field(options, "withSession"),
                    "projectTrustContextFactory": lambda cwd: self.createProjectTrustContext({"cwd": cwd}),
                },
            )
            if read_field(result, "cancelled"):
                return result
            self.showStatus("Resumed session")
            return result
        except MissingSessionCwdError as error:
            selectedCwd = await self.promptForMissingSessionCwd(error)
            if not selectedCwd:
                self.showStatus("Resume cancelled")
                return {"cancelled": True}
            result = await self.runtimeHost.switchSession(
                sessionPath,
                {
                    "cwdOverride": selectedCwd,
                    "withSession": read_field(options, "withSession"),
                    "projectTrustContextFactory": lambda cwd: self.createProjectTrustContext({"cwd": cwd}),
                },
            )
            if read_field(result, "cancelled"):
                return result
            self.showStatus("Resumed session in current cwd")
            return result
        except Exception as error:  # noqa: BLE001 - fatal, as upstream
            return await self.handleFatalRuntimeError("Failed to resume session", error)

    def getLoginProviderOptions(self, authType: Literal["oauth", "api_key"] | None = None) -> list[AuthSelectorProvider]:
        options: list[AuthSelectorProvider] = []
        for provider in self.session.modelRuntime.getProviders():
            authStatus = self.session.modelRuntime.getProviderAuthStatus(provider.id)
            status = (
                AuthCheck(
                    type="oauth" if self.session.modelRuntime.isUsingOAuth(provider.id) else "api_key",
                    source=authStatus.label if authStatus.label is not None else authStatus.source,
                )
                if authStatus.configured
                else None
            )
            subscription = getattr(provider.auth.oauth, "isSubscription", None) is True
            if (not authType or authType == "oauth") and provider.auth.oauth:
                options.append(
                    AuthSelectorProvider(
                        id=provider.id,
                        name=provider.name,
                        authType="oauth",
                        method=provider.auth.oauth,
                        status=status,
                        subscription=subscription,
                    )
                )
            if (not authType or authType == "api_key") and provider.auth.apiKey:
                options.append(
                    AuthSelectorProvider(
                        id=provider.id,
                        name=provider.name,
                        authType="api_key",
                        method=provider.auth.apiKey,
                        status=status,
                        subscription=subscription,
                    )
                )
        return sorted(options, key=functools.cmp_to_key(_byProviderName))

    async def getLogoutProviderOptions(self) -> list[AuthSelectorProvider]:
        options: list[AuthSelectorProvider] = []
        for credential in await self.session.modelRuntime.listCredentials(
            AuthOperationOptions(signal=TimeoutAbortSignal(15_000))
        ):
            provider = self.session.modelRuntime.getProvider(credential.providerId)
            options.append(
                AuthSelectorProvider(
                    id=credential.providerId,
                    name=provider.name if provider else credential.providerId,
                    authType=credential.type,
                    status=AuthCheck(type=credential.type, source="stored credential"),
                    subscription=getattr(provider.auth.oauth, "isSubscription", None) is True if provider else False,
                )
            )
        return sorted(options, key=functools.cmp_to_key(_byProviderName))

    def findLoginProviderOptions(self, providerRef: str) -> list[AuthSelectorProvider]:
        normalizedProviderRef = jsTrim(providerRef).lower()
        if not normalizedProviderRef:
            return []

        return [
            provider
            for provider in self.getLoginProviderOptions()
            if provider.id.lower() == normalizedProviderRef or provider.name.lower() == normalizedProviderRef
        ]

    # MISAKA fork: `/login PROVIDER --account NAME` stores a named credential record, so the login methods below
    # carry the account through to `loginProvider`.

    async def handleLoginCommand(self, providerRef: str | None = None) -> None:
        if not providerRef:
            self.showLoginAuthTypeSelector()
            return

        account: str | None = None
        if "--account" in providerRef:
            try:
                args = shlex.split(providerRef)
                if len(args) < 3 or args[-2] != "--account" or "--account" in args[:-2]:
                    raise ValueError("Usage: /login PROVIDER --account NAME")
                providerRef, account = " ".join(args[:-2]), args[-1]
                oauth_account_key(providerRef, account)
            except ValueError as error:
                self.showError(errorMessage(error))
                return

        providerOptions = self.findLoginProviderOptions(providerRef)
        if len(providerOptions) == 1:
            await self.startProviderLogin(providerOptions[0], account=account)
            return

        if len(providerOptions) > 1:
            providerIds = {provider.id for provider in providerOptions}
            if len(providerIds) == 1:
                self.showLoginAuthTypeSelector(providerOptions, account=account)
                return

        self.showLoginProviderSelector(None, providerRef, account=account)

    async def startProviderLogin(
        self,
        providerOption: AuthSelectorProvider,
        onBack: Callable[[], None] | None = None,
        *,
        account: str | None = None,
    ) -> None:
        """`onBack` reopens the selector the login was started from when the user cancels it."""
        if providerOption.authType == "oauth":
            await self.showLoginDialog(providerOption.id, providerOption.name, onBack, account=account)
        elif getattr(providerOption.method, "login", None):
            await self.showApiKeyLoginDialog(providerOption.id, providerOption.name, onBack, account=account)
        else:
            self.showAmbientAuthDialog(providerOption, onBack)

    def showLoginAuthTypeSelector(
        self, providerOptions: list[AuthSelectorProvider] | None = None, *, account: str | None = None
    ) -> None:
        # The top-level selector offers Radius directly, as its last option.
        radiusOption = (
            None
            if providerOptions
            else next(
                (provider for provider in self.getLoginProviderOptions("oauth") if provider.id == RADIUS_PROVIDER_ID),
                None,
            )
        )
        radiusText = f"Sign in with {radiusOption.name}" if radiusOption else None
        radiusLabel = f"{radiusText}{formatAuthSelectorProviderStatus(radiusOption)}" if radiusOption else None
        oauthProvider = next((provider for provider in providerOptions or [] if provider.authType == "oauth"), None)
        oauthLoginLabel = getattr(oauthProvider.method, "loginLabel", None) if oauthProvider else None
        subscriptionLabel = oauthLoginLabel if oauthLoginLabel is not None else "Sign in with an account"
        apiKeyLabel = "Sign in with an API key"
        availableAuthTypes = (
            {provider.authType for provider in providerOptions} if providerOptions else {"oauth", "api_key"}
        )
        options: list[str] = []
        if "oauth" in availableAuthTypes:
            options.append(subscriptionLabel)
        if "api_key" in availableAuthTypes:
            options.append(apiKeyLabel)
        if radiusLabel:
            options.append(radiusLabel)

        if len(options) == 0:
            self.showStatus("No login methods available.")
            return

        if providerOptions and len(options) == 1:
            providerOption = jsIndex(providerOptions, 0)
            if providerOption:
                self._schedule_task(self.startProviderLogin(providerOption, account=account))
            return

        title = (
            f"Select authentication method for {providerOptions[0].name}:"
            if providerOptions
            else "Select authentication method:"
        )

        def create(done: Callable[[], None]) -> dict[str, Any]:
            def onSelect(option: str) -> None:
                done()
                if radiusOption and option == radiusLabel:
                    self._schedule_task(
                        self.startProviderLogin(
                            radiusOption, lambda: self.showLoginAuthTypeSelector(account=account), account=account
                        )
                    )
                    return
                authType: Literal["oauth", "api_key"] = "oauth" if option == subscriptionLabel else "api_key"
                if providerOptions:
                    providerOption = next(
                        (provider for provider in providerOptions if provider.authType == authType), None
                    )
                    if providerOption:
                        self._schedule_task(
                            self.startProviderLogin(
                                providerOption,
                                lambda: self.showLoginAuthTypeSelector(providerOptions, account=account),
                                account=account,
                            )
                        )
                    return
                self.showLoginProviderSelector(authType, account=account)

            def onCancel() -> None:
                done()
                self.ui.requestRender()

            selector = (
                createLoginMenuSelector(
                    self.ui,
                    title,
                    options,
                    RadiusOption(label=radiusLabel, text=radiusText),
                    onSelect,
                    onCancel,
                )
                if radiusLabel and radiusText
                else ExtensionSelectorComponent(title, options, onSelect, onCancel)
            )
            return {"component": selector, "focus": selector, "dispose": lambda: selector.dispose()}

        self.showSelector(create)

    def showLoginProviderSelector(
        self,
        authType: Literal["oauth", "api_key"] | None = None,
        initialSearchInput: str | None = None,
        *,
        account: str | None = None,
    ) -> None:
        providerOptions = self.getLoginProviderOptions(authType)
        if len(providerOptions) == 0:
            message = (
                "No account providers available."
                if authType == "oauth"
                else "No API key providers available."
                if authType == "api_key"
                else "No login providers available."
            )
            self.showStatus(message)
            return

        def create(done: Callable[[], None]) -> dict[str, Any]:
            async def onSelect(providerId: str, selectedAuthType: str) -> None:
                done()

                providerOption = next(
                    (
                        provider
                        for provider in providerOptions
                        if provider.id == providerId and provider.authType == selectedAuthType
                    ),
                    None,
                )
                if not providerOption:
                    return

                await self.startProviderLogin(
                    providerOption,
                    lambda: self.showLoginProviderSelector(authType, initialSearchInput, account=account),
                    account=account,
                )

            def onCancel() -> None:
                done()
                if authType:
                    self.showLoginAuthTypeSelector(account=account)
                else:
                    self.ui.requestRender()

            selector = OAuthSelectorComponent(
                "login",
                providerOptions,
                lambda providerId, selectedAuthType: self._schedule_task(onSelect(providerId, selectedAuthType)),
                onCancel,
                initialSearchInput,
            )
            return {"component": selector, "focus": selector}

        self.showSelector(create)

    async def showOAuthSelector(self, mode: Literal["login", "logout"]) -> None:
        if mode == "login":
            self.showLoginAuthTypeSelector()
            return

        try:
            providerOptions = await self.getLogoutProviderOptions()
        except Exception as error:  # noqa: BLE001 - shown to the user
            self.showError(f"Could not read stored credentials: {errorMessage(error)}")
            return
        if len(providerOptions) == 0:
            self.showStatus(
                "No stored credentials to remove. /logout only removes credentials saved by /login; environment "
                "variables and models.json config are unchanged."
            )
            return

        def create(done: Callable[[], None]) -> dict[str, Any]:
            async def onSelect(providerId: str) -> None:
                done()

                providerOption = next((provider for provider in providerOptions if provider.id == providerId), None)
                if not providerOption:
                    return

                try:
                    await self.session.modelRuntime.logout(
                        providerOption.id, AuthOperationOptions(signal=TimeoutAbortSignal(15_000))
                    )
                    self.updateAvailableProviderCount()
                    message = (
                        f"Logged out of {providerOption.name}"
                        if providerOption.authType == "oauth"
                        else f"Removed stored API key for {providerOption.name}. Environment variables and "
                        "models.json config are unchanged."
                    )
                    self.showStatus(message)
                except Exception as error:  # noqa: BLE001 - shown to the user
                    message = errorMessage(error)
                    self.showError(
                        f"Credentials removed for {providerOption.name}, but local model state could not be "
                        f"synchronized: {message}"
                        if isinstance(error, CredentialSynchronizationError)
                        else f"Logout failed: {message}"
                    )

            def onCancel() -> None:
                done()
                self.ui.requestRender()

            def onLogoutSelect(providerId: str, _authType: Any = None) -> None:
                self._schedule_task(onSelect(providerId))

            selector = OAuthSelectorComponent(mode, providerOptions, onLogoutSelect, onCancel)
            return {"component": selector, "focus": selector}

        self.showSelector(create)

    async def completeProviderAuthentication(
        self,
        providerId: str,
        providerName: str,
        authType: Literal["oauth", "api_key"],
        previousModel: Model | None,
        *,
        account: str | None = None,
    ) -> None:
        actionLabel = f"Logged in to {providerName}" if authType == "oauth" else f"Saved API key for {providerName}"

        # MISAKA fork: a named account record leaves the main-model credentials and the session model alone.
        if account is not None:
            self.showStatus(
                f"{actionLabel} (account: {account}). Saved to {get_auth_path()}; main-model credentials unchanged."
            )
            return

        session = self.session
        # MISAKA fork: pi adopts the new provider's model only on the `unknown/unknown` sentinel, its one modelless
        # state. A MISAKA session without a saved default starts on a catalogue pair nothing can call, so a session
        # whose model is not available counts as modelless too.
        needsModel = isUnknownModel(previousModel) or not any(
            model.provider == read_field(previousModel, "provider") and model.id == read_field(previousModel, "id")
            for model in session.modelRuntime.getAvailableSnapshot()
        )
        # Dynamic catalogs may be empty until the first authenticated network refresh.
        deferSelection = (
            needsModel
            and hasDefaultModelProvider(providerId)
            and not any(
                model.provider == providerId and model.id == defaultModelPerProvider[providerId]
                for model in session.modelRuntime.getAvailableSnapshot()
            )
        )

        async def finishAuthentication() -> None:
            selectedModel: Model | None = None
            selectionError: str | None = None
            if needsModel:
                availableModels = self.session.modelRuntime.getAvailableSnapshot()
                providerModels = [model for model in availableModels if model.provider == providerId]
                # Matches LLAMA_PROVIDER_ID from extensions/llama/provider.py; kept inline to avoid coupling interactive
                # mode to the built-in extension.
                if providerId == "llama.cpp":
                    selectionError = llamaCppPostLoginGuidance(actionLabel, len(providerModels))
                # MISAKA fork: a provider defined in models.json has no built-in default, and its first available
                # model serves the person who just configured their own gateway better than an error.
                elif len(providerModels) == 0:
                    selectionError = (
                        f"{actionLabel}, but no models are available for that provider. Use /model to select a model."
                    )
                else:
                    defaultModelId = defaultModelPerProvider.get(providerId)
                    # Radius catalogs vary by account; prefer balanced, then use catalog order.
                    selectedModel = next((model for model in providerModels if model.id == defaultModelId), None) or (
                        providerModels[0] if defaultModelId is None or providerId == "radius" else None
                    )
                    if not selectedModel:
                        selectionError = (
                            f'{actionLabel}, but its default model "{defaultModelId}" is not available. Use /model '
                            "to select a model."
                        )
                    else:
                        try:
                            await self.session.setModel(selectedModel, {"persist": True})
                        except Exception as error:  # noqa: BLE001 - reported below
                            selectedModel = None
                            selectionError = (
                                f"{actionLabel}, but selecting its default model failed: {errorMessage(error)}. Use "
                                "/model to select a model."
                            )

            self.updateAvailableProviderCount()
            self.footer.invalidate()
            self.updateEditorBorderColor()
            if selectedModel:
                self.showStatus(f"{actionLabel}. Selected {selectedModel.id}. Credentials saved to {get_auth_path()}")
                self._schedule_task(self.maybeWarnAboutAnthropicSubscriptionAuth(selectedModel))
            else:
                self.showStatus(f"{actionLabel}. Credentials saved to {get_auth_path()}")
                if selectionError:
                    self.showError(selectionError)
                else:
                    self._schedule_task(self.maybeWarnAboutAnthropicSubscriptionAuth())

        if deferSelection:
            self.showStatus(f"{actionLabel}. Credentials saved to {get_auth_path()}. Refreshing model catalog…")
        else:
            await finishAuthentication()

        controller = AbortController()
        timeout = setTimeout(lambda: controller.abort(), 15_000)

        async def refresh() -> None:
            try:
                result = await session.modelRuntime.refresh(
                    ModelsRefreshOptions(providers=[providerId], signal=controller.signal)
                )
                if result.aborted:
                    self.showWarning(f"{actionLabel}, but its model catalog refresh timed out; using cached models.")
                elif len(result.errors) > 0:
                    self.showWarning(f"{actionLabel}, but its model catalog could not be refreshed; using cached models.")
                # Do not replace a model or session selected while the refresh was running.
                if deferSelection and self.session is session and session.model is previousModel:
                    await finishAuthentication()
                self.updateAvailableProviderCount()
                self.footer.invalidate()
                self.ui.requestRender()
            except Exception as error:  # noqa: BLE001 - warned
                self.showWarning(
                    f"{actionLabel}, but its model catalog could not be refreshed: {errorMessage(error)}"
                )
            finally:
                clearTimeout(timeout)

        self._schedule_task(refresh())

    def showAmbientAuthDialog(self, providerOption: AuthSelectorProvider, onBack: Callable[[], None] | None = None) -> None:
        def restoreEditor() -> None:
            self.editorContainer.clear()
            self.editorContainer.addChild(self.editor)
            self.ui.setFocus(self.editor)
            self.ui.requestRender()

        def onComplete(_success: bool, _message: str | None) -> None:
            restoreEditor()
            if onBack:
                onBack()

        dialog = LoginDialogComponent(
            self.ui,
            providerOption.id,
            onComplete,
            providerOption.name,
            f"{providerOption.name} setup",
        )
        methodName = getattr(providerOption.method, "name", None)
        dialog.showInfo(f"{methodName if methodName is not None else 'Authentication'} is configured outside {APP_NAME}.", [], True)

        self.editorContainer.clear()
        self.editorContainer.addChild(dialog)
        self.ui.setFocus(dialog)
        self.ui.requestRender()

    async def showApiKeyLoginDialog(
        self,
        providerId: str,
        providerName: str,
        onBack: Callable[[], None] | None = None,
        *,
        account: str | None = None,
    ) -> None:
        # MISAKA fork: Bedrock signs in through AWS profiles, which have no named records.
        if providerId == "amazon-bedrock" and account is not None:
            self.showError("Bedrock uses AWS profiles rather than named API-key records")
            return
        previousModel = self.session.model

        def onComplete(_success: bool, _message: str | None) -> None:
            # Completion handled below
            pass

        dialog = LoginDialogComponent(self.ui, providerId, onComplete, providerName)

        if providerId == "amazon-bedrock":
            # MISAKA fork: no "See: providers.md" lines (MISAKA ships no providers.md).
            dialog.showDetails(
                [theme.fg("text", "You can also use an AWS profile, IAM keys, or role-based credentials.")]
            )

        self.editorContainer.clear()
        self.editorContainer.addChild(dialog)
        self.ui.setFocus(dialog)
        self.ui.requestRender()

        def restoreEditor() -> None:
            self.editorContainer.clear()
            self.editorContainer.addChild(self.editor)
            self.ui.setFocus(self.editor)
            self.ui.requestRender()

        try:
            await self.loginProvider(dialog, providerId, providerName, "api_key", account=account)
            restoreEditor()
            await self.completeProviderAuthentication(providerId, providerName, "api_key", previousModel, account=account)
        except Exception as error:  # noqa: BLE001 - reported below
            restoreEditor()
            errorMsg = errorMessage(error)
            if isinstance(error, CredentialSynchronizationError):
                self.showError(f"Saved API key for {providerName}, but local model state could not be synchronized: {errorMsg}")
            elif errorMsg == "Login cancelled":
                if onBack:
                    onBack()
            else:
                self.showError(f"Failed to save API key for {providerName}: {errorMsg}")

    def showAuthSelect(self, dialog: LoginDialogComponent, prompt: Any, providerId: str) -> asyncio.Future[str]:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def restoreDialog() -> None:
            self.editorContainer.clear()
            self.editorContainer.addChild(dialog)
            self.ui.setFocus(dialog)
            self.ui.requestRender()

        promptOptions = read_field(prompt, "options")
        labels = [read_field(option, "label") for option in promptOptions]

        def onSelect(optionLabel: str) -> None:
            restoreDialog()
            id = next(
                (read_field(option, "id") for option in promptOptions if read_field(option, "label") == optionLabel), None
            )
            if future.done():
                return
            if id:
                future.set_result(id)
            else:
                future.set_exception(RuntimeError("Login cancelled"))

        def onCancel() -> None:
            restoreDialog()
            if not future.done():
                future.set_exception(RuntimeError("Login cancelled"))

        selector = ExtensionSelectorComponent(
            read_field(prompt, "message"),
            labels,
            onSelect,
            onCancel,
            {"description": RADIUS_LOGIN_INTRO if providerId == RADIUS_PROVIDER_ID else None},
        )
        self.editorContainer.clear()
        self.editorContainer.addChild(selector)
        self.ui.setFocus(selector)
        self.ui.requestRender()
        return future

    async def showAuthPrompt(self, dialog: LoginDialogComponent, prompt: Any, providerId: str) -> str:
        promptType = read_field(prompt, "type")
        if promptType == "select":
            response: Awaitable[str] = self.showAuthSelect(dialog, prompt, providerId)
        elif promptType == "manual_code":
            response = dialog.showManualInput(read_field(prompt, "message"))
        else:
            response = dialog.showPrompt(read_field(prompt, "message"), read_field(prompt, "placeholder"))
        signal = read_field(prompt, "signal")
        if not signal:
            return await response
        if signal_aborted(signal):
            raise RuntimeError("Login cancelled")
        # MISAKA fork: `raceWithAbortSignal` stands in for the race against an abort listener.
        try:
            return await raceWithAbortSignal(response, signal)
        except RuntimeError as error:
            if signal_aborted(signal):
                raise RuntimeError("Login cancelled") from error
            raise

    def notifyAuthDialog(self, dialog: LoginDialogComponent, event: Any) -> None:
        eventType = read_field(event, "type")
        if eventType == "auth_url":
            dialog.showAuth(read_field(event, "url"), read_field(event, "instructions"))
        elif eventType == "device_code":
            dialog.showDeviceCode(event)
            dialog.showWaiting("Waiting for authentication...")
        elif eventType == "info":
            dialog.showInfo(read_field(event, "message"), read_field(event, "links"))
        else:
            dialog.showProgress(read_field(event, "message"))

    async def loginProvider(
        self,
        dialog: LoginDialogComponent,
        providerId: str,
        providerName: str,
        method: Literal["api_key", "oauth"],
        *,
        account: str | None = None,
    ) -> None:
        self.programStatus.setBlocked("login", {"kind": "auth", "message": f"Log in to {providerName}"})
        try:
            await self.session.modelRuntime.login(
                providerId,
                method,
                SimpleNamespace(
                    signal=dialog.signal,
                    prompt=lambda prompt: self.showAuthPrompt(dialog, prompt, providerId),
                    notify=lambda event: self.notifyAuthDialog(dialog, event),
                ),
                # MISAKA fork: MISAKA introduces itself by its own name (OpenAI's agent name hint, Codex originator).
                LoginOptions(getDeviceId=lambda: self.settingsManager.getOrCreateDeviceId(), agentName=CLIENT_NAME),
                credentialId=oauth_account_key(providerId, account) if account is not None else None,
            )
        finally:
            self.programStatus.setBlocked("login", None)

    async def showLoginDialog(
        self,
        providerId: str,
        providerName: str,
        onBack: Callable[[], None] | None = None,
        *,
        account: str | None = None,
    ) -> None:
        previousModel = self.session.model
        dialog = LoginDialogComponent(self.ui, providerId, lambda _success, _message: None, providerName)
        self.editorContainer.clear()
        self.editorContainer.addChild(dialog)
        self.ui.setFocus(dialog)
        self.ui.requestRender()

        def restoreEditor() -> None:
            self.editorContainer.clear()
            self.editorContainer.addChild(self.editor)
            self.ui.setFocus(self.editor)
            self.ui.requestRender()

        try:
            await self.loginProvider(dialog, providerId, providerName, "oauth", account=account)
            restoreEditor()
            await self.completeProviderAuthentication(providerId, providerName, "oauth", previousModel, account=account)
            if providerId == RADIUS_PROVIDER_ID:
                self.offerRadiusMcpServer(providerId, providerName)
        except Exception as error:  # noqa: BLE001 - reported below
            restoreEditor()
            errorMsg = errorMessage(error)
            if isinstance(error, CredentialSynchronizationError):
                self.showError(f"Logged in to {providerName}, but local model state could not be synchronized: {errorMsg}")
            elif errorMsg == "Login cancelled":
                if onBack:
                    onBack()
            else:
                self.showError(f"Failed to login to {providerName}: {errorMsg}")

    def offerRadiusMcpServer(self, providerId: str, providerName: str) -> None:
        """Offer to point the Radius MCP server in the global mcp.json at the Radius login, adding the server
        when missing. Nothing is asked when a global server already uses this login."""
        # MISAKA fork: the one module outside the bundled extensions that imports one, by the owner's decision
        # (2026-10-12) to follow upstream here; imported on use, so a session that never logs in to Radius
        # does not load it.
        from misaka.extensions.mcp.config import addMcpServerConfig, loadMcpConfig

        # MISAKA fork: the global mcp.json is this window's role's own (`profiles.mcp_dir`).
        mcpDir = profiles.mcp_dir(self.settingsManager.getModelProfile())
        mcpPath = os.path.join(mcpDir, "mcp.json")

        def normalizeUrl(url: str) -> str:
            return re.sub(r"/+\Z", "", url)

        servers = loadMcpConfig({
            "agentDir": mcpDir,
            "cwd": self.sessionManager.getCwd(),
            "projectTrusted": False,
        })["servers"]
        existing = next(
            (
                server
                for server in servers
                if "url" in server["config"] and normalizeUrl(server["config"]["url"]) == normalizeUrl(RADIUS_MCP_URL)  # type: ignore[typeddict-item]
            ),
            None,
        )
        if (
            existing is not None
            and "url" in existing["config"]
            and (existing["config"].get("auth") or {}).get("provider") == providerId  # type: ignore[union-attr]
        ):
            return

        name = existing["name"] if existing is not None else "radius"
        if existing is None and any(server["name"] == name for server in servers):
            name = "radius-mcp"
        config: dict[str, Any] = (
            {**existing["config"], "auth": {"provider": providerId}}
            if existing is not None and "url" in existing["config"]
            else {"url": RADIUS_MCP_URL, "auth": {"provider": providerId}}
        )
        # `auth` replaces the MCP OAuth sign-in.
        config.pop("oauth", None)

        def create(done: Callable[[], None]) -> dict[str, Any]:
            def onSelect(option: str) -> None:
                done()
                if option != "Yes":
                    return
                try:
                    addMcpServerConfig(mcpPath, name, config)  # type: ignore[arg-type]
                except Exception as error:  # noqa: BLE001 - reported to the user
                    self.showError(f"Could not update {mcpPath}: {errorMessage(error)}")
                    return
                # The MCP extension reads mcp.json when the session starts.
                spawn(self.handleReloadCommand(), self._backgroundTasks)

            def onCancel() -> None:
                done()
                self.ui.requestRender()

            selector = ExtensionSelectorComponent(f"Configure {providerName} MCP in {mcpPath}?", ["Yes", "No"], onSelect, onCancel)
            return {"component": selector, "focus": selector}

        self.showSelector(create)

    # =========================================================================
    # Command handlers
    # =========================================================================

    async def handleReloadCommand(self) -> None:
        if self.session.isStreaming:
            self.showWarning("Wait for the current response to finish before reloading.")
            return
        if self.session.isCompacting:
            self.showWarning("Wait for compaction to finish before reloading.")
            return

        self.resetExtensionUI()

        reloadBox = Container()

        def borderColor(s: str) -> str:
            return theme.fg("border", s)

        reloadBox.addChild(DynamicBorder(borderColor))
        reloadBox.addChild(Spacer(1))
        reloadBox.addChild(
            ThemedText(
                lambda: theme.fg("muted", "Reloading keybindings, extensions, skills, prompts, themes, and context files..."),
                1,
                0,
            )
        )
        reloadBox.addChild(Spacer(1))
        reloadBox.addChild(DynamicBorder(borderColor))

        previousEditor = self.editor
        self.editorContainer.clear()
        self.editorContainer.addChild(reloadBox)
        self.ui.setFocus(reloadBox)
        self.ui.requestRender(True)
        # MISAKA fork: one turn of the event loop stands in for `process.nextTick`.
        await asyncio.sleep(0)

        def dismissReloadBox(editor: Any) -> None:
            self.editorContainer.clear()
            self.editorContainer.addChild(editor)
            self.ui.setFocus(editor)
            self.ui.requestRender()

        chatRestoredBeforeSessionStart = False
        reloadBoxDismissed = False

        def restoreChatBeforeSessionStart() -> None:
            nonlocal chatRestoredBeforeSessionStart
            if chatRestoredBeforeSessionStart:
                return
            self.hideThinkingBlock = self.settingsManager.getHideThinkingBlock()
            self.outputPad = self.settingsManager.getOutputPad()
            self.rebuildChatFromMessages()
            chatRestoredBeforeSessionStart = True

        try:
            await self.session.reload({"beforeSessionStart": restoreChatBeforeSessionStart})
            restoreChatBeforeSessionStart()
            self.keybindings.reload()
            activeHeader = self.customHeader if self.customHeader is not None else self.builtInHeader
            if isExpandable(activeHeader):
                activeHeader.setExpanded(self.toolOutputExpanded)
            setRegisteredThemes(read_field(self.session.resourceLoader.getThemes(), "themes", []))
            self.applyRuntimeSettings()
            self.themeController.applyFromSettings()
            self.setupAutocompleteProvider()
            runner = self.session.extensionRunner
            self.setupExtensionShortcuts(runner)
            self.showLoadedResources({"force": False, "showDiagnosticsWhenQuiet": True})
            savedImplicitProjectTrust = self.maybeSaveImplicitProjectTrustAfterReload()
            modelsJsonError = self.session.modelRuntime.getError()
            if modelsJsonError:
                self.showError(f"models.json error: {modelsJsonError}")
            self.showStatus(
                "Reloaded keybindings, extensions, skills, prompts, themes, and context files; saved project trust"
                if savedImplicitProjectTrust
                else "Reloaded keybindings, extensions, skills, prompts, themes, and context files"
            )
            dismissReloadBox(self.editor)
            reloadBoxDismissed = True
        except Exception as error:  # noqa: BLE001 - shown to the user
            if not reloadBoxDismissed:
                dismissReloadBox(previousEditor)
            self.showError(f"Reload failed: {errorMessage(error)}")

    async def handleExportCommand(self, text: str) -> None:
        outputPath = self.getPathCommandArgument(text, "/export")

        try:
            if outputPath and outputPath.endswith(".jsonl"):
                filePath = self.session.exportToJsonl(outputPath)
                self.showStatus(f"Session exported to: {filePath}")
            else:
                filePath = await self.session.exportToHtml(outputPath, {"themeName": theme.name})
                self.showStatus(f"Session exported to: {filePath}")
        except Exception as error:  # noqa: BLE001 - shown to the user
            self.showError(f"Failed to export session: {errorMessage(error)}")

    def getPathCommandArgument(self, text: str, command: Literal["/export", "/import"]) -> str | None:
        if text == command:
            return None
        if not text.startswith(f"{command} "):
            return None

        argsString = jsTrimStart(text[len(command) + 1 :])
        if not argsString:
            return None

        firstChar = argsString[0]
        if firstChar == '"' or firstChar == "'":
            closingQuoteIndex = argsString.find(firstChar, 1)
            if closingQuoteIndex < 0:
                return None
            return argsString[1:closingQuoteIndex]

        firstWhitespace = _JS_WHITESPACE.search(argsString)
        if firstWhitespace is None:
            return argsString
        return argsString[: firstWhitespace.start()]

    async def handleImportCommand(self, text: str) -> None:
        inputPath = self.getPathCommandArgument(text, "/import")
        if not inputPath:
            self.showError("Usage: /import <path.jsonl>")
            return

        confirmed = await self.showExtensionConfirm("Import session", f"Replace current session with {inputPath}?")
        if not confirmed:
            self.showStatus("Import cancelled")
            return

        try:
            self.clearStatusIndicator()
            result = await self.runtimeHost.importFromJsonl(inputPath)
            if read_field(result, "cancelled"):
                self.showStatus("Import cancelled")
                return
            self.showStatus(f"Session imported from: {inputPath}")
        except MissingSessionCwdError as error:
            selectedCwd = await self.promptForMissingSessionCwd(error)
            if not selectedCwd:
                self.showStatus("Import cancelled")
                return
            result = await self.runtimeHost.importFromJsonl(inputPath, selectedCwd)
            if read_field(result, "cancelled"):
                self.showStatus("Import cancelled")
                return
            self.showStatus(f"Session imported from: {inputPath}")
        # MISAKA fork: a file that is not a session is reported like a missing one, not as a fatal error.
        except (SessionImportFileNotFoundError, InvalidSessionFileError) as error:
            self.showError(f"Failed to import session: {errorMessage(error)}")
        except Exception as error:  # noqa: BLE001 - fatal, as upstream
            await self.handleFatalRuntimeError("Failed to import session", error)

    async def handleShareCommand(self) -> None:
        await shareSession(
            SessionShareContext(
                session=self.session,
                ui=self.ui,
                editorContainer=self.editorContainer,
                editor=self.editor,
                showStatus=lambda message: self.showStatus(message),
                showError=lambda message: self.showError(message),
            )
        )

    # MISAKA fork: no `handleBugCommand` (no `/bug`).

    async def handleCopyCommand(self, options: dict[str, Any] | None = None) -> None:
        # MISAKA fork (D1): copying the selection and flashing the confirmation belong to the fullscreen renderer.
        text = self.session.getLastAssistantText()
        if not text:
            self.showError("No agent messages to copy yet.")
            return

        try:
            await copyToClipboard(text)
            self.showStatus("Copied last agent message to clipboard")
        except Exception as error:  # noqa: BLE001 - shown to the user
            self.showError(errorMessage(error))

    def handleNameCommand(self, text: str) -> None:
        name = jsTrim(re.sub(r"^/name\s*", "", text))
        if not name:
            currentName = self.sessionManager.getSessionName()
            if currentName:
                self.chatContainer.addChild(Spacer(1))
                self.chatContainer.addChild(ThemedText(lambda: theme.fg("dim", f"Session name: {currentName}"), 1, 0))
            else:
                self.showWarning("Usage: /name <name>")
            self.ui.requestRender()
            return

        try:
            self.session.setSessionName(name)
        except ValueError as refusal:   # a role's canonical Bot Chat keeps its name (Hermes `/title`)
            self.showError(str(refusal))
            return
        sessionName = self.sessionManager.getSessionName()
        if sessionName != name:
            self.showWarning(f"Session name was normalized from {jsonStringify(name)} to {jsonStringify(sessionName)}")
        self.chatContainer.addChild(Spacer(1))
        displayName = sessionName if sessionName is not None else name
        self.chatContainer.addChild(ThemedText(lambda: theme.fg("dim", f"Session name set: {displayName}"), 1, 0))
        self.ui.requestRender()

    def handleSessionCommand(self) -> None:
        stats = self.session.getSessionStats()
        sessionName = self.sessionManager.getSessionName()
        entries = self.sessionManager.getEntries()
        cacheWaste = computeCacheWaste(entries, self.session.modelRuntime)

        # Cost/token totals per provider/model actually used (e.g. OpenRouter `auto`
        # resolves to a concrete responseModel). Usage without model attribution is
        # grouped separately so the breakdown reconciles with the session total.
        usageBreakdown = getUsageCostBreakdown(entries)

        # Snapshot the stats; the text is built on demand so it follows theme changes.
        cacheWarmingStatus = self.session.cacheWarmingStatus
        cacheWarmingMode = self.settingsManager.getCacheWarmingMode()
        model = self.session.model
        selectedModelKey = f"{read_field(model, 'provider')}/{read_field(model, 'id')}"

        def localeString(value: float) -> str:
            # MISAKA fork: `toLocaleString()` in the en-US form: digits grouped, at most three decimals.
            return f"{value:,.3f}".rstrip("0").rstrip(".")

        def renderInfo() -> str:
            info = f"{theme.bold('Session Info')}\n\n"
            if sessionName:
                info += f"{theme.fg('dim', 'Name:')} {sessionName}\n"
            info += f"{theme.fg('dim', 'File:')} {stats.sessionFile if stats.sessionFile is not None else 'In-memory'}\n"
            info += f"{theme.fg('dim', 'ID:')} {stats.sessionId}\n\n"
            info += f"{theme.bold('Messages')}\n"
            info += f"{theme.fg('dim', 'Total:')} {stats.totalMessages}\n"
            info += f"{theme.fg('dim', 'User:')} {stats.userMessages}\n"
            info += f"{theme.fg('dim', 'Assistant:')} {stats.assistantMessages}\n"
            info += f"{theme.fg('dim', 'Tools:')} {stats.toolCalls} calls, {stats.toolResults} results\n\n"
            info += f"{theme.bold('Tokens')}\n"
            # "Input" is the full prompt volume. With cache activity, split it into
            # cached (served from cache) vs uncached (everything else) - the only
            # provider-independent split. Cache writes, where reported, are a detail
            # of the uncached portion.
            input = stats.tokens.input
            cacheRead = stats.tokens.cacheRead
            cacheWrite = stats.tokens.cacheWrite
            promptTokens = input + cacheRead + cacheWrite
            info += f"{theme.fg('dim', 'Input:')} {localeString(promptTokens)}\n"
            if promptTokens > 0 and (cacheRead > 0 or cacheWrite > 0):
                hitRate = theme.fg("dim", f"({toFixed((cacheRead / promptTokens) * 100, 1)}%)")
                info += f"  {theme.fg('dim', 'Cached:')} {localeString(cacheRead)} {hitRate}\n"
                written = (
                    f" {theme.fg('dim', f'({localeString(cacheWrite)} written to cache)')}" if cacheWrite > 0 else ""
                )
                info += f"  {theme.fg('dim', 'Uncached:')} {localeString(input + cacheWrite)}{written}\n"
            info += f"{theme.fg('dim', 'Output:')} {localeString(stats.tokens.output)}\n"
            info += f"{theme.fg('dim', 'Total:')} {localeString(stats.tokens.total)}\n"

            info += f"\n{theme.bold('Cache Warming')}\n"
            info += f"{theme.fg('dim', 'Mode:')} {cacheWarmingMode}\n"
            status = (
                formatCacheWarmingStatus(cacheWarmingStatus)
                if cacheWarmingStatus
                else "Inactive (cache warming unavailable)"
            )
            info += f"{theme.fg('dim', 'Status:')} {status}\n"
            decision = read_field(cacheWarmingStatus, "decision")
            if read_field(decision, "economicsAvailable"):
                info += f"{theme.fg('dim', 'Cache miss penalty:')} ${toFixed(decision.missCost, 3)}\n"
                info += f"{theme.fg('dim', 'Refresh cost:')} ${toFixed(decision.warmCost, 3)}\n"

            if stats.cost > 0 or cacheWaste.missedTokens > 0:
                info += f"\n{theme.bold('Cost')}\n"
                info += f"{theme.fg('dim', 'Total:')} ${toFixed(stats.cost, 3)}"
                # A single entry repeats the total, unless it names a model other than the selected one.
                if len(usageBreakdown) > 1 or read_field(jsIndex(usageBreakdown, 0), "key") != selectedModelKey:
                    for entry in usageBreakdown:
                        info += (
                            f"\n  {theme.fg('dim', f'{entry.key}:')} ${toFixed(entry.cost, 3)} "
                            f"{theme.fg('dim', f'({formatTokens(entry.tokens)} tokens)')}"
                        )
                if cacheWaste.missedTokens > 0:
                    missLabel = "1 miss" if cacheWaste.missCount == 1 else f"{cacheWaste.missCount} misses"
                    detail = f"{localeString(cacheWaste.missedTokens)} tokens, {missLabel}"
                    info += (
                        f"\n{theme.fg('dim', 'Cache Re-billed:')} ${toFixed(cacheWaste.missedCost, 3)} {theme.fg('dim', f'({detail})')}"
                        if cacheWaste.missedCost >= 0.0001
                        else f"\n{theme.fg('dim', 'Cache Re-billed:')} {detail}"
                    )
            return info

        self.chatContainer.addChild(Spacer(1))
        self.chatContainer.addChild(ThemedText(renderInfo, 1, 0))
        self.ui.requestRender()

    # MISAKA fork: no `handleChangelogCommand` (no changelog ships).

    def getAppKeyDisplay(self, action: str) -> str:
        """Get capitalized display string for an app keybinding action."""
        return keyDisplayText(action)

    def getEditorKeyDisplay(self, action: str) -> str:
        """Get capitalized display string for an editor keybinding action."""
        return keyDisplayText(action)

    def handleHotkeysCommand(self) -> None:
        # Navigation keybindings
        cursorUp = self.getEditorKeyDisplay("tui.editor.cursorUp")
        cursorDown = self.getEditorKeyDisplay("tui.editor.cursorDown")
        cursorLeft = self.getEditorKeyDisplay("tui.editor.cursorLeft")
        cursorRight = self.getEditorKeyDisplay("tui.editor.cursorRight")
        cursorWordLeft = self.getEditorKeyDisplay("tui.editor.cursorWordLeft")
        cursorWordRight = self.getEditorKeyDisplay("tui.editor.cursorWordRight")
        cursorLineStart = self.getEditorKeyDisplay("tui.editor.cursorLineStart")
        cursorLineEnd = self.getEditorKeyDisplay("tui.editor.cursorLineEnd")
        jumpForward = self.getEditorKeyDisplay("tui.editor.jumpForward")
        jumpBackward = self.getEditorKeyDisplay("tui.editor.jumpBackward")
        pageUp = self.getEditorKeyDisplay("tui.editor.pageUp")
        pageDown = self.getEditorKeyDisplay("tui.editor.pageDown")

        # Editing keybindings
        submit = self.getEditorKeyDisplay("tui.input.submit")
        newLine = self.getEditorKeyDisplay("tui.input.newLine")
        deleteWordBackward = self.getEditorKeyDisplay("tui.editor.deleteWordBackward")
        deleteWordForward = self.getEditorKeyDisplay("tui.editor.deleteWordForward")
        deleteToLineStart = self.getEditorKeyDisplay("tui.editor.deleteToLineStart")
        deleteToLineEnd = self.getEditorKeyDisplay("tui.editor.deleteToLineEnd")
        yank = self.getEditorKeyDisplay("tui.editor.yank")
        yankPop = self.getEditorKeyDisplay("tui.editor.yankPop")
        undo = self.getEditorKeyDisplay("tui.editor.undo")
        tab = self.getEditorKeyDisplay("tui.input.tab")

        # App keybindings
        interrupt = self.getAppKeyDisplay("app.interrupt")
        clear = self.getAppKeyDisplay("app.clear")
        exit = self.getAppKeyDisplay("app.exit")
        suspend = self.getAppKeyDisplay("app.suspend")
        cycleThinkingLevel = self.getAppKeyDisplay("app.thinking.cycle")
        cycleModelForward = self.getAppKeyDisplay("app.model.cycleForward")
        selectModel = self.getAppKeyDisplay("app.model.select")
        expandTools = self.getAppKeyDisplay("app.tools.expand")
        toggleThinking = self.getAppKeyDisplay("app.thinking.toggle")
        externalEditor = self.getAppKeyDisplay("app.editor.external")
        cycleModelBackward = self.getAppKeyDisplay("app.model.cycleBackward")
        copyMessage = self.getAppKeyDisplay("app.message.copy")
        followUp = self.getAppKeyDisplay("app.message.followUp")
        dequeue = self.getAppKeyDisplay("app.message.dequeue")
        pasteImage = self.getAppKeyDisplay("app.clipboard.pasteImage")

        # MISAKA fork: "Copy last assistant message" (selections belong to the fullscreen renderer, D1), and copied
        # files paste on Windows as well.
        hotkeys = f"""
**Navigation**
| Key | Action |
|-----|--------|
| `{cursorUp}` / `{cursorDown}` / `{cursorLeft}` / `{cursorRight}` | Move cursor / browse history |
| `{cursorWordLeft}` / `{cursorWordRight}` | Move by word |
| `{cursorLineStart}` | Start of line |
| `{cursorLineEnd}` | End of line |
| `{jumpForward}` | Jump forward to character |
| `{jumpBackward}` | Jump backward to character |
| `{pageUp}` / `{pageDown}` | Scroll by page |

**Editing**
| Key | Action |
|-----|--------|
| `{submit}` | Send message |
| `{newLine}` | New line{" (Ctrl+Enter on Windows Terminal)" if sys.platform == "win32" else ""} |
| `{deleteWordBackward}` | Delete word backwards |
| `{deleteWordForward}` | Delete word forwards |
| `{deleteToLineStart}` | Delete to start of line |
| `{deleteToLineEnd}` | Delete to end of line |
| `{yank}` | Paste the most-recently-deleted text |
| `{yankPop}` | Cycle through the deleted text after pasting |
| `{undo}` | Undo |

**Other**
| Key | Action |
|-----|--------|
| `{tab}` | Path completion / accept autocomplete |
| `{interrupt}` | Cancel autocomplete / abort streaming |
| `{clear}` | Clear editor (first) / exit (second) |
| `{exit}` | Exit (when editor is empty) |
| `{suspend}` | Suspend to background |
| `{cycleThinkingLevel}` | Cycle thinking level |
| `{cycleModelForward}` / `{cycleModelBackward}` | Cycle models |
| `{selectModel}` | Open model selector |
| `{expandTools}` | Toggle tool output expansion |
| `{toggleThinking}` | Toggle thinking block visibility |
| `{externalEditor}` | Edit message in external editor |
| `{copyMessage}` | Copy last assistant message |
| `{followUp}` | Queue follow-up message |
| `{dequeue}` | Restore queued messages |
| `{pasteImage}` | Paste files on macOS and Windows, images, or text from clipboard |
| `/` | Slash commands |
| `!` | Run bash command |
| `!!` | Run bash command (excluded from context) |
"""

        # Add extension-registered shortcuts
        extensionRunner = self.session.extensionRunner
        shortcuts = extensionRunner.getShortcuts(self.keybindings.getEffectiveConfig())
        if len(shortcuts) > 0:
            hotkeys += """
**Extensions**
| Key | Action |
|-----|--------|
"""
            for key, shortcut in shortcuts.items():
                description = shortcut.description if shortcut.description is not None else shortcut.extensionPath
                keyDisplay = formatKeyText(key, KeyTextFormatOptions(capitalize=True))
                hotkeys += f"| `{keyDisplay}` | {description} |\n"

        self.chatContainer.addChild(Spacer(1))
        self.chatContainer.addChild(DynamicBorder())
        self.chatContainer.addChild(ThemedText(lambda: theme.bold(theme.fg("accent", "Keyboard Shortcuts")), 1, 0))
        self.chatContainer.addChild(Spacer(1))
        self.chatContainer.addChild(Markdown(jsTrim(hotkeys), 1, 1, self.getMarkdownThemeWithSettings()))
        self.chatContainer.addChild(DynamicBorder())
        self.ui.requestRender()

    async def handleClearCommand(self, notice: str = "✓ New session started", *, carry: bool = False) -> bool:
        """MISAKA fork: `notice` is the confirmation shown (`/clear` shows its own), `carry` hands the context to the
        new session (`/new --carry`), and the result says whether a new session started."""
        self.clearStatusIndicator()
        try:
            result = await self.runtimeHost.newSession({"carryOverContext": True} if carry else None)
            if read_field(result, "cancelled"):
                return False
            self.chatContainer.addChild(Spacer(1))
            self.chatContainer.addChild(ThemedText(lambda: theme.fg("accent", notice), 1, 1))
            self.ui.requestRender()
            return True
        except ContextCarryUnsupportedError as error:
            # MISAKA fork: the context engine refused the carry-over; the current session is intact.
            self.showError(errorMessage(error))
            return False
        except Exception as error:  # noqa: BLE001 - fatal, as upstream
            await self.handleFatalRuntimeError("Failed to create session", error)

    async def handleTrueClearCommand(self) -> None:
        """MISAKA fork: `/clear` wipes the current session in place. It runs the full `/new` reset for a clean state,
        then adopts the old id and file (truncated to its header line) and deletes the freshly allocated empty session
        file, so the conversation keeps writing into the cleared session under the same identity."""
        oldId = self.sessionManager.getSessionId()
        oldFile = self.sessionManager.getSessionFile()
        if not await self.handleClearCommand("✓ Session cleared (same session, history wiped)"):
            return
        sm = self.sessionManager
        if not oldId:
            return
        newFile = sm.getSessionFile()
        if newFile and oldFile and newFile != oldFile:
            with contextlib.suppress(OSError):
                if os.path.exists(newFile):
                    os.unlink(newFile)
        sm.sessionId = oldId
        entries = sm.fileEntries
        if entries and isinstance(entries[0], dict) and entries[0].get("type") == "session":
            entries[0]["id"] = oldId
        if oldFile:
            sm.sessionFile = oldFile
            sm.rewrite_file()
            sm.flushed = True

    async def handleBoardCommand(self) -> None:
        """MISAKA fork: `/board` shows the team's task board (read-only)."""
        from misaka.core.network import board

        cwd = self.session.extensionRunner.cwd
        # The board reads SQLite, which stays off the event loop.
        text = await asyncio.to_thread(board.board_for, cwd)
        self.showExtensionNotify(text or "(No task cards on the board.)", "info")

    def handleDebugCommand(self) -> None:
        width = self.ui.terminal.columns
        height = self.ui.terminal.rows
        allLines = self.ui.render(width)

        def jsonDefault(value: Any) -> Any:
            # MISAKA fork: messages are dataclasses, which `json.dumps` cannot write by itself.
            if is_dataclass(value) and not isinstance(value, type):
                return asdict(value)
            return getattr(value, "__dict__", None) or str(value)

        debugLogPath = get_debug_log_path()
        debugData = "\n".join(
            [
                f"Debug output at {datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')}",
                f"Terminal: {width}x{height}",
                f"Total lines: {len(allLines)}",
                "",
                "=== All rendered lines with visible widths ===",
                # MISAKA fork: the lines without their escape sequences, so the dump stays readable: since 1.2 a
                # rendered line also carries the fake cursor's APC markers.
                *[
                    f"[{idx}] (w={visibleWidth(line)}) {jsonStringify(stripTerminalSequences(line))}"
                    for idx, line in enumerate(allLines)
                ],
                "",
                "=== Agent messages (JSONL) ===",
                *[json.dumps(msg, default=jsonDefault, ensure_ascii=False) for msg in self.session.messages],
                "",
            ]
        )

        # MISAKA fork: the dump carries the whole conversation, so it is written the way auth.json is.
        os.makedirs(os.path.dirname(debugLogPath), mode=0o700, exist_ok=True)
        with open(debugLogPath, "w", encoding="utf-8") as file:
            file.write(debugData)
        with contextlib.suppress(OSError):
            os.chmod(debugLogPath, 0o600)

        self.chatContainer.addChild(Spacer(1))
        self.chatContainer.addChild(
            ThemedText(lambda: f"{theme.fg('accent', '✓ Debug log written')}\n{theme.fg('muted', debugLogPath)}", 1, 1)
        )
        self.ui.requestRender()

    # MISAKA fork: no `handleArminSaysHi` or `handleDementedDelves` (easter eggs).

    async def handleBashCommand(self, command: str, excludeFromContext: bool = False) -> None:
        extensionRunner = self.session.extensionRunner

        # Emit user_bash event to let extensions intercept
        try:
            eventResult = await extensionRunner.emitUserBash(
                {
                    "type": "user_bash",
                    "command": command,
                    "excludeFromContext": excludeFromContext,
                    "cwd": self.sessionManager.getCwd(),
                }
            )
        except Exception:  # noqa: BLE001 - reported by the runner
            # The extension runner already reported the error. Do not fall back to local execution.
            return

        # If extension returned a full result, use it directly
        if read_field(eventResult, "result"):
            result = _coerce_bash_result(read_field(eventResult, "result"))

            # Create UI component for display
            self.bashComponent = BashExecutionComponent(command, self.ui, excludeFromContext, self.outputPad)
            if self.session.isStreaming:
                self.pendingMessagesContainer.addChild(self.bashComponent)
                self.pendingBashComponents.append(self.bashComponent)
            else:
                self.chatContainer.addChild(self.bashComponent)

            # Show output and complete
            if result.output:
                self.bashComponent.appendOutput(result.output)
            self.bashComponent.setComplete(
                result.exitCode,
                result.cancelled,
                _make_bash_truncation_result(result.output) if result.truncated else None,
                result.fullOutputPath,
            )

            # Record the result in session
            self.session.recordBashResult(command, result, {"excludeFromContext": excludeFromContext})
            self.bashComponent = None
            self.ui.requestRender()
            return

        # Normal execution path (possibly with custom operations)
        isDeferred = self.session.isStreaming
        self.bashComponent = BashExecutionComponent(command, self.ui, excludeFromContext, self.outputPad)

        if isDeferred:
            # Show in pending area when agent is streaming
            self.pendingMessagesContainer.addChild(self.bashComponent)
            self.pendingBashComponents.append(self.bashComponent)
        else:
            # Show in chat immediately when agent is idle
            self.chatContainer.addChild(self.bashComponent)
        self.ui.requestRender()

        def onChunk(chunk: str) -> None:
            if self.bashComponent:
                self.bashComponent.appendOutput(chunk)
                self.ui.requestRender()

        try:
            result = _coerce_bash_result(
                await self.session.executeBash(
                    command,
                    onChunk,
                    {"excludeFromContext": excludeFromContext, "operations": read_field(eventResult, "operations")},
                )
            )

            if self.bashComponent:
                self.bashComponent.setComplete(
                    result.exitCode,
                    result.cancelled,
                    _make_bash_truncation_result(result.output) if result.truncated else None,
                    result.fullOutputPath,
                )
        except Exception as error:  # noqa: BLE001 - shown to the user
            if self.bashComponent:
                self.bashComponent.setComplete(None, False)
            self.showError(f"Bash command failed: {errorMessage(error)}")

        self.bashComponent = None
        self.ui.requestRender()

    async def handleCompactCommand(self, customInstructions: str | None = None) -> None:
        self.clearStatusIndicator()

        try:
            await self.session.compact(customInstructions)
        except Exception:  # noqa: BLE001, S110 - Ignore, will be emitted as an event
            pass

    def stop(self) -> None:
        # MISAKA fork (D1): no fullscreen exit output to choose.
        self.disposeActiveSelector()
        # MISAKA fork: cancels the extension dialog waiting for its turn as well.
        self._resetExtensionPrompts()
        if self.settingsManager.getShowTerminalProgress():
            self.ui.terminal.setProgress(False)
        self.clearStatusIndicator()
        # MISAKA fork (D11): no theme auto-sync to disable.
        self.clearExtensionTerminalInputListeners()
        self.footer.dispose()
        self.footerDataProvider.dispose()
        if self.unsubscribe:
            self.unsubscribe()
        if self.isInitialized:
            self.stopInteractiveTui()
            self.isInitialized = False
        self.unregisterSignalHandlers()

    # MISAKA fork: Python helpers.

    def _schedule_task(self, awaitable: Coroutine[Any, Any, Any]) -> None:
        """MISAKA fork: ``void promise`` -- the coroutine starts at once, as a JavaScript async call does, and is held
        until it finishes (asyncio holds tasks weakly). A failure is shown in the chat, where Node would crash on the
        unhandled rejection."""
        task = spawn(awaitable, self._backgroundTasks)
        task.add_done_callback(self._reportBackgroundFailure)

    def _reportBackgroundFailure(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and not isinstance(error, SystemExit):
            self.showError(errorMessage(error))


__all__ = [
    "InteractiveMode",
    "InteractiveModeOptions",
    "createInteractiveTui",
    "formatCrashExtensionHint",
    "formatResumeCommand",
]

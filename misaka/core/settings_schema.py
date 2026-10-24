"""Translated from pi's ``packages/coding-agent/src/core/settings-schema.ts`` (v1.3.0).

MISAKA fork: the TypeBox schemas are pydantic models on ``model_schema.TypeBoxObject`` (TypeBox's validation semantics:
extra keys allowed, no ``null`` for an optional field, no coercion), as the ai schema modules are; a description is the
field's ``Field(description=...)`` or the class docstring, a ``default`` annotation is ``json_schema_extra``
(``schemaDefault``). As upstream, nothing validates settings.json against ``SettingsSchema`` at runtime: it gives the
types below and ``scripts/generate_schemas.py`` its ``misaka/schemas/settings.schema.json``.

What the schema describes is MISAKA's settings.json, not pi's:
- left out, because misaka has no such feature: ``lastChangelogVersion``, ``npmCommand``, ``enableAnalytics``,
  ``trackingId``, ``packages`` (no package installs), ``tuiMode`` and the four ``fullscreen*`` keys (no fullscreen
  renderer), and the legacy ``queueMode``, ``websockets`` and ``retry.maxDelayMs`` (misaka never shipped them and has
  no settings migration);
- ``skills`` is MISAKA's skill-index section, not pi's list of skill paths;
- MISAKA's own sections follow pi's keys: ``web``, ``moa``, ``allies``, and one section per ``config/product.KNOBS``
  section (``research``, ``network``, ``subagents``, ...). An extension's own sections (core may not name them) stay
  under ``additionalProperties``; the generator adds the bundled ones' descriptions.
- the descriptions say MISAKA where pi's say Pi, and describe misaka's meaning where it differs.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from pydantic import ConfigDict, Field, create_model

from misaka.ai.providers.model_schema import (
    MODEL_THINKING_LEVELS,
    TypeBoxInteger,
    TypeBoxLiteralUnion,
    TypeBoxObject,
    schemaDefault,
)
from misaka.ai.types import Transport
from misaka.config.product import KNOBS, NOT_NEGATIVE
from misaka.core.settings_defaults import SETTINGS_DEFAULTS

_MAX_SAFE_INTEGER = 2**53 - 1
_NO_DEFAULT: Any = object()


def _optional(description: str | None = None, default: Any = _NO_DEFAULT) -> Any:
    """MISAKA fork: ``Type.Optional(T({ description?, default? }))`` without ``T``, which is the field's annotation."""
    return Field(
        default=None,
        description=description,
        json_schema_extra=None if default is _NO_DEFAULT else schemaDefault(default),
    )


def nonNegativeSafeInteger() -> Any:
    """``Type.Integer({ minimum: 0, maximum: Number.MAX_SAFE_INTEGER })``. MISAKA fork: a default annotation is the
    field's ``_optional(..., default)``."""
    return TypeBoxInteger(ge=0, le=_MAX_SAFE_INTEGER)


# `timeoutSetting(options)` without its options, which are the field's.
_TimeoutSetting = Annotated[float, Field(ge=0)] | Literal["disabled"]

# MISAKA fork: `ModelThinkingLevelSchema` as a field: a multi-value `Literal` in a schema is a `TypeBoxLiteralUnion`, so
# its `enum` keeps the declared order whatever else the process imported.
_ModelThinkingLevelField = TypeBoxLiteralUnion(*MODEL_THINKING_LEVELS)


class CompactionModelOverrideSchema(TypeBoxObject):
    reserveTokens: nonNegativeSafeInteger() | None = None
    keepRecentTokens: nonNegativeSafeInteger() | None = None


class CompactionSettingsSchema(TypeBoxObject):
    enabled: bool | None = _optional(default=SETTINGS_DEFAULTS["compaction"]["enabled"])
    reserveTokens: nonNegativeSafeInteger() | None = _optional(default=SETTINGS_DEFAULTS["compaction"]["reserveTokens"])
    keepRecentTokens: nonNegativeSafeInteger() | None = _optional(
        default=SETTINGS_DEFAULTS["compaction"]["keepRecentTokens"]
    )
    modelOverrides: dict[str, CompactionModelOverrideSchema] | None = _optional(
        'Per-model overrides keyed by exact "provider/modelId" strings.'
    )


class BranchSummarySettingsSchema(TypeBoxObject):
    reserveTokens: float | None = _optional(
        "Tokens reserved for the prompt and LLM response.", SETTINGS_DEFAULTS["branchSummary"]["reserveTokens"]
    )
    skipPrompt: bool | None = _optional(
        'When true, skips the "Summarize branch?" prompt and defaults to no summary.',
        SETTINGS_DEFAULTS["branchSummary"]["skipPrompt"],
    )


class ProviderRetrySettingsSchema(TypeBoxObject):
    timeoutMs: float | None = _optional("SDK or provider request timeout in milliseconds.")
    maxRetries: float | None = _optional("SDK or provider retry attempts.")
    maxRetryDelayMs: float | None = _optional(
        "Maximum server-requested delay before failing.", SETTINGS_DEFAULTS["retry"]["provider"]["maxRetryDelayMs"]
    )


class RetrySettingsSchema(TypeBoxObject):
    enabled: bool | None = _optional(default=SETTINGS_DEFAULTS["retry"]["enabled"])
    maxRetries: float | None = _optional(default=SETTINGS_DEFAULTS["retry"]["maxRetries"])
    baseDelayMs: float | None = _optional(
        "Exponential backoff base delay in milliseconds: 2s, 4s, 8s.", SETTINGS_DEFAULTS["retry"]["baseDelayMs"]
    )
    maxAgentDelayMs: float | None = _optional(default=SETTINGS_DEFAULTS["retry"]["maxAgentDelayMs"])
    provider: ProviderRetrySettingsSchema | None = None
    # MISAKA fork: no legacy `maxDelayMs` (misaka never shipped it and has no settings migration).


class TerminalSettingsSchema(TypeBoxObject):
    showImages: bool | None = _optional(
        "Show images when the terminal supports them.", SETTINGS_DEFAULTS["terminal"]["showImages"]
    )
    imageWidthCells: float | None = _optional(
        "Preferred inline image width in terminal cells.", SETTINGS_DEFAULTS["terminal"]["imageWidthCells"]
    )
    clearOnShrink: bool | None = _optional(
        "Clear empty rows when content shrinks.", SETTINGS_DEFAULTS["terminal"]["clearOnShrink"]
    )
    showTerminalProgress: bool | None = _optional(
        "Show OSC 9;4 terminal progress indicators.", SETTINGS_DEFAULTS["terminal"]["showTerminalProgress"]
    )
    hyperlinks: bool | Literal["auto"] | None = None
    images: TypeBoxLiteralUnion("kitty", "iterm2", "auto", False) | None = None
    trueColor: bool | Literal["auto"] | None = None


class ImageSettingsSchema(TypeBoxObject):
    autoResize: bool | None = _optional(
        "Resize images to 2000x2000 maximum for better model compatibility.", SETTINGS_DEFAULTS["images"]["autoResize"]
    )
    blockImages: bool | None = _optional(
        "When true, prevents all images from being sent to LLM providers.", SETTINGS_DEFAULTS["images"]["blockImages"]
    )


class ThinkingBudgetsSettingsSchema(TypeBoxObject):
    # `thinkingBudgetsSettings(options)`: the options are the field's.
    minimal: float | None = None
    low: float | None = None
    medium: float | None = None
    high: float | None = None


# `MarkdownSettingsSchema.mermaid` without its options.
_MermaidRenderingModeSchema = TypeBoxLiteralUnion("off", "final", "streaming")


class MarkdownSettingsSchema(TypeBoxObject):
    codeBlockIndent: str | None = _optional(default=SETTINGS_DEFAULTS["markdown"]["codeBlockIndent"])
    mermaid: _MermaidRenderingModeSchema | None = _optional(default=SETTINGS_DEFAULTS["markdown"]["mermaid"])


class WarningSettingsSchema(TypeBoxObject):
    anthropicExtraUsage: bool | None = _optional(default=SETTINGS_DEFAULTS["warnings"]["anthropicExtraUsage"])


# `codemodeMode(options)` without its options; `CodemodeModeSchema`.
CodemodeModeSchema = TypeBoxLiteralUnion("on", "only")


class CodemodeSettingsSchema(TypeBoxObject):
    mode: CodemodeModeSchema | None = _optional(
        'How codemode presents tools. "on" keeps direct tools declared and annotates tools callable from scripts; codemode lists only tools without direct exposure. "only" lists every script-callable tool in codemode and does not declare active direct tools.',
        SETTINGS_DEFAULTS["codemode"]["mode"],
    )
    inlineBudget: Annotated[float, Field(ge=0)] | None = _optional(
        "Estimated tokens available for inline codemode tool declarations.", SETTINGS_DEFAULTS["codemode"]["inlineBudget"]
    )


class _SkillsSettingsSchema(TypeBoxObject):
    """MISAKA's skill index: more skill folders, and skills to hide. A list of skill paths is not read here."""

    # MISAKA fork: the whole class (MISAKA's skill index, core/skills/layers.py, replaced upstream's skills and its
    # list of skill paths).
    external_dirs: str | list[str] | None = _optional(
        "More folders of skills to read; ~/.agents/skills is always read when it exists."
    )
    disabled: str | list[str] | None = _optional("Skill names to hide from every role.")
    platform_disabled: dict[str, str | list[str]] | None = _optional(
        "Skill names to hide on one surface, keyed by its name."
    )


# MISAKA fork: the product knobs (`config/product.KNOBS`), one object per settings.json section. A knob's type is
# what `product.setting()` casts its value to; its default stays with the caller (two are computed), so none is given.
# The schema gives the canonical JSON type; `setting()` also takes its string spellings ("8", "yes"), which an editor
# then flags. A knob that may not be negative (`product.NOT_NEGATIVE`) is refused at start, and has `minimum: 0`.
_KNOB_TYPES: dict[Any, Any] = {int: TypeBoxInteger(), float: float, bool: bool, str: str, None: Any}
_NOT_NEGATIVE_KNOB_TYPES: dict[Any, Any] = {int: TypeBoxInteger(ge=0), float: Annotated[float, Field(ge=0)]}
_PRODUCT_SECTIONS: dict[str, str] = {
    "documents": "Document reading.",
    "mcp": "MCP servers, as sub-agents wait for them. The servers themselves are in each role's mcp.json.",
    "network": "How many Sister cards run at once.",
    "panel": "The panel.",
    "research": "Research runs.",
    "skills": _SkillsSettingsSchema.__doc__ or "",
    "subagents": "Sub-agents of a session.",
    "tui": "The chat window.",
    "vision": "Reading images for models that cannot see.",
}
_KNOB_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("documents", "ocr_langs"): 'Languages for reading scanned PDFs, joined with "+". Default: eng+chi_sim+jpn.',
    ("mcp", "required_wait"): "Seconds a sub-agent waits for an MCP server its definition requires. Default: 30.",
    ("network", "max_concurrent_per_sister"): "Cards one Sister runs at once. Default: the machine limit.",
    ("network", "max_concurrent_sisters"): "Cards running at once on this machine. Default: free memory / 256 MiB, between 4 and 12.",
    ("panel", "prefix"): 'The panel\'s prefix key, "ctrl+" and a letter. Default: ctrl+b.',
    ("research", "beast_at"): "Share of the token cap at which a card is told to wrap up. Default: 0.85.",
    ("research", "plan_approval"): "Every research plan waits for your go-ahead. Default: true.",
    ("research", "token_cap"): "Token budget of one research run, its cards and sub-agents included; 0 is no cap.",
    ("subagents", "agent_list_in_messages"): "List the available sub-agent types in messages.",
    ("subagents", "auto_background_tasks"): "Move a foreground sub-agent to the background after two minutes.",
    ("subagents", "auto_memory"): "Let sub-agents keep memory between tasks.",
    ("subagents", "background_tasks"): "Allow sub-agents to run in the background.",
    ("subagents", "builtin_agents"): "Offer the built-in sub-agent types.",
    ("subagents", "coordinator_mode"): "Coordinator mode: a session does not fork sub-agents from its own conversation.",
    ("subagents", "effort_level"): '"auto", "low", "medium", "high", "xhigh" or "max", for models that take an effort setting.',
    ("subagents", "inherit_process_group"): "Run sub-agents in the session's own process group instead of a new one.",
    ("subagents", "managed_agents_dir"): "A folder of sub-agent definitions that take precedence over all others.",
    ("subagents", "max_concurrent"): "Sub-agents one session runs at once. Default: 20.",
    ("subagents", "memory_home"): "Where sub-agent memory is kept. Default: state/agent-memory.",
    ("subagents", "memory_snapshot"): "In a trusted project, start a sub-agent's memory from the project's memory snapshot.",
    ("subagents", "simple"): "Simple mode: sub-agents keep no memory.",
    ("subagents", "small_fast_model"): "The model a sub-agent type's hooks use when they name none.",
    ("subagents", "task_max_output"): "Characters from the end of a background task's output that the output tool shows.",
    ("subagents", "verification_agent"): "Offer the built-in verification sub-agent type.",
    ("tui", "esc_timeout_ms"): "How long Esc waits to see whether it starts an Alt+key, in milliseconds.",
    ("vision", "model"): "One vision model (provider/model) for the whole home, which reads images for models that cannot see.",
}


def _productSection(section: str) -> type[TypeBoxObject]:
    knobs = {
        key: (
            (_NOT_NEGATIVE_KNOB_TYPES if (section, key) in NOT_NEGATIVE else _KNOB_TYPES)[cast] | None,
            _optional(_KNOB_DESCRIPTIONS.get((section, key))),
        )
        for (knobSection, key), cast in sorted(KNOBS.items())
        if knobSection == section
    }
    return create_model(  # type: ignore[call-overload, no-any-return]
        f"_{section.title()}SectionSchema",
        __base__=_SkillsSettingsSchema if section == "skills" else TypeBoxObject,
        __module__=__name__,
        __doc__=_PRODUCT_SECTIONS[section],
        **knobs,
    )


_productSectionSchemas = {section: _productSection(section) for section in sorted(_PRODUCT_SECTIONS)}
_SkillsSectionSchema = _productSectionSchemas["skills"]
if set(_productSectionSchemas) != {section for section, _key in KNOBS} | {"skills"}:
    raise AssertionError("every config/product.KNOBS section needs a description in settings_schema._PRODUCT_SECTIONS")


class _SettingsSchemaObject(TypeBoxObject):
    # `{ additionalProperties: true }`: every extension and tool may keep a section of its own. A comment, not a
    # docstring: pydantic would publish a docstring as the schema's description.
    model_config = ConfigDict(json_schema_extra={"additionalProperties": True})


class _PiSettingsSchema(_SettingsSchemaObject):
    """The keys misaka shares with pi, in pi's order. MISAKA fork: the class; ``SettingsSchema`` adds MISAKA's
    sections after them."""

    schema_: str | None = Field(default=None, alias="$schema", description="JSON Schema reference.")
    defaultProvider: str | None = _optional(
        "Provider of the default model. A role's own settings.json may hold its own pair, which wins for that role."
    )
    defaultModel: str | None = _optional(
        "Default model id, with defaultProvider. A role's own settings.json may hold its own pair, which wins for that role."
    )
    defaultThinkingLevel: _ModelThinkingLevelField | None = _optional(
        "Thinking level a new session starts at. A role's own settings.json may hold its own, which wins for that role."
    )
    modelThinkingLevels: dict[str, _ModelThinkingLevelField] | None = _optional(
        'Per-model default thinking level overrides keyed by "provider/modelId".'
    )
    transport: TypeBoxLiteralUnion("auto", "sse", "websocket", "websocket-cached") | None = _optional(
        default=SETTINGS_DEFAULTS["transport"]
    )
    sessionShareDestination: TypeBoxLiteralUnion("auto", "gist", "radius") | None = _optional(
        'Destination used by /share. "auto" uses Radius when authenticated and otherwise creates a GitHub gist.',
        SETTINGS_DEFAULTS["sessionShareDestination"],
    )
    steeringMode: TypeBoxLiteralUnion("all", "one-at-a-time") | None = _optional(default=SETTINGS_DEFAULTS["steeringMode"])
    followUpMode: TypeBoxLiteralUnion("all", "one-at-a-time") | None = _optional(default=SETTINGS_DEFAULTS["followUpMode"])
    theme: str | None = None
    compaction: CompactionSettingsSchema | None = None
    branchSummary: BranchSummarySettingsSchema | None = None
    retry: RetrySettingsSchema | None = None
    hideThinkingBlock: bool | None = _optional(default=SETTINGS_DEFAULTS["hideThinkingBlock"])
    showCacheMissNotices: bool | None = _optional(
        "Show cache cost and provider recovery notices.", SETTINGS_DEFAULTS["showCacheMissNotices"]
    )
    externalEditor: str | None = _optional(
        "Command for Ctrl+G external editor; takes precedence over VISUAL and EDITOR."
    )
    shellPath: str | None = _optional(
        "Custom shell path, for example for Cygwin on Windows, with support for leading ~ expansion."
    )
    quietStartup: bool | Literal["header"] | None = _optional(
        'When true, hide all startup output. When "header", keep only the startup header.',
        SETTINGS_DEFAULTS["quietStartup"],
    )
    defaultProjectTrust: TypeBoxLiteralUnion("ask", "always", "never") | None = _optional(
        "Global setting only.", SETTINGS_DEFAULTS["defaultProjectTrust"]
    )
    shellCommandPrefix: str | None = _optional(
        "Prefix prepended to every bash command, for example to enable shell aliases."
    )
    collapseChangelog: bool | None = _optional(
        # MISAKA fork: no changelog ships, so the setting changes nothing.
        "Kept for compatibility; MISAKA ships no changelog, so this has no effect.",
        SETTINGS_DEFAULTS["collapseChangelog"],
    )
    enableInstallTelemetry: bool | None = _optional(
        # MISAKA fork: misaka's meaning (no install ping), default off.
        'Name this client "misaka" to aggregator gateways such as OpenRouter, NVIDIA and Cloudflare; nothing else is sent.',
        SETTINGS_DEFAULTS["enableInstallTelemetry"],
    )
    extensions: list[str] | None = _optional(
        "Local extension file paths or directories, and +builtin:<name> or -builtin:<name> entries that turn a built-in extension on or off."
    )
    deviceId: str | None = _optional(
        "Stable installation UUID, generated when authentication first needs it. Global only."
    )
    skills: _SkillsSectionSchema | None = None  # type: ignore[valid-type]
    prompts: list[str] | None = _optional("Local prompt template file paths or directories.")
    themes: list[str] | None = _optional("Local theme file paths or directories.")
    enableSkillCommands: bool | None = _optional(
        "Register skills as /skill:name commands.", SETTINGS_DEFAULTS["enableSkillCommands"]
    )
    terminal: TerminalSettingsSchema | None = None
    images: ImageSettingsSchema | None = None
    enabledModels: list[str] | None = _optional(
        "Model patterns for cycling, in the same format as the --models CLI flag."
    )
    defaultTools: list[str] | None = _optional(
        "Initial tool selection. Plain names replace the inherited selection; +name and -name entries add or remove tools."
    )
    doubleEscapeAction: TypeBoxLiteralUnion("fork", "tree", "none") | None = _optional(
        "Action for double-escape with an empty editor.", SETTINGS_DEFAULTS["doubleEscapeAction"]
    )
    treeFilterMode: TypeBoxLiteralUnion("default", "no-tools", "user-only", "labeled-only", "all") | None = _optional(
        "Default filter when opening /tree.", SETTINGS_DEFAULTS["treeFilterMode"]
    )
    thinkingBudgets: ThinkingBudgetsSettingsSchema | None = _optional("Custom token budgets for thinking levels.")
    editorPaddingX: float | None = _optional(
        "Horizontal padding for the input editor.", SETTINGS_DEFAULTS["editorPaddingX"]
    )
    outputPad: TypeBoxLiteralUnion(0, 1) | None = _optional(
        "Horizontal padding for transcript content.", SETTINGS_DEFAULTS["outputPad"]
    )
    autocompleteMaxVisible: float | None = _optional(
        "Maximum visible items in the autocomplete dropdown.", SETTINGS_DEFAULTS["autocompleteMaxVisible"]
    )
    # Since pi 1.2 the drawn cursor and the terminal's are exclusive (1cedd327).
    showHardwareCursor: bool | None = _optional("Use the terminal cursor instead of MISAKA's drawn cursor.")
    markdown: MarkdownSettingsSchema | None = None
    warnings: WarningSettingsSchema | None = None
    codemode: CodemodeSettingsSchema | None = None
    sessionDir: str | None = _optional(
        "Custom session storage directory, in the same format as the --session-dir CLI flag."
    )
    httpProxy: str | None = _optional(
        "Proxy URL applied as HTTP_PROXY and HTTPS_PROXY for MISAKA-managed HTTP clients."
    )
    httpIdleTimeoutMs: _TimeoutSetting | None = _optional(
        'HTTP header or body idle timeout in milliseconds; 0 or "disabled" disables it.'
    )
    cacheWarming: TypeBoxLiteralUnion("off", "streaming", "idle") | None = _optional(
        'Cache-warming profile. "idle" also warms between agent runs. Global only because each refresh costs money.',
        SETTINGS_DEFAULTS["cacheWarming"],
    )
    websocketConnectTimeoutMs: _TimeoutSetting | None = _optional(
        'WebSocket connect or open handshake timeout in milliseconds; 0 or "disabled" disables it.'
    )


# MISAKA fork: MISAKA's own sections, whose modules read whatever shape they are given and normalize it.
# MISAKA fork: the sub-agent system's own keys (core/subagent, ported from CCB), read from the home's settings.json,
# a trusted project's and the managed settings.
# The modes a top-level session can start in (subagent.configuration.TOP_LEVEL_PERMISSION_MODES).
_TopLevelPermissionModeField = TypeBoxLiteralUnion("default", "plan", "acceptEdits", "bypassPermissions", "dontAsk")


class _SubagentPermissionsSchema(TypeBoxObject):
    """Permission rules for sub-agents and their hooks."""

    allow: list[str] | None = _optional('Tool rules that are allowed without asking, such as "Bash(git status)".')
    ask: list[str] | None = _optional("Tool rules that always ask first.")
    deny: list[str] | None = _optional('Tool rules that are refused; "Agent(type)" hides that sub-agent type.')
    additionalDirectories: list[str] | None = _optional("More folders sub-agents may work in, besides the workspace.")
    writeDirectories: dict[str, list[str]] | None = _optional(
        "Absolute folders that write or edit (the keys) may change outside the workspace."
    )
    defaultMode: _TopLevelPermissionModeField | None = _optional(
        "The permission mode every session starts in (CCB's defaultMode); its sub-agents inherit it. "
        "Unset is bypassPermissions: nothing asks. A session nobody watches (a card, a contact turn) "
        "refuses what would ask."
    )
    disableBypassPermissionsMode: Literal["disable"] | None = _optional(
        '"disable" keeps every session out of bypassPermissions; an unset defaultMode is then default.'
    )


class _SubagentWorktreeSchema(TypeBoxObject):
    """How the git worktree of a sub-agent started with isolation "worktree" is made."""

    sparsePaths: list[str] | None = _optional("Check out only these repository paths.")
    symlinkDirectories: list[str] | None = _optional(
        "Repository folders linked into the worktree instead of copied (trusted projects only)."
    )


_misakaSections: dict[str, Any] = {
    "web": (
        dict[str, Any] | None,
        _optional(
            "Web search and fetch backends, their tiers, caches and blocklists; `misaka web` writes it. A role's own settings.json may hold its own web section, laid over this one."
        ),
    ),
    "moa": (
        dict[str, Any] | None,
        _optional("Mixture-of-Agents presets (presets, default_preset), published as models of the moa provider."),
    ),
    "allies": (
        dict[str, Any] | None,
        _optional("Other vendors' agents that can take cards, by name: a preset name, or an entry with command, env and description."),
    ),
    "agents": (
        dict[str, dict[str, Any]] | None,
        _optional("Sub-agent types, by name: description, prompt, and the other fields of an agent definition file."),
    ),
    "pluginOptions": (dict[str, dict[str, Any]] | None, _optional("Options for each plugin's sub-agents, by plugin name.")),
    "permissions": (_SubagentPermissionsSchema | None, Field(default=None)),
    "hooks": (
        dict[str, list[dict[str, Any]]] | None,
        _optional("Sub-agent hooks, by event: matchers, each with the hooks it runs."),
    ),
    "disableAllHooks": (bool | None, _optional("Run no sub-agent hooks.")),
    "worktree": (_SubagentWorktreeSchema | None, Field(default=None)),
    "cleanupPeriodDays": (
        Annotated[float, Field(ge=0)] | None,
        _optional("Days after which a sub-agent worktree that is no longer used is removed. Default: 30."),
    ),
    "autoMemoryEnabled": (bool | None, _optional("Let sub-agents keep memory between tasks. Default: true.")),
    **{
        section: (schema | None, Field(default=None))
        for section, schema in _productSectionSchemas.items()
        if section != "skills"
    },
}

SettingsSchema: type[_SettingsSchemaObject] = create_model(  # type: ignore[call-overload]
    "SettingsSchema",
    __base__=_PiSettingsSchema,
    __module__=__name__,
    __doc__=None,
    **_misakaSections,
)


# `Static<typeof ...>`. MISAKA fork: settings are read as plain JSON mappings, so the object types are mappings.
type CompactionModelOverride = dict[str, Any]
type CompactionSettings = dict[str, Any]
type BranchSummarySettings = dict[str, Any]
type ProviderRetrySettings = dict[str, Any]
type RetrySettings = dict[str, Any]
type TerminalSettings = dict[str, Any]
type ImageSettings = dict[str, Any]
type ThinkingBudgetsSettings = dict[str, Any]
type MermaidRenderingMode = Literal["off", "final", "streaming"]
type MarkdownSettings = dict[str, Any]
type WarningSettings = dict[str, Any]
# How the codemode tool presents tools while it is active.
# - `on`: declared tools that scripts can call get a note on calling them from scripts appended to
#   their description; the codemode description lists only the tools without `direct` exposure.
# - `only`: the codemode description lists every tool scripts can call, and active `direct` tools are
#   not declared to the model.
type CodemodeMode = Literal["on", "only"]


class CodemodeSettings(TypedDict, total=False):
    # Default: `on`.
    mode: CodemodeMode
    # Estimated tokens (characters / 4) the codemode description may spend on tool declarations. Default: 3000.
    inlineBudget: float


type DefaultProjectTrust = Literal["ask", "always", "never"]
# true hides all startup output, "header" keeps only the startup header.
type QuietStartup = bool | Literal["header"]
type TransportSetting = Transport
type SessionShareDestination = Literal["auto", "gist", "radius"]
type CacheWarmingMode = Literal["off", "streaming", "idle"]
# MISAKA fork: `skills` is MISAKA's skill-index section, not pi's `string[]`.
type Settings = dict[str, Any]


__all__ = [
    "BranchSummarySettings",
    "BranchSummarySettingsSchema",
    "CacheWarmingMode",
    "CodemodeMode",
    "CodemodeModeSchema",
    "CodemodeSettings",
    "CodemodeSettingsSchema",
    "CompactionModelOverride",
    "CompactionModelOverrideSchema",
    "CompactionSettings",
    "CompactionSettingsSchema",
    "DefaultProjectTrust",
    "ImageSettings",
    "ImageSettingsSchema",
    "MarkdownSettings",
    "MarkdownSettingsSchema",
    "MermaidRenderingMode",
    "ProviderRetrySettings",
    "ProviderRetrySettingsSchema",
    "QuietStartup",
    "RetrySettings",
    "RetrySettingsSchema",
    "SessionShareDestination",
    "Settings",
    "SettingsSchema",
    "TerminalSettings",
    "TerminalSettingsSchema",
    "ThinkingBudgetsSettings",
    "ThinkingBudgetsSettingsSchema",
    "TransportSetting",
    "WarningSettings",
    "WarningSettingsSchema",
    "nonNegativeSafeInteger",
]

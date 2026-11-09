"""Translated from pi's ``modes/interactive/components/index.ts`` (v1.1.0): UI components for extensions.

MISAKA forks: the names load on first use (PEP 562). In TypeScript, importing one component module does not run the
index; in Python a package's ``__init__`` runs before any of its submodules, so eager imports here would load every
component (and the tools behind them) whenever any one of them is imported. Not ported: ``ArminComponent`` (easter
egg) and ``FirstTimeSetupComponent`` (pi's official distribution only). MISAKA adds ``AskUserQuestionComponent``.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, str] = {
    "AssistantMessageComponent": "assistant_message",
    "BashExecutionComponent": "bash_execution",
    "BorderedLoader": "bordered_loader",
    "BranchSummaryMessageComponent": "branch_summary_message",
    "CompactionSummaryMessageComponent": "compaction_summary_message",
    "CustomEditor": "custom_editor",
    "CustomEditorOptions": "custom_editor",
    "CustomMessageComponent": "custom_message",
    "RenderDiffOptions": "diff",
    "renderDiff": "diff",
    "DynamicBorder": "dynamic_border",
    "ExtensionEditorComponent": "extension_editor",
    "ExtensionInputComponent": "extension_input",
    "ExtensionSelectorComponent": "extension_selector",
    "FooterComponent": "footer",
    "keyHint": "keybinding_hints",
    "keyText": "keybinding_hints",
    "rawKeyHint": "keybinding_hints",
    "LoginDialogComponent": "login_dialog",
    "ModelSelectorComponent": "model_selector",
    "OAuthSelectorComponent": "oauth_selector",
    "ModelsCallbacks": "scoped_models_selector",
    "ModelsConfig": "scoped_models_selector",
    "ScopedModelsSelectorComponent": "scoped_models_selector",
    "SessionSelectorComponent": "session_selector",
    "SettingsCallbacks": "settings_selector",
    "SettingsConfig": "settings_selector",
    "SettingsSelectorComponent": "settings_selector",
    "ShowImagesSelectorComponent": "show_images_selector",
    "SkillInvocationMessageComponent": "skill_invocation_message",
    "ThemeSelectorComponent": "theme_selector",
    "ThinkingSelectorComponent": "thinking_selector",
    "ToolExecutionComponent": "tool_execution",
    "ToolExecutionOptions": "tool_execution",
    "TreeSelectorComponent": "tree_selector",
    "TrustSelectorComponent": "trust_selector",
    "UserMessageComponent": "user_message",
    "UserMessageSelectorComponent": "user_message_selector",
    "truncateToVisualLines": "visual_truncate",
    "VisualTruncateResult": "visual_truncate",
    # MISAKA additions
    "AskUserQuestionComponent": "ask_user_question",
}

__all__ = [
    "AskUserQuestionComponent",
    "AssistantMessageComponent",
    "BashExecutionComponent",
    "BorderedLoader",
    "BranchSummaryMessageComponent",
    "CompactionSummaryMessageComponent",
    "CustomEditor",
    "CustomEditorOptions",
    "CustomMessageComponent",
    "DynamicBorder",
    "ExtensionEditorComponent",
    "ExtensionInputComponent",
    "ExtensionSelectorComponent",
    "FooterComponent",
    "LoginDialogComponent",
    "ModelSelectorComponent",
    "ModelsCallbacks",
    "ModelsConfig",
    "OAuthSelectorComponent",
    "RenderDiffOptions",
    "ScopedModelsSelectorComponent",
    "SessionSelectorComponent",
    "SettingsCallbacks",
    "SettingsConfig",
    "SettingsSelectorComponent",
    "ShowImagesSelectorComponent",
    "SkillInvocationMessageComponent",
    "ThemeSelectorComponent",
    "ThinkingSelectorComponent",
    "ToolExecutionComponent",
    "ToolExecutionOptions",
    "TreeSelectorComponent",
    "TrustSelectorComponent",
    "UserMessageComponent",
    "UserMessageSelectorComponent",
    "VisualTruncateResult",
    "keyHint",
    "keyText",
    "rawKeyHint",
    "renderDiff",
    "truncateToVisualLines",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value

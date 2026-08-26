"""Authentication guidance helpers for coding-agent errors."""

from __future__ import annotations

from misaka.config import get_auth_path, get_models_path

_UNKNOWN_PROVIDER = "unknown"


def get_provider_login_help() -> str:
    return "\n".join(
        [
            "Use /login to log into a provider via OAuth or API key, or set the provider's API key",
            "in the environment. Credentials are kept in:",
            f"  {get_auth_path()}",
            f"Custom providers and models are defined in:",
            f"  {get_models_path()}",
            "`misaka auth check` reports what is configured.",
        ]
    )


def format_no_models_available_message() -> str:
    return f"No models available. {get_provider_login_help()}"


def format_no_model_selected_message() -> str:
    return f"No model selected.\n\n{get_provider_login_help()}\n\nThen use /model to select a model."


def format_no_api_key_found_message(provider: str) -> str:
    provider_display = "the selected model" if provider == _UNKNOWN_PROVIDER else provider
    return f"No API key found for {provider_display}.\n\n{get_provider_login_help()}"


formatNoApiKeyFoundMessage = format_no_api_key_found_message
formatNoModelSelectedMessage = format_no_model_selected_message
formatNoModelsAvailableMessage = format_no_models_available_message
getProviderLoginHelp = get_provider_login_help

__all__ = [
    "formatNoApiKeyFoundMessage",
    "formatNoModelSelectedMessage",
    "formatNoModelsAvailableMessage",
    "format_no_api_key_found_message",
    "format_no_model_selected_message",
    "format_no_models_available_message",
    "getProviderLoginHelp",
    "get_provider_login_help",
]

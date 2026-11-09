"""MISAKA's provider overview: which providers are signed in and have their vendor SDK installed.

MISAKA fork: pi has no such overview. ``misaka auth check`` with neither ``--provider`` nor
``--model`` prints it (pi rejects that form), and the setup wizard reads the same SDK check.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass

from misaka.ai.api._sdk import EXTRAS
from misaka.ai.models import ModelsRefreshOptions
from misaka.cli.auth_check import createAuthCheckModelRuntime
from misaka.config import get_auth_path
from misaka.core.auth_storage import AuthStorage, ReadOnlyAuthStorage
from misaka.core.model_runtime import ModelRuntime

# Which vendor SDK each wire protocol needs, as ``api -> (module, PyPI package)``. The
# provider modules import these behind ``try/except ImportError`` and only fail at the first
# request, through ``misaka.ai.api._sdk.require`` -- so a credential alone proves
# nothing about whether the provider can serve a call. An api absent from this table
# (``pi-messages``, ``openai-codex-responses``, ``mistral-conversations``) speaks its
# protocol over httpx and needs no extra. ``EXTRAS`` turns the package into the extra name
# for the pip command.
API_SDK_REQUIREMENTS = {
    "anthropic-messages": ("anthropic", "anthropic"),
    "openai-completions": ("openai", "openai"),
    "openai-responses": ("openai", "openai"),
    "azure-openai-responses": ("openai", "openai"),
    "google-generative-ai": ("google.genai", "google-genai"),
    "google-vertex": ("google.genai", "google-genai"),
    "bedrock-converse-stream": ("boto3", "boto3"),
}


def sdk_installed(module: str) -> bool:
    """Whether ``module`` could be imported, without importing it.

    Uncached on purpose: the overview asks a few dozen times in one process and never
    again, and the path finder's own directory cache already makes each answer cheap.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        # A namespace parent without the child (``google`` without ``google.genai``), or a
        # package whose own import machinery is broken: either way it cannot serve a call.
        return False


def missing_sdk_extras(runtime: ModelRuntime, provider: str) -> list[str]:
    """The extras to install before ``provider`` can serve any request, or ``[]``.

    A provider that offers several wire protocols (github-copilot, opencode) is usable as
    soon as one of them has its SDK, so only a provider with *no* usable api is reported.
    """
    requirements = {
        API_SDK_REQUIREMENTS.get(model.api)
        for model in runtime.getModels()
        if model.provider == provider
    }
    if not requirements or None in requirements:
        return []  # unknown provider, or an api that needs no SDK: claim nothing
    if any(sdk_installed(module) for module, _package in requirements):
        return []
    return sorted({EXTRAS[package] for _module, package in requirements})


def install_hint(extras: list[str]) -> str:
    return " or ".join(f"pip install 'misaka[{extra}]'" for extra in extras)


@dataclass(slots=True)
class AuthRuntime:
    credentials: AuthStorage | ReadOnlyAuthStorage
    runtime: ModelRuntime


async def create_auth_runtime(*, read_only: bool, refresh: bool = False) -> AuthRuntime:
    """``refresh`` fills the availability snapshot the status overviews read."""
    credentials = ReadOnlyAuthStorage(get_auth_path()) if read_only else AuthStorage.create()
    runtime = await createAuthCheckModelRuntime(credentials)
    if refresh:
        await runtime.refresh(ModelsRefreshOptions(allowNetwork=False))
    return AuthRuntime(credentials=credentials, runtime=runtime)


async def print_provider_overview() -> int:
    """One line per known provider; 0 when every one is ready, 1 otherwise."""
    auth = await create_auth_runtime(read_only=False, refresh=True)
    known = {model.provider for model in auth.runtime.getAvailableSnapshot()} | {
        credential.providerId for credential in await auth.runtime.listCredentials()
    }
    if not known:
        print(f"No provider credentials are configured. See {get_auth_path()}.")
        return 1
    bad = 0
    for target in sorted(known):
        status = auth.runtime.getProviderAuthStatus(target)
        ready = bool(status.configured or status.source)
        # "!" rather than "✗": the credential is fine, the install is not. Both count as not
        # ready, so the exit code still refuses to call this provider usable.
        missing = missing_sdk_extras(auth.runtime, target) if ready else []
        mark = "!" if missing else "✓" if ready else "✗"
        detail = status.source or "not configured"
        if status.label:
            detail += f" ({status.label})"
        if missing:
            detail += f"; SDK not installed -- {install_hint(missing)}"
        print(f"{mark} {target}  {detail}")
        if not ready or missing:
            bad += 1
    return 1 if bad else 0


__all__ = [
    "API_SDK_REQUIREMENTS",
    "AuthRuntime",
    "create_auth_runtime",
    "install_hint",
    "missing_sdk_extras",
    "print_provider_overview",
    "sdk_installed",
]

"""Script-safe authentication CLI, aligned with pi's auth commands."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Literal

from misaka.ai.auth.resolve import AuthResolutionOverrides
from misaka.ai.auth.types import (
    ApiKeyCredential,
    AuthCheck,
    AuthResult,
    OAuthCredential,
)
from misaka.ai.models_store import InMemoryModelsStore
from misaka.ai.types import Model
from misaka.config import get_agent_dir, get_auth_path
from misaka.core.auth_storage import (
    AuthStorage,
    AuthStorageBackend,
    AuthStorageCredentialStore,
    FileAuthStorageBackend,
)
from misaka.core.model_registry import ModelRegistry

type AuthCommandKind = Literal["check", "api_key", "bearer_token"]

_DEFAULT_BEARER_TOKEN_MIN_EXPIRY_MS = 30 * 60_000
_PRINT_TIMEOUT_SECONDS = 15

_AUTH_HELP = """Usage:
  misaka auth print-api-key [--provider <provider>] [--model <model>]
  misaka auth print-bearer-token [--provider <provider>] [--model <model>] [--min-expiry <duration>]
  misaka auth check [--provider <provider>] [--model <model>] [--json] [--credentials] [--no-refresh]

Auth commands require at least one of --provider or --model. Checks refresh expired OAuth credentials by default; --no-refresh prevents this. --credentials emits the credential, or includes it in JSON output.

MISAKA compatibility: `misaka auth`, `misaka auth check`, and `misaka auth check PROVIDER` keep the human-readable provider overview. Their existing `--show` option keeps appending resolved credentials to those human-readable lines.
"""


class AuthCommandError(Exception):
    """A user-facing auth command error with its process exit code."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(slots=True)
class AuthCommand:
    kind: AuthCommandKind
    provider: str | None = None
    model: str | None = None
    json: bool = False
    credentials: bool = False
    no_refresh: bool = False
    min_expiry_ms: int | None = None
    legacy: bool = False
    show: bool = False


@dataclass(slots=True)
class AuthCheckResult:
    status: Literal["ready", "not_ready", "invalid"]
    provider: str
    reason: (
        Literal[
            "provider_not_found",
            "credentials_not_configured",
            "credential_not_available",
            "invalid_state",
        ]
        | None
    ) = None
    auth_type: Literal["api_key", "oauth"] | None = None
    credential: str | None = None

    def as_json(self) -> str:
        value: dict[str, str] = {"status": self.status, "provider": self.provider}
        if self.reason is not None:
            value["reason"] = self.reason
        if self.auth_type is not None:
            value["authType"] = self.auth_type
        if self.credential is not None:
            value["credentials"] = self.credential
        return json.dumps(value, separators=(",", ":"))


@dataclass(slots=True)
class _AuthRuntime:
    storage: AuthStorage
    credentials: AuthStorageCredentialStore
    registry: ModelRegistry


@dataclass(slots=True)
class _AuthTarget:
    provider: str
    model: Model | None = None
    check: AuthCheck | None = None


class _ReadOnlyAuthStorageBackend(AuthStorageBackend):
    """One-shot safe auth.json snapshot; every mutation fails closed."""

    def __init__(self, auth_path: str) -> None:
        self._reader = FileAuthStorageBackend(auth_path)
        self._loaded = False
        self._content: str | None = None

    def _load(self) -> str | None:
        if self._loaded:
            return self._content
        try:
            self._content, _revision = self._reader._read_file()
        except FileNotFoundError:
            self._content = None
        self._loaded = True
        return self._content

    def withLock(self, fn):
        outcome = fn(self._load())
        if outcome.next is not None or outcome.publish is not None:
            raise RuntimeError("Read-only credential storage cannot modify auth.json")
        return outcome.result

    async def withLockAsync(self, fn, options=None):
        del fn, options
        raise RuntimeError("Read-only credential storage cannot modify auth.json")


def _parse_duration(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)(ms|s|m|h)", value)
    if match is None:
        raise AuthCommandError("--min-expiry must use a duration such as 30m or 1h", 1)
    try:
        # Reachable despite `[0-9]+`: `int()` refuses a decimal string longer than
        # `sys.get_int_max_str_digits()` (4300 by default), so `"1"*5000 + "m"` lands here
        # rather than becoming an absurd duration.
        amount = int(match.group(1))
    except ValueError as error:
        raise AuthCommandError(
            "--min-expiry must use a duration such as 30m or 1h", 1
        ) from error
    multiplier = {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000}[match.group(2)]
    return amount * multiplier


def parse_auth_command(args: list[str]) -> AuthCommand | None:
    """Parse the raw arguments after ``misaka auth``; ``None`` means help."""
    if not args:
        return AuthCommand(kind="check", legacy=True)
    if args[0] in {"help", "-h", "--help"}:
        return None

    command_name = args[0]
    if command_name == "check":
        kind: AuthCommandKind = "check"
        command_args = args[1:]
    elif command_name == "print-api-key":
        kind = "api_key"
        command_args = args[1:]
    elif command_name == "print-bearer-token":
        kind = "bearer_token"
        command_args = args[1:]
    elif command_name == "--show":
        kind = "check"
        command_args = args
    else:
        raise AuthCommandError(
            f'Unknown auth command "{command_name}". Use "misaka auth print-api-key", '
            '"misaka auth print-bearer-token", or "misaka auth check".',
            1,
        )

    if any(arg in {"-h", "--help"} for arg in command_args):
        return None

    provider: str | None = None
    model: str | None = None
    json_output = False
    credentials = False
    no_refresh = False
    show = False
    min_expiry_ms: int | None = None
    positionals: list[str] = []

    index = 0
    while index < len(command_args):
        arg = command_args[index]
        if arg in {"--provider", "--model", "--min-expiry"}:
            if index + 1 >= len(command_args) or (
                arg != "--min-expiry" and command_args[index + 1].startswith("-")
            ):
                raise AuthCommandError(f"{arg} requires a value", 1)
            value = command_args[index + 1]
            index += 2
            if arg == "--provider":
                provider = value
            elif arg == "--model":
                model = value
            elif kind != "bearer_token":
                raise AuthCommandError(
                    "--min-expiry is only supported by print-bearer-token", 1
                )
            else:
                min_expiry_ms = _parse_duration(value)
            continue
        if arg in {"--json", "--credentials", "--no-refresh"}:
            if kind != "check":
                raise AuthCommandError(f"{arg} is only supported by auth check", 1)
            json_output = json_output or arg == "--json"
            credentials = credentials or arg == "--credentials"
            no_refresh = no_refresh or arg == "--no-refresh"
            index += 1
            continue
        if arg == "--show":
            if kind != "check":
                raise AuthCommandError("--show is only supported by auth check", 1)
            show = True
            index += 1
            continue
        if arg.startswith("-"):
            raise AuthCommandError(
                f'Unknown option {arg} for "auth '
                f'{"check" if kind == "check" else "print-api-key" if kind == "api_key" else "print-bearer-token"}".',
                1,
            )
        positionals.append(arg)
        index += 1

    if kind != "check" and positionals:
        raise AuthCommandError("Auth commands only accept --provider and --model", 1)
    if len(positionals) > 1:
        raise AuthCommandError(
            "Auth checks accept at most one legacy provider argument", 2
        )
    if positionals:
        if provider is not None:
            raise AuthCommandError(
                "Use either the legacy provider argument or --provider, not both", 2
            )
        provider = positionals[0]

    provider = provider.strip() if provider is not None else None
    model = model.strip() if model is not None else None
    provider = provider or None
    model = model or None
    legacy = (
        kind == "check"
        and not json_output
        and not credentials
        and not no_refresh
        and model is None
        and (not args or command_name in {"check", "--show"})
        and "--provider" not in command_args
    )
    if not legacy and provider is None and model is None:
        message = (
            "Auth checks require --provider <provider> or --model <model>"
            if kind == "check"
            else "Credential printing requires --provider <provider> or --model <model>"
        )
        raise AuthCommandError(message, 2 if kind == "check" else 1)
    if show and not legacy:
        raise AuthCommandError(
            "--show is only supported by the legacy human-readable auth check", 2
        )

    return AuthCommand(
        kind=kind,
        provider=provider,
        model=model,
        json=json_output,
        credentials=credentials,
        no_refresh=no_refresh,
        min_expiry_ms=min_expiry_ms,
        legacy=legacy,
        show=show,
    )


def get_auth_credential(auth: AuthResult | None) -> str | None:
    if auth is None:
        return None
    if auth.auth.apiKey:
        return auth.auth.apiKey
    for name, value in (auth.auth.headers or {}).items():
        if name.lower() != "authorization" or not isinstance(value, str):
            continue
        match = re.fullmatch(r"Bearer\s+(.+)", value, flags=re.IGNORECASE)
        if match is not None:
            return match.group(1)
    return None


def _create_runtime(*, read_only: bool) -> _AuthRuntime:
    storage = (
        AuthStorage.fromStorage(_ReadOnlyAuthStorageBackend(get_auth_path()))
        if read_only
        else AuthStorage.create()
    )
    registry = ModelRegistry(
        storage,
        os.path.join(get_agent_dir(), "models.json"),
        InMemoryModelsStore(),
    )
    return _AuthRuntime(
        storage=storage,
        credentials=AuthStorageCredentialStore(storage),
        registry=registry,
    )


def _validate_storage(storage: AuthStorage) -> None:
    """Raises ``AuthCommandError`` so the reason survives.

    ``_check_auth`` folds anything raised here into a structured ``invalid`` result, but
    ``print-api-key`` / ``print-bearer-token`` replace every non-``AuthCommandError`` with
    the generic "Failed to resolve credential"; raising the specific type is what puts the
    offending provider's name in front of the user on that path too.
    """
    # Deliberately *not* an AuthCommandError: a parse error quotes the offending bytes of
    # auth.json, and `print-api-key` prints an AuthCommandError verbatim. Same for the
    # pydantic validations below. Only the messages this function writes itself -- which
    # name a provider and nothing else -- are safe to surface.
    if storage.loadError is not None:
        raise storage.loadError
    if not isinstance(storage.data, dict):
        raise AuthCommandError("Invalid auth.json: expected an object", 1)
    for provider, value in storage.data.items():
        if not isinstance(provider, str) or not isinstance(value, dict):
            raise AuthCommandError(f'Invalid auth.json credential for provider "{provider}"', 1)
        if value.get("type") == "api_key":
            ApiKeyCredential.model_validate(value)
        elif value.get("type") == "oauth":
            expires = value.get("expires")
            if isinstance(expires, bool) or not isinstance(expires, (int, float)):
                raise AuthCommandError(
                    f'Invalid auth.json credential for provider "{provider}"', 1
                )
            OAuthCredential.model_validate(value)
        else:
            raise AuthCommandError(f'Invalid auth.json credential for provider "{provider}"', 1)


def _model_candidates(
    registry: ModelRegistry, model_reference: str
) -> list[_AuthTarget]:
    reference = model_reference.lower()
    seen: set[tuple[str, str]] = set()
    matches: list[_AuthTarget] = []
    for model in registry.getAll():
        key = model.provider.lower(), model.id.lower()
        if key in seen:
            continue
        seen.add(key)
        if (
            model.id.lower() == reference
            or f"{model.provider}/{model.id}".lower() == reference
        ):
            matches.append(_AuthTarget(provider=model.provider, model=model))
    return matches


def _is_canonical_model(target: _AuthTarget, model_reference: str) -> bool:
    return (
        target.model is not None
        and f"{target.provider}/{target.model.id}".lower() == model_reference.lower()
    )


def _provider_model_candidates(
    registry: ModelRegistry, provider: str, model_reference: str
) -> list[_AuthTarget]:
    candidates = [
        target
        for target in _model_candidates(registry, model_reference)
        if target.provider == provider
    ]
    canonical = [
        target for target in candidates if _is_canonical_model(target, model_reference)
    ]
    return canonical or candidates


def _canonical_provider(registry: ModelRegistry, provider: str) -> str | None:
    provider_ids = {model.provider for model in registry.getAll()} | {
        item.id for item in registry.getNativeProviders()
    }
    lowered = provider.lower()
    return next(
        (item for item in sorted(provider_ids) if item.lower() == lowered), None
    )


def _resolve_explicit_target(
    registry: ModelRegistry, provider: str, model_reference: str | None
) -> _AuthTarget:
    canonical_provider = _canonical_provider(registry, provider)
    if canonical_provider is None:
        if model_reference is None:
            return _AuthTarget(provider=provider)
        raise AuthCommandError(
            f'Unknown provider "{provider}". Use --list-models to see available providers/models.',
            1,
        )
    if model_reference is None:
        return _AuthTarget(provider=canonical_provider)
    matches = _provider_model_candidates(registry, canonical_provider, model_reference)
    if len(matches) != 1:
        raise AuthCommandError(
            f'Model "{model_reference}" not found for provider "{canonical_provider}". '
            "Use --list-models to see available models.",
            1,
        )
    return matches[0]


async def _resolve_check_target(
    command: AuthCommand, registry: ModelRegistry
) -> _AuthTarget:
    if command.provider is not None:
        return _resolve_explicit_target(registry, command.provider, command.model)
    candidates = _model_candidates(registry, command.model or "")
    if not candidates:
        raise AuthCommandError(
            f'Model "{command.model}" not found. Use --list-models to see available models.',
            2,
        )
    if len(candidates) == 1:
        return candidates[0]

    canonical = [
        target
        for target in candidates
        if _is_canonical_model(target, command.model or "")
    ]
    if len(canonical) > 1:
        raise AuthCommandError(
            f'Model "{command.model}" has multiple canonical matches. Specify --provider.',
            2,
        )
    if canonical:
        check = await registry.checkProviderAuth(canonical[0].provider)
        if check is not None:
            canonical[0].check = check
            return canonical[0]
        configured_raw: list[_AuthTarget] = []
        for target in candidates:
            if target is canonical[0]:
                continue
            check = await registry.checkProviderAuth(target.provider)
            if check is not None:
                target.check = check
                configured_raw.append(target)
        return configured_raw[0] if len(configured_raw) == 1 else canonical[0]

    configured: list[_AuthTarget] = []
    for target in candidates:
        check = await registry.checkProviderAuth(target.provider)
        if check is not None:
            target.check = check
            configured.append(target)
    if len(configured) == 1:
        return configured[0]
    providers = ", ".join(target.provider for target in candidates)
    raise AuthCommandError(
        f'Model "{command.model}" matches multiple providers ({providers}). Specify --provider.',
        2,
    )


async def _resolve_auth(
    runtime: _AuthRuntime,
    target: _AuthTarget,
    overrides: AuthResolutionOverrides | None = None,
) -> AuthResult | None:
    if target.model is not None:
        return await runtime.registry.getAuth(target.model, overrides)
    return await runtime.registry.getProviderAuth(target.provider, overrides)


def _invalid_result(
    command: AuthCommand, provider: str | None = None
) -> AuthCheckResult:
    return AuthCheckResult(
        status="invalid",
        provider=provider or command.provider or command.model or "unknown",
        reason="invalid_state",
    )


async def _check_auth(command: AuthCommand) -> AuthCheckResult:
    target: _AuthTarget | None = None
    try:
        runtime = _create_runtime(read_only=command.no_refresh)
        _validate_storage(runtime.storage)
        if runtime.registry.getError() is not None:
            return _invalid_result(command)
        target = await _resolve_check_target(command, runtime.registry)
        if not runtime.registry.hasProvider(target.provider):
            return AuthCheckResult(
                status="not_ready",
                provider=target.provider,
                reason="provider_not_found",
            )

        check = target.check or await runtime.registry.checkProviderAuth(
            target.provider
        )
        if check is None:
            return AuthCheckResult(
                status="not_ready",
                provider=target.provider,
                reason="credentials_not_configured",
            )

        auth: AuthResult | None = None
        if not command.no_refresh:
            auth = await runtime.registry.getProviderAuth(target.provider)
            if auth is None:
                return AuthCheckResult(
                    status="not_ready",
                    provider=target.provider,
                    reason="credentials_not_configured",
                )

        credential: str | None = None
        if command.credentials:
            if command.no_refresh and check.type == "oauth":
                stored = await runtime.credentials.read(target.provider)
                credential = (
                    stored.access if isinstance(stored, OAuthCredential) else None
                )
            else:
                auth = auth or await runtime.registry.getProviderAuth(target.provider)
                credential = get_auth_credential(auth)
            if not credential:
                return AuthCheckResult(
                    status="not_ready",
                    provider=target.provider,
                    reason="credential_not_available",
                )

        return AuthCheckResult(
            status="ready",
            provider=target.provider,
            auth_type=check.type,
            credential=credential,
        )
    except Exception:  # noqa: BLE001 - malformed/runtime auth is a structured invalid result
        return _invalid_result(command, target.provider if target is not None else None)


async def _print_credential(command: AuthCommand) -> str:
    runtime = _create_runtime(read_only=False)
    _validate_storage(runtime.storage)
    if runtime.registry.getError() is not None:
        raise AuthCommandError(runtime.registry.getError() or "Invalid model state", 1)

    credential_types = {
        info.providerId: info.type for info in await runtime.credentials.list()
    }
    if command.provider is not None:
        target = _resolve_explicit_target(
            runtime.registry, command.provider, command.model
        )
        if not runtime.registry.hasProvider(target.provider):
            raise AuthCommandError(
                f'Unknown provider "{command.provider}". Use --list-models to see available providers.',
                1,
            )
        targets = [target]
    else:
        targets: list[_AuthTarget] = []
        for provider in credential_types:
            matches = _provider_model_candidates(
                runtime.registry, provider, command.model or ""
            )
            if len(matches) > 1:
                raise AuthCommandError(
                    f'Model "{command.model}" has multiple matches for configured '
                    f'provider "{provider}".',
                    1,
                )
            targets.extend(matches)
        if not targets:
            raise AuthCommandError(
                f'Model "{command.model}" not found. Use --list-models to see available models.',
                1,
            )

    resolved: list[tuple[str, str]] = []
    for target in targets:
        credential_type = credential_types.get(target.provider)
        if command.kind == "api_key" and credential_type == "oauth":
            continue
        if command.kind == "bearer_token" and credential_type != "oauth":
            continue
        overrides = (
            AuthResolutionOverrides(
                minOAuthValidityMs=(
                    command.min_expiry_ms
                    if command.min_expiry_ms is not None
                    else _DEFAULT_BEARER_TOKEN_MIN_EXPIRY_MS
                )
            )
            if command.kind == "bearer_token"
            else None
        )
        value = get_auth_credential(await _resolve_auth(runtime, target, overrides))
        if value is not None:
            resolved.append((target.provider, value))

    if len(resolved) == 1:
        return resolved[0][1]
    if not resolved:
        provider = targets[0].provider if targets else command.provider
        credential_type = credential_types.get(provider or "")
        if (
            command.provider is not None
            and command.kind == "api_key"
            and credential_type == "oauth"
        ):
            raise AuthCommandError(
                f'Provider "{provider}" is configured with OAuth, not an API key', 1
            )
        if (
            command.provider is not None
            and command.kind == "bearer_token"
            and credential_type != "oauth"
        ):
            raise AuthCommandError(
                f'Provider "{provider}" is not configured with an OAuth bearer token',
                1,
            )
        kind = "API key" if command.kind == "api_key" else "OAuth bearer token"
        raise AuthCommandError(f"No usable {kind} is configured", 1)
    providers = ", ".join(provider for provider, _value in resolved)
    raise AuthCommandError(
        f"Multiple configured providers matched ({providers}). Specify --provider.", 1
    )


def _run_legacy_check(provider: str | None, *, show: bool) -> int:
    runtime = _create_runtime(read_only=False)
    known = {model.provider for model in runtime.registry.getAvailable()} | set(
        runtime.storage.getAll()
    )
    targets = [provider] if provider else sorted(known)
    if not targets:
        print(f"No provider credentials are configured. See {get_auth_path()}.")
        return 1
    bad = 0
    for target in targets:
        status = runtime.registry.getProviderAuthStatus(target)
        ready = bool(status.configured or status.source)
        mark = "✓" if ready else "✗"
        detail = status.source or "not configured"
        if status.label:
            detail += f" ({status.label})"
        line = f"{mark} {target}  {detail}"
        if show and ready:
            key = asyncio.run(runtime.registry.getApiKeyForProvider(target))
            line += f"  {key}" if key else " (credential could not be resolved)"
        print(line)
        if not ready:
            bad += 1
    return 1 if bad else 0


def _write_auth_check(result: AuthCheckResult, *, json_output: bool) -> int:
    print(
        result.as_json()
        if json_output
        else result.credential
        if result.credential is not None
        else result.status
    )
    return 0 if result.status == "ready" else 1 if result.status == "not_ready" else 2


def run_auth_command(args: list[str]) -> None:
    """Run ``misaka auth`` and terminate with the command's documented status."""
    try:
        command = parse_auth_command(args)
        if command is None:
            print(_AUTH_HELP, end="")
            raise SystemExit(0)
        if command.legacy:
            raise SystemExit(_run_legacy_check(command.provider, show=command.show))
        if command.kind == "check":
            result = asyncio.run(_check_auth(command))
            raise SystemExit(_write_auth_check(result, json_output=command.json))

        async def timed_print() -> str:
            async with asyncio.timeout(_PRINT_TIMEOUT_SECONDS):
                return await _print_credential(command)

        try:
            credential = asyncio.run(timed_print())
        except TimeoutError as error:
            raise AuthCommandError(
                f"Credential resolution timed out after {_PRINT_TIMEOUT_SECONDS} seconds",
                1,
            ) from error
        except AuthCommandError:
            raise
        except Exception as error:
            raise AuthCommandError("Failed to resolve credential", 1) from error
        sys.stdout.write(f"{credential}\n")
        raise SystemExit(0)
    except AuthCommandError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(error.exit_code) from None


__all__ = [
    "AuthCheckResult",
    "AuthCommand",
    "AuthCommandError",
    "get_auth_credential",
    "parse_auth_command",
    "run_auth_command",
]

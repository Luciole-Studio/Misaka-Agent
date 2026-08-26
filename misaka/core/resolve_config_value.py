"""Resolve configuration values from literals, environment, or shell commands."""

from __future__ import annotations

import errno
import os
import subprocess

from misaka.utils.shell import get_shell_config

_command_result_cache: dict[str, str | None] = {}


def resolve_config_value(config: str) -> str | None:
    if config.startswith("!"):
        return _execute_command(config)
    return os.environ.get(config) or config


def _execute_with_configured_shell(command: str) -> tuple[bool, str | None]:
    try:
        shell_config = get_shell_config()
        result = subprocess.run(
            [shell_config.shell, *shell_config.args, command],
            check=False,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            shell=False,
        )
    except FileNotFoundError:
        return False, None
    except OSError as error:
        if error.errno == errno.ENOENT:
            return False, None
        return True, None
    except subprocess.TimeoutExpired:
        return True, None
    except Exception:
        return False, None

    if result.returncode != 0:
        return True, None
    value = (result.stdout or "").strip()
    return True, value or None


def _execute_with_default_shell(command: str) -> str | None:
    try:
        result = subprocess.run(
            command,
            check=False,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            shell=True,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def _execute_command_uncached(command_config: str) -> str | None:
    command = command_config[1:]
    if os.name == "nt":
        executed, value = _execute_with_configured_shell(command)
        return value if executed else _execute_with_default_shell(command)
    return _execute_with_default_shell(command)


def _execute_command(command_config: str) -> str | None:
    if command_config in _command_result_cache:
        return _command_result_cache[command_config]

    result = _execute_command_uncached(command_config)
    _command_result_cache[command_config] = result
    return result


def resolve_config_value_uncached(config: str) -> str | None:
    if config.startswith("!"):
        return _execute_command_uncached(config)
    return os.environ.get(config) or config


def resolve_config_value_or_throw(config: str, description: str) -> str:
    resolved = resolve_config_value_uncached(config)
    if resolved is not None:
        return resolved

    if config.startswith("!"):
        raise Exception(f"Failed to resolve {description} from shell command: {config[1:]}")
    raise Exception(f"Failed to resolve {description}")


def resolve_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
    if not headers:
        return None
    resolved: dict[str, str] = {}
    for key, value in headers.items():
        resolved_value = resolve_config_value(value)
        if resolved_value:
            resolved[key] = resolved_value
    return resolved or None


def resolve_headers_or_throw(
    headers: dict[str, str] | None,
    description: str,
) -> dict[str, str] | None:
    if not headers:
        return None
    resolved = {
        key: resolve_config_value_or_throw(value, f'{description} header "{key}"')
        for key, value in headers.items()
    }
    return resolved or None


def clear_config_value_cache() -> None:
    _command_result_cache.clear()


resolveConfigValue = resolve_config_value
resolveConfigValueUncached = resolve_config_value_uncached
resolveConfigValueOrThrow = resolve_config_value_or_throw
resolveHeadersOrThrow = resolve_headers_or_throw
clearConfigValueCache = clear_config_value_cache

__all__ = [
    "clearConfigValueCache",
    "resolveConfigValue",
    "resolveConfigValueOrThrow",
    "resolveConfigValueUncached",
    "resolveHeadersOrThrow",
]

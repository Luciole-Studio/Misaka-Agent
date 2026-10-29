"""Resolve configuration values from literals, environment, or shell commands."""

from __future__ import annotations

import errno
import os
import re
import subprocess
from collections.abc import Mapping

from misaka.utils.shell import getShellConfig, normalize_command_for_stdin

_command_result_cache: dict[str, str | None] = {}

_ENV_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ENV_VAR_NAME_PREFIX_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# A parsed config value is either a shell command or a template: a sequence of
# ("literal", text) and ("env", name) parts.
type _TemplatePart = tuple[str, str]


def appendLiteral(parts: list[_TemplatePart], value: str) -> None:
    if not value:
        return
    if parts and parts[-1][0] == "literal":
        parts[-1] = ("literal", parts[-1][1] + value)
        return
    parts.append(("literal", value))


def parseConfigValueTemplate(config: str) -> list[_TemplatePart]:
    parts: list[_TemplatePart] = []
    index = 0
    length = len(config)

    while index < length:
        dollar_index = config.find("$", index)
        if dollar_index < 0:
            appendLiteral(parts, config[index:])
            break

        appendLiteral(parts, config[index:dollar_index])
        next_char = config[dollar_index + 1] if dollar_index + 1 < length else None

        if next_char in ("$", "!"):
            appendLiteral(parts, next_char)
            index = dollar_index + 2
            continue

        if next_char == "{":
            end_index = config.find("}", dollar_index + 2)
            if end_index < 0:
                appendLiteral(parts, "$")
                index = dollar_index + 1
                continue
            name = config[dollar_index + 2 : end_index]
            if _ENV_VAR_NAME_RE.match(name):
                parts.append(("env", name))
            else:
                appendLiteral(parts, config[dollar_index : end_index + 1])
            index = end_index + 1
            continue

        match = _ENV_VAR_NAME_PREFIX_RE.match(config, dollar_index + 1)
        if match:
            parts.append(("env", match.group(0)))
            index = dollar_index + 1 + len(match.group(0))
            continue

        appendLiteral(parts, "$")
        index = dollar_index + 1

    return parts


def _is_command(config: str) -> bool:
    return config.startswith("!")


def resolveEnvConfigValue(name: str, env: Mapping[str, str] | None = None) -> str | None:
    if env:
        scoped = env.get(name)
        if scoped:
            return scoped
    return os.environ.get(name) or None


def _template_env_var_names(parts: list[_TemplatePart]) -> list[str]:
    names: list[str] = []
    for kind, value in parts:
        if kind != "env" or value in names:
            continue
        names.append(value)
    return names


def resolveTemplate(parts: list[_TemplatePart], env: Mapping[str, str] | None = None) -> str | None:
    resolved = ""
    for kind, value in parts:
        if kind == "literal":
            resolved += value
            continue
        env_value = resolveEnvConfigValue(value, env)
        if env_value is None:
            return None
        resolved += env_value
    return resolved


def getConfigValueEnvVarName(config: str) -> str | None:
    """The single environment variable a config value is made of, if that is all it is."""
    if _is_command(config):
        return None
    parts = parseConfigValueTemplate(config)
    if len(parts) == 1 and parts[0][0] == "env":
        return parts[0][1]
    return None


def getConfigValueEnvVarNames(config: str) -> list[str]:
    """Every environment variable referenced by a config value, in order, deduplicated."""
    if _is_command(config):
        return []
    return _template_env_var_names(parseConfigValueTemplate(config))


def getMissingConfigValueEnvVarNames(config: str, env: Mapping[str, str] | None = None) -> list[str]:
    """The referenced environment variables that resolve to nothing."""
    return [
        name for name in getConfigValueEnvVarNames(config) if resolveEnvConfigValue(name, env) is None
    ]


def isCommandConfigValue(config: str) -> bool:
    """Whether a config value runs a shell command rather than expanding a template."""
    return _is_command(config)


def isConfigValueConfigured(config: str, env: Mapping[str, str] | None = None) -> bool:
    """Whether every environment variable a config value references is set."""
    return not getMissingConfigValueEnvVarNames(config, env)


def clearConfigValueCache() -> None:
    """Clear the config value command cache. Exported for testing."""
    _command_result_cache.clear()


def resolveConfigValue(config: str, env: Mapping[str, str] | None = None) -> str | None:
    """Resolve a config value (API key, header value, ...) to an actual value.

    - A leading ``!`` runs the rest as a shell command and uses its stdout (cached).
    - ``$ENV_VAR`` and ``${ENV_VAR}`` references interpolate the named environment
      variable; any number of them may appear in one value.
    - In non-command values, ``$$`` escapes a literal ``$`` and ``$!`` a literal ``!``.
    - Anything else is a literal.

    Returns ``None`` when a referenced environment variable is unset, so a
    misconfigured value is reported as such instead of being sent upstream verbatim.
    """
    if _is_command(config):
        return executeCommand(config)
    return resolveTemplate(parseConfigValueTemplate(config), env)


def executeWithConfiguredShell(command: str) -> tuple[bool, str | None]:
    try:
        shell_config = getShellConfig()
        command_from_stdin = shell_config.commandTransport == "stdin"
        result = subprocess.run(
            (
                [shell_config.shell, *shell_config.args]
                if command_from_stdin
                else [shell_config.shell, *shell_config.args, command]
            ),
            check=False,
            input=(
                normalize_command_for_stdin(command).encode("utf-8")
                if command_from_stdin
                else None
            ),
            stdin=None if command_from_stdin else subprocess.DEVNULL,
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
    except Exception:  # noqa: BLE001 - any failure to probe the command means 'not available'
        return False, None

    if result.returncode != 0:
        return True, None
    value = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    return True, value or None


def executeWithDefaultShell(command: str) -> str | None:
    try:
        result = subprocess.run(
            command,
            check=False,
            encoding="utf-8",
            errors="replace",  # pi's execSync decodes with Node's lenient UTF-8
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            shell=True,
        )
    except Exception:  # noqa: BLE001 - any failure to run the resolver command yields no value
        return None

    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def executeCommandUncached(command_config: str) -> str | None:
    command = command_config[1:]
    if os.name == "nt":
        executed, value = executeWithConfiguredShell(command)
        return value if executed else executeWithDefaultShell(command)
    return executeWithDefaultShell(command)


def executeCommand(command_config: str) -> str | None:
    if command_config in _command_result_cache:
        return _command_result_cache[command_config]

    result = executeCommandUncached(command_config)
    _command_result_cache[command_config] = result
    return result


def resolveConfigValueUncached(config: str, env: Mapping[str, str] | None = None) -> str | None:
    if _is_command(config):
        return executeCommandUncached(config)
    return resolveTemplate(parseConfigValueTemplate(config), env)


def resolveConfigValueOrThrow(config: str, description: str, env: Mapping[str, str] | None = None) -> str:
    resolved = resolveConfigValueUncached(config, env)
    if resolved is not None:
        return resolved

    if _is_command(config):
        raise RuntimeError(f"Failed to resolve {description} from shell command: {config[1:]}")

    missing = getMissingConfigValueEnvVarNames(config, env)
    if len(missing) == 1:
        raise RuntimeError(f"Failed to resolve {description} from environment variable: {missing[0]}")
    if len(missing) > 1:
        raise RuntimeError(f"Failed to resolve {description} from environment variables: {', '.join(missing)}")
    raise RuntimeError(f"Failed to resolve {description}")


def resolveHeadersOrThrow(
    headers: dict[str, str] | None,
    description: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    if not headers:
        return None
    resolved = {
        key: resolveConfigValueOrThrow(value, f'{description} header "{key}"', env)
        for key, value in headers.items()
    }
    return resolved or None


__all__ = [
    "clearConfigValueCache",
    "getConfigValueEnvVarName",
    "getConfigValueEnvVarNames",
    "getMissingConfigValueEnvVarNames",
    "isCommandConfigValue",
    "isConfigValueConfigured",
    "resolveConfigValue",
    "resolveConfigValueOrThrow",
    "resolveConfigValueUncached",
    "resolveHeadersOrThrow",
]

"""Translated from pi's ``packages/coding-agent/src/cli/auth-command.ts`` (v1.1.0).

MISAKA fork: the help text names the command ``misaka`` (``APP_NAME``) where pi's is written as ``pi``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from misaka.ai.auth.types import AuthResult
from misaka.cli.args import Args
from misaka.config import APP_NAME
from misaka.utils.js import JS_WHITESPACE_CLASS, jsTrim

type AuthCommandKind = Literal["check", "api_key", "bearer_token"]


@dataclass(slots=True)
class AuthCommand:
    kind: AuthCommandKind
    args: list[str]
    json: bool
    credentials: bool
    noRefresh: bool
    minExpiryMs: int | None = None


class AuthCommandError(Exception):
    pass


AUTH_COMMAND_USAGE: dict[AuthCommandKind, str] = {
    "check": f"{APP_NAME} auth check --provider <provider> [--json] [--credentials] [--no-refresh]",
    "api_key": f"{APP_NAME} auth print-api-key --provider <provider> [--model <model>]",
    "bearer_token": f"{APP_NAME} auth print-bearer-token --provider <provider> [--model <model>] [--min-expiry <duration>]",
}


def getAuthCommandName(kind: AuthCommandKind) -> str:
    return "auth check" if kind == "check" else "auth print-api-key" if kind == "api_key" else "auth print-bearer-token"


def getAuthCommandUsage(kind: AuthCommandKind) -> str:
    return AUTH_COMMAND_USAGE[kind]


def isAuthCommandHelp(args: list[str]) -> bool:
    return bool(args) and args[0] == "auth" and (
        len(args) < 2 or args[1] == "help" or "--help" in args or "-h" in args
    )


def printAuthCommandHelp() -> None:
    print(f"""Usage:
  {APP_NAME} auth print-api-key [--provider <provider>] [--model <model>]
  {APP_NAME} auth print-bearer-token [--provider <provider>] [--model <model>] [--min-expiry <duration>]
  {APP_NAME} auth check [--provider <provider>] [--model <model>] [--json] [--credentials] [--no-refresh]

Auth commands require at least one of --provider or --model. Checks refresh expired OAuth credentials by default; --no-refresh prevents this. --credentials emits the credential, or includes it in JSON output.""")


_DURATION = re.compile(r"([0-9]+)(ms|s|m|h)", re.IGNORECASE)


def parseAuthCommand(args: list[str]) -> AuthCommand | None:
    if not args or args[0] != "auth":
        return None

    sub = args[1] if len(args) > 1 else None
    kind: AuthCommandKind | None = (
        "check" if sub == "check" else "api_key" if sub == "print-api-key" else "bearer_token" if sub == "print-bearer-token" else None
    )
    if not kind:
        raise AuthCommandError(
            f'Unknown auth command "{sub if sub is not None else ""}". Use "{APP_NAME} auth print-api-key", "{APP_NAME} auth print-bearer-token", or "{APP_NAME} auth check".'
        )

    commandArgs: list[str] = []
    json = False
    credentials = False
    noRefresh = False
    minExpiryMs: int | None = None
    index = 2
    while index < len(args):
        arg = args[index]
        if arg == "--min-expiry":
            if kind != "bearer_token":
                raise AuthCommandError("--min-expiry is only supported by print-bearer-token")
            index += 1
            value = args[index] if index < len(args) else None
            match = _DURATION.fullmatch(value) if value else None
            if not match:
                raise AuthCommandError("--min-expiry must use a duration such as 30m or 1h")
            amount = int(match[1])
            unit = match[2]
            minExpiryMs = amount * (1 if unit == "ms" else 1_000 if unit == "s" else 60_000 if unit == "m" else 3_600_000)
            index += 1
            continue
        if arg == "--json" or arg == "--credentials" or arg == "--no-refresh":
            if kind != "check":
                raise AuthCommandError(f"{arg} is only supported by auth check")
            if arg == "--json":
                json = True
            elif arg == "--credentials":
                credentials = True
            else:
                noRefresh = True
            index += 1
            continue
        commandArgs.append(arg)
        index += 1

    return AuthCommand(kind=kind, args=commandArgs, json=json, credentials=credentials, noRefresh=noRefresh, minExpiryMs=minExpiryMs)


def validateAuthCommandArgs(args: Args, kind: AuthCommandKind) -> dict[str, str | None]:
    provider = (jsTrim(args.provider) if args.provider is not None else "") or None
    model = (jsTrim(args.model) if args.model is not None else "") or None
    if len(args.unknownFlags) > 0:
        option = next(iter(args.unknownFlags))
        raise AuthCommandError(f'Unknown option --{option} for "{getAuthCommandName(kind)}".')
    if args.apiKey is not None or len(args.messages) > 0 or len(args.fileArgs) > 0:
        raise AuthCommandError("Auth commands only accept --provider and --model")
    if kind == "check":
        if not provider and not model:
            raise AuthCommandError("Auth checks require --provider <provider> or --model <model>")
        return {"provider": provider, "model": model}
    if not provider and not model:
        raise AuthCommandError("Credential printing requires --provider <provider> or --model <model>")
    return {"provider": provider, "model": model}


_BEARER = re.compile(rf"Bearer[{JS_WHITESPACE_CLASS}]+([^\n\r  ]+)", re.IGNORECASE)


def getAuthCredential(auth: AuthResult | None) -> str | None:
    if auth is not None and auth.auth.apiKey:
        return auth.auth.apiKey
    headers = (auth.auth.headers if auth is not None else None) or {}
    authorization = next((value for name, value in headers.items() if name.lower() == "authorization"), None)
    if not isinstance(authorization, str):
        return None
    match = _BEARER.fullmatch(authorization)
    return match[1] if match else None


__all__ = [
    "AuthCommand",
    "AuthCommandError",
    "AuthCommandKind",
    "getAuthCommandName",
    "getAuthCommandUsage",
    "getAuthCredential",
    "isAuthCommandHelp",
    "parseAuthCommand",
    "printAuthCommandHelp",
    "validateAuthCommandArgs",
]

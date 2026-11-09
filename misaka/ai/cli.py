"""Translated from pi's ``packages/ai/src/cli.ts`` (v1.1.0): a command-line interface for OAuth provider login.

MISAKA fork: credentials go to misaka's credential store, not ``./auth.json``; ``run()`` is the entry
(``python -m misaka.ai``), because a Python module must not run on import.
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import uuid
from typing import Any

from misaka.ai.auth.types import LoginOptions
from misaka.ai.providers.all import builtinProviders
from misaka.config import get_auth_path
from misaka.core.auth_storage import (
    AuthStorage,  # the one credential store: 0600, locked, never clobbers
)
from misaka.core.provider_attribution import CLIENT_NAME
from misaka.utils.js import AbortController

_PROVIDERS = [provider for provider in builtinProviders() if provider.auth.oauth is not None]


def _prompt(question: str) -> asyncio.Future[str]:
    """``rl.question``.

    MISAKA fork: answered from a daemon thread. A browser sign-in races the loopback callback
    against a paste prompt, and a question the callback made moot is never answered;
    ``input()`` cannot be interrupted, and a thread from ``asyncio.to_thread`` left in it
    would keep ``asyncio.run`` from returning. A daemon thread ends with the process, as
    upstream's question ends with ``rl.close()``.
    """
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[str] = loop.create_future()

    def settle(value: str | None, error: BaseException | None) -> None:
        if answer.done():
            return
        if error is not None:
            answer.set_exception(error)
        else:
            answer.set_result(value or "")

    def read() -> None:
        try:
            value = input(question)
        except BaseException as error:  # noqa: BLE001 - handed to the awaiting coroutine
            result: tuple[str | None, BaseException | None] = (None, error)
        else:
            result = (value, None)
        # The loop is gone once the login no longer waits for this answer.
        try:
            loop.call_soon_threadsafe(settle, *result)
        except RuntimeError:
            pass

    threading.Thread(target=read, daemon=True).start()
    return answer


def _parseInt(value: str) -> int | None:
    """``Number.parseInt(value, 10)``, with ``None`` for ``NaN``."""
    match = re.match(r"\s*([+-]?\d+)", value)
    return int(match.group(1)) if match else None


async def _answerPrompt(authPrompt: Any) -> str:
    if authPrompt.type == "select":
        print(f"\n{authPrompt.message}")
        for index in range(len(authPrompt.options)):
            print(f"  {index + 1}. {authPrompt.options[index].label}")
        number = _parseInt(await _prompt(f"Enter number (1-{len(authPrompt.options)}): "))
        choice = number - 1 if number is not None else -1
        selected = authPrompt.options[choice] if 0 <= choice < len(authPrompt.options) else None
        if not selected:
            raise RuntimeError("Invalid selection")
        return selected.id
    return await _prompt(f"{authPrompt.message}{f' ({authPrompt.placeholder})' if authPrompt.placeholder else ''}: ")


def _notify(event: Any) -> None:
    if event.type == "auth_url":
        print(f"\nOpen this URL in your browser:\n{event.url}")
        if event.instructions:
            print(event.instructions)
    elif event.type == "device_code":
        print(f"\nOpen this URL in your browser:\n{event.verificationUri}")
        print(f"Enter code: {event.userCode}")
    elif event.type in ("info", "progress"):
        print(event.message)


class _Interaction:
    def __init__(self) -> None:
        self.signal = AbortController().signal

    def prompt(self, authPrompt: Any) -> Any:
        return _answerPrompt(authPrompt)

    def notify(self, event: Any) -> None:
        _notify(event)


async def _login(providerId: str) -> None:
    provider = next((entry for entry in _PROVIDERS if entry.id == providerId), None)
    if provider is None or provider.auth.oauth is None:
        raise RuntimeError(f"Unknown provider: {providerId}")
    # This dev CLI does not persist an installation ID; apps should reuse one across logins.
    # MISAKA fork: misaka introduces itself by its own name.
    credential = await provider.auth.oauth.login(
        _Interaction(), LoginOptions(getDeviceId=lambda: str(uuid.uuid4()), agentName=CLIENT_NAME)
    )
    # MISAKA fork: stored in misaka's credential store, not in `auth.json` under the
    # working directory.
    storage = AuthStorage.create()

    async def store(_current: Any) -> Any:
        return credential

    await storage.modify(providerId, store)
    print(f"\nCredentials saved to {get_auth_path()}")


async def _main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else None
    if not command or command in ("help", "--help", "-h"):
        providerList = "\n".join(f"  {provider.id.ljust(20)} {provider.name}" for provider in _PROVIDERS)
        print(
            "Usage: python -m misaka.ai <command> [provider]\n\nCommands:\n  login [provider]  Login to an OAuth "
            f"provider\n  list              List available providers\n\nProviders:\n{providerList}"
        )
        return
    if command == "list":
        for provider in _PROVIDERS:
            print(f"{provider.id.ljust(20)} {provider.name}")
        return
    if command == "login":
        providerId = args[1] if len(args) > 1 else None
        if not providerId:
            for index in range(len(_PROVIDERS)):
                print(f"  {index + 1}. {_PROVIDERS[index].name}")
            number = _parseInt(await _prompt(f"Enter number (1-{len(_PROVIDERS)}): "))
            index = number - 1 if number is not None else -1
            providerId = _PROVIDERS[index].id if 0 <= index < len(_PROVIDERS) else None
        if not providerId or not any(provider.id == providerId for provider in _PROVIDERS):
            raise RuntimeError(f"Unknown provider: {providerId if providerId is not None else ''}")
        await _login(providerId)
        return
    raise RuntimeError(f"Unknown command: {command}")


def run() -> None:
    try:
        asyncio.run(_main())
    except Exception as error:
        print("Error:", str(error), file=sys.stderr)
        raise SystemExit(1) from error

"""Command-line interface for OAuth provider login."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from misaka.ai.utils.oauth import get_oauth_provider, get_oauth_providers

AUTH_FILE = Path("auth.json")
PROVIDERS = get_oauth_providers()


async def _prompt(question: str) -> str:
    return await asyncio.to_thread(input, question)


def load_auth() -> dict[str, dict[str, Any]]:
    if not AUTH_FILE.exists():
        return {}
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_auth(auth: dict[str, dict[str, Any]]) -> None:
    AUTH_FILE.write_text(json.dumps(auth, indent=2), encoding="utf-8")


async def login(provider_id: str) -> None:
    provider = get_oauth_provider(provider_id)
    if provider is None:
        print(f"Unknown provider: {provider_id}", file=sys.stderr)
        raise SystemExit(1)

    async def on_prompt(prompt: Any) -> str:
        placeholder = f" ({prompt.placeholder})" if getattr(prompt, "placeholder", None) else ""
        return await _prompt(f"{prompt.message}{placeholder}: ")

    async def on_select(prompt: Any) -> str | None:
        print(f"\n{prompt.message}")
        for index, option in enumerate(prompt.options, start=1):
            print(f"  {index}. {option.label}")
        choice = await _prompt(f"Enter number (1-{len(prompt.options)}): ")
        try:
            selected_index = int(choice, 10) - 1
        except ValueError:
            return None
        if 0 <= selected_index < len(prompt.options):
            return prompt.options[selected_index].id
        return None

    class _Callbacks:
        signal = None
        onProgress = staticmethod(print)
        onDeviceCode = staticmethod(
            lambda info: print(
                f"\nOpen this URL in your browser:\n{info.verificationUri}\n"
                f"Enter code: {info.userCode}\n"
            )
        )
        onAuth = staticmethod(
            lambda info: print(
                f"\nOpen this URL in your browser:\n{info.url}"
                + (f"\n{info.instructions}" if getattr(info, "instructions", None) else "")
                + "\n"
            )
        )
        onPrompt = staticmethod(on_prompt)
        onSelect = staticmethod(on_select)
        onManualCodeInput = None

    credentials = await provider.login(_Callbacks())
    auth = load_auth()
    auth[provider_id] = {"type": "oauth", **credentials.model_dump(mode="json")}
    save_auth(auth)
    print(f"\nCredentials saved to {AUTH_FILE}")


def _format_provider_list() -> str:
    return "\n".join(f"  {provider.id.ljust(20)} {provider.name}" for provider in PROVIDERS)


async def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else None

    if command is None or command in {"help", "--help", "-h"}:
        provider_list = _format_provider_list()
        print(
            "Usage: harn-ai <command> [provider]\n\n"
            "Commands:\n"
            "  login [provider]  Login to an OAuth provider\n"
            "  list              List available providers\n\n"
            "Providers:\n"
            f"{provider_list}\n\n"
            "Examples:\n"
            "  harn-ai login              # interactive provider selection\n"
            "  harn-ai login anthropic    # login to specific provider\n"
            "  harn-ai list               # list providers\n"
        )
        return

    if command == "list":
        print("Available OAuth providers:\n")
        for provider in PROVIDERS:
            print(f"  {provider.id.ljust(20)} {provider.name}")
        return

    if command == "login":
        provider_id = args[1] if len(args) > 1 else None

        if provider_id is None:
            print("Select a provider:\n")
            for index, provider in enumerate(PROVIDERS, start=1):
                print(f"  {index}. {provider.name}")
            print()

            choice = await _prompt(f"Enter number (1-{len(PROVIDERS)}): ")
            try:
                selected_index = int(choice, 10) - 1
            except ValueError:
                selected_index = -1

            if selected_index < 0 or selected_index >= len(PROVIDERS):
                print("Invalid selection", file=sys.stderr)
                raise SystemExit(1)
            provider_id = PROVIDERS[selected_index].id

        if not any(provider.id == provider_id for provider in PROVIDERS):
            print(f"Unknown provider: {provider_id}", file=sys.stderr)
            print("Use 'harn-ai list' to see available providers", file=sys.stderr)
            raise SystemExit(1)

        print(f"Logging in to {provider_id}...")
        await login(provider_id)
        return

    print(f"Unknown command: {command}", file=sys.stderr)
    print("Use 'harn-ai --help' for usage", file=sys.stderr)
    raise SystemExit(1)


def run() -> None:
    try:
        asyncio.run(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    run()

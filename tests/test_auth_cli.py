"""``misaka auth`` over the model runtime, after pi's ``auth-check.test.ts`` and
``credential-print.test.ts``; the command is ``runAuthCommand`` in ``misaka/cli/engine.py``."""

from __future__ import annotations

import json
import time

import pytest

from misaka.ai.auth.types import OAuthCredential
from misaka.cli import auth_check
from misaka.cli.args import parseArgs
from misaka.cli.auth_command import AuthCommandError, parseAuthCommand
from misaka.cli.credential_print import resolveCredentialForPrint
from misaka.cli.engine import runAuthCommand
from misaka.config import home
from misaka.core.auth_storage import AuthStorage


def _store(data) -> None:
    path = home.path("auth")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


async def _check(capsys, *args: str) -> tuple[int, dict]:
    code = await runAuthCommand(["auth", "check", *args, "--json"])
    return code, json.loads(capsys.readouterr().out)


def _hour_ms() -> int:
    return int(time.time() * 1000) + 60 * 60 * 1000


async def test_reports_a_configured_provider_as_ready(capsys):
    _store({"openai": {"type": "api_key", "key": "test-key"}})

    code, result = await _check(capsys, "--provider", "openai")

    assert code == 0
    assert result == {"status": "ready", "provider": "openai", "authType": "api_key"}


async def test_resolves_the_provider_from_model(capsys):
    _store({"openai": {"type": "api_key", "key": "test-key"}})

    assert (await _check(capsys, "--model", "openai/gpt-4.1"))[1]["provider"] == "openai"
    assert (await _check(capsys, "--provider", "openai", "--model", "gpt-4.1"))[1]["status"] == "ready"


def _patch_refresh(monkeypatch, provider_id: str, refreshed: list, access: str | None = None):
    create = auth_check.createAuthCheckModelRuntime

    async def patched(credentials):
        runtime = await create(credentials)
        oauth = runtime.getProvider(provider_id).auth.oauth

        async def refresh(credential, _signal):
            refreshed.append(credential.access)
            return OAuthCredential(type="oauth", access=access or "fresh-token", refresh="refresh-token",
                                   expires=_hour_ms())

        oauth.refresh = refresh
        return runtime

    monkeypatch.setattr(auth_check, "createAuthCheckModelRuntime", patched)
    return patched


async def test_reads_credentials_without_refreshing_oauth_when_requested(monkeypatch, capsys):
    _store({"openai-codex": {"type": "oauth", "access": "old-token", "refresh": "refresh-token", "expires": 0}})
    refreshed: list = []
    _patch_refresh(monkeypatch, "openai-codex", refreshed)

    code, result = await _check(capsys, "--provider", "openai-codex", "--no-refresh", "--credentials")

    assert code == 0 and result["credentials"] == "old-token"
    assert refreshed == []


async def test_refreshes_oauth_by_default(monkeypatch, capsys):
    _store({"openai-codex": {"type": "oauth", "access": "old-token", "refresh": "refresh-token", "expires": 0}})
    refreshed: list = []
    _patch_refresh(monkeypatch, "openai-codex", refreshed)

    code, result = await _check(capsys, "--provider", "openai-codex")

    assert code == 0 and result["status"] == "ready"
    assert refreshed == ["old-token"]


async def test_reports_an_unknown_provider_as_not_ready(capsys):
    code, result = await _check(capsys, "--provider", "not-installed")

    assert code == 1
    assert result == {"status": "not_ready", "provider": "not-installed", "reason": "provider_not_found"}


async def test_does_not_treat_an_unresolved_stored_environment_reference_as_configured(monkeypatch, capsys):
    monkeypatch.delenv("MISSING_AUTH_CHECK_KEY", raising=False)
    _store({"openai": {"type": "api_key", "key": "$MISSING_AUTH_CHECK_KEY"}})

    _code, result = await _check(capsys, "--provider", "openai", "--no-refresh")

    assert (result["status"], result["reason"]) == ("not_ready", "credentials_not_configured")


async def test_reports_malformed_auth_state_as_invalid(capsys):
    _store("{invalid-json")

    code, result = await _check(capsys, "--provider", "openai", "--no-refresh")

    assert code == 2
    assert result == {"status": "invalid", "provider": "openai", "reason": "invalid_state"}


async def test_does_not_create_an_auth_file_or_its_parent_directory(capsys):
    _code, result = await _check(capsys, "--provider", "openai", "--no-refresh")

    assert (result["status"], result["reason"]) == ("not_ready", "credentials_not_configured")
    assert not home.path("auth").exists()
    assert not home.path("auth").parent.exists()


async def test_creates_an_auth_check_runtime_without_catalog_storage():
    runtime = await auth_check.createAuthCheckModelRuntime(AuthStorage.inMemory())

    assert runtime.getProvider("openai") is not None
    assert not home.path("models_store").exists()


async def test_prints_a_resolved_api_key(capsys):
    _store({"openai": {"type": "api_key", "key": "test-api-key"}})

    assert await runAuthCommand(["auth", "print-api-key", "--provider", "openai"]) == 0
    assert capsys.readouterr().out == "test-api-key\n"


async def test_resolves_model_without_provider_only_among_catalog_models_of_configured_providers(capsys):
    # pi 1.3 credential-print.test.ts. anthropic has no gpt-5.5; an uncatalogued id on a configured provider must
    # not count as a match.
    _store({"openai": {"type": "api_key", "key": "openai-test-key"},
            "anthropic": {"type": "api_key", "key": "anthropic-test-key"}})

    assert await runAuthCommand(["auth", "print-api-key", "--model", "gpt-5.5"]) == 0
    assert capsys.readouterr().out == "openai-test-key\n"


async def test_prints_bearer_tokens_resolved_from_an_authorization_header(capsys):
    _store({"kimi-coding": {
        "type": "oauth", "access": "header-test-token", "refresh": "test-refresh-token", "expires": _hour_ms(),
    }})

    assert await runAuthCommand(["auth", "print-bearer-token", "--provider", "kimi-coding"]) == 0
    assert capsys.readouterr().out == "header-test-token\n"


async def test_refreshes_an_expired_oauth_token_before_printing_it(monkeypatch):
    _store({"openai-codex": {"type": "oauth", "access": "old-test-token", "refresh": "test-refresh-token",
                             "expires": 0}})
    refreshed: list = []
    patched = _patch_refresh(monkeypatch, "openai-codex", refreshed, access="fresh-test-token")
    runtime = await patched(AuthStorage.create())

    credential = await resolveCredentialForPrint(parseArgs(["--provider", "openai-codex"]), runtime, "bearer_token")

    assert credential == "fresh-test-token"
    assert refreshed == ["old-test-token"]
    assert json.loads(home.path("auth").read_text(encoding="utf-8"))["openai-codex"]["access"] == "fresh-test-token"


async def test_an_oauth_provider_does_not_print_as_an_api_key(capsys):
    _store({"openai-codex": {"type": "oauth", "access": "test-token-not-to-be-printed",
                             "refresh": "test-refresh-token", "expires": _hour_ms()}})

    assert await runAuthCommand(["auth", "print-api-key", "--provider", "openai-codex"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "configured with OAuth, not an API key" in captured.err


def test_parse_rejects_flags_for_the_wrong_subcommand():
    with pytest.raises(AuthCommandError, match="only supported by print-bearer-token"):
        parseAuthCommand(["auth", "check", "--min-expiry", "1h"])
    with pytest.raises(AuthCommandError, match="only supported by auth check"):
        parseAuthCommand(["auth", "print-api-key", "--json"])
    assert parseAuthCommand(["auth", "print-bearer-token", "--min-expiry", "2H"]).minExpiryMs == 7_200_000


async def test_bare_auth_prints_help(capsys):
    assert await runAuthCommand(["auth"]) == 0
    assert capsys.readouterr().out.startswith("Usage:")


async def test_a_bare_check_prints_the_misaka_provider_overview(capsys):
    _store({"openai": {"type": "api_key", "key": "test-key"}})

    await runAuthCommand(["auth", "check"])

    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith(("✓ openai  stored", "! openai  stored")) for line in lines)

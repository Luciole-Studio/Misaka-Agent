"""Web settings use the same providers, profile config and extension trust as sessions."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import httpx

from misaka.config import get_agent_dir
from misaka.core.web import config, dispatch, registry
from misaka.core.web.scope import WebScope, current_scope


async def _discover(extension_paths):
    from misaka.cli.engine import create_project_trust_context
    from misaka.core.project_trust import ProjectTrustStore, resolve_project_trusted
    from misaka.core.resource_loader import DefaultResourceLoader

    cwd, agent_dir = os.getcwd(), get_agent_dir()
    loader = DefaultResourceLoader({"cwd": cwd, "agentDir": agent_dir,
                                    "additionalExtensionPaths": extension_paths,
                                    "noPromptTemplates": True, "noThemes": True, "noContextFiles": True})

    async def trust(payload):
        return await resolve_project_trusted({
            "cwd": cwd, "trustStore": ProjectTrustStore(agent_dir),
            "defaultProjectTrust": loader.settingsManager.getDefaultProjectTrust(),
            "extensionsResult": payload["extensionsResult"],
            "projectTrustContext": create_project_trust_context(
                cwd=cwd, mode="print", settings_manager=loader.settingsManager, has_ui=False),
            "onExtensionError": lambda message: print(message, file=sys.stderr),
        })

    try:
        await loader.reload({"resolveProjectTrust": trust})
        return loader.getExtensions()
    except BaseException:
        loader.getExtensions().runtime.invalidate()
        raise


def _provider_rows(provider):
    schema = provider.get_setup_schema()
    return [schema, *schema.get("variants", [])]


def _providers():
    for provider in registry.list_providers(include_disabled=True):
        owner = current_scope().owners.get(provider.name, "builtin")
        capabilities = "/".join(cap for cap in ("search", "extract") if getattr(provider, f"supports_{cap}")())
        state = "disabled" if config.provider_disabled(provider.name) else (
            "ready" if registry.provider_is_ready(provider) else "not ready")
        print(f"{provider.name}: {capabilities}  {state}  owner={owner}")
        for row in _provider_rows(provider):
            print(f"  {row.get('name', provider.display_name)}  [{row.get('badge', '')}]")
            if row.get("tag"):
                print(f"    {row['tag']}")
            if row.get("post_setup") == "ddgs":
                print("    Install explicitly: misaka web setup ddgs --install --yes")
            if row.get("post_setup") == "xai_grok":
                print("    OAuth login: misaka web setup xai --login --yes")


def _status():
    from misaka.core.web import debug, network
    from misaka.core.web.timeouts import (
        OPERATION_DEFAULTS,
        http_timeout,
        operation_seconds,
    )

    debug_enabled = debug.enabled()
    network_status = network.status()
    http_timeout("default", 60)  # Validate before reporting a ready configuration.
    limits = {name: operation_seconds(name) for name in OPERATION_DEFAULTS}
    for capability, resolve in (("Search", dispatch.resolve_provider), ("Extract", dispatch.resolve_extractor)):
        provider, backend, error = resolve()
        if provider is None:
            print(f"{capability} backend: {backend or 'none'}  — {error}")
            continue
        ready = "ready" if registry.provider_is_ready(provider) else "not ready"
        mode = "keyless ring" if dispatch.serves_keyless(provider) else "provider direct"
        print(f"{capability} backend: {backend}  ({ready}; {mode})")
    print(f"Keyless ring: {'on' if config.keyless_tier_enabled() else 'off'}"
          f"   rescue: {'on' if config.keyless_rescue_enabled() else 'off'}")
    print(f"Searchable now: {'yes' if registry.web_search_available() else 'no'}")
    print("HTTP timeout overrides (seconds; otherwise provider defaults): "
          + json.dumps(config.web_config().get("http_timeout", {}), sort_keys=True))
    print("Operation deadlines (seconds; cleanup is awaited): "
          + ", ".join(f"{name}={seconds:g}" for name, seconds in limits.items()))
    print(f"Web debug: {'on' if debug_enabled else 'off'}; directory: {debug.directory()}")
    print(network_status)
    from misaka.cli.web_browser import status as browser_status
    browser_status()
    backend, extract_backend = registry.search_backend_name(), registry.extract_backend_name()
    tiers = config.web_config().get("provider_tier")
    stale = [name for name in tiers if name not in {backend, extract_backend}] if isinstance(tiers, dict) else []
    if stale:
        print(f"Tier pins on other vendors: {', '.join(sorted(stale))}"
              "   (`misaka web unset provider_tier.<vendor>` to clear)")
    print("Credentials (presence only; process env overrides files, including empty exports):")
    for name, is_set, source in config.credential_status():
        print(f"  {'✓' if is_set else '·'} {name}" + (f"  ({source})" if source else ""))
    print(f"\nConfig file: {config._config_path()}")
    if current_scope().profile_dir is not None:
        print(f"Shared defaults: {os.path.expanduser(config.CFG['web_config'])}")


def _select(rows, prompt):
    for index, row in enumerate(rows, 1):
        print(f"  {index}. {row['name']}")
    choice = input(f"{prompt} [1]: ").strip() or "1"
    if not choice.isdecimal() or not 1 <= int(choice) <= len(rows):
        raise ValueError("Choose one of the listed numbers")
    return rows[int(choice) - 1]


def _post_setup(row, args):
    hook = row.get("post_setup")
    if args.install and hook != "ddgs":
        raise ValueError("--install applies to the ddgs setup row")
    if args.login and hook != "xai_grok":
        raise ValueError("--login applies to the xAI setup row")
    if hook not in {None, "ddgs", "xai_grok"}:
        raise ValueError(f"Unknown Web post_setup hook: {hook}")
    if args.install:
        # An explicit CLI action, never an availability probe or a first-tool-call install.
        uv = shutil.which("uv")
        command = [uv, "pip", "install", "--python", sys.executable, "ddgs"] if uv else [
            sys.executable, "-m", "pip", "install", "ddgs"]
        subprocess.run(command, check=True)
    if args.login:
        from misaka.core.auth_storage import AuthStorage
        from misaka.core.web.backends.xai import _auth_path

        def show_code(info):
            print(f"Open {info.verificationUri}\nDevice code: {info.userCode}")

        asyncio.run(AuthStorage.create(_auth_path()).login(
            "xai", SimpleNamespace(onDeviceCode=show_code, signal=None)))


def _setup(args):
    providers = registry.list_providers(include_disabled=True)
    name = args.key
    if name is None:
        if args.yes:
            raise ValueError("Name a provider with --yes: misaka web setup <provider> --yes")
        selected = _select([{"name": provider.name} for provider in providers], "Provider")
        name = selected["name"]
    provider = registry.get_provider(name, include_disabled=True)
    if provider is None:
        raise ValueError(f"Unknown Web provider: {name}; see `misaka web providers`")
    if config.provider_disabled(name):
        raise ValueError(f"Web provider '{name}' is disabled; run `misaka web enable {name}` first")
    rows = _provider_rows(provider)
    if args.tier is not None:
        row = next((row for row in rows if row.get("web_tier") == args.tier), None)
        if row is None and args.tier != "auto":
            raise ValueError(f"Provider '{name}' has no {args.tier} setup row")
        row = rows[-1] if row is None else row
    else:
        row = _select(rows, "Tier") if len(rows) > 1 and not args.yes else rows[0]
    capabilities = [cap for cap in ("search", "extract") if getattr(provider, f"supports_{cap}")()]
    selected = capabilities if args.capability is None else (
        ["search", "extract"] if args.capability == "both" else [args.capability])
    if not selected or any(cap not in capabilities for cap in selected):
        raise ValueError(f"Provider '{name}' supports only {', '.join(capabilities) or 'no capabilities'}")
    print(f"Setup: {row.get('name', name)}")
    if row.get("tag"):
        print(row["tag"])
    changes = {f"{cap}_backend": name for cap in selected}
    tier = args.tier if args.tier is not None else row.get("web_tier")
    if tier is not None:
        changes[f"provider_tier.{name}"] = tier
    if not args.yes and not args.login:
        for variable in row.get("env_vars", []):
            key = variable["key"]
            if variable.get("url"):
                print(f"{key}: {variable['url']}")
            value = getpass.getpass(f"{variable.get('prompt', key)} (Enter keeps current): ").strip()
            if value:
                changes[f"env.{key}"] = value
    _post_setup(row, args)
    path = config.update_config(changes)
    print(f"Saved {', '.join(selected)} selection in {path}")
    if row.get("post_setup") == "ddgs" and not registry.provider_is_ready(provider):
        print("ddgs is not installed; run `misaka web setup ddgs --install --yes`.")
    _status()


def _accounts(args):
    from misaka.core.auth_storage import AuthStorage, oauth_account_key
    from misaka.core.web.backends.xai import _auth_path

    storage = AuthStorage.create(_auth_path())
    if args.op == "accounts":
        for key, value in storage.getAll().items():
            if (key == "xai" or key.startswith("xai:")) and isinstance(value, dict):
                print(f"{key.partition(':')[2] or '(default)'}: {value.get('type', 'unknown')}")
        return
    account = args.key  # absent means the same default grant as /login xai
    key = oauth_account_key("xai", account)
    if args.op == "logout":
        storage.remove(key)
        print(f"Removed xAI account {account or '(default)'}")
    else:
        def show_code(info):
            print(f"Open {info.verificationUri}\nDevice code: {info.userCode}")
        asyncio.run(storage.login("xai", SimpleNamespace(onDeviceCode=show_code, signal=None), account=account))
        print(f"Saved xAI account {account or '(default)'}")


def run(args):
    with WebScope(args.profile).activate():
        try:
            if args.op != "setup" and any((args.capability, args.tier, args.install, args.login)):
                raise ValueError("--capability/--tier/--install/--login apply only to `web setup`")
            if args.yes and args.op not in {"setup", "browser-setup", "browser-install"}:
                raise ValueError("--yes applies only to setup or explicit browser installation")
            if args.op != "set" and args.value is not None:
                raise ValueError("Only `web set` takes a value argument")
            if args.op == "set":
                if not args.key or args.value is None:
                    raise ValueError("Usage: misaka web set <key> <value>")
                before = config.web_config().get("provider_tier") or {}
                path = config.set_config(args.key, args.value)
                print(f"Set {args.key} in {path}")
                after = config.web_config().get("provider_tier") or {}
                cleared = set(before) - set(after) if isinstance(before, dict) and isinstance(after, dict) else set()
                if cleared:
                    print(f"Cleared tier pin on {', '.join(sorted(cleared))}")
                return
            if args.op == "unset":
                if not args.key:
                    raise ValueError("Usage: misaka web unset <key>")
                print(f"Unset {args.key} in {config.unset_config(args.key)}")
                return
            if args.op in {"accounts", "login", "logout"}:
                _accounts(args)
                return
            config.web_config(strict=True)
            if args.op.startswith("gateway-") or args.op in {"browser-connect", "browser-disconnect", "browser-install"}:
                from misaka.cli.web_browser import run
                run(args)
                return
            result = asyncio.run(_discover(args.extension))
            try:
                registry.ensure_backends_registered()
                registry.replace_extension_providers(result.extensions)
                for error in result.errors:
                    print(config.redact_secrets(f"Extension {error['path']}: {error['error']}"), file=sys.stderr)
                current_scope().browser_providers = {name: provider for extension in result.extensions
                                                     for name, provider in getattr(extension, "browserProviders", {}).items()}
                if args.op.startswith("browser-"):
                    from misaka.cli.web_browser import run
                    run(args)
                elif args.op == "setup":
                    _setup(args)
                elif args.op == "providers":
                    _providers()
                elif args.op in {"enable", "disable"}:
                    if registry.get_provider(args.key, include_disabled=True) is None:
                        raise ValueError("Name a registered provider; see `misaka web providers`")
                    path = config.set_provider_enabled(args.key, args.op == "enable")
                    print(f"{args.op.title()}d Web provider {args.key} in {path}")
                else:
                    _status()
            finally:
                result.runtime.invalidate()
        except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError, httpx.HTTPError) as error:
            print(config.redact_secrets(str(error)), file=sys.stderr)
            raise SystemExit(2) from error

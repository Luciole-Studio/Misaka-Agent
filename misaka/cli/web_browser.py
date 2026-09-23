"""Explicit browser installation/settings; never run from startup availability probes."""
import asyncio
import getpass
import json
import os
import shutil
import subprocess
from types import SimpleNamespace

from misaka.config import home
from misaka.core.web import config, gateway
from misaka.core.web.browser import settings
from misaka.core.web.browser.providers import providers
from misaka.core.web.scope import current_scope


def status():
    cfg = settings.config()
    print(f'Browser route: {settings.route()}; backend: {cfg.get("backend", "auto")}; engine: {cfg.get("engine", "auto")}')
    print('Browser tools available before session permission ceiling: ' + ', '.join(sorted(settings.available_tools())))
    for name in ('agent-browser', 'browser-use'):
        print(f'{name}: {settings.executable(name) or "not installed"}')
    print('Nous gateway: ' + ('token present; entitlement checked on use' if gateway.available() else 'not logged in'))
    print('Real profile: ' + ('explicit isolated copy' if cfg.get('use_real_profile') else 'off'))


def run(args):
    if args.op.startswith('gateway-'):
        auth = gateway.storage()
        if args.op == 'gateway-login':
            def show(info):
                print(f'Open {info.verificationUri}\nDevice code: {info.userCode}')
            asyncio.run(auth.login('nous', SimpleNamespace(onDeviceCode=show, signal=None)))
            print('Nous OAuth saved in this profile auth.json')
        elif args.op == 'gateway-logout':
            auth.remove('nous')
            current_scope().gateway_accounts.clear()
            print('Nous profile grant removed; explicit TOOL_GATEWAY_USER_TOKEN still takes precedence if configured')
        else:
            _, account = asyncio.run(gateway.account(force=True))
            print(json.dumps(account, indent=2))
        return
    if args.op == 'browser-status':
        status()
    elif args.op == 'browser-providers':
        print('local / cdp / camofox / controller are session-owned adapters, not paid providers.')
        for name, provider in providers().items():
            schema = provider.get_setup_schema()
            print(f'{name}: {"configured" if provider.is_available() else "not configured"}; {schema.get("badge", "extension")}')
            print('  ' + ', '.join(row['key'] for row in schema.get('env_vars', [])))
    elif args.op == 'browser-setup':
        name = args.key
        if name not in {'local', 'cdp', 'camofox'} and name not in providers():
            raise ValueError('Choose local, cdp, camofox or a name from browser-providers')
        changes = {'browser.enabled': 'true', 'browser.cloud_provider': name}
        from misaka.cli.web import _provider_rows, _select
        variants = _provider_rows(providers()[name]) if name in providers() else [{'name': name, 'env_vars': []}]
        row = _select(variants, 'Browser setup') if len(variants) > 1 and not args.yes else variants[0]
        if row.get('post_setup'):
            raise ValueError('Browser provider setup uses explicit browser-install or gateway-login, not post_setup hooks')
        if not args.yes:
            for variable in row.get('env_vars', []):
                value = getpass.getpass(f'{variable.get("prompt", variable["key"])} (Enter keeps current): ').strip()
                if value:
                    changes['env.' + variable['key']] = value
        config.update_config(changes)
        status()
    elif args.op == 'browser-connect':
        if not args.key:
            raise ValueError('browser-connect requires an explicit CDP discovery or WebSocket URL')
        from misaka.core.web.browser.cdp import resolve_endpoint
        # Probe only discovery. Opening/closing tabs belongs to the future session owner.
        asyncio.run(resolve_endpoint(args.key))
        config.update_config({'browser.cdp_url': args.key, 'browser.enabled': 'true'})
        print('CDP endpoint saved; sessions attach their own tab on first use')
    elif args.op == 'browser-disconnect':
        config.update_config({}, remove=('browser.cdp_url', 'browser.controller_command', 'env.BROWSER_CDP_URL'))
        print('Profile browser binding removed for future sessions; active owners keep their lifecycle')
        if os.environ.get('BROWSER_CDP_URL'):
            print('The process BROWSER_CDP_URL override remains set; unset it in your shell')
    elif args.op == 'browser-install':
        name = args.key or 'agent-browser'
        if not args.yes and input(f'Install pinned {name} into the selected profile? [y/N] ').lower() != 'y':
            return
        root = home.path('web_tools', current_scope().profile_dir)
        root.mkdir(parents=True, exist_ok=True)
        if name == 'agent-browser':
            npm = shutil.which('npm')
            if not npm:
                raise ValueError('Install Node/npm first, then run browser-install again')
            subprocess.run([npm, 'install', '--prefix', str(root), '--save-exact', 'agent-browser@0.26.0'], check=True)
            cli = root / 'node_modules' / '.bin' / 'agent-browser'
            subprocess.run([str(cli), 'install'], env=settings.subprocess_env(), check=True)
        elif name == 'browser-use':
            uv = shutil.which('uv')
            if not uv:
                raise ValueError('Install uv first; Browser Use is isolated from MISAKA dependencies')
            env = root / 'browser-use'
            if not (env / 'pyvenv.cfg').exists():
                subprocess.run([uv, 'venv', str(env)], check=True)
            python = env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            subprocess.run([uv, 'pip', 'install', '--python', str(python), 'browser-use==0.13.10'], check=True)
            cli = env / ('Scripts/browser-use.exe' if os.name == 'nt' else 'bin/browser-use')
            config.update_config({'browser.exec_command': str(cli)})
        else:
            raise ValueError('browser-install takes agent-browser or browser-use; configure lightpanda_path explicitly for Lightpanda')
        print('Install complete. CDP transport also needs MISAKA\'s browser extra: uv sync --extra browser')
        status()

"""Private ownership receipts: reap only dead MISAKA owners in the same profile.

No process-name sweep, personal-tab discovery or background global daemon. Reaping
runs on explicit browser use. Cloud creations with lost receipts remain bounded by
the vendor TTL; a local file cannot prove an unacknowledged remote creation.
"""
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

import httpx
import psutil

from misaka.config import get_agent_dir
from misaka.core.web.browser.cdp import Supervisor
from misaka.core.web.browser.providers import CloudLease
from misaka.core.web.scope import current_scope
from misaka.utils.async_lifecycle import run_in_thread
from misaka.utils.atomic import write_text


def prefix():
    identity = str(current_scope().profile_dir or get_agent_dir())
    return 'misaka-browser-' + hashlib.sha256(identity.encode()).hexdigest()[:16] + '-'


def parent():
    process = psutil.Process()
    return [process.pid, process.create_time()]


def valid_identity(identity):
    import math
    return (isinstance(identity, (tuple, list)) and len(identity) == 2
            and type(identity[0]) is int and identity[0] > 0
            and type(identity[1]) in {int, float} and math.isfinite(identity[1]) and identity[1] > 0)


def alive(identity):
    if not valid_identity(identity):
        return True  # Unknown ownership is not proof that it is safe to reap.
    try:
        return psutil.Process(identity[0]).create_time() == identity[1]
    except psutil.NoSuchProcess:
        return False


def receipt(session):
    data = {'parent': parent(), 'daemon': session.daemon_identity, 'harness': session.harness_identity,
            'native': [session.native_process.pid, psutil.Process(session.native_process.pid).create_time()] if session.native_process and session.native_process.returncode is None else None,
            'cdp_url': session.cdp_url if session.external_tab else None,
            'target': session.supervisor.root if session.supervisor else None,
            'lease': asdict(session.lease) if isinstance(session.lease, CloudLease) else None}
    write_text(str(session.root / 'owner.json'), json.dumps(data), mode=0o600)


def reap_process(identity):
    if not valid_identity(identity) or not alive(identity):
        return
    try:
        process = psutil.Process(identity[0])
        children = process.children(recursive=True)
        for child in reversed(children):
            child.terminate()
        process.terminate()
        _, remaining = psutil.wait_procs([*children, process], timeout=2)
        for child in remaining:
            child.kill()
        psutil.wait_procs(remaining, timeout=2)
    except psutil.NoSuchProcess:
        pass


def stale():
    base = Path('/tmp' if os.name == 'posix' else tempfile.gettempdir())
    result = []
    for root in base.glob(prefix() + '*'):
        if root.is_symlink() or not root.is_dir() or (hasattr(os, 'getuid') and root.stat().st_uid != os.getuid()):
            continue
        try:
            data = json.loads((root / 'owner.json').read_text())
            if isinstance(data, dict) and not alive(data.get('parent')):
                result.append((root, data))
        except (OSError, ValueError, psutil.Error):
            continue
        if len(result) >= 32:
            break  # Bounded work per first browser use; subsequent calls drain more.
    return result


async def reap():
    # ponytail: at most one stale owner per launch, avoiding N remote timeouts on startup.
    for root, data in (await run_in_thread(stale))[:1]:
        failures = []
        if data.get('cdp_url') and data.get('target'):
            supervisor = Supervisor()
            try:
                await supervisor.connect(data['cdp_url'], target_id=data['target'])
                await supervisor.close(close_tab=True)
            except (OSError, ValueError, RuntimeError, TimeoutError, httpx.HTTPError) as error:
                failures.append(error)
            finally:
                try:
                    await supervisor.close()
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    failures.append(error)
        if data.get('lease'):
            try:
                lease = CloudLease(**data['lease'])
                await lease.close()
                data['lease'] = asdict(lease)
                # Releasing a cloud browser also closes its otherwise-unreachable tab.
                failures.clear()
                data['cdp_url'] = None
            except (OSError, ValueError, RuntimeError, TimeoutError, httpx.HTTPError) as error:
                failures.append(error)
        for key in ('harness', 'daemon', 'native'):
            try:
                await run_in_thread(reap_process, data.get(key))
            except (OSError, psutil.Error) as error:
                failures.append(error)
        if failures:
            await run_in_thread(write_text, str(root / 'owner.json'), json.dumps(data), mode=0o600)
        else:
            await run_in_thread(shutil.rmtree, root)
        await asyncio.sleep(0)

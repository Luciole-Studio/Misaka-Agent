"""Small host seams for the pinned distribution algorithms, not a second Hub."""
import asyncio
import base64
import contextvars
import json
import time
from concurrent.futures import ThreadPoolExecutor as _Pool

import httpx

from .scope import check_active, current_scope, get_secret


class ThreadPoolExecutor(_Pool):
    """Context-local role identity follows each worker; all futures are owned."""
    def submit(self, fn, /, *args, **kwargs):
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


DaemonThreadPoolExecutor = ThreadPoolExecutor  # Native import seam; never daemon/abandoned.


def decode_claims(token):
    # Advisory routing only. The server, not this decoder, authenticates the JWT.
    parts = token.split('.')
    if len(parts) != 3:
        raise ValueError('Malformed bearer token')
    value = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
    if not isinstance(value, dict):
        raise TypeError('Malformed bearer claims')
    return value


def resolve_nous_runtime_credentials():
    return {'api_key': get_secret('NOUS_API_KEY') or get_secret('HERMES_SYNC_TOKEN')}


def http_get(url, *, timeout=20, headers=None, params=None, follow_redirects=True):
    """Preserve HTTPX responses, with MISAKA's existing per-hop/pinned bounded transport."""
    from misaka.core.tools._web.bounded import (
        UnsafeUrlError,
        open_checked_stream,
        read_bounded,
    )
    from misaka.core.web.scope import WebScope
    scope = current_scope()
    check_active(scope)
    deadline = time.monotonic() + timeout
    if scope.deadline is not None:
        deadline = min(deadline, scope.deadline)
    if params:
        url = str(httpx.URL(url).copy_merge_params(params))
    async def fetch():
        web = WebScope(str(scope.profile), environment=scope.environment)
        check_active(scope)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpx.ReadTimeout('Skill Hub request deadline exceeded')
        with web.activate():
            async with asyncio.timeout(remaining), open_checked_stream(url, headers=headers, timeout=remaining,
                    max_redirects=5 if follow_redirects else 0, service='skill_hub') as response:
                check_active(scope)
                body, truncated = await read_bounded(response, max_bytes=128 * 1024 * 1024, deadline=deadline, signal=scope.stop)
                check_active(scope)
                if truncated:
                    raise ValueError("Skill registry response exceeds byte budget.")
                # Decoded body: don't feed its old gzip header to HTTPX a second time.
                safe_headers = {k: v for k, v in response.headers.items()
                                if k not in ('content-encoding', 'content-length', 'transfer-encoding')}
                return httpx.Response(response.status_code, headers=safe_headers,
                                      content=body, request=httpx.Request('GET', str(response.url)))
    async def owned():
        task = asyncio.create_task(fetch())
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.05)
                check_active(scope)
            return task.result()
        finally:
            # Stop also covers waiting for headers/redirects, not just body chunks.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    try:
        return asyncio.run(owned())
    except TimeoutError as error:
        raise httpx.ReadTimeout('Skill Hub request deadline exceeded', request=httpx.Request('GET', url)) from error
    except (UnsafeUrlError, ValueError) as error:
        raise httpx.RequestError(str(error), request=httpx.Request('GET', url)) from error


def guarded_get(url, *, timeout=20):
    try:
        return http_get(url, timeout=timeout)
    except (httpx.HTTPError, OSError):
        return None


def validate_tree_entries(entries):
    """Validate all remote members before callers concatenate paths or delete a target."""
    from .vendor.skills_sync_client_wire import SyncError
    seen = set()
    if not isinstance(entries, list) or len(entries) > 4096:
        raise SyncError('Invalid or oversized remote tree')
    for entry in entries:
        if not isinstance(entry, dict):
            raise SyncError('Invalid remote tree entry')
        name = entry.get('name')
        if (not isinstance(name, str) or not name or name in ('.', '..')
                or any(c in name for c in '/\\:') or any(ord(c) < 32 for c in name)
                or name.casefold() in seen):
            raise SyncError('Unsafe or duplicate remote tree member')
        seen.add(name.casefold())
        if entry.get('kind') not in ('blob', 'tree') or not isinstance(entry.get('hash'), str):
            raise SyncError('Invalid remote object kind or address')
    return entries


def checked_sync_target(dest):
    from .scope import _skills_dir
    from .write import _safe_parents
    dest = __import__('pathlib').Path(dest).absolute()
    root = _skills_dir().absolute()
    if dest == root or not dest.is_relative_to(root):
        raise ValueError('Sync target is outside its owned Skill directory.')
    _safe_parents(dest)
    return dest


def pull_selected(client, identity):
    """Native pull boundary correction: empty opt-ins never mean all remote objects.

    Keep the wire/manifest/merge functions, protect local edits and user opt-outs,
    and stage complete replacements rather than accumulate remote-deleted files.
    """
    import shutil

    from .scope import _skills_dir
    from .vendor import skill_usage as usage
    from .vendor import skills_sync_client as sync
    from .vendor.skills_sync_client_wire import ObjectSet, build_tree, merge_skill
    sync.checked_capabilities(client)
    head = sync.read_ref_hash(client, sync.user_head_ref(identity['owner']))
    state = sync.read_sync_state()
    if not head:
        return {'ok': True, 'reason': 'no remote HEAD yet', 'noop': True}
    if head == state.get('head'):
        return {'ok': True, 'reason': 'already up to date', 'head': head, 'noop': True}
    root = sync.root_tree_of_commit(client, head)
    remote = sync.skill_trees_of_root(client, root)
    manifest = sync.read_manifest_of_root(client, root)
    flags = usage.load_usage()
    opted = set(sync._opted_in_rel_paths())
    from .scope import locally_deleted_skills
    deleted = locally_deleted_skills()
    adopted = set()
    for name, enabled in (manifest or {}).items():
        record = flags.get(name, {})
        if enabled and name in remote and record.get('sync') is not False:
            path = checked_sync_target(_skills_dir() / name)
            protected = (name.split('/')[0] == '_org' or name in usage.read_suppressed_names()
                         or record.get('state') == usage.STATE_ARCHIVED or name in deleted
                         or not usage.is_agent_created(name) or not usage.is_agent_created(name.rsplit('/', 1)[-1]))
            if not protected and (not path.exists() or sync.is_sync_eligible(name)):
                opted.add(name)
                if record.get('sync') is not True:
                    adopted.add(name)
    updated, conflicted = [], []
    base = sync.skill_trees_of_root(client, sync.root_tree_of_commit(client, state['head'])) if state.get('head') else {}
    for name in sorted(opted & remote.keys()):
        if name.split('/')[0] == '_org' or name in usage.read_suppressed_names():
            continue
        dest = checked_sync_target(_skills_dir() / name)
        if dest.exists():
            if not sync.is_sync_eligible(name):
                continue
            ours = build_tree(dest, ObjectSet(), max_object_bytes=sync.DEFAULT_MAX_OBJECT_BYTES)
            decision = merge_skill(base.get(name), ours, remote[name])
            if decision == 'overlap':
                conflicted.append(name)
                continue
            if decision in ('ours', 'either'):
                continue
            shutil.rmtree(dest)
        sync.materialize_tree(client, remote[name], dest)
        usage.set_sync(name, True)
        updated.append(name)
    if conflicted:
        return {'ok': False, 'conflict': True, 'head': head, 'updated': [], 'conflicted': conflicted,
                'message': 'Personal sync has overlapping local edits; no role bytes or baseline were published.'}
    sync.write_sync_state({**state, 'head': head})
    return {'ok': True, 'head': head, 'updated': updated, 'opt_in_adopted': sorted(adopted)}


def sync_http_client(base_url):
    from .scope import _current
    scope = _current.get()
    if scope is None:
        # Direct standalone wire clients have no role; HTTPX retains its native environment behavior.
        return httpx.Client(follow_redirects=False)
    from misaka.core.web.network import proxy_for_url, tls_verify
    from misaka.core.web.scope import WebScope
    with WebScope(str(scope.profile), environment=scope.environment).activate():
        return httpx.Client(follow_redirects=False, trust_env=False,
                            proxy=proxy_for_url(base_url, api=True), verify=tls_verify())


def owned_sync_request(client, method, url, *, timeout=30, **kwargs):
    """A native HTTP call with bounded decoded bytes, stop checks and one operation deadline."""
    import time

    from .scope import _current
    scope = _current.get()
    deadline = getattr(scope, 'deadline', None)
    if deadline is None:
        deadline = time.monotonic() + 300
    def check():
        if scope is not None and scope.stop.is_set():
            raise RuntimeError('Skill sync owner stopped; reconcile remote state before retrying publication.')
        if time.monotonic() >= deadline:
            raise httpx.ReadTimeout('Skill sync operation deadline exceeded')
    check()
    with client.stream(method, url, timeout=min(timeout, max(0.01, deadline - time.monotonic())), **kwargs) as response:
        chunks, total = [], 0
        for chunk in response.iter_bytes():
            check()
            total += len(chunk)
            if total > 26214400:
                raise ValueError('Skill sync response exceeds object byte budget')
            chunks.append(chunk)
        headers = {k:v for k,v in response.headers.items() if k not in ('content-encoding', 'content-length', 'transfer-encoding')}
        return httpx.Response(response.status_code, headers=headers, content=b''.join(chunks), request=response.request)

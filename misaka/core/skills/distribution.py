"""Role-scoped distribution transactions; source and sync algorithms stay native.

A single publication owner stages Skill bytes AND their native manifests together.
Approvals carry immutable after-images, never a URL to download again after review.
"""
import dataclasses
import json
import shutil
import tempfile
from datetime import UTC
from pathlib import Path

from . import guard, index, write
from .scope import (
    SkillScope,
    check_active,
    current_scope,
    referenced_skill_names,
    using_scope,
)

READS = frozenset(('hub-search', 'hub-browse', 'hub-inspect', 'hub-installed', 'hub-update-check',
    'tap-list', 'bundled-diff', 'bundled-modified', 'optional-list', 'sync-status', 'hub-snapshot-export', 'generation-status'))
MUTATIONS = frozenset(('hub-install', 'hub-uninstall', 'hub-update', 'tap-add', 'tap-remove',
    'bundled-sync', 'bundled-reset', 'bundled-opt-in', 'bundled-opt-out', 'bundled-remove',
    'optional-restore', 'sync-push', 'sync-pull', 'org-pull', 'org-propose', 'sync-device', 'hub-snapshot-import', 'hub-publish', 'archive', 'restore', 'backup', 'restore-backup', 'org-clear', 'generation-activate'))
OPERATIONS = READS | MUTATIONS
REMOTE_WRITES = frozenset(('sync-push', 'org-propose', 'hub-publish'))


def _jsonable(value):
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _receipt(value):
    result = _jsonable(value)
    if not isinstance(result, dict):
        raise TypeError('Invalid Skill operation receipt.')
    return {**result, 'success': result.get('success', result.get('ok', True))}


def _sources(supplied):
    from .vendor.skills_hub_search import create_source_router
    return supplied if supplied is not None else create_source_router()


def _fetch(identifier, sources):
    from .vendor.hub_cli_algorithms import _resolve_source_meta_and_bundle
    from .vendor.skills_hub_search import unified_search
    if "/" not in identifier:
        matches = [r for r in unified_search(identifier, sources, limit=20) if r.name.lower() == identifier.lower()]
        official = [r for r in matches if r.source == "official"]
        selected = matches if len(matches) == 1 else official
        if len(selected) != 1:
            raise ValueError("Use a full Skill identifier; candidates: " + ", ".join(r.identifier for r in matches))
        identifier = selected[0].identifier
    meta, bundle, _source = _resolve_source_meta_and_bundle(identifier, sources)
    if bundle is None:
        raise ValueError("No source returned the requested Skill: " + identifier)
    bundle.metadata = {**(getattr(meta, "extra", {}) or {}), **bundle.metadata}
    return bundle


def _install(identifier, *, sources, category='', force=False, update=False):
    from .vendor import skillevaluator_scan as tier1
    from .vendor import skills_hub as hub
    from .vendor import skills_hub_install as native
    bundle = _fetch(identifier, sources)
    existing = hub.HubLockFile().get_installed(bundle.name)
    if existing and not update and not force:
        raise ValueError('Already Hub-installed; use hub-update to replace the pinned installation.')
    if update:
        if not existing:
            raise ValueError('Update target is not Hub-installed.')
        if existing['identifier'] != bundle.identifier:
            raise ValueError('Update source identity differs from the existing installation.')
        category = str(Path(existing['install_path']).parent)
        category = '' if category == '.' else category
    if bundle.source == "official" and not category:
        category = "/".join(bundle.identifier.split("/")[1:-1])
    total = sum(len(c.encode() if isinstance(c, str) else c) for c in bundle.files.values())
    if total > 64 * 1024 * 1024 or len(bundle.files) > 4096:
        raise ValueError('Skill bundle exceeds the byte/file budget.')
    stage = native.quarantine_bundle(bundle)
    scan, provenance = guard.scan_skill_cached(stage, source=bundle.metadata.get('repo') or (bundle.identifier.rsplit("/", 1)[0] if bundle.source == "github" else bundle.trust_level),
                                                source_url=bundle.identifier)
    allowed, reason = guard.should_allow_install(scan, force=force)
    try:
        advisory = tier1.run_tier1_scan(stage) if tier1.tier1_advisory_enabled() else tier1.Tier1Report(available=False, error='disabled')
    except Exception as error:  # noqa: BLE001 - advisory dependency failures never bypass or replace enforcement
        advisory = tier1.Tier1Report(available=False, error=str(error))
    if allowed is not True:
        raise ValueError(reason + '\n' + guard.format_scan_report(scan))
    dest = native.install_from_quarantine(stage, bundle.name, category, bundle, scan, provenance)
    return {'success': True, 'path': str(dest), 'scan': scan, 'advisory': advisory}


def _run(operation, name, **kw):
    from .vendor import skills_hub as hub
    from .vendor import skills_hub_install as install
    from .vendor import skills_hub_official as official
    from .vendor import skills_hub_search as search
    from .vendor import skills_sync as bundled
    from .vendor import skills_sync_bundled_ops as bundled_ops
    from .vendor import skills_sync_client as sync
    from .vendor import skills_sync_client_org as org
    from .vendor import skills_sync_optional as optional
    sources = kw.get('sources')
    if operation == 'org-clear':
        org._clear_active_org_marker()
        if org._sidecar_path(None, 'ORG_ACTIVE_MARKER').exists():
            raise ValueError('Active organisation marker could not be cleared.')
        return {'success': True, 'message': 'Inactive organisation mirror is hidden.'}
    if operation == 'generation-status':
        from .release import status
        return status(current_scope().profile)
    if operation == 'generation-activate':
        from .release import activate
        return activate(current_scope(), kw.get('expected_digest'))
    if operation in ('backup', 'restore-backup'):
        from .vendor import curator_backup
        if operation == 'backup':
            path = curator_backup.snapshot_skills(reason=kw.get("reason", "manual"), protect_ids=kw.get("protect_ids"))
            return {'success': path is not None, 'path': str(path) if path else None}
        ok, message, path = curator_backup.rollback(name)
        return {'success': ok, 'message': message, 'path': str(path) if path else None}
    if operation in ('archive', 'restore'):
        from .operations import restore_archive
        from .vendor import skill_usage
        ok, message = skill_usage.archive_skill(name) if operation == 'archive' else restore_archive(name)
        return {'success': ok, 'message': message}
    if operation in ('hub-search', 'hub-browse'):
        results, counts, timed_out = search.parallel_search_sources(_sources(sources), name or '',
            source_filter=kw.get('source', 'all'), overall_timeout=kw.get('timeout', 30))
        return {'success': True, 'results': results, 'counts': counts, 'timed_out': timed_out}
    if operation == 'hub-inspect':
        for source in _sources(sources):
            if (meta := source.inspect(name)):
                return {'success': True, 'skill': meta}
        return {'success': False, 'error': 'Skill not found.'}
    if operation == 'hub-snapshot-export':
        from datetime import datetime
        return {'success': True, 'snapshot': {'hermes_version': '0.1.0',
            'exported_at': datetime.now(UTC).isoformat(),
            'skills': [{'name': e['name'], 'source': e.get('source', ''), 'identifier': e.get('identifier', ''),
                        'category': str(Path(e['install_path']).parent) if '/' in e.get('install_path', '') else ''}
                       for e in hub.HubLockFile().list_installed()], 'taps': hub.TapsManager().list_taps()}}
    if operation == 'hub-snapshot-import':
        snapshot = kw.get('snapshot')
        if snapshot is None:
            snapshot = json.loads(Path(name).read_text())
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('skills', []), list) or not isinstance(snapshot.get('taps', []), list):
            raise ValueError('Invalid Hub snapshot.')
        for tap in snapshot.get('taps', []):
            if tap.get('repo'):
                hub.TapsManager().add(tap['repo'], tap.get('path', 'skills/'))
        results = []
        for entry in snapshot.get('skills', []):
            if not entry.get('identifier'):
                raise ValueError('Snapshot entry has no source identifier.')
            selected = [src for src in _sources(sources) if install._source_matches(src, entry['source'])]
            results.append(_install(entry['identifier'], sources=selected,
                                   category=entry.get('category', ''), force=kw.get('force', False)))
        return {'success': True, 'results': results}
    if operation == 'hub-publish':
        from .scope import find_skill
        from .vendor.hub_cli_algorithms import _github_publish
        from .vendor.skills_hub_github import GitHubAuth
        entry = find_skill(name)
        if entry is None:
            raise ValueError('Publication requires a Skill in this role.')
        repo = kw.get('repo', '')
        if len(repo.split('/')) != 2 or any(not part or part in ('.', '..') for part in repo.split('/')):
            raise ValueError('Publication requires owner/repo.')
        from .vendor.skills_hub_models import _validate_skill_name
        metadata = entry['frontmatter']
        if not metadata.get('description', ''):
            raise ValueError("SKILL.md must have a description in frontmatter.")
        publish_name = metadata.get('name', entry['path'].name)
        _validate_skill_name(publish_name)
        scan = guard.scan_skill(entry['path'], source='self', honor_ignore=False)
        if scan.verdict == 'dangerous':
            raise ValueError(guard.format_scan_report(scan))
        auth = GitHubAuth()
        if not auth.is_authenticated():
            raise ValueError('GitHub authentication is required.')
        ok, message = _github_publish(entry['path'], publish_name, repo, auth)
        return {'success': ok, 'message': message}
    if operation == 'hub-installed':
        return {'success': True, 'skills': hub.HubLockFile().list_installed()}
    if operation == 'hub-update-check':
        return {'success': True, 'updates': install.check_for_skill_updates(sources=_sources(sources))}
    if operation == 'hub-install':
        return _install(name, sources=_sources(sources), category=kw.get('category', ''), force=kw.get('force', False))
    if operation == 'hub-update':
        record = hub.HubLockFile().get_installed(name)
        if not record:
            raise ValueError('Skill not Hub-installed.')
        path = install._resolve_lock_install_path(record['install_path'], name)
        if path.exists() and guard.content_hash(path) != record['content_hash'] and not kw.get('force'):
            raise ValueError('Local Skill was edited; explicit force is required to overwrite it.')
        return _install(record['identifier'], sources=[src for src in _sources(sources) if install._source_matches(src, record['source'])], force=kw.get('force', False), update=True)
    if operation == 'hub-uninstall':
        ok, message = install.uninstall_skill(name)
        return {'success': ok, 'message': message}
    if operation.startswith('tap-'):
        manager = hub.TapsManager()
        if operation == 'tap-list':
            return {'success': True, 'taps': manager.list_taps()}
        changed = manager.add(name, path=kw.get('category') or 'skills/') if operation == 'tap-add' else manager.remove(name)
        return {'success': True, 'changed': changed}
    if operation == 'bundled-sync':
        return {'success': True, **bundled.sync_skills(quiet=True)}
    if operation == 'bundled-diff':
        return bundled_ops.diff_bundled_skill(name)
    if operation == 'bundled-modified':
        return {'success': True, 'skills': bundled_ops.list_user_modified_bundled_skills()}
    if operation == 'bundled-reset':
        return bundled_ops.reset_bundled_skill(name, restore=kw.get('restore', False))
    if operation in ('bundled-opt-in', 'bundled-opt-out'):
        return bundled_ops.set_bundled_skills_opt_out(operation == 'bundled-opt-out')
    if operation == 'bundled-remove':
        return bundled_ops.remove_pristine_bundled_skills(dry_run=kw.get('dry_run', False))
    if operation == 'optional-list':
        return {'success': True, 'skills': official.OptionalSkillSource().list_local()}
    if operation == 'optional-restore':
        return optional.restore_official_optional_skill(name, restore=kw.get('restore', False))
    if operation == 'sync-status':
        return {'success': True, **sync.sync_status()}
    if operation == 'sync-device':
        return {'success': True, 'device': sync.set_device_name(name)}
    if operation in ('sync-push', 'sync-pull', 'org-pull', 'org-propose'):
        identity = kw.get('identity')
        if identity is None:
            identity = org.resolve_org_identity() if operation.startswith('org-') else sync.resolve_identity()
        if operation.startswith('org-'):
            from .vendor.skills_hub_models import _validate_skill_name
            _validate_skill_name(identity.get('org_id', ''))
            if not identity.get('org_role'):
                raise sync.SyncInertError('Shared organisation membership is required.')
        if not identity.get('nous_admin'):
            raise sync.SyncInertError('The pinned sync protocol requires the Nous admin claim.')
        if not sync.sync_feature_enabled() and not kw.get('client'):
            raise sync.SyncInertError('Skill Sync is disabled in the role configuration.')
        client, owned = kw.get('client'), None
        try:
            if client is None:
                from .vendor.skills_sync_client_wire import SyncClient
                base = sync.resolve_sync_base_url()
                url = __import__('urllib.parse', fromlist=['urlsplit']).urlsplit(base)
                if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
                    raise ValueError('Invalid Skill Sync service origin.')
                client = owned = SyncClient(base, identity['api_key'])
            if operation == 'sync-push':
                return sync.push_skills(client, identity=identity)
            if operation == 'sync-pull':
                return sync.pull_skills(client, identity=identity)
            if operation == 'org-pull':
                return org.pull_org_skills(client, identity=identity)
            return org.propose_skill(name, client, identity=identity)
        finally:
            if owned is not None:
                owned.close()
    raise ValueError('Unknown Skill distribution operation: ' + operation)


def _validate_metadata(root):
    # Operational state corruption is not an empty registry. Never replace it silently.
    write._safe_parents(root)
    if root.exists():
        for child in root.iterdir():
            if child.name.startswith('.') and child.name != '.git':
                for path in (child, *child.rglob('*')) if child.is_dir() and not child.is_symlink() else (child,):
                    if path.is_symlink():
                        raise ValueError('Skill metadata is a symlink: ' + str(path.relative_to(root)))
    for relative in ('.usage.json', '.hub/lock.json', '.hub/taps.json', '.sync_state', '.sync_manifest', '.archive-origins.json', '.curator_state'):
        path = root / relative
        if path.exists():
            write._safe_parents(path.parent)
            if path.is_symlink() or not isinstance(json.loads(path.read_text()), dict):
                raise ValueError('Invalid Skill state; bytes preserved: ' + relative)


def _pre_publish(before, after, scope, operation):
    old = {p['path']: p for p in before['files']}
    new = {p['path']: p for p in after['files']}
    from .vendor import skill_usage
    with using_scope(scope):
        referenced = referenced_skill_names()
        for path in old.keys() - new.keys():
            if Path(path).name == 'SKILL.md':
                name = Path(path).parent.as_posix()
                if name in referenced or skill_usage.get_record(name).get("pinned", False):
                    raise ValueError('Removal would break a pinned/referenced Skill: ' + name)
        # A remote pull is not a route for adding/changing external-library links.
        for path in old.keys() | new.keys():
            if any(items.get(path, {}).get('symlink') is not None for items in (old, new)) and old.get(path) != new.get(path):
                raise ValueError('Distribution must preserve external-library links: ' + path)
        _scan_publication(old, new, after)


def _scan_publication(old, new, after):
    """Scan immutable changed Skill bytes, including approvals, never URLs or live links."""
    from .manage import _security_scan
    roots = {Path(path).parent for path in new if Path(path).name == 'SKILL.md'
             and not any(part.startswith('.') for part in Path(path).parent.parts)}
    changed = {path for path in old.keys() | new.keys() if old.get(path) != new.get(path)}
    affected = {parent for path in changed for parent in Path(path).parents if parent in roots}
    if not affected:
        return
    for path, item in new.items():
        if item.get('symlink') is not None and any(Path(path).is_relative_to(root) for root in affected):
            raise ValueError('Changed Skill contains a library link; detached scan requires regular files: ' + path)
    # ponytail: reuse the already owned image store; only changed Skill directories
    # are scanned. The whole-role transaction already pays for one detached image.
    with tempfile.TemporaryDirectory(prefix='misaka-skill-scan-') as temporary:
        root = Path(temporary) / 'skills'
        write._materialize(root, after)
        for relative in sorted(affected):
            if error := _security_scan(root / relative):
                raise ValueError(f'{relative}: {error}')


def execute(operation, name=None, *, scope=None, **kwargs):
    import time
    scope = scope or current_scope()
    deadline = time.monotonic() + 300
    scope = dataclasses.replace(scope, deadline=deadline if scope.deadline is None else min(scope.deadline, deadline))
    try:
        with using_scope(scope):
            check_active(scope)
            if operation in READS:
                _validate_metadata(scope.profile / 'skills')
                return _receipt(_run(operation, name, **kwargs))
            # Verified account withdrawal changes visibility metadata, never Skill content.
            decision, reason = ('allow', '') if operation == 'org-clear' else write.evaluate_gate()
            if decision == 'off':
                return {'success': False, 'error': reason}
            if operation in REMOTE_WRITES and kwargs.get('dry_run'):
                return {'success': True, 'dry_run': True, 'remote_requests': 0, 'operation': operation}
            if operation in REMOTE_WRITES and decision != 'allow':
                return {'success': False, 'error': 'Remote publication requires the write gate to allow it; no request was sent.'}
            # ponytail: one Skill writer lock through native I/O. Split publication into
            # optimistic phases only if measured latency justifies another protocol.
            with write.mutation_lock():
                check_active(scope)
                write.recover_transactions()
                target = scope.profile / 'skills'
                _validate_metadata(target)
                if scope.stop.is_set():
                    raise ValueError('Skill distribution owner stopped.')
                before = write.tree_image(target)
                with tempfile.TemporaryDirectory(prefix='misaka-skill-distribution-') as temporary:
                    home = Path(temporary).resolve()
                    if target.exists():
                        shutil.copytree(target, home / 'skills', symlinks=True)
                    else:
                        (home / 'skills').mkdir()
                    # Native code must never write through a copied link into a live library.
                    # Hide links while it runs, then restore only untouched link slots.
                    links = [item for item in before['files'] if item.get('symlink') is not None]
                    for item in links:
                        (home / 'skills' / item['path']).unlink()
                    staged = dataclasses.replace(scope, storage=home)
                    with using_scope(staged):
                        result = _receipt(_run(operation, name, **kwargs))
                    if result.get('success', result.get('ok', True)) is False:
                        return result
                    check_active(scope)
                    for item in links:
                        path = home / 'skills' / item['path']
                        if not path.exists() and not path.is_symlink():
                            if not path.parent.is_dir():
                                raise ValueError('Distribution would detach an external-library link: ' + item['path'])
                            path.symlink_to(item['symlink'])
                    after = write.tree_image(home / 'skills')
                    _pre_publish(before, after, scope, operation)
                    if write.tree_image(target, store=False) != before:
                        raise ValueError('Role Skill tree changed concurrently; staged publication was discarded.')
                    # Replace temp paths only in declared path fields, not arbitrary Skill content.
                    for field in ('path', 'marker', 'dest'):
                        if isinstance(result.get(field), str):
                            path = Path(result[field])
                            if path.is_absolute() and path.resolve().is_relative_to(home):
                                result[field] = str(scope.profile / path.resolve().relative_to(home))
                    if before == after or kwargs.get('dry_run'):
                        return {**result, 'success': True, 'changed': before != after, 'dry_run': bool(kwargs.get('dry_run'))}
                    payload = {'version': 3, 'kind': 'distribution', 'profile_dir': str(scope.profile),
                        'workspace': str(scope.workspace), 'operation': operation, 'name': name,
                        'before': before, 'after': after, 'result': result}
                    if decision == 'stage':
                        pending = write.stage(payload, summary=operation + ' ' + str(name or ''))
                        return {'success': False, 'pending_id': pending['id'], 'message': reason}
                    check_active(scope)
                    return _publish(payload)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        return {'success': False, 'error': str(error), 'remote_effects_possible': operation in REMOTE_WRITES}


def _publish(payload):
    root = Path(payload['profile_dir']) / 'skills'
    if write.tree_image(root, store=False) != payload['before']:
        raise ValueError('Role Skill tree changed after preview; stage again.')
    scope = SkillScope(Path(payload['profile_dir']), Path(payload['workspace']))
    _pre_publish(payload['before'], payload['after'], scope, payload['operation'])
    if payload['operation'] == 'generation-activate':
        from .release import _live_misaka_processes
        if _live_misaka_processes():
            raise ValueError('Live owners appeared before publication; activation was discarded.')
    journal = write.prepare_transaction([root], action=payload['operation'], skill=payload.get('name') or '*',
        evidence={'profile_dir': str(scope.profile), 'workspace': str(scope.workspace), 'distribution': True})
    entry = write.commit_transaction(journal, [payload['after']])
    index.invalidate()
    return {**payload['result'], 'success': True, 'ledger_id': entry}


def apply_pending(payload):
    if payload.get('kind') != 'distribution' or payload.get('operation') not in MUTATIONS - REMOTE_WRITES:
        return {'success': False, 'error': 'Unknown or remote distribution approval.'}
    try:
        with write.mutation_lock():
            if write.evaluate_gate()[0] == 'off':
                return {'success': False, 'error': write.evaluate_gate()[1]}
            return _publish(payload)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {'success': False, 'error': str(error)}

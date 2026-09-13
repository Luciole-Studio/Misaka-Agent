"""Installed-provider Skill metadata through the existing resource-loader owner.

No Hermes plugin manager is created. Hosts declaring metadata.state='installed'
provide activateSkillProvider(namespace); active contributions need no callback.
Withdrawn/disabled contributions never become advertised or explicitly readable.
"""
import asyncio
import inspect

from misaka.utils.async_lifecycle import run_in_thread, settle

from .layers import extension_resources, extension_roots, parse_skill_name


async def prepare(loader, name, activated, source=None):
    if not isinstance(name, str) or ':' not in name:
        return
    namespace, _ = parse_skill_name(name)
    candidates = []
    for resource in extension_resources(loader):
        if not isinstance(resource, dict):
            continue
        metadata = resource.get('metadata', {})
        if source:
            from pathlib import Path
            if not Path(source).absolute().is_relative_to(Path(resource['path']).absolute()):
                continue
        labelled = {**resource, 'metadata': {**metadata, 'enabled': True, 'available': True, 'state': 'active'}}
        roots = list(extension_roots([labelled]))
        if not roots or roots[0][0] != 'extension:' + namespace:
            continue
        candidates.append(metadata)
    if len(candidates) > 1:
        raise ValueError('Ambiguous Skill provider; choose an exact source: ' + namespace)
    for metadata in candidates:
        state = metadata.get('state', 'active')
        if metadata.get('enabled') is False or metadata.get('available') is False or state in ('disabled', 'unavailable', 'withdrawn'):
            activated.discard(namespace)
            raise ValueError('Skill provider is unavailable: ' + namespace)
        if state != 'installed' or namespace in activated:
            return
        activate = getattr(loader, 'activateSkillProvider', None)
        if activate is None:
            raise ValueError('Installed Skill provider has no activation owner: ' + namespace)
        if inspect.iscoroutinefunction(activate):
            result, cancelled = await settle(asyncio.create_task(activate(namespace)))
            if cancelled is not None:
                raise cancelled
        else:
            result = await run_in_thread(activate, namespace)
            if inspect.isawaitable(result):
                result, cancelled = await settle(asyncio.ensure_future(result))
                if cancelled is not None:
                    raise cancelled
        if result is False:
            raise ValueError('Skill provider activation failed: ' + namespace)
        activated.add(namespace)
        return
    activated.discard(namespace)  # Withdrawal also invalidates prior successful activation.

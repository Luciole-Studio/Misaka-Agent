"""Port of CCB services/mcp/headersHelper.ts (77a7934e15d69da13879112ed7db695c9ee7a52a).

MISAKA uses its own environment names and retains project trust in headless mode.
Failure is non-blocking, as upstream; do not log helper output or credentials.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os


async def server_headers(name: str, config: dict) -> dict[str, str]:
    headers = dict(config.get('headers') or {})
    helper = config.get('headersHelper')
    if not helper:
        return headers
    if config.get('scope') in {'project', 'local'} and os.environ.get('MISAKA_PROJECT_TRUST') != '1':
        logging.getLogger(__name__).warning('MCP %s headersHelper skipped: project trust not established', name)
        return headers
    from misaka.core.subagent.runtime import _run

    env = {**os.environ, 'MISAKA_MCP_SERVER_NAME': name, 'MISAKA_MCP_SERVER_URL': config['url']}
    try:
        code, output, _ = await asyncio.wait_for(
            _run(['/bin/sh', '-c', helper], cwd=config.get('cwd'), env=env), 10,
        )
        if code or not output.strip():
            raise ValueError('helper failed')
        dynamic = json.loads(output)
        if not isinstance(dynamic, dict) or any(not isinstance(value, str) for value in dynamic.values()):
            raise ValueError('helper must return string headers')
        headers.update(dynamic)
    except (OSError, ValueError, TimeoutError):
        logging.getLogger(__name__).warning('MCP %s headersHelper failed; using static headers', name)
    return headers

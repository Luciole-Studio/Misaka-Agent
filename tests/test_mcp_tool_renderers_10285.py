"""pi coding-agent's ``test/suite/regressions/10285-mcp-tool-renderers.test.ts``."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from types import SimpleNamespace

from misaka.core.session_manager import SessionManager
from misaka.extensions.mcp import createMcpExtension
from misaka.modes.interactive.theme.theme import initTheme, theme
from misaka.utils.ansi import stripAnsi
from misaka.utils.values import read_field
from tests.suite_harness import createHarness

mcpExtension = createMcpExtension({"loadConfig": lambda _ctx: {"servers": [], "errors": []}})


# Regression #10285: a resumed session renders MCP tool calls before their server connected, if it
# ever does. They render with the MCP renderers anyway, instead of the expanded fallback.
async def test_renders_calls_to_mcp_tools_that_are_not_registered():
    initTheme("dark")
    async with createHarness(extensionFactories=[mcpExtension]) as harness:

        def resolve(toolName: str):
            return harness.session.extensionRunner.resolveToolRenderers(
                toolName, lambda: harness.session.getToolDefinition(toolName)
            )

        renderCall = read_field(resolve("mcp__my_docs__search"), "renderCall")
        call = renderCall({"query": "pi"}, theme, SimpleNamespace(expanded=False, lastComponent=None))
        assert 'my_docs/search query="pi"' in stripAnsi("\n".join(call.render(100)))
        assert resolve("not_mcp") is None
        # Registered tools keep their own renderers.
        assert read_field(resolve("read"), "renderCall") is harness.session.getToolDefinition("read").renderCall


async def test_renders_them_in_html_exports_too(tmp_path):
    initTheme("dark")
    sessionManager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    async with createHarness(extensionFactories=[mcpExtension], sessionManager=sessionManager) as harness:
        sessionManager.appendMessage({"role": "user", "content": "search", "timestamp": 1})
        sessionManager.appendMessage({
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "call-1", "name": "mcp__my_docs__search", "arguments": {"query": "pi"}}],
            "api": "anthropic-messages",
            "provider": "anthropic",
            "model": "test",
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": "toolUse",
            "timestamp": 2,
        })

        html = Path(await harness.session.exportToHtml(str(tmp_path / "export.html"))).read_text(encoding="utf-8")
        match = re.search(r'<script id="session-data" type="application/json">([^<]*)</script>', html)
        session = json.loads(base64.b64decode(match.group(1) if match else "").decode("utf-8"))
        assert "my_docs/search" in stripAnsi(((session.get("renderedTools") or {}).get("call-1") or {}).get("callHtml") or "")

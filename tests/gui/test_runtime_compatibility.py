"""The browser host must assemble a real session through the current core APIs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_provider_verification_uses_the_configured_runtime(self):
        from misaka.ui.gui.settings_worker import op_verify

        model = SimpleNamespace(provider="fixture", id="fixture-model")
        complete = AsyncMock(return_value=SimpleNamespace(
            stopReason="stop", content=[SimpleNamespace(text="OK")],
        ))
        registry = SimpleNamespace(find=lambda provider, model_id: model, completeSimple=complete)
        with patch("misaka.ui.gui.settings_worker._runtime", return_value=SimpleNamespace(registry=registry)):
            result = op_verify({"provider": model.provider, "model": model.id})
        self.assertIn("OK", result["message"])
        complete.assert_awaited_once()
        self.assertIs(complete.call_args.args[0], model)

    def test_headless_host_starts_with_the_current_core(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, home = root / "workspace", root / "home"
            workspace.mkdir()
            home.mkdir()
            (home / "settings.json").write_text(json.dumps({
                "defaultProvider": "fixture", "defaultModel": "fixture-model",
            }), encoding="utf-8")
            (home / "models.json").write_text(json.dumps({"providers": {"fixture": {
                "api": "openai-completions", "baseUrl": "http://127.0.0.1:9/v1", "apiKey": "test-secret",
                "models": [{"id": "fixture-model", "name": "Fixture", "contextWindow": 32768, "maxTokens": 64}],
            }}}), encoding="utf-8")
            script = """
import asyncio
import sys
from unittest.mock import patch
from misaka.ui.gui.chat_runner import ChatHost

async def check():
    host = ChatHost({"workspace": sys.argv[1]})
    with patch("misaka.ui.gui.chat_runner.emit") as emit:
        try:
            await host.start()
            assert host.session.model.provider == "fixture"
            assert host.session.model.id == "fixture-model"
            assert any(call.args[0].get("type") == "ready" for call in emit.call_args_list)
        finally:
            await host._dispose()

asyncio.run(check())
"""
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", script, str(workspace)],
                cwd=Path(__file__).resolve().parents[2],
                env={**os.environ, "MISAKA_HOME": str(home), "MISAKA_OFFLINE": "1"},
                capture_output=True, text=True, encoding="utf-8", timeout=45, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

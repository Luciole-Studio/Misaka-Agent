"""Translated from pi's ``packages/ai/test/models-entry.test.ts`` (v1.1.0).

MISAKA fork: Node's module ``load`` hook becomes a look at ``sys.modules`` after a faux completion in a fresh
interpreter: no vendor SDK and neither the generated catalog nor a provider data module is loaded. (TypeBox has no
Python counterpart.)
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

FORBIDDEN = ["anthropic", "openai", "google.genai", "boto3", "misaka.ai.models_generated", "misaka.ai.providers.all"]


def test_runs_a_faux_completion_without_catalogs_or_sdks_through_the_models_entry():
    script = textwrap.dedent(
        f"""
        import asyncio, json, sys
        from misaka.ai.models import createModels, createProvider
        from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider
        from misaka.ai.types import Context

        assert callable(createProvider)
        models = createModels()
        assert models.getModels() == []
        assert models.getProviders() == []
        faux = fauxProvider()
        models.setProvider(faux.provider)
        faux.setResponses([fauxAssistantMessage("OK")])
        response = asyncio.run(models.completeSimple(faux.getModel(), Context(messages=[])))
        assert response.stopReason == "stop"
        assert [block.model_dump(exclude_none=True) for block in response.content] == [{{"type": "text", "text": "OK"}}]
        loaded = [name for name in {json.dumps(FORBIDDEN)} if name in sys.modules]
        loaded += [name for name in sys.modules if name.startswith("misaka.ai.providers.") and name.endswith("_models")]
        print(json.dumps(loaded))
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []

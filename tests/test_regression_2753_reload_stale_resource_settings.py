"""Translated from pi's ``packages/coding-agent/test/suite/regressions/2753-reload-stale-resource-settings.test.ts``
(v1.1.0)."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import tempfile
from typing import Any

from misaka.ai.auth.types import ApiKeyCredential
from misaka.ai.compat import registerFauxProvider
from misaka.core.agent_session_runtime import (
    CreateAgentSessionRuntimeResult,
    createAgentSessionRuntime,
)
from misaka.core.agent_session_services import (
    createAgentSessionFromServices,
    createAgentSessionServices,
)
from misaka.core.auth_storage import AuthStorage
from misaka.core.model_runtime import ModelRuntime
from misaka.core.session_manager import SessionManager
from misaka.utils.values import read_field


async def test_applies_updated_top_level_prompt_settings_on_reload_after_startup():
    tempDir = tempfile.mkdtemp(prefix="pi-2753-")
    agentDir = os.path.join(tempDir, "agent")
    promptsDir = os.path.join(agentDir, "prompts")
    os.makedirs(promptsDir, exist_ok=True)
    pathlib.Path(promptsDir, "test.md").write_text("Echo test prompt\n", encoding="utf-8")

    faux = registerFauxProvider({"models": [{"id": "faux-1", "reasoning": False}]})
    authStorage = AuthStorage.inMemory()

    async def apiKey(_current: Any) -> ApiKeyCredential:
        return ApiKeyCredential(type="api_key", key="faux-key")

    await authStorage.modify(faux.getModel().provider, apiKey)
    modelRuntime = await ModelRuntime.create({"authStorage": authStorage, "modelsPath": os.path.join(agentDir, "models.json")})

    def registerFaux(pi: Any) -> None:
        pi.registerProvider(faux.getModel().provider, {
            "baseUrl": faux.getModel().baseUrl,
            "apiKey": "faux-key",
            "api": faux.api,
            "models": [
                {
                    "id": registeredModel.id,
                    "name": registeredModel.name,
                    "api": registeredModel.api,
                    "reasoning": registeredModel.reasoning,
                    "input": registeredModel.input,
                    "cost": registeredModel.cost,
                    "contextWindow": registeredModel.contextWindow,
                    "maxTokens": registeredModel.maxTokens,
                }
                for registeredModel in faux.models
            ],
        })

    async def createRuntime(options: Any) -> CreateAgentSessionRuntimeResult:
        services = await createAgentSessionServices({
            "cwd": options["cwd"],
            "agentDir": agentDir,
            "modelRuntime": modelRuntime,
            "resourceLoaderOptions": {"extensionFactories": [registerFaux], "noSkills": True, "noThemes": True},
        })
        result = await createAgentSessionFromServices({
            "services": services,
            "sessionManager": options["sessionManager"],
            "sessionStartEvent": options.get("sessionStartEvent"),
            "model": faux.getModel(),
        })
        return CreateAgentSessionRuntimeResult(
            session=result["session"],
            services=services,
            diagnostics=services.diagnostics,
            extensionsResult=result.get("extensionsResult"),
            modelFallbackMessage=result.get("modelFallbackMessage"),
        )

    runtime = await createAgentSessionRuntime(
        createRuntime, {"cwd": tempDir, "agentDir": agentDir, "sessionManager": SessionManager.create(tempDir)}
    )
    try:
        assert "test" in [read_field(prompt, "name") for prompt in runtime.session.promptTemplates]

        pathlib.Path(agentDir, "settings.json").write_text(
            f"{json.dumps({'prompts': ['-prompts/test.md']}, indent=2)}\n", encoding="utf-8"
        )

        await runtime.session.reload()

        assert runtime.services.settingsManager.getGlobalSettings().get("prompts") == ["-prompts/test.md"]
        assert "test" not in [read_field(prompt, "name") for prompt in runtime.session.promptTemplates]
    finally:
        runtime.session.dispose()
        faux.unregister()
        shutil.rmtree(tempDir, ignore_errors=True)

"""Provider shortlists and catalogue imports, with isolated storage and mocked upstreams."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from misaka.ui.gui import settings_worker as worker
from misaka.ui.gui.model_preferences import visible_models


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        env = patch.dict(os.environ, {"MISAKA_HOME": str(self.home), "MISAKA_OFFLINE": "1"})
        env.start()
        self.addCleanup(env.stop)
        worker.op_save_custom({"provider": "sample", "api": "openai-completions",
                               "base_url": "http://127.0.0.1:9/v1", "api_key": "test-secret",
                               "models": ["old", "unused"]})

    def test_live_catalog_over_400_imports_only_selected_and_preserves_siblings(self):
        models_file = self.home / "models.json"
        before = json.loads(models_file.read_text(encoding="utf-8"))
        before["note"] = "保留"
        models_file.write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")
        data = {"data": [{"id": f"vendor/m{i}", "name": f"Model {i}", "context_length": 8192,
                           "supported_parameters": ["reasoning"], "top_provider": {"max_completion_tokens": 1024}}
                          for i in range(601)]}
        with patch.object(worker, "_fetch_one", return_value=(200, data)) as fetch:
            fetched = worker.op_fetch_provider_models({"provider": "sample"})
        self.assertEqual(len(fetched["models"]), 601)
        self.assertEqual(fetch.call_args.args[1]["Authorization"], "Bearer test-secret")
        worker.op_save_model_selection({"provider": "sample", "enabled": True, "models": ["vendor/m1", "vendor/m600"]})
        listed = worker.op_models({"configured_only": True})["models"]
        self.assertEqual({m["id"] for m in listed}, {"vendor/m1", "vendor/m600"})
        self.assertEqual(listed[0]["contextWindow"], 8192)
        registry = worker._runtime().registry
        self.assertEqual(registry.find("sample", "vendor/m1").maxTokens, 1024)
        self.assertTrue(registry.find("sample", "vendor/m1").reasoning)
        saved = json.loads(models_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["note"], "保留")
        self.assertEqual(saved["providers"]["sample"]["apiKey"], "test-secret")
        self.assertEqual(len(saved["providers"]["sample"]["models"]), 4)
        reopened = worker.op_provider_models({"provider": "sample"})
        self.assertEqual(len(reopened["models"]), 601)
        self.assertEqual(set(reopened["selected"]), {"vendor/m1", "vendor/m600"})

    def test_disable_clear_and_provider_identity(self):
        worker.op_save_custom({"provider": "second", "api": "openai-completions", "base_url": "http://127.0.0.1:9/v1",
                               "api_key": "other-secret", "models": ["old"]})
        for provider in ("sample", "second"):
            worker.op_save_model_selection({"provider": provider, "models": ["old"], "enabled": True})
        self.assertEqual({m["provider"] for m in worker.op_models({"configured_only": True})["models"]}, {"sample", "second"})
        worker.op_save_model_selection({"provider": "sample", "models": ["old"], "enabled": False})
        self.assertEqual([m["provider"] for m in worker.op_models({"configured_only": True})["models"]], ["second"])
        self.assertEqual(worker.op_provider_models({"provider": "sample"})["selected"], ["old"])
        worker.op_save_model_selection({"provider": "second", "models": [], "enabled": True})
        self.assertEqual(worker.op_models({"configured_only": True})["models"], [])

    def test_rejected_fetch_and_unknown_selection_do_not_overwrite_settings(self):
        worker.op_save_model_selection({"provider": "sample", "models": ["old"], "enabled": True})
        path = self.home / "settings.json"
        before = path.read_bytes()
        with patch.object(worker, "_fetch_one", return_value=(401, {"error": "test-secret"})):
            with self.assertRaisesRegex(ValueError, "401"):
                worker.op_fetch_provider_models({"provider": "sample"})
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "未知模型"):
            worker.op_save_model_selection({"provider": "sample", "models": ["made-up"]})
        self.assertEqual(path.read_bytes(), before)

    def test_subscription_catalog_does_not_send_token_to_public_endpoint(self):
        registry = worker._runtime().registry
        model = next(m for m in registry.getAll() if m.provider == "openai-codex")
        with patch.object(worker, "_provider_auth", return_value=(registry, model, {"apiKey": "subscription-secret"})), \
             patch.object(worker, "_fetch_one") as fetch:
            result = worker.op_fetch_provider_models({"provider": "openai-codex"})
        fetch.assert_not_called()
        self.assertIn("内置模型目录", result["message"])
        self.assertTrue(result["models"])

    def test_openrouter_connection_checks_credentials_without_inference(self):
        registry = worker._runtime().registry
        model = next(m for m in registry.getAll() if m.provider == "openrouter")
        with patch.object(worker, "_provider_auth", return_value=(registry, model, {"apiKey": "test-secret"})), \
             patch.object(worker, "_fetch_one", return_value=(200, {"data": {"label": "test"}})) as fetch, \
             patch.object(worker, "op_verify") as infer:
            worker.op_test_provider({"provider": "openrouter"})
        self.assertTrue(fetch.call_args.args[0].endswith("/api/v1/key"))
        infer.assert_not_called()

    def test_unset_preferences_keep_only_existing_defaults_and_pins(self):
        registry = worker._runtime().registry
        with patch("misaka.ui.gui.model_preferences.initial_refs", return_value={"sample/old"}):
            self.assertEqual([m.id for m in visible_models(registry.getAvailable())], ["old"])

    def test_catalog_pagination_and_non_chat_models(self):
        registry = worker._runtime().registry
        model = registry.find("sample", "old").model_copy(update={"api": "google-generative-ai"})
        pages = [(200, {"models": [{"name": "models/chat-a", "supportedGenerationMethods": ["generateContent"]},
                                   {"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "a b"}),
                 (200, {"models": [{"name": "models/chat-b", "supportedGenerationMethods": ["generateContent"]}]})]
        with patch.object(worker, "_provider_auth", return_value=(registry, model, {"apiKey": "test-secret"})), \
             patch.object(worker, "_fetch_one", side_effect=pages) as fetch:
            result = worker.op_fetch_provider_models({"provider": "sample"})
        self.assertEqual({m["id"] for m in result["models"]}, {"chat-a", "chat-b"})
        self.assertIn("pageToken=a+b", fetch.call_args.args[0])

    def test_model_selection_preserves_explicit_dated_ids(self):
        worker.op_save_custom({"provider": "sample", "api": "openai-completions",
                               "base_url": "http://127.0.0.1:9/v1", "api_key": "test-secret",
                               "models": [{"id": "m", "name": "Model"}, {"id": "m-2026-01-01", "name": "Model"}]})
        worker.op_save_model_selection({"provider": "sample", "enabled": True, "models": ["m-2026-01-01"]})
        for params in ({"configured_only": True}, {"provider": "sample", "enabled_only": True}):
            self.assertEqual([m["id"] for m in worker.op_models(params)["models"]], ["m-2026-01-01"])

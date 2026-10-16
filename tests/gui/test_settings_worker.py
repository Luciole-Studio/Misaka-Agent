"""Settings worker against a throwaway MISAKA_HOME. Never touches the real home."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from misaka.ui.gui.services import Settings
from misaka.ui.gui.settings_worker import (
    MARK, op_models, op_models_overview, op_ping_custom, op_probe_custom, op_remove_custom, op_save_custom,
)


class SettingsWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / "home"
        self.workspace = root / "workspace"
        self.home.mkdir()
        self.workspace.mkdir()
        real = Path.home() / ".misaka"
        self.real_settings = real / "settings.json"
        self.real_sisters = real / "profiles" / "sisters"
        self.settings_stamp = self._stamp(self.real_settings)
        self.sister_names = self._names(self.real_sisters)

    def _stamp(self, path: Path):
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    def _names(self, path: Path):
        if not path.is_dir():
            return None
        return sorted(p.name for p in path.iterdir())

    def tearDown(self):
        self.assertEqual(self._stamp(self.real_settings), self.settings_stamp)
        self.assertEqual(self._names(self.real_sisters), self.sister_names)

    def call(self, op, params=None):
        env = {**os.environ, "MISAKA_HOME": str(self.home), "PYTHONUTF8": "1",
               "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1", "TERM": "dumb"}
        request = json.dumps({"op": op, "params": params or {}, "workspace": str(self.workspace)}, ensure_ascii=False)
        done = subprocess.run(
            [sys.executable, "-u", "-X", "utf8", "-m", "misaka.ui.gui.settings_worker"],
            input=request + "\n", capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=self.workspace, env=env, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in reversed(done.stdout.splitlines()):
            if line.startswith(MARK):
                return json.loads(line[len(MARK):])
        tail = "\n".join((done.stderr or "").splitlines()[-8:])
        self.fail(f"worker 没有返回标记行（exit {done.returncode}）：{tail}")

    def test_overview_and_models_read_the_throwaway_home(self):
        overview = self.call("overview")
        self.assertTrue(overview["ok"], overview)
        data = overview["data"]
        self.assertIn("version", data)
        self.assertTrue(data["python_ok"])
        self.assertIsInstance(data["tools"], list)
        self.assertTrue(data["plan_approval"])
        self.assertTrue(str(data["paths"]["roles"]).startswith(str(self.home)))
        models = self.call("models_overview")
        self.assertTrue(models["ok"], models)
        self.assertGreater(len(models["data"]["providers"]), 0)
        self.assertEqual(models["data"]["targets"][0]["label"], "全局默认")

    def test_create_sister_and_plan_approval_roundtrip(self):
        empty = self.call("sisters")
        self.assertTrue(empty["ok"], empty)
        self.assertEqual(empty["data"]["sisters"], [])
        created = self.call("create_sister", {"id": "10032", "specialty": "查找证据"})
        self.assertTrue(created["ok"], created)
        self.assertIn("10032", created["data"]["message"])
        listed = self.call("sisters")
        self.assertEqual([item["id"] for item in listed["data"]["sisters"]], ["10032"])
        self.assertIn("查找证据", listed["data"]["sisters"][0]["description"])
        turned_off = self.call("set_research", {"plan_approval": False})
        self.assertTrue(turned_off["ok"], turned_off)
        self.assertFalse(self.call("overview")["data"]["plan_approval"])
        self.call("set_research", {"plan_approval": True})
        self.assertTrue(self.call("overview")["data"]["plan_approval"])
        saved = json.loads((self.home / "settings.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["research"]["plan_approval"])

    def test_sister_model_pin_crosses_providers_and_can_follow_global_again(self):
        before = self.call("models_overview")["data"]["global"]
        created = self.call("create_sister", {"id": "10087", "model": "anthropic/claude-opus-5"})
        self.assertTrue(created["ok"], created)
        self.assertEqual(self.call("sisters")["data"]["sisters"][0]["model"], "anthropic/claude-opus-5")
        changed = self.call("set_default", {"target": "10087", "provider": "google", "model": "gemini-2.5-flash"})
        self.assertTrue(changed["ok"], changed)
        overview = self.call("models_overview")["data"]
        self.assertEqual(overview["global"], before)
        self.assertEqual(next(t for t in overview["targets"] if t["key"] == "10087")["pinned"], "google/gemini-2.5-flash")
        cleared = self.call("set_default", {"target": "10087"})
        self.assertTrue(cleared["ok"], cleared)
        self.assertEqual(self.call("sisters")["data"]["sisters"][0]["model"], "")

    def test_unknown_op_and_service_call(self):
        failed = self.call("not_an_op")
        self.assertFalse(failed["ok"])
        self.assertIn("未知设置操作", failed["error"])
        with mock.patch.dict(os.environ, {"MISAKA_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}):
            data = Settings().call("sisters", {}, str(self.workspace))
            with self.assertRaisesRegex(ValueError, "未知设置操作"):
                Settings().call("not_an_op", {}, str(self.workspace))
        self.assertEqual(data["sisters"], [])

    def test_terminal_preference_survives_worker_restart(self):
        default = self.call("terminal")
        self.assertTrue(default["ok"], default)
        self.assertEqual(default["data"]["selected"], "auto")
        selected = default["data"]["choices"][-1]["id"]
        saved = self.call("set_terminal", {"terminal": selected})
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(self.call("terminal")["data"]["selected"], selected)
        raw = (self.home / "settings.json").read_bytes()
        self.assertEqual(json.loads(raw.decode("utf-8"))["gui"]["terminal"], selected)
        failed = self.call("set_terminal", {"terminal": "cmd & echo unwanted"})
        self.assertFalse(failed["ok"])
        self.assertEqual((self.home / "settings.json").read_bytes(), raw)

    def test_web_overview_is_readable(self):
        web = self.call("web_overview")
        self.assertTrue(web["ok"], web)
        self.assertIn("search", web["data"]["resolved"])
        self.assertIn("extract", web["data"]["resolved"])
        self.assertIsInstance(web["data"]["providers"], list)


class _CatalogHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = self.server.state
        path = self.path.split("?", 1)[0]
        state["seen"].append((path, {key.lower(): value for key, value in self.headers.items()}))
        status, headers, body = state["routes"].get(path, (404, {}, b""))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class CustomModelSettingsTests(unittest.TestCase):
    """Custom services write only the throwaway models.json and talk only to 127.0.0.1."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        real = Path.home() / ".misaka"
        self.real_settings = real / "settings.json"
        self.real_sisters = real / "profiles" / "sisters"
        self.settings_stamp = self._stamp(self.real_settings)
        self.sister_names = self._names(self.real_sisters)
        self.env = mock.patch.dict(os.environ, {"MISAKA_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CatalogHandler)
        self.httpd.state = {"seen": [], "routes": {}}
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

        def stop_server():
            self.httpd.shutdown()
            self.httpd.server_close()

        self.addCleanup(stop_server)

    def tearDown(self):
        self.assertEqual(self._stamp(self.real_settings), self.settings_stamp)
        self.assertEqual(self._names(self.real_sisters), self.sister_names)

    def _stamp(self, path: Path):
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    def _names(self, path: Path):
        if not path.is_dir():
            return None
        return sorted(item.name for item in path.iterdir())

    @property
    def origin(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def models_path(self) -> Path:
        return self.home / "models.json"

    def read_models(self) -> dict:
        return json.loads(self.models_path().read_text(encoding="utf-8"))

    def reset_routes(self):
        self.httpd.state["seen"].clear()
        self.httpd.state["routes"].clear()

    def route(self, path, status=200, body=b"", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.httpd.state["routes"][path] = (status, headers or {}, body)

    def seed(self):
        self.models_path().write_text(
            """
            {
              "note": "keep-me",
              "providers": {
                // sibling stays
                "other-gateway": {
                  "baseUrl": "http://127.0.0.1:9/v1",
                  "api": "openai-completions",
                  "apiKey": "sibling-secret-value",
                  "headers": {"X-Keep": "yes"},
                  "models": [{"id": "kept-model"}]
                },
                "anthropic": {"apiKey": "overlay-secret-value"}
              }
            }
            """,
            encoding="utf-8",
        )

    def test_save_update_and_remove_keep_the_rest_of_the_file(self):
        self.seed()
        saved = op_save_custom({
            "provider": "ollama", "name": "本机 Ollama", "api": "openai-completions",
            "base_url": "http://127.0.0.1:9/v1", "api_key": "kept-secret-value",
            "models": [{"id": "llama3", "name": "Llama 3", "contextWindow": 8192, "reasoning": False}, "llama3"],
        })
        self.assertIn("ollama", saved["message"])
        document = self.read_models()
        self.assertEqual(document["note"], "keep-me")
        self.assertEqual(document["providers"]["other-gateway"]["apiKey"], "sibling-secret-value")
        self.assertEqual(document["providers"]["other-gateway"]["headers"], {"X-Keep": "yes"})
        self.assertEqual(document["providers"]["anthropic"]["apiKey"], "overlay-secret-value")
        ollama = document["providers"]["ollama"]
        self.assertEqual(ollama["apiKey"], "kept-secret-value")
        self.assertEqual(ollama["models"], [{"id": "llama3", "name": "Llama 3", "contextWindow": 8192, "reasoning": False}])
        self.assertNotIn("// sibling", self.models_path().read_text(encoding="utf-8"))

        updated = op_save_custom({
            "provider": "ollama", "name": "本机 Ollama", "api": "openai-completions",
            "base_url": "http://127.0.0.1:9/v1/", "api_key": "",
            "models": ["qwen"],
        })
        self.assertIn("1 个模型", updated["message"])
        ollama = self.read_models()["providers"]["ollama"]
        self.assertEqual(ollama["apiKey"], "kept-secret-value")
        self.assertEqual(ollama["baseUrl"], "http://127.0.0.1:9/v1")
        self.assertEqual(ollama["models"], [{"id": "qwen"}])

        overview = op_models_overview({})
        dumped = json.dumps(overview, ensure_ascii=False)
        for secret in ("kept-secret-value", "sibling-secret-value", "overlay-secret-value"):
            self.assertNotIn(secret, dumped)
        custom = {item["id"]: item for item in overview["custom"]["services"]}
        self.assertEqual(custom["ollama"]["key_hint"], "已保存在本机")
        self.assertEqual(custom["ollama"]["models"], [{"id": "qwen"}])
        self.assertNotIn("anthropic", custom)
        listed = {item["id"]: item for item in overview["providers"]}
        self.assertTrue(listed["ollama"]["custom"])
        self.assertEqual(listed["ollama"]["source"], "自定义服务")
        self.assertEqual(listed["ollama"]["name"], "本机 Ollama")
        self.assertTrue(listed["ollama"]["configured"])
        self.assertIn("qwen", [item["id"] for item in op_models({"provider": "ollama"})["models"]])

        removed = op_remove_custom({"provider": "ollama"})
        self.assertIn("ollama", removed["message"])
        self.assertNotIn("ollama", self.read_models()["providers"])
        self.assertIn("other-gateway", self.read_models()["providers"])
        with self.assertRaisesRegex(ValueError, "没有这个自定义服务"):
            op_remove_custom({"provider": "anthropic"})
        self.assertEqual(self.read_models()["providers"]["anthropic"]["apiKey"], "overlay-secret-value")

    def test_save_rejects_builtin_ids_bad_urls_and_a_broken_file(self):
        with self.assertRaisesRegex(ValueError, "内置服务商"):
            op_save_custom({"provider": "anthropic", "api": "anthropic-messages",
                            "base_url": "https://api.anthropic.com", "api_key": "local", "models": ["claude"]})
        with self.assertRaisesRegex(ValueError, "http 或 https"):
            op_save_custom({"provider": "local", "api": "openai-completions",
                            "base_url": "file:///C:/Windows", "api_key": "local", "models": ["m"]})
        with self.assertRaisesRegex(ValueError, "不要把密钥写进地址"):
            op_save_custom({"provider": "local", "api": "openai-completions",
                            "base_url": "http://user:pass@127.0.0.1:9/v1", "api_key": "local", "models": ["m"]})
        with self.assertRaisesRegex(ValueError, "请填写 API Key"):
            op_save_custom({"provider": "local", "api": "openai-completions",
                            "base_url": "http://127.0.0.1:9/v1", "api_key": "", "models": ["m"]})
        self.assertFalse(self.models_path().exists())
        self.models_path().write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "无法解析"):
            op_save_custom({"provider": "local", "api": "openai-completions",
                            "base_url": "http://127.0.0.1:9/v1", "api_key": "local", "models": ["m"]})
        self.assertEqual(self.models_path().read_text(encoding="utf-8"), "{")

    def test_command_key_is_stored_verbatim_and_not_shown(self):
        op_save_custom({
            "provider": "cmd-gateway", "api": "openai-completions", "base_url": "http://127.0.0.1:9/v1",
            "api_key": "!echo secret-from-command", "models": ["m"],
        })
        self.assertEqual(self.read_models()["providers"]["cmd-gateway"]["apiKey"], "!echo secret-from-command")
        service = op_models_overview({})["custom"]["services"][0]
        self.assertEqual(service["key_hint"], "由本机命令提供")
        self.assertNotIn("secret-from-command", json.dumps(service))

    def test_catalog_auth_redirects_and_provider_headers(self):
        secret = "secret-token-value"
        self.route("/v1/models", 200, {"data": [{"id": "llama3"}]})
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
                                           "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""}):
            found = op_probe_custom({"api": "openai-completions", "base_url": self.origin + "/v1", "api_key": secret})
        self.assertEqual(found["models"], [{"id": "llama3", "name": "llama3"}])
        self.assertEqual(self.httpd.state["seen"][0][1]["authorization"], "Bearer " + secret)

        self.reset_routes()
        self.route("/v1/models", 401, {"error": "no"})
        self.route("/models", 200, {"data": [{"id": "should-not-appear"}]})
        with self.assertRaisesRegex(ValueError, "密钥被拒绝"):
            op_probe_custom({"api": "anthropic-messages", "base_url": self.origin, "api_key": secret})
        self.assertEqual([path for path, _headers in self.httpd.state["seen"]], ["/v1/models"])

        self.reset_routes()
        self.route("/v1/models", 302, b"", {"Location": "/v1/catalog"})
        self.route("/v1/catalog", 200, {"data": [{"id": "after-redirect"}]})
        found = op_probe_custom({"api": "openai-completions", "base_url": self.origin + "/v1", "api_key": secret})
        self.assertEqual(found["models"][0]["id"], "after-redirect")

        self.reset_routes()
        self.route("/v1/models", 302, b"", {"Location": "http://example.com/v1/models"})
        with self.assertRaisesRegex(ValueError, "另一个主机"):
            op_probe_custom({"api": "openai-completions", "base_url": self.origin + "/v1", "api_key": secret})

        self.reset_routes()
        self.route("/v1beta/models", 200, {"models": [{"name": "models/gemini-2.0-flash", "displayName": "Gemini Flash"}]})
        found = op_probe_custom({"api": "google-generative-ai", "base_url": self.origin + "/v1beta", "api_key": "goog-key"})
        self.assertEqual(found["models"], [{"id": "gemini-2.0-flash", "name": "Gemini Flash"}])
        self.assertEqual(self.httpd.state["seen"][0][1]["x-goog-api-key"], "goog-key")
        self.assertNotIn("authorization", self.httpd.state["seen"][0][1])

        self.reset_routes()
        self.route("/v1/models", 404, {})
        self.route("/models", 200, {"data": [{"id": "claude-custom", "display_name": "Custom Claude"}]})
        found = op_probe_custom({"api": "anthropic-messages", "base_url": self.origin, "api_key": "anth-key"})
        self.assertEqual(found["models"], [{"id": "claude-custom", "name": "Custom Claude"}])
        self.assertEqual([path for path, _headers in self.httpd.state["seen"]], ["/v1/models", "/models"])
        headers = self.httpd.state["seen"][1][1]
        self.assertEqual(headers["x-api-key"], "anth-key")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")

        self.reset_routes()
        empty = op_probe_custom({"api": "openai-completions", "base_url": self.origin + "/v1", "api_key": ""})
        self.assertEqual(empty["models"], [])
        self.assertIn("没有模型列表", empty["message"])

        self.reset_routes()
        self.route("/v1/models", 200, b"not-json")
        plain = op_probe_custom({"api": "openai-completions", "base_url": self.origin + "/v1", "api_key": ""})
        self.assertEqual(plain["models"], [])
        self.assertIn("不是模型列表", plain["message"])

    def test_stored_env_key_is_resolved_only_for_the_request(self):
        op_save_custom({
            "provider": "env-gateway", "api": "openai-completions", "base_url": self.origin + "/v1",
            "api_key": "$MISAKA_CUSTOM_PROBE_KEY", "models": ["m"],
        })
        self.route("/v1/models", 200, {"data": [{"id": "from-env"}]})
        with mock.patch.dict(os.environ, {"MISAKA_CUSTOM_PROBE_KEY": "resolved-secret-value"}):
            found = op_probe_custom({"provider": "env-gateway", "api": "openai-completions",
                                     "base_url": self.origin + "/v1", "api_key": ""})
        self.assertEqual(found["models"][0]["id"], "from-env")
        self.assertEqual(self.httpd.state["seen"][0][1]["authorization"], "Bearer resolved-secret-value")
        self.assertEqual(self.read_models()["providers"]["env-gateway"]["apiKey"], "$MISAKA_CUSTOM_PROBE_KEY")
        overview = json.dumps(op_models_overview({}), ensure_ascii=False)
        self.assertNotIn("resolved-secret-value", overview)
        self.assertIn("$MISAKA_CUSTOM_PROBE_KEY", overview)

    def test_ping_uses_the_typed_model_and_scrubs_the_key(self):
        secret = "sk-test-secret-0123456789"
        captured = {}

        async def ok(model, context, options=None):
            captured["model"] = model
            captured["key"] = options.apiKey
            captured["max_tokens"] = options.maxTokens
            return SimpleNamespace(stopReason="stop", content=[SimpleNamespace(text="OK")])

        with mock.patch("misaka.ai.stream.complete_simple", ok):
            result = op_ping_custom({"api": "openai-completions", "base_url": self.origin + "/v1",
                                     "api_key": secret, "model": "llama3"})
        self.assertEqual(result["message"], "模型已回复：OK")
        self.assertEqual(captured["model"].provider, "custom")
        self.assertEqual(captured["model"].baseUrl, self.origin + "/v1")
        self.assertEqual(captured["model"].id, "llama3")
        self.assertEqual(captured["key"], secret)
        self.assertEqual(captured["max_tokens"], 32)

        async def boom(model, context, options=None):
            raise RuntimeError(f"upstream said {options.apiKey}")

        with mock.patch("misaka.ai.stream.complete_simple", boom):
            with self.assertRaises(ValueError) as caught:
                op_ping_custom({"api": "openai-completions", "base_url": self.origin + "/v1",
                                "api_key": secret, "model": "llama3"})
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("******", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

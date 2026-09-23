"""Offline model isolation at role-worker boundaries; no live profiles or sessions."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from misaka.config import home
from misaka.core.network import sister_runtime, worker
from misaka.core.subagent.model import resolve_model_spec
from misaka.core.subagent.runtime import RoleContext


class Registry:
    def __init__(self):
        self.unauthenticated = set()
        self.models = [SimpleNamespace(provider=provider, id=model) for provider, model in (
            ("lo-provider", "lo-model"), ("sister-provider", "sister-model"),
            ("sister-provider", "shared"), ("other-provider", "shared"),
            ("other-provider", "other-model"), ("sister-provider", "claude-sonnet-4-5"),
            ("sister-provider", "family/model"),
        )]

    def getAll(self):
        return list(self.models)

    def getAvailable(self):
        return [model for model in self.models if self.hasConfiguredAuth(model)]

    def find(self, provider, model_id):
        return next((model for model in self.models if model.provider == provider and model.id == model_id), None)

    def hasConfiguredAuth(self, model):
        return model.provider not in self.unauthenticated


class WorkerModelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.profile = self.root / "profiles" / "10032"
        self.profile.mkdir(parents=True)
        self.registry = Registry()
        self.session = SimpleNamespace(
            model=SimpleNamespace(provider="lo-provider", id="lo-model"),
            modelRegistry=self.registry,
        )
        self.cfg = {"profiles_root": str(self.profile.parent), "provider": "other-provider",
                    "default_model": "other-model", "db": str(self.root / "board.db")}
        self.row = {"id": "card-1", "assignee": "10032", "model": None, "output_dir": None,
                    "generation": 1, "claim_lock": "fixture", "body": "Read the fixture"}
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MISAKA_SUBAGENT_MODEL", None)
        for target, value in (
            ("misaka.core.network.worker.task_store.task_state_dir", str(self.root / "state")),
            ("misaka.config.sessions.card_session_dir", str(self.root / "session")),
            ("misaka.config.profiles.shared_soul", str(self.root / "MISAKA.md")),
            ("misaka.config.identity.prompt_sections", []),
            ("misaka.core.skills.sandbox.snapshot_stack", None),
        ):
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        assembly = patch("misaka.core.wiring.assemble", side_effect=lambda spec: SimpleNamespace(spec=spec))
        assembly.start()
        self.addCleanup(assembly.stop)

    def pin(self, reference):
        provider, _, model = reference.partition("/")
        (self.profile / "settings.json").write_text(json.dumps(
            {"defaultProvider": provider, "defaultModel": model, "other": "retained"}))

    def role_entry(self, model=None, **kwargs):
        from misaka.core.wiring import role_session_setup
        return role_session_setup(str(self.profile), str(self.root), model=model, **kwargs)

    def card(self, model=None):
        return worker.card_session_setup({**self.row, "model": model}, str(self.root / "work"),
                                         str(self.profile), "lo-provider", "lo-model")

    def manager(self, model=None):
        return sister_runtime._SisterManager(self.session, self.cfg, {**self.row, "model": model},
                                             str(self.root))

    def test_role_and_card_defaults_are_settings_not_cli_overrides(self):
        self.pin("sister-provider/sister-model")
        for result in (self.role_entry(), self.card()):
            flags, assembly = result[:2]
            self.assertNotIn("--model", flags)
            self.assertNotIn("--provider", flags)
            self.assertEqual(assembly.spec.profile_dir, str(self.profile))

    def test_explicit_models_reach_engine_without_wrong_provider(self):
        for model in ("other-provider/other-model", "sister-model", "family/model"):
            for result in (self.role_entry(model), self.card(model)):
                flags = result[0]
                self.assertEqual(flags[flags.index("--model") + 1], model)
                self.assertNotIn("--provider", flags)

    def test_resumed_role_session_does_not_receive_a_role_model_override(self):
        self.pin("sister-provider/sister-model")
        session_file = str(self.root / "old.jsonl")
        flags = self.role_entry()[0] + ["--session-dir", str(self.root), "--session", session_file]
        self.assertEqual(flags[flags.index("--session") + 1], session_file)
        self.assertNotIn("--model", flags)

    def test_sister_uses_own_pair_not_lo_provider(self):
        for reference in ("sister-provider/sister-model", "sister-provider/family/model"):
            with self.subTest(reference=reference):
                self.pin(reference)
                manager = self.manager()
                self.assertEqual(manager.model, reference)
                self.assertEqual(manager.role_context.model_override, reference)

    def test_unpinned_sister_uses_global_pair_not_lo_current_model(self):
        self.assertEqual(self.manager().model, "other-provider/other-model")

    def test_an_unpinned_role_follows_the_global_pair_not_lo_current_model(self):
        (self.profile / "settings.json").write_text(json.dumps({"other": "retained"}))
        self.assertEqual(self.manager().model, "other-provider/other-model")

    def test_half_a_pin_fails_instead_of_borrowing_lo_provider(self):
        (self.profile / "settings.json").write_text(json.dumps({"defaultModel": "shared"}))
        with self.assertRaisesRegex(ValueError, "go together"):
            self.manager()

    def test_missing_auth_fails_before_parent_provider_can_capture_pin(self):
        self.pin("sister-provider/sister-model")
        self.registry.unauthenticated.add("sister-provider")
        with self.assertRaisesRegex(ValueError, "authentication.*sister-provider/sister-model"):
            self.manager()
        self.assertEqual(self.manager("other-provider/other-model").model, "other-provider/other-model")

    def test_malformed_profile_fails_instead_of_using_global_default(self):
        (self.profile / "settings.json").write_text("not json")
        with self.assertRaises(ValueError):
            self.manager()

    def test_complete_override_does_not_read_broken_role_default(self):
        for raw in ("not json", '{"defaultModel":"shared"}',
                    '{"defaultProvider":"sister-provider","defaultModel":"removed-model"}'):
            with self.subTest(raw=raw):
                (self.profile / "settings.json").write_text(raw)
                self.assertEqual(self.manager("other-provider/other-model").model, "other-provider/other-model")
                with self.assertRaises(ValueError):
                    self.manager("inherit")

    def test_card_override_preserves_alias_inherit_and_role_endpoint(self):
        self.pin("sister-provider/sister-model")
        for override, expected in (
            ("inherit", "sister-provider/sister-model"),
            ("sonnet", "sister-provider/claude-sonnet-4-5"),
            ("shared", "sister-provider/shared"),
            ("other-provider/other-model", "other-provider/other-model"),
        ):
            with self.subTest(override=override):
                self.assertEqual(self.manager(override).model, expected)

    def test_sister_override_does_not_become_nested_agent_global_override(self):
        self.pin("sister-provider/sister-model")
        manager = self.manager()
        child_env = manager.child_env_extra(None)
        self.assertNotIn("MISAKA_SUBAGENT_MODEL", child_env)
        with patch.dict(os.environ, child_env):
            context = RoleContext.capture(profile_dir=str(self.profile), workspace=str(self.root))
        self.assertIsNone(context.model_override)
        self.assertEqual(resolve_model_spec({}, None, "other-provider/other-model",
                                            {"provider": "sister-provider", "id": "sister-model"},
                                            self.registry.getAvailable()),
                         ("other-provider", "other-model"))


class MoADefaultModelTests(unittest.TestCase):
    def test_default_slots_decode_canonical_references_without_a_registry(self):
        from misaka.core.moa import provider as moa

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"MISAKA_HOME": directory}), \
                patch("misaka.ai.models.get_providers", return_value=["builtin-provider", "global-provider"]), \
                patch("misaka.core.model_registry.ModelRegistry", side_effect=AssertionError("recursive registry")):
            home.path("models").parent.mkdir(parents=True, exist_ok=True)
            home.path("models").write_text(json.dumps({"providers": {"custom-provider": {}}}))
            for reference, expected in (
                ("builtin-provider/role-model", {"provider": "builtin-provider", "model": "role-model"}),
                ("custom-provider/family/role-model", {"provider": "custom-provider", "model": "family/role-model"}),
                ("legacy-role-model", {"provider": "global-provider", "model": "legacy-role-model"}),
                ("family/raw-model", {"provider": "global-provider", "model": "family/raw-model"}),
                ("inherit", {"provider": "global-provider", "model": "global-model"}),
            ):
                cfg = {"provider": "global-provider", "default_model": "global-model", "lo_model": reference}
                with self.subTest(reference=reference), patch.object(moa, "current_config", return_value=cfg):
                    refs, aggregator = moa._default_slots()
                    self.assertEqual(refs, [expected, {"provider": "global-provider", "model": "global-model"}])
                    self.assertEqual(aggregator, expected)

    def test_global_pair_keeps_raw_slash_id_even_when_prefix_is_a_provider(self):
        from misaka.core.moa import provider as moa

        cfg = {"provider": "global-provider", "default_model": "builtin-provider/raw-model",
               "lo_model": "builtin-provider/raw-model"}
        with patch.object(moa, "current_config", return_value=cfg), \
                patch("misaka.ai.models.get_providers", return_value=["builtin-provider"]):
            refs, aggregator = moa._default_slots()
        expected = {"provider": "global-provider", "model": "builtin-provider/raw-model"}
        self.assertEqual(refs, [expected, expected])
        self.assertEqual(aggregator, expected)

    def test_explicit_moa_slots_remain_untouched(self):
        from misaka.core.moa import provider as moa

        cfg = {"provider": "global-provider", "default_model": "global-model", "lo_model": "global-model"}
        raw = {"reference_models": [{"provider": "advisor", "model": "raw/model"}],
               "aggregator": {"provider": "aggregator", "model": "other/raw-model"}}
        original = json.dumps(raw)
        with patch.object(moa, "current_config", return_value=cfg):
            result = moa._normalize_preset(raw)
        self.assertEqual(result["reference_models"], [{**raw["reference_models"][0], "enabled": True}])
        self.assertEqual(result["aggregator"], raw["aggregator"])
        self.assertEqual(json.dumps(raw), original)


if __name__ == "__main__":
    unittest.main()

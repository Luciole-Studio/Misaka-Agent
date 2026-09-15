"""Regression checks for the 200-character skill-index description budget."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from misaka.core.skills import index
from misaka.core.skills.manage import validate_frontmatter
from misaka.core.skills.vendor.learn_prompt import _AUTHORING_STANDARDS
from misaka.core.skills.vendor.manager import SKILL_MANAGE_SCHEMA
from misaka.core.skills.vendor.metadata import (
    SKILL_PROMPT_DESC_LIMIT,
    extract_skill_description,
    is_skill_description_truncated_for_prompt,
)


def skill_text(description):
    return f'---\nname: sample\ndescription: "{description}"\n---\n# Sample\nBody.\n'


class SkillPromptDescriptionTests(unittest.TestCase):
    def test_boundaries_and_prefix(self):
        self.assertEqual(SKILL_PROMPT_DESC_LIMIT, 200)
        for char in ("x", "研"):
            for length in (0, 60, 61, 199, 200, 201, 1000):
                with self.subTest(char=char, length=length):
                    raw = char * length
                    metadata = {"description": raw}
                    expected = raw if length <= 200 else raw[:197] + "..."
                    self.assertEqual(extract_skill_description(metadata), expected)
                    self.assertEqual(is_skill_description_truncated_for_prompt(metadata), length > 200)
        name = "<inline:misaka_lcm>:misaka_lcm"
        entry = {"name": name, "description": "x" * 200, "layer": "extension", "category": "general"}
        self.assertIn(f"- {name}: {'x' * 200}\n", index.render_prompt([entry]))

    def test_old_personal_cache_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            skill = root / "sample" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(skill_text("x" * 250))
            cache = Path(directory) / "cache"
            cache.mkdir()
            with patch.object(index, "_snapshot_dir", return_value=str(cache)):
                manifest = index._manifest(root)
                scanned = index._scan_root(root)
                scanned["skills"][0]["description"] = "x" * 57 + "..."
                Path(index._snapshot_path(root)).write_text(json.dumps({
                    "version": 6, "manifest": manifest, "real_root": str(root.resolve()), **scanned,
                }))
                self.assertIsNone(index._load_snapshot(root, manifest))
                current = index._layer("role", root)
                self.assertEqual(current["skills"][0]["description"], "x" * 197 + "...")
                self.assertEqual(index._load_snapshot(root, manifest)["version"], index.SNAPSHOT_VERSION)

    def test_sealed_snapshot_description_is_rederived_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            skill = Path(directory) / "SKILL.md"
            skill.write_text(skill_text("研" * 150))
            original = skill.read_bytes()
            saved = {"entries": [{"path": str(skill), "description": "研" * 57 + "..."}]}
            with patch("misaka.core.skills.sandbox.read_manifest", return_value=saved):
                result = index._layer("sandbox", directory)
            self.assertEqual(result["skills"][0]["description"], "研" * 150)
            self.assertEqual(skill.read_bytes(), original)

    def test_authoring_uses_the_same_budget(self):
        self.assertIsNone(validate_frontmatter(skill_text("x" * 200), new_skill=True))
        self.assertIn("200-character", validate_frontmatter(skill_text("x" * 201), new_skill=True))
        self.assertIn("<=200 characters", _AUTHORING_STANDARDS)
        self.assertIn("first 197 chars", SKILL_MANAGE_SCHEMA["description"])


if __name__ == "__main__":
    unittest.main()

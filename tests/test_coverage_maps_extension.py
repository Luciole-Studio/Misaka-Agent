"""coverage-maps ships with the coverage extension: a session with coverage_scan has the maps too."""
import asyncio
from pathlib import Path

from misaka.config import CFG
from misaka.core.agent_session import AgentSession
from misaka.core.skills import index, layers
from misaka.extensions import coverage


def _discovered():
    handlers = {}
    harness = type("Harness", (), {"registerTool": lambda self, definition: None,
                                    "on": lambda self, name, handler: handlers.setdefault(name, handler)})()
    coverage.register(harness)
    found = asyncio.run(handlers["resources_discover"]({"type": "resources_discover"}, None))
    session = object.__new__(AgentSession)
    return session.buildExtensionResourcePaths(
        [{"path": path, "extensionPath": "builtin:coverage"} for path in found["skillPaths"]])


def test_the_coverage_extension_offers_the_maps(tmp_path):
    last_order = Path(CFG["roles_root"]).expanduser() / "last_order"
    roots = layers.skill_roots(str(last_order), str(tmp_path), extension_paths=_discovered())
    [entry] = index.candidates(roots, "builtin:coverage:coverage_maps")
    assert entry["name"] == "builtin:coverage:coverage_maps"  # named by the extension's own path
    assert sorted(p.name for p in (Path(entry["dir"]) / "references").iterdir()) == [
        "disciplines.md", "facets.md", "internal.md", "questions.md", "societies.md", "traditions.md"]
    # The prompts and the skill's own text say "coverage maps", unprefixed.
    assert [e["dir"] for e in index.candidates(roots, "coverage-maps")] == [entry["dir"]]

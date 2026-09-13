# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / tools/skills_guard.py; see PROVENANCE.json and LICENSE.
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from contextlib import suppress
from dataclasses import asdict
from typing import Tuple
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..guard import ScanResult
SCANNER_VERSION = "misaka-skills-guard-v3"

def _content_digest(skill_path: Path) -> str:
    """Canonical SHA-256 over (POSIX relative path, file bytes) ORDERED by the rel-path STRING — Path sorting is
    case-insensitive on Windows and diverged from ``skills_hub.bundle_content_hash`` (every installed skill then
    reported ``update_available`` forever). String order keeps both sides byte-symmetric.

    Ordering by ``sorted(rglob(...))`` diverged from the bundle side on Windows: Path comparison is
    case-insensitive there (normcase), while ``bundle_content_hash`` sorts plain strings — the same skill
    hashed to different digests and every installed skill reported ``update_available`` forever (#62310).
    """
    if not skill_path.is_dir():
        return hashlib.sha256(skill_path.read_bytes()).hexdigest()
    h = hashlib.sha256()
    for rel, p in sorted((p.relative_to(skill_path).as_posix(), p) for p in skill_path.rglob("*") if p.is_file()):
        h.update(rel.encode("utf-8") + b"\x00")
        h.update(p.read_bytes())
    return h.hexdigest()


def content_hash(skill_path: Path) -> str:
    """Short integrity hash (paths mixed in, so swapping two files' contents changes it). MUST stay symmetric
    with ``tools.skills_hub_install.bundle_content_hash`` — change both at once."""
    return f"sha256:{_content_digest(skill_path)[:16]}"


def scan_skill_cached(skill_path: Path, source: str = "community", *, source_url: str = "",
                      cache_dir: Path | None = None) -> Tuple[ScanResult, dict]:
    """Scan plus attestation dict; the cache (keyed by content digest + source identity) only serves exact
    current content under the current scanner version."""
    from ..guard import ScanResult, Finding, scan_skill
    digest = _content_digest(skill_path)
    cache_root = cache_dir or skill_path.parent / ".scan-cache"
    source_identity = hashlib.sha256(f"{source}\0{source_url}".encode("utf-8")).hexdigest()[:16]
    cache_file = cache_root / f"{digest}-{source_identity}.json"
    expected = {"bundle_hash": f"sha256:{digest}", "scanner_version": SCANNER_VERSION, "source": source,
                "source_url": source_url}
    cached = None
    with suppress(OSError, json.JSONDecodeError):
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
    if isinstance(cached, dict) and all(cached.get(k) == v for k, v in expected.items()):
        result = ScanResult(skill_path.name, source, cached["trust_level"], cached["verdict"],
                            [Finding(**item) for item in cached.get("findings", [])], cached["scanned_at"],
                            cached.get("summary", ""))
        provenance = {**cached, "fresh": False}
    else:
        result = scan_skill(skill_path, source=source)
        findings = [asdict(item) for item in result.findings]
        provenance = {**expected, "verdict": result.verdict, "trust_level": result.trust_level, "findings": findings,
                      "rules": sorted({item["pattern_id"] for item in findings}), "scanned_at": result.scanned_at,
                      "summary": result.summary, "fresh": True}
        with suppress(OSError):
            cache_root.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    result.scan_provenance = provenance
    return result, provenance


def full_content_hash(skill_path: Path) -> str:
    """Full canonical digest used to bind scanner attestations."""
    return f"sha256:{_content_digest(skill_path)}"


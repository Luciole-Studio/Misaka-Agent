#!/usr/bin/env python3
"""Compare, or regenerate, the built-in model catalog against upstream pi-ai.

`misaka/ai/models_generated.py` and `image_models_generated.py` are transcriptions of the
data files shipped in the npm package `@earendil-works/pi-ai` -- **not** of anything in
pi's git tree, where `providers/data/` is gitignored. There is no generator upstream to
borrow, so the honest form of "regenerate" here is "re-transcribe from the published
tarball", and the honest form of "is it current?" is a full comparison.

That comparison is the point. The previous hand port drifted silently: 497 models
missing, 253 priced wrong, and nothing could see it, because a catalog that is merely
stale still looks like a catalog. Pricing feeds cost display and compaction thresholds
read `contextWindow`, so a wrong entry is wrong arithmetic rather than a visible error.

    python scripts/catalog_sync.py --check            # compare against the pinned version
    python scripts/catalog_sync.py --check --version latest
    python scripts/catalog_sync.py --write --version 0.84.4

`--check` exits non-zero when the local tables differ from the version it compared with,
so it can gate a release without being in `make check` (it needs the network).
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = "https://registry.npmjs.org/@earendil-works/pi-ai"
PINNED = "0.84.4"


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def _resolve_version(version: str) -> str:
    if version != "latest":
        return version
    return _fetch_json(REGISTRY)["dist-tags"]["latest"]


def _provider_data(version: str) -> dict[str, dict]:
    """`{provider: {model_id: entry}}` from the published tarball's providers/data."""
    meta = _fetch_json(f"{REGISTRY}/{version}")
    with urllib.request.urlopen(meta["dist"]["tarball"], timeout=120) as response:
        raw = response.read()
    out: dict[str, dict] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if not name.startswith("package/dist/providers/data/") or not name.endswith(".json"):
                continue
            if name.endswith(".manifest.json"):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            payload = json.loads(handle.read().decode("utf-8"))
            # One file per provider, keyed by the api its models speak, then by model id.
            # The provider name is the file stem; a provider with models on two apis has
            # two top-level keys, so the ids are merged rather than one branch taken.
            if not isinstance(payload, dict):
                continue
            models: dict[str, dict] = {}
            for by_api in payload.values():
                if isinstance(by_api, dict):
                    models.update(by_api)
            if models:
                out[Path(name).stem] = models
    return out


def _local_models() -> dict[str, dict]:
    sys.path.insert(0, str(ROOT))
    from misaka.ai import models_generated

    return models_generated._RAW_MODELS


def _compare(local: dict[str, dict], upstream: dict[str, dict]) -> dict:
    local_keys = {(p, m) for p, models in local.items() for m in models}
    up_keys = {(p, m) for p, models in upstream.items() for m in models}
    missing = sorted(up_keys - local_keys)
    extra = sorted(local_keys - up_keys)
    differing: list[tuple[str, str, str]] = []
    for provider, model in sorted(local_keys & up_keys):
        a, b = local[provider][model], upstream[provider][model]
        for field in sorted(set(a) | set(b)):
            if a.get(field) != b.get(field):
                differing.append((provider, model, field))
    return {
        "providers_local": len(local), "providers_upstream": len(upstream),
        "models_local": len(local_keys), "models_upstream": len(up_keys),
        "missing": missing, "extra": extra, "differing": differing,
    }


def _manifest(version: str) -> dict:
    meta = _fetch_json(f"{REGISTRY}/{version}")
    with urllib.request.urlopen(meta["dist"]["tarball"], timeout=120) as response:
        raw = response.read()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        handle = tar.extractfile("package/dist/providers/data/.manifest.json")
        return json.loads(handle.read().decode("utf-8")) if handle else {}


def _rewrite(models: dict[str, dict], manifest: dict, version: str) -> None:
    """Replace the table and its provenance, keeping everything else in the file.

    Only the four provenance assignments and the `_RAW_MODELS` literal are rewritten, so
    the module docstring, imports and the `MODELS` comprehension below survive edits made
    to them for reasons that have nothing to do with the data.
    """
    import hashlib
    import pprint

    path = ROOT / "misaka" / "ai" / "models_generated.py"
    text = path.read_text(encoding="utf-8")
    ordered = {p: dict(sorted(models[p].items())) for p in sorted(models)}
    payload = json.dumps(ordered, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()

    head, sep, tail = text.partition("_RAW_MODELS: Final[")
    if not sep:
        raise SystemExit("models_generated.py: _RAW_MODELS assignment not found")
    _, _, after = tail.partition("\n\nMODELS: Final[")

    literal = pprint.pformat(ordered, width=100, sort_dicts=False)
    head = _replace_assignment(head, "BUILTIN_MODEL_DATA_GENERATED_AT",
                               f'"{manifest.get("generatedAt", "")}"')
    head = _replace_assignment(head, "BUILTIN_MODEL_DATA_SCHEMA_VERSION",
                               str(manifest.get("schemaVersion", 3)))
    head = _replace_assignment(head, "BUILTIN_MODEL_DATA_CONTENT_HASH", f'"{digest}"')
    path.write_text(
        f"{head}_RAW_MODELS: Final[dict[str, dict[str, dict[str, object]]]] = {literal}"
        f"\n\nMODELS: Final[{after}",
        encoding="utf-8")

    script = pathlib.Path(__file__)
    script.write_text(
        _replace_assignment(script.read_text(encoding="utf-8"), "PINNED", f'"{version}"'),
        encoding="utf-8")


def _replace_assignment(text: str, name: str, value: str) -> str:
    import re

    pattern = re.compile(rf"^{re.escape(name)} = .*$", re.MULTILINE)
    if not pattern.search(text):
        raise SystemExit(f"assignment {name} not found")
    return pattern.sub(f"{name} = {value}", text, count=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=PINNED,
                        help=f'npm version to compare with, or "latest" (default: {PINNED})')
    parser.add_argument("--check", action="store_true", help="compare only; non-zero exit on any difference")
    parser.add_argument("--write", action="store_true",
                        help="re-transcribe models_generated.py from that version")
    args = parser.parse_args()

    version = _resolve_version(args.version)
    latest = _resolve_version("latest")
    print(f"local transcription pinned at {PINNED}; comparing with {version} (npm latest: {latest})")

    result = _compare(_local_models(), _provider_data(version))
    print(f"providers: local {result['providers_local']} / upstream {result['providers_upstream']}")
    print(f"models:    local {result['models_local']} / upstream {result['models_upstream']}")
    print(f"missing locally: {len(result['missing'])} | extra locally: {len(result['extra'])} "
          f"| field-level differences: {len(result['differing'])}")
    for provider, model in result["missing"][:10]:
        print(f"  missing  {provider}/{model}")
    for provider, model in result["extra"][:10]:
        print(f"  extra    {provider}/{model}")
    for provider, model, field in result["differing"][:10]:
        print(f"  differs  {provider}/{model}.{field}")
    if len(result["missing"]) + len(result["extra"]) + len(result["differing"]) > 30:
        print("  ... (truncated)")

    if args.write:
        manifest = _manifest(version)
        _rewrite(_provider_data(version), manifest, version)
        print(f"\nrewrote misaka/ai/models_generated.py from {version} "
              f"(generatedAt {manifest.get('generatedAt')})")
        print("PINNED in this script was updated too; review the diff, then run make check.")

    if args.check:
        drift = len(result["missing"]) + len(result["extra"]) + len(result["differing"])
        if drift:
            print(f"\ncatalog_sync: {drift} difference(s) against {version}")
            return 1
        print(f"\ncatalog_sync: identical to {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

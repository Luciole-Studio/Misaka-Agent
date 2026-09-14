#!/usr/bin/env python3
"""Regenerate ``misaka/ai/models_generated.py`` from upstream pi-ai's shipped catalog.

The catalog is data, not code: pi-ai publishes one JSON file per provider under
``dist/providers/data`` plus a ``.manifest.json`` carrying the schema version and the
timestamp upstream generated it. This script transcribes those files and nothing else.

It exists because the previous refresh was done by hand and drifted badly -- measured
against 0.84.3 the hand port was missing 497 models, carried 218 upstream had dropped and
priced 253 wrong -- and no amount of reading would have caught that. A transcription that
runs is checkable; one that is typed is not.

    python scripts/refresh_model_catalog.py --version 0.85.1
    python scripts/refresh_model_catalog.py --data-dir path/to/dist/providers/data

Prints what changed, so the commit message can name it. ``--check`` rebuilds and reports
whether the committed file already matches, without writing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pprint
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "misaka" / "ai" / "models_generated.py"
PACKAGE = "@earendil-works/pi-ai"

# pprint's settings are part of the file's identity: they are what makes a refresh a
# reviewable diff instead of one 20,000-line line.
WIDTH = 100

PREAMBLE = '''"""The model catalog, regenerated from upstream (pi) ``packages/ai/src/providers/data``.

Every entry is a straight transcription of what pi-ai ships, so a refresh means re-running
``scripts/refresh_model_catalog.py`` rather than editing entries here. The table was
originally ported by hand and drifted: measured against ``@earendil-works/pi-ai@0.84.3`` it
was missing 497 models, carried 218 upstream had dropped, priced 253 wrong, and silently
discarded 739 ``compat`` values whose keys no compat model declared (``extra="forbid"``
rejects the entry rather than ignoring the key).

Excluded from lint as generated-shaped code.
"""

from __future__ import annotations

from typing import Final

from misaka.ai.types import Model

# Provenance of the transcription. Upstream ships these in `providers/data/.manifest.json`
# and reads `generatedAt` back through `getBuiltinModelDataGeneratedAt`, so a remote
# catalog overlay can tell whether what it fetched is newer than what shipped.
#
# `CONTENT_HASH` is this file's own, not upstream's: sha256 over `_RAW_MODELS` serialised as
# compact JSON with sorted keys. It covers the table as transcribed, so a hand edit to an
# entry is visible -- which is the point, because the previous drift was undetectable for
# exactly the reason that nothing but a full re-comparison would have found it.
BUILTIN_MODEL_DATA_GENERATED_AT = {generated_at!r}
BUILTIN_MODEL_DATA_SCHEMA_VERSION = {schema_version}
BUILTIN_MODEL_DATA_CONTENT_HASH = {content_hash!r}

_RAW_MODELS: Final[dict[str, dict[str, dict[str, object]]]] = '''

EPILOGUE = '''

MODELS: Final[dict[str, dict[str, Model]]] = {
    provider: {model_id: Model.model_validate(model_data) for model_id, model_data in models.items()}
    for provider, models in _RAW_MODELS.items()
}
'''


def fetch(version: str) -> Path:
    """npm-pack the published package into a temporary directory and return its data dir."""
    out = Path(tempfile.mkdtemp(prefix="pi-ai-"))
    url = subprocess.run(["npm", "view", f"{PACKAGE}@{version}", "dist.tarball"],
                         capture_output=True, text=True, check=True).stdout.strip()
    archive = out / "package.tgz"
    urllib.request.urlretrieve(url, archive)
    with tarfile.open(archive) as tar:
        tar.extractall(out, filter="data")
    return out / "package" / "dist" / "providers" / "data"


def transcribe(data_dir: Path) -> tuple[dict, dict]:
    """``(models by provider, the manifest)``. A provider file keys its models by api; the
    catalog keys them by provider, so the api layer is merged away exactly as upstream's own
    loader does."""
    manifest = json.loads((data_dir / ".manifest.json").read_text())
    catalog: dict[str, dict[str, dict]] = {}
    # By provider name, not by file name: "google.json" sorts after "google-vertex.json"
    # because '-' precedes '.', and the committed catalog is ordered by the provider key.
    for path in sorted(data_dir.glob("*.json"), key=lambda entry: entry.stem):
        if path.name.startswith("."):
            continue
        models: dict[str, dict] = {}
        for entries in json.loads(path.read_text()).values():
            if isinstance(entries, dict):
                models.update(entries)
        # By model id. A provider file groups its models by api, and several providers serve
        # the same catalogue over two or three of them; keeping the file's grouping would put
        # the same model in a different place every time upstream adds an api.
        catalog[path.stem] = {model_id: models[model_id] for model_id in sorted(models)}
    return catalog, manifest


def content_hash(catalog: dict) -> str:
    return hashlib.sha256(json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def render(catalog: dict, manifest: dict) -> str:
    head = PREAMBLE.format(generated_at=manifest["generatedAt"],
                           schema_version=manifest["schemaVersion"],
                           content_hash=content_hash(catalog))
    return head + pprint.pformat(catalog, width=WIDTH, sort_dicts=False) + EPILOGUE


def report(old: dict, new: dict) -> None:
    added = removed = 0
    for provider in sorted(set(old) | set(new)):
        before, after = set(old.get(provider, {})), set(new.get(provider, {}))
        fresh, gone = sorted(after - before), sorted(before - after)
        added, removed = added + len(fresh), removed + len(gone)
        if fresh or gone:
            print(f"{provider}:")
            if fresh:
                print(f"   + {len(fresh):>3}  {', '.join(fresh[:6])}{' ...' if len(fresh) > 6 else ''}")
            if gone:
                print(f"   - {len(gone):>3}  {', '.join(gone[:6])}{' ...' if len(gone) > 6 else ''}")
    print(f"\n{added} model(s) added, {removed} removed; "
          f"{sum(len(v) for v in new.values())} total across {len(new)} providers.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--version", help=f"a published {PACKAGE} version, fetched with npm")
    source.add_argument("--data-dir", type=Path, help="an unpacked providers/data directory")
    parser.add_argument("--check", action="store_true", help="report whether the committed file matches; write nothing")
    args = parser.parse_args()

    data_dir = args.data_dir or fetch(args.version)
    if not (data_dir / ".manifest.json").is_file():
        print(f"No .manifest.json under {data_dir}", file=sys.stderr)
        return 2
    catalog, manifest = transcribe(data_dir)

    sys.path.insert(0, str(TARGET.parent.parent.parent))
    try:
        from misaka.ai.models_generated import _RAW_MODELS as current
    except Exception:  # noqa: BLE001 - a file that will not import is exactly what a refresh fixes
        current = {}

    rendered = render(catalog, manifest)
    if args.check:
        same = TARGET.read_text(encoding="utf-8") == rendered
        print("up to date" if same else "OUT OF DATE")
        if not same:
            report(current, catalog)
        return 0 if same else 1

    report(current, catalog)
    TARGET.write_text(rendered, encoding="utf-8")
    print(f"\nwrote {TARGET}  (upstream generatedAt {manifest['generatedAt']}, schema {manifest['schemaVersion']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

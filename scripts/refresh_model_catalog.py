#!/usr/bin/env python3
"""Update misaka's built-in model data from upstream pi-ai's published catalog.

pi-ai ships its catalog as data: one JSON file per provider under ``dist/providers/data`` and a
``.manifest.json`` carrying the schema version, the generation time and every file's sha256.
misaka reads those files the way pi does, so an update copies them unchanged into
``misaka/ai/providers/data`` and rewrites the modules that read them with the renderers of
``scripts/generate_models.py`` (pi's ``scripts/generate-models.ts``):
``misaka/ai/providers/<provider>_models.py`` per provider (pi's ``providers/<provider>.models.ts``)
and ``misaka/ai/models_generated.py`` gathering them (``models.generated.ts``).

    python scripts/refresh_model_catalog.py --version 0.99.1
    python scripts/refresh_model_catalog.py --data-dir path/to/dist/providers/data

``--check`` reports whether the committed data and modules already match, and writes nothing.

The data is refused unless every file matches the manifest, and unless its schema version is
the one ``misaka/ai/model_catalog.py`` was ported against: a newer schema means pi changed how
its catalog is laid out, and the loader has to be compared with pi's again first.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

# The generated modules are the generator's.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_models import renderAggregator, renderShard
from model_data import (
    MODEL_DATA_MANIFEST_FILE,
    MODEL_DATA_SCHEMA_VERSION,
    providerModule,
    validateGeneratedModelData,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "misaka" / "ai" / "providers" / "data"
PROVIDERS = ROOT / "misaka" / "ai" / "providers"
TARGET = ROOT / "misaka" / "ai" / "models_generated.py"
PACKAGE = "@earendil-works/pi-ai"
MANIFEST = MODEL_DATA_MANIFEST_FILE
SCHEMA_VERSION = MODEL_DATA_SCHEMA_VERSION


def fetch(version: str) -> Path:
    """Download the published package into a temporary directory and return its data dir."""
    out = Path(tempfile.mkdtemp(prefix="pi-ai-"))
    with urllib.request.urlopen(f"https://registry.npmjs.org/{PACKAGE}/{version}") as response:
        tarball = json.load(response)["dist"]["tarball"]
    archive = out / "package.tgz"
    urllib.request.urlretrieve(tarball, archive)
    with tarfile.open(archive) as tar:
        tar.extractall(out, filter="data")
    return out / "package" / "dist" / "providers" / "data"


def provider_files(data_dir: Path) -> list[Path]:
    """The provider files, without the manifest (a dotfile that ``*.json`` also matches)."""
    return sorted(path for path in data_dir.glob("*.json") if not path.name.startswith("."))


def verified(data_dir: Path) -> dict:
    """The manifest, once the directory is exactly what it lists."""
    manifest = json.loads((data_dir / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != SCHEMA_VERSION:
        raise SystemExit(f"{data_dir}: schema {manifest.get('schemaVersion')}, but the loader follows schema "
                         f"{SCHEMA_VERSION}. Compare misaka/ai/model_catalog.py with pi-ai's model-catalog.ts "
                         "and models.generated.ts first, then raise SCHEMA_VERSION.")
    present = {path.name for path in provider_files(data_dir)}
    if present != set(manifest["files"]):
        raise SystemExit(f"{data_dir}: files differ from the manifest: {sorted(present ^ set(manifest['files']))}")
    for name, digest in manifest["files"].items():
        if hashlib.sha256((data_dir / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"{data_dir / name}: sha256 differs from the manifest")
    return manifest


def render(manifest: dict) -> dict[Path, str]:
    """Every generated module by path: one per provider, in pi's order (sorted by id), and the aggregator."""
    providers = sorted(Path(name).stem for name in manifest["files"])
    rendered = {PROVIDERS / f"{providerModule(p)}.py": renderShard(p) for p in providers}
    rendered[TARGET] = renderAggregator(providers)
    return rendered


def generated_modules() -> list[Path]:
    """The generated provider modules on disk."""
    return sorted(PROVIDERS.glob("*_models.py"))


def chat_models(data_dir: Path) -> dict[str, list[str]]:
    return {path.stem: [model["id"] for models in json.loads(path.read_text(encoding="utf-8")).values()
                        for model in models.values() if model.get("type") == "chat"]
            for path in provider_files(data_dir)}


def report(old: dict, new: dict) -> None:
    added = removed = 0
    for provider in sorted(set(old) | set(new)):
        before, after = set(old.get(provider, [])), set(new.get(provider, []))
        fresh, gone = sorted(after - before), sorted(before - after)
        added, removed = added + len(fresh), removed + len(gone)
        if fresh or gone:
            print(f"{provider}:")
            if fresh:
                print(f"   + {len(fresh):>3}  {', '.join(fresh[:6])}{' ...' if len(fresh) > 6 else ''}")
            if gone:
                print(f"   - {len(gone):>3}  {', '.join(gone[:6])}{' ...' if len(gone) > 6 else ''}")
    total = sum(len(models) for models in new.values())
    print(f"\n{added} chat model(s) added, {removed} removed; {total} in total.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--version", help=f"a published {PACKAGE} version")
    source.add_argument("--data-dir", type=Path, help="an unpacked providers/data directory")
    parser.add_argument("--check", action="store_true", help="report whether the committed files match; write nothing")
    args = parser.parse_args()

    data_dir = args.data_dir or fetch(args.version)
    manifest = verified(data_dir)
    rendered = render(manifest)
    files = [MANIFEST, *manifest["files"]]
    if args.check:
        same = (all(path.is_file() and path.read_text(encoding="utf-8") == text for path, text in rendered.items())
                and set(generated_modules()) == {path for path in rendered if path != TARGET}
                and all((DATA / name).is_file() and (DATA / name).read_bytes() == (data_dir / name).read_bytes()
                        for name in files)
                and {path.name for path in provider_files(DATA)} == set(manifest["files"]))
        if same:
            # The committed files are the published ones; they must also be what the loader accepts.
            validateGeneratedModelData(ROOT)
        print("up to date" if same else "OUT OF DATE")
        if not same:
            report(chat_models(DATA), chat_models(data_dir))
        return 0 if same else 1

    old = chat_models(DATA)
    if data_dir.resolve() != DATA.resolve():   # rewriting the list from the committed data copies nothing
        for stale in [*provider_files(DATA), DATA / MANIFEST]:
            stale.unlink(missing_ok=True)
        DATA.mkdir(parents=True, exist_ok=True)
        for name in files:
            shutil.copyfile(data_dir / name, DATA / name)
    for stale in generated_modules():
        if stale not in rendered:
            stale.unlink()
    for path, text in rendered.items():
        path.write_text(text, encoding="utf-8", newline="\n")  # LF on Windows too
    validateGeneratedModelData(ROOT)
    report(old, chat_models(DATA))
    print(f"\nwrote {DATA}, {len(rendered) - 1} provider modules and {TARGET.name}  "
          f"(upstream generatedAt {manifest['generatedAt']}, schema {manifest['schemaVersion']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

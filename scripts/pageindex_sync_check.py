#!/usr/bin/env python3
"""Report how far the vendored PageIndex has drifted from upstream, and from its own record.

The contract is in ``misaka/core/documents/pageindex/PORT_NOTES.md``. It is looser than
hermes-lcm's byte-identity rule and deliberately so: the vendored tree was normalised by
this repository's own lint pass, so almost every file differs from upstream while meaning
exactly the same thing. Byte comparison against that would report 72 files and tell you
nothing.

So the question this asks is not "does it differ" but "does it still *mean* the same":

* both trees are run through the same ruff autofix set until they converge, and only
  files that still differ afterwards are reported. A file inside the lint-equivalence
  class is normalisation; a file outside it is a real edit that must be registered.
* the public surface of every subpackage is compared name by name, which catches a
  rename or a dropped export that lint could never produce.

Neither check needs the network, but both need an upstream checkout, so this is not part
of ``make check``. Run it before a resync and after anyone touches the vendored tree.

    python scripts/pageindex_sync_check.py --upstream ~/src/PageIndex

Exit 1 means drift outside the equivalence class, or a public-name mismatch. Upstream
being ahead of the pin is reported but is exit 0 -- new commits are news, not a defect.
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDORED = ROOT / "misaka" / "documents" / "pageindex" / "flash"
NOTES = ROOT / "misaka" / "documents" / "pageindex" / "PORT_NOTES.md"
UPSTREAM_NOTE = ROOT / "misaka" / "documents" / "pageindex" / "UPSTREAM.md"

# The normalisation both trees are put through before they are compared. These are the
# rules whose output this repository standardises on; running them on upstream too is
# what turns "72 files differ" into "these files actually changed".
RUFF_RULES = "I,F401,UP,C4,SIM,RUF,PLR,FURB,PERF,RET,B"
RUFF_PASSES = 3


def _pin() -> str:
    match = re.search(r"Commit: `([0-9a-f]{7,40})`", UPSTREAM_NOTE.read_text(encoding="utf-8"))
    return match.group(1) if match else ""


def _normalise(tree: Path) -> Path:
    """A copy of `tree` with this repo's lint normalisation applied, run to a fixed point."""
    work = Path(tempfile.mkdtemp(prefix="pi-norm-")) / tree.name
    shutil.copytree(tree, work)
    for _ in range(RUFF_PASSES):
        subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--select", RUFF_RULES,
             "--fix", "--unsafe-fixes", "--quiet", str(work)],
            capture_output=True, check=False)
    return work


def _public_names(path: Path) -> set[str]:
    """Module-level names a caller could import, without executing the module."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return {n for n in names if not n.startswith("_")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True,
                        help="path to a PageIndex checkout (its pageindex/flash is compared)")
    args = parser.parse_args()

    upstream_flash = Path(args.upstream).expanduser() / "pageindex" / "flash"
    if not upstream_flash.is_dir():
        print(f"no pageindex/flash under {args.upstream}")
        return 2

    pin = _pin()
    head = subprocess.run(
        ["git", "-C", str(Path(args.upstream).expanduser()), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=False).stdout.strip()
    print(f"pin {pin[:12] or '(unrecorded)'} | upstream checkout at {head or '(not a git tree)'}")

    registered = set(re.findall(r"^\| `([^`]+)`", NOTES.read_text(encoding="utf-8"), re.MULTILINE)) if NOTES.exists() else set()

    print(f"normalising both trees ({RUFF_RULES}, {RUFF_PASSES} passes)...")
    local_norm, up_norm = _normalise(VENDORED), _normalise(upstream_flash)

    unregistered, name_gaps = [], []
    for path in sorted(VENDORED.rglob("*.py")):
        rel = path.relative_to(VENDORED).as_posix()
        mirror = up_norm / rel
        if not mirror.exists():
            if rel not in registered:
                unregistered.append(f"  {rel}: not in upstream and not registered")
            continue
        if (local_norm / rel).read_bytes() != mirror.read_bytes() and rel not in registered:
            unregistered.append(f"  {rel}: differs after normalisation, not registered")
        local_names, up_names = _public_names(path), _public_names(upstream_flash / rel)
        if local_names != up_names and rel not in registered:
            missing = sorted(up_names - local_names)
            extra = sorted(local_names - up_names)
            name_gaps.append(f"  {rel}: missing {missing or '-'} extra {extra or '-'}")

    added = [p.relative_to(upstream_flash).as_posix() for p in sorted(upstream_flash.rglob("*.py"))
             if not (VENDORED / p.relative_to(upstream_flash)).exists()]

    if added:
        print(f"\nupstream has {len(added)} file(s) the vendored tree does not carry (news, not a defect):")
        for rel in added[:10]:
            print(f"  {rel}")
    if unregistered:
        print(f"\nunregistered drift ({len(unregistered)}):")
        print("\n".join(unregistered))
    if name_gaps:
        print(f"\npublic-name mismatch ({len(name_gaps)}):")
        print("\n".join(name_gaps))
    if not unregistered and not name_gaps:
        print("\npageindex_sync_check: vendored tree is upstream modulo this repo's lint normalisation")
        return 0
    print("\nEvery entry above is either a real local edit that belongs in PORT_NOTES.md, "
          "or a resync that was never finished.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

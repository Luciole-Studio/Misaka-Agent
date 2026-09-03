#!/usr/bin/env python3
"""Report how far the vendored hermes-lcm has drifted from upstream, and from its own record.

The port's contract (``misaka/extensions/hermes_lcm/PORT_NOTES.md``) is that every byte
under ``vendor/`` and ``tests/hermes_lcm_vendor/`` matches the pinned upstream commit
unless the change is registered. A registered change is replayed at the next resync; an
unregistered one is silently lost there -- so the check that matters most is not "is
upstream ahead", it is "did anyone edit a vendored file without writing it down".

What this reports:

* the pin, upstream HEAD, and how many commits the pin is behind;
* every vendored file whose content differs from the same path at the pin, split into
  registered (fine, replay it) and unregistered (a defect, exit 1);
* PORT_NOTES entries with nothing to point at, and ``# misaka:`` markers with no entry;
* the ``ContextEngine`` ABC stub against the real one in the Hermes agent checkout --
  the one seam that is written by hand rather than copied, so byte comparison cannot
  see it drift;
* upstream modules and test files added since the pin, and vendored paths upstream has
  since changed or deleted.

It is not part of ``make check``: CI has no upstream checkout. Run it by hand before a
resync, and after anyone touches the vendored tree.

Usage: ``python scripts/lcm_sync_check.py [--upstream PATH] [--hermes-agent PATH]``.
Exit 1 means unregistered drift. Upstream being ahead is exit 0 with a notice: new
commits are news, not a defect.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT_DIR = Path("misaka/extensions/hermes_lcm")
VENDOR_DIR = PORT_DIR / "vendor"
VENDOR_TESTS_DIR = Path("tests/hermes_lcm_vendor")
STUB_FILE = PORT_DIR / "host/context_engine_abc.py"

DEFAULT_UPSTREAM = Path("~/.hermes/plugins/hermes-lcm").expanduser()
DEFAULT_HERMES_AGENT = Path("~/.hermes/hermes-agent").expanduser()
UPSTREAM_URL = "https://github.com/stephenschoettler/hermes-lcm"

# Files inside the vendored trees that are misaka's, not copies, so nothing upstream
# corresponds to them. Everything else in those trees must match the pin byte for byte.
OURS = {
    VENDOR_DIR / "__init__.py": "package init + the agent.context_engine seam",
    VENDOR_TESTS_DIR / "conftest.py": "the hermes_lcm alias and the skip manifest",
}

MARKER = re.compile(r"#\s*misaka:")
# The registered-change table in PORT_NOTES.md, by its heading.
REGISTERED_HEADING = "## 已登记的改动"
_EMPTY_CELLS = {"", "—", "-", "–"}


def _git(repo: Path, *args: str) -> str | None:
    """``git -C repo args``; None when git says no, so callers can phrase the failure."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def read_pin(repo: Path) -> tuple[str, str]:
    """``(commit, version)`` from UPSTREAM_COMMIT: first line is the sha, rest is prose."""
    text = (repo / PORT_DIR / "UPSTREAM_COMMIT").read_text(encoding="utf-8")
    lines = text.splitlines()
    version = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("version:")), "?")
    return lines[0].strip(), version


def vendored_files(repo: Path) -> dict[Path, str]:
    """Repo-relative path -> the upstream path it was copied from."""
    mapping: dict[Path, str] = {}
    for path in sorted((repo / VENDOR_DIR).glob("*.py")):
        rel = path.relative_to(repo)
        if rel not in OURS:
            mapping[rel] = path.name
    tests = repo / VENDOR_TESTS_DIR
    for path in sorted(tests.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(repo)
        if rel not in OURS:
            mapping[rel] = "tests/" + path.relative_to(tests).as_posix()
    return mapping


def deleted_files(repo: Path) -> list[str]:
    """Vendored paths misaka's own git tracks that are gone from the working tree.

    Deleting a file is the one edit the pin comparison cannot see: what is not on disk is
    not in ``vendored_files`` either, so the count quietly drops and every remaining file
    still matches. That matters most for the fidelity harness, where removing a test file
    is how a failing suite is made to look green.
    """
    listing = _git(repo, "ls-files", "--deleted", "--", str(VENDOR_DIR), str(VENDOR_TESTS_DIR))
    return sorted(line for line in (listing or "").splitlines() if line.strip())


def _blob_ids(upstream: Path, rev: str) -> dict[str, str] | None:
    """Upstream path -> git object id at ``rev``. One git call instead of one per file."""
    listing = _git(upstream, "ls-tree", "-r", rev)
    if listing is None:
        return None
    out: dict[str, str] = {}
    for line in listing.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            out[path] = parts[2]
    return out


def _local_blob_id(path: Path, algorithm: str) -> str:
    """The object id git would give this file, so the comparison is git's own identity."""
    data = path.read_bytes()
    return hashlib.new(algorithm, b"blob %d\0" % len(data) + data).hexdigest()


def registered_changes(repo: Path) -> dict[str, list[str]]:
    """File name -> the PORT_NOTES rows registering a change to it."""
    text = (repo / PORT_DIR / "PORT_NOTES.md").read_text(encoding="utf-8")
    _, _, after = text.partition(REGISTERED_HEADING)
    rows: dict[str, list[str]] = {}
    for line in after.splitlines():
        if line.startswith("##"):
            break  # the section ended; the later tables are about other things
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        name = Path(cells[0]).name
        if name in _EMPTY_CELLS or set(name) <= {"-", ":"} or name == "文件":
            continue
        rows.setdefault(name, []).append(" | ".join(cells))
    return rows


def marked_files(repo: Path, files: dict[Path, str]) -> set[str]:
    """Vendored files carrying a ``# misaka:`` marker, by name."""
    marked = set()
    for rel in files:
        path = repo / rel
        if path.suffix == ".py" and MARKER.search(path.read_text(encoding="utf-8", errors="replace")):
            marked.add(path.name)
    return marked


def abc_stub_findings(repo: Path, hermes_agent: Path) -> tuple[list[str], str]:
    """Compare the hand-written ContextEngine stub with the real ABC. ``(findings, note)``.

    PORT_NOTES calls this the only place the port can drift silently: the stub is written
    from the real ABC, not copied from it, so no hash comparison covers it. Compared the
    way that file prescribes -- abstract set, class attribute values, parameter
    names/kinds/defaults (never annotations, which are spelled differently on either side
    for the same type), and what the two inherited bodies actually return.
    """
    source = hermes_agent / "agent/context_engine.py"
    if not source.exists():
        return [], f"skipped: no Hermes agent checkout at {hermes_agent}"
    # Whoever owns sys.modules["agent"] owns the comparison, and importing the vendored
    # tree installs the stub's own seam there -- so evict any `agent` first, and put back
    # exactly what was there before. Without this the stub would be compared to itself.
    saved = {name: module for name, module in sys.modules.items() if name.split(".")[0] == "agent"}
    for name in saved:
        del sys.modules[name]
    sys.path.insert(0, str(hermes_agent))
    try:
        try:
            real_module = importlib.import_module("agent.context_engine")
        except Exception as error:  # noqa: BLE001 - an unimportable host ABC is the host's problem, not the port's
            return [], f"skipped: {source} did not import ({type(error).__name__}: {error})"
        stub_module = _load_module(repo / STUB_FILE)
    finally:
        sys.path.remove(str(hermes_agent))
        for name in [n for n in sys.modules if n.split(".")[0] == "agent"]:
            del sys.modules[name]
        sys.modules.update(saved)
    real, stub = real_module.ContextEngine, stub_module.ContextEngine

    findings: list[str] = []
    if real.__abstractmethods__ != stub.__abstractmethods__:
        findings.append(
            f"abstract methods differ: real {sorted(real.__abstractmethods__)} "
            f"vs stub {sorted(stub.__abstractmethods__)}"
        )
    for name, value in sorted(vars(stub).items()):
        if name.startswith("_"):
            continue
        target = value.fget if isinstance(value, property) else value
        upstream_value = getattr(real, name, None)
        if upstream_value is None:
            findings.append(f"{name}: the stub defines it, the real ABC no longer does")
            continue
        if callable(target):
            upstream_target = upstream_value.fget if isinstance(upstream_value, property) else upstream_value
            here = [(p.name, p.kind, p.default) for p in inspect.signature(target).parameters.values()]
            there = [(p.name, p.kind, p.default) for p in inspect.signature(upstream_target).parameters.values()]
            if here != there:
                findings.append(f"{name}: signature differs (stub {here} vs real {there})")
        elif upstream_value != value or type(upstream_value) is not type(value):
            findings.append(f"{name}: default differs (stub {value!r} vs real {upstream_value!r})")

    here_probe, there_probe = _probe(stub), _probe(real)
    if here_probe.get_status() != there_probe.get_status():
        findings.append(
            f"get_status() differs: stub {here_probe.get_status()} vs real {there_probe.get_status()}"
        )
    if _reset_state(here_probe) != _reset_state(there_probe):
        findings.append("on_session_reset() leaves different token state behind")
    note = f"compared against {source}"
    return findings, note


def _load_module(path: Path) -> object:
    """Import a file by path, under a name nothing else can collide with."""
    spec = importlib.util.spec_from_file_location(f"_lcm_sync_probe_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_STATE = {
    "last_prompt_tokens": -1,  # the "compression just ran" sentinel: exercises the clamp
    "last_completion_tokens": 7,
    "last_total_tokens": 11,
    "threshold_tokens": 750,
    "context_length": 1000,
    "compression_count": 2,
}


def _probe(cls: type) -> object:
    """A least-effort concrete subclass, so the inherited bodies can be run and compared."""
    namespace: dict[str, object] = {
        name: (lambda self, *args, **kwargs: None) for name in cls.__abstractmethods__
    }
    namespace["name"] = property(lambda self: "probe")
    instance = type("Probe", (cls,), namespace)()
    for attribute, value in _STATE.items():
        setattr(instance, attribute, value)
    return instance


def _reset_state(probe: object) -> dict[str, object]:
    probe.on_session_reset()
    return {name: getattr(probe, name, None) for name in _STATE}


def _upstream_section(upstream: Path, pin: str, files: dict[Path, str]) -> tuple[list[str], list[str]]:
    """What upstream did since the pin. ``(lines, human_needed)``."""
    lines: list[str] = []
    human: list[str] = []
    changes = _git(upstream, "diff", "--name-status", f"{pin}..HEAD")
    if changes is None:
        return ["  could not diff the pin against HEAD"], human
    vendored = set(files.values())
    new_modules, new_tests, changed, deleted, other = [], [], [], [], 0
    for line in changes.splitlines():
        status, _, path = line.partition("\t")
        path = path.split("\t")[-1]  # renames carry both names; the destination is last
        if path in vendored:
            (deleted if status.startswith("D") else changed).append(path)
        elif status.startswith("A") and re.fullmatch(r"[^/]+\.py", path):
            new_modules.append(path)
        elif status.startswith("A") and re.fullmatch(r"tests/test_[^/]+\.py", path):
            new_tests.append(path)
        else:
            other += 1
    if not (new_modules or new_tests or changed or deleted):
        lines.append(f"  nothing that touches the port ({other} other path(s) changed)")
        return lines, human
    for path in changed:
        lines.append(f"  changed upstream, replay it: {path}")
    for path in new_modules:
        lines.append(f"  new upstream module: {path} (vendor it only if the import closure needs it)")
    for path in new_tests:
        lines.append(f"  new upstream test file: {path} (copy it into the fidelity harness)")
    for path in deleted:
        lines.append(f"  DELETED upstream but vendored here: {path}")
        human.append(f"upstream deleted {path}, which this port vendors")
    lines.append(f"  ({other} other upstream path(s) changed: docs, CI, benchmarks)")
    return lines, human


def report(repo: Path, upstream: Path, hermes_agent: Path) -> tuple[list[str], int]:
    """The whole inspection. ``(lines to print, exit code)``."""
    pin, version = read_pin(repo)
    files = vendored_files(repo)
    registered = registered_changes(repo)
    marked = marked_files(repo, files)
    drift: list[str] = []  # unregistered: the exit-1 findings
    human: list[str] = []  # "go find a person before resyncing"

    lines = ["lcm sync check", "", f"  repo             {repo}", f"  upstream         {upstream}"]
    lines.append(f"  pin              {pin[:8]} ({version})")

    blobs = None
    if not (upstream / ".git").exists():
        lines += [
            "  upstream HEAD    -- no checkout, so nothing was compared against the pin",
            "",
            "The drift check needs upstream's own git objects. Get them with:",
            f"  git clone {UPSTREAM_URL} {upstream}",
            "and re-run. Until then only the offline half runs (markers and PORT_NOTES),",
            "which catches a marked edit but not an unmarked one.",
        ]
    elif _git(upstream, "cat-file", "-e", f"{pin}^{{commit}}") is None:
        lines += [
            "  upstream HEAD    -- the pin is unknown to this checkout",
            "",
            f"{pin[:8]} is not an object here. Fetch it with:",
            f"  git -C {upstream} fetch --all --tags",
        ]
    else:
        head = (_git(upstream, "rev-parse", "HEAD") or "?").strip()
        described = (_git(upstream, "describe", "--tags", "--always") or "?").strip()
        behind = (_git(upstream, "rev-list", "--count", f"{pin}..HEAD") or "?").strip()
        lines.append(f"  upstream HEAD    {head[:8]} ({described})")
        lines.append(f"  behind pin       {behind} commit(s)")
        if _git(upstream, "merge-base", "--is-ancestor", pin, "HEAD") is None:
            lines.append("  note: the pin is not an ancestor of HEAD (rebase or force-push upstream)")
        algorithm = (_git(upstream, "rev-parse", "--show-object-format") or "sha1").strip()
        blobs = _blob_ids(upstream, pin)
        if blobs is None:
            lines.append("  note: could not list the pinned tree; nothing was compared")

    lines += ["", "vendored files"]
    for rel in deleted_files(repo):
        lines.append(f"  DELETED HERE: {rel} (tracked by misaka, missing from the working tree)")
        drift.append(f"{rel} is tracked but no longer on disk; a vendored file may not just disappear")
    if blobs is None:
        lines.append(f"  {len(files)} file(s) present, none compared (no pinned tree to compare against)")
    else:
        drifted, absent = [], []
        for rel, upstream_path in sorted(files.items()):
            pinned = blobs.get(upstream_path)
            if pinned is None:
                absent.append((rel, upstream_path))
            elif pinned != _local_blob_id(repo / rel, algorithm):
                drifted.append(rel)
        modules = sum(1 for rel in files if rel.parent.name == "vendor")
        lines.append(f"  {modules} module(s) + {len(files) - modules} test file(s) compared with the pin")
        for rel, upstream_path in absent:
            lines.append(f"  NOT AT THE PIN: {rel} (no {upstream_path} upstream)")
            drift.append(f"{rel} has no counterpart at the pin; it is not a vendored copy")
        for rel in drifted:
            if rel.name in registered:
                lines.append(f"  drifted, registered: {rel}")
            else:
                lines.append(f"  DRIFTED, UNREGISTERED: {rel}")
                drift.append(f"{rel} differs from the pin and PORT_NOTES does not say why")
        if not (drifted or absent):
            lines.append("  no drift: every vendored file is byte-identical to the pin")

    lines += ["", "PORT_NOTES"]
    lines.append(f"  {len(registered)} file(s) registered as changed, {len(marked)} carrying a `# misaka:` marker")
    for name in sorted(registered):
        if name not in {rel.name for rel in files}:
            lines.append(f"  registered but not vendored: {name}")
            drift.append(f"PORT_NOTES registers {name}, which is not a vendored file")
        elif name not in marked:
            lines.append(f"  registered but unmarked: {name} (no `# misaka:` line in the file)")
            drift.append(f"PORT_NOTES registers {name} but the file carries no `# misaka:` marker")
    for name in sorted(marked - set(registered)):
        lines.append(f"  MARKED BUT UNREGISTERED: {name}")
        drift.append(f"{name} carries a `# misaka:` marker with no PORT_NOTES entry")
    if not registered and not marked:
        lines.append("  the table is empty and no vendored file is marked -- they agree")

    findings, note = abc_stub_findings(repo, hermes_agent)
    lines += ["", "ContextEngine ABC stub", f"  {note}"]
    for finding in findings:
        lines.append(f"  DRIFTED: {finding}")
        drift.append(f"the ContextEngine stub no longer matches the real ABC: {finding}")
    if note.startswith("compared") and not findings:
        lines.append("  no drift: the stub still matches the surface the engine uses")

    if blobs is not None:
        upstream_lines, needs_human = _upstream_section(upstream, pin, files)
        lines += ["", "upstream since the pin", *upstream_lines]
        human += needs_human

    lines.append("")
    if drift:
        lines.append(f"result: {len(drift)} unregistered drift finding(s)")
        lines += [f"  - {item}" for item in drift]
        lines.append("")
        lines.append("Either revert the edit, or register it in PORT_NOTES.md with a `# misaka:`")
        lines.append("marker at the line -- an unregistered edit is lost at the next resync.")
    else:
        lines.append("result: no unregistered drift")
    if human:
        lines.append("")
        lines.append("Read docs/plans/hermes-lcm-sync.md before resyncing -- it lists these as stop signs:")
        lines += [f"  - {item}" for item in human]
    return lines, 1 if drift else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the vendored hermes-lcm against upstream.")
    parser.add_argument("--upstream", type=Path, default=DEFAULT_UPSTREAM,
                        help=f"upstream hermes-lcm checkout (default: {DEFAULT_UPSTREAM})")
    parser.add_argument("--hermes-agent", type=Path, default=DEFAULT_HERMES_AGENT,
                        help=f"Hermes agent checkout, for the ABC stub (default: {DEFAULT_HERMES_AGENT})")
    args = parser.parse_args()
    lines, code = report(ROOT, args.upstream, args.hermes_agent)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

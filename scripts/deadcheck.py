#!/usr/bin/env python3
"""Fail when a module-level name has no reader anywhere in the repository.

Rot re-enters a codebase one abandoned helper at a time, so this runs in the gate.
The rule is deliberately blunt: a module-level ``def``/``class``/constant whose name
appears nowhere except its own definition (and its own ``__all__`` entry) is dead and
must be deleted, not kept "for later".

Names reached dynamically -- extension entry points the loader calls by name, the
console script, pytest hooks -- cannot be seen by a token scan, so they live in
ALLOWED below with the reason they are exempt. That list is the honest cost of the
check: every entry is a name the scanner would otherwise report.

A name mentioned in a comment or docstring counts as a reader, so the check
under-reports rather than failing a build over prose: what it prints is a floor.

Usage: ``python scripts/deadcheck.py`` (exit 1 and a list on failure).
"""

from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Vendored or ported-generated: not ours to prune.
SKIP_DIRS = (
    "misaka/core/documents/pageindex",
    # Vendored verbatim from upstream hermes-lcm; pruning it would be a diff
    # against the very thing the next resync replays. host/ is ours and is checked.
    "misaka/extensions/hermes_lcm/vendor",
    # Upstream's own suite, vendored with it: the harness only measures fidelity
    # while it stays byte-identical to what upstream runs.
    "tests/hermes_lcm_vendor",
)
SKIP_FILES = ("models_generated.py", "image_models_generated.py")

# Names with no textual reader, exempt with the mechanism that reaches them.
ALLOWED: dict[str, str] = {
    # Console script and module entry points.
    "main": "console script (pyproject [project.scripts]) and python -m misaka",
    # misaka.core.wiring imports each registry entry and calls these by name.
    "register": "extension entry point, called by misaka.core.wiring",
    "register_provider": "extension entry point, called by misaka.core.wiring",
    "activate": "extension entry point, called by misaka.core.wiring",
    "SESSION_KINDS": "read with getattr by misaka.core.wiring.build_extensions",
    "DEFAULT_KINDS": "extension-loader gating table",
    # pytest collects these by convention.
    "pytest_collection_modifyitems": "pytest hook",
    "pytest_configure": "pytest hook",
}

# Names with no reader that are staying anyway, by the owner's decision rather than because
# anything reaches them. Kept apart from ALLOWED on purpose: ALLOWED is a claim about how the
# code works, and this is a claim about what was decided. Emptying this list is the goal.
OWNER_EXCLUDED: dict[str, str] = {
    "get_image_model": "image stack, excluded from the sixth-round cleanup",
    "get_image_providers": "image stack, excluded from the sixth-round cleanup",
    "get_image_models": "image stack, excluded from the sixth-round cleanup",
    "should_apply_directness_rank_adjustment": "LCM, excluded from the sixth-round cleanup",
    "compute_directness_rank_bonus_upper_bound": "LCM, excluded from the sixth-round cleanup",
    "compute_like_fallback_fetch_limit": "LCM, excluded from the sixth-round cleanup",
    "compute_search_candidate_cap": "LCM, excluded from the sixth-round cleanup",
}

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def sources() -> list[Path]:
    out: list[Path] = []
    for base in ("misaka", "tests", "scripts"):
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if any(rel.startswith(skip) for skip in SKIP_DIRS) or path.name in SKIP_FILES:
                continue
            out.append(path)
    return out


def _collected_by_pytest(node: ast.AST, in_tests: bool) -> bool:
    """Whether pytest reaches this definition without anyone naming it."""
    if (in_tests and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name.startswith(("test_", "Test"))):
        return True
    decorators = getattr(node, "decorator_list", [])
    return any("fixture" in ast.dump(decorator) for decorator in decorators)


def module_level_names(tree: ast.Module, *, in_tests: bool) -> list[tuple[str, int]]:
    """Public module-level definitions: ``(name, lineno)``.

    Dunders are conventions, and a leading underscore already says "private", but a
    private helper with no caller is exactly the rot this looks for -- so both are in.
    """
    found: list[tuple[str, int]] = []
    for node in tree.body:
        if _collected_by_pytest(node, in_tests):
            continue
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and not node.name.startswith("__")):
            found.append((node.name, node.lineno))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("__"):
                    found.append((target.id, node.lineno))
        elif (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and not node.target.id.startswith("__")):
            found.append((node.target.id, node.lineno))
    return found


def scan() -> list[str]:
    files = sources()
    corpus: Counter[str] = Counter()
    definitions: Counter[str] = Counter()
    exported: Counter[str] = Counter()
    candidates: list[tuple[str, str, int]] = []

    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        corpus.update(_TOKEN.findall(text))
        try:
            tree = ast.parse(text)
        except SyntaxError as error:  # a broken file is a different failure
            return [f"{rel}: cannot parse ({error})"]

        in_tests = rel.startswith("tests/")
        for name, lineno in module_level_names(tree, in_tests=in_tests):
            definitions[name] += 1
            candidates.append((rel, name, lineno))

        # An __all__ entry is not a reader: count it so it cannot mask a dead name.
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
            ):
                for item in ast.walk(node.value):
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        exported[item.value] += 1

    dead: list[str] = []
    for rel, name, lineno in candidates:
        if name in ALLOWED or name in OWNER_EXCLUDED:
            continue
        readers = corpus[name] - definitions[name] - exported[name]
        if readers <= 0:
            note = " (only its __all__ entry)" if exported[name] else ""
            dead.append(f"{rel}:{lineno} {name}{note}")
    return dead


def main() -> int:
    dead = scan()
    if not dead:
        print(
            f"deadcheck: no unreferenced module-level names "
            f"({len(OWNER_EXCLUDED)} kept by decision, see OWNER_EXCLUDED)"
        )
        return 0
    print(f"deadcheck: {len(dead)} module-level name(s) with no reader:", file=sys.stderr)
    for line in dead:
        print(f"  {line}", file=sys.stderr)
    print(
        "\nDelete them, or -- if something reaches them dynamically -- add the name to "
        "ALLOWED in scripts/deadcheck.py with the mechanism that does.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

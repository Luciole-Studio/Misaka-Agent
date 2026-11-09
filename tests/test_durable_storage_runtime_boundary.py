"""pi durable's ``test/storage-runtime-boundary.test.ts``.

Upstream walks the source import graph of each portable entry point and fails on a ``node:`` import or on a module of
another runtime. Python resolves imports when a module loads, so the equivalent asks a fresh interpreter what importing
the entry point loaded: no ``.node`` module of this package, and not ``sqlite3``, the counterpart of ``node:sqlite``.
"""

from __future__ import annotations

import json
import subprocess
import sys


def loadedBy(module: str) -> set[str]:
    """The modules a fresh interpreter has loaded after importing ``module``."""
    code = f"import importlib, json, sys; importlib.import_module({module!r}); print(json.dumps(sorted(sys.modules)))"
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return set(json.loads(completed.stdout.strip().splitlines()[-1]))


def within(loaded: set[str], package: str) -> list[str]:
    return sorted(name for name in loaded if name == package or name.startswith(f"{package}."))


def test_keeps_the_package_root_limited_to_portable_core_storage():
    loaded = loadedBy("misaka.durable")
    assert within(loaded, "misaka.durable.env") == []
    assert within(loaded, "misaka.durable.storage.jsonl") == []
    assert within(loaded, "misaka.durable.storage.sqlite") == []
    assert "sqlite3" not in loaded


def test_keeps_the_portable_sqlite_subpath_free_of_node_imports():
    loaded = loadedBy("misaka.durable.storage.sqlite")
    assert "misaka.durable.storage.sqlite.node" not in loaded
    assert "sqlite3" not in loaded


def test_keeps_the_portable_environment_subpath_free_of_node_imports():
    loaded = loadedBy("misaka.durable.env")
    assert "misaka.durable.env.node" not in loaded


def test_keeps_the_portable_jsonl_subpath_free_of_node_imports():
    loaded = loadedBy("misaka.durable.storage.jsonl")
    assert "misaka.durable.storage.jsonl.node" not in loaded
    assert "misaka.durable.env.node" not in loaded

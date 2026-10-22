"""Facet module loader: ``createHost({"loader": nodeLoader})``. Kept out of ``__init__`` so the core stays free of
module machinery.

Translated from pi's ``packages/chord/src/loader.ts``. MISAKA fork: upstream imports a fresh copy of a facet module
under a new URL query and carries the query, through a Node resolve hook, to the module's relative imports inside a
facet directory. Python's counterpart is a namespace per load: a facet file ``/plugins/a/facet.py`` is imported as
``_chord2_reload.<generation>.plugins.a.facet`` (the dotted name mirrors the path), so its relative imports
(``from . import greeting``, ``from ..b import state``) resolve to modules of the same load, fresh copies, while
absolute imports (``misaka.chord``, shared code) stay single instances. A module outside every facet directory reached
through such a relative import is likewise one instance for all loads (``_chord2_shared.<path>``; it is not the module
the same file is under an absolute name). Upstream evaluates only the modules a facet imports, and a directory is no
module there: a directory above the facet directories is on the dotted name only because the name mirrors the path, so
it is an empty namespace (its ``__init__.py`` never runs). Sources are compiled from the file at every load (no
bytecode cache, which could serve a rewritten file's old code). A module's "default export" is its module attribute
``default`` (an object with ``setup`` and ``uses``, or a mapping with those keys, as a JavaScript object literal).
Facets are named by their file URL, as ``pathToFileURL`` gives it (the path made absolute, symlinks kept).

TODO(loader), as upstream: replace with an isolating, evicting loader. Every reload imports fresh module copies under a
new generation and the old copies are never evicted (they stay in ``sys.modules`` until the process exits). TUI plugins
need isolation, which this loader does not provide.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import os
import sys
import types
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePath
from typing import Any, Final
from urllib.parse import unquote, urlparse

from misaka.chord.host import Facet
from misaka.utils.js import toWellFormed
from misaka.utils.node_path import resolve as resolvePath
from misaka.utils.node_path import sep as pathSep

PARAM: Final = "chord2-reload"
_RELOAD: Final = "_chord2_reload"
_SHARED: Final = "_chord2_shared"

# Directories (with a trailing separator) of loaded facet modules; their files are re-imported on reload.
roots: set[str] = set()
_hooked = False
_generation = 0


def _encode(component: str) -> str:
    """A path component as a dotted-name component (dots are the separator)."""
    return component.replace("%", "%25").replace(".", "%2E")


def _decode(component: str) -> str:
    return component.replace("%2E", ".").replace("%25", "%")


def _moduleName(prefix: str, path: Path) -> str:
    return ".".join([prefix, *(_encode(part) for part in path.parts[1:])])


def _pathOf(parts: Sequence[str]) -> Path:
    return Path(os.sep).joinpath(*(_decode(part) for part in parts))


def _underRoot(path: Path) -> bool:
    text = str(path)
    return any(text.startswith(root) or f"{text}{os.sep}" == root for root in roots)


def _aboveRoot(path: Path) -> bool:
    """A directory above a facet directory, and in none: on module names only because they mirror the path."""
    text = f"{path}{os.sep}"
    return not _underRoot(path) and any(root.startswith(text) for root in roots)


class _SourceLoader(importlib.abc.Loader):
    """Compiles the module from its file at every load."""

    def __init__(self, file: Path | None, alias: str | None = None) -> None:
        self._file = file
        self._alias = alias
        self._aliasSpec: Any = None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType | None:
        if self._alias is not None:
            # A module outside the facet directories: one instance for every load.
            module = importlib.import_module(self._alias)
            self._aliasSpec = module.__spec__
            return module
        return None

    def exec_module(self, module: types.ModuleType) -> None:
        if self._alias is not None:
            # The import system gave the shared module this load's spec; it keeps its own (its relative imports).
            module.__spec__ = self._aliasSpec
            return
        if self._file is None:
            return
        code = compile(self._file.read_text(encoding="utf-8"), str(self._file), "exec", dont_inherit=True)
        exec(code, module.__dict__)  # noqa: S102 - loading a facet module is executing its code


class _ReloadFinder(importlib.abc.MetaPathFinder):
    """Finds ``_chord2_reload.<generation>.<path>`` and ``_chord2_shared.<path>`` modules."""

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> importlib.machinery.ModuleSpec | None:
        parts = fullname.split(".")
        if parts[0] == _RELOAD:
            if len(parts) <= 2:
                return self._namespace(fullname)
            return self._spec(fullname, _pathOf(parts[2:]), shared=False)
        if parts[0] == _SHARED:
            if len(parts) == 1:
                return self._namespace(fullname)
            return self._spec(fullname, _pathOf(parts[1:]), shared=True)
        return None

    def _namespace(self, fullname: str) -> importlib.machinery.ModuleSpec:
        spec = importlib.machinery.ModuleSpec(fullname, _SourceLoader(None), is_package=True)
        spec.submodule_search_locations = []
        return spec

    def _spec(self, fullname: str, target: Path, shared: bool) -> importlib.machinery.ModuleSpec | None:
        alias = None
        if not shared and not _underRoot(target):
            alias = _moduleName(_SHARED, target)
        if target.is_dir():
            init = target / "__init__.py"
            # A directory above the facet directories is no module the facets import: its code does not run.
            code = init if init.is_file() and not _aboveRoot(target) else None
            # A directory without code is no module upstream (imports name files): every load has its own namespace
            # for it, so `from .. import b` finds this load's `b`.
            if code is None:
                alias = None
            spec = importlib.machinery.ModuleSpec(
                fullname,
                _SourceLoader(code if alias is None else None, alias),
                origin=None if code is None else str(code),
                is_package=True,
            )
            # `__file__` of a package's `__init__`, as Python sets it.
            spec.has_location = code is not None
            spec.submodule_search_locations = [str(target)]
            return spec
        file = target.with_name(f"{target.name}.py")
        if file.is_file():
            spec = importlib.machinery.ModuleSpec(fullname, _SourceLoader(file if alias is None else None, alias), origin=str(file))
            spec.has_location = True
            return spec
        return None


def hook() -> None:
    """Installs the finder that resolves facet modules of a load (once)."""
    global _hooked
    if _hooked:
        return
    _hooked = True
    sys.meta_path.insert(0, _ReloadFinder())


def toPath(module: str | PurePath) -> Path:
    """A module given as a path or a file URL, as an absolute path (``path.resolve``: normalized, symlinks kept)."""
    text = str(module)
    if text.startswith("file:"):
        text = unquote(urlparse(text).path)
    return Path(resolvePath(text))


# The characters Node's `pathToFileURL` leaves as they are (letters and digits aside); it encodes every other one.
_URL_PATH_SAFE: Final = frozenset("!$&'()*+,-./:;=@_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")


def pathToFileURL(path: str) -> str:
    """Node's ``pathToFileURL(path).href``: the resolved path with every character outside ``_URL_PATH_SAFE``
    percent-encoded as UTF-8 (Windows: a drive path becomes ``/C:/...``, a UNC path names the host)."""
    resolved = resolvePath(path)
    trailing = path[-1:] == "/" or (sys.platform == "win32" and path[-1:] == "\\")
    if trailing and not resolved.endswith(pathSep):
        resolved += "/"
    host = ""
    if sys.platform == "win32":
        resolved = resolved.replace("\\", "/")
        if resolved.startswith("//"):
            host, _, rest = resolved[2:].partition("/")
            resolved = f"/{rest}"
        else:
            resolved = f"/{resolved}"
    encoded = "".join(
        char if char in _URL_PATH_SAFE else "".join(f"%{byte:02X}" for byte in char.encode("utf-8"))
        for char in toWellFormed(resolved)
    )
    return f"file://{host}{encoded}"


async def nodeLoader(modules: Sequence[str | PurePath]) -> list[Facet]:
    """Imports fresh copies of facet modules (``default``: a FacetDefinition); each facet is named by its file URL."""
    global _generation
    hook()
    _generation += 1
    version = str(_generation)
    facets: list[Facet] = []
    paths = [toPath(module) for module in modules]
    # Every root first: a facet importing another facet's directory gets this load's copy, whatever the order.
    for path in paths:
        roots.add(f"{path.parent}{os.sep}")
    for path in paths:
        name = pathToFileURL(str(path))
        target = path.with_suffix("") if path.suffix == ".py" else path
        loaded = importlib.import_module(".".join([_RELOAD, version, *(_encode(part) for part in target.parts[1:])]))
        definition = getattr(loaded, "default", None)
        setup = _property(definition, "setup")
        if definition is None or not callable(setup):
            raise TypeError(f"{name}: default export must be a facet (defineFacet)")
        facets.append(Facet(name, setup, _property(definition, "uses")))
    return facets


def _property(value: Any, key: str) -> Any:
    """``value[key]`` of a JavaScript object: an object's attribute, or a mapping's key (an object literal's)."""
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)


__all__ = ["nodeLoader"]

"""Chord, durable and env are standalone libraries: the published command line neither imports them nor loads them
(pi 1.2.0 dropped the coding agent's dependency on chord; nothing in pi's coding agent imports durable or env, and
pi #9132: enabling experiments must not pull them into the published CLI). Upstream deleted the experimental
client/server stack that ran on them in 1.2.0, and misaka followed.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "misaka"
REPOSITORY = str(PACKAGE.parent)
# pi-env (misaka.env) runs on durable, durable on chord; the product runs on none of them (D5).
STACK = (
    "misaka.durable",
    "misaka.chord",
    "misaka.env",
)


def _package(path: Path) -> list[str]:
    parts = ["misaka", *path.relative_to(PACKAGE).with_suffix("").parts]
    return parts[:-1]  # the package a module (or an `__init__`) belongs to


def _moduleOf(path: Path) -> str:
    """The module a file in the package is (``__name__``; an ``__init__`` is its package)."""
    parts = ["misaka", *path.relative_to(PACKAGE).with_suffix("").parts]
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


# Calls that import (or locate, or run) the module their first argument names. `import_module` and `find_spec` take a
# relative name's anchor second (or as `package=`); `__import__` imports what its `fromlist` names in the module too.
_IMPORTERS = {"import_module", "__import__", "find_spec", "run_module"}
# Calls that import or run a file, by the index of the argument (and the keyword) holding its path.
_PATH_IMPORTERS = {"spec_from_file_location": (1, "location"), "run_path": (0, "path_name")}
# Calls that run source text: their first argument is scanned as code.
_EVALUATORS = {"exec": "exec", "eval": "eval", "compile": "exec"}
# A path into the stack's directories (`misaka/chord/…`, either separator).
_STACK_PATH = re.compile(r"misaka[/\\](chord|durable|env)(?![\w-])")
# A string constant naming a `misaka.` module, or an object in one (`"misaka.chord.host:createHost"`: an entry point,
# a lazy attribute spec, a patch target).
_DOTTED = "misaka."
# Calls that load what a string argument names (an object path, a patch target), besides the importers above.
_STRING_LOADERS = {"patch", "resolve_name", "locate", "import_string"}
# Calls known to load nothing a string argument names (a logger's name, a message): naming the stack there is not
# reaching it. Every other call may load what its string names (`sys.modules.get(...)`, `pkgutil.get_loader(...)`,
# `monkeypatch.setattr("misaka.….attr", …)`, an entry point's value, a `functools.partial` of an importer), so its string
# arguments keep counting.
_NON_LOADING = {"getLogger", "print", "warn", "debug", "info", "warning", "error", "exception", "critical", "log"}


class _Scope:
    """What the scanner knows of one file beyond each expression: the file (``__name__``, ``__package__``,
    ``__spec__``, ``__file__``), the names bound by exactly one assignment (``NAME = "misaka." + "chord"``, module level
    or in a function) and the names that alias an importer (``load = importlib.import_module``,
    ``from importlib import import_module as load``)."""

    def __init__(self, tree: ast.AST, path: Path | None) -> None:
        self.path = path
        assigned: dict[str, list[ast.expr]] = {}
        self.aliases: dict[str, str] = {}
        importers = {*_IMPORTERS, *_PATH_IMPORTERS, "walk_packages"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                assigned.setdefault(node.targets[0].id, []).append(node.value)
                function = _calleeName(node.value)
                if function in importers:
                    self.aliases[node.targets[0].id] = function
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                assigned.setdefault(node.target.id, []).append(node.value)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in importers and alias.asname is not None:
                        self.aliases[alias.asname] = alias.name
        self.bound = {name: values[0] for name, values in assigned.items() if len(values) == 1}

    def text(self, node: ast.expr, seen: frozenset[str] = frozenset()) -> tuple[str, bool]:
        """The constant text a string expression starts with, and whether that is all of it: constants, ``+``,
        f-strings, ``%`` and ``.format`` on a constant template, ``"sep".join([...])``, ``__name__``/``__package__``/
        ``__spec__.name``/``__spec__.parent`` (from the file's path) and the names bound once to such an expression."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, True
        if isinstance(node, ast.Name):
            if node.id in {"__name__", "__package__"} and self.path is not None:
                module = _moduleOf(self.path)
                package = ".".join(_package(self.path)) if self.path.stem != "__init__" else module
                return (module if node.id == "__name__" else package), True
            value = self.bound.get(node.id)
            if value is not None and node.id not in seen:
                return self.text(value, seen | {node.id})
            return "", False
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "__spec__"
            and node.attr in {"name", "parent"}
        ):
            return self.text(ast.Name("__name__" if node.attr == "name" else "__package__"), seen)
        if isinstance(node, ast.JoinedStr):
            text = ""
            for part in node.values:
                if isinstance(part, ast.Constant):
                    text += part.value
                    continue
                known = isinstance(part, ast.FormattedValue) and part.conversion == -1 and part.format_spec is None
                inner, whole = self.text(part.value, seen) if known else ("", False)
                text += inner
                if not whole:
                    return text, False
            return text, True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, whole = self.text(node.left, seen)
            if not whole:
                return left, False
            right, whole = self.text(node.right, seen)
            return left + right, whole
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            template, whole = self.text(node.left, seen)
            values = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            return self._formatted(template, whole, values, "%", lambda known: template % tuple(known), seen)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            template, whole = self.text(node.func.value, seen)
            if node.func.attr == "format":
                # A keyword argument is not followed: the text before the first placeholder is all that is known.
                values = node.args if not node.keywords else [*node.args, ast.Name("?")]
                return self._formatted(template, whole, values, "{", lambda known: template.format(*known), seen)
            if node.func.attr == "join" and whole and len(node.args) == 1 and isinstance(node.args[0], ast.List | ast.Tuple):
                joined: list[str] = []
                for element in node.args[0].elts:
                    text, complete = self.text(element, seen)
                    joined.append(text)
                    if not complete:
                        return template.join(joined), False
                return template.join(joined), True
        return "", False

    def _formatted(
        self,
        template: str,
        whole: bool,
        values: list[ast.expr],
        marker: str,
        format: Callable[[list[str]], str],
        seen: frozenset[str],
    ) -> tuple[str, bool]:
        """A template filled with ``values``: all of it when the template and every value are known, else the text
        before its first placeholder."""
        if not whole:
            return template, False
        texts = [self.text(value, seen) for value in values]
        if all(complete for _, complete in texts):
            try:
                return format([text for text, _ in texts]), True
            except (TypeError, ValueError, IndexError, KeyError):
                pass
        return template.replace(marker * 2, "\0").split(marker, 1)[0].replace("\0", marker), False

    def file(self, node: ast.expr) -> Path | str | None:
        """The file a path expression names: a constant, or a join of constants onto ``__file__``
        (``Path(__file__).parent / "x"``, ``os.path.join(os.path.dirname(__file__), "x")``)."""
        if isinstance(node, ast.Name) and node.id == "__file__":
            return self.path
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            base = self.file(node.value)
            return base.parent if isinstance(base, Path) else None
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
            base = self.file(node.value.value)
            index = node.slice.value if isinstance(node.slice, ast.Constant) else None
            if isinstance(base, Path) and isinstance(index, int) and 0 <= index < len(base.parents):
                return base.parents[index]
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            base = self.file(node.left)
            name, whole = self.text(node.right)
            return base / name if isinstance(base, Path) and whole else None
        if isinstance(node, ast.Call):
            function = _calleeName(node)
            arguments = [self.file(argument) for argument in node.args]
            if function in {"Path", "PurePath", "abspath", "realpath", "normpath"} and len(arguments) == 1:
                return arguments[0] if not isinstance(arguments[0], str) else Path(arguments[0])
            if function == "resolve" and isinstance(node.func, ast.Attribute) and not node.args:
                return self.file(node.func.value)
            if function == "dirname" and len(arguments) == 1:
                return arguments[0].parent if isinstance(arguments[0], Path) else None
            if function == "join" and arguments and isinstance(arguments[0], Path):
                joined = arguments[0]
                for argument in node.args[1:]:
                    name, whole = self.text(argument)
                    if not whole:
                        return None
                    joined = joined / name
                return joined
        text, whole = self.text(node)
        return text if whole else None


def _calleeName(node: ast.expr) -> str | None:
    """The name a call (or a bare reference) uses for its function: ``f``, ``module.f``, ``getattr(module, "f")``."""
    function = node.func if isinstance(node, ast.Call) else node
    if isinstance(function, ast.Call) and getattr(function.func, "id", None) == "getattr" and len(function.args) == 2:
        attribute = function.args[1]
        return attribute.value if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str) else None
    return getattr(function, "attr", getattr(function, "id", None))


def _argument(call: ast.Call, index: int, keyword: str) -> ast.expr | None:
    if len(call.args) > index:
        return call.args[index]
    return next((item.value for item in call.keywords if item.arg == keyword), None)


def _resolve(module: str, anchor: str) -> str:
    """``importlib.util.resolve_name(module, anchor)`` for a relative name."""
    level = len(module) - len(module.lstrip("."))
    parts = anchor.split(".")
    parts = parts[: len(parts) - (level - 1)] if level > 1 else parts
    return ".".join([*parts, *([module.lstrip(".")] if module.lstrip(".") else [])])


def _named(call: ast.Call, scope: _Scope) -> list[str]:
    """The modules an importing call names: a known name, or for one computed at run time its constant prefix followed
    by ``*`` (a relative name with an unknown anchor is ``*``); ``__import__``'s ``fromlist`` entries too."""
    function = _calleeName(call)
    function = scope.aliases.get(function, function) if isinstance(call.func, ast.Name) else function
    if function in _IMPORTERS:
        target = _argument(call, 0, "name")
        if target is None:
            return []
        module, whole = scope.text(target)
        anchorNode = next((item.value for item in call.keywords if item.arg == "package"), None)
        if anchorNode is None and function in {"import_module", "find_spec"} and len(call.args) > 1:
            anchorNode = call.args[1]
        if module.startswith(".") and anchorNode is not None:
            anchor, known = scope.text(anchorNode)
            if not known:
                return ["*"]
            module = _resolve(module, anchor)
        named = [module if whole else f"{module}*"]
        fromlist = _argument(call, 3, "fromlist") if function == "__import__" else None
        if whole and isinstance(fromlist, ast.List | ast.Tuple):
            for item in fromlist.elts:
                text, complete = scope.text(item)
                named.append(f"{module}.{text}" if complete else f"{module}.{text}*")
        return named
    if function in _PATH_IMPORTERS:
        index, keyword = _PATH_IMPORTERS[function]
        location = _argument(call, index, keyword)
        target = None if location is None else scope.file(location)
        return [] if target is None else _pathModules(target)
    if function == "walk_packages":
        # A walk imports every package under the path it is given (to find their submodules): over `misaka.__path__`,
        # or with a `misaka.` prefix, it reaches the stack.
        where = _argument(call, 0, "path")
        prefix, _ = scope.text(_argument(call, 1, "prefix") or ast.Constant(""))
        if isinstance(where, ast.Attribute) and where.attr == "__path__":
            dotted = ast.unparse(where.value)
            if dotted == "misaka" or dotted.startswith(_DOTTED):
                return [f"{dotted}.*"]
        return [f"{prefix}*"] if prefix.startswith(_DOTTED) else []
    if function in _EVALUATORS and isinstance(call.func, ast.Name) and call.args:
        source, whole = scope.text(call.args[0])
        if whole:
            try:
                tree = ast.parse(source, mode=_EVALUATORS[function])
            except SyntaxError:
                return []
            return [module for module, _ in importedModules(tree, scope.path)]
    return []


def _pathModules(target: Path | str) -> list[str]:
    """The stack module a file path points into: the module itself for a path inside the package, else the stack
    package a ``misaka/chord/…`` path names."""
    if isinstance(target, Path) and target.is_absolute():
        try:
            return [".".join(["misaka", *target.relative_to(PACKAGE).with_suffix("").parts])]
        except ValueError:
            return []
    return [f"misaka.{match.group(1)}" for match in _STACK_PATH.finditer(str(target))]


def _plainArguments(tree: ast.AST, scope: _Scope) -> set[int]:
    """The string constants passed straight to a call known to load nothing (``_NON_LOADING``:
    ``logging.getLogger("misaka.chord")``, ``print(...)``, a logger method), by node id: naming a module there is not
    reaching it. A string passed to any other call, or anywhere else (an assignment, a table, a subscript such as
    ``sys.modules[...]``), may be loaded or handed on, so it keeps counting."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = _calleeName(node)
        function = scope.aliases.get(function, function) if isinstance(node.func, ast.Name) else function
        if function not in _NON_LOADING:
            continue
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                found.add(id(argument))
    return found


def _docstrings(tree: ast.AST) -> set[int]:
    """The docstrings of a file (prose, not code), by node id."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def reaches(module: str, packages: tuple[str, ...]) -> bool:
    """Whether ``module`` is in ``packages``; a computed name (``prefix*``) when one of them can start with it."""
    if not module.endswith("*"):
        return under(module, packages)
    prefix = module[:-1]
    # A name computed from nothing known (`import_module(name)`) is judged by the run-time tests below.
    return prefix != "" and any(package.startswith(prefix) or prefix.startswith(f"{package}.") for package in packages)


def importedModules(tree: ast.AST, path: Path | None = None) -> list[tuple[str, int]]:
    """Every module a file imports or reaches:

    - ``import`` and ``from … import`` statements, absolute and relative, at any depth (``TYPE_CHECKING``,
      ``if False``, ``try`` included);
    - calls of ``importlib.import_module``/``__import__`` (with its ``fromlist``)/``find_spec`` (with its anchor)/
      ``runpy.run_module``, also through an alias or ``getattr``: their name evaluated by ``_Scope.text``, a name
      computed at run time as its constant prefix and ``*``;
    - files given to ``spec_from_file_location``/``runpy.run_path`` (``__file__``-relative joins resolved), and
      ``pkgutil.walk_packages`` over a ``misaka`` package's ``__path__``;
    - the code ``exec``/``eval``/``compile`` run when it is a known string;
    - attribute chains on the ``misaka`` package (``misaka.chord.createHost``, ``importlib.reload(misaka.chord)``);
    - string constants naming a ``misaka.`` module or an object in one (``"misaka.chord.host:createHost"``), or a path
      into the stack's directories (``"misaka/chord/node.py"``); docstrings are prose and are not read.

    A name computed from nothing the file states (``import_module(name)`` with ``name`` from a loop or a parameter) is
    out of a static scan's reach: the run-time tests below load the command line, the chat panel and research and check
    that none of the stack is in ``sys.modules``."""
    modules: list[tuple[str, int]] = []
    scope = _Scope(tree, path)
    docstrings = _docstrings(tree)
    notLoaded = _plainArguments(tree, scope)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            elif path is not None:
                package = _package(path)
                package = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                base = ".".join([*package, *([node.module] if node.module else [])])
            else:
                continue
            if base:
                modules += [(base, node.lineno)] + [(f"{base}.{alias.name}", node.lineno) for alias in node.names]
        elif isinstance(node, ast.Call):
            modules += [(named, node.lineno) for named in _named(node, scope)]
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            dotted = ast.unparse(node)
            if dotted.startswith(_DOTTED) and dotted.replace(".", "").replace("_", "").isalnum():
                modules.append((dotted, node.lineno))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings | notLoaded:
            if node.value.startswith(_DOTTED):
                target = node.value.split(":", 1)[0]
                if target.replace(".", "").replace("_", "").isalnum():
                    modules.append((target, node.lineno))
            modules += [(module, node.lineno) for module in _pathModules(node.value)]
    return modules


def under(module: str, packages: tuple[str, ...]) -> bool:
    return any(module == package or module.startswith(f"{package}.") for package in packages)


# How the scan judges each way of reaching a module (`source`, as the file `misaka/<file>`): `True` where the stack is
# reached (or may be: a computed name whose known prefix can name it), `False` for the controls that do not reach it.
PROBES = [
    ("import_module const", "import importlib\nimportlib.import_module('misaka.chord')", "core/x.py", True),
    ("import_module concat const", "import importlib\nimportlib.import_module('misaka.' + 'chord')", "core/x.py", True),
    ("fstring misaka.{x}", "import importlib\nimportlib.import_module(f'misaka.{x}')", "core/x.py", True),
    ("percent format", "import importlib\nimportlib.import_module('misaka.%s' % x)", "core/x.py", True),
    ("percent format known", "import importlib\nimportlib.import_module('misaka.%s' % 'env')", "core/x.py", True),
    ("str.format", "import importlib\nimportlib.import_module('misaka.{}'.format(x))", "core/x.py", True),
    ("str.format keyword", "import importlib\nimportlib.import_module('misaka.{n}'.format(n=x))", "core/x.py", True),
    ("'.'.join", "import importlib\nimportlib.import_module('.'.join(['misaka','chord']))", "core/x.py", True),
    ("__import__ const", "__import__('misaka.chord')", "core/x.py", True),
    ("__import__ fromlist", "__import__('misaka', fromlist=['chord'])", "core/x.py", True),
    ("__import__ positional fromlist", "__import__('misaka', globals(), locals(), ['durable'])", "core/x.py", True),
    ("importlib.__import__", "import importlib\nimportlib.__import__('misaka.chord')", "core/x.py", True),
    ("builtins.__import__", "import builtins\nbuiltins.__import__('misaka.chord')", "core/x.py", True),
    ("find_spec const", "import importlib.util\nimportlib.util.find_spec('misaka.chord')", "core/x.py", True),
    ("find_spec relative", "import importlib.util\nimportlib.util.find_spec('.chord', 'misaka')", "core/x.py", True),
    ("find_spec package=", "import importlib.util\nimportlib.util.find_spec('.env', package='misaka')", "core/x.py", True),
    (
        "spec_from_file_location path",
        "import importlib.util\nimportlib.util.spec_from_file_location('c', 'misaka/chord/host.py')",
        "core/x.py",
        True,
    ),
    (
        "spec_from_file_location Path(__file__)",
        (
            "import importlib.util, pathlib\n"
            "importlib.util.spec_from_file_location('c', pathlib.Path(__file__).parent.parent / 'chord' / 'host.py')"
        ),
        "core/x.py",
        True,
    ),
    (
        "spec_from_file_location os.path",
        (
            "import importlib.util, os\n"
            "importlib.util.spec_from_file_location('c', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'env'))"
        ),
        "core/x.py",
        True,
    ),
    ("runpy.run_module", "import runpy\nrunpy.run_module('misaka.chord.node')", "core/x.py", True),
    ("runpy.run_path", "import runpy\nrunpy.run_path('misaka/chord/node.py')", "core/x.py", True),
    ("pkgutil.resolve_name", "import pkgutil\npkgutil.resolve_name('misaka.chord:createHost')", "core/x.py", True),
    ("pkgutil.walk_packages", "import pkgutil, misaka\nlist(pkgutil.walk_packages(misaka.__path__, 'misaka.'))", "core/x.py", True),
    (
        "pkgutil.iter_modules + import",
        (
            "import pkgutil, importlib, misaka\nfor m in pkgutil.iter_modules(misaka.__path__):\n"
            "    importlib.import_module('misaka.' + m.name)"
        ),
        "core/x.py",
        True,
    ),
    ("entry point string", "EP = 'misaka.chord.node:main'", "core/x.py", True),
    ("sys.modules", "import sys\nsys.modules['misaka.chord']", "core/x.py", True),
    ("from misaka import chord", "from misaka import chord", "core/x.py", True),
    ("from . import chord (misaka/__init__)", "from . import chord", "__init__.py", True),
    ("from . import chord (misaka/x.py)", "from . import chord", "x.py", True),
    ("from .. import chord", "from .. import chord", "a/b.py", True),
    ("from ..chord import x", "from ..chord import x", "a/__init__.py", True),
    ("import misaka.chord as c", "import misaka.chord as c", "core/x.py", True),
    ("alias of import_module", "import importlib\nim = importlib.import_module\nim('misaka.chord')", "core/x.py", True),
    ("alias concat", "import importlib\nim = importlib.import_module\nim('misaka.' + 'chord')", "core/x.py", True),
    ("from importlib import as", "from importlib import import_module as load\nload('misaka.' + 'env')", "core/x.py", True),
    ("getattr importer", "import importlib\ngetattr(importlib, 'import_module')('misaka.' + 'chord')", "core/x.py", True),
    ("exec import", "exec('import misaka.chord')", "core/x.py", True),
    ("eval __import__", "eval(\"__import__('misaka.chord')\")", "core/x.py", True),
    ("compile", "exec(compile('from misaka import chord', 'x', 'exec'))", "core/x.py", True),
    ("module constant concat", "import importlib\nNAME = 'misaka.ch' + 'ord'\nimportlib.import_module(NAME)", "core/x.py", True),
    ("module constant", "import importlib\nNAME = 'misaka.chord'\nimportlib.import_module(NAME)", "core/x.py", True),
    (
        "function-local constant",
        "import importlib\ndef f():\n    name = 'misaka.' + 'durable'\n    return importlib.import_module(name)",
        "core/x.py",
        True,
    ),
    ("import_module(name=)", "import importlib\nimportlib.import_module(name='misaka.' + 'chord')", "core/x.py", True),
    ("__name__ relative", "import importlib\nimportlib.import_module(f'{__name__}.chord')", "__init__.py", True),
    ("__package__ relative", "import importlib\nimportlib.import_module('.chord', __package__)", "x.py", True),
    ("__name__ + '.chord'", "import importlib\nimportlib.import_module(__name__ + '.chord')", "__init__.py", True),
    ("__spec__.parent relative", "import importlib\nimportlib.import_module('.chord', __spec__.parent)", "x.py", True),
    ("TYPE_CHECKING", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from misaka.chord import Host", "core/x.py", True),
    ("if False", "if False:\n    import misaka.durable", "core/x.py", True),
    ("try/except", "try:\n    import misaka.env\nexcept ImportError:\n    pass", "core/x.py", True),
    (
        "lazy __getattr__ table",
        (
            "import importlib\n_T={'Host':('misaka.chord','Host')}\ndef __getattr__(n):\n    m,a=_T[n]\n"
            "    return getattr(importlib.import_module(m),a)"
        ),
        "core/x.py",
        True,
    ),
    (
        "lazy __getattr__ short names",
        "import importlib\n_T={'Host':'chord'}\ndef __getattr__(n):\n    return importlib.import_module('misaka.'+_T[n])",
        "core/x.py",
        True,
    ),
    ("importlib.reload", "import importlib, misaka\nimportlib.reload(misaka.chord)", "core/x.py", True),
    ("mock.patch target", "from unittest import mock\nmock.patch('misaka.chord.host.createHost')", "core/x.py", True),
    ("file path string", "P = 'misaka/chord/host.py'", "core/x.py", True),
    ("windows path string", "P = 'misaka\\\\durable\\\\tool.py'", "core/x.py", True),
    ("attribute access", "import misaka\nmisaka.chord.createHost", "core/x.py", True),
    # Calls outside the importers that still load, or hand on, the module a string names.
    ("functools.partial of an importer", "import functools, importlib\nfunctools.partial(importlib.import_module, 'misaka.chord')()", "core/x.py", True),
    ("sys.modules.get", "import sys\nsys.modules.get('misaka.chord')", "core/x.py", True),
    ("lambda around an importer", "import importlib\n(lambda n: importlib.import_module(n))('misaka.chord')", "core/x.py", True),
    (
        "entry point value",
        "from importlib.metadata import EntryPoint\nEntryPoint(name='x', value='misaka.chord.host:createHost', group='g').load()",
        "core/x.py",
        True,
    ),
    ("pkgutil.get_loader", "import pkgutil\npkgutil.get_loader('misaka.chord')", "core/x.py", True),
    ("sys.modules.__getitem__", "import sys\nsys.modules.__getitem__('misaka.chord')", "core/x.py", True),
    ("monkeypatch.setattr target", "def t(monkeypatch):\n    monkeypatch.setattr('misaka.chord.host.createHost', None)", "core/x.py", True),
    # Calls known to load nothing (`_NON_LOADING`): a module name passed to them is no reach.
    ("logger name", "import logging\nlogging.getLogger('misaka.chord')", "core/x.py", False),
    ("logger message", "import logging\nlogging.getLogger(__name__).info('misaka.durable')", "core/x.py", False),
    ("warning text", "import warnings\nwarnings.warn('misaka.env')", "core/x.py", False),
    ("logger name, keyword", "import logging\nlogging.getLogger(name='misaka.durable')", "core/x.py", False),
    ("printed name", "print('misaka.env')", "core/x.py", False),
    # Names a computed import's known prefix can still complete to the stack: judged as reaching it.
    ("prefix misaka.e", "import importlib\nimportlib.import_module(f'misaka.e{x}')", "core/x.py", True),
    ("prefix misaka.", "import importlib\nimportlib.import_module('misaka.' + sub)", "core/x.py", True),
    # Controls.
    ("docstring mention", '"""This module never uses misaka.chord."""', "core/x.py", False),
    ("docstring exact", '"""misaka.chord"""', "core/x.py", False),
    ("misaka.environment", "X = 'misaka.environment'", "core/x.py", False),
    ("misaka/environment path", "P = 'misaka/environment/x.py'", "core/x.py", False),
    ("prefix of another package", "import importlib\nimportlib.import_module(f'misaka.extensions.{x}')", "core/x.py", False),
    ("comment", "# import misaka.chord\nx = 1", "core/x.py", False),
    ("fully dynamic", "import importlib\ndef f(name):\n    return importlib.import_module(name)", "core/x.py", False),
    ("own subpackage", "import importlib\nimportlib.import_module(f'{__name__}.chord')", "core/__init__.py", False),
]


@pytest.mark.parametrize(("source", "file", "caught"), [probe[1:] for probe in PROBES], ids=[probe[0] for probe in PROBES])
def test_the_scan_judges_each_way_of_reaching_a_module(source: str, file: str, caught: bool):
    modules = importedModules(ast.parse(source), PACKAGE / file)
    assert any(reaches(module, STACK) for module, _ in modules) is caught, modules


def test_nothing_outside_the_stack_imports_it():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).parts
        if under(".".join(["misaka", *relative[:1]]), STACK):
            continue
        for module, line in importedModules(ast.parse(path.read_text(encoding="utf-8")), path):
            if reaches(module, STACK):
                offenders.append(f"{path.relative_to(PACKAGE.parent)}:{line}: {module}")
    assert offenders == []


def test_the_published_command_line_does_not_load_the_stack(tmp_path):
    program = (
        "import sys\n"
        "from misaka.__main__ import main\n"
        "try:\n"
        "    main(['--version'])\n"
        "except SystemExit:\n"
        "    pass\n"
        f"loaded = sorted(name for name in sys.modules if any(name == p or name.startswith(p + '.') for p in {STACK!r}))\n"
        "print('LOADED', loaded)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={
            **os.environ,
            "MISAKA_HOME": str(tmp_path / "home"),
            "MISAKA_OFFLINE": "1",
            "PI_EXPERIMENTAL": "1",
            "PYTHONPATH": REPOSITORY,
            "PYTHONUTF8": "1",
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "LOADED []"


def test_the_chat_panel_and_research_do_not_load_the_stack(tmp_path):
    program = (
        "import sys\n"
        "import misaka.modes.interactive.interactive_mode, misaka.ui.panel, misaka.core.research.workflow\n"
        f"loaded = sorted(name for name in sys.modules if any(name == p or name.startswith(p + '.') for p in {STACK!r}))\n"
        "print('LOADED', loaded)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env={**os.environ, "MISAKA_HOME": str(tmp_path / "home"), "PYTHONPATH": REPOSITORY, "PYTHONUTF8": "1"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "LOADED []"

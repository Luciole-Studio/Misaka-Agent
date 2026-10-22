"""pi chord's ``test/loader.test.ts`` with its scenario ``test/fixtures/loader.ts``.

MISAKA adaptations: upstream runs the scenario in a child Node process because vitest's module runner bypasses the
resolve hooks the loader installs; Python's loader is a meta path finder that works in-process, so the scenario runs
here, on facet modules written to ``tmp_path`` (Python files: relative imports are ``from .greeting import greeting``
and ``from ..b.state import state``, the dynamic import of another facet's module is ``importlib.import_module`` with
the module's own package, the default export is the module attribute ``default``).
"""

from __future__ import annotations

import pytest

from misaka.chord import createHost, local, method
from misaka.chord._typebox import Type
from misaka.chord.loader import nodeLoader

# The test's own copy of the plugin's contract: binding is by description, not object identity.
Hello = local(
    "t.hello",
    {"version": 1, "input": Type.Object({}), "members": {"greet": method(Type.String(), Type.String())}},
)

facetSource = """
from misaka.chord import defineFacet, local, method
from misaka.chord._typebox import Type

from .greeting import greeting

Hello = local(
    "t.hello",
    {"version": 1, "input": Type.Object({}), "members": {"greet": method(Type.String(), Type.String())}},
)

default = defineFacet(lambda host: host.serve(Hello, lambda *_: {"greet": lambda name, ctx: greeting + " " + name}))
"""


async def test_loads_facet_modules_and_reloads_them_with_fresh_copies_of_their_relative_imports(tmp_path):
    directory = tmp_path / f"plugins-{id(tmp_path)}"
    # Separate from `directory`, which becomes a reload root once its facet loads.
    plugins = tmp_path / f"plugins-{id(tmp_path)}-order"
    directory.mkdir()

    def writeGreeting(text: str) -> None:
        (directory / "greeting.py").write_text(f"greeting = {text!r}\n", encoding="utf-8")

    (directory / "facet.py").write_text(facetSource, encoding="utf-8")
    writeGreeting("v1")
    host = createHost({"self": {"kind": "host"}, "loader": nodeLoader, "onError": lambda _error: None})
    await host.load([str(directory / "facet.py")])
    ref = host.hold(Hello, {})
    assert await ref.greet("x") == "v1 x"
    writeGreeting("v2")
    report = await host.reload([str(directory / "facet.py")])
    assert report["changed"] == ["t.hello"]
    assert await ref.greet("x") == "v2 x"
    await host.close()

    # A facet listed first that imports a later facet's directory shares that facet's copy of the module.
    Shared = local(
        "t.shared",
        {"version": 1, "input": Type.Object({}), "members": {"same": method(None, Type.Boolean())}},
    )
    (plugins / "a").mkdir(parents=True)
    (plugins / "b").mkdir(parents=True)
    (plugins / "b" / "state.py").write_text("state = {}\n", encoding="utf-8")
    (plugins / "b" / "facet.py").write_text(
        "from misaka.chord import defineFacet\nfrom .state import state\nown = state\ndefault = defineFacet(lambda host: None)\n",
        encoding="utf-8",
    )
    (plugins / "a" / "facet.py").write_text(
        "import importlib\n"
        "from misaka.chord import defineFacet, local, method\n"
        "from misaka.chord._typebox import Type\n"
        "from ..b.state import state\n"
        'Shared = local("t.shared", {"version": 1, "input": Type.Object({}), "members": {"same": method(None, Type.Boolean())}})\n'
        "async def same(ctx):\n"
        '    return importlib.import_module("..b.facet", __package__).own is state\n'
        'default = defineFacet(lambda host: host.serve(Shared, lambda *_: {"same": same}))\n',
        encoding="utf-8",
    )
    ordered = createHost({"self": {"kind": "host"}, "loader": nodeLoader, "onError": lambda _error: None})
    await ordered.load([str(plugins / "a" / "facet.py"), str(plugins / "b" / "facet.py")])
    assert await ordered.hold(Shared, {}).same() is True
    await ordered.close()


async def test_needs_a_configured_loader_for_modules():
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    with pytest.raises(RuntimeError, match=r"createHost\(\{ loader \}\)"):
        await host.load(["./facet.py"])

"""pi chord's ``test/context.test.ts`` and ``test/boundary.test.ts``."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from misaka.chord.context import (
    BACKGROUND_CONTEXT,
    TODO_CONTEXT,
    awaitWithContext,
    createContextKey,
    withAbortSignal,
    withCancel,
    withContextValue,
    withoutAbortSignal,
)
from misaka.utils.js import UNDEFINED, AbortController


def test_provides_distinct_empty_root_contexts():
    key = createContextKey("value")

    assert TODO_CONTEXT is not BACKGROUND_CONTEXT
    assert TODO_CONTEXT.abortSignal is None
    assert TODO_CONTEXT.value(key) is UNDEFINED
    assert str(BACKGROUND_CONTEXT) == "[Context BACKGROUND_CONTEXT]"
    assert str(TODO_CONTEXT) == "[Context TODO_CONTEXT]"


def test_layers_typed_values_without_modifying_parents():
    firstKey = createContextKey("first")
    secondKey = createContextKey("second")
    first = withContextValue(firstKey, "one", BACKGROUND_CONTEXT)
    second = withContextValue(secondKey, 2, first)
    replaced = withContextValue(firstKey, "updated", second)

    assert BACKGROUND_CONTEXT.value(firstKey) is UNDEFINED
    assert first.value(firstKey) == "one"
    assert first.value(secondKey) is UNDEFINED
    assert second.value(firstKey) == "one"
    assert second.value(secondKey) == 2
    assert replaced.value(firstKey) == "updated"
    assert second.value(firstKey) == "one"
    assert str(replaced) == "[Context BACKGROUND_CONTEXT].WithValue(first).WithValue(second).WithValue(first)"


def test_inherits_parent_cancellation_and_isolates_child_cancellation():
    parentController = AbortController()
    parent = withAbortSignal(parentController.signal, BACKGROUND_CONTEXT)
    child = withCancel(parent)
    sibling = withCancel(parent)
    childListener = []
    child.context.abortSignal.addEventListener("abort", lambda: childListener.append(True))

    child.cancel("child")
    assert child.context.abortSignal.aborted is True
    assert child.context.abortSignal.reason == "child"
    assert sibling.context.abortSignal.aborted is False
    assert parent.abortSignal.aborted is False
    assert len(childListener) == 1

    parentController.abort("parent")
    assert sibling.context.abortSignal.aborted is True
    assert sibling.context.abortSignal.reason == "parent"


def test_masks_caller_cancellation_for_mandatory_cleanup():
    controller = AbortController()
    key = createContextKey("value")
    context = withContextValue(key, "preserved", withAbortSignal(controller.signal, BACKGROUND_CONTEXT))
    cleanup = withoutAbortSignal(context)

    controller.abort()
    assert context.abortSignal.aborted is True
    assert cleanup.abortSignal is None
    assert cleanup.value(key) == "preserved"


async def test_stops_waiting_when_the_invocation_is_cancelled():
    controller = AbortController()
    context = withAbortSignal(controller.signal, BACKGROUND_CONTEXT)
    work = asyncio.get_running_loop().create_future()
    waiting = asyncio.ensure_future(awaitWithContext(work, context))
    cancellation = RuntimeError("cancelled")
    await asyncio.sleep(0)

    controller.abort(cancellation)
    with pytest.raises(RuntimeError) as raised:
        await waiting
    assert raised.value is cancellation
    work.set_result("completed later")
    assert await work == "completed later"

    async def completed():
        return "completed"

    assert await awaitWithContext(completed(), BACKGROUND_CONTEXT) == "completed"


async def test_does_not_cancel_the_underlying_work_and_reports_an_error_it_ends_with():
    controller = AbortController()
    context = withAbortSignal(controller.signal, BACKGROUND_CONTEXT)
    work = asyncio.get_running_loop().create_future()
    waiting = asyncio.ensure_future(awaitWithContext(work, context))
    await asyncio.sleep(0)
    controller.abort()
    with pytest.raises(Exception, match="aborted"):
        await waiting
    assert not work.cancelled()
    work.set_exception(ValueError("late failure"))
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="late failure"):
        await work


# -- package boundary ----------------------------------------------------------------------------

# The modules outside misaka.chord that chord may import: the JavaScript built-ins it relies on, Node's `path` (the
# loader names facets as `pathToFileURL` does) and the identity set.
ALLOWED = {
    "misaka.utils.js",
    "misaka.utils.js_regexp",
    "misaka.utils.node_path",
    "misaka.utils.identity",
    "misaka.utils.collation",
}


def test_does_not_depend_on_other_harness_packages():
    source = Path(__file__).resolve().parents[1] / "misaka" / "chord"
    violations = []
    for path in sorted(source.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
                names = [node.module]
            elif isinstance(node, ast.ImportFrom) and node.level > 0:
                # A relative import is judged by what it resolves to, as upstream judges a relative specifier.
                package = ["misaka", "chord", *path.relative_to(source).parts[:-1]]
                base = package[: len(package) - (node.level - 1)]
                names = [".".join([*base, *(node.module.split(".") if node.module else [])])]
            for name in names:
                if name.startswith("misaka") and not name.startswith("misaka.chord") and name not in ALLOWED:
                    violations.append(f"{path.relative_to(source)}: {name}")
    assert violations == []

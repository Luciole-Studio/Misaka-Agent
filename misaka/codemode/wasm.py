"""Translated from pi's ``packages/codemode/src/wasm.ts``.

MISAKA fork: the module is compiled for wasmtime (``quickjs_wasi.engine()``) on a thread of its own, so the event
loop is not held while it compiles, and the default file is the copy of ``quickjs-wasi/quickjs.wasm`` that ships in
``misaka/codemode/quickjs_wasi``. The cache is per process, not per event loop.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import os
import threading
from typing import Any

from . import quickjs_wasi

# A compiled `wasmtime.Module` of `quickjs.wasm`, for `quickjs_wasi.engine()`.
type CodemodeWasmModule = Any

_modules: dict[str, concurrent.futures.Future[CodemodeWasmModule]] = {}
_lock = threading.Lock()


def loadQuickJSWasm(path: str | os.PathLike[str] | None = None) -> asyncio.Future[CodemodeWasmModule]:
    """Read and compile the QuickJS wasm once per path. ``path`` defaults to the file shipped with misaka; pass it
    when that file lives elsewhere. A failed load is retried on the next call. Call it on the running event loop."""
    resolved = str(quickjs_wasi.WASM_PATH) if path is None else os.fspath(path)
    with _lock:
        module = _modules.get(resolved)
        if module is None:
            module = _modules[resolved] = concurrent.futures.Future()
            threading.Thread(target=_compile, args=(resolved, module), name="codemode-wasm", daemon=True).start()
    return asyncio.wrap_future(module)


def _compile(path: str, module: concurrent.futures.Future[CodemodeWasmModule]) -> None:
    try:
        with open(path, "rb") as file:
            compiled = quickjs_wasi.compileModule(file.read())
    except Exception as error:  # noqa: BLE001 - the load's failure, for its awaiters
        with _lock:
            if _modules.get(path) is module:
                del _modules[path]
        module.set_exception(error)
        return
    module.set_result(compiled)


@atexit.register
def _closeModules() -> None:
    """Release the compiled modules while wasmtime-py's bindings still exist (see ``quickjs_wasi._closeEngine``)."""
    with _lock:
        modules = list(_modules.values())
        _modules.clear()
    for module in modules:
        if module.done() and module.exception() is None:
            module.result().close()


__all__ = ["CodemodeWasmModule", "loadQuickJSWasm"]

"""Translated from pi's ``packages/ai/src/utils/abort-signals.ts`` (v1.1.0).

MISAKA fork: signals are duck-typed (see ``misaka.utils._abort_signal``); a listener is added with
``misaka.utils.js.addAbortListener``, which returns the function that removes it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from misaka.utils.js import UNDEFINED, AbortController, addAbortListener
from misaka.utils.values import signal_aborted


@dataclass(slots=True)
class CombinedAbortSignal:
    cleanup: Callable[[], None]
    signal: Any = None


def combineAbortSignals(signals: list[Any]) -> CombinedAbortSignal:
    activeSignals = [signal for signal in signals if signal is not None]
    if len(activeSignals) == 0:
        return CombinedAbortSignal(cleanup=lambda: None)
    if len(activeSignals) == 1:
        return CombinedAbortSignal(signal=activeSignals[0], cleanup=lambda: None)

    controller = AbortController()
    listeners: list[Callable[[], None]] = []

    def abort(signal: Any) -> None:
        if not controller.signal.aborted:
            # `signal.reason` is undefined for a signal that carries none; the controller then makes its AbortError.
            reason = getattr(signal, "reason", None)
            controller.abort(UNDEFINED if reason is None else reason)

    for signal in activeSignals:
        if signal_aborted(signal):
            abort(signal)
            break

        def listener(signal: Any = signal) -> None:
            abort(signal)

        listeners.append(addAbortListener(signal, listener))

    def cleanup() -> None:
        for remove in listeners:
            remove()

    return CombinedAbortSignal(signal=controller.signal, cleanup=cleanup)


__all__ = ["CombinedAbortSignal", "combineAbortSignals"]

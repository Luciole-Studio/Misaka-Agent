"""A process-local gate for foreground requests on one session's model route."""

from __future__ import annotations

import math
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal
from uuid import uuid4

from misaka.ai.runtime_observation import (
    has_observation_sink,
    observation_context,
    observe,
    request_identity,
)

type Route = tuple[str, str, str, str, str]  # session, provider, API, endpoint, model

_LIMIT = 5
_WINDOW = 120.0
_COOLDOWN = 30


class CircuitOpenError(RuntimeError):
    pass


@dataclass(slots=True)
class _Circuit:
    failures: deque[float] = field(default_factory=deque)
    open_until: float | None = None
    epoch: int = 0
    in_flight: int = 0
    probing: bool = False


@dataclass(frozen=True, slots=True)
class Permit:
    route: Route
    epoch: int
    probe: bool


def _record_rejection(route: Route, remaining: int, probing: bool) -> None:
    if not has_observation_sink():
        return
    auxiliary = request_identity() is None
    context = observation_context(request_id=uuid4().hex, auxiliary=True) if auxiliary else nullcontext()
    with context:
        if auxiliary:
            observe("model_started", provider=route[1], model=route[4])
        observe("circuit_rejected", provider=route[1], model=route[4], delay_ms=remaining,
                reason="probe_pending" if probing else "circuit_open")
        observe("http_observable")  # The provider and its transport were never called.
        if auxiliary:
            observe("model_finished", status="error", duration_ms=0)


class RetryCircuits:
    def __init__(self) -> None:
        self._routes: dict[Route, _Circuit] = {}

    def admit(self, route: Route, *, background: bool = False) -> Permit | None:
        now = monotonic()
        circuit = self._routes.get(route)
        if circuit is None and background:
            return None
        if circuit is None:
            circuit = self._routes[route] = _Circuit()
        probing = circuit.open_until is not None
        if probing:
            remaining = max(0, math.ceil((circuit.open_until - now) * 1000))
            if remaining or circuit.probing or background:
                _record_rejection(route, remaining, circuit.probing)
                detail = f"try again in {math.ceil(remaining / 1000)}s" if remaining else "waiting for a foreground probe"
                raise CircuitOpenError(f"Model route circuit is open: {detail}.")
            circuit.probing = True
            observe("circuit_probe", provider=route[1], model=route[4])
        elif background:
            return None
        circuit.in_flight += 1
        return Permit(route, circuit.epoch, probing)

    def finish(self, permit: Permit, outcome: Literal["success", "transient", "ignored"]) -> int | None:
        """Return the cooldown in seconds if this request opened the circuit."""
        route = permit.route
        circuit = self._routes[route]
        circuit.in_flight -= 1
        if permit.epoch != circuit.epoch:
            self._forget_idle(route, circuit)
            return None
        now = monotonic()
        if permit.probe:
            circuit.probing = False
            observe("circuit_probe_finished", provider=route[1], model=route[4], status=outcome)
        if outcome == "success":
            circuit.failures.clear()
            circuit.open_until = None
        elif outcome == "transient":
            circuit.failures.append(now)
            while circuit.failures[0] <= now - _WINDOW:
                circuit.failures.popleft()
            if permit.probe or len(circuit.failures) >= _LIMIT:
                circuit.open_until = now + _COOLDOWN
                circuit.epoch += 1
                observe("circuit_opened", provider=route[1], model=route[4], delay_ms=_COOLDOWN * 1000,
                        failure_count=len(circuit.failures), reason="transient_failure")
                circuit.failures.clear()
                return _COOLDOWN
        self._forget_idle(route, circuit)
        return None

    def _forget_idle(self, route: Route, circuit: _Circuit) -> None:
        if circuit.in_flight == 0 and not circuit.failures and circuit.open_until is None:
            self._routes.pop(route, None)

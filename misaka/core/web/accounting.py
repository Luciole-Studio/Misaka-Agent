"""Count attempted outbound operations, including failure and cancellation, once."""
from contextlib import asynccontextmanager

from misaka.core.platform import budget
from misaka.core.web.debug import attempt
from misaka.utils.async_lifecycle import run_in_thread


@asynccontextmanager
async def account_call(service: str, backend: str, subject: str, *, unit: str = "http_request", **facts):
    link = None
    try:
        with attempt(service, backend, subject, unit) as link:
            yield facts
    finally:
        # Retain the turn's context and drain the ledger write before the caller exits.
        await run_in_thread(
            budget.record_external_call, service, subject=subject, backend=backend, unit=unit, **facts, **(link or {})
        )

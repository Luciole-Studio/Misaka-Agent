"""DuckDuckGo search, via the optional ``ddgs`` package.

Ported from Hermes' ``plugins/web/ddgs/provider.py``. The ``ddgs`` package is an optional
dependency: :meth:`DDGSWebSearchProvider.is_available` reflects whether it is importable,
and the provider registers either way so a "not installed" answer is possible at all.

**Isolation, and why this is not just a call.** ``ddgs``/``primp`` can block inside native
code while holding the GIL. A thread with a timeout cannot fire in that state -- the
waiter never reacquires the GIL -- so a hung DuckDuckGo response takes the whole process
down with it, through Ctrl+C and SIGTERM alike. Each search therefore runs in a disposable
child process the parent can terminate and kill. Hermes reached this design after two
failed attempts (a bare timeout, then a thread pool); the child process is the third and
the one that works, so it is carried over rather than re-simplified.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import Any

from misaka.core.web.config import without_credentials
from misaka.core.web.provider import WebSearchProvider

logger = logging.getLogger(__name__)

# Overall wall-clock cap for a single ddgs search. The DDGS constructor's ``timeout`` only
# bounds individual HTTP requests; ddgs's multi-engine retry loop has no overall cap, so a
# slow or rate-limited DuckDuckGo response can hang the caller indefinitely. The hard cap
# is enforced here by killing the worker process.
_SEARCH_TIMEOUT_SECS = 30

# After terminate(), wait this long before escalating to kill().
_TERMINATE_GRACE_SECS = 1.0

_WORKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ddgs_worker.py")


def _run_ddgs_search(query: str, safe_limit: int) -> list[dict[str, Any]]:
    """Run the blocking ddgs query and return normalized hits.

    Module-level (not a closure) so the child worker can import it and so tests can patch
    it. ``DDGS(timeout=...)`` bounds each individual HTTP request; the overall wall-clock
    cap is the parent's process timeout.
    """
    from ddgs import DDGS

    results: list[dict[str, Any]] = []
    with DDGS(timeout=10) as client:
        for i, hit in enumerate(client.text(query, max_results=safe_limit)):
            if i >= safe_limit:
                break
            url = str(hit.get("href") or hit.get("url") or "")
            results.append(
                {
                    "title": str(hit.get("title", "")),
                    "url": url,
                    "description": str(hit.get("body", "")),
                    "position": i + 1,
                }
            )
    return results


def _worker_argv() -> list[str]:
    """Command that runs the search worker. The seam tests replace with a stub script."""
    return [sys.executable, _WORKER_PATH]


def _worker_env() -> dict[str, str]:
    """Child environment with MISAKA importable and no credentials in it.

    Running the worker as a script puts its own directory on ``sys.path[0]``, which is not
    enough to ``import misaka``; the repo (or site-packages) root is prepended instead,
    resolved from the live package rather than by counting ``dirname`` calls -- that stays
    correct for both a source checkout and an installed wheel.

    Every credential-shaped variable is stripped first. Hermes runs this same worker under
    ``_sanitize_subprocess_env`` for the same reason: the child exists to hand one query to
    a third-party library, and that library reading ``os.environ`` is the only way any of
    those keys could leave the machine on this path.
    """
    import misaka

    env = without_credentials(dict(os.environ))
    root = os.path.dirname(os.path.dirname(os.path.abspath(misaka.__file__)))
    existing = env.get("PYTHONPATH", "")
    if root and root not in existing.split(os.pathsep):
        env["PYTHONPATH"] = root + os.pathsep + existing if existing else root
    return env


async def _terminate_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Terminate a worker, escalate to kill, and wait so no orphan remains."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECS)
            return
        except TimeoutError:
            pass
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECS)
        except TimeoutError:
            logger.warning("DDGS worker pid=%s did not exit after kill", proc.pid)
    except ProcessLookupError:
        pass  # already gone between the poll and the signal
    except Exception as exc:  # noqa: BLE001 - best-effort cleanup
        logger.debug("DDGS worker reap error: %s", exc)


async def _run_ddgs_search_bounded(query: str, safe_limit: int) -> list[dict[str, Any]]:
    """Run :func:`_run_ddgs_search` in a disposable process with a hard deadline.

    Raises ``TimeoutError`` when the deadline passes and ``RuntimeError`` when the worker
    answers with nothing usable. A cancellation (the session aborting the tool) kills the
    child on the way out rather than leaving it to finish a search nobody wants.
    """
    request = json.dumps({"query": query, "safe_limit": safe_limit}).encode()
    spawn: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        # Own session so a hung native grandchild is reaped with the worker.
        else {"start_new_session": True}
    )
    proc = await asyncio.create_subprocess_exec(
        *_worker_argv(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # DEVNULL avoids the classic deadlock where a chatty child fills the stderr pipe
        # buffer while the parent only drains stdout.
        stderr=subprocess.DEVNULL,
        env=_worker_env(),
        **spawn,
    )
    try:
        raw, _err = await asyncio.wait_for(
            proc.communicate(request), timeout=_SEARCH_TIMEOUT_SECS
        )
    except TimeoutError:
        await _terminate_and_reap(proc)
        raise TimeoutError(
            f"DuckDuckGo search timed out after {_SEARCH_TIMEOUT_SECS}s"
        ) from None
    except asyncio.CancelledError:
        await _terminate_and_reap(proc)
        raise
    finally:
        await _terminate_and_reap(proc)

    text = (raw or b"").decode("utf-8", "replace").strip()
    if not text:
        raise RuntimeError(f"DDGS worker exited without a result (code={proc.returncode})")

    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DDGS worker returned invalid JSON: {text[:200]!r}") from exc

    if not isinstance(envelope, dict):
        # A malformed envelope is the worker breaking its own protocol, which the caller
        # reports as a search failure -- not a caller passing the wrong type, so not a
        # TypeError however much it looks like an isinstance check.
        raise RuntimeError(  # noqa: TRY004 - protocol failure, not a caller type error
            f"DDGS worker returned an invalid envelope: {envelope!r}"
        )
    if envelope.get("ok"):
        results = envelope.get("results") or []
        if not isinstance(results, list):
            raise RuntimeError("DDGS worker returned non-list results")
        return results
    raise RuntimeError(str(envelope.get("error") or "DDGS worker failed"))


class DDGSWebSearchProvider(WebSearchProvider):
    """DuckDuckGo HTML-scrape search provider.

    No API key needed. Rate limits are enforced server-side by DuckDuckGo; the provider
    surfaces ddgs's own exceptions as ``{"success": False, "error": ...}`` rather than
    raising.
    """

    @property
    def name(self) -> str:
        return "ddgs"

    @property
    def display_name(self) -> str:
        return "DuckDuckGo (ddgs)"

    def is_available(self) -> bool:
        """Return True when the ``ddgs`` package is importable.

        Probes the import once; cheap because Python caches it. Must NOT perform network
        I/O -- this runs at tool-registration time.
        """
        try:
            import ddgs  # noqa: F401 - availability probe only
        except ImportError:
            return False
        return True

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a DuckDuckGo search and return normalized results."""
        if not self.is_available():
            return {
                "success": False,
                "error": "ddgs package is not installed - run `pip install ddgs`",
            }

        # DDGS().text yields at most `max_results` items; cap defensively in case the
        # package ignores the hint.
        safe_limit = max(1, int(limit))

        try:
            web_results = await _run_ddgs_search_bounded(query, safe_limit)
        except TimeoutError:
            logger.warning(
                "DDGS search timed out after %ds for query: %r", _SEARCH_TIMEOUT_SECS, query
            )
            return {
                "success": False,
                "error": (
                    f"DuckDuckGo search timed out after {_SEARCH_TIMEOUT_SECS}s - "
                    "DuckDuckGo may be rate-limiting or slow. Try again later "
                    "or switch to a different search provider."
                ),
            }
        except Exception as exc:  # noqa: BLE001 - ddgs raises its own exceptions
            logger.warning("DDGS search error: %s", exc)
            return {"success": False, "error": f"DuckDuckGo search failed: {exc}"}

        logger.info("DDGS search '%s': %d results (limit %d)", query, len(web_results), limit)
        return {"success": True, "data": {"web": web_results}}

    def setup_hint(self) -> dict[str, Any]:
        return {
            "name": "DuckDuckGo (ddgs)",
            "badge": "free - no key - search only",
            "tag": "Search via the ddgs Python package - no API key",
            "env_vars": [],
        }

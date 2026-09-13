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

from misaka.core.web.accounting import account_call
from misaka.core.web.config import without_credentials
from misaka.core.web.provider import WebSearchProvider
from misaka.core.web.timeouts import ddgs_request_timeout, operation_seconds
from misaka.utils.async_lifecycle import settle

logger = logging.getLogger(__name__)

# After terminate(), wait this long before escalating to kill().
_TERMINATE_GRACE_SECS = 1.0


def _run_ddgs_search(query: str, safe_limit: int, request_timeout: float) -> list[dict[str, Any]]:
    """Run the blocking ddgs query and return normalized hits.

    Module-level (not a closure) so the child worker can import it and so tests can patch
    it. ``DDGS(timeout=...)`` bounds each individual HTTP request; the overall wall-clock
    cap is the parent's process timeout.
    """
    from ddgs import DDGS

    results: list[dict[str, Any]] = []
    with DDGS(timeout=request_timeout, verify=os.environ.get("SSL_CERT_FILE") or True) as client:
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
    # A script in backends/ shadows the external ddgs package with our ddgs.py.
    # -P also excludes an unrelated module in the caller's working directory.
    return [sys.executable, "-P", "-m", "misaka.core.web.backends._ddgs_worker"]


def _worker_env() -> dict[str, str]:
    """Child environment with MISAKA importable and no credentials in it.

    The worker uses safe module execution (-P -m). Supply the repo/site-packages root
    explicitly, resolved from the live package rather than the working directory;
    this works for both a source checkout and an installed wheel.

    Every credential-shaped variable is stripped first. Hermes runs this same worker under
    ``_sanitize_subprocess_env`` for the same reason: the child exists to hand one query to
    a third-party library, and that library reading ``os.environ`` is the only way any of
    those keys could leave the machine on this path.
    """
    import misaka
    from misaka.core.web.config import provider_env
    from misaka.core.web.network import TLS_VARIABLES, _proxy_url, proxy_environment
    from misaka.core.web.scope import current_scope

    snapshot = current_scope().environment
    env = without_credentials(dict(os.environ if snapshot is None else snapshot))
    # Native transports read their own proxy environment. Normalize precedence
    # before spawning; never mutate the parent process or inherit application keys.
    for name, value in proxy_environment().items():
        env.pop(name.lower(), None)
        if value and name != "NO_PROXY":
            value = _proxy_url(value, name)
        env[name] = value
    for name in TLS_VARIABLES:
        env[name] = provider_env(name)
    if provider_env("SSL_CERT_DIR") and not provider_env("SSL_CERT_FILE"):
        raise ValueError("DDGS needs SSL_CERT_FILE (a PEM bundle); its native client has no CA-directory option")
    native_proxy = provider_env("DDGS_PROXY")
    env["DDGS_PROXY"] = _proxy_url(native_proxy, "DDGS_PROXY") if native_proxy else ""
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
    except ProcessLookupError:
        pass  # already gone between the poll and the signal
    # The deadline starts cancellation; it is not permission to leave the worker
    # unowned after SIGKILL. Reaping may take longer than the grace interval.
    await proc.wait()


async def _run_ddgs_search_bounded(query: str, safe_limit: int) -> list[dict[str, Any]]:
    """Run :func:`_run_ddgs_search` in a disposable process with a hard deadline.

    Raises ``TimeoutError`` when the deadline passes and ``RuntimeError`` when the worker
    answers with nothing usable. A cancellation (the session aborting the tool) kills the
    child on the way out rather than leaving it to finish a search nobody wants.
    """
    seconds = operation_seconds("ddgs")
    if seconds == 0:
        raise TimeoutError("DuckDuckGo search deadline is 0s; no worker started")
    request = json.dumps({"query": query, "safe_limit": safe_limit,
                          "request_timeout": ddgs_request_timeout()}).encode()
    spawn: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        # Keep terminal signals separate from the parent's awaited cleanup.
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
        # DDGS owns its engine HTTP internally. Record the observable operation,
        # not a fabricated count of the hidden requests it might make.
        async with account_call("web_search", "ddgs", query, unit="provider_operation"):
            raw, _err = await asyncio.wait_for(
                proc.communicate(request), timeout=seconds
            )
    except TimeoutError:
        raise TimeoutError(
            f"DuckDuckGo search timed out after {seconds:g}s"
        ) from None
    finally:
        _, cancelled = await settle(asyncio.create_task(_terminate_and_reap(proc)))
        if cancelled is not None:
            raise cancelled

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
        except TimeoutError as error:
            logger.warning(
                "DDGS search reached its operation timeout for query: %r", query
            )
            return {
                "success": False,
                "error": (
                    f"{error} - "
                    "DuckDuckGo may be rate-limiting or slow. Try again later "
                    "or switch to a different search provider."
                ),
            }
        except Exception as exc:  # noqa: BLE001 - ddgs raises its own exceptions
            logger.warning("DDGS search error: %s", exc)
            return {"success": False, "error": f"DuckDuckGo search failed: {exc}"}

        logger.info("DDGS search '%s': %d results (limit %d)", query, len(web_results), limit)
        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "DuckDuckGo (ddgs)",
            "badge": "free - no key - search only",
            "tag": "Search via the ddgs Python package - no API key",
            "env_vars": [],
            "post_setup": "ddgs",
        }

"""Web search/extract provider ABC.

Ported from Hermes' ``agent/web_search_provider.py``. Defines the pluggable-backend
interface for both web capabilities. Providers register instances with
:func:`misaka.core.web.registry.register_provider`; the active search provider
(selected via ``search_backend`` / ``backend`` in ``~/.misaka/web.json``) services every
``web_search`` call, and the active extract provider (``extract_backend`` / ``backend``)
services every ``web_extract`` call. A provider advertises what it can do with
:meth:`WebSearchProvider.supports_search` and :meth:`WebSearchProvider.supports_extract`,
so one class can serve both -- which Firecrawl, Tavily, Exa, Parallel and Keenable all do.

**Response shape (preserved from the legacy contract).** This is the interface between
the provider layer and the tool that renders results; not one field is renamed.

Search results::

    {
        "success": True,
        "data": {
            "web": [
                {"title": str, "url": str, "description": str, "position": int},
                ...
            ]
        }
    }

On failure::

    {"success": False, "error": str}

``data`` may additionally carry ``served_by`` (a ring vendor other than the one asked
for answered), ``rescued_from`` and ``backend_error`` (the configured backend failed and
the keyless ring served this one call). Those keys are annotations, never a replacement
for ``web``.

Extract results are a *list*, one entry per requested URL and in the same order::

    [
        {
            "url": str,
            "title": str,
            "content": str,
            "raw_content": str,      # the untruncated text, when the vendor gives one
            "metadata": dict,        # optional; {"sourceURL", "title"} by convention
            "error": str,            # optional; present only on a per-URL failure
        },
        ...
    ]

Order parity is a contract, not a courtesy: the tool reconstructs the caller's original
argument list by position, so a provider that drops a failed URL instead of returning an
entry with ``error`` set will hand the model somebody else's page under the wrong address.

MISAKA also reaches pages through ``web_fetch``, which dials them itself. The two are not
redundant: ``web_fetch`` fetches and saves one page as citable evidence, ``web_extract``
asks a vendor that renders JavaScript and reads PDFs for up to five at once.
"""

from __future__ import annotations

import abc
from typing import Any


class WebSearchProvider(abc.ABC):
    """Abstract base class for a web search/extract backend.

    Subclasses must implement :meth:`is_available` and at least one of :meth:`search` /
    :meth:`extract`. The capability flags let the registry route each tool call to a
    provider that can actually serve it, and let a multi-capability vendor advertise both
    from a single class.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable short identifier used in the ``search_backend`` / ``backend`` config keys.

        Lowercase, no spaces; hyphens permitted to preserve existing user-visible names.
        Examples: ``brave-free``, ``ddgs``, ``searxng``, ``firecrawl``.
        """

    @property
    def display_name(self) -> str:
        """Human-readable label. Defaults to ``name``."""
        return self.name

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return True when this provider can service calls.

        Typically a cheap check (env var present, optional Python dep importable,
        instance URL set). Must NOT make network calls -- this runs at tool-registration
        time and on every availability probe.
        """

    def is_keyless_available(self) -> bool:
        """Return True when this provider can serve calls WITHOUT credentials.

        A separate, weaker tier than :meth:`is_available`: providers with a public
        anonymous free tier return True here so the registry can fall back to them when
        NO provider is configured or keyed -- and only then. Keyless availability must
        never make :meth:`is_available` return True, or the legacy preference walk would
        route users with real credentials for a lower-priority backend onto the free
        tier of a higher-priority one.

        Like :meth:`is_available`, this must be cheap and must NOT make network calls.
        Default: False.
        """
        return False

    def supports_search(self) -> bool:
        """Whether this provider implements :meth:`search`. Default: True."""
        return True

    def supports_extract(self) -> bool:
        """Whether this provider implements :meth:`extract`. Default: False.

        Extraction needs a vendor that renders the page; the search-only backends
        (``ddgs``, ``searxng``, ``brave-free``, ``xai``) return an index's answer and have
        nothing to render with, so they leave this alone.
        """
        return False

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a web search, returning the response shape in the module docstring.

        Async where Hermes is sync: MISAKA drives tools from one event loop, and a
        blocking vendor call there stalls every other tool in the session. Providers
        whose upstream is genuinely blocking (``ddgs``) keep that work off the loop
        themselves rather than pushing the problem onto the caller.

        Never raises for a vendor-side failure: an unreachable backend, a rejected key,
        and an exhausted quota all come back as ``{"success": False, "error": ...}`` so
        the dispatcher can decide whether to rescue the call.

        Concrete rather than abstract, as in Hermes: a provider that only extracts says so
        with :meth:`supports_search` and inherits this, instead of writing a stub that
        pretends to search. Callers gate on the flag before calling.
        """
        raise NotImplementedError(
            f"{self.name} does not support search (override supports_search)"
        )

    async def extract(self, urls: list[str], *, format: str | None = None) -> list[dict[str, Any]]:
        """Fetch the clean text of each URL, in the order given.

        Override when :meth:`supports_extract` returns True. Returns one entry per URL,
        in the module docstring's shape -- a vendor-side failure for one page is that
        page's ``error`` field, never an exception and never a missing entry, because the
        caller reassembles its argument list by position.

        Raising IS the signal for a whole-backend failure (a rejected key, an unreachable
        endpoint): the dispatcher catches it and may route the batch through the keyless
        ring once. A provider that swallows such a failure into per-URL errors gets that
        rescue too, via the all-entries-failed check.

        *format* is ``"markdown"``, ``"html"`` or None; Hermes passes it through and only
        Firecrawl acts on it, the rest ignoring it. Kept rather than dropped so the
        parameter is there the day the tool exposes it.
        """
        raise NotImplementedError(
            f"{self.name} does not support extract (override supports_extract)"
        )

    def setup_hint(self) -> dict[str, Any]:
        """Return provider metadata for a future setup UI.

        Hermes' ``get_setup_schema``, feeding the ``hermes tools`` picker. MISAKA has no
        picker yet; the data is carried anyway because it is the only place that records,
        per vendor, which credential to set and where to get it -- and losing that means
        losing the reason each ``is_available`` looks at the name it does. Shape::

            {"name": str, "badge": str, "tag": str,
             "env_vars": [{"key": str, "prompt": str, "url": str}, ...]}
        """
        return {"name": self.display_name, "badge": "", "tag": "", "env_vars": []}

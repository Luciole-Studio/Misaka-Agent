"""Web Search Provider ABC.

Ported from Hermes' ``agent/web_search_provider.py``. Defines the pluggable-backend
interface for web search. Providers register instances with
:func:`misaka.extensions.web.registry.register_provider`; the active one (selected via
``search_backend`` / ``backend`` in ``~/.misaka/web.json``) services every ``web_search``
tool call.

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

Hermes' ABC also carries the ``extract`` capability, whose contract is the second half
of the same docstring. MISAKA reaches pages through ``web_fetch``, so nothing here
implements extraction and the capability is not modelled -- adding it back means adding
``supports_extract`` / ``extract`` and a capability filter in the registry, both of which
Hermes still has.
"""

from __future__ import annotations

import abc
from typing import Any


class WebSearchProvider(abc.ABC):
    """Abstract base class for a web search backend."""

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

    @abc.abstractmethod
    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a web search, returning the response shape in the module docstring.

        Async where Hermes is sync: MISAKA drives tools from one event loop, and a
        blocking vendor call there stalls every other tool in the session. Providers
        whose upstream is genuinely blocking (``ddgs``) keep that work off the loop
        themselves rather than pushing the problem onto the caller.

        Never raises for a vendor-side failure: an unreachable backend, a rejected key,
        and an exhausted quota all come back as ``{"success": False, "error": ...}`` so
        the dispatcher can decide whether to rescue the call.
        """

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

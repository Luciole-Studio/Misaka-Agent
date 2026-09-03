"""The bundled search backends, one module per vendor.

Hermes discovers these as plugins under ``plugins/web/<vendor>/`` with a ``register(ctx)``
entry point. MISAKA's extension loader only discovers session extensions, not backends, so
registration is an explicit list here -- the same nine vendors, registered in one call.

Order in the list is irrelevant: preference lives in
:data:`misaka.core.web.registry._LEGACY_PREFERENCE` and in the keyless ring.
"""

from __future__ import annotations

from misaka.core.web.registry import get_provider, register_provider


def register_builtin_providers() -> None:
    """Instantiate and register every bundled backend that nothing has claimed.

    A name already in the registry is left alone. Hermes reaches the same outcome by
    discovery order -- a user plugin in ``~/.hermes/plugins/web/<name>/`` loads after the
    bundled one and overwrites it -- and the property that matters is the same either way:
    someone who registers their own ``tavily`` keeps it, and still gets the other eight.
    """
    from misaka.core.web.backends.brave_free import BraveFreeWebSearchProvider
    from misaka.core.web.backends.ddgs import DDGSWebSearchProvider
    from misaka.core.web.backends.exa import ExaWebSearchProvider
    from misaka.core.web.backends.firecrawl import FirecrawlWebSearchProvider
    from misaka.core.web.backends.keenable import KeenableWebSearchProvider
    from misaka.core.web.backends.parallel import ParallelWebSearchProvider
    from misaka.core.web.backends.searxng import SearXNGWebSearchProvider
    from misaka.core.web.backends.tavily import TavilyWebSearchProvider
    from misaka.core.web.backends.xai import XAIWebSearchProvider

    for provider in (
        BraveFreeWebSearchProvider(),
        DDGSWebSearchProvider(),
        ExaWebSearchProvider(),
        FirecrawlWebSearchProvider(),
        KeenableWebSearchProvider(),
        ParallelWebSearchProvider(),
        SearXNGWebSearchProvider(),
        TavilyWebSearchProvider(),
        XAIWebSearchProvider(),
    ):
        if get_provider(provider.name) is None:
            register_provider(provider)

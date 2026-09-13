"""Bundled providers form each Web scope's base; session extensions overlay them.

Order here does not select a backend: the registry's preference ladder and keyless
ring do that. Built-ins never overwrite an explicit registration or extension owner.
"""

from __future__ import annotations

from misaka.core.web.scope import current_scope


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
    from misaka.core.web.backends.firecrawl import (
        FirecrawlWebSearchProvider,
        NousWebSearchProvider,
    )
    from misaka.core.web.backends.keenable import KeenableWebSearchProvider
    from misaka.core.web.backends.parallel import ParallelWebSearchProvider
    from misaka.core.web.backends.perplexity import PerplexityWebSearchProvider
    from misaka.core.web.backends.searxng import SearXNGWebSearchProvider
    from misaka.core.web.backends.tavily import TavilyWebSearchProvider
    from misaka.core.web.backends.xai import XAIWebSearchProvider

    for provider in (
        BraveFreeWebSearchProvider(),
        DDGSWebSearchProvider(),
        ExaWebSearchProvider(),
        FirecrawlWebSearchProvider(),
        NousWebSearchProvider(),
        KeenableWebSearchProvider(),
        ParallelWebSearchProvider(),
        PerplexityWebSearchProvider(),
        SearXNGWebSearchProvider(),
        TavilyWebSearchProvider(),
        XAIWebSearchProvider(),
    ):
        scope = current_scope()
        with scope.lock:
            scope.builtins.setdefault(provider.name, provider)
            scope.providers.setdefault(provider.name, scope.builtins[provider.name])

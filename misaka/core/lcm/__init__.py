"""LCM, the lossless-compaction memory context engine.

A port of the hermes-lcm plugin (github.com/stephenschoettler/hermes-lcm). ``vendor/``
carries upstream verbatim -- see ``UPSTREAM_COMMIT`` for the pinned revision and
``PORT_NOTES.md`` for the resync contract -- and ``host/`` adapts it to misaka.

The package is named for the concept, like everything else a user meets
(``MISAKA_LCM_*``, ``~/.misaka/lcm.db``, ``misaka lcm``); the source is credited here and
in ``vendor/``. Upstream's own suite, vendored under ``tests/hermes_lcm_vendor/``, still
imports ``hermes_lcm`` -- its conftest aliases that name to ``vendor/``.
"""

SESSION_KINDS = {"foreground", "dm", "card", "beast"}


def part(spec):
    from .host.extension import LcmPart

    # Which of the fifteen tools the session is offered depends on its kind
    # (``host/tools.py`` ``withheld``); this is the one place that knows it.
    return LcmPart(kind=spec.kind)

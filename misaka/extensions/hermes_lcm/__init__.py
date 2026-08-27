"""hermes-lcm: the LCM (lossless-compaction memory) context engine.

A port of the hermes-lcm plugin (github.com/stephenschoettler/hermes-lcm). ``vendor/``
carries upstream verbatim -- see ``UPSTREAM_COMMIT`` for the pinned revision and
``PORT_NOTES.md`` for the resync contract -- and ``host/`` adapts it to misaka.

The package is named for its source, the way the web tools credit Hermes; the
user-facing surface stays generic ``lcm`` (``MISAKA_LCM_*``, ``~/.misaka/lcm.db``,
``misaka lcm``), because those name the concept.
"""

SESSION_KINDS = {"foreground", "dm", "card", "beast"}


def activate(spec):
    from .host.extension import register

    return register

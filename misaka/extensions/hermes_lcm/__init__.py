"""LCM, the lossless-compaction memory context engine.

A port of the hermes-lcm plugin (github.com/stephenschoettler/hermes-lcm). ``vendor/``
carries upstream with the registered host seams -- see ``UPSTREAM_COMMIT`` for the pinned revision and
``PORT_NOTES.md`` for the resync contract -- and ``host/`` adapts it to misaka.

The package is named for the concept, like everything else a user meets
(``MISAKA_LCM_*``, ``~/.misaka/lcm.db``, ``/lcm``); the source is credited here and
in ``vendor/``. Upstream's own suite, vendored under ``tests/hermes_lcm_vendor/``, still
imports ``hermes_lcm`` -- its conftest aliases that name to ``vendor/``.
"""

SESSION_KINDS = {"foreground", "dm", "card", "beast", "child", "bare"}


def activate(spec):
    from .host.extension import register

    def register_for_session(harn):
        return register(harn, kind=spec.kind)

    return register_for_session

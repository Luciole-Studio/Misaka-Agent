"""hermes-lcm: the LCM (lossless-compaction memory) context engine.

A slim port of the hermes-lcm plugin (github.com/stephenschoettler/hermes-lcm) -- misaka
carries a small subset of it. The package is named for its source, the way the web tools
credit Hermes; the user-facing surface stays generic ``lcm`` (the ``MISAKA_LCM_*`` vars,
``~/.misaka/lcm.db``, ``misaka lcm``, ``context_engine=lcm``), because those name the
concept and rewiring them would orphan an existing install's memory database.
"""

SESSION_KINDS = {"foreground", "dm", "card", "beast"}


def activate(spec):
    from .extension import register
    return register

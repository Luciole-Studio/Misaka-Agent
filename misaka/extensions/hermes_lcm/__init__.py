"""hermes-lcm: the LCM (lossless-compaction memory) context engine.

Two implementations live here. The files beside this one are the pre-port mini version
misaka wrote from scratch; ``vendor/`` is the upstream plugin
(github.com/stephenschoettler/hermes-lcm) carried verbatim, with ``host/`` adapting it
to misaka. ``activate`` below picks between them.

The package is named for its source, the way the web tools credit Hermes; the
user-facing surface stays generic ``lcm`` (the ``MISAKA_LCM_*`` vars, ``~/.misaka/lcm.db``,
``misaka lcm``), because those name the concept and rewiring them would orphan an
existing install's memory database.
"""

from misaka.config import CFG

SESSION_KINDS = {"foreground", "dm", "card", "beast"}


def activate(spec):
    """Pick the implementation this install asked for.

    ``context_engine=hermes-lcm`` runs the ported upstream engine out of ``vendor/``;
    anything else keeps the mini implementation, which carries the ``native`` escape
    hatch inside itself. One value switches, and the same value switches back.

    Backing out has one asymmetry worth the six lines: the mini implementation ingests
    on every session regardless of ``context_engine``, so pointing it at a database the
    migrator has already rebuilt would grow mini tables inside upstream's schema and
    leave a file neither side can read. When the database has been ported and the mini
    implementation is what is selected, this session simply runs without LCM -- a
    reversible configuration mistake stays reversible.
    """
    import logging

    from .host import switch

    if switch.selected():
        from .host.extension import register
        return register
    if switch.schema(switch.database_path()) == "ported":
        logging.getLogger(__name__).warning(
            "LCM database %s has been migrated to the ported engine's schema, but "
            "context_engine=%s selects the pre-port implementation; this session runs "
            "without LCM. Set MISAKA_CONTEXT_ENGINE=%s, or restore the backup that "
            "`misaka lcm migrate` wrote.",
            switch.database_path(), CFG.get("context_engine"), switch.ENGINE_VALUE,
        )
        return None
    from .extension import register
    return register

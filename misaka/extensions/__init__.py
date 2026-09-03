"""Bundled extensions, in the sense Pi's ``src/extensions/`` gives the word: what ships in
the package and reaches a session through the extension API alone.

Two belong here for good -- ``llama``, a local-model provider and the one extension Pi
itself bundles, and ``moa``, the mixture-of-agents provider. The rest of what sits in this
folder today is product wiring on its way into ``misaka.core``: other packages import it
as a library, and no session could do without it. None of it is found by scanning this
folder any more; every entry, these two included, is named in
``misaka.core.wiring.REGISTRY`` and qualifies itself there. The engine-level extension
protocol stays in :mod:`misaka.core.extensions`.
"""

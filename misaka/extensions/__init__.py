"""Bundled extensions, in the sense Pi's ``src/extensions/`` gives the word: what ships in
the package and reaches a session through the extension API alone.

Two live here -- ``llama``, a local-model provider and the one extension Pi itself
bundles, and ``moa``, the mixture-of-agents provider. Nothing is found by scanning this
folder: both are named in ``misaka.core.wiring.REGISTRY`` beside the product's own
session wiring, and qualify themselves there. The engine-level extension protocol stays
in :mod:`misaka.core.extensions`.
"""

"""Mixture-of-Agents provider extension (hermes MoA as a virtual provider).

MoA runs as a persistent mode, hermes style: pick the MoA preset with /model and
every turn fans out to the reference models until you switch away. The one-shot
/moa command was removed on 2026-08-23 (user decision: the mode is the feature).

Registered from ``misaka.extensions.builtInExtensions``; ``extension.register_provider``
is the factory.
"""

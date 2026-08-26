"""herdr's multiplexer in Python (Apache-2.0). The daemon owns every pseudo-terminal and the
layout (spaces, tabs, split trees); singleton by socket -- whoever binds is the daemon, a stale
socket nobody answers on is removed. The snapshot records shape only (panes, argv, cwd, card),
never processes: restore recreates, and card panes are never rerun automatically. Layering:
``daemon.py`` and ``client.py`` know only config and the card data layer, never the chat.
"""

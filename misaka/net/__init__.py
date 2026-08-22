"""Misaka Network daemon: the pane (pseudo-terminal) host, a Python port of herdr's core design.

Three ideas taken directly from herdr (Apache-2.0):
- The daemon owns every PTY: a client disconnect only removes an observer; the
  process in the pane keeps running.
- Singleton by socket: whoever binds is the daemon; a stale socket nobody
  answers on is removed and recreated.
- The snapshot records shape only (panes, argv, cwd), never processes:
  restore = recreate, and card panes are never rerun automatically (they cost
  money; wait for a human -- Constitution 7).

Layering: ``net`` knows only config and the board data layer (the card state
machine), never cli/tui.
"""

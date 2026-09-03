"""Everything drawn on a terminal, under one roof, from two lineages.

``tui/``    pi's terminal engine (MIT) and, inside it, ``tui/interactive/``: pi's chat mode, the
            program running in every pane. Files mirror pi's ``packages/tui`` and
            ``packages/coding-agent/src/modes/interactive`` by name.
``panel/``  herdr's terminal multiplexer (Apache-2.0): ``daemon.py`` hosts the pseudo-terminals
            and owns the layout, ``client.py`` talks to it, ``panel.py`` draws spaces, tabs and
            panes, ``geometry.py`` is the function-by-function port of herdr's layout maths.

They meet in one place only: ``panel/geometry.py`` reads the chat palette from
``tui/interactive/theme`` so the chrome matches the panes. Nothing else crosses: a pane holds a
chat as a process on a pseudo-terminal, and the chat reaches the daemon only through extensions
(``core/panel/agent_state.py``, ``core/panel/fork_split.py``).
"""

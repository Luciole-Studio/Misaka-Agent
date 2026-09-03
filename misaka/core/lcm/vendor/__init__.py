"""Vendored hermes-lcm, copied verbatim from upstream.

Every module in this package is a byte-for-byte copy of the file of the same name in
hermes-lcm at the commit pinned in ``../UPSTREAM_COMMIT``. Upstream's own imports are
package-relative, so they resolve inside this package unchanged, and upstream's test
suite runs against it through the ``hermes_lcm`` alias installed by
``tests/hermes_lcm_vendor/conftest.py``.

Do not edit these files to match repository style: a diff against upstream is how the
next version gets replayed. The only edits allowed are the four kinds listed in
``../PORT_NOTES.md``, each marked ``# misaka:`` at the line and recorded there.

This file is not one of them -- upstream's own ``__init__.py`` is the Hermes plugin
entry point and is not vendored -- so the one seam the port needs lives here instead of
inside a vendored module: importing ``..host.context_engine_abc`` registers the
``agent.context_engine`` module that ``engine.py`` imports at its line 21. It has to
happen before any submodule is imported, which is exactly what a package ``__init__``
guarantees.
"""

from ..host import context_engine_abc as _context_engine_abc  # noqa: F401

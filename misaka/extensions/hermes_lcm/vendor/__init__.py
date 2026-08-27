"""Vendored hermes-lcm, copied verbatim from upstream.

Every module in this package is a byte-for-byte copy of the file of the same name in
hermes-lcm at the commit pinned in ``../UPSTREAM_COMMIT``. Upstream's own imports are
package-relative, so they resolve inside this package unchanged, and upstream's test
suite runs against it through the ``hermes_lcm`` alias installed by
``tests/hermes_lcm_vendor/conftest.py``.

Do not edit these files to match repository style: a diff against upstream is how the
next version gets replayed. The only edits allowed are the four kinds listed in
``../PORT_NOTES.md``, each marked ``# misaka:`` at the line and recorded there.
"""

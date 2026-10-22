"""pi chord's ``test/types.test.ts``.

The upstream file is a TypeScript compile-time check (``expectTypeOf`` and ``@ts-expect-error``, checked by tsc): state
members are ``Readable<T>``, another variant's members need narrowing, and ``get`` is awaited. Python has no static
counterpart for these conditional types (``misaka.chord.contract`` aliases them to ``Any``), so the case is not ported;
the runtime behaviour it describes is covered by ``test_chord_host.py`` (variants, awaited ``get``).
"""

from __future__ import annotations

import pytest


def test_state_members_are_t_other_variants_members_need_narrowing_gets_are_awaited():
    # not ported: "state members are T; other variants' members need narrowing; gets are awaited" — a tsc-only check.
    pytest.skip("a TypeScript compile-time check (expectTypeOf); Python has no static counterpart")

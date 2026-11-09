"""pi chord's ``test/fixtures/echo.ts``: the contract the manager tests' child host serves."""

from __future__ import annotations

from misaka.chord import hostNamed, method, remote
from misaka.chord._typebox import Type

Echo = remote(
    "t.echo",
    {
        "version": 1,
        "input": Type.Object({}),
        "host": hostNamed("w1"),
        "variants": {
            "main": {"methods": {"echo": method(Type.String(), Type.String()), "pid": method(None, Type.Number())}}
        },
    },
)

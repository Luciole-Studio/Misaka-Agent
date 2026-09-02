"""One line-length limit for every asyncio stream this repo frames by line.

``asyncio`` defaults to 64 KiB. ``StreamReader.readline()`` on a longer line raises
``ValueError`` and the stream cannot be resynchronised afterwards -- the tail of the
over-long line is still arriving -- so the only correct response is to treat that stream
as finished and say why. ``misaka/extensions/mcp.py`` is the worked example; the gate
check is ``scripts/streamcheck.py``.
"""

from __future__ import annotations

STREAM_LIMIT = 32 * 1024 * 1024

__all__ = ["STREAM_LIMIT"]

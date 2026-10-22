"""MISAKA fork: a tool call's arguments while they stream in, kept raw and parsed sparingly.

pi re-parses the whole accumulated buffer with ``parseStreamingJson`` on every fragment; in Python that cost 31 s of
loop-thread CPU for one 38 KB argument, so adapters keep the raw text here and parse it on a schedule, and the final
arguments go through upstream's ``parseToolCallArguments`` (``finish_into``).
"""

from __future__ import annotations

from typing import Any

from misaka.ai.utils.json_parse import parseStreamingJson, parseToolCallArguments

STREAMING_PARSE_THRESHOLD_BYTES = 2048


class StreamingArgs:
    """A tool call's arguments while they stream in: kept raw, parsed sparingly.

    Providers deliver tool arguments as JSON fragments, and every adapter used to hand
    ``parseStreamingJson`` the *whole* accumulated buffer again on every fragment.
    That parse is up to four full scans of the buffer (``JSON.parse``, the ``repairJson``
    pass, then ``partial-json`` on the raw and on the repaired text), so a 38KB Write argument
    arriving in 20-character fragments was parsed ~1950 times over growing prefixes --
    measured at 31s of loop-thread CPU for one tool call.

    The raw string is the durable half and is always exact: the fragments are appended
    verbatim, and ``finish()`` parses the complete buffer at the end of the block, which
    is the value the tool actually runs with. ``arguments`` is the *live view* in
    between, refreshed often enough for the UI and rarely enough to stay cheap.
    """

    __slots__ = ("_parsed_length", "_raw", "_value")

    def __init__(self, initial: str = "") -> None:
        self._raw = initial
        self._parsed_length = -1  # nothing parsed yet: -1 so an initial buffer still earns one
        self._value: Any = {}

    @property
    def raw(self) -> str:
        """Every fragment seen so far, concatenated and unaltered."""
        return self._raw

    def append(self, delta: str) -> None:
        if delta:
            self._raw += delta

    @property
    def arguments(self) -> Any:
        """The last parsed value, re-parsed first if enough new bytes have arrived."""
        if self._is_stale():
            self._parse()
        return self._value

    def finish(self) -> Any:
        """The final arguments, parsed by ``parseToolCallArguments``: a complete object, never prefix recovery.

        Raises ``ValueError`` carrying the ``argumentsError`` text when the buffer is not one. Incremental UI
        previews still use the permissive parser in ``arguments``.
        """
        parsed = parseToolCallArguments(self._raw)
        if "argumentsError" in parsed:
            raise ValueError(parsed["argumentsError"])
        self._value = parsed["arguments"]
        self._parsed_length = len(self._raw)
        return self._value

    def finish_into(self, tool_call) -> None:
        """``Object.assign(block, parseToolCallArguments(partialJson))``: a malformed call stays in the batch with
        ``argumentsError`` set, and tool argument validation reports it back to the model instead of executing."""
        parsed = parseToolCallArguments(self._raw)
        tool_call.arguments = parsed["arguments"]
        tool_call.argumentsError = parsed.get("argumentsError")
        if "argumentsError" not in parsed:
            self._value = parsed["arguments"]
            self._parsed_length = len(self._raw)

    def _is_stale(self) -> bool:
        total = len(self._raw)
        pending = total - self._parsed_length
        if pending <= 0:
            return False
        if total <= STREAMING_PARSE_THRESHOLD_BYTES:
            # Parsing a buffer this small costs microseconds, and the TUI renders tool
            # arguments while they stream -- a tool call under the threshold (nearly all
            # of them) must keep showing its path the moment it is spelled out.
            return True
        # Above it, wait for a fixed slice *and* for a fraction of what is already there.
        # The fixed slice alone would still parse len/slice prefixes of average len/2,
        # which is the same quadratic with a smaller constant; the fraction is what keeps
        # the total work proportional to the argument's length.
        return pending >= max(STREAMING_PARSE_THRESHOLD_BYTES, total // 8)

    def _parse(self) -> None:
        self._value = parseStreamingJson(self._raw)
        self._parsed_length = len(self._raw)


__all__ = ["STREAMING_PARSE_THRESHOLD_BYTES", "StreamingArgs"]

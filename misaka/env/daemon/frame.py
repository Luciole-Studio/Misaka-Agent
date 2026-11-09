"""Length-prefixed frames: ``u32 length | u8 type | u32 id | u32 jsonLength | json | payload`` (docs/protocol.md)."""

from __future__ import annotations

import codecs
import json
import re
import struct
from typing import Any, BinaryIO

REQUEST = 1
RESULT = 2
ERROR = 3
EVENT = 4
CANCEL = 5
PING = 6

# Largest frame either side accepts.
MAX_FRAME = 16 * 1024 * 1024
# Largest payload a response carries, leaving room for its JSON.
MAX_PAYLOAD = MAX_FRAME - 64 * 1024

_HEADER = struct.Struct(">IBII")
_SURROGATE = re.compile("[\ud800-\udfff]")


def _replace_surrogates(error: UnicodeError) -> tuple[str, int]:
    # A lone surrogate becomes U+FFFD, as Rust's lossy conversion of an OS string makes it.
    if not isinstance(error, UnicodeEncodeError):
        raise error
    return "�" * (error.end - error.start), error.end


codecs.register_error("pi-env-replace", _replace_surrogates)


class InvalidData(OSError):
    """``io::ErrorKind::InvalidData``: the framing is broken, which ends the connection."""


class Frame:
    __slots__ = ("id", "json", "kind", "payload")

    def __init__(self, kind: int, id: int, json: Any, payload: bytes = b"") -> None:
        self.kind = kind
        self.id = id
        self.json = json
        self.payload = payload


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _well_formed(value: Any) -> bool:
    if isinstance(value, str):
        return _SURROGATE.search(value) is None
    if isinstance(value, list):
        return all(_well_formed(item) for item in value)
    if isinstance(value, dict):
        return all(_well_formed(key) and _well_formed(item) for key, item in value.items())
    return True


def parse_json(data: bytes) -> Any:
    """``serde_json::from_slice``: UTF-8 JSON without ``NaN``, ``Infinity`` or lone surrogate escapes; ``None``
    (``Value::Null``) when it does not parse."""
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except ValueError:
        return None
    return value if _well_formed(value) else None


# serde_json's `Value` accessors, for the parsed JSON: `value[key]` is `None` (`Value::Null`) when missing or when the
# value is not an object, and each `as_*` is `None` for a value of another type.


def get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def as_u64(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**64:
        return value
    return None


def as_f64(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value)
    except OverflowError:
        return None


def as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _read_exact(input: BinaryIO, length: int) -> bytes | None:
    """``read_exact``: ``length`` bytes, or ``None`` when the input ends first."""
    chunks = []
    remaining = length
    while remaining > 0:
        chunk = input.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(input: BinaryIO) -> Frame | None:
    """Read one frame; ``None`` at a clean end of input."""
    prefix = _read_exact(input, 4)
    if prefix is None:
        return None
    length = int.from_bytes(prefix, "big")
    if not 9 <= length <= MAX_FRAME:
        raise InvalidData("frame length out of range")
    body = _read_exact(input, length)
    if body is None:
        raise EOFError("failed to fill whole buffer")
    kind = body[0]
    id = int.from_bytes(body[1:5], "big")
    json_length = int.from_bytes(body[5:9], "big")
    if 9 + json_length > length:
        raise InvalidData("JSON length out of range")
    # Unparseable JSON leaves the framing intact: the frame arrives with `Value::Null` and gets an error reply.
    value = {} if json_length == 0 else parse_json(body[9 : 9 + json_length])
    payload = body[9 + json_length :]
    return Frame(kind, id, value, payload)


def write_frame(output: BinaryIO, frame: Frame) -> None:
    try:
        text = json.dumps(frame.json, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise InvalidData("unserializable JSON") from None
    data = text.encode("utf-8", "pi-env-replace")
    length = 9 + len(data) + len(frame.payload)
    if length > MAX_FRAME:
        raise InvalidData("frame too large")
    output.write(_HEADER.pack(length, frame.kind, frame.id, len(data)))
    output.write(data)
    output.write(frame.payload)
    output.flush()

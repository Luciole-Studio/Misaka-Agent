"""MISAKA fork: the WHATWG ``URL`` and ``URLSearchParams`` pi's MCP code and pi-ai's OAuth flows use.

Node parses URLs by the WHATWG URL Standard: ``new URL("https://h:99999")`` throws ``TypeError``,
``HTTP://Example.COM:80`` becomes ``http://example.com/``, a relative reference resolves against a base,
and ``URLSearchParams`` serialises ``application/x-www-form-urlencoded`` (``*`` raw, ``~`` encoded), none
of which ``urllib.parse`` or ``httpx.URL`` do alike. This covers the parts the MCP client, its OAuth
flow and the sign-in flows rely on: parsing, resolution, normalisation (hosts too: IPv4 shorthand, IPv6
compression), ``origin`` and the query and fragment setters.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Iterator, Mapping
from urllib.parse import quote, unquote_to_bytes, urljoin, urlsplit

_SPECIAL_PORTS: dict[str, int | None] = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21, "file": None}
# Host code points the URL Standard forbids (besides `%`, checked after decoding).
_FORBIDDEN_HOST = re.compile(r"[\x00\t\n\r #/:<>?@\[\\\]^|]")
_LEADING_TRAILING = re.compile(r"^[\x00-\x20]+|[\x00-\x20]+$")
_TAB_NEWLINE = re.compile(r"[\t\n\r]")
_SURROGATE = re.compile("[\ud800-\udfff]")
# Percent-encode sets (the URL Standard's path, query and fragment sets, leaving `%` itself alone).
_PATH_SAFE = "!$&'()*+,-./:;=@_~%[]|^"
_QUERY_SAFE = "!$&'()*+,-./:;=?@_~%[]{}|^`\\"
_SPECIAL_QUERY_SAFE = "!$&()*+,-./:;=?@_~%[]{}|^`\\"
_FRAGMENT_SAFE = "!#$&'()*+,-./:;=?@_~%[]{}|^\\"
_FORM_RAW = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789*-._")


def _invalid() -> TypeError:
    return TypeError("Invalid URL")


def _removeDotSegments(path: str) -> str:
    output: list[str] = []
    segments = path.split("/")
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment in (".", "%2e", "%2E"):
            if last:
                output.append("")
            continue
        if segment in ("..", ".%2e", ".%2E", "%2e.", "%2E.", "%2e%2e", "%2E%2E", "%2e%2E", "%2E%2e"):
            if len(output) > 1:
                output.pop()
            if last:
                output.append("")
            continue
        output.append(segment)
    return "/".join(output)


def _parseIPv4Number(text: str) -> int | None:
    """The URL Standard's IPv4 number parser: decimal, octal after a leading ``0``, hex after ``0x``."""
    if not text:
        return None
    radix = 10
    if len(text) >= 2 and text[:2] in ("0x", "0X"):
        text, radix = text[2:], 16
    elif len(text) >= 2 and text[0] == "0":
        text, radix = text[1:], 8
    if not text:
        return 0
    digits = {10: "0123456789", 8: "01234567", 16: "0123456789abcdefABCDEF"}[radix]
    if any(char not in digits for char in text):
        return None
    return int(text, radix)


def _endsInANumber(host: str) -> bool:
    parts = host.split(".")
    if parts[-1] == "":
        if len(parts) == 1:
            return False
        parts.pop()
    last = parts[-1]
    return (last != "" and all(char in "0123456789" for char in last)) or _parseIPv4Number(last) is not None


def _parseIPv4(host: str) -> str:
    """The URL Standard's IPv4 parser (``127.1``, ``0x7f.1``, ``2130706433``), serialised dotted-decimal."""
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    if len(parts) > 4:
        raise _invalid()
    numbers = [_parseIPv4Number(part) for part in parts]
    if any(number is None for number in numbers):
        raise _invalid()
    values = [number for number in numbers if number is not None]
    if any(value > 255 for value in values[:-1]) or values[-1] >= 256 ** (5 - len(values)):
        raise _invalid()
    address = values[-1] + sum(value * 256 ** (3 - index) for index, value in enumerate(values[:-1]))
    return ".".join(str((address >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _serializeIPv6(pieces: list[int]) -> str:
    """The URL Standard's IPv6 serialiser: hex pieces, the first longest run of two or more zeros as ``::``."""
    start, length, index = -1, 1, 0
    while index < 8:
        end = index
        while end < 8 and pieces[end] == 0:
            end += 1
        if end - index > length:
            start, length = index, end - index
        index = end + 1 if end == index else end
    if start < 0:
        return ":".join(f"{piece:x}" for piece in pieces)
    head = ":".join(f"{piece:x}" for piece in pieces[:start])
    tail = ":".join(f"{piece:x}" for piece in pieces[start + length :])
    return f"{head}::{tail}"


def _encodeHost(host: str) -> str:
    if host.startswith("["):
        # The URL Standard's IPv6 parser and serialiser: the longest run of two or more zero pieces
        # becomes `::`, pieces are lower-case hex, and an embedded IPv4 address is written as two pieces.
        inner = host[1:-1] if host.endswith("]") else None
        if inner is None or "%" in inner:
            raise _invalid()
        try:
            packed = ipaddress.IPv6Address(inner).packed
        except ValueError as error:
            raise _invalid() from error
        return f"[{_serializeIPv6([int.from_bytes(packed[index : index + 2], 'big') for index in range(0, 16, 2)])}]"
    decoded = unquote_to_bytes(host).decode("utf-8", "replace")
    if _FORBIDDEN_HOST.search(decoded) or "%" in decoded:
        raise _invalid()
    try:
        ascii = decoded.encode("idna").decode("ascii") if not decoded.isascii() else decoded
    except UnicodeError as error:
        raise _invalid() from error
    ascii = ascii.lower()
    return _parseIPv4(ascii) if _endsInANumber(ascii) else ascii


class URLSearchParams:
    """``URLSearchParams``: an ordered list of name/value pairs, serialised as form data."""

    def __init__(self, init: str | Mapping[str, str] | Iterable[tuple[str, str]] | None = None) -> None:
        self._pairs: list[tuple[str, str]] = []
        self._url: URL | None = None
        if init is None:
            return
        if isinstance(init, str):
            self._pairs = _parseForm(init.removeprefix("?"))
        elif isinstance(init, Mapping):
            self._pairs = [(str(name), str(value)) for name, value in init.items()]
        else:
            self._pairs = [(str(name), str(value)) for name, value in init]

    def get(self, name: str) -> str | None:
        return next((value for key, value in self._pairs if key == name), None)

    def getAll(self, name: str) -> list[str]:
        return [value for key, value in self._pairs if key == name]

    def has(self, name: str) -> bool:
        return any(key == name for key, _value in self._pairs)

    def set(self, name: str, value: str) -> None:
        for index, (key, _existing) in enumerate(self._pairs):
            if key == name:
                self._pairs[index] = (name, str(value))
                self._pairs = [
                    pair for later, pair in enumerate(self._pairs) if later <= index or pair[0] != name
                ]
                break
        else:
            self._pairs.append((name, str(value)))
        self._update()

    def append(self, name: str, value: str) -> None:
        self._pairs.append((name, str(value)))
        self._update()

    def delete(self, name: str) -> None:
        self._pairs = [pair for pair in self._pairs if pair[0] != name]
        self._update()

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(list(self._pairs))

    def entries(self) -> Iterator[tuple[str, str]]:
        return iter(self)

    def _update(self) -> None:
        if self._url is not None:
            serialized = str(self)
            self._url._query = serialized if serialized else None

    def __str__(self) -> str:
        return "&".join(f"{_formEncode(name)}={_formEncode(value)}" for name, value in self._pairs)

    def toString(self) -> str:
        return str(self)


def _formEncode(text: str) -> str:
    """``application/x-www-form-urlencoded``: alphanumerics and ``*-._`` raw, space as ``+``, every other
    byte percent-encoded (``quote`` would keep ``~``). A USVString: a lone surrogate becomes U+FFFD."""
    return "".join(
        chr(byte) if byte in _FORM_RAW else "+" if byte == 0x20 else f"%{byte:02X}"
        for byte in _SURROGATE.sub("�", text).encode("utf-8")
    )


def _parseForm(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in text.split("&"):
        if not part:
            continue
        name, _equals, value = part.partition("=")
        pairs.append((_formDecode(name), _formDecode(value)))
    return pairs


def _formDecode(text: str) -> str:
    return unquote_to_bytes(text.replace("+", " ")).decode("utf-8", "replace")


class URL:
    """``new URL(input, base?)``; raises ``TypeError("Invalid URL")`` where the constructor throws."""

    def __init__(self, input: str | URL, base: str | URL | None = None) -> None:
        text = input.href if isinstance(input, URL) else str(input)
        text = _TAB_NEWLINE.sub("", _LEADING_TRAILING.sub("", text))
        baseUrl = None
        if base is not None:
            baseUrl = base if isinstance(base, URL) else URL(base)
            if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", text):
                if re.match(r"^[/\\]{2}" if baseUrl._special else r"^//", text):
                    # A scheme-relative reference keeps only the base's scheme (the relative slash state): for a
                    # special scheme every leading slash or backslash is skipped and the authority follows, so
                    # `//?q` against `http://h/` has an empty host and fails.
                    rest = text.lstrip("/\\") if baseUrl._special else text[2:]
                    text = f"{baseUrl.protocol}//{rest}"
                else:
                    text = urljoin(baseUrl.href, text)
        # A special scheme other than `file` takes its authority after any number of slashes or
        # backslashes, none included (`https:example.com`), unless the base has the same scheme.
        special = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):(?![/\\]{2}(?![/\\]))", text)
        if special and special.group(1).lower() in _SPECIAL_PORTS and special.group(1).lower() != "file" and (
            baseUrl is None or baseUrl.protocol != f"{special.group(1).lower()}:"
        ):
            text = f"{special.group(1)}://{text[special.end():].lstrip(chr(47) + chr(92))}"
        try:
            parts = urlsplit(text)
        except ValueError as error:  # an unbalanced `[` in the host, which `urlsplit` rejects on its own
            raise _invalid() from error
        scheme = parts.scheme.lower()
        if not scheme or not re.fullmatch(r"[a-z][a-z0-9+.-]*", scheme):
            raise _invalid()
        self._scheme = scheme
        self._special = scheme in _SPECIAL_PORTS
        self._username = ""
        self._password = ""
        self._hostname = ""
        self._port = ""
        netloc = parts.netloc
        if self._special and scheme != "file" and not netloc:
            raise _invalid()
        if netloc:
            userinfo, at, hostport = netloc.rpartition("@")
            if at:
                username, _colon, password = userinfo.partition(":")
                self._username, self._password = username, password
            if hostport.startswith("["):
                close = hostport.find("]")
                if close < 0:
                    raise _invalid()
                host, rest = hostport[: close + 1], hostport[close + 1 :]
                port = rest[1:] if rest.startswith(":") else ""
                if rest and not rest.startswith(":"):
                    raise _invalid()
            else:
                host, _colon, port = hostport.partition(":")
            if port:
                # ASCII digits only: `str.isdigit` also accepts other scripts' digits and superscripts.
                if not re.fullmatch(r"[0-9]+", port) or int(port) > 65535:
                    raise _invalid()
                port = str(int(port))
            if self._special and scheme != "file" and not host:
                raise _invalid()
            self._hostname = _encodeHost(host) if self._special else host
            self._port = "" if port and _SPECIAL_PORTS.get(scheme) == int(port) else port
        path = parts.path
        if self._special:
            path = path.replace("\\", "/")
            path = _removeDotSegments(path or "/")
            if not path.startswith("/"):
                path = f"/{path}"
            path = quote(path, safe=_PATH_SAFE)
        else:
            path = quote(path, safe=_PATH_SAFE)
        self._path = path
        beforeFragment = text.split("#", 1)[0]
        query = parts.query if "?" in beforeFragment else None
        self._query: str | None = (
            quote(query, safe=_SPECIAL_QUERY_SAFE if self._special else _QUERY_SAFE) if query is not None else None
        )
        fragment = parts.fragment if "#" in text else None
        self._fragment: str | None = quote(fragment, safe=_FRAGMENT_SAFE) if fragment is not None else None
        self._searchParams: URLSearchParams | None = None

    @property
    def protocol(self) -> str:
        return f"{self._scheme}:"

    @property
    def username(self) -> str:
        return self._username

    @property
    def password(self) -> str:
        return self._password

    @property
    def hostname(self) -> str:
        return self._hostname

    @property
    def port(self) -> str:
        return self._port

    @port.setter
    def port(self, value: str) -> None:
        """``url.port = "8080"``: the leading digits are taken, a default port reads back as ``""``, and
        an invalid value leaves the port alone, as the URL Standard's setter does."""
        text = str(value)
        if text == "":
            self._port = ""
            return
        digits = re.match(r"[0-9]+", text)
        if digits is None:
            return
        port = int(digits.group(0))
        if port > 65535:
            return
        self._port = "" if _SPECIAL_PORTS.get(self._scheme) == port else str(port)

    @property
    def host(self) -> str:
        return f"{self._hostname}:{self._port}" if self._port else self._hostname

    @property
    def origin(self) -> str:
        if self._scheme not in ("http", "https", "ws", "wss", "ftp"):
            return "null"
        return f"{self._scheme}://{self.host}"

    @property
    def pathname(self) -> str:
        return self._path

    @pathname.setter
    def pathname(self, value: str) -> None:
        path = value if value.startswith("/") or not self._special else f"/{value}"
        self._path = quote(_removeDotSegments(path) if self._special else path, safe=_PATH_SAFE)

    @property
    def search(self) -> str:
        return f"?{self._query}" if self._query else ""

    @search.setter
    def search(self, value: str) -> None:
        query = value.removeprefix("?")
        self._query = quote(query, safe=_SPECIAL_QUERY_SAFE if self._special else _QUERY_SAFE) if query else None
        if self._searchParams is not None:
            self._searchParams._pairs = _parseForm(self._query or "")

    @property
    def searchParams(self) -> URLSearchParams:
        if self._searchParams is None:
            params = URLSearchParams(self._query or "")
            params._url = self
            self._searchParams = params
        return self._searchParams

    @property
    def hash(self) -> str:
        return f"#{self._fragment}" if self._fragment else ""

    @hash.setter
    def hash(self, value: str) -> None:
        fragment = value.removeprefix("#")
        self._fragment = quote(fragment, safe=_FRAGMENT_SAFE) if fragment else None

    @property
    def href(self) -> str:
        authority = ""
        if self._hostname or self._special:
            userinfo = ""
            if self._username or self._password:
                userinfo = self._username + (f":{self._password}" if self._password else "") + "@"
            authority = f"//{userinfo}{self.host}"
        query = f"?{self._query}" if self._query is not None else ""
        fragment = f"#{self._fragment}" if self._fragment is not None else ""
        return f"{self._scheme}:{authority}{self._path}{query}{fragment}"

    def toString(self) -> str:
        return self.href

    def toJSON(self) -> str:
        return self.href

    def __str__(self) -> str:
        return self.href

    def __repr__(self) -> str:
        return f"URL({self.href!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, URL) and other.href == self.href

    def __hash__(self) -> int:
        return hash(self.href)

    @staticmethod
    def canParse(input: str | URL, base: str | URL | None = None) -> bool:
        try:
            URL(input, base)
        except TypeError:
            return False
        return True


__all__ = ["URL", "URLSearchParams"]

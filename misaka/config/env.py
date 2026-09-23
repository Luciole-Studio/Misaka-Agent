"""The home's ``.env``: environment for code that is not MISAKA.

Hermes keeps vendor keys and plugin variables in ``~/.hermes/.env`` and loads it into the
process at start; MISAKA does the same with ``<home>/.env`` (``home.LAYOUT["env"]``), and a
role may overlay it with ``profiles/<role>/.env``. What belongs here is what other code reads
from the process environment -- a plugin's ``LCM_*`` knobs, an SDK's ``AZURE_OPENAI_*``, the
API key a skill's script expects, a proxy an external tool honours. What never belongs here
is a ``MISAKA_*`` variable: MISAKA's own switches are settings in ``settings.json``, and the
``MISAKA_*`` names a parent hands a child (its role, its card, its leases) would turn every
process into that child. The loader skips them and says so once.

Precedence: the shell wins over the home's file; a role's file wins over the home's file but
not over the shell. Syntax is dotenv's: ``KEY=value``, an optional ``export`` prefix, single
or double quotes (double quotes take ``\\n``, ``\\t``, ``\\"`` and ``\\\\``), ``#`` comments on
their own line or after an unquoted value. A line that does not parse refuses the whole file:
half a credential set is worse than none.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from misaka.config import home

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RESERVED_PREFIX = "MISAKA_"
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "$": "$"}

# Names set from the home's file, so a role's file may override them (the shell's own are
# never overridden). Per process; children inherit the values, not this bookkeeping.
_loaded_from_home: set[str] = set()


class EnvFileError(ValueError):
    """The file cannot be used: a line that does not parse, or a path that is not a plain file."""


def path(role_dir: str | os.PathLike[str] | None = None) -> Path:
    return home.path("env", role_dir)


def parse(text: str, *, source: str = ".env") -> dict[str, str]:
    """The assignments in ``text``, in order; later lines win over earlier ones."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("export ", "export\t")):
            line = line[len("export"):].lstrip()
        name, sep, rest = line.partition("=")
        name = name.strip()
        if not sep or not NAME_RE.fullmatch(name):
            raise EnvFileError(f"{source}:{number}: expected NAME=value")
        values[name] = _value(rest.strip(), source, number)
    return values


def _value(rest: str, source: str, number: int) -> str:
    if rest.startswith('"'):
        out, i = [], 1
        while i < len(rest):
            char = rest[i]
            if char == "\\" and i + 1 < len(rest) and rest[i + 1] in _ESCAPES:
                out.append(_ESCAPES[rest[i + 1]])
                i += 2
                continue
            if char == '"':
                trailing = rest[i + 1:].strip()
                if trailing and not trailing.startswith("#"):
                    raise EnvFileError(f"{source}:{number}: text after the closing quote")
                return "".join(out)
            out.append(char)
            i += 1
        raise EnvFileError(f"{source}:{number}: unterminated double quote")
    if rest.startswith("'"):
        end = rest.find("'", 1)
        if end < 0:
            raise EnvFileError(f"{source}:{number}: unterminated single quote")
        trailing = rest[end + 1:].strip()
        if trailing and not trailing.startswith("#"):
            raise EnvFileError(f"{source}:{number}: text after the closing quote")
        return rest[1:end]
    # Unquoted: a comment starts at the first `#` that follows whitespace.
    comment = re.search(r"\s#", rest)
    return (rest[:comment.start()] if comment else rest).strip()


def read(role_dir: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """One layer's file as a mapping; a missing file is empty. Raises ``EnvFileError`` on a
    file that cannot be trusted (unparseable, or a symlink)."""
    target = path(role_dir)
    if target.is_symlink():
        raise EnvFileError(f"{home.display(target)} must be a plain file, not a symlink")
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return parse(text, source=home.display(target))


def _reserved(names, source: str) -> list[str]:
    skipped = sorted(name for name in names if name.startswith(RESERVED_PREFIX))
    if skipped:
        logger.warning("%s: ignoring %s -- MISAKA_* variables are settings or a parent's hand-off, never .env",
                       source, ", ".join(skipped))
    return skipped


def load() -> list[str]:
    """Put the home's file into this process's environment, for names the shell did not set.
    Returns the names set. A file that does not parse sets nothing and is reported."""
    try:
        values = read()
    except EnvFileError as error:
        logger.warning("%s", error)
        return []
    skipped = set(_reserved(values, home.display(path())))
    applied = []
    for name, value in values.items():
        if name in skipped or (name in os.environ and name not in _loaded_from_home):
            continue
        os.environ[name] = value
        _loaded_from_home.add(name)
        applied.append(name)
    return applied


def role_overlay(role_dir: str | os.PathLike[str] | None) -> dict[str, str]:
    """What a role's file adds to this process's environment for that role's session: its
    names the shell did not set (over the home's file, under the shell). A file that cannot be
    read adds nothing and is reported."""
    if not role_dir:
        return {}
    try:
        stored = read(role_dir)
    except EnvFileError as error:
        logger.warning("%s", error)
        return {}
    skipped = set(_reserved(stored, home.display(path(role_dir))))
    return {name: value for name, value in stored.items()
            if name not in skipped and (name not in os.environ or name in _loaded_from_home)}


def values(role_dir: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """The environment a role's code sees: the process environment with the role's overlay.
    Without a role, the process environment."""
    return {**os.environ, **role_overlay(role_dir)}


def _quote(value: str) -> str:
    if value and not re.search(r"[\s#'\"\\$]", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def write(changes: dict[str, str], role_dir: str | os.PathLike[str] | None = None, *,
          remove: tuple[str, ...] = ()) -> Path:
    """Set and remove names in one layer's file, keeping its other lines (comments included).
    Created owner-only; locked for the read-modify-write. ``MISAKA_*`` names are refused."""
    from filelock import FileLock

    from misaka.utils import atomic

    for name in (*changes, *remove):
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"not an environment variable name: {name!r}")
        if name.startswith(RESERVED_PREFIX):
            raise ValueError(f"{name} is a MISAKA setting or hand-off, not an .env entry")
    for name, value in changes.items():
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"{name}: a value is text without NUL")
    target = path(role_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target) + ".lock", timeout=10):
        if target.is_symlink():
            raise EnvFileError(f"{home.display(target)} must be a plain file, not a symlink")
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        parse("\n".join(lines), source=home.display(target))      # never edit a file we cannot read back
        pending = dict(changes)
        kept: list[str] = []
        for raw in lines:
            name = _assigned_name(raw)
            if name in remove:
                continue
            if name in pending:
                kept.append(f"{name}={_quote(pending.pop(name))}")
                continue
            kept.append(raw)
        kept.extend(f"{name}={_quote(value)}" for name, value in pending.items())
        atomic.write_text(target, "\n".join(kept) + ("\n" if kept else ""), mode=0o600)
        for name in remove:
            if name in _loaded_from_home and role_dir is None:
                _loaded_from_home.discard(name)
    return target


def _assigned_name(raw: str) -> str | None:
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith(("export ", "export\t")):
        line = line[len("export"):].lstrip()
    name, sep, _ = line.partition("=")
    return name.strip() if sep and NAME_RE.fullmatch(name.strip()) else None


__all__ = ["NAME_RE", "RESERVED_PREFIX", "EnvFileError", "load", "parse", "path", "read", "role_overlay",
           "values", "write"]

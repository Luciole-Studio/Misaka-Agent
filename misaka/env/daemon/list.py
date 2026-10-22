"""``listFiles``: the files and directories below a directory, as ``fd --hidden --exclude .git`` lists them (without
following symbolic links), skipping what git ignores.

Ported from pi's ``packages/env/daemon/src/list.rs`` (v1.3.0). MISAKA fork: upstream walks with the ``ignore`` crate
(0.4.33, with ``globset`` 0.4.20), which fd and ripgrep use; the daemon runs on the remote machine's own Python with the
standard library only, so this module walks with ``os.scandir`` and ports what the crate does with upstream's
``WalkBuilder`` settings (``ignore(false)``, ``parents``/``git_ignore``/``git_exclude``/``git_global`` on, ``require_git``,
``current_dir(root)``). The crate, not git, is the reference wherever the two disagree:

- Rules (``gitignore.rs`` ``GitignoreBuilder::add_line`` and ``globset``'s parser): a ``#`` line is a comment; trailing
  Unicode white space (tabs too) is trimmed unless the line ends with ``\\ ``; ``\\!``/``\\#`` escape the first character;
  ``!`` whitelists; a leading ``/`` anchors; a trailing ``/`` (and a ``\\`` before it) is dropped and matches directories
  only; a pattern without ``/`` gets ``**/``; a trailing ``/**`` gets ``/*``. Glob syntax is globset's: ``{a,b}``
  alternation, ``[...]`` classes without POSIX ``[:name:]`` classes and without escapes inside, a negated class that
  matches ``/``, ``\\`` escaping any character elsewhere (a dangling one, an unopened ``}``, an unclosed ``{`` or a
  reversed range makes the line no rule), ``*``/``?`` within a component, ``**`` only as a whole component. Matching is
  case sensitive, over the path's bytes.
- Which files: each directory's ``.gitignore``, matched relative to it; the deepest one with a matching rule decides,
  then ``info/exclude``, then the global excludes file. A directory holding ``.git`` (a directory or a file) or ``.jj``
  is a repository: rules stop there, above it nothing applies, its ``info/exclude`` joins (a ``.git`` file is followed
  to its ``gitdir:``, whose ``commondir`` holds ``info/exclude``; without ``commondir`` there is none). This holds below
  the listed directory too: a nested repository starts afresh, and a directory that is not in one but holds some
  applies each one's rules inside it. Outside any repository nothing is ignored. The listed directory's parents count
  as well (through its canonical path).
- The global excludes file is ``core.excludesFile`` of ``$GIT_CONFIG_GLOBAL``, else ``~/.gitconfig``, else
  ``$XDG_CONFIG_HOME/git/config``, else the system configuration, else ``$XDG_CONFIG_HOME/git/ignore``, found as the
  crate finds it (the first ``excludesfile = …`` line of a file, in any section; ``~`` expanded); its rules are matched
  relative to the listed directory.
- A rule that whitelists a hidden name lists it even when ``hidden`` is false. ``.ignore`` files are not read.

What is not ported: the crate's regular expressions run in linear time, Python's ``re`` backtracks (same matches; the
listing's timeout bounds a pathological pattern), and the crate's regex size limits (a file whose rules compile past
them is no rules there) are not applied.
"""

from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Callable
from typing import Any

from . import system
from .errors import Failure
from .frame import MAX_PAYLOAD, as_bool, as_u64, get

DEFAULT_MAX_ENTRIES = 50_000
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_MS = 10_000


class Options:
    """``hidden``: include names starting with ``.`` (and, on Windows, hidden files). ``gitignore``: skip what git ignores
    inside git work trees: ``.gitignore`` files, ``.git/info/exclude``, the global excludes file. ``exclude``: names
    skipped with everything below them, in addition to ``.git``. ``max_bytes``: bytes of the listing payload: per entry
    one kind byte, the path, and a NUL. ``timeout``: seconds."""

    def __init__(
        self,
        hidden: bool = True,
        gitignore: bool = True,
        exclude: list[str] | None = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout: float = DEFAULT_TIMEOUT_MS / 1000,
    ) -> None:
        self.hidden = hidden
        self.gitignore = gitignore
        self.exclude = [] if exclude is None else exclude
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.timeout = timeout

    @staticmethod
    def from_json(json: Any) -> Options:
        def count(key: str, default: int) -> int:
            value = get(json, key)
            if value is None:
                return default
            parsed = as_u64(value)
            if parsed is None:
                raise Failure("EINVAL", f"invalid field {key}")
            return parsed

        names = get(json, "exclude")
        if names is None:
            exclude: list[str] = []
        elif isinstance(names, list) and all(isinstance(name, str) for name in names):
            exclude = list(names)
        else:
            raise Failure("EINVAL", "invalid field exclude")
        hidden = as_bool(get(json, "hidden"))
        gitignore = as_bool(get(json, "gitignore"))
        return Options(
            hidden=True if hidden is None else hidden,
            gitignore=True if gitignore is None else gitignore,
            exclude=exclude,
            max_entries=count("maxEntries", DEFAULT_MAX_ENTRIES),
            max_bytes=min(count("maxBytes", DEFAULT_MAX_BYTES), MAX_PAYLOAD),
            timeout=count("timeoutMs", DEFAULT_TIMEOUT_MS) / 1000,
        )


class Listing:
    """``payload``: records of a kind byte (``f``, ``d``, ``l``), the path relative to the root with ``/`` separators, and
    a NUL."""

    def __init__(self) -> None:
        self.payload = bytearray()
        self.count = 0
        self.truncated = False


# --- Globs: globset's parser and regular expressions (glob.rs), with the options gitignore.rs builds them with ---------
# (`literal_separator`, `backslash_escape`, `allow_unclosed_class`; not `case_insensitive`, not `empty_alternates`).

# Tokens, as globset's `Token`: (kind,) or (kind, value).
LITERAL = "Literal"
ANY = "Any"
ZERO_OR_MORE = "ZeroOrMore"
RECURSIVE_PREFIX = "RecursivePrefix"
RECURSIVE_SUFFIX = "RecursiveSuffix"
RECURSIVE_ZERO_OR_MORE = "RecursiveZeroOrMore"
CLASS = "Class"
ALTERNATES = "Alternates"


class GlobError(Exception):
    """A glob that does not parse (globset's ``ErrorKind``): the line it came from is no rule."""


def is_separator(character: str | None) -> bool:
    """``std::path::is_separator``."""
    return character == "/" or (system.WINDOWS and character == "\\")


class _Parser:
    """globset's ``Parser``: the glob's characters one at a time, alternation branches on a stack."""

    def __init__(self, glob: str) -> None:
        self.glob = glob
        self.alternates_stack: list[int] = []
        self.branches: list[list[tuple[Any, ...]]] = [[]]
        self.index = 0
        self.prev: str | None = None
        self.cur: str | None = None
        self.found_unclosed_class = False

    def parse(self) -> None:
        while True:
            character = self.bump()
            if character is None:
                return
            if character == "?":
                self.push_token((ANY,))
            elif character == "*":
                self.parse_star()
            elif character == "[" and not self.found_unclosed_class:
                self.parse_class()
            elif character == "{":
                self.push_alternate()
            elif character == "}":
                self.pop_alternate()
            elif character == ",":
                self.parse_comma()
            elif character == "\\":
                self.parse_backslash()
            else:
                self.push_token((LITERAL, character))

    def push_alternate(self) -> None:
        self.alternates_stack.append(len(self.branches))
        self.branches.append([])

    def pop_alternate(self) -> None:
        if not self.alternates_stack:
            raise GlobError("unopened alternate group; missing '{'")
        start = self.alternates_stack.pop()
        alternates = self.branches[start:]
        del self.branches[start:]
        self.push_token((ALTERNATES, alternates))

    def push_token(self, token: tuple[Any, ...]) -> None:
        self.branches[-1].append(token)

    def pop_token(self) -> tuple[Any, ...]:
        return self.branches[-1].pop()

    def have_tokens(self) -> bool:
        return len(self.branches[-1]) > 0

    def parse_comma(self) -> None:
        # Outside an alternation a comma is a literal; inside it starts the next branch.
        if not self.alternates_stack:
            self.push_token((LITERAL, ","))
        else:
            self.branches.append([])

    def parse_backslash(self) -> None:
        character = self.bump()
        if character is None:
            raise GlobError("dangling '\\'")
        self.push_token((LITERAL, character))

    def parse_star(self) -> None:
        prev = self.prev
        if self.peek() != "*":
            self.push_token((ZERO_OR_MORE,))
            return
        self.bump()
        if not self.have_tokens():
            following = self.peek()
            if following is not None and not is_separator(following):
                self.push_token((ZERO_OR_MORE,))
                self.push_token((ZERO_OR_MORE,))
            else:
                self.push_token((RECURSIVE_PREFIX,))
                self.bump()
            return
        if not is_separator(prev) and (len(self.branches) <= 1 or (prev != "," and prev != "{")):
            self.push_token((ZERO_OR_MORE,))
            self.push_token((ZERO_OR_MORE,))
            return
        following = self.peek()
        if following is None:
            self.bump()
            is_suffix = True
        elif following in (",", "}") and len(self.branches) >= 2:
            is_suffix = True
        elif is_separator(following):
            self.bump()
            is_suffix = False
        else:
            self.push_token((ZERO_OR_MORE,))
            self.push_token((ZERO_OR_MORE,))
            return
        last = self.pop_token()
        if last[0] in (RECURSIVE_PREFIX, RECURSIVE_SUFFIX):
            self.push_token(last)
        else:
            self.push_token((RECURSIVE_SUFFIX,) if is_suffix else (RECURSIVE_ZERO_OR_MORE,))

    def parse_class(self) -> None:
        # Where to go back to when the class never closes: then `[` is a literal, and so is every later one.
        saved = (self.index, self.prev, self.cur)
        ranges: list[tuple[str, str]] = []
        negated = self.peek() in ("!", "^")
        if negated:
            self.bump()
        first = True
        in_range = False

        def add_to_last_range(add: str) -> None:
            low = ranges[-1][0]
            ranges[-1] = (low, add)
            if add < low:
                raise GlobError(f"invalid range; '{low}' > '{add}'")

        while True:
            character = self.bump()
            if character is None:
                self.index, self.prev, self.cur = saved
                self.found_unclosed_class = True
                self.push_token((LITERAL, "["))
                return
            if character == "]":
                if first:
                    ranges.append(("]", "]"))
                else:
                    break
            elif character == "-":
                if first:
                    ranges.append(("-", "-"))
                elif in_range:
                    add_to_last_range("-")
                    in_range = False
                else:
                    in_range = True
            else:
                if in_range:
                    add_to_last_range(character)
                else:
                    ranges.append((character, character))
                in_range = False
            first = False
        if in_range:
            # The class ended with `-`: a literal one.
            ranges.append(("-", "-"))
        self.push_token((CLASS, negated, ranges))

    def bump(self) -> str | None:
        self.prev = self.cur
        if self.index < len(self.glob):
            self.cur = self.glob[self.index]
            self.index += 1
        else:
            self.cur = None
        return self.cur

    def peek(self) -> str | None:
        return self.glob[self.index] if self.index < len(self.glob) else None


def _escaped(character: str) -> str:
    """``char_to_escaped_literal``: the character's UTF-8 bytes, each one escaped, for a regular expression over
    bytes."""
    return "".join(f"\\x{byte:02x}" for byte in character.encode("utf-8"))


def _tokens_regex(tokens: list[tuple[Any, ...]]) -> str:
    """``Tokens::tokens_to_regex`` with ``literal_separator``."""
    out: list[str] = []
    for token in tokens:
        kind = token[0]
        if kind == LITERAL:
            out.append(_escaped(token[1]))
        elif kind == ANY:
            out.append("[^/]")
        elif kind == ZERO_OR_MORE:
            out.append("[^/]*")
        elif kind == RECURSIVE_PREFIX:
            out.append("(?:/?|.*/)")
        elif kind == RECURSIVE_SUFFIX:
            out.append("/.*")
        elif kind == RECURSIVE_ZERO_OR_MORE:
            out.append("(?:/|/.*/)")
        elif kind == CLASS:
            body = "".join(
                _escaped(low) if low == high else f"{_escaped(low)}-{_escaped(high)}" for low, high in token[2]
            )
            out.append(f"[^{body}]" if token[1] else f"[{body}]")
        else:
            # An empty branch is dropped (no `empty_alternates`); with none left the group matches nothing extra.
            parts = [part for part in (_tokens_regex(branch) for branch in token[1]) if part != ""]
            if parts:
                out.append(f"(?:{'|'.join(parts)})")
    return "".join(out)


def glob_regex(glob: str) -> re.Pattern[bytes]:
    """The regular expression globset compiles ``glob`` to (``GlobBuilder::build``, ``to_regex_with``, ``new_regex``):
    over bytes, ``.`` matching a newline too, anchored at both ends (``fullmatch``)."""
    parser = _Parser(glob)
    parser.parse()
    if len(parser.branches) > 1:
        raise GlobError("unclosed alternate group; missing '}'")
    tokens = parser.branches[0]
    # The whole glob `**` matches everything.
    body = ".*" if tokens == [(RECURSIVE_PREFIX,)] else _tokens_regex(tokens)
    try:
        return re.compile(body.encode("ascii"), re.DOTALL)
    except re.error as error:
        raise GlobError(str(error)) from None


# --- Gitignore rules: the crate's gitignore.rs ------------------------------------------------------------------------


class Rule:
    """One gitignore glob (the crate's ``gitignore::Glob``): a regular expression over the path, as bytes, relative to
    its file's directory."""

    __slots__ = ("directory_only", "negated", "regex")

    def __init__(self, regex: re.Pattern[bytes], negated: bool, directory_only: bool) -> None:
        self.regex = regex
        self.negated = negated
        self.directory_only = directory_only


# Rust's `char::is_whitespace` (Unicode `White_Space`), what `str::trim_end` trims; `str.isspace` also takes U+001C-001F.
_WHITE_SPACE = "\t\n\x0b\x0c\r \x85\xa0                　"


def parse_rule(line: str) -> Rule | None:
    """A rule from one line of a gitignore file (``GitignoreBuilder::add_line``), or ``None`` for a blank line, a comment,
    or a glob that does not parse."""
    if line.startswith("#"):
        return None
    if not line.endswith("\\ "):
        line = line.rstrip(_WHITE_SPACE)
    if line == "":
        return None
    whitelist = False
    absolute = False
    if line.startswith(("\\!", "\\#")):
        line = line[1:]
        absolute = line[:1] == "/"
    else:
        if line.startswith("!"):
            whitelist = True
            line = line[1:]
        if line.startswith("/"):
            # Anchored to the file's directory: wildcards cannot match a `/` before it.
            line = line[1:]
            absolute = True
    directory_only = False
    if line.endswith("/"):
        directory_only = True
        line = line[:-1]
        # An escaped trailing slash loses its escape too.
        line = line.removesuffix("\\")
    actual = line
    # Without a slash it matches at any depth.
    if not absolute and "/" not in line and not (actual.startswith("**/") or actual == "**"):
        actual = f"**/{actual}"
    # `dir/**` matches what is inside, not the directory itself.
    if actual.endswith("/**"):
        actual = f"{actual}/*"
    try:
        regex = glob_regex(actual)
    except GlobError:
        return None
    return Rule(regex, whitelist, directory_only)


def _candidate(path: str | bytes) -> bytes:
    return path.encode("utf-8") if isinstance(path, str) else path


class Matcher:
    """The rules of one gitignore file (the crate's ``Gitignore``), matched relative to the directory it applies to."""

    def __init__(self, rules: list[Rule]) -> None:
        self.rules = rules

    @staticmethod
    def read(path: str | bytes) -> Matcher | None:
        """``GitignoreBuilder::add``: the file's lines, the first without a leading byte order mark; reading stops at a
        line that is not UTF-8 (``BufRead::lines``). ``None`` when the file cannot be read or has no rules."""
        try:
            with open(path, "rb") as file:  # windows-footgun: ok - binary mode
                content = file.read()
        except (OSError, ValueError):
            return None
        rules: list[Rule] = []
        lines = content.split(b"\n")
        ends_with_newline = lines[-1] == b""
        if ends_with_newline:
            lines.pop()
        for index, raw in enumerate(lines):
            # `lines()` drops the `\r` of a `\r\n`, not a `\r` that ends the file.
            if raw.endswith(b"\r") and (index < len(lines) - 1 or ends_with_newline):
                raw = raw[:-1]
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                break
            if index == 0:
                line = line.lstrip("﻿")
            rule = parse_rule(line)
            if rule is not None:
                rules.append(rule)
        return Matcher(rules) if rules else None

    def matched(self, relative: str | bytes, is_dir: bool) -> bool | None:
        """Whether the last matching rule ignores (``True``) or whitelists (``False``) ``relative``; ``None`` without a
        match. A directory-only rule is skipped for a file."""
        candidate = _candidate(relative)
        for rule in reversed(self.rules):
            if rule.directory_only and not is_dir:
                continue
            if rule.regex.fullmatch(candidate):
                return not rule.negated
        return None


# --- Which rules apply: the crate's dir.rs ----------------------------------------------------------------------------


def _join(base: str | bytes, name: str) -> str | bytes:
    return os.path.join(base, name.encode("utf-8") if isinstance(base, bytes) else name)


def _first_line(path: str | bytes) -> str | None:
    """``BufReader::lines().next()``: the first line without its ``\\n`` (or ``\\r\\n``); ``None`` when the file cannot be
    read, is empty, or the line is not UTF-8."""
    try:
        with open(path, "rb") as file:  # windows-footgun: ok - binary mode
            content = file.read()
    except (OSError, ValueError):
        return None
    if content == b"":
        return None
    end = content.find(b"\n")
    if end == -1:
        line = content
    else:
        line = content[:end]
        if line.endswith(b"\r"):
            line = line[:-1]
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        return None


def resolve_git_commondir(directory: str | bytes, git_mode: int | None) -> str | bytes | None:
    """Where ``info/exclude`` lives for the repository in ``directory`` (the crate's ``resolve_git_commondir``):
    ``.git`` when it is not a file; for a ``.git`` file, the ``commondir`` of the directory its ``gitdir: `` line names,
    relative to that directory when it starts with ``.``; ``None`` when that fails. Like the crate, and unlike git, a
    relative ``gitdir`` is taken from the daemon's working directory, and a git directory without ``commondir`` (a
    submodule's) has no ``info/exclude``."""
    git_dir = _join(directory, ".git")
    if git_mode is None or not stat.S_ISREG(git_mode):
        return git_dir
    line = _first_line(git_dir)
    if line is None or not line.startswith("gitdir: "):
        return None
    real_git_dir = line[len("gitdir: ") :]
    commondir = _first_line(os.path.join(real_git_dir, "commondir"))
    if commondir is None:
        return None
    return os.path.join(real_git_dir, commondir) if commondir.startswith(".") else commondir


class Directory:
    """The rules a directory contributes (the crate's ``IgnoreInner``): its ``.gitignore`` and, when it holds a
    repository (``has_git``: ``.git`` or ``.jj``), that repository's ``info/exclude``, both matched relative to it.
    ``offset``: where a path relative to the listed directory continues relative to this one; ``prefix``: what a parent
    of the listed directory puts in front of such a path. ``any_git``: whether this directory or one above holds a
    repository."""

    __slots__ = ("any_git", "exclude", "has_git", "ignore", "offset", "prefix")

    def __init__(self, path: str | bytes) -> None:
        try:
            git_mode: int | None = os.stat(_join(path, ".git")).st_mode
        except (OSError, ValueError):
            git_mode = None
        self.has_git = git_mode is not None or os.path.exists(_join(path, ".jj"))
        self.ignore = Matcher.read(_join(path, ".gitignore"))
        self.exclude: Matcher | None = None
        if git_mode is not None:
            common = resolve_git_commondir(path, git_mode)
            if common is not None:
                self.exclude = Matcher.read(_join(common, "info/exclude"))
        self.offset = 0
        self.prefix = b""
        self.any_git = self.has_git


def _home_dir() -> str | None:
    """``std::env::home_dir``: ``$HOME`` unless it is empty, else the user database's home directory."""
    if system.WINDOWS:
        return system.home() or None
    home = os.environ.get("HOME")
    if home:
        return home
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir  # windows-footgun: ok - `import pwd` fails first on Windows
    except (ImportError, KeyError, OSError):
        return None


def _non_empty_variable(key: str) -> str | None:
    value = os.environ.get(key)
    return value if value else None


def _read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as file:  # windows-footgun: ok - binary mode
            return file.read()
    except (OSError, ValueError):
        return None


_EXCLUDES_FILE = re.compile(rb'(?im)^\s*excludesfile\s*=\s*"?\s*(\S+?)\s*"?\s*$')


def _parse_excludes_file(data: bytes | None) -> str | None:
    """``parse_excludes_file``: the value of the first ``excludesfile = …`` line, ``~`` expanded."""
    if data is None:
        return None
    found = _EXCLUDES_FILE.search(data)
    if found is None:
        return None
    try:
        value = found.group(1).decode("utf-8")
    except UnicodeDecodeError:
        return None
    home = _home_dir()
    if home is None:
        return value
    return value.replace("~", os.fsencode(home).decode("utf-8", "replace"))


def gitconfig_excludes_path() -> str | None:
    """Where the global excludes file is (the crate's ``gitconfig_excludes_path``)."""
    home = _home_dir()
    xdg = _non_empty_variable("XDG_CONFIG_HOME") or (None if home is None else os.path.join(home, ".config"))
    global_config = _non_empty_variable("GIT_CONFIG_GLOBAL")
    for config in (
        global_config,
        None if home is None else os.path.join(home, ".gitconfig"),
        None if xdg is None else os.path.join(xdg, "git", "config"),
        _non_empty_variable("GIT_CONFIG_SYSTEM") or "/etc/gitconfig",
    ):
        found = None if config is None else _parse_excludes_file(_read(config))
        if found is not None:
            return found
    return None if xdg is None else os.path.join(xdg, "git", "ignore")


def global_excludes() -> Matcher | None:
    """The global excludes file's rules (``build_global``); a relative path is the daemon's working directory's, as the
    crate opens it."""
    path = gitconfig_excludes_path()
    if path is None or not os.path.isfile(path):
        return None
    return Matcher.read(path)


class Ignores:
    """The crate's ``Ignore`` chain for a walk: the listed directory's parents (``add_parents``, from its canonical path
    up), then a ``Directory`` per directory the walk is in, and the global excludes file matched relative to the listed
    directory."""

    def __init__(self, root: str | bytes) -> None:
        self.globals = global_excludes()
        self.parents: list[Directory] = []
        canonical = os.path.realpath(root)
        current = canonical
        while True:
            parent = os.path.dirname(current)
            if parent == current:
                break
            directory = Directory(parent)
            rest = canonical[len(parent) :]
            rest = rest.lstrip(os.sep if isinstance(rest, str) else os.sep.encode())
            if isinstance(rest, str):
                rest = system.to_string(rest).encode("utf-8")
            directory.prefix = (rest.replace(b"\\", b"/") if system.WINDOWS else rest) + b"/"
            self.parents.append(directory)
            current = parent
        self.parents_have_git = any(directory.has_git for directory in self.parents)

    def root(self, path: str | bytes) -> Directory:
        directory = Directory(path)
        directory.any_git = directory.has_git or self.parents_have_git
        return directory

    @staticmethod
    def child(parent: Directory, path: str | bytes, relative: bytes) -> Directory:
        directory = Directory(path)
        directory.offset = len(relative) + 1
        directory.any_git = directory.has_git or parent.any_git
        return directory

    def matched(self, chain: list[Directory], relative: bytes, is_dir: bool) -> bool | None:
        """``Ignore::matched_ignore`` for ``relative`` (to the listed directory) in the last directory of ``chain``:
        ``True`` ignored, ``False`` whitelisted, ``None`` neither."""
        if not chain[-1].any_git:
            return None
        ignored: bool | None = None
        excluded: bool | None = None
        saw_git = False
        for directory in reversed(chain):
            path = relative[directory.offset :]
            if ignored is None and directory.ignore is not None:
                ignored = directory.ignore.matched(path, is_dir)
            if excluded is None and directory.exclude is not None:
                excluded = directory.exclude.matched(path, is_dir)
            if directory.has_git:
                saw_git = True
                break
        if not saw_git:
            for directory in self.parents:
                path = directory.prefix + relative
                if ignored is None and directory.ignore is not None:
                    ignored = directory.ignore.matched(path, is_dir)
                if excluded is None and directory.exclude is not None:
                    excluded = directory.exclude.matched(path, is_dir)
                if directory.has_git:
                    break
        if ignored is not None:
            return ignored
        if excluded is not None:
            return excluded
        return None if self.globals is None else self.globals.matched(relative, is_dir)


# --- The walk --------------------------------------------------------------------------------------------------------


def _is_hidden(name: bytes, entry: Any) -> bool:
    if name.startswith(b"."):
        return True
    if system.WINDOWS:
        try:
            attributes = entry.stat(follow_symlinks=False).st_file_attributes
        except (OSError, AttributeError):
            return False
        return bool(attributes & stat.FILE_ATTRIBUTE_HIDDEN)  # type: ignore[attr-defined]
    return False


def list_files(root: str, options: Options, aborted: Callable[[], bool]) -> Listing:
    """List ``root`` depth first, siblings in byte order of their names. Stops with ``truncated`` at the first entry
    beyond a bound; fails with ``aborted`` once ``aborted()`` is true. Entries that cannot be read are skipped."""
    try:
        metadata = os.stat(system.os_path(root))
    except (OSError, ValueError) as error:
        raise Failure.io(error, "scandir", root) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise Failure("ENOTDIR", f"ENOTDIR: not a directory, scandir '{root}'").path(root)
    top = system.os_path(root)
    ignores = Ignores(top) if options.gitignore else None
    exclude = set(options.exclude)
    started = time.monotonic()
    listing = Listing()

    def entries(directory: Any) -> Any:
        """The entries of a directory, by its path as the system takes it, in byte order of their names."""
        try:
            with os.scandir(directory) as scanned:
                found = list(scanned)
        except (OSError, ValueError):
            return iter(())
        found.sort(key=lambda entry: os.fsencode(entry.name))
        return iter(found)

    # Depth first: a stack of (the entries of a directory, its path relative to the root, as text and as the bytes rules
    # match), with the chain of the directories' rules alongside.
    stack: list[tuple[Any, str, bytes]] = [(entries(top), "", b"")]
    chain: list[Directory] = [] if ignores is None else [ignores.root(top)]
    while stack:
        if aborted():
            raise Failure("aborted", "aborted")
        iterator, base, base_raw = stack[-1]
        entry = next(iterator, None)
        if entry is None:
            stack.pop()
            if chain:
                chain.pop()
            continue
        raw = os.fsencode(entry.name)
        name = raw.decode("utf-8", "replace")
        if name == ".git" or name in exclude:
            continue
        try:
            mode = entry.stat(follow_symlinks=False).st_mode
        except OSError:
            continue
        if stat.S_ISLNK(mode):
            kind = b"l"
        elif stat.S_ISDIR(mode):
            kind = b"d"
        elif stat.S_ISREG(mode):
            kind = b"f"
        else:
            continue
        path = name if base == "" else f"{base}/{name}"
        matched_name = name.encode("utf-8") if system.WINDOWS else raw
        relative = matched_name if base_raw == b"" else base_raw + b"/" + matched_name
        # A rule decides first; a hidden name is left out only when none does (`matched_dir_entry`).
        decided = None if ignores is None else ignores.matched(chain, relative, kind == b"d")
        if decided is True:
            continue
        if decided is None and not options.hidden and _is_hidden(raw, entry):
            continue
        encoded = path.encode("utf-8")
        size = len(encoded) + 2
        if (
            listing.count >= options.max_entries
            or len(listing.payload) + size > options.max_bytes
            or time.monotonic() - started > options.timeout
        ):
            listing.truncated = True
            break
        listing.payload.append(kind[0])
        listing.payload.extend(encoded)
        listing.payload.append(0)
        listing.count += 1
        if kind == b"d":
            stack.append((entries(entry.path), path, relative))
            if ignores is not None:
                chain.append(ignores.child(chain[-1], entry.path, relative))
    return listing


def run(json: Any, aborted: Callable[[], bool]) -> tuple[dict[str, Any], bytes]:
    root = get(json, "path")
    if not isinstance(root, str):
        raise Failure("EINVAL", "missing field path")
    listing = list_files(root, Options.from_json(json), aborted)
    return {"count": listing.count, "truncated": listing.truncated}, bytes(listing.payload)


__all__ = [
    "Directory",
    "Ignores",
    "Listing",
    "Matcher",
    "Options",
    "Rule",
    "glob_regex",
    "list_files",
    "parse_rule",
    "resolve_git_commondir",
    "run",
]

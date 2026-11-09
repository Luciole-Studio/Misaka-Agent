"""Translated from pi-tui's ``autocomplete.ts`` (v1.1.0).

MISAKA forks, each marked where it happens: Node's ``path`` functions come from ``misaka.utils.node_path`` (Node's
own semantics, so completions match upstream's on Windows too); the TS object literals are TypedDicts; fd's stderr is
read and dropped (Node leaves the pipe unread).
"""

from __future__ import annotations

import functools
import os
import re
import stat
import sys
from collections import abc
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from misaka.ui.tui.fuzzy import fuzzyFilter
from misaka.ui.tui.utils import autocompleteBoundaryRegex, autocompleteSeparatorRegex
from misaka.utils import js_regexp, node_path
from misaka.utils.child_process import spawn_child_process
from misaka.utils.collation import localeCompare
from misaka.utils.js import Utf8Decoder, awaitValue, jsIndex, jsTrim, jsTrimStart
from misaka.utils.js_regexp import RegExp
from misaka.utils.paths import homedir

PATH_DELIMITERS = {" ", "\t", '"', "'", "="}
tokenStartRegex = RegExp(f"{autocompleteBoundaryRegex.source}$", "u")
# Opening wrappers that may precede a path in prose, mapped to their closing counterpart.
PATH_WRAPPERS: dict[str, str] = {"(": ")", "[": "]", "{": "}", "<": ">", "`": "`"}


def toDisplayPath(value: str) -> str:
    return value.replace("\\", "/")


def escapeRegex(value: str) -> str:
    return js_regexp.replace(value, RegExp(r"[.*+?^${}()|[\]\\]", "g"), "\\$&")


def buildFdPathQuery(query: str) -> str:
    normalized = toDisplayPath(query)
    if "/" not in normalized:
        return normalized

    hasTrailingSeparator = normalized.endswith("/")
    trimmed = js_regexp.replace(normalized, RegExp(r"^\/+|\/+$", "g"), "")
    if not trimmed:
        return normalized

    separatorPattern = "[\\\\/]"
    segments = [escapeRegex(segment) for segment in trimmed.split("/") if segment]
    if len(segments) == 0:
        return normalized

    pattern = separatorPattern.join(segments)
    if hasTrailingSeparator:
        pattern += separatorPattern
    return pattern


def findLastDelimiter(text: str) -> int:
    lastDelimiter = -1
    index = 0
    for character in text:
        index += len(character)
        if character in PATH_DELIMITERS or autocompleteSeparatorRegex.test(character):
            lastDelimiter = index - 1
    return lastDelimiter


# Strip opening wrappers before a path, e.g. "(~/Dev" -> "~/Dev" or "`src/ma" -> "src/ma".
# Keep a wrapper if the token also contains its closer, e.g. "app/[slug]/pa" or "(group)/pa".
def stripLeadingWrappers(token: str) -> str:
    result = token
    while len(result) > 0:
        closer = PATH_WRAPPERS.get(result[0])
        if not closer or result.find(closer, 1) != -1:
            break
        result = result[1:]
    return result


def findUnclosedQuoteStart(text: str) -> int | None:
    inQuotes = False
    quoteStart = -1

    for i in range(len(text)):
        if text[i] == '"':
            inQuotes = not inQuotes
            if inQuotes:
                quoteStart = i

    return quoteStart if inQuotes else None


def isTokenStart(text: str, index: int) -> bool:
    start = index
    while start > 0 and PATH_WRAPPERS.get(text[start - 1]):
        start -= 1
    return (text[start - 1] if start > 0 else "") in PATH_DELIMITERS or tokenStartRegex.test(text[:start])


def extractQuotedPrefix(text: str) -> str | None:
    quoteStart = findUnclosedQuoteStart(text)
    if quoteStart is None:
        return None

    if quoteStart > 0 and text[quoteStart - 1] == "@":
        if not isTokenStart(text, quoteStart - 1):
            return None
        return text[quoteStart - 1 :]

    if not isTokenStart(text, quoteStart):
        return None

    return text[quoteStart:]


def _isRooted(path: str) -> bool:
    """MISAKA fork: a path from the root, which upstream tests as a leading ``/``; on Windows a drive path
    (``C:/`` or ``C:\\``) too, which ``path.join`` would otherwise put under the base directory."""
    return path.startswith("/") or (sys.platform == "win32" and re.match(r"[A-Za-z]:[\\/]", path) is not None)


class _PathPrefix(TypedDict):
    """MISAKA fork: a name for the inline result type of ``parsePathPrefix``."""

    rawPrefix: str
    isAtPrefix: bool
    isQuotedPrefix: bool


def parsePathPrefix(prefix: str) -> _PathPrefix:
    if prefix.startswith('@"'):
        return {"rawPrefix": prefix[2:], "isAtPrefix": True, "isQuotedPrefix": True}
    if prefix.startswith('"'):
        return {"rawPrefix": prefix[1:], "isAtPrefix": False, "isQuotedPrefix": True}
    if prefix.startswith("@"):
        return {"rawPrefix": prefix[1:], "isAtPrefix": True, "isQuotedPrefix": False}
    return {"rawPrefix": prefix, "isAtPrefix": False, "isQuotedPrefix": False}


class _CompletionValueOptions(TypedDict):
    """MISAKA fork: a name for the inline ``options`` type of ``buildCompletionValue``."""

    isDirectory: bool
    isAtPrefix: bool
    isQuotedPrefix: bool


def buildCompletionValue(path: str, options: _CompletionValueOptions) -> str:
    needsQuotes = options["isQuotedPrefix"] or autocompleteSeparatorRegex.test(path)
    prefix = "@" if options["isAtPrefix"] else ""

    if not needsQuotes:
        return f"{prefix}{path}"

    openQuote = f'{prefix}"'
    closeQuote = '"'
    return f"{openQuote}{path}{closeQuote}"


class _FdEntry(TypedDict):
    """MISAKA fork: a name for the inline ``{ path; isDirectory }`` type."""

    path: str
    isDirectory: bool


# Use fd to walk directory tree (fast, respects .gitignore)
async def walkDirectoryWithFd(
    baseDir: str,
    fdPath: str,
    query: str,
    maxResults: int,
    signal: Any,
    maxDepth: int | None = None,
) -> list[_FdEntry]:
    args = [
        "--base-directory",
        baseDir,
        "--max-results",
        str(maxResults),
        "--type",
        "f",
        "--type",
        "d",
        "--follow",
        "--hidden",
        "--exclude",
        ".git",
        "--exclude",
        ".git/*",
        "--exclude",
        ".git/**",
    ]

    if maxDepth is not None:
        args.extend(["--max-depth", str(maxDepth)])

    if "/" in toDisplayPath(query):
        args.append("--full-path")

    if query:
        args.append(buildFdPathQuery(query))

    if signal.aborted:
        return []

    # `child.stdout.setEncoding("utf-8")`: a string decoder, which keeps a leading BOM.
    decoder = Utf8Decoder(ignoreBOM=True)
    stdout = ""

    def onData(fd: int, data: bytes) -> None:
        nonlocal stdout
        # MISAKA fork: stderr is read and dropped; Node leaves that pipe unread.
        if fd == 1:
            stdout += decoder.decode(data)

    try:
        child = await spawn_child_process(fdPath, *args, cwd=os.getcwd(), on_data=onData)
    except Exception:  # noqa: BLE001 - `child.on("error")`: no suggestions
        return []

    def onAbort() -> None:
        if child.returncode is None:
            child.kill()

    signal.addEventListener("abort", onAbort, True)
    try:
        code = await child.wait()
    except Exception:  # noqa: BLE001 - `child.on("error")`: no suggestions
        return []
    finally:
        signal.removeEventListener("abort", onAbort)
    stdout += decoder.decode(b"", True)

    if signal.aborted or code != 0 or not stdout:
        return []

    lines = [line for line in jsTrim(stdout).split("\n") if line]
    results: list[_FdEntry] = []

    for line in lines:
        displayLine = toDisplayPath(line)
        hasTrailingSeparator = displayLine.endswith("/")
        normalizedPath = displayLine[:-1] if hasTrailingSeparator else displayLine
        if normalizedPath == ".git" or normalizedPath.startswith(".git/") or "/.git/" in normalizedPath:
            continue

        results.append({"path": displayLine, "isDirectory": hasTrailingSeparator})

    return results


@dataclass(slots=True)
class AutocompleteItem:
    value: str
    label: str
    description: str | None = None


type Awaitable[T] = T | abc.Awaitable[T]


@dataclass(slots=True)
class SlashCommand:
    name: str
    description: str | None = None
    argumentHint: str | None = None
    # Function to get argument completions for this command
    # Returns null if no argument completion is available
    getArgumentCompletions: Callable[[str], Awaitable[list[AutocompleteItem] | None]] | None = None


@dataclass(slots=True)
class AutocompleteSuggestions:
    items: list[AutocompleteItem]
    prefix: str  # What we're matching against (e.g., "/" or "src/")


class AppliedCompletion(TypedDict):
    """MISAKA fork: a name for the inline result type of ``AutocompleteProvider.applyCompletion``."""

    lines: list[str]
    cursorLine: int
    cursorCol: int


class AutocompleteProvider(Protocol):
    """MISAKA fork: a Protocol cannot mark members optional, so the two optional ones are not declared and callers read
    them with ``getattr``: ``triggerCharacters`` (characters that should naturally trigger this provider at token
    boundaries) and ``shouldTriggerFileCompletion(lines, cursorLine, cursorCol)`` (whether file completion should
    trigger for explicit Tab completion). ``getSuggestions``'s ``options`` holds ``signal`` and an optional ``force``.
    """

    # Get autocomplete suggestions for current text/cursor position
    # Returns null if no suggestions available
    async def getSuggestions(
        self,
        lines: list[str],
        cursorLine: int,
        cursorCol: int,
        options: dict[str, Any],
    ) -> AutocompleteSuggestions | None: ...

    # Apply the selected item
    # Returns the new text and cursor position
    def applyCompletion(
        self,
        lines: list[str],
        cursorLine: int,
        cursorCol: int,
        item: AutocompleteItem,
        prefix: str,
    ) -> AppliedCompletion: ...


class _CommandItem(TypedDict):
    name: str
    label: str
    description: str | None


class _ScopedFuzzyQuery(TypedDict):
    baseDir: str
    query: str
    displayBase: str


class _FuzzyFileOptions(TypedDict):
    isQuotedPrefix: bool
    signal: Any


class _ScoredEntry(_FdEntry):
    score: int


# Combined provider that handles both slash commands and file paths
class CombinedAutocompleteProvider:
    def __init__(
        self,
        commands: Sequence[SlashCommand | AutocompleteItem] | None,
        basePath: str,
        fdPath: str | None = None,
    ) -> None:
        self.commands: Sequence[SlashCommand | AutocompleteItem] = commands if commands is not None else []
        self.basePath = basePath
        self.fdPath = fdPath

    async def getSuggestions(
        self,
        lines: list[str],
        cursorLine: int,
        cursorCol: int,
        options: dict[str, Any],
    ) -> AutocompleteSuggestions | None:
        currentLine = jsIndex(lines, cursorLine) or ""
        textBeforeCursor = currentLine[:cursorCol]

        atPrefix = self.extractAtPrefix(textBeforeCursor)
        if atPrefix:
            parsed = parsePathPrefix(atPrefix)
            suggestions = await self.getFuzzyFileSuggestions(
                parsed["rawPrefix"],
                {"isQuotedPrefix": parsed["isQuotedPrefix"], "signal": options["signal"]},
            )
            if len(suggestions) == 0:
                return None

            return AutocompleteSuggestions(items=suggestions, prefix=atPrefix)

        commandText = jsTrimStart(textBeforeCursor)
        if not options.get("force") and commandText.startswith("/"):
            spaceIndex = commandText.find(" ")

            if spaceIndex == -1:
                prefix = commandText[1:]
                commandItems: list[_CommandItem] = []
                for cmd in self.commands:
                    name = _commandName(cmd)
                    hint = getattr(cmd, "argumentHint", None) or None
                    desc = cmd.description if cmd.description is not None else ""
                    fullDesc = (f"{hint} — {desc}" if desc else hint) if hint else desc
                    commandItems.append({"name": name, "label": name, "description": fullDesc or None})

                bareNameMatches = fuzzyFilter(
                    commandItems,
                    prefix,
                    lambda item: item["name"].removeprefix("skill:"),
                )
                bareNameMatchSet = {id(item) for item in bareNameMatches}
                fullNameOnlyMatches = fuzzyFilter(
                    [item for item in commandItems if item["name"].startswith("skill:") and id(item) not in bareNameMatchSet],
                    prefix,
                    lambda item: item["name"],
                )
                filtered = [
                    AutocompleteItem(
                        value=item["name"],
                        label=item["label"],
                        description=item["description"] if item["description"] else None,
                    )
                    for item in [*bareNameMatches, *fullNameOnlyMatches]
                ]

                if len(filtered) == 0:
                    return None

                return AutocompleteSuggestions(items=filtered, prefix=commandText)

            commandName = commandText[1:spaceIndex]
            argumentText = commandText[spaceIndex + 1 :]

            command = next((cmd for cmd in self.commands if _commandName(cmd) == commandName), None)
            getArgumentCompletions = getattr(command, "getArgumentCompletions", None) if command else None
            if not command or not getArgumentCompletions:
                return None

            argumentSuggestions = await awaitValue(getArgumentCompletions(argumentText))
            if not isinstance(argumentSuggestions, list) or len(argumentSuggestions) == 0:
                return None

            return AutocompleteSuggestions(items=argumentSuggestions, prefix=argumentText)

        pathMatch = self.extractPathPrefix(textBeforeCursor, bool(options.get("force")))
        if pathMatch is None:
            return None

        fileSuggestions = self.getFileSuggestions(pathMatch)
        if len(fileSuggestions) == 0:
            return None

        return AutocompleteSuggestions(items=fileSuggestions, prefix=pathMatch)

    def applyCompletion(
        self,
        lines: list[str],
        cursorLine: int,
        cursorCol: int,
        item: AutocompleteItem,
        prefix: str,
    ) -> AppliedCompletion:
        currentLine = jsIndex(lines, cursorLine) or ""
        beforePrefix = currentLine[: cursorCol - len(prefix)]
        afterCursor = currentLine[cursorCol:]
        isQuotedPrefix = prefix.startswith('"') or prefix.startswith('@"')  # noqa: PIE810 - upstream test
        hasLeadingQuoteAfterCursor = afterCursor.startswith('"')
        hasTrailingQuoteInItem = item.value.endswith('"')
        adjustedAfterCursor = (
            afterCursor[1:] if isQuotedPrefix and hasTrailingQuoteInItem and hasLeadingQuoteAfterCursor else afterCursor
        )

        # Check if we're completing a slash command (prefix starts with "/" but NOT a file path)
        # Slash commands are at the start of the line and don't contain path separators after the first /
        isSlashCommand = prefix.startswith("/") and jsTrim(beforePrefix) == "" and "/" not in prefix[1:]
        if isSlashCommand:
            # This is a command name completion
            newLine = f"{beforePrefix}/{item.value} {adjustedAfterCursor}"
            newLines = [*lines]
            newLines[cursorLine] = newLine

            return {
                "lines": newLines,
                "cursorLine": cursorLine,
                "cursorCol": len(beforePrefix) + len(item.value) + 2,  # +2 for "/" and space
            }

        # Check if we're completing a file attachment (prefix starts with "@")
        if prefix.startswith("@"):
            # This is a file attachment completion
            # Don't add space after directories so user can continue autocompleting
            isDirectory = item.label.endswith("/")
            suffix = "" if isDirectory else " "
            newLine = f"{beforePrefix + item.value}{suffix}{adjustedAfterCursor}"
            newLines = [*lines]
            newLines[cursorLine] = newLine

            hasTrailingQuote = item.value.endswith('"')
            cursorOffset = len(item.value) - 1 if isDirectory and hasTrailingQuote else len(item.value)

            return {
                "lines": newLines,
                "cursorLine": cursorLine,
                "cursorCol": len(beforePrefix) + cursorOffset + len(suffix),
            }

        # Check if we're in a slash command context (beforePrefix contains "/command ")
        textBeforeCursor = currentLine[:cursorCol]
        if "/" in textBeforeCursor and " " in textBeforeCursor:
            # This is likely a command argument completion
            newLine = beforePrefix + item.value + adjustedAfterCursor
            newLines = [*lines]
            newLines[cursorLine] = newLine

            isDirectory = item.label.endswith("/")
            hasTrailingQuote = item.value.endswith('"')
            cursorOffset = len(item.value) - 1 if isDirectory and hasTrailingQuote else len(item.value)

            return {
                "lines": newLines,
                "cursorLine": cursorLine,
                "cursorCol": len(beforePrefix) + cursorOffset,
            }

        # For file paths, complete the path
        newLine = beforePrefix + item.value + adjustedAfterCursor
        newLines = [*lines]
        newLines[cursorLine] = newLine

        isDirectory = item.label.endswith("/")
        hasTrailingQuote = item.value.endswith('"')
        cursorOffset = len(item.value) - 1 if isDirectory and hasTrailingQuote else len(item.value)

        return {
            "lines": newLines,
            "cursorLine": cursorLine,
            "cursorCol": len(beforePrefix) + cursorOffset,
        }

    # Extract @ prefix for fuzzy file suggestions
    def extractAtPrefix(self, text: str) -> str | None:
        quotedPrefix = extractQuotedPrefix(text)
        if quotedPrefix is not None and quotedPrefix.startswith('@"'):
            return quotedPrefix

        lastDelimiterIndex = findLastDelimiter(text)
        token = stripLeadingWrappers(text if lastDelimiterIndex == -1 else text[lastDelimiterIndex + 1 :])

        if token.startswith("@"):
            return token

        return None

    # Extract a path-like prefix from the text before cursor
    def extractPathPrefix(self, text: str, forceExtract: bool = False) -> str | None:
        quotedPrefix = extractQuotedPrefix(text)
        if quotedPrefix:
            return quotedPrefix

        lastDelimiterIndex = findLastDelimiter(text)
        pathPrefix = stripLeadingWrappers(text if lastDelimiterIndex == -1 else text[lastDelimiterIndex + 1 :])

        # For forced extraction (Tab key), always return something
        if forceExtract:
            return pathPrefix

        # For natural triggers, return if it looks like a path, ends with /, starts with ~/, .
        # Only return empty string if the text looks like it's starting a path context
        if "/" in pathPrefix or pathPrefix.startswith(".") or pathPrefix.startswith("~/"):  # noqa: PIE810 - upstream test
            return pathPrefix

        # Return an empty prefix after whitespace or CJK punctuation, but not for empty text.
        # Empty text should not trigger file suggestions - that's for forced Tab completion
        if pathPrefix == "" and text != "" and tokenStartRegex.test(text):
            return pathPrefix

        return None

    # Expand home directory (~/) to actual home path
    def expandHomePath(self, path: str) -> str:
        if path.startswith("~/"):
            expandedPath = node_path.join(_homedir(), path[2:])
            # Preserve trailing slash if original path had one
            return f"{expandedPath}/" if path.endswith("/") and not expandedPath.endswith("/") else expandedPath
        elif path == "~":
            return _homedir()
        return path

    def resolveScopedFuzzyQuery(self, rawQuery: str) -> _ScopedFuzzyQuery | None:
        normalizedQuery = toDisplayPath(rawQuery)
        slashIndex = normalizedQuery.rfind("/")
        if slashIndex == -1:
            return None

        displayBase = normalizedQuery[: slashIndex + 1]
        query = normalizedQuery[slashIndex + 1 :]

        baseDir: str
        if displayBase.startswith("~/"):
            baseDir = self.expandHomePath(displayBase)
        elif _isRooted(displayBase):
            baseDir = displayBase
        else:
            baseDir = node_path.join(self.basePath, displayBase)

        try:
            if not stat.S_ISDIR(os.stat(baseDir).st_mode):
                return None
        except Exception:  # noqa: BLE001 - `statSync` throwing: not a directory
            return None

        return {"baseDir": baseDir, "query": query, "displayBase": displayBase}

    def scopedPathForDisplay(self, displayBase: str, relativePath: str) -> str:
        normalizedRelativePath = toDisplayPath(relativePath)
        if displayBase == "/":
            return f"/{normalizedRelativePath}"
        return f"{toDisplayPath(displayBase)}{normalizedRelativePath}"

    # Get file/directory suggestions for a given path prefix
    def getFileSuggestions(self, prefix: str) -> list[AutocompleteItem]:
        try:
            searchDir: str
            searchPrefix: str
            parsed = parsePathPrefix(prefix)
            rawPrefix, isAtPrefix, isQuotedPrefix = parsed["rawPrefix"], parsed["isAtPrefix"], parsed["isQuotedPrefix"]
            expandedPrefix = rawPrefix

            # Handle home directory expansion
            if expandedPrefix.startswith("~"):
                expandedPrefix = self.expandHomePath(expandedPrefix)

            isRootPrefix = (
                rawPrefix == ""
                or rawPrefix == "./"
                or rawPrefix == "../"
                or rawPrefix == "~"
                or rawPrefix == "~/"
                or rawPrefix == "/"
                or (isAtPrefix and rawPrefix == "")
            )

            if isRootPrefix:
                # Complete from specified position
                if rawPrefix.startswith("~") or _isRooted(expandedPrefix):
                    searchDir = expandedPrefix
                else:
                    searchDir = node_path.join(self.basePath, expandedPrefix)
                searchPrefix = ""
            elif rawPrefix.endswith("/"):
                # If prefix ends with /, show contents of that directory
                if rawPrefix.startswith("~") or _isRooted(expandedPrefix):
                    searchDir = expandedPrefix
                else:
                    searchDir = node_path.join(self.basePath, expandedPrefix)
                searchPrefix = ""
            else:
                # Split into directory and file prefix
                dir = node_path.dirname(expandedPrefix)
                file = node_path.basename(expandedPrefix)
                if rawPrefix.startswith("~") or _isRooted(expandedPrefix):
                    searchDir = dir
                else:
                    searchDir = node_path.join(self.basePath, dir)
                searchPrefix = file

            with os.scandir(searchDir) as entries:
                entryList = list(entries)
            suggestions: list[AutocompleteItem] = []

            for entry in entryList:
                if not entry.name.lower().startswith(searchPrefix.lower()):
                    continue

                # Check if entry is a directory (or a symlink pointing to a directory)
                isDirectory = entry.is_dir(follow_symlinks=False)
                if not isDirectory and entry.is_symlink():
                    try:
                        fullPath = node_path.join(searchDir, entry.name)
                        isDirectory = stat.S_ISDIR(os.stat(fullPath).st_mode)
                    except Exception:  # noqa: BLE001, S110 - upstream ignores the error
                        # Broken symlink or permission error - treat as file
                        pass

                relativePath: str
                name = entry.name
                displayPrefix = rawPrefix

                if displayPrefix.endswith("/"):
                    # If prefix ends with /, append entry to the prefix
                    relativePath = displayPrefix + name
                elif "/" in displayPrefix or "\\" in displayPrefix:
                    # Preserve ~/ format for home directory paths
                    if displayPrefix.startswith("~/"):
                        homeRelativeDir = displayPrefix[2:]  # Remove ~/
                        dir = node_path.dirname(homeRelativeDir)
                        relativePath = f"~/{name if dir == '.' else node_path.join(dir, name)}"
                    elif displayPrefix.startswith("/"):
                        # Absolute path - construct properly
                        dir = node_path.dirname(displayPrefix)
                        if dir == "/":
                            relativePath = f"/{name}"
                        else:
                            relativePath = f"{dir}/{name}"
                    else:
                        relativePath = node_path.join(node_path.dirname(displayPrefix), name)
                        # path.join normalizes away ./ prefix, preserve it
                        if displayPrefix.startswith("./") and not relativePath.startswith("./"):
                            relativePath = f"./{relativePath}"
                else:
                    # For standalone entries, preserve ~/ if original prefix was ~/
                    if displayPrefix.startswith("~"):
                        relativePath = f"~/{name}"
                    else:
                        relativePath = name

                relativePath = toDisplayPath(relativePath)
                pathValue = f"{relativePath}/" if isDirectory else relativePath
                value = buildCompletionValue(
                    pathValue,
                    {"isDirectory": isDirectory, "isAtPrefix": isAtPrefix, "isQuotedPrefix": isQuotedPrefix},
                )

                suggestions.append(AutocompleteItem(value=value, label=name + ("/" if isDirectory else "")))

            # Sort directories first, then alphabetically
            def compare(a: AutocompleteItem, b: AutocompleteItem) -> int:
                aIsDir = a.label.endswith("/")
                bIsDir = b.label.endswith("/")
                if aIsDir and not bIsDir:
                    return -1
                if not aIsDir and bIsDir:
                    return 1
                return localeCompare(a.label, b.label)

            suggestions.sort(key=_comparator(compare))

            return suggestions
        except Exception:  # noqa: BLE001 - upstream catches everything
            # Directory doesn't exist or not accessible
            return []

    # Score an entry against the query (higher = better match)
    # isDirectory adds bonus to prioritize folders
    def scoreEntry(self, filePath: str, query: str, isDirectory: bool) -> int:
        fileName = node_path.basename(filePath)
        lowerFileName = fileName.lower()
        lowerQuery = query.lower()

        score = 0

        # Exact filename match (highest)
        if lowerFileName == lowerQuery:
            score = 100
        # Filename starts with query
        elif lowerFileName.startswith(lowerQuery):
            score = 80
        # Substring match in filename
        elif lowerQuery in lowerFileName:
            score = 50
        # Substring match in full path
        elif lowerQuery in filePath.lower():
            score = 30

        # Directories get a bonus to appear first
        if isDirectory and score > 0:
            score += 10

        return score

    async def getBaseDirSuggestions(self, baseDir: str, query: str, signal: Any) -> list[_FdEntry]:
        if not self.fdPath or signal.aborted:
            return []

        return await walkDirectoryWithFd(baseDir, self.fdPath, query, 100, signal, 1)

    # Fuzzy file search using fd (fast, respects .gitignore)
    async def getFuzzyFileSuggestions(self, query: str, options: _FuzzyFileOptions) -> list[AutocompleteItem]:
        if not self.fdPath or options["signal"].aborted:
            return []

        try:
            scopedQuery = self.resolveScopedFuzzyQuery(query)
            fdBaseDir = scopedQuery["baseDir"] if scopedQuery is not None else self.basePath
            fdQuery = scopedQuery["query"] if scopedQuery is not None else query
            baseDirEntries = await self.getBaseDirSuggestions(fdBaseDir, fdQuery, options["signal"])
            recursiveEntries = await walkDirectoryWithFd(fdBaseDir, self.fdPath, fdQuery, 100, options["signal"])
            seenPaths = {entry["path"] for entry in baseDirEntries}
            entries: list[_FdEntry] = [*baseDirEntries]
            for entry in recursiveEntries:
                if entry["path"] in seenPaths:
                    continue
                seenPaths.add(entry["path"])
                entries.append(entry)
            if options["signal"].aborted:
                return []

            scoredEntries: list[_ScoredEntry] = []
            for entry in entries:
                scored: _ScoredEntry = {
                    **entry,
                    "score": self.scoreEntry(entry["path"], fdQuery, entry["isDirectory"]) if fdQuery else 1,
                }
                if scored["score"] > 0:
                    scoredEntries.append(scored)

            def compare(a: _ScoredEntry, b: _ScoredEntry) -> int:
                scoreDiff = b["score"] - a["score"]
                if scoreDiff != 0:
                    return scoreDiff

                aDepth = len([segment for segment in toDisplayPath(a["path"]).split("/") if segment])
                bDepth = len([segment for segment in toDisplayPath(b["path"]).split("/") if segment])
                depthDiff = aDepth - bDepth
                if depthDiff != 0:
                    return depthDiff

                lengthDiff = len(a["path"]) - len(b["path"])
                if lengthDiff != 0:
                    return lengthDiff

                return localeCompare(a["path"], b["path"])

            scoredEntries.sort(key=_comparator(compare))
            topEntries = scoredEntries[:20]

            suggestions: list[AutocompleteItem] = []
            for topEntry in topEntries:
                entryPath, isDirectory = topEntry["path"], topEntry["isDirectory"]
                pathWithoutSlash = entryPath[:-1] if isDirectory else entryPath
                displayPath = (
                    self.scopedPathForDisplay(scopedQuery["displayBase"], pathWithoutSlash)
                    if scopedQuery is not None
                    else pathWithoutSlash
                )
                entryName = node_path.basename(pathWithoutSlash)
                completionPath = f"{displayPath}/" if isDirectory else displayPath
                value = buildCompletionValue(
                    completionPath,
                    {"isDirectory": isDirectory, "isAtPrefix": True, "isQuotedPrefix": options["isQuotedPrefix"]},
                )

                suggestions.append(
                    AutocompleteItem(value=value, label=entryName + ("/" if isDirectory else ""), description=displayPath)
                )

            return suggestions
        except Exception:  # noqa: BLE001 - upstream catches everything
            return []

    # Check if we should trigger file completion (called on Tab key)
    def shouldTriggerFileCompletion(self, lines: list[str], cursorLine: int, cursorCol: int) -> bool:
        currentLine = jsIndex(lines, cursorLine) or ""
        textBeforeCursor = currentLine[:cursorCol]

        # Don't trigger if we're typing a slash command at the start of the line
        if jsTrim(textBeforeCursor).startswith("/") and " " not in jsTrim(textBeforeCursor):  # noqa: SIM103 - upstream if
            return False

        return True


def _commandName(cmd: SlashCommand | AutocompleteItem) -> str:
    """MISAKA fork: upstream's inline ``"name" in cmd ? cmd.name : cmd.value``."""
    return cmd.name if hasattr(cmd, "name") else cmd.value  # type: ignore[union-attr]


def _homedir() -> str:
    """MISAKA fork: ``os.homedir()``."""
    return homedir()


def _comparator[T](compare: Callable[[T, T], int]) -> Callable[[T], Any]:
    """MISAKA fork: ``Array.prototype.sort(compare)`` as a sort key (both sorts are stable)."""
    return functools.cmp_to_key(compare)


__all__ = [
    "AppliedCompletion",
    "AutocompleteItem",
    "AutocompleteProvider",
    "AutocompleteSuggestions",
    "CombinedAutocompleteProvider",
    "SlashCommand",
]

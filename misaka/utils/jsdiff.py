"""Line and word diffs and unified patches, translated from the npm ``diff`` package (jsdiff 8.0.4).

``diffLines`` and ``diffWords`` are jsdiff's Myers search (``diff/base.js``) over line tokens (``diff/line.js``) and over
words with their surrounding whitespace (``diff/word.js`` and the helpers of ``util/string.js``); ``structuredPatch``,
``formatPatch`` and ``createTwoFilesPatch`` are ``patch/create.js``. A diff is a list of ``{"count", "added", "removed",
"value"}`` parts and a structured patch is ``{"oldFileName", "newFileName", "oldHeader", "newHeader", "hunks"}`` with
``{"oldStart", "oldLines", "newStart", "newLines", "lines"}`` hunks. Only what pi uses is ported: the
``maxEditLength``, ``timeout``, ``ignoreWhitespace``, ``ignoreCase``, ``oneChangePerToken``, ``intlSegmenter``,
``stripTrailingCr`` and callback options are not.

jsdiff is Copyright (c) 2009-2015, Kevin Decker <kpdecker@gmail.com>, under the BSD 3-Clause License; its conditions and
disclaimer are ``LICENSES/jsdiff.txt`` (indexed in THIRD_PARTY_NOTICES.md).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from misaka.utils import js_regexp
from misaka.utils.js import jsTrim
from misaka.utils.js_regexp import RegExp

INCLUDE_HEADERS: dict[str, bool] = {"includeIndex": True, "includeUnderline": True, "includeFileHeaders": True}
FILE_HEADERS_ONLY: dict[str, bool] = {"includeIndex": False, "includeUnderline": False, "includeFileHeaders": True}
OMIT_HEADERS: dict[str, bool] = {"includeIndex": False, "includeUnderline": False, "includeFileHeaders": False}

_LINE_BREAK = re.compile(r"(\n|\r\n)")


class _Component:
    __slots__ = ("added", "count", "previousComponent", "removed", "value")

    def __init__(self, count: int, added: bool, removed: bool, previousComponent: _Component | None) -> None:
        self.count = count
        self.added = added
        self.removed = removed
        self.previousComponent = previousComponent
        self.value = ""


class _Path:
    __slots__ = ("lastComponent", "oldPos")

    def __init__(self, oldPos: int, lastComponent: _Component | None) -> None:
        self.oldPos = oldPos
        self.lastComponent = lastComponent


def tokenize(value: str) -> list[str]:
    """Lines with their line breaks attached; the empty token after a final line break is dropped."""
    linesAndNewlines = _LINE_BREAK.split(value)
    # Ignore the final empty token that occurs if the string ends with a new line
    if not linesAndNewlines[-1]:
        linesAndNewlines.pop()
    # Merge the content and line separators into single tokens
    retLines: list[str] = []
    for index, line in enumerate(linesAndNewlines):
        if index % 2:
            retLines[-1] += line
        else:
            retLines.append(line)
    return retLines


def diffLines(oldStr: str, newStr: str) -> list[dict[str, Any]]:
    """The line diff of two strings, as jsdiff's ``diffLines`` computes it."""
    oldTokens = [token for token in tokenize(oldStr) if token]
    newTokens = [token for token in tokenize(newStr) if token]
    return _diffTokens(oldTokens, newTokens)


def _equals(left: str, right: str) -> bool:
    return left == right


def _join(chars: list[str]) -> str:
    return "".join(chars)


def _diffTokens(
    oldTokens: list[str],
    newTokens: list[str],
    equals: Callable[[str, str], bool] = _equals,
    join: Callable[[list[str]], str] = _join,
) -> list[dict[str, Any]]:
    newLen, oldLen = len(newTokens), len(oldTokens)
    editLength = 1
    maxEditLength = newLen + oldLen
    bestPath: dict[int, _Path | None] = {0: _Path(-1, None)}

    # Seed editLength = 0, i.e. the content starts with the same values
    start = bestPath[0]
    assert start is not None
    newPos = _extractCommon(start, newTokens, oldTokens, 0, equals)
    if start.oldPos + 1 >= oldLen and newPos + 1 >= newLen:
        # Identity per the equality and tokenizer
        return _buildValues(start.lastComponent, newTokens, oldTokens, join)

    # Once we hit the right edge of the edit graph on some diagonal k, we can definitely reach the end of the edit
    # graph in no more than k edits, so there is no point considering any moves to diagonal k+1 any more. Similarly,
    # once we have reached the bottom of the edit graph, there is no point considering moves to lower diagonals.
    minDiagonalToConsider: float = -math.inf
    maxDiagonalToConsider: float = math.inf

    while editLength <= maxEditLength:
        diagonalPath = int(max(minDiagonalToConsider, -editLength))
        while diagonalPath <= min(maxDiagonalToConsider, editLength):
            removePath = bestPath.get(diagonalPath - 1)
            addPath = bestPath.get(diagonalPath + 1)
            if removePath is not None:
                # No one else is going to attempt to use this value, clear it
                bestPath[diagonalPath - 1] = None

            canAdd = False
            if addPath is not None:
                # what newPos will be after we do an insertion:
                addPathNewPos = addPath.oldPos - diagonalPath
                canAdd = 0 <= addPathNewPos < newLen

            canRemove = removePath is not None and removePath.oldPos + 1 < oldLen
            if not canAdd and not canRemove:
                # If this path is a terminal then prune
                bestPath[diagonalPath] = None
                diagonalPath += 2
                continue

            # Select the diagonal that we want to branch from. We select the prior path whose position in the old string
            # is the farthest from the origin and does not pass the bounds of the diff graph
            if not canRemove or (canAdd and removePath.oldPos < addPath.oldPos):  # type: ignore[union-attr]
                basePath = _addToPath(addPath, True, False, 0)  # type: ignore[arg-type]
            else:
                basePath = _addToPath(removePath, False, True, 1)  # type: ignore[arg-type]

            newPos = _extractCommon(basePath, newTokens, oldTokens, diagonalPath, equals)

            if basePath.oldPos + 1 >= oldLen and newPos + 1 >= newLen:
                # If we have hit the end of both strings, then we are done
                return _buildValues(basePath.lastComponent, newTokens, oldTokens, join)
            bestPath[diagonalPath] = basePath
            if basePath.oldPos + 1 >= oldLen:
                maxDiagonalToConsider = min(maxDiagonalToConsider, diagonalPath - 1)
            if newPos + 1 >= newLen:
                minDiagonalToConsider = max(minDiagonalToConsider, diagonalPath + 1)
            diagonalPath += 2
        editLength += 1
    raise RuntimeError("The diff search ended without reaching the end of both inputs")


def _addToPath(path: _Path, added: bool, removed: bool, oldPosInc: int) -> _Path:
    last = path.lastComponent
    if last is not None and last.added == added and last.removed == removed:
        return _Path(path.oldPos + oldPosInc, _Component(last.count + 1, added, removed, last.previousComponent))
    return _Path(path.oldPos + oldPosInc, _Component(1, added, removed, last))


def _extractCommon(
    basePath: _Path, newTokens: list[str], oldTokens: list[str], diagonalPath: int, equals: Callable[[str, str], bool]
) -> int:
    newLen, oldLen = len(newTokens), len(oldTokens)
    oldPos = basePath.oldPos
    newPos = oldPos - diagonalPath
    commonCount = 0
    while newPos + 1 < newLen and oldPos + 1 < oldLen and equals(oldTokens[oldPos + 1], newTokens[newPos + 1]):
        newPos += 1
        oldPos += 1
        commonCount += 1
    if commonCount:
        basePath.lastComponent = _Component(commonCount, False, False, basePath.lastComponent)
    basePath.oldPos = oldPos
    return newPos


def _buildValues(
    lastComponent: _Component | None,
    newTokens: list[str],
    oldTokens: list[str],
    join: Callable[[list[str]], str] = _join,
) -> list[dict[str, Any]]:
    # First we convert our linked list of components in reverse order to an array in the right order:
    components: list[_Component] = []
    while lastComponent is not None:
        components.append(lastComponent)
        lastComponent = lastComponent.previousComponent
    components.reverse()

    newPos = 0
    oldPos = 0
    for component in components:
        if not component.removed:
            component.value = join(newTokens[newPos : newPos + component.count])
            newPos += component.count
            # Common case
            if not component.added:
                oldPos += component.count
        else:
            component.value = join(oldTokens[oldPos : oldPos + component.count])
            oldPos += component.count
    return [
        {"count": component.count, "added": component.added, "removed": component.removed, "value": component.value}
        for component in components
    ]


# diff/word.js

# Based on https://en.wikipedia.org/wiki/Latin_script_in_Unicode
#
# Chars/ranges counted as "word" characters by this regex are as follows:
#
# + U+00AD  Soft hyphen
# + 00C0–00FF (letters with diacritics from the Latin-1 Supplement), except:
#   - U+00D7  × Multiplication sign
#   - U+00F7  ÷ Division sign
# + Latin Extended-A, 0100–017F
# + Latin Extended-B, 0180–024F
# + IPA Extensions, 0250–02AF
# + Spacing Modifier Letters, 02B0–02FF, except:
#   - U+02C7  ˇ &#711;  Caron
#   - U+02D8  ˘ &#728;  Breve
#   - U+02D9  ˙ &#729;  Dot Above
#   - U+02DA  ˚ &#730;  Ring Above
#   - U+02DB  ˛ &#731;  Ogonek
#   - U+02DC  ˜ &#732;  Small Tilde
#   - U+02DD  ˝ &#733;  Double Acute Accent
# + Latin Extended Additional, 1E00–1EFF
extendedWordChars = (
    "a-zA-Z0-9_\\u{AD}\\u{C0}-\\u{D6}\\u{D8}-\\u{F6}\\u{F8}-\\u{2C6}\\u{2C8}-\\u{2D7}\\u{2DE}-\\u{2FF}\\u{1E00}-\\u{1EFF}"
)
# Each token is one of the following:
# - A punctuation mark plus the surrounding whitespace
# - A word plus the surrounding whitespace
# - Pure whitespace (but only in the special case where the entire text
#   is just whitespace)
#
# This regex does NOT fully implement the tokenization rules described above.
# Instead, it gives runs of whitespace their own "token". The tokenize method
# then handles stitching whitespace tokens onto adjacent word or punctuation
# tokens.
tokenizeIncludingWhitespace = RegExp(f"[{extendedWordChars}]+|\\s+|[^{extendedWordChars}]", "ug")
_WHITESPACE = RegExp("\\s")
_LEADING_WHITESPACE = RegExp("^\\s+")
_LEADING_WHITESPACE_RUN = RegExp("^\\s*")


def _wordEquals(left: str, right: str) -> bool:
    return jsTrim(left) == jsTrim(right)


def _wordTokenize(value: str) -> list[str]:
    parts: list[str] = js_regexp.match(value, tokenizeIncludingWhitespace) or []
    tokens: list[str] = []
    prevPart: str | None = None
    for part in parts:
        if _WHITESPACE.test(part):
            if prevPart is None:
                tokens.append(part)
            else:
                tokens.append(tokens.pop() + part)
        elif prevPart is not None and _WHITESPACE.test(prevPart):
            if tokens[len(tokens) - 1] == prevPart:
                tokens.append(tokens.pop() + part)
            else:
                tokens.append(prevPart + part)
        else:
            tokens.append(part)
        prevPart = part
    return tokens


def _wordJoin(tokens: list[str]) -> str:
    # Tokens being joined here will always have appeared consecutively in the
    # same text, so we can simply strip off the leading whitespace from all the
    # tokens except the first (and except any whitespace-only tokens - but such
    # a token will always be the first and only token anyway) and then join them
    # and the whitespace around words and punctuation will end up correct.
    return "".join(token if i == 0 else js_regexp.replace(token, _LEADING_WHITESPACE, "") for i, token in enumerate(tokens))


def _wordPostProcess(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not changes:
        return changes
    lastKeep: dict[str, Any] | None = None
    # Change objects representing any insertion or deletion since the last
    # "keep" change object. There can be at most one of each.
    insertion: dict[str, Any] | None = None
    deletion: dict[str, Any] | None = None
    for change in changes:
        if change["added"]:
            insertion = change
        elif change["removed"]:
            deletion = change
        else:
            if insertion or deletion:  # May be false at start of text
                dedupeWhitespaceInChangeObjects(lastKeep, deletion, insertion, change)
            lastKeep = change
            insertion = None
            deletion = None
    if insertion or deletion:
        dedupeWhitespaceInChangeObjects(lastKeep, deletion, insertion, None)
    return changes


def diffWords(oldStr: str, newStr: str) -> list[dict[str, Any]]:
    """The word diff of two strings, as jsdiff's ``diffWords`` computes it."""
    oldTokens = [token for token in _wordTokenize(oldStr) if token]
    newTokens = [token for token in _wordTokenize(newStr) if token]
    return _wordPostProcess(_diffTokens(oldTokens, newTokens, _wordEquals, _wordJoin))


def dedupeWhitespaceInChangeObjects(
    startKeep: dict[str, Any] | None,
    deletion: dict[str, Any] | None,
    insertion: dict[str, Any] | None,
    endKeep: dict[str, Any] | None,
) -> None:
    """Before returning, tidy up the leading and trailing whitespace of the change objects to eliminate cases where
    trailing whitespace in one object is repeated as leading whitespace in the next (see jsdiff's examples)."""
    # Between two "keep" change objects (or before the first or after the last
    # change object), we can have either:
    # * A "delete" followed by an "insert"
    # * Just an "insert"
    # * Just a "delete"
    # We handle the three cases separately.
    if deletion and insertion:
        oldWsPrefix, oldWsSuffix = leadingAndTrailingWs(deletion["value"])
        newWsPrefix, newWsSuffix = leadingAndTrailingWs(insertion["value"])

        if startKeep:
            commonWsPrefix = longestCommonPrefix(oldWsPrefix, newWsPrefix)
            startKeep["value"] = replaceSuffix(startKeep["value"], newWsPrefix, commonWsPrefix)
            deletion["value"] = removePrefix(deletion["value"], commonWsPrefix)
            insertion["value"] = removePrefix(insertion["value"], commonWsPrefix)
        if endKeep:
            commonWsSuffix = longestCommonSuffix(oldWsSuffix, newWsSuffix)
            endKeep["value"] = replacePrefix(endKeep["value"], newWsSuffix, commonWsSuffix)
            deletion["value"] = removeSuffix(deletion["value"], commonWsSuffix)
            insertion["value"] = removeSuffix(insertion["value"], commonWsSuffix)
    elif insertion:
        # The whitespaces all reflect what was in the new text rather than
        # the old, so we essentially have no information about whitespace
        # insertion or deletion. We just want to dedupe the whitespace.
        # We do that by having each change object keep its trailing
        # whitespace and deleting duplicate leading whitespace where
        # present.
        if startKeep:
            ws = leadingWs(insertion["value"])
            insertion["value"] = insertion["value"][len(ws) :]
        if endKeep:
            ws = leadingWs(endKeep["value"])
            endKeep["value"] = endKeep["value"][len(ws) :]
        # otherwise we've got a deletion and no insertion
    elif startKeep and endKeep:
        assert deletion is not None
        newWsFull = leadingWs(endKeep["value"])
        delWsStart, delWsEnd = leadingAndTrailingWs(deletion["value"])

        # Any whitespace that comes straight after startKeep in both the old and
        # new texts, assign to startKeep and remove from the deletion.
        newWsStart = longestCommonPrefix(newWsFull, delWsStart)
        deletion["value"] = removePrefix(deletion["value"], newWsStart)

        # Any whitespace that comes straight before endKeep in both the old and
        # new texts, and hasn't already been assigned to startKeep, assign to
        # endKeep and remove from the deletion.
        newWsEnd = longestCommonSuffix(removePrefix(newWsFull, newWsStart), delWsEnd)
        deletion["value"] = removeSuffix(deletion["value"], newWsEnd)
        endKeep["value"] = replacePrefix(endKeep["value"], newWsFull, newWsEnd)

        # If there's any whitespace from the new text that HASN'T already been
        # assigned, assign it to the start:
        startKeep["value"] = replaceSuffix(startKeep["value"], newWsFull, newWsFull[0 : len(newWsFull) - len(newWsEnd)])
    elif endKeep:
        assert deletion is not None
        # We are at the start of the text. Preserve all the whitespace on
        # endKeep, and just remove whitespace from the end of deletion to the
        # extent that it overlaps with the start of endKeep.
        endKeepWsPrefix = leadingWs(endKeep["value"])
        deletionWsSuffix = trailingWs(deletion["value"])
        overlap = maximumOverlap(deletionWsSuffix, endKeepWsPrefix)
        deletion["value"] = removeSuffix(deletion["value"], overlap)
    elif startKeep:
        assert deletion is not None
        # We are at the END of the text. Preserve all the whitespace on
        # startKeep, and just remove whitespace from the start of deletion to
        # the extent that it overlaps with the end of startKeep.
        startKeepWsSuffix = trailingWs(startKeep["value"])
        deletionWsPrefix = leadingWs(deletion["value"])
        overlap = maximumOverlap(startKeepWsSuffix, deletionWsPrefix)
        deletion["value"] = removePrefix(deletion["value"], overlap)


# util/string.js


def longestCommonPrefix(str1: str, str2: str) -> str:
    i = 0
    while i < len(str1) and i < len(str2):
        if str1[i] != str2[i]:
            return str1[0:i]
        i += 1
    return str1[0:i]


def longestCommonSuffix(str1: str, str2: str) -> str:
    # Unlike longestCommonPrefix, we need a special case to handle all scenarios
    # where we return the empty string since str1.slice(-0) will return the
    # entire string.
    if not str1 or not str2 or str1[len(str1) - 1] != str2[len(str2) - 1]:
        return ""
    i = 0
    while i < len(str1) and i < len(str2):
        if str1[len(str1) - (i + 1)] != str2[len(str2) - (i + 1)]:
            return str1[-i:]
        i += 1
    return str1[-i:]


def replacePrefix(string: str, oldPrefix: str, newPrefix: str) -> str:
    if string[0 : len(oldPrefix)] != oldPrefix:
        raise ValueError(f"string {string!r} doesn't start with prefix {oldPrefix!r}; this is a bug")
    return newPrefix + string[len(oldPrefix) :]


def replaceSuffix(string: str, oldSuffix: str, newSuffix: str) -> str:
    if not oldSuffix:
        return string + newSuffix
    if string[-len(oldSuffix) :] != oldSuffix:
        raise ValueError(f"string {string!r} doesn't end with suffix {oldSuffix!r}; this is a bug")
    return string[0 : -len(oldSuffix)] + newSuffix


def removePrefix(string: str, oldPrefix: str) -> str:
    return replacePrefix(string, oldPrefix, "")


def removeSuffix(string: str, oldSuffix: str) -> str:
    return replaceSuffix(string, oldSuffix, "")


def maximumOverlap(string1: str, string2: str) -> str:
    return string2[0 : overlapCount(string1, string2)]


def overlapCount(a: str, b: str) -> int:
    """Nicked from https://stackoverflow.com/a/60422853/1709587"""
    # Deal with cases where the strings differ in length
    startA = 0
    if len(a) > len(b):
        startA = len(a) - len(b)
    endB = len(b)
    if len(a) < len(b):
        endB = len(a)
    # Create a back-reference for each index
    #   that should be followed in case of a mismatch.
    #   We only need B to make these references:
    map = [0] * max(endB, 1)
    k = 0  # Index that lags behind j
    map[0] = 0
    for j in range(1, endB):
        if b[j] == b[k]:
            map[j] = map[k]  # skip over the same character (optional optimisation)
        else:
            map[j] = k
        while k > 0 and b[j] != b[k]:
            k = map[k]
        if b[j] == b[k]:
            k += 1
    # Phase 2: use these references while iterating over A
    k = 0
    for i in range(startA, len(a)):
        while k > 0 and a[i] != b[k]:
            k = map[k]
        if k < len(b) and a[i] == b[k]:
            k += 1
    return k


def trailingWs(string: str) -> str:
    i = len(string) - 1
    while i >= 0:
        if not _WHITESPACE.test(string[i]):
            break
        i -= 1
    return string[i + 1 :]


def leadingWs(string: str) -> str:
    match = js_regexp.match(string, _LEADING_WHITESPACE_RUN)
    return match[0] if match else ""


def leadingAndTrailingWs(string: str) -> tuple[str, str]:
    return leadingWs(string), trailingWs(string)


def _splitLines(text: str) -> list[str]:
    """Split ``text`` into an array of lines, including the trailing newline character (where present)."""
    hasTrailingNl = text.endswith("\n")
    result = [line + "\n" for line in text.split("\n")]
    if hasTrailingNl:
        result.pop()
    else:
        result.append(result.pop()[:-1])
    return result


def structuredPatch(
    oldFileName: str,
    newFileName: str,
    oldStr: str,
    newStr: str,
    oldHeader: str | None = None,
    newHeader: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    options = options if options is not None else {}
    context = options["context"] if options.get("context") is not None else 4
    if options.get("newlineIsToken"):
        raise RuntimeError("newlineIsToken may not be used with patch-generation functions, only with diffing functions")

    diff = diffLines(oldStr, newStr)
    # STEP 1: Build up the patch with no "\ No newline at end of file" lines and with the arrays of lines containing
    #         trailing newline characters. We'll tidy up later...
    diff.append({"value": "", "lines": []})  # Append an empty value to make cleanup easier

    def contextLines(lines: list[str]) -> list[str]:
        return [" " + entry for entry in lines]

    hunks: list[dict[str, Any]] = []
    oldRangeStart = 0
    newRangeStart = 0
    curRange: list[str] = []
    oldLine = 1
    newLine = 1
    for i, current in enumerate(diff):
        lines = current["lines"] if current.get("lines") is not None else _splitLines(current["value"])
        current["lines"] = lines

        if current.get("added") or current.get("removed"):
            # If we have previous context, start with that
            if not oldRangeStart:
                prev = diff[i - 1] if i - 1 >= 0 else None
                oldRangeStart = oldLine
                newRangeStart = newLine

                if prev is not None:
                    curRange = contextLines(prev["lines"][-context:]) if context > 0 else []
                    oldRangeStart -= len(curRange)
                    newRangeStart -= len(curRange)

            # Output our changes
            for line in lines:
                curRange.append(("+" if current["added"] else "-") + line)

            # Track the updated file position
            if current["added"]:
                newLine += len(lines)
            else:
                oldLine += len(lines)
        else:
            # Identical context lines. Track line changes
            if oldRangeStart:
                # Close out any changes that have been output (or join overlapping)
                if len(lines) <= context * 2 and i < len(diff) - 2:
                    # Overlapping
                    curRange.extend(contextLines(lines))
                else:
                    # end the range and output
                    contextSize = min(len(lines), context)
                    curRange.extend(contextLines(lines[:contextSize]))

                    hunks.append(
                        {
                            "oldStart": oldRangeStart,
                            "oldLines": oldLine - oldRangeStart + contextSize,
                            "newStart": newRangeStart,
                            "newLines": newLine - newRangeStart + contextSize,
                            "lines": curRange,
                        }
                    )
                    oldRangeStart = 0
                    newRangeStart = 0
                    curRange = []
            oldLine += len(lines)
            newLine += len(lines)

    # Step 2: eliminate the trailing ``\n`` from each line of each hunk, and, where needed, add
    #         "\ No newline at end of file".
    for hunk in hunks:
        index = 0
        while index < len(hunk["lines"]):
            if hunk["lines"][index].endswith("\n"):
                hunk["lines"][index] = hunk["lines"][index][:-1]
            else:
                hunk["lines"].insert(index + 1, "\\ No newline at end of file")
                index += 1  # Skip the line we just added, then continue iterating
            index += 1

    return {
        "oldFileName": oldFileName,
        "newFileName": newFileName,
        "oldHeader": oldHeader,
        "newHeader": newHeader,
        "hunks": hunks,
    }


def formatPatch(patch: dict[str, Any], headerOptions: dict[str, bool] | None = None) -> str:
    """Create a unified diff patch from a structured patch."""
    if headerOptions is None:
        headerOptions = INCLUDE_HEADERS
    ret: list[str] = []
    if headerOptions["includeIndex"] and patch["oldFileName"] == patch["newFileName"]:
        ret.append("Index: " + patch["oldFileName"])
    if headerOptions["includeUnderline"]:
        ret.append("===================================================================")
    if headerOptions["includeFileHeaders"]:
        ret.append("--- " + patch["oldFileName"] + ("" if patch.get("oldHeader") is None else "\t" + patch["oldHeader"]))
        ret.append("+++ " + patch["newFileName"] + ("" if patch.get("newHeader") is None else "\t" + patch["newHeader"]))
    for hunk in patch["hunks"]:
        # Unified Diff Format quirk: If the chunk size is 0, the first number is one lower than one would expect.
        if hunk["oldLines"] == 0:
            hunk["oldStart"] -= 1
        if hunk["newLines"] == 0:
            hunk["newStart"] -= 1
        ret.append(f"@@ -{hunk['oldStart']},{hunk['oldLines']} +{hunk['newStart']},{hunk['newLines']} @@")
        ret.extend(hunk["lines"])
    return "\n".join(ret) + "\n"


def createTwoFilesPatch(
    oldFileName: str,
    newFileName: str,
    oldStr: str,
    newStr: str,
    oldHeader: str | None = None,
    newHeader: str | None = None,
    options: dict[str, Any] | None = None,
) -> str:
    patchObj = structuredPatch(oldFileName, newFileName, oldStr, newStr, oldHeader, newHeader, options)
    return formatPatch(patchObj, None if options is None else options.get("headerOptions"))


__all__ = [
    "FILE_HEADERS_ONLY",
    "INCLUDE_HEADERS",
    "OMIT_HEADERS",
    "createTwoFilesPatch",
    "diffLines",
    "formatPatch",
    "structuredPatch",
    "tokenize",
]

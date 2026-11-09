"""Translated from pi's ``packages/coding-agent/src/core/tools/edit-diff.ts`` (v1.1.0).

Shared diff computation utilities for the edit and similar tools.

MISAKA fork: a fuzzy edit keeps the original spelling of every byte outside its matched spans, where pi rewrites
each touched line from the normalized text (``applyReplacementsPreservingUnchangedLines``, still exported). An
exact match counts its occurrences in the content's own coordinates, a whitespace-only oldText counts as empty, and
a file that is not UTF-8 is refused in the preview as the edit itself refuses it.
"""

from __future__ import annotations

import asyncio
import bisect
import errno
import re
import unicodedata
from dataclasses import dataclass

from misaka.core.tools._common import regular_file, whole_file_bytes
from misaka.core.tools.path_utils import resolveToCwd
from misaka.utils.js import jsTrimEnd
from misaka.utils.jsdiff import FILE_HEADERS_ONLY, createTwoFilesPatch, diffLines
from misaka.utils.text import splitBom


def detectLineEnding(content: str) -> str:
    crlfIdx = content.find("\r\n")
    lfIdx = content.find("\n")
    if lfIdx == -1:
        return "\n"
    if crlfIdx == -1:
        return "\n"
    return "\r\n" if crlfIdx < lfIdx else "\n"


def normalizeToLF(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restoreLineEndings(text: str, ending: str) -> str:
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


_FUZZY_CHAR_MAP = str.maketrans({
    # Smart single quotes → '
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    # Smart double quotes → "
    "“": '"', "”": '"', "„": '"', "‟": '"',
    # Various dashes/hyphens → -
    # U+2010 hyphen, U+2011 non-breaking hyphen, U+2012 figure dash,
    # U+2013 en-dash, U+2014 em-dash, U+2015 horizontal bar, U+2212 minus
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    # Special spaces → regular space
    # U+00A0 NBSP, U+2002-U+200A various spaces, U+202F narrow NBSP,
    # U+205F medium math space, U+3000 ideographic space
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", "　": " ",
})


def normalizeForFuzzyMatch(text: str) -> str:
    """Normalize text for fuzzy matching. Applies progressive transformations:
    - Strip trailing whitespace from each line
    - Normalize smart quotes to ASCII equivalents
    - Normalize Unicode dashes/hyphens to ASCII hyphen
    - Normalize special Unicode spaces to regular space
    """
    return (
        "\n".join(
            # Strip trailing whitespace per line
            jsTrimEnd(line) for line in unicodedata.normalize("NFKC", text).split("\n")
        ).translate(_FUZZY_CHAR_MAP)
    )


def splitLinesWithEndings(content: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+", content)


@dataclass(slots=True)
class LineSpan:
    start: int
    end: int


@dataclass(slots=True)
class MatchedEdit:
    editIndex: int
    matchIndex: int
    matchLength: int
    newText: str


type TextReplacement = MatchedEdit


def getLineSpans(content: str) -> list[LineSpan]:
    spans: list[LineSpan] = []
    offset = 0
    for line in splitLinesWithEndings(content):
        span = LineSpan(start=offset, end=offset + len(line))
        offset = span.end
        spans.append(span)
    return spans


def getReplacementLineRange(lines: list[LineSpan], replacement: TextReplacement) -> tuple[int, int]:
    replacementStart = replacement.matchIndex
    replacementEnd = replacement.matchIndex + replacement.matchLength

    startLine = -1
    for i, line in enumerate(lines):
        if replacementStart >= line.start and replacementStart < line.end:
            startLine = i
            break
    if startLine == -1:
        raise RuntimeError("Replacement range is outside the base content.")

    endLine = startLine
    while endLine < len(lines) and lines[endLine].end < replacementEnd:
        endLine += 1
    if endLine >= len(lines):
        raise RuntimeError("Replacement range is outside the base content.")

    return startLine, endLine + 1


def applyReplacements(content: str, replacements: list[TextReplacement], offset: int = 0) -> str:
    result = content
    for replacement in reversed(replacements):
        matchIndex = replacement.matchIndex - offset
        result = result[:matchIndex] + replacement.newText + result[matchIndex + replacement.matchLength :]
    return result


def applyReplacementsPreservingUnchangedLines(
    originalContent: str,
    baseContent: str,
    replacements: list[TextReplacement],
) -> str:
    """Apply replacements matched against `baseContent` to `originalContent` while
    preserving unchanged line blocks from the original.

    This is useful when `baseContent` is a normalized view of the original. Each
    replacement is widened to the lines it actually touches, those touched lines
    are rewritten from the normalized base, and all other lines are copied back
    from `originalContent`. The actual replacement ranges drive preservation so
    duplicate normalized lines cannot be aligned to the wrong occurrence.
    """
    originalLines = splitLinesWithEndings(originalContent)
    baseLines = getLineSpans(baseContent)
    if len(originalLines) != len(baseLines):
        raise RuntimeError("Cannot preserve unchanged lines because the base content has a different line count.")

    groups: list[dict[str, object]] = []
    sortedReplacements = sorted(replacements, key=lambda replacement: replacement.matchIndex)
    for replacement in sortedReplacements:
        startLine, endLine = getReplacementLineRange(baseLines, replacement)
        current = groups[-1] if groups else None
        if current and startLine < current["endLine"]:  # type: ignore[operator]
            current["endLine"] = max(current["endLine"], endLine)  # type: ignore[call-overload]
            current["replacements"].append(replacement)  # type: ignore[attr-defined]
            continue
        groups.append({"startLine": startLine, "endLine": endLine, "replacements": [replacement]})

    originalLineIndex = 0
    result = ""
    for group in groups:
        groupStart, groupEnd = int(group["startLine"]), int(group["endLine"])  # type: ignore[call-overload]
        result += "".join(originalLines[originalLineIndex:groupStart])

        groupStartOffset = baseLines[groupStart].start
        groupEndOffset = baseLines[groupEnd - 1].end
        result += applyReplacements(
            baseContent[groupStartOffset:groupEndOffset],
            group["replacements"],  # type: ignore[arg-type]
            groupStartOffset,
        )
        originalLineIndex = groupEnd
    result += "".join(originalLines[originalLineIndex:])

    return result


# MISAKA fork: the lossless splice that replaces applyReplacementsPreservingUnchangedLines for fuzzy edits.


def _normFrag(text: str) -> str:
    """``normalizeForFuzzyMatch`` for an intra-line fragment: no per-line trimEnd."""
    return unicodedata.normalize("NFKC", text).translate(_FUZZY_CHAR_MAP)


# Beyond this length, recovery only accepts the same-index fast path; the fallback
# per-column search is quadratic in the line length.
_RECOVERY_SCAN_LIMIT = 5000


def _origCol(origLine: str, normLine: str, col: int) -> int | None:
    """Index into ``origLine`` whose prefix normalizes to ``normLine[:col]``, or None.

    NFKC is not prefix-stable (composition can merge characters at a cut point), so this
    scans candidate cut points instead of mapping arithmetically. Newlines never interact
    across NFKC, which is what makes the per-line search sound.
    """
    if col == 0:
        return 0
    if col > len(normLine):
        return None
    target = normLine[:col]
    # Most candidate boundaries stay at the same index even when a same-width
    # character (for example a curly quote) was normalized. Try that cut first so
    # long lines do not need the quadratic fallback scan.
    if col <= len(origLine) and _normFrag(origLine[:col]) == target:
        return col
    if len(origLine) > _RECOVERY_SCAN_LIMIT:
        return None
    for cut in range(1, len(origLine) + 1):
        if _normFrag(origLine[:cut]) == target:
            return cut
    return None


def _endCut(origLine: str, normLine: str, col: int) -> int | None:
    """``_origCol`` for a boundary that *ends* a gap.

    ``normalizeForFuzzyMatch`` trims every line's end, so a gap ending at the end of a
    normalized line ends, in original coordinates, at the end of the *original* line --
    trailing whitespace included. Asking ``_origCol`` for that column returns the
    trimmed index instead, which silently drops those bytes even though they sit
    outside the requested edit. The mirror case (a gap that *starts* there) is already
    right: ``origLine[cutA:]`` runs to the real end of the line.
    """
    if col == len(normLine):
        return len(origLine)
    return _origCol(origLine, normLine, col)


def _recoverGap(
    origLines: list[str],
    normLines: list[str],
    start: tuple[int, int],
    end: tuple[int, int],
) -> str | None:
    """The original spelling of the normalized-content span ``start``..``end`` (line, col).

    Whole lines inside the span are returned verbatim from the original. A boundary that
    cuts a line mid-way is recovered through ``_origCol``; ``None`` means the normalized
    boundary has no proven lossless mapping back to the original line.
    """
    if start == end:
        return ""
    (lineA, colA), (lineB, colB) = start, end
    if lineA == lineB:
        cutA = _origCol(origLines[lineA], normLines[lineA], colA)
        cutB = _endCut(origLines[lineA], normLines[lineA], colB)
        if cutA is not None and cutB is not None and cutA <= cutB:
            return origLines[lineA][cutA:cutB]
        return None
    cutA = _origCol(origLines[lineA], normLines[lineA], colA)
    cutB = _endCut(origLines[lineB], normLines[lineB], colB)
    if cutA is None or cutB is None:
        return None
    head = origLines[lineA][cutA:]
    tail = origLines[lineB][:cutB]
    return "\n".join([head, *origLines[lineA + 1 : lineB], tail])


def _spliceEditsPreservingOriginal(
    normalizedContent: str,
    baseContent: str,
    matchedEdits: list[MatchedEdit],
) -> str | None:
    """Apply ``matchedEdits`` (spans in ``baseContent`` coordinates) onto the original
    ``normalizedContent``. Returns None when line structure or a span boundary cannot
    be mapped back without rewriting text outside the requested edits."""
    origLines = normalizedContent.split("\n")
    normLines = baseContent.split("\n")
    if len(origLines) != len(normLines):
        return None

    lineStarts = [0]
    for line in normLines:
        lineStarts.append(lineStarts[-1] + len(line) + 1)

    def locate(index: int) -> tuple[int, int]:
        lineNumber = bisect.bisect_right(lineStarts, index) - 1
        return lineNumber, index - lineStarts[lineNumber]

    pieces: list[str] = []
    cursor = 0
    for matched in matchedEdits:
        gap = _recoverGap(origLines, normLines, locate(cursor), locate(matched.matchIndex))
        if gap is None:
            return None
        pieces.append(gap)
        pieces.append(matched.newText)
        cursor = matched.matchIndex + matched.matchLength
    # The final gap runs to the end of the file: take the original tail verbatim. Mapping
    # only the start avoids locate(len(baseContent)) landing before the last line's
    # trailing whitespace when the file has no final newline (the last line is trimmed
    # in normalized coordinates, so an end-mapped cut would drop that whitespace).
    tailLine, tailCol = locate(cursor)
    cut = _origCol(origLines[tailLine], normLines[tailLine], tailCol)
    if cut is None:
        return None
    head = origLines[tailLine][cut:]
    pieces.append("\n".join([head, *origLines[tailLine + 1 :]]))
    return "".join(pieces)


@dataclass(slots=True)
class FuzzyMatchResult:
    # Whether a match was found
    found: bool
    # The index where the match starts (in the content that should be used for replacement)
    index: int
    # Length of the matched text
    matchLength: int
    # Whether fuzzy matching was used (false = exact match)
    usedFuzzyMatch: bool
    # The content to use for replacement operations.
    # When exact match: original content. When fuzzy match: normalized content.
    contentForReplacement: str


@dataclass(slots=True)
class Edit:
    oldText: str
    newText: str


@dataclass(slots=True)
class AppliedEditsResult:
    baseContent: str
    newContent: str


def fuzzyFindText(content: str, oldText: str) -> FuzzyMatchResult:
    """Find oldText in content, trying exact match first, then fuzzy match.
    When fuzzy matching is used, the returned contentForReplacement is the
    fuzzy-normalized version of the content (trailing whitespace stripped,
    Unicode quotes/dashes normalized to ASCII).
    """
    # Try exact match first
    exactIndex = content.find(oldText)
    if exactIndex != -1:
        return FuzzyMatchResult(
            found=True,
            index=exactIndex,
            matchLength=len(oldText),
            usedFuzzyMatch=False,
            contentForReplacement=content,
        )

    # Try fuzzy match - work entirely in normalized space
    fuzzyContent = normalizeForFuzzyMatch(content)
    fuzzyOldText = normalizeForFuzzyMatch(oldText)
    fuzzyIndex = fuzzyContent.find(fuzzyOldText)

    if fuzzyIndex == -1:
        return FuzzyMatchResult(
            found=False,
            index=-1,
            matchLength=0,
            usedFuzzyMatch=False,
            contentForReplacement=content,
        )

    # When fuzzy matching, return offsets in normalized space. Callers can use
    # the normalized content to compute replacements, then decide how much of
    # that normalized output should be written back.
    return FuzzyMatchResult(
        found=True,
        index=fuzzyIndex,
        matchLength=len(fuzzyOldText),
        usedFuzzyMatch=True,
        contentForReplacement=fuzzyContent,
    )


def countOccurrences(content: str, oldText: str, *, usedFuzzyMatch: bool) -> int:
    """MISAKA fork: pi always counts in normalized coordinates. An exact match is unique or not in the content's own
    coordinates; folding both sides first would refuse a uniquely located oldText because an unrelated line happens to
    normalize to the same text (``x—y`` next to ``x-y``, curly next to straight quotes). Only a fuzzy match is
    genuinely ambiguous in normalized coordinates.
    """
    if not usedFuzzyMatch:
        return content.count(oldText)
    fuzzyContent = normalizeForFuzzyMatch(content)
    fuzzyOldText = normalizeForFuzzyMatch(oldText)
    return len(fuzzyContent.split(fuzzyOldText)) - 1


def getNotFoundError(path: str, editIndex: int, totalEdits: int) -> RuntimeError:
    if totalEdits == 1:
        return RuntimeError(
            f"Could not find the exact text in {path}. The old text must match exactly including all whitespace and newlines."
        )
    return RuntimeError(
        f"Could not find edits[{editIndex}] in {path}. The oldText must match exactly including all whitespace and newlines."
    )


def getDuplicateError(path: str, editIndex: int, totalEdits: int, occurrences: int) -> RuntimeError:
    if totalEdits == 1:
        return RuntimeError(
            f"Found {occurrences} occurrences of the text in {path}. The text must be unique. Please provide more context to make it unique."
        )
    return RuntimeError(
        f"Found {occurrences} occurrences of edits[{editIndex}] in {path}. Each oldText must be unique. Please provide more context to make it unique."
    )


def getEmptyOldTextError(path: str, editIndex: int, totalEdits: int) -> RuntimeError:
    if totalEdits == 1:
        return RuntimeError(f"oldText must not be empty in {path}.")
    return RuntimeError(f"edits[{editIndex}].oldText must not be empty in {path}.")


def getNoChangeError(path: str, totalEdits: int) -> RuntimeError:
    if totalEdits == 1:
        return RuntimeError(
            f"No changes made to {path}. The replacement produced identical content. This might indicate an issue with special characters or the text not existing as expected."
        )
    return RuntimeError(f"No changes made to {path}. The replacements produced identical content.")


def _getFuzzyMappingError(path: str) -> RuntimeError:
    """MISAKA fork: a fuzzy edit whose spans cannot be mapped back to the original spelling."""
    return RuntimeError(
        f"Fuzzy edit in {path} has no proven lossless mapping to the original text. "
        "Use oldText copied exactly from the file."
    )


def applyEditsToNormalizedContent(
    normalizedContent: str,
    edits: list[Edit | dict[str, str]],
    path: str,
) -> AppliedEditsResult:
    """Apply one or more exact-text replacements to LF-normalized content.

    All edits are matched against the same original content. Replacements are
    then applied in reverse order so offsets remain stable. If any edit needs
    fuzzy matching, the operation runs in fuzzy-normalized content space and then
    overlays those line-level changes onto the original content so unchanged line
    blocks keep their original bytes.
    """
    normalizedEdits = [
        Edit(
            oldText=normalizeToLF(edit.oldText if isinstance(edit, Edit) else edit["oldText"]),
            newText=normalizeToLF(edit.newText if isinstance(edit, Edit) else edit["newText"]),
        )
        for edit in edits
    ]

    for i, edit in enumerate(normalizedEdits):
        # MISAKA fork: empty *after* normalization, not just literally empty: matching folds each line's
        # trailing whitespace away, so a whitespace-only oldText would match at offset 0 with
        # length 0 and splice newText in as an insertion reported as a replacement.
        if normalizeForFuzzyMatch(edit.oldText) == "":
            raise getEmptyOldTextError(path, i, len(normalizedEdits))

    initialMatches = [fuzzyFindText(normalizedContent, edit.oldText) for edit in normalizedEdits]
    usedFuzzyMatch = any(match.usedFuzzyMatch for match in initialMatches)
    replacementBaseContent = normalizeForFuzzyMatch(normalizedContent) if usedFuzzyMatch else normalizedContent

    matchedEdits: list[MatchedEdit] = []
    for i, edit in enumerate(normalizedEdits):
        matchResult = fuzzyFindText(replacementBaseContent, edit.oldText)
        if not matchResult.found:
            raise getNotFoundError(path, i, len(normalizedEdits))

        occurrences = countOccurrences(replacementBaseContent, edit.oldText, usedFuzzyMatch=matchResult.usedFuzzyMatch)
        if occurrences > 1:
            raise getDuplicateError(path, i, len(normalizedEdits), occurrences)

        matchedEdits.append(
            MatchedEdit(
                editIndex=i,
                matchIndex=matchResult.index,
                matchLength=matchResult.matchLength,
                newText=edit.newText,
            )
        )

    matchedEdits.sort(key=lambda matched: matched.matchIndex)
    for i in range(1, len(matchedEdits)):
        previous = matchedEdits[i - 1]
        current = matchedEdits[i]
        if previous.matchIndex + previous.matchLength > current.matchIndex:
            raise RuntimeError(
                f"edits[{previous.editIndex}] and edits[{current.editIndex}] overlap in {path}. Merge them into one edit or target disjoint regions."
            )

    baseContent = normalizedContent
    if usedFuzzyMatch:
        # MISAKA fork (pi: applyReplacementsPreservingUnchangedLines rewrites every touched line from the normalized
        # base). Fuzzy coordinates, original spelling: matches were found in the normalized text, but
        # the file must not be rewritten in normalized form — that silently NFKC-folds every
        # untouched line (curly quotes, full-width characters, ligatures), which for a corpus
        # of humanities sources is data corruption, not normalization. Splice each newText at
        # its matched span and recover every gap between spans from the original content.
        def isNoop(matched: MatchedEdit) -> bool:
            edit = normalizedEdits[matched.editIndex]
            if edit.newText == edit.oldText:
                return True
            spanEnd = matched.matchIndex + matched.matchLength
            return (
                initialMatches[matched.editIndex].usedFuzzyMatch
                and matched.newText == replacementBaseContent[matched.matchIndex:spanEnd]
            )

        effectiveEdits = [matched for matched in matchedEdits if not isNoop(matched)]
        if not effectiveEdits:
            raise getNoChangeError(path, len(normalizedEdits))
        newContent = _spliceEditsPreservingOriginal(normalizedContent, replacementBaseContent, effectiveEdits)
        if newContent is None:
            # Falling back to normalized text would silently rewrite content outside the
            # requested span. Reject the fuzzy edit before the caller reaches writeFile.
            raise _getFuzzyMappingError(path)
    else:
        newContent = applyReplacements(replacementBaseContent, matchedEdits)

    if baseContent == newContent:
        raise getNoChangeError(path, len(normalizedEdits))

    return AppliedEditsResult(baseContent=baseContent, newContent=newContent)


def generateUnifiedPatch(path: str, oldContent: str, newContent: str, contextLines: int = 4) -> str:
    """Generate a standard unified patch."""
    return createTwoFilesPatch(
        path, path, oldContent, newContent, None, None, {"context": contextLines, "headerOptions": FILE_HEADERS_ONLY}
    )


def generateDiffString(oldContent: str, newContent: str, contextLines: int = 4) -> EditDiffResult:
    """Generate a display-oriented diff string with line numbers and context.
    Returns both the diff string and the first changed line number (in the new file).
    """
    parts = diffLines(oldContent, newContent)
    output: list[str] = []

    oldLines = oldContent.split("\n")
    newLines = newContent.split("\n")
    maxLineNum = max(len(oldLines), len(newLines))
    lineNumWidth = len(str(maxLineNum))

    oldLineNum = 1
    newLineNum = 1
    lastWasChange = False
    firstChangedLine: int | None = None

    for i, part in enumerate(parts):
        raw = part["value"].split("\n")
        if raw[-1] == "":
            raw.pop()

        if part.get("added") or part.get("removed"):
            # Capture the first changed line (in the new file)
            if firstChangedLine is None:
                firstChangedLine = newLineNum

            # Show the change
            for line in raw:
                if part.get("added"):
                    lineNum = str(newLineNum).rjust(lineNumWidth, " ")
                    output.append(f"+{lineNum} {line}")
                    newLineNum += 1
                else:
                    # removed
                    lineNum = str(oldLineNum).rjust(lineNumWidth, " ")
                    output.append(f"-{lineNum} {line}")
                    oldLineNum += 1
            lastWasChange = True
        else:
            # Context lines - only show a few before/after changes
            nextPartIsChange = i < len(parts) - 1 and bool(parts[i + 1].get("added") or parts[i + 1].get("removed"))
            hasLeadingChange = lastWasChange
            hasTrailingChange = nextPartIsChange

            if hasLeadingChange and hasTrailingChange:
                if len(raw) <= contextLines * 2:
                    for line in raw:
                        lineNum = str(oldLineNum).rjust(lineNumWidth, " ")
                        output.append(f" {lineNum} {line}")
                        oldLineNum += 1
                        newLineNum += 1
                else:
                    leadingLines = raw[0:contextLines]
                    trailingLines = raw[len(raw) - contextLines :]
                    skippedLines = len(raw) - len(leadingLines) - len(trailingLines)

                    for line in leadingLines:
                        lineNum = str(oldLineNum).rjust(lineNumWidth, " ")
                        output.append(f" {lineNum} {line}")
                        oldLineNum += 1
                        newLineNum += 1

                    output.append(f" {''.rjust(lineNumWidth, ' ')} ...")
                    oldLineNum += skippedLines
                    newLineNum += skippedLines

                    for line in trailingLines:
                        lineNum = str(oldLineNum).rjust(lineNumWidth, " ")
                        output.append(f" {lineNum} {line}")
                        oldLineNum += 1
                        newLineNum += 1
            elif hasLeadingChange:
                shownLines = raw[0:contextLines]
                skippedLines = len(raw) - len(shownLines)

                for line in shownLines:
                    lineNum = str(oldLineNum).rjust(lineNumWidth, " ")
                    output.append(f" {lineNum} {line}")
                    oldLineNum += 1
                    newLineNum += 1

                if skippedLines > 0:
                    output.append(f" {''.rjust(lineNumWidth, ' ')} ...")
                    oldLineNum += skippedLines
                    newLineNum += skippedLines
            elif hasTrailingChange:
                skippedLines = max(0, len(raw) - contextLines)
                if skippedLines > 0:
                    output.append(f" {''.rjust(lineNumWidth, ' ')} ...")
                    oldLineNum += skippedLines
                    newLineNum += skippedLines

                for line in raw[skippedLines:]:
                    lineNum = str(oldLineNum).rjust(lineNumWidth, " ")
                    output.append(f" {lineNum} {line}")
                    oldLineNum += 1
                    newLineNum += 1
            else:
                # Skip these context lines entirely
                oldLineNum += len(raw)
                newLineNum += len(raw)

            lastWasChange = False

    return EditDiffResult(diff="\n".join(output), firstChangedLine=firstChangedLine)


@dataclass(slots=True)
class EditDiffResult:
    diff: str
    firstChangedLine: int | None


@dataclass(slots=True)
class EditDiffError:
    error: str


async def computeEditsDiff(path: str, edits: list[Edit | dict[str, str]], cwd: str) -> EditDiffResult | EditDiffError:
    """Compute the diff for one or more edit operations without applying them.
    Used for preview rendering in the TUI before the tool executes.
    """
    absolutePath = resolveToCwd(path, cwd)

    try:
        # Check if file exists and is readable
        try:
            await asyncio.to_thread(regular_file, absolutePath)
        except Exception as error:  # noqa: BLE001 - any access failure becomes the user-facing edit error
            code = errno.errorcode.get(error.errno) if isinstance(error, OSError) and error.errno else None
            errorMessage = f"Error code: {code}" if code else f"Error: {error}"
            return EditDiffError(error=f"Could not edit file: {path}. {errorMessage}.")

        # Read the file
        buffer = await asyncio.to_thread(whole_file_bytes, absolutePath, "edit")
        try:
            rawContent = buffer.decode("utf-8")
        except UnicodeDecodeError as error:
            # MISAKA fork: the edit refuses a file that is not UTF-8, so its preview does too.
            return EditDiffError(
                error=f"Could not edit file: {path}. It is not valid UTF-8 text ({error}); "
                "editing it as text would corrupt it. Use bash for binary files."
            )

        # Strip BOM before matching (LLM won't include invisible BOM in oldText)
        content = splitBom(rawContent)["text"]
        normalizedContent = normalizeToLF(content)
        applied = applyEditsToNormalizedContent(normalizedContent, edits, path)

        # Generate the diff
        return generateDiffString(applied.baseContent, applied.newContent)
    except Exception as err:  # noqa: BLE001 - any diff failure is returned as EditDiffError
        return EditDiffError(error=str(err))


async def computeEditDiff(path: str, oldText: str, newText: str, cwd: str) -> EditDiffResult | EditDiffError:
    """Compute the diff for a single edit operation without applying it.
    Kept as a convenience wrapper for single-edit callers.
    """
    return await computeEditsDiff(path, [Edit(oldText=oldText, newText=newText)], cwd)


__all__ = [
    "AppliedEditsResult",
    "Edit",
    "EditDiffError",
    "EditDiffResult",
    "FuzzyMatchResult",
    "applyEditsToNormalizedContent",
    "applyReplacementsPreservingUnchangedLines",
    "computeEditDiff",
    "computeEditsDiff",
    "detectLineEnding",
    "fuzzyFindText",
    "generateDiffString",
    "generateUnifiedPatch",
    "normalizeForFuzzyMatch",
    "normalizeToLF",
    "restoreLineEndings",
]

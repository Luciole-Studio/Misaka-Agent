"""Shared diff computation utilities for the edit and similar tools.

Translated from pi's ``packages/durable/src/tools/edit-diff.ts``. The ``diff`` package is ``misaka.utils.jsdiff`` (a port
of jsdiff 8.0.4). An edit is ``{"oldText", "newText"}``, a fuzzy match result is ``{"found", "index", "matchLength",
"usedFuzzyMatch", "contentForReplacement"}`` and an applied result is ``{"baseContent", "newContent"}``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from misaka.utils.js import jsLength, jsTrimEnd
from misaka.utils.jsdiff import FILE_HEADERS_ONLY, createTwoFilesPatch, diffLines


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


def normalizeForFuzzyMatch(text: str) -> str:
    """Normalize text for fuzzy matching. Applies progressive transformations:

    - Strip trailing whitespace from each line
    - Normalize smart quotes to ASCII equivalents
    - Normalize Unicode dashes/hyphens to ASCII hyphen
    - Normalize special Unicode spaces to regular space"""
    text = "\n".join(jsTrimEnd(line) for line in unicodedata.normalize("NFKC", text).split("\n"))
    # Smart single quotes -> '
    text = re.sub("[\u2018\u2019\u201a\u201b]", "'", text)
    # Smart double quotes -> "
    text = re.sub("[\u201c\u201d\u201e\u201f]", '"', text)
    # Various dashes/hyphens -> -
    # U+2010 hyphen, U+2011 non-breaking hyphen, U+2012 figure dash, U+2013 en-dash, U+2014 em-dash,
    # U+2015 horizontal bar, U+2212 minus
    text = re.sub("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]", "-", text)
    # Special spaces -> regular space
    # U+00A0 NBSP, U+2002-U+200A various spaces, U+202F narrow NBSP, U+205F medium math space, U+3000 ideographic space
    return re.sub("[\u00a0\u2002-\u200a\u202f\u205f\u3000]", " ", text)


def splitLinesWithEndings(content: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+", content)


def getLineSpans(content: str) -> list[dict[str, int]]:
    offset = 0
    spans = []
    for line in splitLinesWithEndings(content):
        span = {"start": offset, "end": offset + len(line)}
        offset = span["end"]
        spans.append(span)
    return spans


def getReplacementLineRange(lines: list[dict[str, int]], replacement: dict[str, Any]) -> dict[str, int]:
    replacementStart = replacement["matchIndex"]
    replacementEnd = replacement["matchIndex"] + replacement["matchLength"]

    startLine = -1
    for i, line in enumerate(lines):
        if line["start"] <= replacementStart < line["end"]:
            startLine = i
            break
    if startLine == -1:
        raise RuntimeError("Replacement range is outside the base content.")

    endLine = startLine
    while endLine < len(lines) and lines[endLine]["end"] < replacementEnd:
        endLine += 1
    if endLine >= len(lines):
        raise RuntimeError("Replacement range is outside the base content.")

    return {"startLine": startLine, "endLine": endLine + 1}


def applyReplacements(content: str, replacements: list[dict[str, Any]], offset: int = 0) -> str:
    result = content
    for replacement in reversed(replacements):
        matchIndex = replacement["matchIndex"] - offset
        result = result[:matchIndex] + replacement["newText"] + result[matchIndex + replacement["matchLength"] :]
    return result


def applyReplacementsPreservingUnchangedLines(
    originalContent: str, baseContent: str, replacements: list[dict[str, Any]]
) -> str:
    """Apply replacements matched against ``baseContent`` to ``originalContent`` while preserving unchanged line blocks
    from the original.

    This is useful when ``baseContent`` is a normalized view of the original. Each replacement is widened to the lines
    it actually touches, those touched lines are rewritten from the normalized base, and all other lines are copied back
    from ``originalContent``. The actual replacement ranges drive preservation so duplicate normalized lines cannot be
    aligned to the wrong occurrence."""
    originalLines = splitLinesWithEndings(originalContent)
    baseLines = getLineSpans(baseContent)
    if len(originalLines) != len(baseLines):
        raise RuntimeError("Cannot preserve unchanged lines because the base content has a different line count.")

    groups: list[dict[str, Any]] = []
    for replacement in sorted(replacements, key=lambda item: item["matchIndex"]):
        range_ = getReplacementLineRange(baseLines, replacement)
        current = groups[-1] if groups else None
        if current is not None and range_["startLine"] < current["endLine"]:
            current["endLine"] = max(current["endLine"], range_["endLine"])
            current["replacements"].append(replacement)
            continue
        groups.append({**range_, "replacements": [replacement]})

    originalLineIndex = 0
    result = ""
    for group in groups:
        result += "".join(originalLines[originalLineIndex : group["startLine"]])

        groupStartOffset = baseLines[group["startLine"]]["start"]
        groupEndOffset = baseLines[group["endLine"] - 1]["end"]
        result += applyReplacements(baseContent[groupStartOffset:groupEndOffset], group["replacements"], groupStartOffset)
        originalLineIndex = group["endLine"]
    result += "".join(originalLines[originalLineIndex:])

    return result


def fuzzyFindText(content: str, oldText: str) -> dict[str, Any]:
    """Find oldText in content, trying exact match first, then fuzzy match. When fuzzy matching is used, the returned
    contentForReplacement is the fuzzy-normalized version of the content (trailing whitespace stripped, Unicode
    quotes/dashes normalized to ASCII)."""
    # Try exact match first
    exactIndex = content.find(oldText)
    if exactIndex != -1:
        return {
            "found": True,
            "index": exactIndex,
            "matchLength": len(oldText),
            "usedFuzzyMatch": False,
            "contentForReplacement": content,
        }

    # Try fuzzy match - work entirely in normalized space
    fuzzyContent = normalizeForFuzzyMatch(content)
    fuzzyOldText = normalizeForFuzzyMatch(oldText)
    fuzzyIndex = fuzzyContent.find(fuzzyOldText)

    if fuzzyIndex == -1:
        return {
            "found": False,
            "index": -1,
            "matchLength": 0,
            "usedFuzzyMatch": False,
            "contentForReplacement": content,
        }

    # When fuzzy matching, return offsets in normalized space. Callers can use the normalized content to compute
    # replacements, then decide how much of that normalized output should be written back.
    return {
        "found": True,
        "index": fuzzyIndex,
        "matchLength": len(fuzzyOldText),
        "usedFuzzyMatch": True,
        "contentForReplacement": fuzzyContent,
    }


def stripBom(content: str) -> dict[str, str]:
    """Strip UTF-8 BOM if present, return both the BOM (if any) and the text without it."""
    if content.startswith("\ufeff"):
        return {"bom": "\ufeff", "text": content[1:]}
    return {"bom": "", "text": content}


def countOccurrences(content: str, oldText: str) -> int:
    fuzzyContent = normalizeForFuzzyMatch(content)
    fuzzyOldText = normalizeForFuzzyMatch(oldText)
    # ``content.split("")`` splits into UTF-16 code units.
    if len(fuzzyOldText) == 0:
        return jsLength(fuzzyContent) - 1
    return len(fuzzyContent.split(fuzzyOldText)) - 1


def getNotFoundError(path: str, editIndex: int, totalEdits: int) -> Exception:
    if totalEdits == 1:
        return RuntimeError(
            f"Could not find the exact text in {path}. The old text must match exactly including all whitespace and newlines."
        )
    return RuntimeError(
        f"Could not find edits[{editIndex}] in {path}. The oldText must match exactly including all whitespace and newlines."
    )


def getDuplicateError(path: str, editIndex: int, totalEdits: int, occurrences: int) -> Exception:
    if totalEdits == 1:
        return RuntimeError(
            f"Found {occurrences} occurrences of the text in {path}. The text must be unique. Please provide more context to make it unique."
        )
    return RuntimeError(
        f"Found {occurrences} occurrences of edits[{editIndex}] in {path}. Each oldText must be unique. Please provide more context to make it unique."
    )


def getEmptyOldTextError(path: str, editIndex: int, totalEdits: int) -> Exception:
    if totalEdits == 1:
        return RuntimeError(f"oldText must not be empty in {path}.")
    return RuntimeError(f"edits[{editIndex}].oldText must not be empty in {path}.")


def getNoChangeError(path: str, totalEdits: int) -> Exception:
    if totalEdits == 1:
        return RuntimeError(
            f"No changes made to {path}. The replacement produced identical content. This might indicate an issue with special characters or the text not existing as expected."
        )
    return RuntimeError(f"No changes made to {path}. The replacements produced identical content.")


def applyEditsToNormalizedContent(normalizedContent: str, edits: list[dict[str, str]], path: str) -> dict[str, str]:
    """Apply one or more exact-text replacements to LF-normalized content.

    All edits are matched against the same original content. Replacements are then applied in reverse order so offsets
    remain stable. If any edit needs fuzzy matching, the operation runs in fuzzy-normalized content space and then
    overlays those line-level changes onto the original content so unchanged line blocks keep their original bytes."""
    normalizedEdits = [
        {"oldText": normalizeToLF(edit["oldText"]), "newText": normalizeToLF(edit["newText"])} for edit in edits
    ]

    for i, edit in enumerate(normalizedEdits):
        if len(edit["oldText"]) == 0:
            raise getEmptyOldTextError(path, i, len(normalizedEdits))

    initialMatches = [fuzzyFindText(normalizedContent, edit["oldText"]) for edit in normalizedEdits]
    usedFuzzyMatch = any(match["usedFuzzyMatch"] for match in initialMatches)
    replacementBaseContent = normalizeForFuzzyMatch(normalizedContent) if usedFuzzyMatch else normalizedContent

    matchedEdits: list[dict[str, Any]] = []
    for i, edit in enumerate(normalizedEdits):
        matchResult = fuzzyFindText(replacementBaseContent, edit["oldText"])
        if not matchResult["found"]:
            raise getNotFoundError(path, i, len(normalizedEdits))

        occurrences = countOccurrences(replacementBaseContent, edit["oldText"])
        if occurrences > 1:
            raise getDuplicateError(path, i, len(normalizedEdits), occurrences)

        matchedEdits.append(
            {
                "editIndex": i,
                "matchIndex": matchResult["index"],
                "matchLength": matchResult["matchLength"],
                "newText": edit["newText"],
            }
        )

    matchedEdits.sort(key=lambda item: item["matchIndex"])
    for i in range(1, len(matchedEdits)):
        previous = matchedEdits[i - 1]
        current = matchedEdits[i]
        if previous["matchIndex"] + previous["matchLength"] > current["matchIndex"]:
            raise RuntimeError(
                f"edits[{previous['editIndex']}] and edits[{current['editIndex']}] overlap in {path}. Merge them into one edit or target disjoint regions."
            )

    baseContent = normalizedContent
    newContent = (
        applyReplacementsPreservingUnchangedLines(normalizedContent, replacementBaseContent, matchedEdits)
        if usedFuzzyMatch
        else applyReplacements(replacementBaseContent, matchedEdits)
    )

    if baseContent == newContent:
        raise getNoChangeError(path, len(normalizedEdits))

    return {"baseContent": baseContent, "newContent": newContent}


def generateUnifiedPatch(path: str, oldContent: str, newContent: str, contextLines: int = 4) -> str:
    """Generate a standard unified patch."""
    return createTwoFilesPatch(
        path, path, oldContent, newContent, None, None, {"context": contextLines, "headerOptions": FILE_HEADERS_ONLY}
    )


def generateDiffString(oldContent: str, newContent: str, contextLines: int = 4) -> dict[str, Any]:
    """Generate a display-oriented diff string with line numbers and context. Returns both the diff string and the first
    changed line number (in the new file), ``None`` when nothing changed."""
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

        if part["added"] or part["removed"]:
            # Capture the first changed line (in the new file)
            if firstChangedLine is None:
                firstChangedLine = newLineNum

            # Show the change
            for line in raw:
                if part["added"]:
                    output.append(f"+{str(newLineNum).rjust(lineNumWidth)} {line}")
                    newLineNum += 1
                else:
                    # removed
                    output.append(f"-{str(oldLineNum).rjust(lineNumWidth)} {line}")
                    oldLineNum += 1
            lastWasChange = True
        else:
            # Context lines - only show a few before/after changes
            nextPartIsChange = i < len(parts) - 1 and (parts[i + 1]["added"] or parts[i + 1]["removed"])
            hasLeadingChange = lastWasChange
            hasTrailingChange = nextPartIsChange

            if hasLeadingChange and hasTrailingChange:
                if len(raw) <= contextLines * 2:
                    for line in raw:
                        output.append(f" {str(oldLineNum).rjust(lineNumWidth)} {line}")
                        oldLineNum += 1
                        newLineNum += 1
                else:
                    leadingLines = raw[:contextLines]
                    trailingLines = raw[len(raw) - contextLines :]
                    skippedLines = len(raw) - len(leadingLines) - len(trailingLines)

                    for line in leadingLines:
                        output.append(f" {str(oldLineNum).rjust(lineNumWidth)} {line}")
                        oldLineNum += 1
                        newLineNum += 1

                    output.append(f" {''.rjust(lineNumWidth)} ...")
                    oldLineNum += skippedLines
                    newLineNum += skippedLines

                    for line in trailingLines:
                        output.append(f" {str(oldLineNum).rjust(lineNumWidth)} {line}")
                        oldLineNum += 1
                        newLineNum += 1
            elif hasLeadingChange:
                shownLines = raw[:contextLines]
                skippedLines = len(raw) - len(shownLines)

                for line in shownLines:
                    output.append(f" {str(oldLineNum).rjust(lineNumWidth)} {line}")
                    oldLineNum += 1
                    newLineNum += 1

                if skippedLines > 0:
                    output.append(f" {''.rjust(lineNumWidth)} ...")
                    oldLineNum += skippedLines
                    newLineNum += skippedLines
            elif hasTrailingChange:
                skippedLines = max(0, len(raw) - contextLines)
                if skippedLines > 0:
                    output.append(f" {''.rjust(lineNumWidth)} ...")
                    oldLineNum += skippedLines
                    newLineNum += skippedLines

                for line in raw[skippedLines:]:
                    output.append(f" {str(oldLineNum).rjust(lineNumWidth)} {line}")
                    oldLineNum += 1
                    newLineNum += 1
            else:
                # Skip these context lines entirely
                oldLineNum += len(raw)
                newLineNum += len(raw)

            lastWasChange = False

    return {"diff": "\n".join(output), "firstChangedLine": firstChangedLine}


__all__ = [
    "applyEditsToNormalizedContent",
    "detectLineEnding",
    "fuzzyFindText",
    "generateDiffString",
    "generateUnifiedPatch",
    "normalizeForFuzzyMatch",
    "normalizeToLF",
    "restoreLineEndings",
    "stripBom",
]

"""Shared diff computation helpers for the edit tool."""

from __future__ import annotations

import asyncio
import difflib
import errno as errno_module
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from misaka.core.tools.path_utils import resolve_to_cwd


@dataclass(slots=True)
class Edit:
    oldText: str
    newText: str


@dataclass(slots=True)
class FuzzyMatchResult:
    found: bool
    index: int
    matchLength: int
    usedFuzzyMatch: bool
    contentForReplacement: str


@dataclass(slots=True)
class AppliedEditsResult:
    baseContent: str
    newContent: str


@dataclass(slots=True)
class EditDiffResult:
    diff: str
    firstChangedLine: int | None


@dataclass(slots=True)
class EditDiffError:
    error: str


@dataclass(slots=True)
class _MatchedEdit:
    editIndex: int
    matchIndex: int
    matchLength: int
    newText: str


@dataclass(slots=True)
class _StripBomResult:
    bom: str
    text: str

    def __iter__(self) -> Iterator[str]:
        yield self.bom
        yield self.text


@dataclass(slots=True)
class _DiffPart:
    value: str
    added: bool = False
    removed: bool = False


def detect_line_ending(content: str) -> str:
    crlf_index = content.find("\r\n")
    lf_index = content.find("\n")
    if lf_index == -1 or crlf_index == -1:
        return "\n"
    return "\r\n" if crlf_index < lf_index else "\n"


def normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


def normalize_for_fuzzy_match(text: str) -> str:
    return (
        "\n".join(line.rstrip() for line in unicodedata.normalize("NFKC", text).split("\n"))
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201a", "'")
        .replace("\u201b", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u201e", '"')
        .replace("\u201f", '"')
        .replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2015", "-")
        .replace("\u2212", "-")
        .replace("\u00a0", " ")
        .replace("\u2002", " ")
        .replace("\u2003", " ")
        .replace("\u2004", " ")
        .replace("\u2005", " ")
        .replace("\u2006", " ")
        .replace("\u2007", " ")
        .replace("\u2008", " ")
        .replace("\u2009", " ")
        .replace("\u200a", " ")
        .replace("\u202f", " ")
        .replace("\u205f", " ")
        .replace("\u3000", " ")
    )


def fuzzy_find_text(content: str, old_text: str) -> FuzzyMatchResult:
    exact_index = content.find(old_text)
    if exact_index != -1:
        return FuzzyMatchResult(
            found=True,
            index=exact_index,
            matchLength=len(old_text),
            usedFuzzyMatch=False,
            contentForReplacement=content,
        )

    fuzzy_content = normalize_for_fuzzy_match(content)
    fuzzy_old_text = normalize_for_fuzzy_match(old_text)
    fuzzy_index = fuzzy_content.find(fuzzy_old_text)
    if fuzzy_index == -1:
        return FuzzyMatchResult(
            found=False,
            index=-1,
            matchLength=0,
            usedFuzzyMatch=False,
            contentForReplacement=content,
        )

    return FuzzyMatchResult(
        found=True,
        index=fuzzy_index,
        matchLength=len(fuzzy_old_text),
        usedFuzzyMatch=True,
        contentForReplacement=fuzzy_content,
    )


def strip_bom(content: str) -> _StripBomResult:
    return _StripBomResult(bom="\ufeff", text=content[1:]) if content.startswith("\ufeff") else _StripBomResult(bom="", text=content)


def _count_occurrences(content: str, old_text: str) -> int:
    return normalize_for_fuzzy_match(content).count(normalize_for_fuzzy_match(old_text))


def _get_not_found_error(path: str, edit_index: int, total_edits: int) -> RuntimeError:
    if total_edits == 1:
        return RuntimeError(
            f"Could not find the exact text in {path}. "
            "The old text must match exactly including all whitespace and newlines."
        )
    return RuntimeError(
        f"Could not find edits[{edit_index}] in {path}. "
        "The oldText must match exactly including all whitespace and newlines."
    )


def _get_duplicate_error(path: str, edit_index: int, total_edits: int, occurrences: int) -> RuntimeError:
    if total_edits == 1:
        return RuntimeError(
            f"Found {occurrences} occurrences of the text in {path}. "
            "The text must be unique. Please provide more context to make it unique."
        )
    return RuntimeError(
        f"Found {occurrences} occurrences of edits[{edit_index}] in {path}. "
        "Each oldText must be unique. Please provide more context to make it unique."
    )


def _get_empty_old_text_error(path: str, edit_index: int, total_edits: int) -> RuntimeError:
    if total_edits == 1:
        return RuntimeError(f"oldText must not be empty in {path}.")
    return RuntimeError(f"edits[{edit_index}].oldText must not be empty in {path}.")


def _get_no_change_error(path: str, total_edits: int) -> RuntimeError:
    if total_edits == 1:
        return RuntimeError(
            f"No changes made to {path}. The replacement produced identical content. "
            "This might indicate an issue with special characters or the text not existing as expected."
        )
    return RuntimeError(f"No changes made to {path}. The replacements produced identical content.")


def apply_edits_to_normalized_content(
    normalized_content: str,
    edits: list[Edit | dict[str, str]],
    path: str,
) -> AppliedEditsResult:
    normalized_edits = [
        Edit(
            oldText=normalize_to_lf(edit.oldText if isinstance(edit, Edit) else edit["oldText"]),
            newText=normalize_to_lf(edit.newText if isinstance(edit, Edit) else edit["newText"]),
        )
        for edit in edits
    ]

    for index, edit in enumerate(normalized_edits):
        if edit.oldText == "":
            raise _get_empty_old_text_error(path, index, len(normalized_edits))

    initial_matches = [fuzzy_find_text(normalized_content, edit.oldText) for edit in normalized_edits]
    base_content = (
        normalize_for_fuzzy_match(normalized_content)
        if any(match.usedFuzzyMatch for match in initial_matches)
        else normalized_content
    )

    matched_edits: list[_MatchedEdit] = []
    for index, edit in enumerate(normalized_edits):
        match_result = fuzzy_find_text(base_content, edit.oldText)
        if not match_result.found:
            raise _get_not_found_error(path, index, len(normalized_edits))

        occurrences = _count_occurrences(base_content, edit.oldText)
        if occurrences > 1:
            raise _get_duplicate_error(path, index, len(normalized_edits), occurrences)

        matched_edits.append(
            _MatchedEdit(
                editIndex=index,
                matchIndex=match_result.index,
                matchLength=match_result.matchLength,
                newText=edit.newText,
            )
        )

    matched_edits.sort(key=lambda item: item.matchIndex)
    for index in range(1, len(matched_edits)):
        previous = matched_edits[index - 1]
        current = matched_edits[index]
        if previous.matchIndex + previous.matchLength > current.matchIndex:
            raise RuntimeError(
                f"edits[{previous.editIndex}] and edits[{current.editIndex}] overlap in {path}. "
                "Merge them into one edit or target disjoint regions."
            )

    new_content = base_content
    for matched in reversed(matched_edits):
        new_content = (
            new_content[: matched.matchIndex]
            + matched.newText
            + new_content[matched.matchIndex + matched.matchLength :]
        )

    if base_content == new_content:
        raise _get_no_change_error(path, len(normalized_edits))

    return AppliedEditsResult(baseContent=base_content, newContent=new_content)


def generate_unified_patch(path: str, old_content: str, new_content: str, context_lines: int = 4) -> str:
    return "".join(
        difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
            n=context_lines,
        )
    )


def _diff_lines(old_content: str, new_content: str) -> list[_DiffPart]:
    old_chunks = old_content.splitlines(keepends=True)
    new_chunks = new_content.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=old_chunks, b=new_chunks)

    parts: list[_DiffPart] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            parts.append(_DiffPart(value="".join(old_chunks[old_start:old_end])))
        elif tag == "delete":
            parts.append(_DiffPart(value="".join(old_chunks[old_start:old_end]), removed=True))
        elif tag == "insert":
            parts.append(_DiffPart(value="".join(new_chunks[new_start:new_end]), added=True))
        elif tag == "replace":
            removed_value = "".join(old_chunks[old_start:old_end])
            added_value = "".join(new_chunks[new_start:new_end])
            if removed_value:
                parts.append(_DiffPart(value=removed_value, removed=True))
            if added_value:
                parts.append(_DiffPart(value=added_value, added=True))
    return parts


def generate_diff_string(old_content: str, new_content: str, context_lines: int = 4) -> EditDiffResult:
    parts = _diff_lines(old_content, new_content)

    old_lines = old_content.split("\n")
    new_lines = new_content.split("\n")
    max_line_num = max(len(old_lines), len(new_lines))
    line_num_width = len(str(max_line_num))
    output: list[str] = []
    old_line_num = 1
    new_line_num = 1
    last_was_change = False
    first_changed_line: int | None = None

    for part_index, part in enumerate(parts):
        raw = part.value.split("\n")
        if raw and raw[-1] == "":
            raw.pop()

        if part.added or part.removed:
            if first_changed_line is None:
                first_changed_line = new_line_num

            for line in raw:
                if part.added:
                    output.append(f"+{str(new_line_num).rjust(line_num_width)} {line}")
                    new_line_num += 1
                else:
                    output.append(f"-{str(old_line_num).rjust(line_num_width)} {line}")
                    old_line_num += 1

            last_was_change = True
            continue

        next_part_is_change = part_index < len(parts) - 1 and (parts[part_index + 1].added or parts[part_index + 1].removed)
        has_leading_change = last_was_change
        has_trailing_change = next_part_is_change

        if has_leading_change and has_trailing_change:
            if len(raw) <= context_lines * 2:
                for line in raw:
                    output.append(f" {str(old_line_num).rjust(line_num_width)} {line}")
                    old_line_num += 1
                    new_line_num += 1
            else:
                leading_lines = raw[:context_lines]
                trailing_lines = raw[-context_lines:]
                skipped_lines = len(raw) - len(leading_lines) - len(trailing_lines)

                for line in leading_lines:
                    output.append(f" {str(old_line_num).rjust(line_num_width)} {line}")
                    old_line_num += 1
                    new_line_num += 1

                output.append(f" {' '.rjust(line_num_width)} ...")
                old_line_num += skipped_lines
                new_line_num += skipped_lines

                for line in trailing_lines:
                    output.append(f" {str(old_line_num).rjust(line_num_width)} {line}")
                    old_line_num += 1
                    new_line_num += 1
        elif has_leading_change:
            shown_lines = raw[:context_lines]
            skipped_lines = len(raw) - len(shown_lines)
            for line in shown_lines:
                output.append(f" {str(old_line_num).rjust(line_num_width)} {line}")
                old_line_num += 1
                new_line_num += 1
            if skipped_lines > 0:
                output.append(f" {' '.rjust(line_num_width)} ...")
                old_line_num += skipped_lines
                new_line_num += skipped_lines
        elif has_trailing_change:
            skipped_lines = max(0, len(raw) - context_lines)
            if skipped_lines > 0:
                output.append(f" {' '.rjust(line_num_width)} ...")
                old_line_num += skipped_lines
                new_line_num += skipped_lines
            for line in raw[skipped_lines:]:
                output.append(f" {str(old_line_num).rjust(line_num_width)} {line}")
                old_line_num += 1
                new_line_num += 1
        else:
            old_line_num += len(raw)
            new_line_num += len(raw)

        last_was_change = False

    return EditDiffResult(diff="\n".join(output), firstChangedLine=first_changed_line)


def _format_access_error(error: BaseException) -> str:
    if isinstance(error, OSError) and error.errno is not None:
        code = errno_module.errorcode.get(error.errno)
        if code:
            return f"Error code: {code}"
    if isinstance(error, Exception):
        return f"Error: {error}"
    return str(error)


def _check_readable_file(absolute_path: str) -> None:
    with open(absolute_path, "rb"):
        return


async def compute_edits_diff(path: str, edits: list[Edit | dict[str, str]], cwd: str) -> EditDiffResult | EditDiffError:
    absolute_path = resolve_to_cwd(path, cwd)
    try:
        try:
            await asyncio.to_thread(_check_readable_file, absolute_path)
        except BaseException as error:  # noqa: BLE001 - any access failure becomes the user-facing edit error
            return EditDiffError(error=f"Could not edit file: {path}. {_format_access_error(error)}.")

        raw_content = await asyncio.to_thread(Path(absolute_path).read_text, encoding="utf-8")
        _bom, content = strip_bom(raw_content)
        normalized_content = normalize_to_lf(content)
        applied = apply_edits_to_normalized_content(normalized_content, edits, path)
        return generate_diff_string(applied.baseContent, applied.newContent)
    except Exception as error:  # noqa: BLE001 - any diff failure is returned as EditDiffError
        return EditDiffError(error=str(error))


async def compute_edit_diff(path: str, old_text: str, new_text: str, cwd: str) -> EditDiffResult | EditDiffError:
    return await compute_edits_diff(path, [Edit(oldText=old_text, newText=new_text)], cwd)


detectLineEnding = detect_line_ending
normalizeToLF = normalize_to_lf
restoreLineEndings = restore_line_endings
normalizeForFuzzyMatch = normalize_for_fuzzy_match
fuzzyFindText = fuzzy_find_text
stripBom = strip_bom
applyEditsToNormalizedContent = apply_edits_to_normalized_content
generateUnifiedPatch = generate_unified_patch
generateDiffString = generate_diff_string
computeEditsDiff = compute_edits_diff
computeEditDiff = compute_edit_diff

__all__ = [
    "AppliedEditsResult",
    "Edit",
    "EditDiffError",
    "EditDiffResult",
    "FuzzyMatchResult",
    "applyEditsToNormalizedContent",
    "computeEditDiff",
    "computeEditsDiff",
    "detectLineEnding",
    "fuzzyFindText",
    "generateDiffString",
    "generateUnifiedPatch",
    "normalizeForFuzzyMatch",
    "normalizeToLF",
    "restoreLineEndings",
    "stripBom",
]

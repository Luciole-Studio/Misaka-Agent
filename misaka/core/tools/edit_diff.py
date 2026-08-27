"""Shared diff computation helpers for the edit tool."""

from __future__ import annotations

import asyncio
import bisect
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


_COMPAT_CHAR_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u00a0": " ", "\u2002": " ", "\u2003": " ", "\u2004": " ",
    "\u2005": " ", "\u2006": " ", "\u2007": " ", "\u2008": " ",
    "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u205f": " ",
    "\u3000": " ",
})


def normalize_for_fuzzy_match(text: str) -> str:
    return "\n".join(
        line.rstrip() for line in unicodedata.normalize("NFKC", text).split("\n")
    ).translate(_COMPAT_CHAR_MAP)


def _norm_frag(text: str) -> str:
    """``normalize_for_fuzzy_match`` for an intra-line fragment: no per-line rstrip."""
    return unicodedata.normalize("NFKC", text).translate(_COMPAT_CHAR_MAP)


# A pathological single line longer than this skips original-text recovery for its
# boundary fragment (the per-column search below is quadratic in the line length).
_RECOVERY_SCAN_LIMIT = 5000


def _orig_col(orig_line: str, norm_line: str, col: int) -> int | None:
    """Index into ``orig_line`` whose prefix normalizes to ``norm_line[:col]``, or None.

    NFKC is not prefix-stable (composition can merge characters at a cut point), so this
    scans candidate cut points instead of mapping arithmetically. Newlines never interact
    across NFKC, which is what makes the per-line search sound.
    """
    if col == 0:
        return 0
    if col > len(norm_line) or len(orig_line) > _RECOVERY_SCAN_LIMIT:
        return None
    target = norm_line[:col]
    for cut in range(1, len(orig_line) + 1):
        if _norm_frag(orig_line[:cut]) == target:
            return cut
    return None


def _recover_gap(
    orig_lines: list[str],
    norm_lines: list[str],
    start: tuple[int, int],
    end: tuple[int, int],
) -> str:
    """The original spelling of the normalized-content span ``start``..``end`` (line, col).

    Whole lines inside the span are returned verbatim from the original. A boundary that
    cuts a line mid-way is recovered through ``_orig_col``; when that fails, only that
    line's fragment falls back to its normalized form.
    """
    (line_a, col_a), (line_b, col_b) = start, end
    if line_a == line_b:
        cut_a = _orig_col(orig_lines[line_a], norm_lines[line_a], col_a)
        cut_b = _orig_col(orig_lines[line_a], norm_lines[line_a], col_b)
        if cut_a is not None and cut_b is not None and cut_a <= cut_b:
            return orig_lines[line_a][cut_a:cut_b]
        return norm_lines[line_a][col_a:col_b]
    cut_a = _orig_col(orig_lines[line_a], norm_lines[line_a], col_a)
    head = orig_lines[line_a][cut_a:] if cut_a is not None else norm_lines[line_a][col_a:]
    cut_b = _orig_col(orig_lines[line_b], norm_lines[line_b], col_b)
    tail = orig_lines[line_b][:cut_b] if cut_b is not None else norm_lines[line_b][:col_b]
    return "\n".join([head, *orig_lines[line_a + 1 : line_b], tail])


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


def _count_occurrences(content: str, old_text: str, *, used_fuzzy_match: bool) -> int:
    """Occurrences of ``old_text`` in ``content``, counted where the match was found.

    An exact match is unique or not in the content's own coordinates; folding both sides
    first would refuse a uniquely located oldText because an unrelated line happens to
    normalize to the same text (``x—y`` next to ``x-y``, curly next to straight quotes).
    Only a fuzzy match is genuinely ambiguous in normalized coordinates.
    """
    if not used_fuzzy_match:
        return content.count(old_text)
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
        # Empty *after* normalization, not just literally empty: matching folds each line's
        # trailing whitespace away, so a whitespace-only oldText would match at offset 0 with
        # length 0 and splice newText in as an insertion reported as a replacement.
        if normalize_for_fuzzy_match(edit.oldText) == "":
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

        occurrences = _count_occurrences(
            base_content, edit.oldText, used_fuzzy_match=match_result.usedFuzzyMatch
        )
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

    if base_content is not normalized_content:
        # Fuzzy coordinates, original bytes: matches were found in the normalized text, but
        # the file must not be rewritten in normalized form — that silently NFKC-folds every
        # untouched line (curly quotes, full-width characters, ligatures), which for a corpus
        # of humanities sources is data corruption, not normalization. Splice each newText at
        # its matched span and recover every gap between spans from the original content.
        reconstructed = _splice_edits_preserving_original(
            normalized_content, base_content, matched_edits
        )
        if reconstructed is not None:
            if reconstructed == normalized_content:
                raise _get_no_change_error(path, len(normalized_edits))
            return AppliedEditsResult(baseContent=normalized_content, newContent=reconstructed)
        # Line structure diverged under normalization (never observed for NFKC; guarded
        # anyway): fall back to the normalized splice below rather than corrupt offsets.

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


def _splice_edits_preserving_original(
    normalized_content: str,
    base_content: str,
    matched_edits: list[_MatchedEdit],
) -> str | None:
    """Apply ``matched_edits`` (spans in ``base_content`` coordinates) onto the original
    ``normalized_content``. Returns None when the two texts do not share line structure."""
    orig_lines = normalized_content.split("\n")
    norm_lines = base_content.split("\n")
    if len(orig_lines) != len(norm_lines):
        return None

    line_starts = [0]
    for line in norm_lines:
        line_starts.append(line_starts[-1] + len(line) + 1)

    def locate(index: int) -> tuple[int, int]:
        line_number = bisect.bisect_right(line_starts, index) - 1
        return line_number, index - line_starts[line_number]

    pieces: list[str] = []
    cursor = 0
    for matched in matched_edits:
        pieces.append(_recover_gap(orig_lines, norm_lines, locate(cursor), locate(matched.matchIndex)))
        span_end = matched.matchIndex + matched.matchLength
        if matched.newText == base_content[matched.matchIndex : span_end]:
            # A no-op edit (oldText == newText modulo normalization) must not fold the
            # span's original spelling; keeping the original bytes also lets a lone no-op
            # surface as the "No changes" error instead of a silent success.
            pieces.append(_recover_gap(orig_lines, norm_lines, locate(matched.matchIndex), locate(span_end)))
        else:
            pieces.append(matched.newText)
        cursor = span_end
    # The final gap runs to the end of the file: take the original tail verbatim. Mapping
    # only the start avoids locate(len(base_content)) landing before the last line's
    # trailing whitespace when the file has no final newline (the last line is rstripped
    # in normalized coordinates, so an end-mapped cut would drop that whitespace).
    tail_line, tail_col = locate(cursor)
    cut = _orig_col(orig_lines[tail_line], norm_lines[tail_line], tail_col)
    head = orig_lines[tail_line][cut:] if cut is not None else norm_lines[tail_line][tail_col:]
    pieces.append("\n".join([head, *orig_lines[tail_line + 1 :]]))
    return "".join(pieces)


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


__all__ = [
    "AppliedEditsResult",
    "Edit",
    "EditDiffError",
    "EditDiffResult",
    "FuzzyMatchResult",
]

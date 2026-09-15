"""The office tool: one file, a list of ops, one receipt.

FrontierAgent has one ``create_file`` that produces every deliverable -- Word, Excel,
PowerPoint and the text formats -- through a single ``path + ops`` contract (audit
D153-D160). misaka had no writer for any of them, so the only way to produce a .docx was a
model hand-driving python-docx through bash, where a typo is a traceback the model then has
to debug instead of doing the research it was asked for.

The two decisions the contract rests on:

* **ops are a list applied in order to one file**, so one call does twelve edits and the
  model gets one receipt instead of twelve round trips;
* **text is literal**. Formatting is structured parameters -- a run with ``bold: true``,
  a ``list`` with a level -- never Markdown syntax typed into the text, which a document
  writer stores as the asterisks it is. When the arguments look like Markdown anyway, the
  receipt says so without changing what was written.

``save_to``-style behaviour is deliberately absent from ``read`` for the mirror-image
reason; see ``tests/test_office_read_tool.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.experimental import get_experimental_tool_sampling
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import abort_race
from misaka.core.tools._office.schema import OPERATION_CONTRACT
from misaka.core.tools.file_mutation_queue import with_file_mutation_queue
from misaka.core.tools.path_utils import resolve_to_cwd
from misaka.core.tools.render_utils import render_tool_path, str_value
from misaka.core.tools.tool_definition_wrapper import wrap_tool_definition
from misaka.ui.tui import Text
from misaka.utils.values import read_field, signal_aborted

# The shorthand fields that fold into a single ``create``. A model asked for a text file
# should not have to know the ops grammar to write one.
_SHORTHAND = ("content", "rows", "data")


class OfficeToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(description="Path to the file to create or edit")
    ops: list[dict[str, Any]] | str | None = Field(
        default=None,
        description=(
            "Operations applied in order to the file: a JSON array of single-key objects, "
            'e.g. [{"set_cell": {"sheet": "S", "cell": "B2", "value": "=SUM(B3:B9)"}}]. '
            "Also accepts a JSON string, or @<path> naming a JSON file to read it from."
        ),
    )
    content: str | None = Field(
        default=None,
        description="Whole-file text (.txt/.md/.csv/.json/...); shorthand for one create op",
    )
    rows: list[Any] | None = Field(
        default=None, description="Rows for a csv/tsv/jsonl file; shorthand for one create op"
    )
    data: Any = Field(
        default=None, description="A JSON value for a .json/.jsonl file; shorthand for one create op"
    )
    overwrite: bool = Field(
        default=False, description="Allow create to replace a file that already exists"
    )


office_tool_system_prompt_contribution = {
    "snippet": "Create or edit .docx/.xlsx/.pptx and text deliverables through structured ops",
    "guidelines": [
        (
            "Use office when its structured operations fit the deliverable. Choose a better-suited "
            "available tool or skill when appropriate; ordinary text files may use file tools. "
            "A failed office call is not a prerequisite for choosing another suitable method."
        ),
        (
            "Preserve existing content when editing; use overwrite=true only for an intentional "
            "complete rebuild."
        ),
        (
            "For calculations derived from spreadsheet cells, prefer formulas over hard-coded "
            "results. Source data and externally computed results may be numeric values with "
            "their provenance and method recorded. Check the receipt: uncached formulas are "
            "not verified calculated results."
        ),
        (
            "In Office rich-text formats, use structured formatting rather than Markdown "
            "markers. In .md and .html files, markup is the intended literal content."
        ),
        (
            "Follow the document's template and choose available fonts that cover its characters, "
            "including CJK. Do not assume a font is installed merely because it can be named."
        ),
        (
            "Validate the output's content, structure and numbers by reading it back. Office "
            "text extraction does not verify layout or rendered glyphs: inspect rendered pages "
            "when visual fidelity matters and rendering is available; otherwise report that "
            "visual verification was not performed."
        ),
    ],
}
officeToolSystemPromptContribution = office_tool_system_prompt_contribution


def _load_ops(raw, cwd):
    """The ops list from whatever form the model sent, or ``(None, error)``.

    ``@<path>`` reads the JSON from a file so a long batch does not have to survive being
    retyped into a tool call. That path must be **inside the working directory**: the
    permission layer guards where this tool writes by inspecting ``path``, and it never
    sees this one, so an unconstrained ``@`` would be a read of any file on disk smuggled
    into a tool that is classified as a writer.
    """
    if raw is None:
        return None, ""
    if isinstance(raw, list):
        return raw, ""
    text = str(raw).strip()
    if text.startswith("@"):
        named = text[1:].strip()
        source = resolve_to_cwd(named, cwd)
        root = os.path.realpath(cwd)
        resolved = os.path.realpath(source)
        if resolved != root and not resolved.startswith(root + os.sep):
            return None, (f"the ops file {named!r} is outside the working directory; "
                          "pass ops inline or put the file inside it")
        try:
            with open(source, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as error:
            return None, f"cannot read ops file {named!r}: {error}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        return None, f"ops is not valid JSON: {error}"
    if not isinstance(parsed, list):
        return None, "ops must be a JSON array of single-key objects"
    return parsed, ""


def _desugar(parsed):
    """The ops to run, or ``(None, error)``.

    ``content``/``rows``/``data`` fold into one ``create``: writing a report should not
    require knowing the grammar. Sending both is refused rather than guessed at -- the two
    say different things about what the file should end up containing.
    """
    shorthand = {name: getattr(parsed, name) for name in _SHORTHAND
                 if getattr(parsed, name) is not None}
    if parsed.ops is not None and shorthand:
        return None, ("pass either ops or content/rows/data, not both: they describe the "
                      "same file two different ways")
    if shorthand:
        return [{"create": {**shorthand, "overwrite": parsed.overwrite}}], ""
    return None, ""


def create_office_tool_definition(
    cwd: str,
    _options: Mapping[str, Any] | None = None,
) -> ToolDefinition[OfficeToolInput | dict[str, Any], None]:
    async def execute(
        _tool_call_id: str,
        params: OfficeToolInput | dict[str, Any],
        signal: Any | None = None,
        _on_update: Callable[[AgentToolResult], None] | None = None,
        _ctx: Any = None,
    ) -> AgentToolResult:
        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")

        parsed = OfficeToolInput.model_validate(params)
        absolute_path = resolve_to_cwd(parsed.path, cwd)

        ops, problem = _desugar(parsed)
        if problem:
            raise RuntimeError(problem)
        if ops is None:
            ops, problem = _load_ops(parsed.ops, cwd)
            if problem:
                raise RuntimeError(problem)
        if not ops:
            raise RuntimeError(
                "office needs ops (a JSON array of single-key objects) or, for a whole "
                "text file, content/rows/data."
            )

        from misaka.core.tools import _office

        problem = _office.validate_ops(ops)
        if problem:
            raise RuntimeError(problem)

        from misaka.core.tools._office.paths import output_paths, resolve_ops
        ops = resolve_ops(ops, cwd)

        async def mutate() -> AgentToolResult:
            async def worker() -> AgentToolResult:
                # Parsing and saving an OOXML package is CPU-bound and unbounded in the
                # size of the document; on the event loop it stalls every other session.
                receipt = await asyncio.to_thread(
                    _office.run_ops, absolute_path, ops, overwrite=parsed.overwrite)
                from misaka.core.tools._office._intent import archive
                await asyncio.to_thread(archive, absolute_path, ops, workspace=cwd)
                if str(receipt).startswith(("[error]", "✗")):
                    raise RuntimeError(str(receipt))
                return AgentToolResult(content=[TextContent(text=receipt)], details=None)

            worker_task = asyncio.create_task(worker())
            try:
                async with abort_race(signal) as abort_task:
                    if abort_task is None:
                        return await asyncio.shield(worker_task)
                    done, _pending = await asyncio.wait(
                        {worker_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
                    if abort_task in done and worker_task not in done:
                        raise RuntimeError("Operation aborted")
                    return await asyncio.shield(worker_task)
            finally:
                # to_thread cannot stop an in-flight save. Hold every destination lock
                # through completion even when the caller aborts or cancels this task.
                while not worker_task.done():
                    try:
                        await asyncio.shield(worker_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:  # noqa: BLE001 - preserve the caller cancellation
                        break
                if not worker_task.cancelled():
                    worker_task.exception()  # retrieve failures when cancellation won

        # Exports mutate a second path. Canonical, sorted lock order prevents both
        # cross-document export races and deadlocks when two calls share destinations.
        paths = sorted({os.path.realpath(p) for p in output_paths(absolute_path, ops)})

        async def locked(index=0):
            if index == len(paths):
                if signal_aborted(signal):
                    raise RuntimeError("Operation aborted")
                return await mutate()
            return await with_file_mutation_queue(paths[index], lambda: locked(index + 1))

        return await locked()

    def render_call(args: Any, theme_obj: Any, context: Any) -> Text:
        text = context.lastComponent if isinstance(context.lastComponent, Text) else Text("", 0, 0)
        raw_path = str_value(read_field(args, "path")) or ""
        shown = render_tool_path(raw_path, context.cwd) if raw_path else ""
        raw_ops = read_field(args, "ops")
        count = len(raw_ops) if isinstance(raw_ops, list) else 0
        suffix = f" ({count} ops)" if count else ""
        text.setText(theme_obj.fg("secondary", f"office {shown}{suffix}"))
        return text

    def render_result(result: Any, _options: Any, theme_obj: Any, context: Any) -> Text:
        text = context.lastComponent if isinstance(context.lastComponent, Text) else Text("", 0, 0)
        content = read_field(result, "content", [])
        body = "\n".join((read_field(block, "text") or "") for block in content
                         if read_field(block, "type") == "text") if isinstance(content, list) else ""
        colour = "error" if read_field(result, "isError") else "secondary"
        text.setText(theme_obj.fg(colour, body))
        return text

    return ToolDefinition(
        name="office",
        label="office",
        description=(
            "Create or edit a deliverable file through structured operations: Word "
            "(.docx), Excel (.xlsx), PowerPoint (.pptx) and text formats (.txt, .md, "
            ".csv, .tsv, .json, .jsonl, .html). Pass path plus ops, a JSON array of "
            "single-key objects applied in order to that one file — do many edits in one "
            "call. For a whole text file pass content (or rows/data) instead of ops. create "
            "rejects an existing path unless overwrite=true; use editing operations to preserve "
            "other content. Text is literal: Office rich-text formatting uses structured "
            "parameters, while .md/.html retain their markup. docx ops: create, replace_text, insert_paragraph, "
            "insert_heading, insert_table, format_text, format_paragraph, add_hyperlink, "
            "add_image, set_page_number, set_page_margins, set_page_orientation, "
            "set_header_footer. xlsx ops: create, set_cell, set_range, add_sheet, "
            "delete_sheet, rename_sheet, set_cell_format, add_table, add_chart, "
            "clear_charts, merge_cells, unmerge_cells, freeze_panes, set_column_width, "
            "set_row_height, set_page_setup, hide_sheet, show_sheet, add_named_range, "
            "delete_named_range, add_data_validation, add_conditional_formatting, "
            "set_auto_filter, set_number_format, add_image. pptx ops: create, add_slide, "
            "set_text, add_textbox, add_table, add_image, set_notes, replace_text, "
            "add_shape, add_chart, format_text, duplicate_slide, delete_slide, "
            "set_slide_size. Text ops: create, append, replace_text."
        ) + "\n\n" + OPERATION_CONTRACT,
        promptSnippet=office_tool_system_prompt_contribution["snippet"],
        promptGuidelines=list(office_tool_system_prompt_contribution["guidelines"]),
        parameters=OfficeToolInput,
        constrainedSampling=get_experimental_tool_sampling(),
        execute=execute,
        renderCall=render_call,
        renderResult=render_result,
    )


def create_office_tool(cwd: str, options: Mapping[str, Any] | None = None) -> AgentTool:
    return wrap_tool_definition(create_office_tool_definition(cwd, options))


createOfficeTool = create_office_tool
createOfficeToolDefinition = create_office_tool_definition

__all__ = [
    "OfficeToolInput",
    "createOfficeTool",
    "createOfficeToolDefinition",
    "create_office_tool",
    "create_office_tool_definition",
    "officeToolSystemPromptContribution",
    "office_tool_system_prompt_contribution",
]

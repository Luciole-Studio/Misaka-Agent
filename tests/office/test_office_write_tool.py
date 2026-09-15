"""The office tool: one file, a list of ops, one receipt, all or nothing.

misaka had no writer for any Office format, so producing a .docx meant a model hand-driving
python-docx through bash -- where a typo is a traceback it then has to debug instead of
doing the research it was asked for. FrontierAgent's ``create_file`` is the shape this
follows: ``path`` plus ``ops``, applied in order to one file, one receipt back.

What is tested here is the contract around the ops rather than the ops themselves (those
are in ``test_office_docx_write`` and its siblings): the shorthand, the refusals, the
atomicity of a batch, and the fact that a receipt tells the truth about what is now on
disk.
"""
from __future__ import annotations

import importlib
import json

import pytest

from misaka.core.tools.office import create_office_tool_definition

importlib.import_module("docx")
importlib.import_module("openpyxl")


async def _office(cwd, **params):
    tool = create_office_tool_definition(str(cwd))
    result = await tool.execute("call-1", params, None, None, None)
    return "\n".join(part.text for part in result.content if hasattr(part, "text"))


# ---- the ops contract ------------------------------------------------------------------

async def test_a_batch_of_ops_is_one_call_and_one_receipt(tmp_path):
    receipt = await _office(
        tmp_path, path="book.xlsx",
        ops=[{"create": {"sheets": [{"name": "S", "headers": ["a", "b"]}]}},
             {"set_cell": {"sheet": "S", "cell": "A2", "value": 1}},
             {"freeze_panes": {"sheet": "S", "cell": "A2"}}])
    assert "3 ops applied" in receipt
    assert (tmp_path / "book.xlsx").exists()


async def test_a_clean_batch_folds_instead_of_listing_every_op(tmp_path):
    """A per-op transcript costs a screen of "✓ set_cell" the model cannot act on, and
    buries the one line that says something went wrong."""
    ops = [{"create": {"sheets": [{"name": "S"}]}}]
    ops += [{"set_cell": {"sheet": "S", "cell": f"A{n}", "value": n}} for n in range(1, 11)]
    receipt = await _office(tmp_path, path="many.xlsx", ops=ops)
    assert len(receipt.splitlines()) <= 4
    assert "11 ops applied" in receipt


async def test_an_op_that_changed_nothing_is_named(tmp_path):
    """Not an error, but almost always a mistake -- and silence about it is how a model
    comes to believe it edited a file it never touched."""
    await _office(tmp_path, path="notes.md", content="hello")
    receipt = await _office(
        tmp_path, path="notes.md",
        ops=[{"append": {"content": "\nmore"}},
             {"replace_text": {"find": "nowhere", "replace": "x"}}])
    assert "1 wrote nothing" in receipt
    assert "no match for 'nowhere'" in receipt


async def test_ops_must_be_single_key_objects(tmp_path):
    """The shape is load-bearing: a two-key object has no defined order and the model that
    wrote it meant a sequence. The refusal names the shape, so it can be fixed."""
    with pytest.raises(RuntimeError) as caught:
        await _office(tmp_path, path="x.md",
                      ops=[{"create": {"content": "a"}, "append": {"content": "b"}}])
    assert "single-key object {op_name: {params}}" in str(caught.value)


async def test_an_unknown_op_names_what_this_format_writes(tmp_path):
    await _office(tmp_path, path="book.xlsx",
                  ops=[{"create": {"sheets": [{"name": "S"}]}}])
    with pytest.raises(RuntimeError) as caught:
        await _office(tmp_path, path="book.xlsx", ops=[{"teleport": {}}])
    receipt = str(caught.value)
    assert "unknown xlsx op 'teleport'" in receipt
    assert "set_cell" in receipt


async def test_a_format_this_tool_does_not_write_is_refused_by_name(tmp_path):
    with pytest.raises(RuntimeError) as caught:
        await _office(tmp_path, path="scan.pdf", ops=[{"create": {"content": "x"}}])
    assert ".pdf" in str(caught.value)
    assert ".docx" in str(caught.value)


# ---- the shorthand ---------------------------------------------------------------------

async def test_content_alone_writes_a_whole_text_file(tmp_path):
    """Writing a report should not require knowing the ops grammar."""
    await _office(tmp_path, path="report.md", content="# Findings\n\nbody\n")
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == "# Findings\n\nbody\n"


async def test_rows_become_a_csv_with_its_quoting(tmp_path):
    await _office(tmp_path, path="data.csv",
                  rows=[["region", "note"], ["North", "a, with a comma"]])
    assert (tmp_path / "data.csv").read_text(encoding="utf-8") == (
        'region,note\nNorth,"a, with a comma"\n')


async def test_data_becomes_json(tmp_path):
    await _office(tmp_path, path="out.json", data={"n": 1, "items": ["a"]})
    assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8")) == {
        "n": 1, "items": ["a"]}


async def test_ops_and_the_shorthand_together_are_refused(tmp_path):
    """They describe the same file two different ways; guessing which one was meant is how
    a deliverable comes out as neither."""
    with pytest.raises(RuntimeError) as caught:
        await _office(tmp_path, path="x.md", content="a",
                      ops=[{"create": {"content": "b"}}])
    assert "either ops or content/rows/data" in str(caught.value)


async def test_ops_may_arrive_as_a_json_string(tmp_path):
    await _office(tmp_path, path="x.md",
                  ops=json.dumps([{"create": {"content": "from a string"}}]))
    assert (tmp_path / "x.md").read_text(encoding="utf-8") == "from a string"


async def test_a_long_batch_can_come_from_a_file(tmp_path):
    (tmp_path / "batch.json").write_text(
        json.dumps([{"create": {"content": "from a file"}}]), encoding="utf-8")
    await _office(tmp_path, path="x.md", ops="@batch.json")
    assert (tmp_path / "x.md").read_text(encoding="utf-8") == "from a file"


async def test_an_ops_file_outside_the_working_directory_is_refused(tmp_path):
    """The permission layer guards where this tool writes by reading ``path``, and never
    sees this one. An unconstrained @ would be a read of any file on disk, smuggled into a
    tool classified as a writer."""
    outside = tmp_path.parent / "elsewhere.json"
    outside.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError) as caught:
        await _office(tmp_path / "work", path="x.md", ops=f"@{outside}")
    assert "outside the working directory" in str(caught.value)


# ---- what the receipt promises ----------------------------------------------------------

async def test_create_refuses_a_path_that_exists(tmp_path):
    await _office(tmp_path, path="report.md", content="original")
    with pytest.raises(RuntimeError) as caught:
        await _office(tmp_path, path="report.md", content="replacement")
    receipt = str(caught.value)
    assert "already exists" in receipt
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == "original"


async def test_overwrite_rebuilds_it(tmp_path):
    await _office(tmp_path, path="report.md", content="original")
    await _office(tmp_path, path="report.md", content="replacement", overwrite=True)
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == "replacement"


async def test_a_failed_batch_leaves_the_file_exactly_as_it_was(tmp_path):
    """FrontierAgent edits the file in place, so a batch that fails on op 2 of 3 leaves a
    half-built document and a receipt listing which ops "survived" -- and a model that
    retries the batch then applies op 1 twice. The batch runs on a copy instead."""
    await _office(tmp_path, path="book.xlsx",
                  ops=[{"create": {"sheets": [{"name": "S", "headers": ["a"]}]}}])
    before = (tmp_path / "book.xlsx").read_bytes()
    with pytest.raises(RuntimeError) as caught:
        await _office(
            tmp_path, path="book.xlsx",
            ops=[{"set_cell": {"sheet": "S", "cell": "A2", "value": "kept?"}},
                 {"delete_sheet": {"sheet": "no such sheet"}},
                 {"set_cell": {"sheet": "S", "cell": "A3", "value": "never"}}])
    receipt = str(caught.value)
    assert "STOPPED at op 2/3" in receipt
    assert "ops 3-3 not executed" in receipt
    assert (tmp_path / "book.xlsx").read_bytes() == before


async def test_the_receipt_names_the_file_the_model_can_open(tmp_path):
    """The ops run against a staging copy; naming it in the receipt would send the model
    looking for a file that no longer exists."""
    receipt = await _office(tmp_path, path="report.md", content="body")
    assert ".office-" not in receipt
    assert "report.md" in receipt


async def test_a_workbook_with_formulas_says_its_values_are_not_in_the_file_yet(tmp_path):
    """openpyxl stores the formula and cannot store its result. Without this line a model
    reads the workbook back, sees empty cells, and has no idea why."""
    receipt = await _office(
        tmp_path, path="sums.xlsx",
        ops=[{"create": {"sheets": [{"name": "S", "headers": ["v"],
                                     "rows": [[1], [2], ["=SUM(A2:A3)"]]}]}}])
    assert "cached values are empty" in receipt


async def test_a_workbook_without_formulas_says_nothing_about_recalculation(tmp_path):
    receipt = await _office(
        tmp_path, path="plain.xlsx",
        ops=[{"create": {"sheets": [{"name": "S", "headers": ["v"], "rows": [[1]]}]}}])
    assert "cached values" not in receipt


async def test_markdown_in_a_document_gets_a_hint_and_is_written_as_typed(tmp_path):
    """A document is not a markdown file: ``**bold**`` in a .docx paragraph is four literal
    asterisks. Guessing which asterisks were meant as emphasis is how a writer corrupts a
    document, so the content is untouched and the receipt says what to use instead."""
    receipt = await _office(
        tmp_path, path="doc.docx",
        ops=[{"create": {"blocks": [{"type": "paragraph", "text": "**Total** revenue"}]}}])
    assert "a run's bold field" in receipt

    from misaka.core.documents.office import docx as reader
    assert "**Total** revenue" in reader.render(str(tmp_path / "doc.docx"))


async def test_markdown_in_a_markdown_file_gets_no_hint(tmp_path):
    """There the markup is the content, and the hint would be advice to stop doing the
    right thing."""
    receipt = await _office(tmp_path, path="notes.md", content="**bold** text")
    assert "hint" not in receipt


# ---- the archive ------------------------------------------------------------------------

async def test_every_call_is_archived_so_a_wrong_deliverable_can_be_explained(tmp_path, monkeypatch):
    """The ops that produced a file are gone the moment the call returns; afterwards there
    is only the file and a receipt saying it worked."""
    from misaka.config.product import CFG
    monkeypatch.setitem(CFG, "office_intent", str(tmp_path / "intent"))

    await _office(tmp_path, path="report.md", content="one")
    await _office(tmp_path, path="report.md", ops=[{"append": {"content": "\ntwo"}}])

    buckets = list((tmp_path / ".office-intent").iterdir())
    assert len(buckets) == 1                      # both calls named the same file
    names = sorted(entry.name for entry in buckets[0].iterdir())
    assert names == ["001_create.json", "002_append.json"]
    record = json.loads((buckets[0] / "001_create.json").read_text(encoding="utf-8"))
    assert record["ops"][0]["create"]["content"] == "one"


async def test_archiving_never_fails_the_write_that_succeeded(tmp_path, monkeypatch):
    # A path that cannot be a directory: the archive has to give up quietly.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    (tmp_path / ".office-intent").symlink_to(blocker)

    await _office(tmp_path, path="report.md", content="body")
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == "body"


# ---- how it sits beside the other tools -------------------------------------------------

def test_office_is_a_registered_tool_with_a_prompt_snippet():
    """A tool with no snippet is invisible in the prompt's tool list, so the model never
    learns it exists."""
    from misaka.core import tools

    assert "office" in tools.all_tool_names
    assert tools.create_tool_definition("office", ".").promptSnippet


async def test_office_is_active_by_default(tmp_path):
    """It is the only way to produce a deliverable, so it has to be in the default set --
    behind a flag it would be a capability nobody uses. Three lists decide that and they
    are separate: the sdk's default, and the hardcoded fallbacks in system_prompt and
    agent_session that apply when no list was configured."""
    import inspect

    from misaka.core import agent_session, sdk, system_prompt

    for module in (sdk, system_prompt, agent_session):
        source = inspect.getsource(module)
        assert '"read", "bash", "edit", "write", "office"' in source, module.__name__


def test_office_is_governed_like_the_other_mutating_tools():
    """It puts bytes at a path, so it shares plan-mode denial, the workspace guard and the
    acceptEdits allowance. A guard naming only edit and write would have let a deliverable
    be written anywhere on disk."""
    from misaka.core.subagent.policy import CLASSIFIED_TOOLS, MUTATING_PATH_TOOLS

    assert "office" in MUTATING_PATH_TOOLS
    assert "office" in CLASSIFIED_TOOLS


def test_plan_mode_denies_office():
    from misaka.core.subagent.policy import _plan_denial

    assert _plan_denial("office", {"path": "report.docx"}) is not None


def test_the_prompt_contribution_is_direct_module_api(tmp_path):
    """The same shape every tool module exposes: the contribution is reachable on the
    module under both spellings and stays off the facades, and what the definition carries
    is a copy, so a caller editing the definition's list cannot rewrite the module's."""
    from misaka import core as core_facade
    from misaka.core import sdk as sdk_facade
    from misaka.core import tools as tools_facade
    from misaka.core.tools import office as office_module

    contribution = office_module.officeToolSystemPromptContribution
    definition = create_office_tool_definition(str(tmp_path))

    assert contribution is office_module.office_tool_system_prompt_contribution
    assert definition.promptSnippet == contribution["snippet"]
    assert definition.promptGuidelines == contribution["guidelines"]
    assert definition.promptGuidelines is not contribution["guidelines"]
    assert "officeToolSystemPromptContribution" in office_module.__all__
    for facade in (tools_facade, sdk_facade, core_facade):
        assert not hasattr(facade, "officeToolSystemPromptContribution")


def test_the_guidelines_say_what_a_model_must_not_do_instead():
    """These are behaviour, not documentation: without the first one a model reaches for a
    bash heredoc, and without the formula one it computes a total itself and writes the
    number, which then never updates."""
    from misaka.core.tools.office import office_tool_system_prompt_contribution

    joined = "\n".join(office_tool_system_prompt_contribution["guidelines"])
    assert "Choose a better-suited" in joined
    assert "prefer formulas" in joined
    assert "structured formatting rather than Markdown" in joined

"""Exercise the actual Research picker and model-supplied text without a live terminal."""
import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.research.wiring import research
from misaka.ui.tui import visibleWidth
from misaka.ui.tui.interactive.components.ask_user_question import (
    AskUserQuestionComponent,
)


def research_questions():
    tree = ast.parse(Path(research.__file__).read_text())
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "AskUserQuestionComponent")
    return eval(compile(ast.Expression(call.args[0]), "<research picker>", "eval"), vars(research))


def plain(lines):
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", "\n".join(lines))


def assert_fits(component, width):
    lines = component.render(width)
    assert all(visibleWidth(line) <= width for line in lines), [
        (visibleWidth(line), line) for line in lines if visibleWidth(line) > width]
    assert all("\n" not in line and "\r" not in line for line in lines)
    return lines


@pytest.mark.parametrize("width", [1, 2, 10, 30, 60, 71, 72, 93, 105, 120])
def test_research_picker_all_pages_fit(width):
    questions = research_questions()
    component = AskUserQuestionComponent(questions, lambda _result: None)
    for page in range(len(questions) + 1):
        component.current = page
        component.answers = {q["question"]: q["options"][0]["label"] for q in questions[:page]}
        assert_fits(component, width)
    component.current = 2
    if width >= 30:
        rendered = plain(assert_fits(component, width))
        assert re.sub(r"\s+", "", research._ROUNDS_QUESTION) in re.sub(r"\s+", "", rendered)


@pytest.mark.parametrize("width", [1, 2, 10, 30, 60, 71, 72, 93])
@pytest.mark.parametrize("preview", [False, True])
def test_long_question_options_other_notes_and_review_fit(width, preview):
    question = {"header": "Methods", "question": "Question\n第二行 " + "很长的人文研究问题" * 12,
                "options": [{"label": "Long option " + "多维方法" * 30,
                             "description": "Description " * 30,
                             **({"preview": "## Preview\n" + "Long preview " * 200} if preview else {})},
                            {"label": "Brief option", "description": "Description"}]}
    component = AskUserQuestionComponent([question], lambda _result: None,
                                         tui=SimpleNamespace(terminal=SimpleNamespace(rows=24), requestRender=lambda: None))
    component.warning = "A very long warning " * 20
    component.otherValues[question["question"]] = "Long free-form reply " * 30
    component.notes[question["question"]] = "Long notes " * 40
    assert_fits(component, width)
    component.answers[question["question"]] = component.otherValues[question["question"]]
    component.current = 1
    assert_fits(component, width)
    component.current = 0
    component.focuses[0] = component._other_index()
    component.inputMode = "other"
    component.editor.setText("Typed answer " * 25)
    assert_fits(component, width)
    if preview:
        component.inputMode = "notes"
        assert_fits(component, width)

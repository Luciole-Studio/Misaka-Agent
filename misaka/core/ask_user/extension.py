"""Claude-style structured user questions on MISAKA's Pi extension surface."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from misaka.core.extensions.types import ToolDefinition
from misaka.ui.tui.interactive.components.ask_user_question import (
    AskUserQuestionComponent,
)

ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuestionOption(_StrictModel):
    label: str = Field(min_length=1, max_length=80, description="Short option label")
    description: str = Field(min_length=1, description="What this option means or what it trades off")
    preview: str | None = Field(
        default=None,
        description="Optional Markdown preview of a concrete artifact; single-select questions only",
    )


class Question(_StrictModel):
    question: str = Field(min_length=1, description="The full question shown to the user")
    header: str = Field(min_length=1, max_length=12, description="Short label for the question tab")
    options: list[QuestionOption] = Field(
        min_length=2,
        max_length=4,
        description="Distinct choices; do not include an Other option, the UI adds one",
    )
    multiSelect: bool = Field(default=False, description="Allow the user to pick more than one option")

    @model_validator(mode="after")
    def unique_labels(self) -> Question:
        labels = [option.label for option in self.options]
        if len(labels) != len(set(labels)):
            raise ValueError("option labels must be unique within a question")
        return self


class AskUserQuestionParams(_StrictModel):
    questions: list[Question] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unique_questions(self) -> AskUserQuestionParams:
        texts = [question.question for question in self.questions]
        if len(texts) != len(set(texts)):
            raise ValueError("question texts must be unique")
        return self


def _text_result(text: str, details: dict[str, Any], images: list[dict[str, Any]]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}, *images], "details": details}


def _format_answers(
    questions: list[Question],
    answers: dict[str, str],
    annotations: dict[str, dict[str, str]],
    *,
    include_unanswered: bool = False,
) -> str:
    lines: list[str] = []
    for question in questions:
        text = question.question
        answer = answers.get(text)
        if answer is None and not include_unanswered:
            continue
        lines.append(f'- "{text}"')
        lines.append(f"  Answer: {answer}" if answer is not None else "  (No answer provided)")
        annotation = annotations.get(text, {})
        if annotation.get("preview"):
            lines.append(f"  Selected preview:\n{annotation['preview']}")
        if annotation.get("notes"):
            lines.append(f"  User notes: {annotation['notes']}")
    return "\n".join(lines)


def register(harn: Any) -> None:
    async def execute(
        _tool_call_id: str,
        raw: Any,
        _signal: Any,
        _on_update: Any,
        ctx: Any,
    ) -> dict[str, Any]:
        params = raw if isinstance(raw, AskUserQuestionParams) else AskUserQuestionParams.model_validate(raw)
        if ctx is None or not bool(getattr(ctx, "hasUI", False)):
            # The same role can run without a terminal. Preserve the questions in
            # its transcript, but never invent a human answer or block on stdin.
            return _text_result(
                "No interactive dialog is attached; these questions are unanswered. "
                "Show them to the user in your response. If an answer is required to proceed, "
                "use the workflow's clarification/waiting path; do not assume approval.\n"
                + _format_answers(params.questions, {}, {}, include_unanswered=True),
                {"action": "unanswered", "questions": [q.model_dump() for q in params.questions],
                 "answers": {}, "annotations": {}, "imageCount": 0}, [])

        result = await ctx.ui.custom(
            lambda tui, _theme, keybindings, done: AskUserQuestionComponent(
                [question.model_dump() for question in params.questions],
                done,
                tui=tui,
                keybindings=keybindings,
            )
        )
        if not isinstance(result, dict):
            raise RuntimeError("AskUserQuestion dialog closed without a result")  # noqa: TRY004 - callers treat bad input as ValueError

        action = str(result.get("action", "cancel"))
        answers = dict(result.get("answers") or {})
        annotations = dict(result.get("annotations") or {})
        images = list(result.get("images") or [])
        details = {
            "action": action,
            "questions": [question.model_dump() for question in params.questions],
            "answers": answers,
            "annotations": annotations,
            "imageCount": len(images),
        }
        if action == "submit":
            formatted = _format_answers(params.questions, answers, annotations) or "(No answer provided)"
            return _text_result(f"The user answered:\n{formatted}", details, images)
        if action == "clarify":
            formatted = _format_answers(
                params.questions,
                answers,
                annotations,
                include_unanswered=True,
            )
            return _text_result(
                "The user wants to discuss or clarify these questions before answering. "
                "Ask what they want to clarify, incorporate the response, and ask again if needed.\n"
                f"Current partial answers:\n{formatted}",
                details,
                images,
            )
        return _text_result("The user declined to answer these questions.", details, images)

    harn.registerTool(
        ToolDefinition(
            name=ASK_USER_QUESTION_TOOL_NAME,
            label="Ask User",
            description=(
                "Ask the user one to four structured questions when a preference or missing "
                "piece of information materially affects the work. Each question offers 2-4 labeled "
                "choices; the UI adds a free-form 'Other' and a 'Chat about this' option automatically. "
                "Optional Markdown previews let the user compare concrete artifacts side by side. "
                "Do not use it for facts you can find in the workspace."
            ),
            parameters=AskUserQuestionParams.model_json_schema(),
            execute=execute,
            executionMode="sequential",
            promptSnippet="Ask the user a structured question",
            promptGuidelines=[
                "Ask only when the answer materially changes what you should do.",
                "Keep headers short and options distinct; never supply an 'Other' option yourself.",
                "Put the recommended choice first and suffix its label with (Recommended).",
                "Use multiSelect only for choices that can coexist.",
                "Use Markdown previews only for single-select questions about concrete artifacts, where seeing them side by side genuinely helps.",
            ],
        )
    )


__all__ = [
    "ASK_USER_QUESTION_TOOL_NAME",
    "AskUserQuestionParams",
    "Question",
    "QuestionOption",
    "register",
]

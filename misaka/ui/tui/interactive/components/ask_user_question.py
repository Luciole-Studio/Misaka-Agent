"""Claude-style AskUserQuestion UI rendered with MISAKA's native TUI and theme."""

from __future__ import annotations

import asyncio
import base64
import os
import unicodedata
from collections.abc import Callable
from itertools import zip_longest
from types import SimpleNamespace
from typing import Any

from misaka.ai.types import ImageContent
from misaka.tui import Editor, EditorOptions, Markdown, getKeybindings, matchesKey, truncateToWidth, visibleWidth, wrapTextWithAnsi
from misaka.utils.clipboard_image import read_clipboard_image

from misaka.modes.interactive.components.extension_editor import edit_text_external
from misaka.modes.interactive.theme.theme import get_editor_theme, get_markdown_theme, theme


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _pad(text: str, width: int) -> str:
    clipped = truncateToWidth(text, width, "")
    return clipped + " " * max(0, width - visibleWidth(clipped))


class _FallbackTUI:
    terminal = SimpleNamespace(rows=40)

    def requestRender(self, *_args: Any) -> None:
        return None


class AskUserQuestionComponent:
    """Question prompt with single-select, multi-select and preview layouts, plus the
    free-form "Other" entry, "Chat about this", and a final review screen."""

    def __init__(
        self,
        questions: list[Any],
        onDone: Callable[[dict[str, Any]], None],
        *,
        tui: Any | None = None,
        keybindings: Any | None = None,
    ) -> None:
        self.questions = questions
        self.onDone = onDone
        self.tui = tui or _FallbackTUI()
        self.keybindings = keybindings
        self.current = 0
        self.focuses = [0 for _ in questions]
        self.submitFocus = 0
        self.answers: dict[str, str] = {}
        self.multiValues: dict[str, list[str]] = {}
        self.otherValues: dict[str, str] = {}
        self.notes: dict[str, str] = {}
        self.images: dict[str, list[ImageContent]] = {}
        self.inputMode: str | None = None
        self.imagesSelected = False
        self.selectedImageIndex = 0
        self.warning: str | None = None
        self._focused = False
        self._tasks: set[asyncio.Task[Any]] = set()

        self.editor = Editor(
            self.tui,
            get_editor_theme(),
            EditorOptions(paddingX=0, autocompleteMaxVisible=3),
        )
        self.editor.onChange = lambda _text: self._input_changed()
        self.editor.onSubmit = lambda _text: self._submit_input()

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.editor.focused = value and self.inputMode is not None and not self.imagesSelected

    def _question(self) -> Any:
        return self.questions[self.current]

    def _key(self, question: Any | None = None) -> str:
        return str(_get(question or self._question(), "question", ""))

    def _options(self, question: Any | None = None) -> list[Any]:
        return list(_get(question or self._question(), "options", []) or [])

    def _is_multi(self, question: Any | None = None) -> bool:
        return bool(_get(question or self._question(), "multiSelect", False))

    def _has_preview(self, question: Any | None = None) -> bool:
        q = question or self._question()
        return not self._is_multi(q) and any(_get(option, "preview") for option in self._options(q))

    def _other_index(self, question: Any | None = None) -> int:
        return len(self._options(question))

    def _content_count(self, question: Any | None = None) -> int:
        q = question or self._question()
        count = len(self._options(q))
        if not self._has_preview(q):
            count += 1
        if self._is_multi(q):
            count += 1
        return count

    def _submit_index(self, question: Any | None = None) -> int | None:
        q = question or self._question()
        return self._content_count(q) - 1 if self._is_multi(q) else None

    def _chat_index(self, question: Any | None = None) -> int:
        return self._content_count(question)

    def _question_images(self, question: Any | None = None) -> list[ImageContent]:
        return self.images.setdefault(self._key(question), [])

    def _other_answer(self, question: Any) -> str:
        value = self.otherValues.get(self._key(question), "").strip()
        if value:
            return f"{value} (Image attached)" if self._question_images(question) else value
        return "(Image attached)" if self._question_images(question) else ""

    def _answer_from_multi(self, question: Any) -> str:
        values = list(self.multiValues.get(self._key(question), []))
        other = self._other_answer(question)
        if other:
            values.append(other)
        return ", ".join(values)

    def _sync_multi_answer(self, question: Any) -> None:
        key = self._key(question)
        answer = self._answer_from_multi(question)
        if answer:
            self.answers[key] = answer
        else:
            self.answers.pop(key, None)

    def _annotations(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for question in self.questions:
            key = self._key(question)
            annotation: dict[str, str] = {}
            selected = self.answers.get(key)
            option = next((item for item in self._options(question) if _get(item, "label") == selected), None)
            if option is not None and _get(option, "preview"):
                annotation["preview"] = str(_get(option, "preview"))
            if self.notes.get(key, "").strip():
                annotation["notes"] = self.notes[key].strip()
            if annotation:
                result[key] = annotation
        return result

    def _finish(self, action: str) -> None:
        images = [] if action == "cancel" else [
            image.model_dump() for question in self.questions for image in self._question_images(question)
        ]
        result = {
            "action": action,
            "answers": dict(self.answers),
            "annotations": self._annotations(),
        }
        if images:
            result["images"] = images
        self.onDone(result)

    def _advance(self) -> None:
        if len(self.questions) == 1 and not self._is_multi():
            self._finish("submit")
            return
        self.current = min(len(self.questions), self.current + 1)
        self._end_input()
        self.warning = None

    def _choose(self, index: int) -> None:
        question = self._question()
        options = self._options(question)
        key = self._key(question)
        if index < len(options):
            label = str(_get(options[index], "label", ""))
            if self._is_multi(question):
                selected = self.multiValues.setdefault(key, [])
                selected.remove(label) if label in selected else selected.append(label)
                self._sync_multi_answer(question)
            else:
                self.answers[key] = label
                self._advance()
            return

        if not self._has_preview(question) and index == self._other_index(question):
            self._begin_input("other")
            return

        if self._is_multi(question) and index == self._submit_index(question):
            self._advance()

    def _editor_value(self) -> str:
        return self.editor.getExpandedText()

    def _input_changed(self) -> None:
        if self.inputMode is None or self.current >= len(self.questions):
            return
        key = self._key()
        value = self._editor_value()
        if self.inputMode == "notes":
            self.notes[key] = value
        else:
            self.otherValues[key] = value
            if self._is_multi():
                self._sync_multi_answer(self._question())

    def _begin_input(self, mode: str) -> None:
        self.inputMode = mode
        key = self._key()
        value = self.notes.get(key, "") if mode == "notes" else self.otherValues.get(key, "")
        self.editor.setText(value)
        self.editor.focused = self.focused
        self.imagesSelected = False
        self.warning = None

    def _end_input(self) -> None:
        if self.inputMode is not None:
            self._input_changed()
        self.inputMode = None
        self.imagesSelected = False
        self.editor.focused = False

    def _submit_input(self) -> None:
        if self.inputMode == "notes":
            self._end_input()
            return
        if self.inputMode != "other":
            return
        question = self._question()
        answer = self._other_answer(question)
        if not answer:
            self.warning = "Type an answer first."
            return
        if self._is_multi(question):
            self._sync_multi_answer(question)
            self._end_input()
        else:
            self.answers[self._key(question)] = answer
            self._end_input()
            self._advance()

    def _move_question(self, delta: int) -> None:
        self._end_input()
        hide_submit = len(self.questions) == 1 and not self._is_multi(self.questions[0])
        maximum = len(self.questions) - 1 if hide_submit else len(self.questions)
        self.current = max(0, min(maximum, self.current + delta))
        self.warning = None
        if self.current < len(self.questions):
            if self.focuses[self.current] == self._other_index() and not self._has_preview():
                self._begin_input("other")

    def _matches_app(self, data: str, action: str) -> bool:
        matcher = getattr(self.keybindings, "matches", None)
        return bool(callable(matcher) and matcher(data, action))

    def _spawn(self, coroutine: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coroutine)
        except RuntimeError:
            coroutine.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _open_external_editor(self) -> None:
        value = await edit_text_external(self.tui, self._editor_value())
        if value is not None and self.inputMode is not None:
            self.editor.setText(value)

    async def _paste_image(self) -> None:
        try:
            image = await read_clipboard_image()
        except Exception:
            image = None
        if image is None or self.inputMode != "other":
            self.warning = "No image found on the clipboard."
        else:
            self._question_images().append(ImageContent(
                data=base64.b64encode(image.bytes).decode("ascii"),
                mimeType=image.mimeType,
            ))
            self.warning = None
            if self._is_multi():
                self._sync_multi_answer(self._question())
        request_render = getattr(self.tui, "requestRender", None)
        if callable(request_render):
            request_render()

    def _handle_image_selection(self, data: str) -> bool:
        if not self.imagesSelected:
            return False
        kb = getKeybindings()
        images = self._question_images()
        if not images:
            self.imagesSelected = False
            return False
        if kb.matches(data, "tui.editor.cursorRight"):
            self.selectedImageIndex = (self.selectedImageIndex + 1) % len(images)
        elif kb.matches(data, "tui.editor.cursorLeft"):
            self.selectedImageIndex = (self.selectedImageIndex - 1) % len(images)
        elif kb.matches(data, "tui.editor.deleteCharBackward"):
            images.pop(self.selectedImageIndex)
            self.selectedImageIndex = min(self.selectedImageIndex, max(0, len(images) - 1))
            self.imagesSelected = bool(images)
            if self._is_multi():
                self._sync_multi_answer(self._question())
        elif kb.matches(data, "tui.select.cancel") or kb.matches(data, "tui.select.up"):
            self.imagesSelected = False
            self.editor.focused = self.focused
        return True

    def _handle_input_mode(self, data: str) -> None:
        kb = getKeybindings()
        if self._matches_app(data, "app.clipboard.pasteImage") and self.inputMode == "other":
            self._spawn(self._paste_image())
            return
        if self._matches_app(data, "app.editor.external") and (os.environ.get("VISUAL") or os.environ.get("EDITOR")):
            self._spawn(self._open_external_editor())
            return
        if self._handle_image_selection(data):
            return

        if self.inputMode == "other" and self._is_multi():
            if kb.matches(data, "tui.input.tab"):
                self._end_input()
                self.focuses[self.current] = self._submit_index() or 0
                return
            if matchesKey(data, "shift+tab"):
                self._end_input()
                self.focuses[self.current] = max(0, self._other_index() - 1)
                return

        if self.inputMode == "notes":
            if kb.matches(data, "tui.select.cancel"):
                self._end_input()
            elif kb.matches(data, "tui.select.confirm") or data == "\n":
                self._submit_input()
            else:
                self.editor.handleInput(data)
            return

        if kb.matches(data, "tui.select.cancel"):
            self._finish("cancel")
        elif kb.matches(data, "tui.select.up") or matchesKey(data, "ctrl+p"):
            self._end_input()
            self.focuses[self.current] = max(0, self._other_index() - 1)
        elif kb.matches(data, "tui.select.down") or matchesKey(data, "ctrl+n"):
            images = self._question_images()
            if images:
                self.imagesSelected = True
                self.selectedImageIndex = len(images) - 1
                self.editor.focused = False
            else:
                self._end_input()
                self.focuses[self.current] = self._submit_index() if self._is_multi() else self._chat_index()
        elif self._is_multi() and (matchesKey(data, "ctrl+enter") or matchesKey(data, "ctrl+return")):
            self._sync_multi_answer(self._question())
            self._end_input()
            self._advance()
        elif kb.matches(data, "tui.select.confirm") or data == "\n":
            self._submit_input()
        elif (kb.matches(data, "tui.editor.deleteCharBackward")
              and not self._editor_value() and self._question_images()):
            self._question_images().pop()
            if self._is_multi():
                self._sync_multi_answer(self._question())
        else:
            self.editor.handleInput(data)

    def handleInput(self, data: str) -> None:
        kb = getKeybindings()
        if self.inputMode is not None:
            self._handle_input_mode(data)
            return
        key = unicodedata.normalize("NFKC", data) if len(data) == 1 else data
        if kb.matches(data, "tui.select.cancel"):
            self._finish("cancel")
            return
        if kb.matches(data, "tui.input.tab"):
            if self.current < len(self.questions) and self._is_multi():
                focus = min(self._submit_index() or 0, self.focuses[self.current] + 1)
                self.focuses[self.current] = focus
                if focus == self._other_index():
                    self._begin_input("other")
            else:
                self._move_question(1)
            return
        if matchesKey(data, "shift+tab"):
            if self.current < len(self.questions) and self._is_multi():
                self.focuses[self.current] = max(0, self.focuses[self.current] - 1)
            else:
                self._move_question(-1)
            return
        if kb.matches(data, "tui.editor.cursorRight"):
            self._move_question(1)
            return
        if kb.matches(data, "tui.editor.cursorLeft"):
            self._move_question(-1)
            return

        if self.current == len(self.questions):
            if kb.matches(data, "tui.select.up"):
                self.submitFocus = max(0, self.submitFocus - 1)
            elif kb.matches(data, "tui.select.down"):
                self.submitFocus = min(1, self.submitFocus + 1)
            elif kb.matches(data, "tui.select.confirm") or data == "\n":
                self._finish("submit" if self.submitFocus == 0 else "cancel")
            elif key in {"1", "2"}:
                self._finish("submit" if key == "1" else "cancel")
            return

        question = self._question()
        focus = self.focuses[self.current]
        if self._has_preview(question):
            if kb.matches(data, "tui.select.up") or matchesKey(data, "ctrl+p"):
                self.focuses[self.current] = max(0, focus - 1)
            elif kb.matches(data, "tui.select.down") or matchesKey(data, "ctrl+n"):
                self.focuses[self.current] = min(self._chat_index(), focus + 1)
            elif kb.matches(data, "tui.select.confirm") or data == "\n":
                self._finish("clarify") if focus == self._chat_index() else self._choose(focus)
            elif key == "n":
                self._begin_input("notes")
            elif len(key) == 1 and key in "123456789":
                self.focuses[self.current] = min(len(self._options()) - 1, int(key) - 1)
            return

        if kb.matches(data, "tui.select.up") or matchesKey(data, "ctrl+p") or (self._is_multi() and key == "k"):
            self.focuses[self.current] = max(0, focus - 1)
            if self.focuses[self.current] == self._other_index():
                self._begin_input("other")
        elif kb.matches(data, "tui.select.down") or matchesKey(data, "ctrl+n") or (self._is_multi() and key == "j"):
            self.focuses[self.current] = min(self._chat_index(), focus + 1)
            if self.focuses[self.current] == self._other_index():
                self._begin_input("other")
        elif kb.matches(data, "tui.select.confirm") or data == "\n":
            self._finish("clarify") if focus == self._chat_index() else self._choose(focus)
        elif self._is_multi() and key == " " and focus < len(self._options()):
            self._choose(focus)
        elif len(key) == 1 and key in "123456789":
            index = int(key) - 1
            if index < len(self._options()):
                self._choose(index)
            elif index == self._other_index():
                self.focuses[self.current] = index
                self._begin_input("other")

    def _nav_line(self, width: int) -> str:
        hide_submit = len(self.questions) == 1 and not self._is_multi(self.questions[0])
        parts: list[str] = []
        for index, question in enumerate(self.questions):
            mark = "☑" if self.answers.get(self._key(question)) else "□"
            label = str(_get(question, "header", f"Q{index + 1}"))
            text = f" {mark} {label} "
            parts.append(theme.bg("selectedBg", theme.fg("text", text)) if index == self.current else theme.fg("muted", text))
        if not hide_submit:
            text = " ✓ Submit "
            parts.append(theme.bg("selectedBg", theme.fg("text", text)) if self.current == len(self.questions) else theme.fg("muted", text))
        arrows = len(self.questions) > 1 or not hide_submit
        line = " ".join(parts)
        return truncateToWidth(("← " if arrows else "") + line + (" →" if arrows else ""), width, "")

    def _inline_editor_lines(self, width: int) -> list[str]:
        rendered = self.editor.render(max(4, width))
        return rendered[1:-1] if len(rendered) > 2 else rendered

    def _attachment_lines(self, width: int) -> list[str]:
        images = self._question_images()
        if not images:
            return []
        labels = []
        for index, _image in enumerate(images):
            label = f"[Image {index + 1}]"
            labels.append(theme.fg("accent", label) if self.imagesSelected and index == self.selectedImageIndex else theme.fg("muted", label))
        suffix = "  ←/→ select · Backspace remove · Esc back" if self.imagesSelected else "  ↓ to select"
        return [truncateToWidth("     " + " ".join(labels) + theme.fg("muted", suffix), width, "")]

    def _option_lines(self, width: int, *, compact: bool = False) -> list[str]:
        question = self._question()
        options = self._options(question)
        focus = self.focuses[self.current]
        key = self._key(question)
        selected_multi = self.multiValues.get(key, [])
        lines: list[str] = []
        for index, option in enumerate(options):
            selected = focus == index
            label = str(_get(option, "label", ""))
            answered = not self._is_multi(question) and self.answers.get(key) == label
            check = "☑ " if self._is_multi(question) and label in selected_multi else "☐ " if self._is_multi(question) else ""
            pointer = theme.fg("accent", "→") if selected else " "
            body = f"{pointer} {index + 1}. {check}{label}{' ✓' if answered and compact else ''}"
            color = "success" if answered else "accent" if selected else "text"
            lines.append(theme.fg(color, body))
            description = str(_get(option, "description", "") or "")
            if description and not compact:
                description_color = "success" if answered else "muted"
                lines.extend(
                    theme.fg(description_color, "     " + part)
                    for part in wrapTextWithAnsi(description, max(1, width - 7))
                )

        if not self._has_preview(question):
            index = self._other_index(question)
            selected = focus == index
            pointer = theme.fg("accent", "→") if selected else " "
            value = self.otherValues.get(key, "")
            if selected and self.inputMode == "other":
                prefix = f"{pointer} {index + 1}. "
                editor_lines = self._inline_editor_lines(max(4, width - visibleWidth(prefix)))
                lines.append(prefix + (editor_lines[0] if editor_lines else ""))
                lines.extend(" " * visibleWidth(prefix) + line for line in editor_lines[1:])
            else:
                label = theme.fg("text" if value else "muted", value or "Type something.")
                lines.append(f"{pointer} {index + 1}. {label}")
            lines.extend(self._attachment_lines(width))

        if self._is_multi(question):
            index = self._submit_index(question)
            selected = focus == index
            pointer = theme.fg("accent", "→") if selected else " "
            label = "Submit" if len(self.questions) == 1 else "Next"
            lines.append(theme.fg("accent" if selected else "text", f"{pointer} {index + 1}. {label}"))
        return lines

    def _preview_box(self, content: str, width: int, max_lines: int) -> list[str]:
        width = max(12, width)
        inner = max(1, width - 4)
        rendered = Markdown(content or "No preview available", 0, 0, get_markdown_theme()).render(inner)
        hidden = max(0, len(rendered) - max_lines)
        rendered = rendered[:max_lines]
        border = theme.fg("borderMuted", "─" * (width - 2))
        lines = [theme.fg("borderMuted", "┌") + border + theme.fg("borderMuted", "┐")]
        for line in rendered:
            lines.append(theme.fg("borderMuted", "│ ") + _pad(line, inner) + theme.fg("borderMuted", " │"))
        if hidden:
            label = f"─── ✂ ─── {hidden} lines hidden "
            fill = "─" * max(0, width - 2 - visibleWidth(label))
            lines.append(theme.fg("warning", "├" + label + fill + "┤"))
        lines.append(theme.fg("borderMuted", "└") + border + theme.fg("borderMuted", "┘"))
        return lines

    def _preview_lines(self, width: int, target_height: int) -> list[str]:
        options = self._options()
        focus = min(self.focuses[self.current], len(options) - 1)
        preview = str(_get(options[focus], "preview", "") or "No preview available")
        left = self._option_lines(30, compact=True)
        notes = self.notes.get(self._key(), "")
        max_lines = max(1, target_height - 7)
        if width < 72:
            result = [*left, "", *self._preview_box(preview, width, max_lines)]
        else:
            left_width = min(30, max(20, width // 3))
            right_width = max(12, width - left_width - 3)
            right = self._preview_box(preview, right_width, max_lines)
            result = [_pad(a or "", left_width) + "   " + (b or "") for a, b in zip_longest(left, right)]
        if self.inputMode == "notes":
            editor_lines = self._inline_editor_lines(max(4, width - 7))
            result += [theme.fg("accent", "Notes: ") + (editor_lines[0] if editor_lines else "")]
            result += ["       " + line for line in editor_lines[1:]]
        else:
            result += [theme.fg("accent", "Notes: ") + theme.fg("muted", notes or "press n to add notes")]
        return result

    def _content_height(self, width: int) -> int:
        maximum = 5
        for question in self.questions:
            if self._has_preview(question):
                maximum = max(maximum, 10)
                continue
            height = 1 + (1 if self._is_multi(question) else 0)
            for option in self._options(question):
                height += 1
                description = str(_get(option, "description", "") or "")
                if description:
                    height += max(1, len(wrapTextWithAnsi(description, max(1, width - 7))))
            maximum = max(maximum, height)
        rows = int(getattr(getattr(self.tui, "terminal", None), "rows", 40) or 40)
        return min(maximum, max(5, rows - 15))

    def _question_lines(self, width: int) -> list[str]:
        question = self._question()
        target = self._content_height(width)
        content = self._preview_lines(width, target) if self._has_preview(question) else self._option_lines(width)
        content += [""] * max(0, target - len(content))
        lines = [theme.fg("text", theme.bold(str(_get(question, "question", "")))), "", *content]
        if self.warning:
            lines += [theme.fg("warning", f"⚠ {self.warning}")]
        lines += ["", theme.fg("borderMuted", "─" * max(1, width))]
        selected = self.focuses[self.current] == self._chat_index()
        number = "" if self._has_preview(question) else f"{self._chat_index() + 1}. "
        pointer = theme.fg("accent", "→") if selected else " "
        text = f"{pointer} {number}Chat about this"
        lines.append(theme.fg("accent", text) if selected else theme.fg("text", text))
        hint = "Enter select · ↑/↓ navigate"
        if len(self.questions) > 1:
            hint += " · Tab/Shift+Tab questions"
        if self._has_preview(question):
            hint += " · n notes"
        if self.inputMode is not None and (os.environ.get("VISUAL") or os.environ.get("EDITOR")):
            hint += " · Ctrl+G external editor"
        lines += ["", theme.fg("muted", hint + " · Esc cancel")]
        return lines

    def _submit_lines(self, width: int) -> list[str]:
        lines = [theme.fg("text", theme.bold("Review your answers")), ""]
        if len(self.answers) != len(self.questions):
            lines += [theme.fg("warning", "⚠ You have not answered all questions"), ""]
        for question in self.questions:
            key = self._key(question)
            if key in self.answers:
                lines += [theme.fg("text", f"• {key}"), theme.fg("success", f"  → {self.answers[key]}")]
        lines += ["", theme.fg("muted", "Ready to submit your answers?"), ""]
        for index, label in enumerate(("Submit answers", "Cancel")):
            selected = index == self.submitFocus
            pointer = theme.fg("accent", "→") if selected else " "
            text = f"{pointer} {index + 1}. {label}"
            lines.append(theme.fg("accent", text) if selected else theme.fg("text", text))
        lines += ["", theme.fg("muted", "Enter select · ↑/↓ navigate · ← back · Esc cancel")]
        return lines

    def render(self, width: int) -> list[str]:
        inner = max(1, width - 2)
        border = theme.fg("border", "─" * max(1, width))
        body = self._submit_lines(inner) if self.current == len(self.questions) else self._question_lines(inner)
        return [border, "", " " + self._nav_line(inner), "", *(" " + line for line in body), "", border]

    def invalidate(self) -> None:
        return None

    def dispose(self) -> None:
        self.editor.focused = False
        for task in list(self._tasks):
            task.cancel()


__all__ = ["AskUserQuestionComponent"]

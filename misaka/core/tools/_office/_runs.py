"""Rich text, and the hint for a model that wrote Markdown instead.

Ported from FrontierAgent's ``_writer_core.py`` ``_norm_runs`` / ``_md_hint``
(audit D78-D80).

Text passed to this tool is **literal**. A document is not a markdown file: ``**bold**``
written into a .docx paragraph produces a paragraph containing four asterisks, and the
model that wrote it has no way to see that from a success receipt. So formatting goes
through structured fields, and when the arguments still look like Markdown the receipt says
so -- without touching the content, because guessing which asterisks were meant as emphasis
and which are a footnote marker is how a writer corrupts a document.
"""
from __future__ import annotations

import re

# Each pattern names the parameter the model should have used instead. A hint that only
# says "this looks like Markdown" leaves the model to guess the replacement.
_MARKDOWN = [
    (re.compile(r"\*\*[^*\n]+\*\*"), "**bold** → a run's bold field"),
    (re.compile(r"\[[^\]\n]+\]\((?:https?|mailto):"), "[text](url) → a run's link field, or add_hyperlink"),
    (re.compile(r"(?m)^\s*(?:[-*]|\d+\.)\s+\S"), "a leading - or 1. → the list / body.items parameters"),
    (re.compile(r"(?m)^\s*#{1,6}\s+\S"), "# heading → a heading block with a level"),
    (re.compile(r"~~[^~\n]+~~"), "~~strike~~ → a run's strike field"),
]


def norm_runs(text):
    """Anything a caller may pass as rich text, as a list of run dicts.

    A string is one run; a list mixes plain strings and dicts; a dict without ``text`` is
    not a run and is dropped. The optional fields (bold, italic, underline, strike, color,
    size, font, link) are left for each format's writer to interpret -- a link is an OXML
    relationship in Word and an ``a:hlinkClick`` in PowerPoint, and neither belongs here.
    """
    if text is None or text == "":
        return []
    if isinstance(text, str):
        return [{"text": text}]
    if isinstance(text, dict):
        return [text] if text.get("text") is not None else []
    if isinstance(text, (list, tuple)):
        runs = []
        for item in text:
            if isinstance(item, str):
                runs.append({"text": item})
            elif isinstance(item, dict) and item.get("text") is not None:
                runs.append(item)
        return runs
    return [{"text": str(text)}]


def plain(runs):
    """The text of a run list, with no formatting -- for anchors and receipts."""
    return "".join(str(run.get("text", "")) for run in runs)


def _strings(value, out):
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, out)


def md_hint(args):
    """One receipt line when the arguments carry Markdown, or "".

    Advisory only. The content is written exactly as given either way.
    """
    found = []
    _strings(args, found)
    blob = "\n".join(found)
    hits = [message for pattern, message in _MARKDOWN if pattern.search(blob)]
    if not hits:
        return ""
    return ("hint: the text contains Markdown (" + "; ".join(hits)
            + "). This tool writes literally and does not parse it — use the parameters instead.")

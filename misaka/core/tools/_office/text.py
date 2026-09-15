"""Text deliverables: .txt .md .csv .tsv .json .jsonl .html.

Ported from FrontierAgent's ``plugins/tools/_writer_text.py`` (audit D82-D84). They are here
for symmetry: ``office`` is then the one tool that produces a deliverable, and a model
writing a report does not have to know that .docx goes one way and .md another -- nor fall
back to a shell heredoc, which is how a file ends up with a broken quote in row 400 and no
one finds out.

Content is written **literally**. For .md and .html the literal content is the formatting;
nothing here parses Markdown, and nothing escapes HTML.
"""
from __future__ import annotations

import csv as _csv
import io
import json
import os

from misaka.core.tools._office._receipt import result

SUFFIXES = frozenset({".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".html", ".htm"})
OPS = ("create", "append", "replace_text")


def _encode(suffix, args):
    """The text to write, or ``None`` when the call carries no content at all.

    Precedence is content > data > rows: a caller that passes a literal string meant that
    string, whatever else is in the arguments.
    """
    if args.get("content") is not None:
        content = args["content"]
        return content if isinstance(content, str) else json.dumps(
            content, ensure_ascii=False, indent=2)
    if args.get("data") is not None:
        data = args["data"]
        if suffix == ".jsonl":
            items = data if isinstance(data, (list, tuple)) else [data]
            return "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items)
        return json.dumps(data, ensure_ascii=False, indent=2)
    if args.get("rows") is not None:
        rows = args["rows"]
        if suffix in (".csv", ".tsv"):
            buffer = io.StringIO()
            writer = _csv.writer(buffer, delimiter="\t" if suffix == ".tsv" else ",",
                                 lineterminator="\n")
            for row in rows:
                writer.writerow(row if isinstance(row, (list, tuple)) else [row])
            return buffer.getvalue()
        if suffix == ".jsonl":
            return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        if suffix == ".json":
            # A .json file has to parse as JSON: rows concatenated line by line would not.
            return json.dumps(rows, ensure_ascii=False, indent=2)
        return "\n".join(row if isinstance(row, str)
                         else json.dumps(row, ensure_ascii=False) for row in rows)
    return None


def write(path, op, args, *, overwrite=False):
    """Apply one op to a text file. Returns a result dict or an ``[error]`` string."""
    suffix = os.path.splitext(path)[1].lower()

    if op == "create":
        if os.path.exists(path) and not (overwrite or args.get("overwrite")):
            return (f"[error] {path} already exists — use append or replace_text to edit it, "
                    'or pass "overwrite": true to rebuild it entirely.')
        content = _encode(suffix, args)
        if content is None:
            return ('[error] create needs one of "content" (a literal string), "data" (a JSON '
                    'object or array) or "rows" (csv/tsv rows, or jsonl objects).')
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return result(f"created {suffix.lstrip('.')}: {path}", counts={"line": lines})

    if op == "append":
        content = _encode(suffix, args)
        if content is None:
            return '[error] append needs one of "content", "data" or "rows".'
        separator = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                existing = handle.read()
            if existing and not existing.endswith("\n"):
                # Appending to a file with no final newline would otherwise join the last
                # existing line to the first new one.
                separator = "\n"
        else:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(separator + content)
        return result(f"appended to {path}")

    if op in ("replace_text", "replace"):
        find = args.get("find")
        if not find:
            return '[error] replace_text needs "find".'
        if not os.path.exists(path):
            return f"[error] {path} not found."
        with open(path, encoding="utf-8") as handle:
            existing = handle.read()
        available = existing.count(find)
        limit = args.get("count")
        # ``count: 0`` means replace nothing and has to be honoured literally; a truthiness
        # test here would fall through and replace every occurrence.
        updated = (existing.replace(find, args.get("replace", ""), int(limit))
                   if limit is not None else existing.replace(find, args.get("replace", "")))
        done = min(available, int(limit)) if limit is not None and int(limit) >= 0 else available
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(updated)
        if done == 0:
            if limit is not None and int(limit) == 0:
                return result(f"replace_text: 0 replacements requested for {find!r}; "
                              "file unchanged")
            return result(f"replace_text: 0 matches for {find!r}",
                          warn=f"no match for {find!r}")
        return result(f"replace_text: {done} replacement(s) in {path}")

    return (f"[error] unsupported op {op!r} for {suffix}: text formats support "
            f"{', '.join(OPS)}.")

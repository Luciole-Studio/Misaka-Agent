#!/usr/bin/env python3
"""Write ``misaka/codemode/runtime/prelude_source.py`` from pi's ``packages/codemode/src/runtime/prelude-source.ts``.

The prelude is JavaScript that runs inside the codemode VM. misaka keeps upstream's text character
for character: this script copies the template literal out of the TypeScript module, carries over
the module's documentation and its ``export const`` constants (with their comments), and splices the
constants into the template the way the template literal does.

    python scripts/gen_codemode_prelude.py --src path/to/pi/packages/codemode/src/runtime/prelude-source.ts

``--check`` reports whether the committed module already matches, and writes nothing.

A placeholder the script does not know (anything but ``${NAME}`` of a constant of the module and
``${JSON.stringify(NAME)}``) stops it: upstream changed how the template is built, and this script
has to be compared with it again first.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "misaka" / "codemode" / "runtime" / "prelude_source.py"
TEMPLATE_START = "export const PRELUDE_SOURCE: string = `"
TEMPLATE_END = "`;"


def _docText(block: str) -> str:
    """The text of a ``/** ... */`` block without its comment markers."""
    lines = []
    for line in block.strip().splitlines():
        line = line.strip()
        if line in ("/**", "*/"):
            continue
        lines.append(line[2:] if line.startswith("* ") else "" if line == "*" else line)
    return "\n".join(lines).strip()


def _parse(source: str) -> tuple[str, list[tuple[str, str, str, bool, bool]], str]:
    """The module documentation, the constants before the template as ``(name, value, doc, exported,
    afterBlankLine)``, and the template's JavaScript."""
    start = source.index(TEMPLATE_START)
    head, rest = source[:start], source[start + len(TEMPLATE_START):]
    end = rest.rindex(TEMPLATE_END)
    js = rest[:end].replace("\\\\", "\\").replace("\\`", "`")
    if "\\${" in js:
        raise SystemExit("the template escapes ${ ...}; compare this script with upstream first")
    match = re.match(r"\s*(/\*\*.*?\*/)", head, re.DOTALL)
    if match is None:
        raise SystemExit("prelude-source.ts has no leading documentation comment")
    doc = _docText(match.group(1))
    constants = []
    pending = ""
    blank = False
    previous = match.end()
    pattern = re.compile(r"(/\*\*.*?\*/)|(export\s+)?const\s+(\w+)\s*=\s*(.*?);", re.DOTALL)
    for found in pattern.finditer(head, match.end()):
        gap = head[previous:found.start()]
        previous = found.end()
        if found.group(1):
            pending, blank = _docText(found.group(1)), "\n\n" in gap
            continue
        constants.append((
            found.group(3),
            " ".join(found.group(4).split()),
            pending,
            bool(found.group(2)),
            blank or (not pending and "\n\n" in gap),
        ))
        pending, blank = "", False
    return doc, constants, js


def _pythonValue(value: str) -> str:
    if re.fullmatch(r"[\d_ *+]+", value):
        return value
    # A JavaScript string literal in either quote is a Python one: neither form uses escapes the other lacks.
    if re.fullmatch(r'"(?:[^"\\]|\\.)*"', value) or re.fullmatch(r"'(?:[^'\\]|\\.)*'", value):
        return f"(\n    {value}\n)"
    raise SystemExit(f"unexpected constant value: {value}")


def generate(source: str) -> str:
    doc, constants, js = _parse(source)
    if '"""' in js or js.endswith("\\"):
        raise SystemExit("the template cannot be written as a raw triple-quoted string")
    names = [name for name, *_ in constants]
    placeholders = sorted(set(re.findall(r"\$\{[^}]+\}", js)))
    replacements = []
    for placeholder in placeholders:
        inner = placeholder[2:-1]
        stringified = re.fullmatch(r"JSON\.stringify\((\w+)\)", inner)
        if inner in names:
            replacements.append((names.index(inner), f'.replace("{placeholder}", str({inner}))'))
        elif stringified and stringified.group(1) in names:
            replacements.append((names.index(stringified.group(1)), f'.replace("{placeholder}", jsonStringify({stringified.group(1)}))'))
        else:
            raise SystemExit(f"unknown placeholder {placeholder}; compare this script with upstream first")
    chain = "\n    ".join(text for _index, text in sorted(replacements))
    lines: list[str] = []
    for name, value, comment, _exported, afterBlankLine in constants:
        if afterBlankLine and lines:
            lines.append("")
        lines.extend(f"# {line}" if line else "#" for line in comment.splitlines())
        lines.append(f"{name} = {_pythonValue(value)}")
    exported = sorted({*(name for name, _value, _doc, isExported, _blank in constants if isExported), "PRELUDE_SOURCE"})
    return f'''"""Translated from pi's ``packages/codemode/src/runtime/prelude-source.ts``. The JavaScript below is
upstream's, character for character; only the way the constants are spliced in is Python's.
Written by ``scripts/gen_codemode_prelude.py``; regenerate it rather than editing it.

{doc}
"""

from __future__ import annotations

from misaka.utils.js import jsonStringify

{chr(10).join(lines)}

_TEMPLATE = r"""{js}"""

PRELUDE_SOURCE: str = (
    _TEMPLATE{chain}
)

__all__ = [{", ".join(f'"{name}"' for name in exported)}]
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--src", required=True, type=Path, help="path to pi's prelude-source.ts")
    parser.add_argument("--check", action="store_true", help="report whether the module matches, write nothing")
    args = parser.parse_args()
    module = generate(args.src.read_text(encoding="utf-8"))
    current = OUTPUT.read_bytes().decode("utf-8") if OUTPUT.exists() else None
    if args.check:
        print("up to date" if current == module else f"{OUTPUT} differs from {args.src}")
        return 0 if current == module else 1
    OUTPUT.write_bytes(module.encode("utf-8"))
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

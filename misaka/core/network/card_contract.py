"""The filename part of a card contract, separate from its prose requirements."""
import re


def split_deliverable(value):
    """Accept one basename on the first line: bare, in backticks, or followed by a colon and requirements.

    Do not fish filenames out of arbitrary prose: an ambiguous contract must be
    corrected before dispatch, rather than silently waiving the completion gate.
    """
    lines = str(value or "").strip().splitlines()
    if not lines:
        raise ValueError("Invalid deliverable: name one file, for example `findings.md`.")
    first, *rest = lines
    quoted = re.fullmatch(r"`([^`]+)`(?:[ \t]*[:：][ \t]*(.*)|[ \t]*)", first)
    described = re.fullmatch(r"([^\s/:：`\\]+\.[A-Za-z0-9]{1,16})[ \t]*[:：][ \t]*(.*)", first)
    if quoted or described:
        name, detail = (quoted or described).groups()
    else:
        name, detail = first, ""
        if any(char.isspace() for char in name) and not re.fullmatch(r"[^`]+\.[A-Za-z0-9]{1,16}", name):
            raise ValueError("Invalid deliverable: put one filename on its own line, in backticks if it contains spaces.")
    if (not name or name in {".", ".."} or name != name.strip()
            or any(char in '/\\:：`' or ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError("Invalid deliverable: use a filename, not a directory or absolute path.")
    return name, "\n".join(([detail] if detail else []) + rest).strip()


def format_deliverable(value):
    """Canonical card form: an explicit filename line, then unchanged requirements."""
    name, detail = split_deliverable(value)
    return f"`{name}`" + (f"\n{detail}" if detail else "")

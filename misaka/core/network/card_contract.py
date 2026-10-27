"""What a card contract holds besides its prose: its deliverable's filename, and the findings a card
declares as it works."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Finding(BaseModel):
    """One finding a card declares (``misaka_card_note``): what it found, what kind of claim that is,
    and where it comes from."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    claim_type: Literal["fact", "inference", "interpretation", "normative"] = "fact"
    quote: str = ""
    source_file: str | None = Field(None, description="Source path or locator, if applicable.")
    doc_id: str | None = None
    page: int | None = Field(None, ge=1, strict=True)


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
    if any(char in '/\\' for char in name) or name in {".", ".."}:
        raise ValueError("Invalid deliverable: use a filename, not a directory or absolute path.")
    if (not name or name != name.strip()
            or any(char in ':：`' or ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError("Invalid deliverable: its first line must be the file's name, such as `memo.md`, with the "
                         f"requirements on the lines after it; the first line reads {name[:60]!r}.")
    return name, "\n".join(([detail] if detail else []) + rest).strip()


# A file extension where a name ends: a dot and a letter, not followed by more of a filename.
# `v1.2 计划.md` and `archive.tar.gz` have one; `credential-sim.md 与 credential-sim.py` has two.
_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,15}(?![A-Za-z0-9_.-])")


def one_file(name):
    """Refuse a deliverable name that names several files. ``split_deliverable`` takes a backticked
    line whole, so `credential-sim.md 与 credential-sim.py` was one name no file could match, and
    eleven cards could never complete (2026-10-24, R52). Checked where a contract is written, not
    where it is read: a finished card woken by a message completes again under the contract it
    already has."""
    if len(_EXTENSION.findall(name)) > 1:
        raise ValueError(f"Invalid deliverable: {name[:80]!r} names more than one file. A card delivers one file: "
                         "put its name on the first line, and the other files it writes (a script, data) on the "
                         "lines after it.")
    return name


def format_deliverable(value):
    """Canonical card form: an explicit filename line, then unchanged requirements."""
    name, detail = split_deliverable(value)
    return f"`{name}`" + (f"\n{detail}" if detail else "")

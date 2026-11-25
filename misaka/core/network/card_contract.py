"""What a card contract holds besides its prose: its deliverable's filename, and the findings a card
declares as it works."""
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LocalWork(BaseModel):
    """An opt-in card that can do its work with named local files."""

    model_config = ConfigDict(extra="forbid")
    input_paths: list[str] = Field(min_length=1, description=(
        "Existing files, relative to the project. List every input the card needs."))
    tools: list[Literal["read", "write", "edit"]] = Field(default_factory=lambda: ["read", "write"], description=(
        "Include read and write. Add edit only when needed. Card notes and completion are supplied automatically."))

    @field_validator("input_paths")
    @classmethod
    def _relative_files(cls, paths):
        normalized = []
        for name in paths:
            path = PureWindowsPath(name)
            posix = path.as_posix()
            if (not path.parts or path.anchor or ".." in path.parts or ":" in name
                    or name.startswith("~") or posix.startswith("~")
                    or name != name.strip() or posix != posix.strip()
                    or any(ord(char) < 32 or char == "`" for char in name)):
                raise ValueError(f"Local work input {name!r} must be a project-relative file path.")
            normalized.append(posix)
        return list(dict.fromkeys(normalized))

    @field_validator("tools")
    @classmethod
    def _required_tools(cls, tools):
        if not {"read", "write"} <= set(tools):
            raise ValueError("Local work requires the read and write tools.")
        return list(dict.fromkeys(tools))

    def resolve_inputs(self, workspace):
        from misaka.core.web.evidence import check_material_read

        root = Path(workspace).resolve(strict=True)
        paths = []
        for name in self.input_paths:
            try:
                path = (root / name).resolve(strict=True)
                relative = path.relative_to(root)
                if relative.parts and relative.parts[0].casefold() == ".git":
                    raise ValueError("expected project material outside .git")
                if not path.is_file():
                    raise ValueError("expected a regular file")
                check_material_read(str(path))
            except (OSError, RuntimeError, ValueError) as error:
                raise ValueError(f"Local work input {name!r} must be an existing file inside the project.") from error
            paths.append(str(path))
        return tuple(dict.fromkeys(paths))

    def tool_names(self):
        return (*self.tools, "misaka_card_note", "misaka_card_complete")


def local_deliverable(name):
    from misaka.core.documents.workspace import SOURCE_DIRECTORY, SOURCE_MANIFEST
    from misaka.core.tools.write import _refuse_office_package
    from misaka.utils.node_path import WINDOWS_RESERVED_NAMES

    if name is None:
        raise ValueError("Local work requires one named deliverable.")
    one_file(name)
    device = name.split(".", 1)[0].rstrip().upper()
    if (name.endswith((".", " ")) or any(char in '<>"|?*' for char in name)
            or device in WINDOWS_RESERVED_NAMES or device in {"CONIN$", "CONOUT$"}
            or name.casefold() in {SOURCE_MANIFEST.casefold(), SOURCE_DIRECTORY.casefold()}):
        raise ValueError(f"Local work deliverable {name!r} is not a writable filename for this card.")
    try:
        _refuse_office_package(name, "write")
    except RuntimeError as error:
        raise ValueError(f"Local work deliverable {name!r}: {error}") from error
    return name


def local_output(workspace, output_dir, name, inputs):
    """Validate the writable entry before any scoped operation uses it."""
    output = Path(output_dir) / name
    try:
        output.relative_to(Path(workspace).resolve(strict=True))
        if output.resolve() != output or output in map(Path, inputs):
            raise ValueError("the deliverable is an alias or a declared input")
        if output.exists() and (not output.is_file() or not os.access(output, os.W_OK)):
            raise ValueError("the deliverable is not a writable regular file")
        ancestor = next(parent for parent in output.parents if parent.exists())
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
            raise ValueError("the output directory is not writable")
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError(f"Local work deliverable {name!r} must be a separate writable file inside the project.") from error
    return output


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


_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,15}(?![A-Za-z0-9_.-])")


def one_file(name):
    """Refuse a deliverable name that names several files. ``split_deliverable`` takes a backticked
    line whole, so `credential-sim.md 与 credential-sim.py` was one name no file could match, and
    eleven cards could never complete (2026-10-24, R52). Checked where a contract is written, not
    where it is read: a card reworked or reopened completes again under the contract it already
    has."""
    if len(_EXTENSION.findall(name)) > 1:
        raise ValueError(f"Invalid deliverable: {name[:80]!r} names more than one file. A card delivers one file: "
                         "put its name on the first line, and the other files it writes (a script, data) on the "
                         "lines after it.")
    return name


def format_deliverable(value):
    """Canonical card form: an explicit filename line, then unchanged requirements."""
    name, detail = split_deliverable(value)
    return f"`{name}`" + (f"\n{detail}" if detail else "")

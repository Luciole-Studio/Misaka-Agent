# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / tools/skill_manager_tool.py; see PROVENANCE.json and LICENSE.
import re
from typing import Optional
import yaml
from .metadata import SKILL_PROMPT_DESC_LIMIT

def _display_create_dir():
    return "the current role skills directory"

MAX_NAME_LENGTH = 64


MAX_DESCRIPTION_LENGTH = 1024


MAX_SKILL_CONTENT_CHARS = 100_000   # ~36k tokens at 2.75 chars/token


MAX_SKILL_FILE_BYTES = 1_048_576    # 1 MiB per supporting file


VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')  # filesystem-safe, URL-friendly


ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}  # for write_file/remove_file


_FRONTMATTER_END_RE = re.compile(r'\n---\s*\n')


_NAME_RULE = "Use lowercase letters, numbers, hyphens, dots, and underscores."


def _check_identifier(value: str, label: str, invalid: str) -> Optional[str]:
    if len(value) > MAX_NAME_LENGTH:
        return f"{label} exceeds {MAX_NAME_LENGTH} characters."
    return None if VALID_NAME_RE.match(value) else invalid


def _validate_name(name: str) -> Optional[str]:
    if not name:
        return "Skill name is required."
    return _check_identifier(
        name, "Skill name", f"Invalid skill name '{name}'. {_NAME_RULE} Must start with a letter or digit.")


def _validate_category(category: Optional[str]) -> Optional[str]:
    if category is None or (isinstance(category, str) and not category.strip()):
        return None
    if not isinstance(category, str):
        return "Category must be a string."
    category = category.strip()
    invalid = (f"Invalid category '{category}'. {_NAME_RULE} "
               "Categories must be a single directory name.")
    if "/" in category or "\\" in category:
        return invalid
    return _check_identifier(category, "Category", invalid)


def _validate_frontmatter(content: str, *, new_skill: bool = False) -> Optional[str]:
    """Validate frontmatter (name + description) and a non-empty body. ``new_skill`` (create
    only) also enforces SKILL_PROMPT_DESC_LIMIT so new skills never lose routing signal to
    index truncation; edit/patch skip it so existing over-limit skills stay maintainable."""
    if not content.strip():
        return "Content cannot be empty."
    content = content.lstrip("\ufeff")  # tolerate a Windows UTF-8 BOM
    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."
    end_match = _FRONTMATTER_END_RE.search(content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."
    try:
        parsed = yaml.safe_load(content[3:end_match.start() + 3])
    except yaml.YAMLError as e:
        return f"YAML frontmatter parse error: {e}"
    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."
    for field in ("name", "description"):
        if field not in parsed:
            return f"Frontmatter must include '{field}' field."
    desc = str(parsed["description"])
    if len(desc) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."
    if new_skill and len(desc.strip().strip("'\"")) > SKILL_PROMPT_DESC_LIMIT:
        return (
            f"Description is {len(desc.strip())} chars — new skills must fit the "
            f"{SKILL_PROMPT_DESC_LIMIT}-char system-prompt budget (one sentence, trigger first, "
            f"ends with a period). The skill index truncates longer descriptions to "
            f"{SKILL_PROMPT_DESC_LIMIT - 3} chars + '...', destroying the routing signal. "
            f"Move detail into the skill body.")
    if not content[end_match.end() + 3:].strip():
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."
    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> Optional[str]:
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters (limit: {MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into a smaller SKILL.md with supporting files in references/ "
            f"or templates/.")
    return None


def _clip(text: str, n: int, ellipsis: str) -> str:
    return text[:n] + (ellipsis if len(text) > n else "")


SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    # ONE advertised call shape (memory-tool pattern): the call IS an operations
    # array. The legacy flat shape (top-level action/name/content/...) is still
    # ACCEPTED for old transcripts and staged-write replay, but not advertised.
    "description": (
        "Create, update, or delete skills — your procedural memory for "
        "recurring task types. The call is an operations array (a single "
        "edit is a list of one); it applies atomically — any failure rolls "
        "every touched skill back. Ops: create (full SKILL.md; lands in "
        f"{_display_create_dir()}; must precede that skill's other "
        "ops), patch (targeted old_string/new_string fix — preferred; "
        "content alone REPLACES the whole file, read it via skill_view() "
        "first), write_file/remove_file (supporting files), delete (sole "
        "op only). Existing skills are modified wherever they live. Keep "
        "the description's first 57 chars a self-contained trigger: 'Use "
        "when <trigger>. <one-line behavior>.' Write lessons, not logs: "
        "imperative rule + why, no PR numbers/dates/incident narration, one "
        "rule per lesson, references/ named by topic (extend before adding). "
        "skill_view() shows format conventions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "description": "Ordered ops; each names its target skill.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Skill name (lowercase, hyphens/underscores, "
                                "max 64 chars); an existing skill's name "
                                "unless creating."
                            )
                        },
                        "action": {
                            "type": "string",
                            "enum": ["create", "patch", "delete", "write_file", "remove_file"]
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "Full SKILL.md text (YAML frontmatter + "
                                "markdown body) for create, or a full "
                                "rewrite on patch."
                            )
                        },
                        "category": {
                            "type": "string",
                            "description": "Optional category subdir for create (e.g. 'devops')."
                        },
                        # patch args: same fuzzy-matching semantics as the
                        # `patch` tool — teach only skill-specific facts here.
                        "old_string": {
                            "type": "string",
                            "description": "Text to find (patch; same matching semantics as the patch tool)."
                        },
                        "new_string": {
                            "type": "string",
                            "description": "Replacement (patch); empty string deletes the match."
                        },
                        "replace_all": {
                            "type": "boolean",
                            "description": "patch: replace all occurrences (default false)."
                        },
                        "file_path": {
                            "type": "string",
                            "description": (
                                "Path RELATIVE to the skill's own directory, "
                                "e.g. 'references/api.md' — no leading slash, "
                                "never absolute. write_file/remove_file: "
                                "required; first segment references/, "
                                "templates/, scripts/, or assets/. patch: "
                                "optional (default SKILL.md)."
                            )
                        },
                        "file_content": {
                            "type": "string",
                            "description": "Content for write_file."
                        }
                    },
                    "required": ["name", "action"]
                }
            },
            # Also accepted, never advertised: the legacy flat single-op fields, and
            # `absorbed_into` on delete ops (curator-only vocabulary; the curator's
            # prompt documents it and the delete guard's error re-teaches it).
        },
        "required": ["operations"],
    },
}


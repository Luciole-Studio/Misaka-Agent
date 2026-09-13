# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / agent/skill_commands.py; see PROVENANCE.json and LICENSE.
import re
from pathlib import Path
from typing import Any, Dict, Optional

_SKILL_INVALID_CHARS = re.compile(r"[^a-z0-9-]")


_SKILL_MULTI_HYPHEN = re.compile(r"-{2,}")


_SKILL_INVOCATION_PREFIX = "[IMPORTANT: The user has invoked the "


_SINGLE_SKILL_MARKER = "The full skill content is loaded below.]"


_SINGLE_SKILL_INSTRUCTION = "The user has provided the following instruction alongside the skill invocation: "


_RUNTIME_NOTE = "\n\n[Runtime note:"


_BUNDLE_MARKER = " skill bundle,"


_BUNDLE_USER_INSTRUCTION = "\nUser instruction: "


_BUNDLE_FIRST_SKILL_BLOCK = "\n\n[Loaded as part of the "


_SKILL_NAME_RE = re.compile(re.escape(_SKILL_INVOCATION_PREFIX) + r'"([^"]*)"')


SKILL_SCAFFOLD_SQL_LIKE = _SKILL_INVOCATION_PREFIX + "%"


SKILL_EXCERPT_JOINT = "\x1e"


def slugify_skill_name(name: str) -> str:
    """Normalize a skill/bundle name to a ``/command`` slug (``Foo Bar`` -> ``foo-bar``);
    strips chars (``+``, ``/``) that would make invalid Telegram command names."""
    cmd = _SKILL_INVALID_CHARS.sub("", name.lower().replace(" ", "-").replace("_", "-"))
    return _SKILL_MULTI_HYPHEN.sub("-", cmd).strip("-")


def append_user_instruction(parts: list, instruction: str) -> str:
    """Append the instruction line to ``parts``; return the stable prefix, which
    ends exactly at the instruction marker so (registered with
    ``agent.prompt_cache_boundary``) the cache planner can break on the scaffold.
    Single construction site guarantees the prefix is a byte-prefix of the message.

    Shared by every builder that ends a static skill scaffold with the caller-supplied volatile instruction
    (single-skill invocations, cron job prompts). Keeping construction in one place guarantees the
    registered prefix stays a byte-prefix of the built message — the invariant the request-time split
    depends on. See #81867.
    """
    stable_prefix = "\n".join(parts) + "\n" + _SINGLE_SKILL_INSTRUCTION
    parts.append(f"{_SINGLE_SKILL_INSTRUCTION}{instruction}")
    return stable_prefix


def extract_user_instruction_from_skill_message(content: Any) -> Optional[str]:
    """Recover the user's instruction from a slash-skill-expanded turn: the
    string unchanged when it is NOT scaffolding, the extracted instruction when
    the scaffolding carried one, or ``None`` for a bare ``/skill`` invocation."""
    if not isinstance(content, str):
        return None
    if not content.startswith(_SKILL_INVOCATION_PREFIX):
        return content
    if _BUNDLE_MARKER in content:
        # Bundles put the instruction before the loaded skills: FIRST marker is the user's.
        return _cut_after(content, _BUNDLE_USER_INSTRUCTION, _BUNDLE_FIRST_SKILL_BLOCK, content.find)
    if _SINGLE_SKILL_MARKER in content:
        # The instruction follows the skill body (which may quote the marker): LAST marker is the user's.
        return _cut_after(content, _SINGLE_SKILL_INSTRUCTION, _RUNTIME_NOTE, content.rfind)
    return None


def describe_skill_invocation(content: Any, separator: str = " — ") -> Optional[str]:
    """Render a slash-skill-expanded turn the way the user typed it:
    ``"/work — fix the title leak"``, ``"/work"`` for a bare invocation, or
    ``None`` when *content* is not scaffolding. ``separator=" "`` gives the
    literal invocation as typed (chat transcripts)."""
    if not isinstance(content, str) or not content.startswith(_SKILL_INVOCATION_PREFIX):
        return None
    match = _SKILL_NAME_RE.match(content)
    name = (match.group(1) if match else "").strip()
    # Bundle headers already carry their typed "/a /b" keys; a single skill is a bare name.
    label = name if name.startswith("/") else f"/{name}"
    instruction = extract_user_instruction_from_skill_message(content)
    if instruction and instruction is not content:
        # An excerpt (head + tail joined by SKILL_EXCERPT_JOINT) can put the
        # joint inside the span — keep only the side the marker was found on.
        instruction = " ".join(instruction.split(SKILL_EXCERPT_JOINT)[0].split())
        if instruction:
            return f"{label}{separator}{instruction}" if name else instruction
    return label if name else None


def _cut_after(message: str, marker: str, stop_marker: str, find) -> Optional[str]:
    """Text between *marker* (located with ``find``) and *stop_marker*, stripped; None if absent/empty."""
    marker_idx = find(marker)
    if marker_idx < 0:
        return None
    return message[marker_idx + len(marker):].split(stop_marker, 1)[0].strip() or None


_SKILL_DIR_NOTE = (
    "Resolve any relative paths in this skill (e.g. `scripts/foo.js`, "
    "`templates/config.yaml`) against that directory, then run them "
    "with the terminal tool using the absolute path."
)


_SETUP_SKIPPED_NOTE = (
    "Required environment setup was skipped. Continue loading the skill "
    "and explain any reduced functionality if it matters."
)


def _setup_note(loaded_skill: dict[str, Any]) -> Optional[str]:
    if loaded_skill.get("setup_skipped"):
        return _SETUP_SKIPPED_NOTE
    return loaded_skill.get("gateway_setup_hint") or (
        loaded_skill.get("setup_note") if loaded_skill.get("setup_needed") else None
    ) or None


def _build_skill_message(
    loaded_skill: dict[str, Any],
    skill_dir: Path | None,
    activation_note: str,
    user_instruction: str = "",
    runtime_note: str = "",
    session_id: str | None = None,
    *, preprocess, inject_config, support_files, skill_view_target: str, skill_view_source=None, register_prefix=None,
) -> str:
    """Format a loaded skill into a user/system message payload."""
    # Preprocess first so downstream blocks see the expanded content.
    content = preprocess(str(loaded_skill.get("content") or ""), skill_dir, session_id)
    parts = [activation_note, "", content.strip()]
    # Absolute skill dir lets the agent run bundled scripts without a skill_view() round-trip.
    if skill_dir:
        parts += ["", f"[Skill directory: {skill_dir}]", _SKILL_DIR_NOTE]
    inject_config(loaded_skill, parts)
    setup_note = _setup_note(loaded_skill)
    if setup_note:
        parts += ["", f"[Skill setup note: {setup_note}]"]
    supporting = support_files
    if supporting and skill_dir:
        parts += ["", "[This skill has supporting files (paths relative to the skill directory above):]"]
        parts += [f"- {sf}" for sf in supporting]
        parts.append(
            f'\nLoad any of these with skill_view(name="{skill_view_target}", '
            + (f'source={skill_view_source!r}, ' if skill_view_source else "") +
            f'file_path="<path>"), or run scripts directly by absolute path '
            f"(e.g. `node {skill_dir}/scripts/foo.js`)."
        )
    stable_prefix = None
    if user_instruction:
        parts.append("")
        # Everything before the volatile instruction is a stable scaffold; the
        # registered boundary lets the cache planner break there (see append_user_instruction).
        # Everything before the caller-supplied instruction is a stable scaffold; declare the exact boundary
        # so the Anthropic cache planner can put a breakpoint on it instead of caching the whole message as
        # one atomic block (#81867). The static instruction prose stays on the stable side; the volatile
        # instruction (webhook payload, ticket IDs, timestamps) and any runtime note ride in the tail.
        stable_prefix = append_user_instruction(parts, user_instruction)
    if runtime_note:
        parts += ["", f"[Runtime note: {runtime_note}]"]
    message = "\n".join(parts)
    if register_prefix is not None and stable_prefix is not None and message.startswith(stable_prefix) and len(message) > len(stable_prefix):
        register_prefix(stable_prefix)
    return message


def _scaffold_header(
    subject: str, loaded_names: list[str], *, lead_lines: list[str] | None = None,
    missing: list[str] | None = None, disabled: list[str] | None = None,
    extra_instruction: str = "", user_instruction: str = "",
) -> str:
    """Header for multi-skill messages (bundles and stacked invocations).
    ``subject`` must end in " skill bundle" so the bundle-format extractor applies."""
    lines = [
        f"[IMPORTANT: The user has invoked the {subject}, "
        f"loading {len(loaded_names)} skills together. Treat every skill below "
        "as active guidance for this turn.]",
        "",
        *(lead_lines or []),
        f"Skills loaded: {', '.join(loaded_names)}",
    ]
    if missing:
        lines.append(f"Skills missing (skipped): {', '.join(missing)}")
    if disabled:
        lines.append(f"Skills disabled for this platform (skipped): {', '.join(disabled)}")
    if extra_instruction:
        lines += ["", f"Bundle instruction: {extra_instruction}"]
    if user_instruction:
        lines += ["", f"User instruction: {user_instruction}"]
    return "\n".join(lines)


def diff_command_snapshots(before: Dict[str, str], after: Dict[str, str]) -> Dict[str, Any]:
    """Diff two {name: description} snapshots into added/removed/unchanged/total.
    Removed entries carry the pre-rescan description (the file may be gone)."""
    return {
        "added": [{"name": n, "description": after[n]} for n in sorted(set(after) - set(before))],
        "removed": [{"name": n, "description": before[n]} for n in sorted(set(before) - set(after))],
        "unchanged": sorted(set(after) & set(before)),
        "total": len(after),
    }


def command_snapshot(cmds: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """``{"/slug": info}`` -> ``{"slug": description}`` for diff_command_snapshots."""
    return {key.lstrip("/"): (info or {}).get("description") or "" for key, info in cmds.items()}


def resolve_slash_key(command: str, table: Dict[str, Any]) -> Optional[str]:
    """``command`` -> ``"/slug"`` when present in *table* (``_`` normalized to ``-``), else None."""
    if not command:
        return None
    cmd_key = f"/{command.replace('_', '-')}"
    return cmd_key if cmd_key in table else None


_MAX_STACKED_SKILLS = 5


def split_stacked_skill_commands(rest: str, resolve_skill_command_key) -> tuple[list[str], str]:
    """Consume further leading ``/skill`` tokens from *rest* (text after the first
    matched command); stops at the first non-skill (or repeated) token, which
    starts the user instruction. Returns ``(extra_cmd_keys, remaining_instruction)``."""
    keys: list[str] = []
    remaining = rest or ""
    while len(keys) < _MAX_STACKED_SKILLS - 1:
        stripped = remaining.lstrip()
        if not stripped.startswith("/"):
            break
        token, tail = (stripped.split(None, 1) + [""])[:2]
        cmd_key = resolve_skill_command_key(token.lstrip("/"))
        if cmd_key is None or cmd_key in keys:
            break
        keys.append(cmd_key)
        remaining = tail
    return keys, remaining.strip()


def _load_skill_blocks(
    identifiers: list[str], load, activation_note, task_id: str | None, *,
    render, missing_label=lambda ident: ident, disabled_names: set | None = None, disabled_as_missing: bool = False,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Load each distinct identifier via *load* and render its block; returns
    ``(loaded_names, missing, disabled, blocks)``. With *disabled_names*, members
    whose canonical (LOADED — identifiers may be paths) name or identifier is
    disabled go to ``disabled`` (or ``missing`` when *disabled_as_missing*)."""
    loaded_names: list[str] = []
    missing: list[str] = []
    disabled: list[str] = []
    blocks: list[str] = []
    seen: set[str] = set()
    for identifier in identifiers:
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        loaded = load(identifier)
        if not loaded:
            missing.append(missing_label(identifier))
            continue
        skill_name = loaded[2]
        if disabled_names and (skill_name in disabled_names or identifier in disabled_names):
            if disabled_as_missing:
                missing.append(identifier)
            else:
                disabled.append(skill_name or identifier)
            continue
        blocks.append(render(loaded, activation_note(skill_name), task_id))
        loaded_names.append(skill_name)
    return loaded_names, missing, disabled, blocks


def build_preloaded_skills_prompt(skill_identifiers: list[str], task_id: str | None = None, *, load_blocks, load_payload, disabled_names) -> tuple[str, list[str], list[str]]:
    """Load skills for session-wide CLI/TUI preloading; returns (prompt_text,
    loaded_skill_names, missing_identifiers). Disabled skills count as missing:
    this path bypasses the scan-time filter, and ``hermes -s <skill>`` must not
    force-load an operator-disabled skill.

    Disabled skills are treated the same as missing ones: this loads via a raw identifier straight into
    ``_load_skill_payload``, bypassing ``get_skill_commands()``'s scan-time disabled filter — mirrors the
    bundle-invocation gate (#59156).
    """
    loaded_names, missing, _disabled, prompt_parts = load_blocks(
        [(raw or "").strip() for raw in skill_identifiers],
        lambda identifier: load_payload(identifier, task_id=task_id),
        lambda name: (f'[IMPORTANT: The user launched this CLI session with the "{name}" skill '
                      "preloaded. Treat its instructions as active guidance for the duration of this "
                      "session unless the user overrides them.]"),
        task_id, disabled_names=disabled_names, disabled_as_missing=True,
    )
    return "\n\n".join(prompt_parts), loaded_names, missing


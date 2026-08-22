"""Progressive skill discovery, invocation, and managed mutation tools."""
import logging
import os
import re
from pathlib import Path

from misaka.skills.manage import lookup_path_error

logger = logging.getLogger(__name__)

_SKILL_INVALID_CHARS = re.compile(r"[^a-z0-9-]")
_SKILL_MULTI_HYPHEN = re.compile(r"-{2,}")
_SKILL_INVOCATION_PREFIX = "[IMPORTANT: The user has invoked the "
_SINGLE_SKILL_MARKER = "The full skill content is loaded below.]"
_SINGLE_SKILL_INSTRUCTION = (
    "The user has provided the following instruction alongside the skill invocation: "
)
_SUPPORT_SUBDIRS = ("references", "templates", "scripts", "assets")


def _slug(name):
    slug = name.lower().replace(" ", "-").replace("_", "-")
    slug = _SKILL_INVALID_CHARS.sub("", slug)
    return _SKILL_MULTI_HYPHEN.sub("-", slug).strip("-")


def scan_skill_commands(profile_dir, cwd=None):
    """Map slash-command slugs to skill info for every skill in the profile's stack."""
    from misaka.core.skills import load_skills_from_dir
    from misaka.skills.layers import skills_stack

    commands = {}
    seen_names = set()
    for skill_dir in skills_stack(profile_dir, cwd=cwd):
        try:
            result = load_skills_from_dir({"dir": str(skill_dir), "source": "misaka"})
        except Exception:  # noqa: BLE001 - one invalid skill must not hide the rest
            logger.warning("Failed to load skills from %s; skipping", skill_dir, exc_info=True)
            continue
        for skill in result.skills:
            if skill.name in seen_names:
                continue
            slug = _slug(skill.name)
            if not slug or slug in commands:
                continue
            seen_names.add(skill.name)
            commands[slug] = {"name": skill.name,
                              "description": skill.description or f"Invoke the {skill.name} skill",
                              "skill_md_path": skill.filePath,
                              "skill_dir": skill.baseDir}
    return commands


def build_skill_message(info, *, user_instruction="", session_id=None):
    """Build the complete skill activation message."""
    from misaka.skills.preprocessing import preprocess_skill_content
    from misaka.utils.frontmatter import parse_frontmatter

    skill_dir = Path(info["skill_dir"])
    raw = Path(info["skill_md_path"]).read_text(encoding="utf-8")
    body = (parse_frontmatter(raw).body or "").strip()
    content = preprocess_skill_content(body, skill_dir, session_id=session_id)

    activation_note = (f'{_SKILL_INVOCATION_PREFIX}"{info["name"]}" skill. '
                       f"{_SINGLE_SKILL_MARKER}")
    parts = [activation_note, "", content.strip()]

    parts.append("")
    parts.append(f"[Skill directory: {skill_dir}]")
    parts.append(
        "Resolve any relative paths in this skill (e.g. `scripts/foo.js`, "
        "`templates/config.yaml`) against that directory, then run them "
        "with the terminal tool using the absolute path.")

    supporting = []
    for subdir in _SUPPORT_SUBDIRS:
        subdir_path = skill_dir / subdir
        if subdir_path.exists():
            for f in sorted(subdir_path.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    supporting.append(str(f.relative_to(skill_dir)))
    if supporting:
        parts.append("")
        parts.append("[This skill has supporting files:]")
        for sf in supporting:
            parts.append(f"- {sf}  ->  {skill_dir / sf}")

    if user_instruction:
        parts.append("")
        parts.append(f"{_SINGLE_SKILL_INSTRUCTION}{user_instruction}")
    return "\n".join(parts)


_SUPPORT_DIRS = ("references", "templates", "assets", "scripts")


def collect_linked_files(skill_dir):
    """Return the skill's support files grouped by support directory."""
    root = Path(skill_dir)
    out = {}
    for category in _SUPPORT_DIRS:
        sub = root / category
        if not sub.is_dir():
            continue
        files = [str(f.relative_to(root)) for f in sorted(sub.rglob("*"))
                 if f.is_file() and not f.is_symlink()]
        if files:
            out[category] = files
    return out


def read_support_file(skill_dir, file_path):
    """Read one support file from inside the skill directory; return ``(text, error)``."""
    err = lookup_path_error(file_path)
    if err:
        return None, err
    root = Path(skill_dir).resolve()
    target = (root / file_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None, f"File is outside the skill directory: {file_path}"
    if not target.is_file():
        return None, f"Skill support file not found: {file_path}"
    try:
        data = target.read_bytes()
    except OSError as e:
        return None, str(e)
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return f"[Binary file: {target.name}, {len(data)} bytes]", None


def tools_for(profile_dir):
    """Build progressive-disclosure skill tools for one profile."""
    from pydantic import BaseModel, ConfigDict, Field

    from misaka.core.extensions.types import ToolDefinition

    class ListParams(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class ViewParams(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str = Field(description="Skill name from `skills_list`.")
        file_path: str = Field(
            "", description="Optional support-file path within the skill; omit to read SKILL.md.")

    def register(harn):
        async def list_execute(tool_call_id, raw, signal, on_update, ctx):
            commands = scan_skill_commands(profile_dir, cwd=os.getcwd())
            if not commands:
                return {"content": [{"type": "text", "text": "No skills are available."}]}
            lines = ["Available skills (use `skill_view` to load full instructions):"]
            for info in sorted(commands.values(), key=lambda i: i["name"]):
                lines.append(f"- {info['name']}: {info['description']}")
            return {"content": [{"type": "text", "text": "\n".join(lines)}],
                    "details": {"count": len(commands)}}

        harn.registerTool(ToolDefinition(
            name="skills_list", label="List skills",
            description="List available skills with a short description; use `skill_view` before applying one.",
            parameters=ListParams.model_json_schema(), execute=list_execute,
            promptSnippet='List the available skills'))

        async def view_execute(tool_call_id, raw, signal, on_update, ctx):
            args = raw if isinstance(raw, ViewParams) else ViewParams(**(raw or {}))
            err = lookup_path_error(args.name)
            if err:
                return {"content": [{"type": "text", "text": err}], "isError": True}
            commands = scan_skill_commands(profile_dir, cwd=os.getcwd())
            info = commands.get(_slug(args.name)) or next(
                (i for i in commands.values() if i["name"] == args.name.strip()), None)
            if info is None:
                names = ", ".join(sorted(i["name"] for i in commands.values())) or "none"
                return {"content": [{"type": "text",
                                     "text": f"Unknown skill '{args.name}'. Available: {names}"}],
                        "isError": True}
            if args.file_path:
                content, err = read_support_file(info["skill_dir"], args.file_path)
                if err:
                    return {"content": [{"type": "text", "text": err}], "isError": True}
                return {"content": [{"type": "text", "text": content}],
                        "details": {"skill": info["name"], "file": args.file_path}}
            try:
                sid = str(ctx.sessionManager.getSessionId())
            except Exception:  # noqa: BLE001 - a missing session ID should not block loading
                sid = None
            message = build_skill_message(info, session_id=sid)
            linked = collect_linked_files(info["skill_dir"])
            if linked:
                message += ('\n\n[Support files: load one with skill_view(name=..., file_path=...)]\n'
                            + "\n".join(f"- {c}: {', '.join(fs)}" for c, fs in linked.items()))
            return {"content": [{"type": "text", "text": message}],
                    "details": {"skill": info["name"], "linked_files": linked}}

        class ManageParams(BaseModel):
            model_config = ConfigDict(extra="forbid")
            action: str = Field(description="Operation: create, edit, patch, delete, write_file, or remove_file.")
            name: str = Field(description="Lowercase kebab-case skill name and directory name.")
            content: str = Field("", description="Complete SKILL.md text for create or edit.")
            file_path: str = Field("", description="Relative support-file path, or optional patch target.")
            file_content: str = Field("", description="Complete content for write_file.")
            old_string: str = Field("", description="Exact text to replace; must match uniquely unless replace_all is true.")
            new_string: str = Field("", description="Replacement text; use an empty string to remove the match.")
            replace_all: bool = Field(False, description="Replace every match instead of requiring one unique match.")
            absorbed_into: str = Field("", description="For delete, optional existing skill that absorbed this skill's useful content.")

        async def manage_execute(tool_call_id, raw, signal, on_update, ctx):
            from misaka.skills import manage as skill_manage
            args = raw if isinstance(raw, ManageParams) else ManageParams(**(raw or {}))
            result = skill_manage.manage(
                args.action, args.name, profile_dir=profile_dir,
                content=args.content or None, file_path=args.file_path or None,
                file_content=args.file_content if args.action == "write_file" else None,
                old_string=args.old_string or None,
                new_string=args.new_string if args.action == "patch" else None,
                replace_all=args.replace_all,
                absorbed_into=args.absorbed_into if args.action == "delete" else None)
            lines = [result.get("message") or result.get("error") or ""]
            for key in ("gist", "description_preview", "hint", "lint_hint"):
                if result.get(key):
                    lines.append(str(result[key]))
            for warning in result.get("lint_warnings") or []:
                lines.append(f"  ⚠ [{warning['rule']}] {warning['message']}")
            return {"content": [{"type": "text", "text": "\n".join(x for x in lines if x)}],
                    "isError": not result.get("success"),
                    "details": {k: v for k, v in result.items() if k != "message"}}

        harn.registerTool(ToolDefinition(
            name="skill_manage", label="Manage skills",
            description="Create, update, or delete a reusable skill; every change goes through approval, validation, the security scan, and the rollback ledger.",
            parameters=ManageParams.model_json_schema(), execute=manage_execute,
            promptSnippet="Create or update a reusable skill",
            promptGuidelines=[
                "New skill descriptions must be one sentence of at most 60 characters; put detail in the body.",
                "Use `skill_manage`, never generic file tools, for every skill mutation.",
                "When the user-controlled gate blocks a write, report it and do not seek a bypass.",
            ]))

        harn.registerTool(ToolDefinition(
            name="skill_view", label="View skill",
            description="Load a skill's processed SKILL.md and support-file index, or read one support file by relative path.",
            parameters=ViewParams.model_json_schema(), execute=view_execute,
            promptSnippet="Load a skill's full instructions",
            promptGuidelines=["When a task matches a skill description, load it with `skill_view` before acting."]))

    return register


def commands_for(profile_dir):
    """Build slash commands for a role's skill stack."""

    def register(harn):
        async def skill_cmd(args, ctx):
            raw = (args or "").strip()
            cwd = os.getcwd()
            commands = scan_skill_commands(profile_dir, cwd=cwd)
            if not raw:
                if not commands:
                    ctx.ui.notify("No skills are available in the current skill stack.", "info")
                    return
                lines = [f"/skill {slug} — {info['description']}"
                         for slug, info in sorted(commands.items())]
                ctx.ui.notify('Available skills:\n' + "\n".join(lines), "info")
                return
            name, _, instruction = raw.partition(" ")
            info = commands.get(_slug(name))
            if info is None:
                ctx.ui.notify(f"Unknown skill '{name}'. Available: {', '.join(sorted(commands))}",
                              "error")
                return
            try:
                sid = str(ctx.sessionManager.getSessionId())
            except Exception:  # noqa: BLE001 - a missing session ID should not block invocation
                sid = None
            message = build_skill_message(info, user_instruction=instruction.strip(),
                                          session_id=sid)
            await ctx.sendUserMessage(message)

        harn.registerCommand("skill", {
            "handler": skill_cmd,
            "description": "List skills, or invoke one with `/skill <name> [instruction]` in the current session."})

        async def learn_cmd(args, ctx):
            from misaka.skills.learn_prompt import build_learn_prompt
            from misaka.skills import write as skill_write
            target = os.path.join(profile_dir, "skills")
            os.makedirs(target, exist_ok=True)
            prompt = build_learn_prompt(args or "", target_dir=target)
            # Every write goes through the skill gate, validation, scan, and ledger.
            decision, note = skill_write.evaluate_gate()
            if decision == "off":
                ctx.ui.notify("Skill writing is disabled globally. Change the write mode before using /learn.", "error")
                return
            prompt += (
                "\n\n---\n[Write path] Use `skill_manage` for every skill change; never write the skill tree with generic file tools. "
                "Create a skill with `skill_manage(action='create', name=..., content=<complete SKILL.md>)` and add support files "
                "with `action='write_file'`. New descriptions must be one sentence of at most 60 characters; put detail in the body."
            )
            if decision == "stage":
                prompt += (
                    f"\n{note}\nThe tool will return a pending ID. Report the skill name and summary, then tell the user "
                    "to inspect it with `misaka skills pending` and approve it with `misaka skills approve <id>`."
                )
            await ctx.sendUserMessage(prompt)

        async def skill_mode_cmd(args, ctx):
            # Slash commands originate from user input, so this is the user-only write-mode entry point.
            from misaka.skills import layers as skill_layers, write as skill_write
            value = (args or "").strip().lower()
            if not value:
                mode = skill_write.write_mode()
                ctx.ui.notify(
                    f"Skill write mode: {mode}\nUse `/skill-mode off|forbid|allow` to change it.",
                    "info",
                )
                return
            if value not in skill_write.WRITE_MODES:
                ctx.ui.notify(f"Unknown skill write mode: {value}. Available: {', '.join(skill_write.WRITE_MODES)}",
                              "error")
                return
            cfg = skill_layers.load_skills_config()
            cfg["skill_write_mode"] = value
            skill_layers._write_skills_config(cfg)
            ctx.ui.notify(f"Skill write mode set to {value}; it takes effect immediately.", "info")

        harn.registerCommand("skill-mode", {
            "handler": skill_mode_cmd,
            "description": "Show or change the skill write mode: off, forbid (user review required), or allow."})

        harn.registerCommand("learn", {
            "handler": learn_cmd,
            "description": "Create or improve a reusable skill from files, links, notes, or the workflow just completed."})

    return register

SESSION_KINDS = {"foreground", "dm", "card"}


def activate(spec):
    commands, tools = commands_for(spec.profile_dir), tools_for(spec.profile_dir)

    def register(harn):
        commands(harn)
        tools(harn)
    return register

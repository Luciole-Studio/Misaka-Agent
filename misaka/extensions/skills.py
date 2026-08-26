"""Skills for one session: the index in the system prompt, ``skills_list`` /
``skill_view`` / ``skill_manage``, and the ``/skill``, ``/learn``, ``/skill-mode``
commands (hermes skills_tool + skill commands).

The engine has no skill loading of its own: this extension is the only
thing that decides which skills a session sees -- the role's three layers, or the
read-only sandbox a card runs against (``SessionSpec.skill_roots``).
"""
import os
from pathlib import Path

from misaka.skills import index as skill_index
from misaka.skills.layers import SKILL_SUPPORT_DIRS, skill_roots
from misaka.skills.manage import lookup_path_error

_SKILL_INVOCATION_PREFIX = "[IMPORTANT: The user has invoked the "
_SINGLE_SKILL_MARKER = "The full skill content is loaded below.]"
_SINGLE_SKILL_INSTRUCTION = (
    "The user has provided the following instruction alongside the skill invocation: "
)


def _body(entry, session_id=None):
    """SKILL.md's body with template variables and inline shell applied."""
    from misaka.skills.preprocessing import preprocess_skill_content
    from misaka.utils.frontmatter import parse_frontmatter

    raw = Path(entry["path"]).read_text(encoding="utf-8")
    body = (parse_frontmatter(raw).body or "").strip()
    return preprocess_skill_content(body, Path(entry["dir"]), session_id=session_id).strip()


def collect_linked_files(skill_dir):
    """The skill's support files grouped by support directory (symlinks never listed)."""
    root = Path(skill_dir)
    out = {}
    for category in sorted(SKILL_SUPPORT_DIRS):
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


def skill_content(entry, session_id=None):
    """What ``skill_view`` returns for SKILL.md (hermes: content plus linked_files): the
    processed body, the skill directory for relative paths, and the support-file index."""
    linked = collect_linked_files(entry["dir"])
    lines = [_body(entry, session_id), "", f"[Skill directory: {entry['dir']}]",
             "Resolve relative paths in the skill against that directory."]
    if linked:
        lines += ["", "[Linked files: load one with skill_view(name=..., file_path=...)]"]
        lines += [f"- {c}: {', '.join(fs)}" for c, fs in linked.items()]
    return "\n".join(lines), linked


def build_skill_message(entry, *, user_instruction="", session_id=None):
    """The ``/skill`` activation message: the processed skill plus the user's instruction."""
    skill_dir = Path(entry["dir"])
    parts = [f'{_SKILL_INVOCATION_PREFIX}"{entry["name"]}" skill. {_SINGLE_SKILL_MARKER}',
             "", _body(entry, session_id), "", f"[Skill directory: {skill_dir}]",
             ("Resolve any relative paths in this skill (e.g. `scripts/foo.js`, "
             "`templates/config.yaml`) against that directory, then run them "
             "with the terminal tool using the absolute path.")]
    supporting = [f for files in collect_linked_files(skill_dir).values() for f in files]
    if supporting:
        parts += ["", "[This skill has supporting files:]"]
        parts += [f"- {sf}  ->  {skill_dir / sf}" for sf in supporting]
    if user_instruction:
        parts += ["", f"{_SINGLE_SKILL_INSTRUCTION}{user_instruction}"]
    return "\n".join(parts)


def register_for(roots, profile_dir, cwd=None, kind="foreground"):
    """Everything skills for one session, against these layer roots; writes (``skill_manage``,
    ``/learn``) go to the role's own ``skills/`` under ``profile_dir``. ``cwd`` is the
    workspace the coding posture is judged in (misaka.skills.coding_context)."""
    from pydantic import BaseModel, ConfigDict, Field

    from misaka.core.extensions.types import ToolDefinition
    from misaka.skills.coding_context import compact_skill_categories

    roots = list(roots)
    workspace = cwd or os.getcwd()

    def entries():
        return skill_index.build(roots)

    def session_id(ctx):
        try:
            return str(ctx.sessionManager.getSessionId())
        except Exception:  # noqa: BLE001 - a missing session ID should not block loading
            return None

    class ListParams(BaseModel):
        model_config = ConfigDict(extra="forbid")
        category: str = Field("", description="Optional category filter to narrow results.")

    class ViewParams(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str = Field(description="The skill name (use skills_list to see available skills).")
        file_path: str = Field(
            "", description="OPTIONAL: Path to a linked file within the skill (e.g., 'references/api.md', "
                            "'templates/config.yaml', 'scripts/validate.py'). Omit to get the main SKILL.md content.")

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

    def register(harn):
        # ── the index in the system prompt (hermes build_skills_system_prompt) ──
        async def advertise(event, _ctx):
            skill_index.invalidate()          # skills edited outside this session (git, an editor) show next turn
            section = skill_index.render_prompt(entries(), skill_index.categories(roots),
                                                compact_skill_categories(workspace))
            if section:
                return {"systemPrompt": event["systemPrompt"].rstrip() + "\n\n" + section}

        async def fresh(_event, _ctx):
            skill_index.invalidate()

        harn.on("before_agent_start", advertise)
        harn.on("session_start", fresh)

        # Live skill trees change only through skill_manage (gate, scan, ledger): the generic file
        # and shell tools are refused on them in every kind of session. A card's sandbox copies are
        # not live trees, so reading them stays possible.
        live_roots = set()
        for _layer, root in skill_roots(profile_dir, workspace):
            live_roots.update({os.path.abspath(root), os.path.realpath(root)})

        def _touches_live_skills(tool, args):
            if tool in ("write", "edit"):
                target = os.path.realpath(os.path.join(workspace, os.path.expanduser(str(args.get("path") or ""))))
                return any(target == root or target.startswith(root + os.sep) for root in live_roots)
            if tool == "bash":
                command = str(args.get("command") or "")
                return any(root in command for root in live_roots)   # ponytail: a text match; the shell is not parsed
            return False

        async def guard_live_skills(event, _ctx):
            args = event.get("input") if isinstance(event, dict) else getattr(event, "input", None)
            tool = event.get("toolName") if isinstance(event, dict) else getattr(event, "toolName", "")
            if _touches_live_skills(str(tool or ""), args if isinstance(args, dict) else {}):
                return {"block": True, "reason": (
                    "Live skill trees change only through skill_manage (approval, scan, ledger). "
                    "write and edit are refused on paths inside them; bash is refused whenever the "
                    "command mentions one at all, reads included -- use skill_view to read a skill."
                )}
            return None

        harn.on("tool_call", guard_live_skills)

        # ── the [Skills] block on the startup screen ──
        # The engine loads no skills of its own, so it has no section to show; this one takes
        # that place (same name, same style) and shows the index instead: names collapsed, the
        # full category tree behind ctrl+o.
        from misaka.core.extensions import startup_sections

        def _dim(text):
            from misaka.ui.tui.interactive.theme.theme import theme
            return theme.fg("dim", text)

        startup_sections.register(
            "Skills",
            lambda: _dim("  " + (", ".join(sorted(e["name"] for e in entries())) or "(none)")),
            lambda: "\n".join(_dim(line) for line in
                              skill_index.index_lines(entries(), skill_index.categories(roots))
                              ) or _dim("  (none)"))

        # ── tools ──
        async def list_execute(tool_call_id, raw, signal, on_update, ctx):
            args = raw if isinstance(raw, ListParams) else ListParams(**(raw or {}))
            found = [e for e in entries() if not args.category or e["category"] == args.category]
            if not found:
                return {"content": [{"type": "text", "text": "No skills are available."}],
                        "details": {"count": 0, "categories": []}}
            text = ("Available skills (use skill_view(name) to load full content):\n"
                    + "\n".join(skill_index.index_lines(found, skill_index.categories(roots))))
            return {"content": [{"type": "text", "text": text}],
                    "details": {"count": len(found),
                                "categories": sorted({e["category"] for e in found})}}

        harn.registerTool(ToolDefinition(
            name="skills_list", label="List skills",
            description="List available skills (name + description). Use skill_view(name) to load full content.",
            parameters=ListParams.model_json_schema(), execute=list_execute,
            promptSnippet="List the available skills"))

        async def view_execute(tool_call_id, raw, signal, on_update, ctx):
            args = raw if isinstance(raw, ViewParams) else ViewParams(**(raw or {}))
            err = lookup_path_error(args.name)
            if err:
                return {"content": [{"type": "text", "text": err}], "isError": True}
            # hermes collision rule: a bare name that several layers claim is refused rather
            # than guessed -- unless one of them is the project's, which overrides on purpose.
            found = skill_index.candidates(roots, args.name)
            exact = [e for e in found if e["rel"] == args.name.strip()]   # the directory path inside a layer
            found = exact or found
            found = [e for e in found if e["layer"] == "project"] or found   # the index's promise: project shadows the rest
            if len({e["dir"] for e in found}) > 1:
                paths = "; ".join(e["path"] for e in found)
                return {"content": [{"type": "text", "text": (
                    f"Ambiguous skill name '{args.name}': {len(found)} skills match across your "
                    f"layers. Refusing to guess — pass the skill's path inside its layer instead "
                    f"of the bare name (e.g. 'category/skill-name'). Matches: {paths}")}],
                        "isError": True}
            entry = found[0] if found else None
            if entry is None:
                names = ", ".join(sorted(e["name"] for e in entries())) or "none"
                return {"content": [{"type": "text",
                                     "text": f"Unknown skill '{args.name}'. Available: {names}"}],
                        "isError": True}
            if args.file_path:
                content, err = read_support_file(entry["dir"], args.file_path)
                if err:
                    return {"content": [{"type": "text", "text": err}], "isError": True}
                return {"content": [{"type": "text", "text": content}],
                        "details": {"skill": entry["name"], "file": args.file_path}}
            text, linked = skill_content(entry, session_id(ctx))
            return {"content": [{"type": "text", "text": text}],
                    "details": {"skill": entry["name"], "linked_files": linked}}

        harn.registerTool(ToolDefinition(
            name="skill_view", label="View skill",
            description="Skills allow for loading information about specific tasks and workflows, as well as scripts and templates. Load a skill's full content or access its linked files (references, templates, scripts). First call returns SKILL.md content plus a 'linked_files' index showing available references/templates/scripts. To access those, call again with file_path parameter.",
            parameters=ViewParams.model_json_schema(), execute=view_execute,
            promptSnippet="Load a skill's full instructions",
            promptGuidelines=["When a task matches a skill description, load it with `skill_view` before acting."]))

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
            if result.get("success"):
                skill_index.invalidate()        # the tree changed: the next turn advertises the new state
            lines = [result.get("message") or result.get("error") or ""]
            for key in ("gist", "description_preview", "hint", "lint_hint"):
                if result.get(key):
                    lines.append(str(result[key]))
            for warning in result.get("lint_warnings") or []:
                lines.append(f"  ⚠ [{warning['rule']}] {warning['message']}")
            return {"content": [{"type": "text", "text": "\n".join(x for x in lines if x)}],
                    "isError": not result.get("success"),
                    "details": {k: v for k, v in result.items() if k != "message"}}

        if kind not in ("card", "child"):   # skills are read-only at run time inside a card and its children
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

        # ── commands ──
        async def skill_cmd(args, ctx):
            raw = (args or "").strip()
            found = entries()
            if not raw:
                if not found:
                    ctx.ui.notify("No skills are available in the current skill stack.", "info")
                    return
                lines = [f"/skill {skill_index.slug(e['name'])} — {e['description']}"
                         for e in sorted(found, key=lambda e: e["name"])]
                ctx.ui.notify('Available skills:\n' + "\n".join(lines), "info")
                return
            name, _, instruction = raw.partition(" ")
            entry = skill_index.find(found, name)
            if entry is None:
                available = ", ".join(sorted(skill_index.slug(e["name"]) for e in found))
                ctx.ui.notify(f"Unknown skill '{name}'. Available: {available}", "error")
                return
            await ctx.sendUserMessage(build_skill_message(
                entry, user_instruction=instruction.strip(), session_id=session_id(ctx)))

        harn.registerCommand("skill", {
            "handler": skill_cmd,
            "description": "List skills, or invoke one with `/skill <name> [instruction]` in the current session."})

        async def learn_cmd(args, ctx):
            from misaka.skills import write as skill_write
            from misaka.skills.learn_prompt import build_learn_prompt
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
            from misaka.skills import layers as skill_layers
            from misaka.skills import write as skill_write
            value = (args or "").strip().lower()
            if not value:
                mode = skill_write.write_mode()
                ctx.ui.notify(
                    f"Skill write mode: {mode}\nUse `/skill-mode off|forbid|ask|allow` to change it "
                    "(forbid and ask both stage writes for your review).",
                    "info",
                )
                return
            if value not in skill_write.WRITE_MODES:
                ctx.ui.notify(f"Unknown skill write mode: {value}. Available: {', '.join(skill_write.WRITE_MODES)}",
                              "error")
                return
            cfg = skill_layers.load_skills_config()
            cfg["skill_write_mode"] = value
            skill_layers.write_skills_config(cfg)
            ctx.ui.notify(f"Skill write mode set to {value}; it takes effect immediately.", "info")

        if kind == "foreground":         # the write mode is the user's to set, at the keyboard
            harn.registerCommand("skill-mode", {
                "handler": skill_mode_cmd,
                "description": "Show or change the skill write mode: off, forbid / ask (writes wait for your review), or allow."})
        if kind not in ("card", "child"):
            harn.registerCommand("learn", {
                "handler": learn_cmd,
                "description": "Create or improve a reusable skill from files, links, notes, or the workflow just completed."})

    return register


SESSION_KINDS = {"foreground", "dm", "card", "child"}


def activate(spec):
    roots = (list(spec.skill_roots) if spec.skill_roots is not None
             else skill_roots(spec.profile_dir, spec.workspace))
    return register_for(roots, spec.profile_dir, cwd=spec.workspace, kind=spec.kind)

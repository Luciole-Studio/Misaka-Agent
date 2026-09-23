"""Skills for one session: the index in the system prompt, ``skills_list`` /
``skill_view`` / ``skill_manage``, and the ``/skill``, ``/learn``, ``/skill-mode``
commands (hermes skills_tool + skill commands).

The engine has no skill loading of its own: this extension is the only
thing that decides which skills a session sees -- the role's three layers, or the
read-only sandbox a card runs against (``SessionSpec.skill_roots``).
"""
import asyncio
import json
import os
import re
import shlex
import tempfile
import threading
from pathlib import Path

from misaka.core.moments import CoreCommand
from misaka.core.platform import home_guard
from misaka.core.skills import bundles, reader, sandbox, visibility
from misaka.core.skills import index as skill_index
from misaka.core.skills.layers import (
    extension_resources,
    extension_roots,
    parse_skill_name,
    skill_roots,
)
from misaka.core.skills.manage import (
    MANAGE_PARAMETERS,
    lookup_path_error,
    prepare_arguments,
)
from misaka.core.skills.vendor import commands as hermes_commands
from misaka.utils.async_lifecycle import run_in_thread, settle

_SKILL_INVOCATION_PREFIX = "[IMPORTANT: The user has invoked the "
_SINGLE_SKILL_MARKER = "The full skill content is loaded below.]"
_SINGLE_SKILL_ACTIVATION_SUFFIX = (
    " skill, indicating they want you to follow its instructions. "
    + _SINGLE_SKILL_MARKER
)
_SINGLE_SKILL_INSTRUCTION = (
    "The user has provided the following instruction alongside the skill invocation: "
)
_SKILL_DIRECTORY_PREFIX = "[Skill directory: "
_SKILL_DIRECTORY_INSTRUCTION = (
    "Resolve any relative paths in this skill (e.g. `scripts/foo.js`, "
    "`templates/config.yaml`) against that directory, then run them "
    "with the terminal tool using the absolute path."
)


def _runtime_name(entry):
    return entry.get("runtime_name", entry["name"])


def _command_name(entry):
    # Provider-qualified identities already name a command. Hermes' Telegram slugger
    # strips their namespace delimiters and rewrites underscores; the TUI accepts both.
    name = _runtime_name(entry)
    return name if entry.get("namespace") else skill_index.slug(name)


def _slash_entries(entries):
    """Hermes auto-command view: normalized handles are first-wins."""
    out, seen = [], set()
    for entry in entries:
        handle = _command_name(entry)
        if handle and handle not in seen:
            seen.add(handle)
            out.append(entry)
    return out


def _render_loaded(loaded, entry, *, activation_note, user_instruction="", session_id=None):
    from misaka.core.skills.layers import config_path, load_skills_config
    from misaka.core.skills.preprocessing import preprocess_skill_content
    from misaka.core.skills.vendor.metadata import (
        extract_skill_config_vars,
        resolve_skill_config_values,
    )
    from misaka.utils.prompt_cache_boundary import register_stable_prefix

    def inject_config(payload, parts):
        fm, _ = skill_index.parse_skill_markdown(payload["content"])
        config = resolve_skill_config_values(extract_skill_config_vars(fm), {"skills": load_skills_config()})
        if config:
            parts += ["", f"[Skill config (from {config_path()}):"]
            parts += [f"  {key} = {value if value is not None and value != '' else '(not set)'}" for key, value in config.items()]
            parts.append("]")

    def preprocess(content, directory, task_id):
        return preprocess_skill_content(content, directory, task_id,
                                        layer=entry.get("origin_layer", entry.get("layer")))

    # One raw read, one preprocessing pass. Native ordering/markers are preserved;
    # only explicit host config, linked-file containment and source identity differ.
    return hermes_commands._build_skill_message(
        loaded, Path(entry["dir"]), activation_note, user_instruction=user_instruction, session_id=session_id,
        preprocess=preprocess, inject_config=inject_config,
        support_files=[p for files in (loaded.get("linked_files") or {}).values() for p in files],
        skill_view_target=_runtime_name(entry), skill_view_source=entry.get("origin_path", entry["path"]),
        register_prefix=register_stable_prefix)


def build_skill_message(entry, *, user_instruction="", session_id=None, activation_note=None, profile_dir=None, runtime=None):
    from misaka.core.skills.runtime import SkillRuntime, using_runtime
    owned = runtime is None
    runtime = SkillRuntime(profile_dir) if owned else runtime
    try:
        # Keep the same owner through readiness AND message preprocessing.
        with using_runtime(runtime):
            loaded = reader.load(entry, session_id, profile_dir=profile_dir, preprocess=False, runtime=runtime)
            if not loaded.get("success"):
                raise ValueError(loaded.get("error", "Skill loading failed."))
            note = activation_note or f'{_SKILL_INVOCATION_PREFIX}"{_runtime_name(entry)}"{_SINGLE_SKILL_ACTIVATION_SUFFIX}'
            return _render_loaded(loaded, entry, activation_note=note, user_instruction=user_instruction, session_id=session_id)
    finally:
        if owned:
            runtime.close()


def parse_skill_invocation_message(text):
    """Parse the Hermes single-skill scaffold for the Pi-derived TUI adapter."""
    if not isinstance(text, str):
        return None
    if text.startswith(_SKILL_INVOCATION_PREFIX) and ' skill bundle,' in text.split("\n", 1)[0]:
        return {"name": hermes_commands.describe_skill_invocation(text) or "skills", "location": "",
                "content": text, "user_instruction": hermes_commands.extract_user_instruction_from_skill_message(text)}
    first, separator, rest = text.partition("\n\n")
    prefix = f'{_SKILL_INVOCATION_PREFIX}"'
    suffix = '"' + _SINGLE_SKILL_ACTIVATION_SUFFIX
    if not separator or not first.startswith(prefix) or not first.endswith(suffix):
        return None
    name = first[len(prefix):-len(suffix)]

    directory_marker = "\n\n" + _SKILL_DIRECTORY_PREFIX
    content, marker, tail = rest.rpartition(directory_marker)
    location, end, after = tail.partition("]\n")
    if not marker or not end or not after.startswith(_SKILL_DIRECTORY_INSTRUCTION):
        return None
    instruction = None
    instruction_marker = "\n\n" + _SINGLE_SKILL_INSTRUCTION
    if instruction_marker in after:
        _, _, instruction = after.partition(instruction_marker)
        instruction = instruction.strip() or None
    return {"name": name, "location": location, "content": skill_index.parse_skill_markdown(content)[1].strip(),
            "user_instruction": instruction}


class SkillsPart:
    """Everything skills for one session, against these layer roots; writes (``skill_manage``,
    ``/learn``) go to the role's own ``skills/`` under ``profile_dir``. ``cwd`` is the
    workspace the coding posture is judged in (misaka.core.skills.coding_context)."""

    def __init__(self, roots, profile_dir, cwd=None, kind="foreground", *, platform="cli", startup_skills=(), runtime=None):
        self.session = None
        self._kind = kind
        self._platform = platform
        from misaka.core.skills.runtime import SkillRuntime
        self.runtime = runtime if runtime is not None else SkillRuntime(profile_dir, platform=platform)
        self._dedup = {}
        from misaka.core.skills.scope import SkillScope
        self.scope = SkillScope(profile_dir, Path(cwd or os.getcwd()))
        self._startup_skills = tuple(startup_skills)
        self._startup_prompt = None
        self._command_snapshot = {}
        self._read_lock = asyncio.Lock()
        self._execution_tmp = None
        self._copy_lock = threading.RLock()
        self._closed = False
        self._curator_task = None
        self._review_scope = None
        self._curator_lock = asyncio.Lock()
        from misaka.core.skills.sync_owner import SyncOwner
        self._sync_owner = SyncOwner(self.scope)
        self._sync_started = False
        self._activated_providers = set()
        self._sealed_root = None
        self.tools = []
        self._commands = []
        from pydantic import BaseModel, ConfigDict, Field

        from misaka.core.extensions.types import ToolDefinition
        from misaka.core.skills.coding_context import compact_skill_categories

        self._base_roots = None if roots is None else list(roots)
        roots = list(roots or ())
        self._roots = roots
        workspace = cwd or os.getcwd()
        self._profile_dir, self._workspace = profile_dir, workspace
        self._live_roots = set()

        def entries():
            return self._entries()

        def prompt_entries():
            return self._entries(prompt=True)

        class ListParams(BaseModel):
            model_config = ConfigDict(extra="forbid")
            category: str = Field("", description="Optional category filter to narrow results.")

        class ViewParams(BaseModel):
            model_config = ConfigDict(extra="forbid")
            name: str = Field(description="The skill name (use skills_list to see available skills).")
            source: str = Field("", description="For a name collision, the exact indexed SKILL.md path shown as source in the list or error. Does not allow arbitrary files.")
            file_path: str = Field(
                "", description="OPTIONAL: Path to a linked file within the skill (e.g., 'references/api.md', "
                                "'templates/config.yaml', 'scripts/validate.py'). Omit to get the main SKILL.md content.")

        # ── the index in the system prompt (hermes build_skills_system_prompt) ──
        async def advertise(event, _ctx):
            active = self._active_tools()
            if active is not None and "skill_view" not in active:
                return ({"systemPrompt": event["systemPrompt"].rstrip() + "\n\n" + self._startup_prompt}
                        if self._startup_prompt else None)
            section = skill_index.render_prompt(prompt_entries(), skill_index.categories(roots),
                                                compact_skill_categories(workspace, platform=self._platform),
                                                can_manage=("skill_manage" in active if active is not None
                                                            else kind not in ("card", "child")), available_tools=active)
            sections = [s for s in (section, self._startup_prompt) if s]
            if sections:
                return {"systemPrompt": event["systemPrompt"].rstrip() + "\n\n" + "\n\n".join(sections)}

        async def fresh(_event, _ctx):
            skill_index.invalidate()

        self._advertise, self._fresh = advertise, fresh

        # Live skill trees change only through skill_manage (gate, scan, ledger): the generic file
        # and shell tools are refused on them in every kind of session. A card's sandbox copies are
        # not live trees, so reading them stays possible. Unresolvable shell substitution is refused
        # as well, but only where no one is watching the command (see `_command_touches`).
        live_roots = self._live_roots
        self._refresh_roots()

        # A window with a person in it is the only attended session: a card, a child, a DM turn
        # and a bare one-shot all run with this guard as the only reader of the command. Naming
        # the attended kind rather than the unattended ones keeps a new kind safe by default.
        unattended = kind != "foreground"

        def _touches_live_skills(tool, args):
            """Why this call is refused -- "path", "dynamic", or the home's own sentence -- or None."""
            if tool in ("write", "edit"):
                target = os.path.realpath(os.path.join(workspace, os.path.expanduser(str(args.get("path") or ""))))
                if any(target == root or target.startswith(root + os.sep) for root in live_roots):
                    return "path"
                # The one place a file tool's target is resolved, so the home's rule is asked here too.
                return home_guard.refusal(target, workspace, kind)
            if tool in {"bash", "powershell"}:
                return _command_touches(str(args.get("command") or ""), workspace, live_roots,
                                        shell=tool, unattended=unattended)
            return None

        async def guard_live_skills(event, _ctx):
            args = event.get("input") if isinstance(event, dict) else getattr(event, "input", None)
            tool = event.get("toolName") if isinstance(event, dict) else getattr(event, "toolName", "")
            args = args if isinstance(args, dict) else {}
            prepared = None
            if tool == "office":
                from misaka.core.tools._office.paths import output_paths
                from misaka.core.tools.office import prepare_office_input
                parsed, path, ops = prepare_office_input(args, workspace)
                # Pass the very ops we checked onward; re-reading @ops could change destinations.
                prepared = {"path": path, "ops": ops, "overwrite": parsed.overwrite}
                protected = next(filter(None, (_touches_live_skills("write", {"path": path})
                                               for path in output_paths(path, ops))), None)
            else:
                protected = _touches_live_skills(str(tool or ""), args)
            if protected == "dynamic":
                return {"block": True, "reason": (
                    "This session runs unattended, so a shell command carrying substitution "
                    "($(...), backticks or ${...}) is refused: the guard that keeps live skill trees "
                    "read-only here cannot resolve where the expansion would point. Rewrite it with "
                    "literal paths. Skills themselves are read with skill_view and changed with skill_manage."
                )}
            if protected and protected != "path":
                return {"block": True, "reason": protected}
            if protected:
                return {"block": True, "reason": (
                    "Live skill trees change only through skill_manage (approval, scan, ledger). "
                    "This call reaches a path inside one; write, edit and office are refused on output "
                    "paths there, and bash and powershell on commands that name one. "
                    "Use skill_view to read skills and skill_manage to change them."
                )}
            return {"updatedInput": prepared} if prepared is not None else None

        self._guard = guard_live_skills

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
            lambda: _dim("  " + (", ".join(sorted(_runtime_name(e) for e in entries())) or "(none)")),
            lambda: "\n".join(_dim(line) for line in
                              skill_index.index_lines(
                                  entries(), skill_index.categories(roots),
                                  name_key="runtime_name", description_key="list_description")
                              ) or _dim("  (none)"))

        # ── tools ──
        async def list_execute(tool_call_id, raw, signal, on_update, ctx):
            args = raw if isinstance(raw, ListParams) else ListParams(**(raw or {}))
            from misaka.core.skills.vendor.view import skills_list
            rows = [{"name": _runtime_name(e), "description": e["list_description"], "category": e["category"],
                     **({"source": e["source"]} if e.get("source") else {})} for e in entries()]
            payload = json.loads(skills_list(rows, args.category or None))
            return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                    "details": {"count": len(payload.get("skills", [])), **payload},
                    "isError": not payload.get("success")}

        self.tools.append(ToolDefinition(
            name="skills_list", label="List skills",
            description="List available skills (name + description). Use skill_view(name) to load full content.",
            parameters=ListParams.model_json_schema(), execute=list_execute,
            promptSnippet="List the available skills"))

        async def view_execute(tool_call_id, raw, signal, on_update, ctx):
            args = raw if isinstance(raw, ViewParams) else ViewParams(**(raw or {}))
            err = lookup_path_error(args.name)
            if err:
                return {"content": [{"type": "text", "text": err}], "isError": True}
            try:
                await self._prepare_provider(args.name, source=args.source)
            except ValueError as error:
                return {"content": [{"type": "text", "text": str(error)}], "isError": True}
            self._refresh_roots()
            entry, err = skill_index.resolve(roots, args.name, source=args.source or None, platform=self._platform)
            if err:
                return {"content": [{"type": "text", "text": err}], "isError": True}
            async with self._read_lock:
                # Resolve identity BEFORE dedup: same names across sources never
                # share state, and disabled/quarantined entries cannot hit a stub.
                source = Path(entry["dir"]) / args.file_path if args.file_path else Path(entry["path"])
                try:
                    st = source.stat()
                    fingerprint = (str(source), st.st_mtime_ns, st.st_size)
                except OSError:
                    fingerprint = None
                key = (entry["path"], args.file_path)
                from misaka.core.skills.layers import load_skills_config
                # Setup/config can change without touching SKILL.md.
                ready = reader.readiness(entry.get("frontmatter", {}), profile_dir, runtime=self.runtime)
                state = (fingerprint, json.dumps(load_skills_config(), sort_keys=True),
                         tuple((e["name"], bool(self.runtime.load_env().get(e["name"]))) for e in ready["required_environment_variables"]),
                         tuple(map(str, ready["missing_credential_files"])))
                if fingerprint is not None and self._dedup.get(key) == state:
                    payload = {"success": True, "status": "unchanged", "name": _runtime_name(entry),
                               "file": args.file_path or "SKILL.md", "dedup": True, "content_returned": False,
                               "message": "Skill content unchanged since it was loaded earlier in this conversation — refer to the earlier skill_view result; it is still current and complete. (Re-issued after context compression, this returns the full content again.)"}
                else:
                    payload = await self._run_activation(self._load_payload, entry, self._session_id(ctx), file_path=args.file_path or None, _ctx=ctx)
                    if payload.get("success") and not payload.get("setup_needed"):
                        self._dedup[key] = state
                        while len(self._dedup) > 200:
                            self._dedup.pop(next(iter(self._dedup)))
                return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                        "details": {**payload, "skill": _runtime_name(entry)}, "isError": not payload.get("success")}

        self.tools.append(ToolDefinition(
            name="skill_view", label="View skill",
            description="Skills allow for loading information about specific tasks and workflows, as well as scripts and templates. Load a skill's full content or access its linked files (references, templates, scripts). First call returns SKILL.md content plus a 'linked_files' index showing available references/templates/scripts. To access those, call again with file_path parameter.",
            parameters=ViewParams.model_json_schema(), execute=view_execute,
            promptSnippet="Load a skill's full instructions"))

        async def manage_execute(tool_call_id, raw, signal, on_update, ctx):
            from misaka.core.skills import manage as skill_manage
            args = skill_manage.prepare_arguments(raw or {})
            if not isinstance(args, dict) or args.keys() - {"operations"}:
                return {"content": [{"type": "text", "text": "Unknown Skill manage arguments; expected operations."}], "isError": True}
            self._refresh_roots()
            from misaka.core.skills.scope import using_scope
            def managed():
                with using_scope(self.scope):
                    return skill_manage.manage(operations=args.get("operations"),
                        profile_dir=profile_dir, workspace=workspace, visible_roots=list(roots))
            result = await run_in_thread(managed)
            if result.get("success"):
                await self._sync_owner.schedule()
            lines = [result.get("message") or result.get("error") or json.dumps(result, ensure_ascii=False)]
            for key in ("gist", "description_preview", "hint", "lint_hint"):
                if result.get(key):
                    lines.append(str(result[key]))
            for warning in result.get("lint_warnings") or []:
                lines.append(f"  ⚠ [{warning['rule']}] {warning['message']}")
            return {"content": [{"type": "text", "text": "\n".join(x for x in lines if x)}],
                    "isError": not result.get("success"),
                    "details": {k: v for k, v in result.items() if k != "message"}}

        if kind not in ("card", "child"):   # skills are read-only at run time inside a card and its children
            self.tools.append(ToolDefinition(
                name="skill_manage", label="Manage skills",
                description="Create, update, or delete a reusable skill; every change goes through approval, validation, the security scan, and the rollback ledger.",
                parameters=MANAGE_PARAMETERS,
                prepareArguments=prepare_arguments,
                execute=manage_execute,
                promptSnippet="Create or update a reusable skill",
                promptGuidelines=[
                    f"New skill descriptions must be one sentence of at most {skill_index.SKILL_PROMPT_DESC_LIMIT} characters; put detail in the body.",
                    "Use `skill_manage`, never generic file tools, for every skill mutation.",
                    "When the user-controlled gate blocks a write, report it and do not seek a bypass.",
                ]))

        # ── commands ──
        async def skill_cmd(args, ctx):
            raw = (args or "").strip()
            found = _slash_entries(entries())
            if not raw:
                if not found:
                    ctx.ui.notify("No skills are available in the current skill stack.", "info")
                    return
                lines = [f"/skill {shlex.quote(_runtime_name(e))}"
                         + (f" — {e['list_description']}" if e.get("list_description") else "")
                         for e in sorted(found, key=_runtime_name)]
                ctx.ui.notify('Available skills:\n' + "\n".join(lines), "info")
                return
            name, _, instruction = raw.partition(" ")
            async with self._read_lock:
                entry, error = skill_index.resolve(roots, name, platform=self._platform)
                if error:
                    raise ValueError(error)
                message = await self._run_activation(self._single_message, entry, instruction.strip(), self._session_id(ctx), _ctx=ctx)
                return message

        self._commands.append(CoreCommand(
            "skill", "List skills, or invoke one with `/skill <name> [instruction]` in the current session.", skill_cmd, is_prompt=True))

        async def reload_cmd(args, ctx):
            before = dict(self._command_snapshot)
            skill_index.invalidate()
            self._refresh_roots()
            self._update_command_snapshot()
            diff = hermes_commands.diff_command_snapshots(before, self._command_snapshot)
            diff["commands"] = len(self._command_snapshot)
            ctx.ui.notify(json.dumps(diff, ensure_ascii=False), "info")

        self._commands.append(CoreCommand("reload-skills", "Rescan Skills and report added/removed commands.", reload_cmd))

        async def learn_cmd(args, ctx):
            if profile_dir is None:
                raise ValueError("Skill writing requires a role profile; select a role before using /learn.")
            from misaka.core.skills import write as skill_write
            from misaka.core.skills.learn_prompt import build_learn_prompt
            target = os.path.join(profile_dir, "skills")
            os.makedirs(target, exist_ok=True)
            prompt = build_learn_prompt(args or "", target_dir=target,
                                        available_tools=self.session.getActiveToolNames() if self.session else ())
            # Every write goes through the skill gate, validation, scan, and ledger.
            decision, note = skill_write.evaluate_gate()
            if decision == "off":
                raise ValueError("Skill writing is disabled globally. Change the write mode before using /learn.")
            prompt += (
                "\n\n---\n[Write path] Use `skill_manage` for every skill change; never write the skill tree with generic file tools. "
                "Create a skill with `skill_manage(action='create', name=..., content=<complete SKILL.md>)` and add support files "
                f"with `action='write_file'`. New descriptions must be one sentence of at most {skill_index.SKILL_PROMPT_DESC_LIMIT} characters; put detail in the body."
            )
            if decision == "stage":
                prompt += (
                    f"\n{note}\nThe tool will return a pending ID. Report the skill name and summary, then tell the user "
                    "to inspect it with `misaka skills pending` and approve it with `misaka skills approve <id>`."
                )
            return prompt

        async def skill_mode_cmd(args, ctx):
            # Slash commands originate from user input, so this is the user-only write-mode entry point.
            from misaka.core.skills import layers as skill_layers
            from misaka.core.skills import write as skill_write
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
            try:
                skill_layers.write_skills_config(cfg)
            except skill_layers.SkillsConfigError as error:
                # A settings.json the user broke by hand is their file to fix; the
                # refusal is the point, so say it rather than crash the command.
                ctx.ui.notify(str(error), "error")
                return
            ctx.ui.notify(f"Skill write mode set to {value}; it takes effect immediately.", "info")

        if kind == "foreground":         # the write mode is the user's to set, at the keyboard
            self._commands.append(CoreCommand(
                "skill-mode",
                "Show or change the skill write mode: off, forbid / ask (writes wait for your review), or allow.",
                skill_mode_cmd))
        if kind not in ("card", "child"):
            self._commands.append(CoreCommand(
                "learn", "Create or improve a reusable skill from files, links, notes, or the workflow just completed.", learn_cmd, is_prompt=True))

        if kind in ("foreground", "dm"):
            async def refine(args, ctx):
                import copy
                from dataclasses import replace

                from misaka.core.skills.operations import review_completed
                if profile_dir is None:
                    ctx.ui.notify("Skill review requires a role profile; select a role before using /refine.", "error")
                    return
                scope = replace(self.scope, origin="background_review", read_marks=None, stop=threading.Event())
                messages = copy.deepcopy(getattr(getattr(self.session, "state", None), "messages", []))
                task = await self._start_curator(scope, lambda: run_in_thread(review_completed, scope, messages, args))
                if task is None:
                    return
                try:
                    result = await asyncio.shield(task)
                    await self._sync_owner.schedule()
                    ctx.ui.notify(result.get("summary", "Skill review completed"), "warning" if result.get("error") else "info")
                except asyncio.CancelledError:
                    await self._stop_curator()
                    raise
                finally:
                    if self._curator_task is task:
                        self._curator_task = None
                        self._review_scope = None
            self._commands.append(CoreCommand("refine", "Review the completed task for reusable Skill improvements.", refine))


    @property
    def commands(self):
        """The setting controls automatic commands; explicit /skill always remains."""
        commands = list(self._commands)
        settings = getattr(self.session, "settingsManager", None)
        if settings and not settings.getEnableSkillCommands():
            return commands
        self._refresh_roots()
        from misaka.core.slash_commands import (
            _LOCAL_ALIAS_SLASH_COMMANDS,
            BUILTIN_SLASH_COMMANDS,
        )
        reserved = {c.name for c in (*BUILTIN_SLASH_COMMANDS, *_LOCAL_ALIAS_SLASH_COMMANDS, *commands)}
        if self.session:
            for part in getattr(getattr(self.session, "moments", None), "parts", ()):
                if part is not self:
                    reserved.update(c.name for c in getattr(part, "commands", ()))
            runner = getattr(self.session, "extensionRunner", None)
            if runner:
                reserved.update(c.name for c in runner.get_registered_commands())
            loader = getattr(self.session, "resourceLoader", None)
            if loader and hasattr(loader, "getPrompts"):
                reserved.update(p.name for p in loader.getPrompts().get("prompts", []))
        entries = _slash_entries(self._entries())
        bundle_table = {k: v for k, v in self._bundles().items() if k[1:] not in reserved}
        table = {"/" + _command_name(e): e for e in entries
                 if _command_name(e) not in reserved
                 and "/" + _command_name(e) not in bundle_table}
        for key, entry in table.items():
            async def invoke(args, ctx, key=key):
                async with self._read_lock:
                    self._refresh_roots()
                    extras, instruction = hermes_commands.split_stacked_skill_commands(
                        args, lambda name: ("/" + name if "/" + name in table else
                                            hermes_commands.resolve_slash_key(name, table)))
                    keys = list(dict.fromkeys([key, *extras]))
                    message = await self._run_activation(self._stack_message, table, keys, instruction, self._session_id(ctx))
                    if message:
                        return message
                    else:
                        raise ValueError("Requested skills are no longer available.")
            commands.append(CoreCommand(key[1:], entry.get("list_description") or f"Invoke {_runtime_name(entry)}", invoke, is_prompt=True))
        for key, info in bundle_table.items():
            async def invoke_bundle(args, ctx, key=key):
                async with self._read_lock:
                    self._refresh_roots()
                    result = await self._run_activation(self._bundle_message, key, args or "", self._session_id(ctx))
                    if result:
                        return result[0]
                    else:
                        raise ValueError("Bundle has no available skills or no longer exists.")
            commands.append(CoreCommand(key[1:], info["description"], invoke_bundle, is_prompt=True))
        return commands

    def _update_command_snapshot(self):
        self._command_snapshot = {c.name: c.description for c in self.commands if c.name not in {x.name for x in self._commands}}

    def _single_message(self, entry, instruction, task_id, *, cancelled=None):
        loaded = self._load_named_skill(_runtime_name(entry), task_id, source=entry["path"], cancelled=cancelled)
        if not loaded:
            raise ValueError("Skill is no longer available.")
        note = f'{_SKILL_INVOCATION_PREFIX}"{loaded[2]}"{_SINGLE_SKILL_ACTIVATION_SUFFIX}'
        return _render_loaded(loaded[0], loaded[0]["_entry"], activation_note=note,
                              user_instruction=instruction, session_id=task_id)

    def _preload(self, task_id, *, cancelled=None):
        disabled = self._disabled_names()
        return hermes_commands.build_preloaded_skills_prompt(
            list(self._startup_skills), task_id,
            load_blocks=lambda *args, **kwargs: self._load_blocks(*args, **kwargs, cancelled=cancelled),
            load_payload=lambda identifier, task_id: self._load_named_skill(identifier, task_id, disabled=disabled, cancelled=cancelled),
            disabled_names=disabled)

    async def _prepare_provider(self, name, source=None):
        if self._base_roots is not None:  # sealed card/child snapshots never activate live extensions
            return
        from misaka.core.skills.providers import prepare
        if not source and isinstance(name, str) and ':' in name:
            self._refresh_roots()
            entry, error = skill_index.resolve(self._roots, name, platform=self._platform)
            if error:
                self._activated_providers.discard(parse_skill_name(name)[0])
                return  # The normal resolver reports missing/disabled/ambiguous, without activation.
            source = entry['path']
        await prepare(getattr(self.session, "resourceLoader", None), name, self._activated_providers, source=source)

    async def _run_activation(self, function, *args, _ctx=None, **kwargs):
        kind = function.__name__
        names = []
        if kind in ("_single_message", "_load_payload") and args:
            names = [(_runtime_name(args[0]), args[0]['path'])]
        elif kind == "_stack_message":
            names = [(_runtime_name(args[0][key]), args[0][key]['path']) for key in args[1] if key in args[0]]
        elif kind == "_bundle_message":
            names = [(name, None) for name in (self._bundles().get(args[0]) or {}).get("skills", [])]
        elif kind == "_preload":
            names = [(name, None) for name in self._startup_skills]
        for name, source in names:
            await self._prepare_provider(name, source=source)
        """Cancel future bundle members, while draining the current owned effect."""
        stop = threading.Event()
        from misaka.core.skills.runtime import using_runtime
        loop = asyncio.get_running_loop()
        if _ctx is None and getattr(self.session, "extensionRunner", None) is not None:
            _ctx = self.session.extensionRunner.create_context()
        captures = set()
        async def capture_owned(name, prompt):
            from misaka.core.skills.secret_input import capture_secret
            task = asyncio.current_task()
            captures.add(task)
            try:
                return await capture_secret(_ctx, self.runtime, name, prompt, stop)
            finally:
                captures.discard(task)
        def capture(name, prompt, metadata):
            from concurrent.futures import TimeoutError as FutureTimeout
            if _ctx is None or not getattr(_ctx, "hasUI", False):
                return {"success": False, "skipped": True}
            future = asyncio.run_coroutine_threadsafe(capture_owned(name, prompt), loop)
            while True:
                if stop.is_set():
                    future.cancel()
                    return {"success": False, "skipped": True}
                try:
                    return future.result(timeout=0.1)
                except FutureTimeout:
                    continue
        def run():
            prior = self.runtime.capture
            self.runtime.capture = capture if _ctx is not None and getattr(_ctx, "hasUI", False) else prior
            try:
                from misaka.core.skills.scope import using_scope
                with using_runtime(self.runtime), using_scope(self.scope):
                    return function(*args, cancelled=stop, **kwargs)
            finally:
                self.runtime.capture = prior
        work = asyncio.create_task(run_in_thread(run))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError as error:
            stop.set()
            try:
                await settle(work)
            finally:
                for task in list(captures):
                    task.cancel()
                    try:
                        await settle(task)
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 - drain every capture before propagating the original cancellation
                        pass
                raise error  # repeated cancellation or worker failure cannot orphan the work

    @staticmethod
    def _session_id(ctx):
        try:
            return str(ctx.sessionManager.getSessionId())
        except Exception:  # noqa: BLE001 - missing session metadata should not block loading
            return None

    def _active_tools(self):
        try:
            return set(self.session.getActiveToolNames()) if self.session else None
        except Exception:  # noqa: BLE001 - old host contexts can omit tool info
            return None

    def _entries(self, *, prompt=False):
        self._refresh_roots()
        active = self._active_tools()
        tools, toolsets = visibility.capabilities(active)
        detect = visibility.environment_detector(kind=self._kind, active=active)
        if prompt:
            return skill_index.build(self._roots, platform=self._platform, available_tools=tools,
                                     available_toolsets=toolsets, detect=detect)
        return skill_index.runtime_build(self._roots, platform=self._platform, detect=detect)

    def _bundles(self):
        if self._base_roots is None:
            return bundles.scan(bundles.bundle_roots(self._profile_dir, self._workspace))
        out = {}
        for layer, root in self._base_roots:
            if layer == "sandbox":
                manifest = sandbox.read_manifest(root)
                records = (manifest or {}).get("bundles", [])
                for info in records:
                    out.setdefault("/" + info["slug"], dict(info))
            elif layer in ("role", "project", "shared"):
                for key, info in bundles.scan([(layer, str(Path(root).parent / "skill-bundles"))]).items():
                    out.setdefault(key, info)
        return out

    def _disabled_names(self):
        from misaka.core.skills.layers import disabled_skill_names
        disabled = set(disabled_skill_names(self._platform))
        return disabled | {
            _runtime_name(e) for e in skill_index.all_entries(self._roots, platform=self._platform, include_disabled=True)
            if skill_index.is_disabled(e, self._platform, disabled=disabled)}

    def _load_named_skill(self, identifier, task_id=None, *, source=None, disabled=None, cancelled=None):
        if cancelled is not None and cancelled.is_set():
            return None
        entry, error = skill_index.resolve(self._roots, identifier, source=source,
                                           platform=self._platform, include_disabled=True)
        if error:
            return None
        if skill_index.is_disabled(entry, self._platform, disabled=disabled):
            return {}, None, _runtime_name(entry)  # native block loader skips BEFORE any execution copy/preprocessing
        try:
            copied = self._execution_entry(entry)
            loaded = reader.load(copied, task_id, profile_dir=self._profile_dir, preprocess=False, runtime=self.runtime)
        except (OSError, ValueError):
            return None
        if not loaded.get("success"):
            return None
        from misaka.core.skills.operations import observe
        observe(entry, self._profile_dir, self._workspace, view=False, task_id=task_id)
        loaded["_entry"] = copied
        return loaded, Path(copied["dir"]), _runtime_name(entry)

    def _load_blocks(self, *args, cancelled=None, **kwargs):
        def render(loaded, note, task_id):
            if cancelled is not None and cancelled.is_set():
                return ""
            return _render_loaded(loaded[0], loaded[0]["_entry"], activation_note=note, session_id=task_id)
        return hermes_commands._load_skill_blocks(*args, **kwargs, render=render)

    def _stack_message(self, table, keys, instruction, task_id, *, cancelled=None):
        disabled_names = self._disabled_names()
        def load(key):
            entry = table[key]
            return self._load_named_skill(_runtime_name(entry), task_id, source=entry["path"], disabled=disabled_names, cancelled=cancelled)
        if len(keys) == 1:
            loaded = load(keys[0])
            if not loaded or loaded[2] in disabled_names:
                return None
            note = f'{_SKILL_INVOCATION_PREFIX}"{loaded[2]}"{_SINGLE_SKILL_ACTIVATION_SUFFIX}'
            return _render_loaded(loaded[0], loaded[0]["_entry"], activation_note=note,
                                  user_instruction=instruction, session_id=task_id)
        names, missing, disabled, blocks = self._load_blocks(keys, load,
            lambda name: f'[Loaded as part of the stacked skill invocation "{name}".]', task_id,
            missing_label=lambda k: k.lstrip("/"), disabled_names=disabled_names, cancelled=cancelled)
        if not blocks:
            return None
        header = hermes_commands._scaffold_header(f'"{" ".join(keys)}" stacked skill bundle', names,
            missing=missing, disabled=disabled, user_instruction=instruction)
        return "\n\n".join([header, *blocks])

    def _bundle_message(self, key, instruction, task_id, *, cancelled=None):
        from misaka.core.skills.vendor.bundles import build_bundle_invocation_message
        disabled_names = self._disabled_names()
        return build_bundle_invocation_message(key, instruction, task_id, self._platform,
            bundles=self._bundles(), load_blocks=lambda *args, **kwargs: self._load_blocks(*args, **kwargs, cancelled=cancelled),
            load_payload=lambda identifier, task_id: self._load_named_skill(identifier, task_id, disabled=disabled_names, cancelled=cancelled),
            disabled_names=disabled_names)

    def attach(self, session):
        self.session = session
        self.scope.model = getattr(session, "model", None)
        self.scope.model_registry = getattr(session, "modelRegistry", None)
        manager = getattr(session, "sessionManager", None)
        if manager is not None:
            manager._skill_runtime = self.runtime

    def _refresh_roots(self):
        # Hermes discovers current roots on access. Extension contributions are a
        # replaceable set, not an append-only history; explicit sandbox roots stay pinned.
        loader = getattr(self.session, "resourceLoader", None)
        extension_paths = extension_resources(loader)
        live = skill_roots(self._profile_dir, self._workspace, extension_paths=extension_paths)
        current = live if self._base_roots is None else list(self._base_roots)
        seen = set()
        roots = []
        for layer, root in current:
            key = (layer if layer.startswith("extension:") else "", os.path.realpath(root))
            if key not in seen:
                seen.add(key)
                roots.append((layer, root))
        if roots != self._roots:
            self._roots[:] = roots
            skill_index.invalidate()
        from misaka.core.skills.layers import protected_skill_roots
        self._live_roots.clear()
        self._live_roots.update(protected_skill_roots(self._profile_dir, self._workspace, extension_paths=extension_paths))
        self._live_roots.update(str(Path(root).resolve()) for _, root in bundles.bundle_roots(self._profile_dir, self._workspace))

    def _execution_entry(self, entry):
        with self._copy_lock:
            if self._closed:
                raise RuntimeError("Skill session is closed.")
            return self._copy_entry(entry)

    def _copy_entry(self, entry):
        """Advertised absolute script paths point to an owned copy, never a guarded live tree."""
        if self._base_roots is not None:
            return entry
        if self._execution_tmp is None:
            self._execution_tmp = str(Path(tempfile.mkdtemp(prefix="misaka-skill-execution-")).resolve())
        return sandbox.execution_entry(entry, self._execution_tmp)

    def _load_payload(self, entry, session_id=None, *, file_path=None, cancelled=None):
        try:
            copied = self._execution_entry(entry)
            payload = reader.load(copied, session_id, file_path=file_path, profile_dir=self._profile_dir, runtime=self.runtime)
            if payload.get("success"):
                from misaka.core.skills.operations import observe
                observe({**entry, "origin_path": str(Path(entry.get("origin_dir", entry["dir"])) / file_path)} if file_path else entry,
                        self._profile_dir, self._workspace, use=not file_path, task_id=session_id)
            return payload
        except (OSError, ValueError) as error:
            return {"success": False, "error": str(error)}

    def resolution_roots(self):
        """The effective post-discovery collection, including explicit empty roots."""
        self._refresh_roots()
        return list(self._roots)

    async def resources_ready(self, event, ctx):
        if not self._sync_started and self._kind in ("foreground", "dm"):
            self._sync_started = True
            await self._sync_owner.schedule(startup=True)
        async with self._read_lock:
            if self._closed:
                raise RuntimeError("Skill session is closed.")
            if self._base_roots is not None and self._sealed_root is None:
                loader = getattr(self.session, "resourceLoader", None)
                extensions = list(extension_roots(extension_resources(loader)))
                prepared = next((root for layer, root in self._base_roots if layer == "sandbox"), None)
                manifest = await run_in_thread(sandbox.read_manifest, prepared, verify_files=True) if prepared else None
                if self._kind == "child" and manifest and manifest["state"] != "sealed":
                    raise ValueError("Nested child received an unsealed Skill snapshot.")
                if prepared:
                    if self._kind != "child":
                        await run_in_thread(sandbox.seal, prepared, extensions)
                    self._sealed_root = prepared
                else:
                    if self._execution_tmp is None:
                        self._execution_tmp = str(Path(tempfile.mkdtemp(prefix="misaka-skill-collection-")).resolve())
                    sealed_root = str(Path(self._execution_tmp) / "sealed")
                    await run_in_thread(sandbox.readonly_copies, skill_index.all_entries(self._base_roots, platform=self._platform), sealed_root,
                                        bundle_records=list(self._bundles().values()), category_descriptions=skill_index.categories(self._base_roots))
                    await run_in_thread(sandbox.seal, sealed_root, extensions)
                    self._sealed_root = sealed_root  # publish only the completed collection
                self._base_roots = [("sandbox", self._sealed_root)]
            self._refresh_roots()
            if self._startup_prompt is None and self._startup_skills:
                prompt, loaded, missing = await self._run_activation(self._preload, self._session_id(ctx))
                if not loaded:
                    raise ValueError("Unknown skill(s): " + ", ".join(missing))
                self._startup_prompt = prompt
                if missing:
                    ctx.ui.notify("Unknown skill(s) requested, skipping: " + ", ".join(missing), "warning")
            self._update_command_snapshot()

    async def _drain_curator(self):
        task = self._curator_task
        if self._review_scope is not None:
            self._review_scope.stop.set()
        if task is not None:
            task.cancel()
            try:
                await settle(task)
            except asyncio.CancelledError:
                pass
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Owned Skill review failed")
        self._curator_task = None
        self._review_scope = None

    async def _stop_curator(self):
        if self._review_scope is not None:
            self._review_scope.stop.set()
        async with self._curator_lock:
            await self._drain_curator()

    async def _start_curator(self, scope, run):
        async with self._curator_lock:
            await self._drain_curator()
            if self._closed:
                return None
            self._review_scope = scope
            self._curator_task = asyncio.create_task(run())
            return self._curator_task

    async def agent_start(self, event, ctx):
        await self._stop_curator()
        self._dedup.clear()
        self.scope.read_marks = None

    async def agent_end(self, event, ctx):
        if self._kind not in ("foreground", "dm") or self._closed or self._profile_dir is None:
            return
        import copy
        from dataclasses import replace

        from misaka.core.skills.operations import review_completed
        from misaka.core.skills.scope import load_config, using_scope
        from misaka.core.skills.vendor import curator
        with using_scope(self.scope):
            cfg = load_config().get("background_review", {})
            # HOST: enabling a newly ported feature must not silently bill existing sessions.
            enabled = isinstance(cfg, dict) and cfg.get("enabled") is True
            curate = curator.is_enabled()
            delay = max(0.0, curator.get_min_idle_hours() * 3600.0) if curate else 0
        if not enabled and not curate:
            return
        scope = replace(self.scope, origin="background_review", read_marks=None,
                        stop=threading.Event(), model=getattr(self.session, "model", None))
        messages = copy.deepcopy(getattr(getattr(self.session, "state", None), "messages", []))
        async def run():
            from filelock import FileLock, Timeout
            lock = FileLock(str(scope.profile / ".curator-owner.lock"), timeout=0)
            scope.profile.mkdir(parents=True, exist_ok=True)
            def owned(function):
                if scope.stop.is_set():
                    return None
                with using_scope(scope):
                    try:
                        with lock:
                            return function()
                    except Timeout:
                        return None
            if enabled:
                await run_in_thread(owned, lambda: review_completed(scope, messages))
                if not scope.stop.is_set():
                    await self._sync_owner.schedule()
            if curate and not scope.stop.is_set():
                await asyncio.sleep(delay)
                await run_in_thread(owned, lambda: curator.maybe_run_curator(idle_for_seconds=delay))
        await self._start_curator(scope, run)

    async def session_compact(self, event, ctx):
        self._dedup.clear()

    def _close_copies(self):
        with self._copy_lock:
            self._closed = True
            try:
                self.runtime.close()
            finally:
                if self._execution_tmp:
                    sandbox.cleanup(self._execution_tmp)
                    self._execution_tmp = None

    async def session_shutdown(self, event, ctx):
        self._closed = True  # Fence queued review/reload callbacks before awaiting.
        await settle(asyncio.create_task(self._shutdown(event)))

    async def _shutdown(self, event):
        await self._stop_curator()
        await self._sync_owner.close()
        self._dedup.clear()
        if event.get("reason") != "reload":
            async with self._read_lock:
                await run_in_thread(self._close_copies)

    async def before_agent_start(self, event, ctx):
        self._refresh_roots()
        return await self._advertise(event, ctx)

    async def session_start(self, event, ctx):
        self._activated_providers.clear()
        if self._sync_owner.closed:
            from misaka.core.skills.sync_owner import SyncOwner
            self._sync_owner = SyncOwner(self.scope)
            self._sync_started = False
        if self.runtime.closed:
            self.runtime = self.runtime.reopen()
            self._startup_prompt = None
            if self.session is not None:
                self.attach(self.session)
        self._closed = False
        self._refresh_roots()
        await self._fresh(event, ctx)

    async def tool_call(self, event, ctx):
        self._refresh_roots()
        return await self._guard(event, ctx)


SESSION_KINDS = {"foreground", "dm", "card", "child", "bare"}


# Command substitution, parameter expansion and a bare variable: everything whose value this
# guard cannot know. PowerShell spells its variables the same way.
_SUBSTITUTION = re.compile(r"\$[({A-Za-z_]|`")


def _has_dynamic_shell_syntax(command, shell):
    # PowerShell has different quoting/escape rules. Keep its conservative policy;
    # this Bash-only literal recognition is not a cross-shell permission parser.
    if shell != "bash":
        return bool(_SUBSTITUTION.search(command))
    if not _SUBSTITUTION.search(command):
        return False
    quote = None
    i = 0
    while i < len(command):
        char = command[i]
        if quote == "'":
            if char == "'":
                quote = None
        elif char == "\\":
            if quote is None or command[i + 1:i + 2] in ('$', '`', '"', "\\", "\n"):
                i += 1
        elif char == '"' and quote == '"':
            quote = None
        elif quote is None and char in ("'", '"'):
            quote = char
        elif char == "`" or command[i:i + 2] in ("$(", "${") or (
                char == "$" and (command[i + 1:i + 2].isalpha() or command[i + 1:i + 2] == "_")):
            # A bare `$HOME` hides a path exactly as `${HOME}` does; the quote state above is
            # what keeps a single-quoted dollar sign literal.
            return True
        i += 1
    if quote is not None:
        return True
    # A quoted string may itself be code (bash -c, eval, python -c, ...). Only
    # exempt simple literal display commands, not every shell-quoted program.
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        argv = list(lexer)
    except ValueError:
        return True
    return (not argv or argv[0] not in {"printf", "echo"}
            or "\n" in command
            or any(token and all(c in ";&|()<>" for c in token) for token in argv))


def _command_touches(command, workspace, live_roots, *, shell="bash", unattended=True):
    """Why a shell command is refused near a live skill tree: "path", "dynamic", or None.

    The literal substring test this replaces read the command as text, so only the
    expanded absolute form was caught: `cd ~/.misaka/profiles/<role>/skills`, or any
    path relative to the workspace, walked straight past a guard whose whole job is to
    keep unattended card sessions out of the live trees.

    Every token that could be a path is resolved the way the shell would resolve it --
    `~` expanded, relatives taken against the workspace, symlinks followed -- and
    compared as a path, not as text. Tokens are taken from `shlex`.

    A command carrying substitution (`$(...)`, backticks, `${...}`), or one `shlex` cannot
    parse at all, hides where it would point. Where nobody is watching -- ``unattended``, a
    card or a child -- refusing it is the safe direction: `skill_view` reads skills and
    `skill_manage` writes them, so nothing legitimate needs the shell to reach one. In a
    conversation with a person in it, that verdict costs more than it buys and only a path
    the guard can actually resolve is refused.
    """
    if not command.strip():
        return None
    if any(root in command for root in live_roots):
        return "path"
    # Substitution is only a reason by itself where nobody is watching. In a card or a child
    # the guard is the only reader of the command, so an expansion it cannot resolve is refused
    # (2026-09-18, B23: that verdict was reaching Last Order too, where it killed
    # `cp board.db board.db.bak-$(date +%s)` and read as if a skill tree were involved).
    dynamic = _has_dynamic_shell_syntax(command, shell)
    # shlex neither expands ${HOME} nor evaluates $(...), so the token scan below cannot see
    # through substitution; refusing it is what keeps an unattended session out of a tree it
    # could otherwise name indirectly.
    try:
        argv = shlex.split(command)
    except ValueError:
        return "dynamic" if unattended else None
    pending = list(argv)
    while pending:
        item = pending.pop()
        if not item or item.startswith("-"):
            continue
        if item.strip() != item or any(char.isspace() for char in item):
            # A token that is itself a command line -- `bash -c "..."`, `eval "..."`, a
            # `find -exec` payload -- hides its paths one level down.
            try:
                pending.extend(shlex.split(item))
            except ValueError:
                if unattended:
                    return "dynamic"
            continue
        candidate = os.path.expanduser(item)
        if not os.path.isabs(candidate):
            candidate = os.path.join(workspace, candidate)
        for resolved in (os.path.abspath(candidate), os.path.realpath(candidate)):
            if any(resolved == root or resolved.startswith(root + os.sep) for root in live_roots):
                return "path"
    return "dynamic" if dynamic and unattended else None


def part(spec):
    if spec.kind == "bare":
        from misaka.config.profiles import is_last_order
        if not is_last_order(spec.profile_dir):
            return None
    return SkillsPart(spec.skill_roots, spec.profile_dir, cwd=spec.workspace, kind=spec.kind, startup_skills=spec.startup_skills)

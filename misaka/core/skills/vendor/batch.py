# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / tools/skill_manager_batch.py; see PROVENANCE.json and LICENSE.
import json
import posixpath

_BATCH_OP_ACTIONS = {"create", "patch", "write_file", "remove_file"}


_BATCH_MAX_OPS = 20


def _validate_batch_ops(operations, default_name, tool_error, check_preflight):
    """Shape checks with no side effects. Returns (names, None) or (None, error_json)."""
    def fail(i, msg):
        return None, tool_error(f"operations[{i}]{msg}", success=False)
    names = []
    for i, op in enumerate(operations):
        if not isinstance(op, dict) or not op.get("action"):
            return fail(i, " needs an 'action'.")
        act = op["action"]
        if act not in _BATCH_OP_ACTIONS:
            return fail(i, f": unknown action '{act}'. Batchable: "
                           f"{', '.join(sorted(_BATCH_OP_ACTIONS))}; delete must be sole.")
        nm = op.get("name") or default_name
        if not nm:
            return fail(i, " needs a 'name' (the skill it targets).")
        names.append(nm)
        if act == "create" and nm in names[:-1]:
            return fail(i, f": create for '{nm}' must precede that skill's other ops.")
        if (preflight := check_preflight(act, nm)) is not None:
            return None, json.dumps(preflight, ensure_ascii=False)
    # Clobber guard: a DESTRUCTIVE op (create/write_file/remove_file/full rewrite) on
    # a file an earlier op touched would SILENTLY discard its work — reject it.
    # Additive patches are always legal. Paths are normalized against spelling variants.
    touched_files = set()
    for i, op in enumerate(operations):
        act, nm = op["action"], names[i]
        # create and full-rewrite patch (content) always hit SKILL.md.
        full_rewrite = act == "patch" and bool(op.get("content"))
        fp = (op.get("file_path") or "").strip()
        target = ("SKILL.md" if (act == "create" or full_rewrite or not fp)
                  else posixpath.normpath(fp.lstrip("/")))
        key = (nm, target)
        if (act in ("create", "write_file", "remove_file") or full_rewrite) and key in touched_files:
            return fail(i, f": {act} on '{target}' of skill '{nm}' — an earlier op in this "
                           f"batch already touched that file, and this op would silently discard its work. "
                           f"One destructive op (write_file/remove_file/full rewrite) per file per batch; put "
                           f"it first, or fold the change in. Patch chains are fine.")
        touched_files.add(key)
    return names, None


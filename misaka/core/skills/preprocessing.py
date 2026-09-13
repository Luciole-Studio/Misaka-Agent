"""Hermes preprocessing, with MISAKA's explicit origin execution policy."""


from .vendor.preprocessing import (
    expand_inline_shell,
    load_skills_config,
    run_inline_shell,
    substitute_template_vars,
)
from .vendor.preprocessing import preprocess_skill_content as _preprocess


def preprocess_skill_content(
    content, skill_dir, session_id=None, skills_cfg=None, *, layer=None
):
    from .layers import PERSONAL_LAYERS

    cfg = dict(skills_cfg if isinstance(skills_cfg, dict) else load_skills_config())
    if layer not in PERSONAL_LAYERS:
        cfg["inline_shell"] = False
    try:
        return _preprocess(content, skill_dir, session_id, cfg)
    except Exception:  # noqa: BLE001 - Hermes' reader falls back to RAW content, including unresolved macros
        return content


__all__ = [
    "expand_inline_shell",
    "load_skills_config",
    "preprocess_skill_content",
    "run_inline_shell",
    "substitute_template_vars",
]

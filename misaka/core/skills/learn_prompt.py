"""Pinned Hermes /learn prompt; only host tool names and author identity change."""

from .vendor.learn_prompt import build_learn_prompt as _build


def build_learn_prompt(user_request, *, target_dir="", available_tools=()):
    # Replace only prompt vocabulary, never user text (which may mention Hermes,
    # a literal tool name, URLs, or code as source requirements).
    marker = "\x00MISAKA_LEARN_REQUEST\x00"
    prompt = _build(marker)
    for old, new in {
        "Hermes": "Misaka",
        "`terminal`": "`bash`",
        "`read_file`": "`read`",
        "`write_file`": "`write`",
        "`search_files`": "`grep`/`find`",
        "`patch`": "`edit`",
    }.items():
        prompt = prompt.replace(old, new)
    prompt = prompt.replace(
        marker,
        user_request.strip()
        if user_request and user_request.strip()
        else "the workflow we just went through in this conversation — review the steps taken and distill them into a reusable skill",
    )
    if target_dir:
        prompt += f"\n\nSkill write target: {target_dir}. Use only skill_manage for changes to this tree."
    if available_tools:
        prompt += (
            "\nOnly call tools actually available in this session: "
            + ", ".join(available_tools)
            + ". Other tool names above are upstream examples, not installed capabilities."
        )
    return prompt

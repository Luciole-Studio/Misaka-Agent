"""CCB AgentTool/prompt.ts getPrompt, with explicit native host bindings.

Pin: 77a7934e15d69da13879112ed7db695c9ee7a52a. Native tools are read/find;
there is no subscription/Teams/CCR host. Fork uses the actual call contract
(omit subagent_type), not source prompt.ts's nonexistent fork:true field.
"""
from __future__ import annotations

from misaka.core.subagent.agents import roster_text
from misaka.core.subagent.background import background_disabled
from misaka.core.subagent.catalog import list_in_messages


def render_prompt(agents, *, fork_mode=False):
    list_via_messages = list_in_messages()
    agent_list = (
        "Available agent types are listed in <system-reminder> messages in the conversation."
        if list_via_messages else
        "Available agent types and the tools they have access to:\n" + roster_text(agents)
    )
    shared = f"""Launch a new agent to handle complex, multi-step tasks autonomously.

The Agent tool launches specialized agents (subprocesses) that autonomously handle complex tasks. Each agent type has specific capabilities and tools available to it.

{agent_list}

When using the Agent tool, specify a subagent_type parameter to select which agent type to use. If omitted, the general-purpose agent is used."""
    if fork_mode:
        shared = shared.removesuffix("If omitted, the general-purpose agent is used.") + (
            "Omit subagent_type to fork the parent conversation context, inheriting full history and model. "
            "Fork mode is active: all launches run in the background."
        )
    when_not_to_use = """
When NOT to use the Agent tool:
- If you want to read a specific file path, use the read tool or the find tool instead of the Agent tool, to find the match more quickly
- If you are searching for a specific class definition like "class Foo", use the find tool instead, to find the match more quickly
- If you are searching for code within a specific file or set of 2-3 files, use the read tool instead of the Agent tool, to find the match more quickly
- Other tasks that are not related to the agent descriptions above
""" if not fork_mode else ""
    concurrency = """
- Launch multiple agents concurrently whenever possible, to maximize performance; to do that, use a single message with multiple tool uses""" if not list_via_messages else ""
    background = """
- You can optionally run agents in the background using the run_in_background parameter. When an agent runs in the background, you will be automatically notified when it completes — do NOT sleep, poll, or proactively check on its progress. Continue with other work or respond to the user instead.
- **Foreground vs background**: Use foreground (default) when you need the agent's results before you can proceed — e.g., research agents whose findings inform your next steps. Use background when you have genuinely independent work to do in parallel.""" if not background_disabled() and not fork_mode else ""
    continuation = (
        "Each non-fork Agent invocation starts without context — provide a complete task description."
        if fork_mode else "Each Agent invocation starts fresh — provide a complete task description."
    )
    when_to_fork = """

## When to fork

When you need to delegate work that benefits from full conversation context (e.g., continuing a multi-file refactor where the child needs the same system prompt and history), omit subagent_type. For most tasks, prefer specialized agent types (Explore, Plan, general-purpose).

**Don't peek.** The tool result includes an `output_file` path — do not Read or tail it unless the user explicitly asks for a progress check. You get a completion notification; trust it.

**Don't race.** After launching, you know nothing about what the fork found. Never fabricate or predict fork results. If the user asks a follow-up before the notification lands, tell them the fork is still running.

**Writing a fork prompt.** Since the fork inherits your context, the prompt is a *directive* — what to do, not what the situation is. Be specific about scope. Don't re-explain background.
""" if fork_mode else ""
    writing_prefix = "When spawning an agent with an explicit subagent_type, it starts with zero context. " if fork_mode else ""
    terse = "For non-fork agents, terse" if fork_mode else "Terse"
    writing = f"""

## Writing the prompt

{writing_prefix}Brief the agent like a smart colleague who just walked into the room — it hasn't seen this conversation, doesn't know what you've tried, doesn't understand why this task matters.
- Explain what you're trying to accomplish and why, what you've already learned or ruled out, and enough context for the agent to make judgment calls.
- If you need a short response, say so ("report in under 200 words").
- Lookups: hand over the exact command. Investigations: hand over the question — prescribed steps become dead weight when the premise is wrong.

{terse} command-style prompts produce shallow, generic work.

**Never delegate understanding.** Don't write "based on your findings, fix the bug" or "based on the research, implement it." Write prompts that prove you understood: include file paths, line numbers, what specifically to change.
"""
    unaware = "" if fork_mode else ", since it is not aware of the user's intent"
    return f"""{shared}
{when_not_to_use}

Usage notes:
- Always include a short description (3-5 words) summarizing what the agent will do{concurrency}
- When the agent is done, it will return a single message back to you. The result returned by the agent is not visible to the user. To show the user the result, you should send a text message back to the user with a concise summary of the result.{background}
- To continue a previously spawned agent, use SendMessage with the agent's ID or name as the `to` field. The agent resumes with its full context preserved. {continuation}
- The agent's outputs should generally be trusted
- Clearly tell the agent whether you expect it to write code or just to do research (search, file reads, web fetches, etc.){unaware}
- If the agent description mentions that it should be used proactively, then you should try your best to use it without the user having to ask for it first. Use your judgement.
- If the user specifies that they want you to run agents "in parallel", you MUST send a single message with multiple Agent tool use content blocks. For example, if you need to launch both a build-validator agent and a test-runner agent in parallel, send a single message with both tool calls.
- You can optionally set `isolation: "worktree"` to run the agent in a temporary git worktree, giving it an isolated copy of the repository. The worktree is automatically cleaned up if the agent makes no changes; if changes are made, the worktree path and branch are returned in the result.{when_to_fork}{writing}"""

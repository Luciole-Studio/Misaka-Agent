"""CLI argument parsing and help display."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Literal, TextIO

from misaka.ai.types import ModelThinkingLevel
from misaka.config import APP_NAME, CONFIG_DIR_NAME, ENV_AGENT_DIR, ENV_SESSION_DIR
from misaka.core.extensions.types import ExtensionFlag

Mode = Literal["text", "json"]


@dataclass(slots=True)
class ArgDiagnostic:
    type: Literal["warning", "error"]
    message: str


@dataclass(slots=True)
class Args:
    provider: str | None = None
    model: str | None = None
    apiKey: str | None = None
    systemPrompt: str | None = None
    appendSystemPrompt: list[str] | None = None
    thinking: ModelThinkingLevel | None = None
    continue_: bool = False
    resume: bool = False
    help: bool = False
    version: bool = False
    mode: Mode | None = None
    noSession: bool = False
    session: str | None = None
    fork: str | None = None
    sessionDir: str | None = None
    models: list[str] | None = None
    tools: list[str] | None = None
    noTools: bool = False
    noBuiltinTools: bool = False
    extensions: list[str] | None = None
    noExtensions: bool = False
    print: bool = False
    export: str | None = None
    promptTemplates: list[str] | None = None
    noPromptTemplates: bool = False
    themes: list[str] | None = None
    useTheme: str | None = None
    noThemes: bool = False
    noContextFiles: bool = False
    listModels: str | bool | None = None
    verbose: bool = False
    messages: list[str] = field(default_factory=list)
    fileArgs: list[str] = field(default_factory=list)
    unknownFlags: dict[str, bool | str] = field(default_factory=dict)
    diagnostics: list[ArgDiagnostic] = field(default_factory=list)


VALID_THINKING_LEVELS: tuple[ModelThinkingLevel, ...] = ("off", "minimal", "low", "medium", "high", "xhigh")


def is_valid_thinking_level(level: str) -> bool:
    return level in VALID_THINKING_LEVELS


def parse_args(args: list[str]) -> Args:
    result = Args()

    index = 0
    while index < len(args):
        arg = args[index]
        has_next = index + 1 < len(args)

        if arg in {"--help", "-h"}:
            result.help = True
        elif arg in {"--version", "-v"}:
            result.version = True
        elif arg == "--mode" and has_next:
            mode = args[index + 1]
            if mode in {"text", "json"}:
                result.mode = mode
            index += 1
        elif arg in {"--continue", "-c"}:
            result.continue_ = True
        elif arg in {"--resume", "-r"}:
            result.resume = True
        elif arg == "--provider" and has_next:
            result.provider = args[index + 1]
            index += 1
        elif arg == "--model" and has_next:
            result.model = args[index + 1]
            index += 1
        elif arg == "--api-key" and has_next:
            result.apiKey = args[index + 1]
            index += 1
        elif arg == "--system-prompt" and has_next:
            result.systemPrompt = args[index + 1]
            index += 1
        elif arg == "--append-system-prompt" and has_next:
            result.appendSystemPrompt = result.appendSystemPrompt or []
            result.appendSystemPrompt.append(args[index + 1])
            index += 1
        elif arg == "--no-session":
            result.noSession = True
        elif arg == "--session" and has_next:
            result.session = args[index + 1]
            index += 1
        elif arg == "--fork" and has_next:
            result.fork = args[index + 1]
            index += 1
        elif arg == "--session-dir" and has_next:
            result.sessionDir = args[index + 1]
            index += 1
        elif arg == "--models" and has_next:
            result.models = [item.strip() for item in args[index + 1].split(",")]
            index += 1
        elif arg in {"--no-tools", "-nt"}:
            result.noTools = True
        elif arg in {"--no-builtin-tools", "-nbt"}:
            result.noBuiltinTools = True
        elif arg in {"--tools", "-t"} and has_next:
            result.tools = [item.strip() for item in args[index + 1].split(",") if item.strip()]
            index += 1
        elif arg == "--thinking" and has_next:
            level = args[index + 1]
            if is_valid_thinking_level(level):
                result.thinking = level
            else:
                result.diagnostics.append(
                    ArgDiagnostic(
                        type="warning",
                        message=(
                            f'Invalid thinking level "{level}". '
                            f"Valid values: {', '.join(VALID_THINKING_LEVELS)}"
                        ),
                    )
                )
            index += 1
        elif arg in {"--print", "-p"}:
            result.print = True
            next_arg = args[index + 1] if has_next else None
            if next_arg is not None and not next_arg.startswith("@") and (
                not next_arg.startswith("-") or next_arg.startswith("---")
            ):
                result.messages.append(next_arg)
                index += 1
        elif arg == "--export" and has_next:
            result.export = args[index + 1]
            index += 1
        elif arg in {"--extension", "-e"} and has_next:
            result.extensions = result.extensions or []
            result.extensions.append(args[index + 1])
            index += 1
        elif arg in {"--no-extensions", "-ne"}:
            result.noExtensions = True
        elif arg == "--prompt-template" and has_next:
            result.promptTemplates = result.promptTemplates or []
            result.promptTemplates.append(args[index + 1])
            index += 1
        elif arg == "--theme" and has_next:
            result.themes = result.themes or []
            result.themes.append(args[index + 1])
            index += 1
        elif arg == "--use-theme":  # Theme for this run only; not written back to settings (pi #7722/9795d602).
            theme_name = args[index + 1] if has_next else None
            if theme_name is None or theme_name.startswith("-"):
                result.diagnostics.append(
                    ArgDiagnostic(type="error", message="--use-theme requires a theme name"))
            else:
                result.useTheme = theme_name
                index += 1
        elif arg in {"--no-prompt-templates", "-np"}:
            result.noPromptTemplates = True
        elif arg == "--no-themes":
            result.noThemes = True
        elif arg in {"--no-context-files", "-nc"}:
            result.noContextFiles = True
        elif arg == "--list-models":
            if has_next and not args[index + 1].startswith("-") and not args[index + 1].startswith("@"):
                result.listModels = args[index + 1]
                index += 1
            else:
                result.listModels = True
        elif arg == "--verbose":
            result.verbose = True
        elif arg.startswith("@"):
            result.fileArgs.append(arg[1:])
        elif arg.startswith("--"):
            flag_name, _, inline_value = arg[2:].partition("=")
            if inline_value:
                result.unknownFlags[flag_name] = inline_value
            else:
                next_arg = args[index + 1] if has_next else None
                if next_arg is not None and not next_arg.startswith("-") and not next_arg.startswith("@"):
                    result.unknownFlags[flag_name] = next_arg
                    index += 1
                else:
                    result.unknownFlags[flag_name] = True
        elif arg.startswith("-"):
            result.diagnostics.append(ArgDiagnostic(type="error", message=f"Unknown option: {arg}"))
        else:
            result.messages.append(arg)

        index += 1

    return result


def print_help(extension_flags: list[ExtensionFlag] | None = None, stream: TextIO | None = None) -> None:
    output = stream or sys.stdout
    extension_flags_text = ""
    if extension_flags:
        lines = []
        for flag in extension_flags:
            value = " <value>" if flag.type == "string" else ""
            description = flag.description or f"Registered by {flag.extensionPath}"
            lines.append(f"  --{flag.name}{value}".ljust(30) + description)
        extension_flags_text = "\nExtension CLI Flags:\n" + "\n".join(lines) + "\n"

    output.write(
        f"""{APP_NAME} - AI coding assistant with read, bash, edit, write tools

Usage:
  {APP_NAME} [options] [@files...] [messages...]

Options:
  --provider <name>              Provider name (default: google)
  --model <pattern>              Model pattern or ID (supports "provider/id" and optional ":<thinking>")
  --api-key <key>                API key (defaults to env vars)
  --system-prompt <text>         System prompt (default: coding assistant prompt)
  --append-system-prompt <text>  Append text or file contents to the system prompt (can be used multiple times)
  --mode <mode>                  Output mode: text (default) or json
  --print, -p                    Non-interactive mode: process prompt and exit
  --continue, -c                 Continue previous session
  --resume, -r                   Select a session to resume
  --session <path|id>            Use specific session file or partial UUID
  --fork <path|id>               Fork specific session file or partial UUID into a new session
  --session-dir <dir>            Directory for session storage and lookup
  --no-session                   Don't save session (ephemeral)
  --models <patterns>            Comma-separated model patterns for Ctrl+P cycling
                                 Supports globs (anthropic/*, *sonnet*) and fuzzy matching
  --no-tools, -nt                Disable all tools by default (built-in and extension)
  --no-builtin-tools, -nbt       Disable built-in tools by default but keep extension/custom tools enabled
  --tools, -t <tools>            Comma-separated allowlist of tool names to enable
                                 Applies to built-in, extension, and custom tools
  --thinking <level>             Set thinking level: off, minimal, low, medium, high, xhigh
  --extension, -e <path>         Load an extension file (can be used multiple times)
  --no-extensions, -ne           Disable extension discovery (explicit -e paths still work)
  --prompt-template <path>       Load a prompt template file or directory (can be used multiple times)
  --no-prompt-templates, -np     Disable prompt template discovery and loading
  --theme <path>                 Load a theme file or directory (can be used multiple times)
  --use-theme <name[/name]>      Set the initial interactive theme for this run (not saved)
  --no-themes                    Disable theme discovery and loading
  --no-context-files, -nc        Disable AGENTS.md and CLAUDE.md discovery and loading
  --export <file>                Export session file to HTML and exit
  --list-models [search]         List available models (with optional fuzzy search)
  --verbose                      Force verbose startup (overrides quietStartup setting)
  --help, -h                     Show this help
  --version, -v                  Show version number

Extensions can register additional flags (e.g., --plan from plan-mode extension).{extension_flags_text}

Examples:
  # Interactive mode
  {APP_NAME}

  # Interactive mode with initial prompt
  {APP_NAME} "List all .ts files in src/"

  # Include files in initial message
  {APP_NAME} @prompt.md @image.png "What color is the sky?"

  # Non-interactive mode (process and exit)
  {APP_NAME} -p "List all .ts files in src/"

  # Multiple messages (interactive)
  {APP_NAME} "Read package.json" "What dependencies do we have?"

  # Continue previous session
  {APP_NAME} --continue "What did we discuss?"

  # Use different model
  {APP_NAME} --provider openai --model gpt-4o-mini "Help me refactor this code"

  # Use model with provider prefix (no --provider needed)
  {APP_NAME} --model openai/gpt-4o "Help me refactor this code"

  # Use model with thinking level shorthand
  {APP_NAME} --model sonnet:high "Solve this complex problem"

  # Limit model cycling to specific models
  {APP_NAME} --models claude-sonnet,claude-haiku,gpt-4o

  # Limit to a specific provider with glob pattern
  {APP_NAME} --models "github-copilot/*"

  # Cycle models with fixed thinking levels
  {APP_NAME} --models sonnet:high,haiku:low

  # Start with a specific thinking level
  {APP_NAME} --thinking high "Solve this complex problem"

  # Read-only mode (no file modifications possible)
  {APP_NAME} --tools read,grep,find,ls -p "Review the code in src/"

  # Export a session file to HTML
  {APP_NAME} --export ~/{CONFIG_DIR_NAME}/agent/sessions/--path--/session.jsonl
  {APP_NAME} --export session.jsonl output.html

Environment Variables:
  ANTHROPIC_API_KEY                - Anthropic Claude API key
  ANTHROPIC_OAUTH_TOKEN            - Anthropic OAuth token (alternative to API key)
  OPENAI_API_KEY                   - OpenAI GPT API key
  AZURE_OPENAI_API_KEY             - Azure OpenAI API key
  AZURE_OPENAI_BASE_URL            - Azure OpenAI/Cognitive Services base URL (e.g. https://{{resource}}.openai.azure.com)
  AZURE_OPENAI_RESOURCE_NAME       - Azure OpenAI resource name (alternative to base URL)
  AZURE_OPENAI_API_VERSION         - Azure OpenAI API version (default: v1)
  AZURE_OPENAI_DEPLOYMENT_NAME_MAP - Azure OpenAI model=deployment map (comma-separated)
  DEEPSEEK_API_KEY                 - DeepSeek API key
  GEMINI_API_KEY                   - Google Gemini API key
  GROQ_API_KEY                     - Groq API key
  CEREBRAS_API_KEY                 - Cerebras API key
  XAI_API_KEY                      - xAI Grok API key
  FIREWORKS_API_KEY                - Fireworks API key
  TOGETHER_API_KEY                 - Together AI API key
  OPENROUTER_API_KEY               - OpenRouter API key
  AI_GATEWAY_API_KEY               - Vercel AI Gateway API key
  ZAI_API_KEY                      - ZAI API key
  MISTRAL_API_KEY                  - Mistral API key
  MINIMAX_API_KEY                  - MiniMax API key
  MOONSHOT_API_KEY                 - Moonshot AI API key
  OPENCODE_API_KEY                 - OpenCode Zen/OpenCode Go API key
  KIMI_API_KEY                     - Kimi For Coding API key
  CLOUDFLARE_API_KEY               - Cloudflare API token (Workers AI and AI Gateway)
  CLOUDFLARE_ACCOUNT_ID            - Cloudflare account id (required for both)
  CLOUDFLARE_GATEWAY_ID            - Cloudflare AI Gateway slug (required for AI Gateway)
  XIAOMI_API_KEY                   - Xiaomi MiMo API key (api.xiaomimimo.com billing)
  XIAOMI_TOKEN_PLAN_CN_API_KEY     - Xiaomi MiMo Token Plan API key (China region)
  XIAOMI_TOKEN_PLAN_AMS_API_KEY    - Xiaomi MiMo Token Plan API key (Amsterdam region)
  XIAOMI_TOKEN_PLAN_SGP_API_KEY    - Xiaomi MiMo Token Plan API key (Singapore region)
  AWS_PROFILE                      - AWS profile for Amazon Bedrock
  AWS_ACCESS_KEY_ID                - AWS access key for Amazon Bedrock
  AWS_SECRET_ACCESS_KEY            - AWS secret key for Amazon Bedrock
  AWS_BEARER_TOKEN_BEDROCK         - Bedrock API key (bearer token)
  AWS_REGION                       - AWS region for Amazon Bedrock (e.g., us-east-1)
  {ENV_AGENT_DIR.ljust(32)} - Config directory (default: ~/{CONFIG_DIR_NAME}/agent)
  {ENV_SESSION_DIR.ljust(32)} - Session storage directory (overridden by --session-dir)

Built-in Tool Names:
  read   - Read file contents
  bash   - Execute bash commands
  edit   - Edit files with find/replace
  write  - Write files (creates/overwrites)
  grep   - Search file contents (read-only, off by default)
  find   - Find files by glob pattern (read-only, off by default)
  ls     - List directory contents (read-only, off by default)
"""
    )


isValidThinkingLevel = is_valid_thinking_level

__all__ = [
    "Args",
    "Mode",
    "isValidThinkingLevel",
    ]

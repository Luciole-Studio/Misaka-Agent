import { spawn } from "node:child_process"
import { mkdtempSync, readFileSync, rmSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { execSync } from "node:child_process"

/**
 * CLI-backed LLM client: routes the answerer + judge stages through a
 * subscription-authenticated CLI (Codex or Claude) instead of a per-token
 * metered API. One process per call; prompt via stdin (sidesteps argv E2BIG);
 * plain text back on stdout / a last-message file. Crash-loud: a nonzero exit
 * or empty output rejects.
 *
 * Enable with `HERMES_MB_LLM_CLI=codex` (primary) or `claude` (fallback).
 */
export type CliLlmBackend = "codex" | "claude"

const CALL_TIMEOUT_MS = Number(process.env.HERMES_MB_CLI_TIMEOUT_MS || 180_000)

export function cliLlmBackend(): CliLlmBackend | null {
  const b = (process.env.HERMES_MB_LLM_CLI || "").trim().toLowerCase()
  return b === "codex" || b === "claude" ? b : null
}

/** Human-readable model id for run-note disclosure. */
export function cliLlmModelId(): string {
  const backend = cliLlmBackend()
  if (backend === "claude") {
    return `${process.env.HERMES_MB_CLAUDE_MODEL || "claude-sonnet-5"} (via claude -p)`
  }
  if (backend === "codex") {
    return `${process.env.HERMES_MB_CODEX_MODEL || "gpt-5.6-sol (codex default)"} (via codex exec)`
  }
  return "n/a"
}

export async function cliComplete(prompt: string): Promise<string> {
  const backend = cliLlmBackend()
  if (!backend) throw new Error("cliComplete called but HERMES_MB_LLM_CLI is not codex|claude")
  const run = () => (backend === "codex" ? codexComplete(prompt) : claudeComplete(prompt))
  try {
    return await run()
  } catch (first) {
    // One retry after a short pause: a transient CLI hiccup (observed: a
    // simultaneous burst of missing codex output files) must not kill an
    // entire 500-call phase. A second consecutive failure is real.
    await new Promise((r) => setTimeout(r, 5000))
    return run()
  }
}

function runProcess(
  command: string,
  args: string[],
  prompt: string,
  env: NodeJS.ProcessEnv,
  readResult: () => string
): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { env })
    let stderr = ""
    const timer = setTimeout(() => {
      child.kill("SIGKILL")
      reject(new Error(`${command} timed out after ${CALL_TIMEOUT_MS}ms`))
    }, CALL_TIMEOUT_MS)

    child.stdout.on("data", () => {}) // agentic/event stdout ignored; result read separately
    child.stderr.on("data", (d) => {
      stderr += d.toString()
    })
    child.on("error", (e) => {
      clearTimeout(timer)
      reject(new Error(`${command} spawn error: ${e.message}`))
    })
    child.on("close", (code) => {
      clearTimeout(timer)
      if (code !== 0) {
        reject(new Error(`${command} exited ${code}: ${stderr.slice(-600)}`))
        return
      }
      try {
        const text = readResult().trim()
        if (!text) {
          reject(new Error(`${command} produced empty output. stderr: ${stderr.slice(-400)}`))
          return
        }
        resolve(text)
      } catch (e) {
        reject(new Error(`${command} output read failed: ${e}`))
      }
    })
    child.stdin.write(prompt)
    child.stdin.end()
  })
}

function codexComplete(prompt: string): Promise<string> {
  const dir = mkdtempSync(join(tmpdir(), "codex-llm-"))
  const outFile = join(dir, "out.txt")
  const effort = process.env.HERMES_MB_CODEX_EFFORT || "low"
  const args = [
    "exec",
    "--skip-git-repo-check",
    "-s",
    "read-only",
    "--ephemeral",
    "-c",
    `model_reasoning_effort=${effort}`,
    "-o",
    outFile,
  ]
  const model = process.env.HERMES_MB_CODEX_MODEL
  if (model) args.push("-m", model)
  args.push("-") // read prompt from stdin
  const done = () => {
    try {
      return readFileSync(outFile, "utf8")
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  }
  return runProcess("codex", args, prompt, process.env, done).catch((e) => {
    rmSync(dir, { recursive: true, force: true })
    throw e
  })
}

/**
 * Fallback backend. Uses the documented robust `claude -p` automation pattern
 * (memory reference_claude_p_automation_auth): an isolated CLAUDE_CONFIG_DIR
 * with an empty settings.json (so the host settings.json z.ai routing is not
 * re-applied), the SDK child-session marker vars scrubbed, and an explicit
 * OAuth credential from the macOS keychain.
 */
function claudeComplete(prompt: string): Promise<string> {
  const dir = mkdtempSync(join(tmpdir(), "claude-llm-"))
  try {
    require("node:fs").writeFileSync(join(dir, "settings.json"), "{}")
  } catch {
    /* best effort */
  }
  const model = process.env.HERMES_MB_CLAUDE_MODEL || "claude-sonnet-5"
  const env: NodeJS.ProcessEnv = { ...process.env, CLAUDE_CONFIG_DIR: dir }
  for (const k of [
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
    "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
  ]) {
    delete env[k]
  }
  if (!env.CLAUDE_CODE_OAUTH_TOKEN && !env.ANTHROPIC_API_KEY) {
    try {
      const raw = execSync(
        `security find-generic-password -s 'Claude Code-credentials' -a "$USER" -w`,
        { encoding: "utf8" }
      )
      const token = JSON.parse(raw)?.claudeAiOauth?.accessToken
      if (token) env.CLAUDE_CODE_OAUTH_TOKEN = token
    } catch {
      /* let claude report "Not logged in" loudly */
    }
  }
  // No stdout side file for claude: capture stdout directly.
  return new Promise<string>((resolve, reject) => {
    const child = spawn("claude", ["-p", "--model", model, "--output-format", "text"], { env })
    let stdout = ""
    let stderr = ""
    const timer = setTimeout(() => {
      child.kill("SIGKILL")
      reject(new Error(`claude timed out after ${CALL_TIMEOUT_MS}ms`))
    }, CALL_TIMEOUT_MS)
    child.stdout.on("data", (d) => (stdout += d.toString()))
    child.stderr.on("data", (d) => (stderr += d.toString()))
    child.on("error", (e) => {
      clearTimeout(timer)
      rmSync(dir, { recursive: true, force: true })
      reject(new Error(`claude spawn error: ${e.message}`))
    })
    child.on("close", (code) => {
      clearTimeout(timer)
      rmSync(dir, { recursive: true, force: true })
      if (code !== 0) return reject(new Error(`claude exited ${code}: ${stderr.slice(-600)}`))
      const text = stdout.trim()
      if (!text) return reject(new Error(`claude produced empty output. stderr: ${stderr.slice(-400)}`))
      resolve(text)
    })
    child.stdin.write(prompt)
    child.stdin.end()
  })
}

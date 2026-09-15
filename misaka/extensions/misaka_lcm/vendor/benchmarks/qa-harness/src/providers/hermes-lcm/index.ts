import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process"
import { existsSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import type {
  Provider,
  ProviderConfig,
  IngestOptions,
  IngestResult,
  SearchOptions,
  IndexingProgressCallback,
} from "../../types/provider"
import type { UnifiedSession } from "../../types/unified"
import { logger } from "../../utils/logger"
import { HERMES_LCM_PROMPTS } from "./prompts"

const DEFAULT_REPO = "/Volumes/LEXAR/hermes-work/hermes-lcm"
const INITIALIZE_TIMEOUT_MS = 300_000 // model download/load on first warmup can be slow
const REQUEST_TIMEOUT_MS = 120_000

interface BridgeResponse {
  ok: boolean
  error?: string
  [key: string]: unknown
}

/**
 * hermes-lcm memory provider.
 *
 * hermes-lcm is a Python/SQLite lossless-context-management plugin, so the
 * provider drives a long-lived Python bridge (`bridge/hermes_lcm_bridge.py`)
 * over newline-delimited JSON on stdin/stdout — the same "persistent backend
 * handle" shape as the Zep provider's SDK client. Ingest accumulates each
 * harness session into a per-container LCM store; search calls the PRODUCTION
 * `tools.lcm_recall` and returns its hits as `{content, metadata}`.
 *
 * Requests are serialized (single pipe, single in-flight request) and the
 * provider is crash-loud: if the bridge exits, the pending call rejects and
 * every subsequent call throws rather than silently degrading.
 */
export class HermesLcmProvider implements Provider {
  name = "hermes-lcm"
  prompts = HERMES_LCM_PROMPTS
  // Single Python process + SQLite + one pipe => run every phase sequentially.
  concurrency = { default: 1 }

  private proc: ChildProcessWithoutNullStreams | null = null
  private stdoutBuffer = ""
  private pending: {
    resolve: (r: BridgeResponse) => void
    reject: (e: Error) => void
    timer: ReturnType<typeof setTimeout>
  } | null = null
  private queue: Promise<unknown> = Promise.resolve()
  private deadError: Error | null = null

  async initialize(_config: ProviderConfig): Promise<void> {
    const repo = process.env.HERMES_LCM_REPO || DEFAULT_REPO
    const python =
      process.env.HERMES_LCM_PYTHON || join(repo, ".venv-fastembed", "bin", "python")
    const script = join(import.meta.dir, "bridge", "hermes_lcm_bridge.py")

    if (!existsSync(python)) {
      throw new Error(
        `hermes-lcm python not found at ${python}. Set HERMES_LCM_PYTHON or create the fastembed venv (see provider README).`
      )
    }
    if (!existsSync(script)) {
      throw new Error(`hermes-lcm bridge script not found at ${script}`)
    }

    const workdir = process.env.HERMES_MB_WORKDIR || join(tmpdir(), "hermes-lcm-mb")
    const env: Record<string, string> = {
      ...process.env,
      HERMES_LCM_REPO: repo,
      HERMES_MB_WORKDIR: workdir,
      HERMES_MB_PROVIDER: process.env.HERMES_MB_PROVIDER || "fastembed",
      PYTHONUNBUFFERED: "1",
    }

    this.proc = spawn(python, [script, "serve"], { env }) as ChildProcessWithoutNullStreams
    this.proc.stdout.setEncoding("utf8")
    this.proc.stderr.setEncoding("utf8")

    this.proc.stdout.on("data", (chunk: string) => this.onStdout(chunk))
    this.proc.stderr.on("data", (chunk: string) => {
      for (const line of chunk.split("\n")) {
        if (line.trim()) logger.debug(`[hermes-lcm] ${line}`)
      }
    })
    this.proc.on("exit", (code, signal) => {
      this.markDead(new Error(`hermes-lcm bridge exited (code=${code}, signal=${signal})`))
    })
    this.proc.on("error", (err) => {
      this.markDead(new Error(`hermes-lcm bridge process error: ${err.message}`))
    })

    const resp = await this.request({ cmd: "initialize" }, INITIALIZE_TIMEOUT_MS)
    logger.info(
      `Initialized hermes-lcm provider (provider=${resp.provider}, model=${resp.model}, dim=${resp.dim})`
    )
  }

  async ingest(sessions: UnifiedSession[], options: IngestOptions): Promise<IngestResult> {
    const documentIds: string[] = []
    // The harness calls ingest one session at a time, but honor a batch too.
    for (const session of sessions) {
      const resp = await this.request({
        cmd: "ingest",
        containerTag: options.containerTag,
        session,
      })
      const ids = (resp.documentIds as string[]) || []
      documentIds.push(...ids)
    }
    return { documentIds }
  }

  async awaitIndexing(
    result: IngestResult,
    _containerTag: string,
    onProgress?: IndexingProgressCallback
  ): Promise<void> {
    // Ingest is fully synchronous (embeddings recorded inline), so indexing is
    // instant. Report every document as completed for the progress tracker.
    onProgress?.({
      completedIds: result.documentIds,
      failedIds: [],
      total: result.documentIds.length,
    })
  }

  async search(query: string, options: SearchOptions): Promise<unknown[]> {
    const resp = await this.request({
      cmd: "search",
      containerTag: options.containerTag,
      query,
      limit: options.limit ?? 25,
    })
    if (resp.degraded) {
      logger.debug(`[hermes-lcm] search degraded: ${resp.degraded_reason}`)
    }
    return (resp.results as unknown[]) || []
  }

  async clear(containerTag: string): Promise<void> {
    if (this.deadError) return
    try {
      await this.request({ cmd: "clear", containerTag })
    } catch (e) {
      logger.warn(`Failed to clear hermes-lcm container ${containerTag}: ${e}`)
    }
  }

  // -- bridge plumbing ------------------------------------------------------

  private onStdout(chunk: string): void {
    this.stdoutBuffer += chunk
    let newlineIndex: number
    while ((newlineIndex = this.stdoutBuffer.indexOf("\n")) !== -1) {
      const line = this.stdoutBuffer.slice(0, newlineIndex).trim()
      this.stdoutBuffer = this.stdoutBuffer.slice(newlineIndex + 1)
      if (!line) continue
      const pending = this.pending
      this.pending = null
      if (!pending) {
        logger.warn(`[hermes-lcm] unexpected bridge output: ${line}`)
        continue
      }
      clearTimeout(pending.timer)
      try {
        pending.resolve(JSON.parse(line) as BridgeResponse)
      } catch (e) {
        pending.reject(new Error(`hermes-lcm bridge sent invalid JSON: ${line} (${e})`))
      }
    }
  }

  private markDead(err: Error): void {
    if (!this.deadError) this.deadError = err
    if (this.pending) {
      clearTimeout(this.pending.timer)
      this.pending.reject(err)
      this.pending = null
    }
  }

  /** Serialize requests: one line on the pipe at a time. */
  private request(payload: Record<string, unknown>, timeoutMs = REQUEST_TIMEOUT_MS): Promise<BridgeResponse> {
    const run = async (): Promise<BridgeResponse> => {
      if (this.deadError) throw this.deadError
      if (!this.proc) throw new Error("hermes-lcm bridge not started")
      const resp = await new Promise<BridgeResponse>((resolve, reject) => {
        const timer = setTimeout(
          () => this.markDead(new Error(`hermes-lcm bridge timed out after ${timeoutMs}ms on ${payload.cmd}`)),
          timeoutMs
        )
        this.pending = { resolve, reject, timer }
        this.proc!.stdin.write(JSON.stringify(payload) + "\n")
      })
      if (!resp.ok) {
        throw new Error(`hermes-lcm ${payload.cmd} failed: ${resp.error}`)
      }
      return resp
    }
    // Chain onto the queue so calls never interleave on the shared pipe.
    const result = this.queue.then(run, run)
    this.queue = result.then(
      () => undefined,
      () => undefined
    )
    return result
  }
}

export default HermesLcmProvider

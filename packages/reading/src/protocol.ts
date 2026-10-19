/** The NDJSON duplex channel between the Python host and this engine.
 *
 * stdout carries protocol lines only; stderr carries diagnostics. Text deltas travel
 * inside JSON too, so the host can render them in the TUI without parsing prose.
 *
 * Two tables, deliberately separate:
 *   - ``pending``: source calls *we* sent and the host has not answered yet.
 *   - ``inflight``: requests the host sent and we are still executing (for cancel).
 * A single table would let a ``source_result`` settle a top-level request (or the reverse)
 * whenever the two ids happened to be close; the ids never share a namespace on the wire,
 * but a reply arriving while another request is running is normal.
 */

import { createInterface, type Interface as ReadlineInterface } from "node:readline";
import type { ModelHandover } from "./types.js";

export type SourceMethod =
  | "source_add"
  | "source_list"
  | "source_outline"
  | "source_read"
  | "source_find";

export interface HelloMessage {
  type: "hello";
  workspace: string;
  runsDirectory?: string;
  /** Complete request parameters from the host's model registry. Key stays in memory. */
  model?: ModelHandover;
  /** What the reader wants out of this run, if they said so. */
  readerIntent?: string;
  persona?: Persona;
}

export interface RequestMessage {
  type: "request";
  id: string;
  method: string;
  params: Record<string, unknown>;
}

export interface SourceResultMessage {
  type: "source_result";
  id: string;
  result?: unknown;
  error?: string;
}

export interface CancelMessage {
  type: "cancel";
  requestId: string;
}

export interface Persona {
  leader: string;
  study: string;
}

export type InboundMessage = HelloMessage | RequestMessage | SourceResultMessage | CancelMessage;

export interface TextMessage {
  type: "text";
  requestId: string;
  role: "leader" | "study" | "compare";
  nodeId?: string;
  delta: string;
}

export interface SourceCallMessage {
  type: "source_call";
  id: string;
  method: SourceMethod;
  params: Record<string, unknown>;
}

export interface GraphChangedMessage {
  type: "graph_changed";
  requestId: string;
  runId: string;
  nodeIds: string[];
}

export interface ResultMessage {
  type: "result";
  requestId: string;
  result: unknown;
}

export interface ErrorMessage {
  type: "error";
  requestId?: string;
  id?: string;
  message: string;
  kind?: string;
}

export interface ReadyMessage {
  type: "ready";
  pid: number;
  node: string;
}

export type OutboundMessage =
  | TextMessage
  | SourceCallMessage
  | GraphChangedMessage
  | ResultMessage
  | ErrorMessage
  | ReadyMessage;

interface PendingSourceCall {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer?: NodeJS.Timeout;
}

const DEFAULT_SOURCE_TIMEOUT_MS = 120_000;

export class ProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProtocolError";
  }
}

export class Channel {
  private readonly out: NodeJS.WritableStream;
  private readonly pending = new Map<string, PendingSourceCall>();
  private readonly inflight = new Map<string, AbortController>();
  private readonly reader: ReadlineInterface;
  private helloHandler: ((hello: HelloMessage) => void) | null = null;
  private requestHandler:
    | ((message: RequestMessage, signal: AbortSignal) => Promise<unknown>)
    | null = null;
  private sequence = 0;
  private closed = false;

  constructor(input: NodeJS.ReadableStream = process.stdin, output: NodeJS.WritableStream = process.stdout) {
    this.out = output;
    this.reader = createInterface({ input, crlfDelay: Infinity });
    this.reader.on("line", (line) => this.receive(line));
    this.reader.on("close", () => this.shutdown(new ProtocolError("The host closed the channel.")));
  }

  onHello(handler: (hello: HelloMessage) => void): void {
    this.helloHandler = handler;
  }

  onRequest(handler: (message: RequestMessage, signal: AbortSignal) => Promise<unknown>): void {
    this.requestHandler = handler;
  }

  /** Announce that the engine is up and reading stdin. */
  ready(): void {
    this.send({ type: "ready", pid: process.pid, node: process.version });
  }

  private write(message: OutboundMessage): void {
    if (this.closed) return;
    this.out.write(`${JSON.stringify(message)}\n`);
  }

  private send(message: OutboundMessage): void {
    this.write(message);
  }

  text(requestId: string, role: TextMessage["role"], delta: string, nodeId?: string): void {
    if (!delta) return;
    this.send({ type: "text", requestId, role, delta, ...(nodeId ? { nodeId } : {}) });
  }

  graphChanged(requestId: string, runId: string, nodeIds: string[]): void {
    this.send({ type: "graph_changed", requestId, runId, nodeIds });
  }

  result(requestId: string, result: unknown): void {
    this.send({ type: "result", requestId, result });
  }

  error(message: string, ids: { requestId?: string; id?: string } = {}, kind?: string): void {
    this.send({ type: "error", message, ...ids, ...(kind ? { kind } : {}) });
  }

  /** Ask the host for corpus content. Resolves when ``source_result`` for this id arrives. */
  callSource(method: SourceMethod, params: Record<string, unknown>, timeoutMs = DEFAULT_SOURCE_TIMEOUT_MS): Promise<unknown> {
    const id = `src-${++this.sequence}`;
    return new Promise<unknown>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new ProtocolError(`The host did not answer ${method} within ${Math.round(timeoutMs / 1000)}s.`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.send({ type: "source_call", id, method, params });
    });
  }

  private receive(line: string): void {
    const trimmed = line.trim();
    if (!trimmed) return;
    let message: InboundMessage;
    try {
      message = JSON.parse(trimmed) as InboundMessage;
    } catch {
      process.stderr.write(`reading: unreadable line from the host: ${trimmed.slice(0, 200)}\n`);
      return;
    }
    switch (message.type) {
      case "hello":
        this.helloHandler?.(message);
        return;
      case "source_result":
        this.settleSource(message);
        return;
      case "cancel":
        this.inflight.get(message.requestId)?.abort();
        return;
      case "request":
        void this.dispatch(message);
        return;
      default:
        process.stderr.write(`reading: unknown message type ${JSON.stringify((message as { type?: string }).type)}\n`);
    }
  }

  private settleSource(message: SourceResultMessage): void {
    const pending = this.pending.get(message.id);
    if (!pending) {
      process.stderr.write(`reading: source_result for unknown id ${message.id}\n`);
      return;
    }
    this.pending.delete(message.id);
    if (pending.timer) clearTimeout(pending.timer);
    if (message.error) pending.reject(new ProtocolError(message.error));
    else pending.resolve(message.result);
  }

  private async dispatch(message: RequestMessage): Promise<void> {
    const handler = this.requestHandler;
    if (!handler) {
      this.error("The engine is not ready yet.", { requestId: message.id });
      return;
    }
    if (this.inflight.has(message.id)) {
      this.error(`Request ${message.id} is already running.`, { requestId: message.id });
      return;
    }
    const controller = new AbortController();
    this.inflight.set(message.id, controller);
    try {
      const result = await handler(message, controller.signal);
      this.result(message.id, result);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      this.error(detail, { requestId: message.id });
    } finally {
      this.inflight.delete(message.id);
    }
  }

  private shutdown(reason: Error): void {
    if (this.closed) return;
    this.closed = true;
    for (const [id, pending] of this.pending) {
      this.pending.delete(id);
      if (pending.timer) clearTimeout(pending.timer);
      pending.reject(reason);
    }
    for (const controller of this.inflight.values()) controller.abort();
    this.inflight.clear();
  }

  close(): void {
    this.reader.close();
    this.shutdown(new ProtocolError("The engine is shutting down."));
  }
}

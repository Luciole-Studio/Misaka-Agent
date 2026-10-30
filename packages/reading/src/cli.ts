#!/usr/bin/env node
/** The engine process MISAKA starts for one reading command.
 *
 * This is *not* a terminal CLI: the user-facing command line (argument parsing, `--json`,
 * progress rendering, the chat prompt) lives in `misaka/reading/host.py`, which also owns
 * the corpus. This process speaks the NDJSON protocol on stdin/stdout for exactly that
 * reason -- it cannot open materials by itself, and pretending otherwise would mean
 * re-implementing document ingestion here.
 *
 * Lifecycle: `hello` (workspace, model handover, persona) → any number of `request`s,
 * each answered with progress lines and one `result`/`error` → stdin closes, we exit.
 */

import process from "node:process";

import { Channel, type HelloMessage, type RequestMessage } from "./protocol.js";
import { ReadingEngine } from "./orchestrator.js";
import { resolveRunDirectory, runPaths } from "./graph.js";
import { SourceBridge } from "./sources.js";
import { isActionId, isContextMode, type ActionRequest, type BranchSpec, type ReadingGraph } from "./types.js";
import { defaultAgentDir } from "./runtime.js";

interface Params {
  [key: string]: unknown;
}

function str(params: Params, key: string): string | undefined {
  const value = params[key];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function requireStr(params: Params, key: string): string {
  const value = str(params, key);
  if (!value) throw new Error(`Missing "${key}".`);
  return value;
}

function strList(params: Params, key: string): string[] {
  const value = params[key];
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())).map((item) => item.trim());
}

function dictList(params: Params, key: string): Params[] {
  const value = params[key];
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Params => Boolean(item) && typeof item === "object" && !Array.isArray(item));
}

function actionOf(params: Params, fallback: string): string {
  const value = str(params, "action") ?? fallback;
  if (!isActionId(value)) {
    throw new Error(`Unknown action "${value}". Use one of: overview, reconstruct, presuppose, transform, compare, translate, reread, generate.`);
  }
  return value;
}

function modeOf(params: Params, fallback: string): string {
  const value = str(params, "contextMode") ?? str(params, "mode") ?? fallback;
  if (!isContextMode(value)) {
    throw new Error(`Unknown context mode "${value}". Use one of: continue, independent, compare, reread.`);
  }
  return value;
}

class EngineProcess {
  private engine: ReadingEngine | null = null;
  private hello: HelloMessage | null = null;

  constructor(private readonly channel: Channel) {}

  private async ready(): Promise<ReadingEngine> {
    if (this.engine) return this.engine;
    const hello = this.hello;
    if (!hello) throw new Error("The host has not sent hello yet.");
    if (!hello.workspace) throw new Error("hello.workspace is required.");

    this.engine = new ReadingEngine({
      bridge: new SourceBridge(this.channel),
      workspace: hello.workspace,
      ...(hello.runsDirectory ? { runsDirectory: hello.runsDirectory } : {}),
      ...(hello.model ? { handover: hello.model } : {}),
      agentDir: process.env.MISAKA_READING_AGENT_DIR || defaultAgentDir(),
      ...(hello.persona ? { persona: hello.persona } : {}),
      ...(hello.readerIntent ? { readerIntent: hello.readerIntent } : {}),
    });
    return this.engine;
  }

  setHello(hello: HelloMessage): void {
    this.hello = hello;
  }

  async handle(message: RequestMessage): Promise<unknown> {
    const engine = await this.ready();
    const requestId = message.id;
    engine.beginRequest({
      text: (role, delta, nodeId) => this.channel.text(requestId, role, delta, nodeId),
      graphChanged: (runId, nodeIds) => this.channel.graphChanged(requestId, runId, nodeIds),
      note: (line) => process.stderr.write(`reading: ${line}\n`),
    });
    try {
      return await this.dispatch(engine, message);
    } finally {
      engine.endRequest();
    }
  }

  private async dispatch(engine: ReadingEngine, message: RequestMessage): Promise<unknown> {
    const params = message.params ?? {};
    // SourceBridge needs the channel; ReadingEngine took it as an option. Rebuild the
    // bridge with the same channel on every request is unnecessary -- see bridgeFor below.
    switch (message.method) {
      case "open": {
        const sources = strList(params, "sources");
        if (!sources.length) throw new Error("open needs at least one source.");
        const question = requireStr(params, "question");
        const run = await engine.openReading(sources, question, {
          workspace: this.hello?.workspace ?? process.cwd(),
          ...(this.hello?.runsDirectory ? { runsDirectory: this.hello.runsDirectory } : {}),
        });
        return {
          runId: run.runId,
          runDirectory: run.runDirectory,
          currentNodeId: run.graph.currentNodeId,
        };
      }
      case "act":
        return this.act(engine, params);
      case "fork": {
        const runId = requireStr(params, "runId");
        const nodeId = requireStr(params, "nodeId");
        const graph = await this.load(engine, runId);
        const parent = node(graph, nodeId);
        const asked = str(params, "question") ?? parent.question;
        const action = actionOf(params, "reconstruct");
        const lenses = strList(params, "lenses");
        if (!lenses.length) throw new Error("fork needs at least one --lens.");
        const branches: BranchSpec[] = lenses.map((lens) => ({
          label: lens,
          question: asked,
          lens,
          action: action as BranchSpec["action"],
          contextMode: "independent",
        }));
        const nodeIds = await engine.fork(runId, nodeId, branches);
        return { runId, nodeIds };
      }
      case "compare": {
        const runId = requireStr(params, "runId");
        const nodeIds = strList(params, "nodeIds");
        if (nodeIds.length < 2) throw new Error("compare needs at least two node ids.");
        const question = requireStr(params, "question");
        const nodeId = await engine.compare(runId, nodeIds, question);
        const graph = await this.load(engine, runId);
        return { runId, nodeId, bodyPath: node(graph, nodeId).bodyPath };
      }
      case "reread": {
        const runId = requireStr(params, "runId");
        const oldNodeId = str(params, "nodeId") ?? requireStr(params, "node");
        const using = strList(params, "usingNodeIds");
        if (!using.length) throw new Error("reread needs at least one --using node.");
        const nodeId = await engine.reread(runId, oldNodeId, using, str(params, "focus"));
        const graph = await this.load(engine, runId);
        return { runId, nodeId, bodyPath: node(graph, nodeId).bodyPath };
      }
      case "graph": {
        const runId = requireStr(params, "runId");
        const graph = await this.load(engine, runId);
        const directory = resolveRunDirectory(
          this.hello?.workspace ?? process.cwd(),
          this.hello?.runsDirectory,
          runId,
        );
        const paths = runPaths(directory);
        return {
          runId,
          currentNodeId: graph.currentNodeId,
          runDirectory: directory,
          graphPath: paths.graphPath,
          graphMarkdownPath: paths.graphMarkdownPath,
          readingPath: paths.readingPath,
          nodes: graph.nodes.map((entry) => ({
            id: entry.id,
            question: entry.question,
            ...(entry.lens ? { lens: entry.lens } : {}),
            bodyPath: entry.bodyPath,
            actions: entry.actions.map((action) => action.action),
          })),
        };
      }
      case "chat": {
        const runId = requireStr(params, "runId");
        const messageText = requireStr(params, "message");
        const reply = await engine.chat(runId, messageText);
        return { runId, text: reply };
      }
      default:
        throw new Error(`Unknown method "${message.method}".`);
    }
  }

  private async act(engine: ReadingEngine, params: Params): Promise<unknown> {
    const runId = requireStr(params, "runId");
    const nodeId = requireStr(params, "nodeId");
    const graph = await this.load(engine, runId);
    const parent = node(graph, nodeId);
    const action = actionOf(params, "reconstruct");
    const mode = modeOf(params, action === "compare" ? "compare" : action === "reread" ? "reread" : "continue");
    const request: ActionRequest = {
      runId,
      nodeId,
      action: action as ActionRequest["action"],
      question: str(params, "question") ?? parent.question,
      contextMode: mode as ActionRequest["contextMode"],
      sourceRefs: parent.sourceRefs,
      ...(str(params, "lens") ? { lens: str(params, "lens") as string } : {}),
      ...(str(params, "focus") ? { focus: str(params, "focus") as string } : {}),
      ...(strList(params, "usingNodeIds").length ? { usingNodeIds: strList(params, "usingNodeIds") } : {}),
    };
    const outcome = await engine.act(request);
    return { runId: outcome.runId, nodeId: outcome.nodeId, bodyPath: outcome.bodyPath };
  }

  private async load(engine: ReadingEngine, runId: string): Promise<ReadingGraph> {
    const cached = engine.graphOf(runId);
    if (cached) return cached;
    const directory = resolveRunDirectory(
      this.hello?.workspace ?? process.cwd(),
      this.hello?.runsDirectory,
      runId,
    );
    const run = await engine.resumeReading(directory);
    return run.graph;
  }

  dispose(): void {
    this.engine?.dispose();
    this.engine = null;
  }
}

function node(graph: ReadingGraph, nodeId: string) {
  const found = graph.nodes.find((entry) => entry.id === nodeId);
  if (!found) throw new Error(`No node ${nodeId} in run ${graph.runId}. Nodes: ${graph.nodes.map((entry) => entry.id).join(", ")}`);
  return found;
}

// The engine takes a SourceBridge bound to this channel; materials always travel through
// the host, so this process never opens a file itself.

async function main(): Promise<void> {
  const argv = process.argv.slice(2);
  if (argv.includes("--version")) {
    process.stdout.write(`${process.env.npm_package_version ?? "0.1.0"}\n`);
    return;
  }
  if (argv.includes("--help") || argv.includes("-h")) {
    process.stderr.write(
      "misaka-reading engine: speaks NDJSON on stdin/stdout and is started by `misaka read`.\n" +
        "Use `misaka read --help` for the user-facing commands.\n",
    );
    return;
  }

  const channel = new Channel();
  const runtime = new EngineProcess(channel);
  channel.onHello((hello) => runtime.setHello(hello));
  channel.onRequest((message) => runtime.handle(message));
  channel.ready();
}

void main();

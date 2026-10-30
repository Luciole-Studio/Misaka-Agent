/** The reading run: materials, sessions, and the graph, kept in step.
 *
 * One `ReadingEngine` lives for one Node process. It owns the model runtime, the source
 * bridge, and an in-memory handle on each open run; everything durable is in
 * `reading/<run>/`. Its methods are the same operations the CLI exposes and the leader's
 * own tools call: an action, a fork, a comparison, a reread, and a chat turn.
 *
 * The graph is written once per completed operation, after every study agent has
 * answered. Fork branches run in parallel but only return results; this file appends them
 * and saves, so two branches can never interleave writes to graph.json.
 */

import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { join } from "node:path";

import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import {
  createSubmitTool,
  loadMethod,
  normalizeSubmit,
  renderActionPrompt,
  resultFromText,
  type SubmitSink,
} from "./actions.js";
import {
  DEFAULT_PERSONA,
  createCompareSession,
  createLeaderSession,
  createStudySession,
  lastStopReason,
  runTurn,
  type CreatedSession,
} from "./agents.js";
import { buildActionContext } from "./context.js";
import {
  addEdge,
  appendNodeSection,
  applyResult,
  bodyPathFor,
  createGraph,
  ensureRunDirectories,
  findNode,
  loadGraph,
  newRunId,
  nextNodeId,
  renderSharedMap,
  resolveRunDirectory,
  runPaths,
  saveGraph,
  saveReaderFile,
  writeNodeFile,
  type RunPaths,
} from "./graph.js";
import { buildRuntime, type ReadingRuntime } from "./runtime.js";
import {
  SOURCE_TOOL_NAMES,
  createSourceTools,
  describeSources,
  type Outline,
  type SourceBridge,
} from "./sources.js";
import type {
  ActionId,
  ActionRequest,
  ActionResult,
  BranchSpec,
  OpenOptions,
  ReadingGraph,
  ReadingNode,
  ReadingRun,
  ReadingSource,
  RelationDraft,
  SourceRef,
} from "./types.js";

/** Incremental output, scoped to one request so each line can be attributed to it. */
export interface Progress {
  text(role: "leader" | "study" | "compare", delta: string, nodeId?: string): void;
  graphChanged(runId: string, nodeIds: string[]): void;
  note(line: string): void;
}

const QUIET: Progress = {
  text() {},
  graphChanged() {},
  note() {},
};

const LEADER_TOOL_NAMES = ["reading_act", "reading_fork", "reading_compare", "reading_reread"];
const ROLE_TOOLS = [...SOURCE_TOOL_NAMES, "reading_submit"];

export interface EngineOptions {
  bridge: SourceBridge;
  workspace: string;
  runsDirectory?: string;
  /** Complete request parameters from the host; without them the SDK's own auth is used. */
  handover?: Parameters<typeof buildRuntime>[0]["handover"];
  /** Directory the SDK may read credentials from on the fallback path. */
  agentDir: string;
  persona?: { leader: string; study: string };
  readerIntent?: string;
}

interface OpenRun {
  runId: string;
  runDirectory: string;
  paths: RunPaths;
  graph: ReadingGraph;
  outlines: Map<string, Outline>;
  readerIntent: string;
  leader: CreatedSession | null;
  /** The map last handed to the leader, so a chat turn only re-sends it when it changed. */
  sharedMap: string;
  /** Study sessions by line id, so a continued line keeps its own agent and history. */
  studySessions: Map<string, CreatedSession>;
}

/** What a node looks like to the leader right after a tool wrote it. */
function leaderSummary(graph: ReadingGraph, nodeIds: string[]): string {
  return nodeIds
    .map((id) => {
      const node = graph.nodes.find((candidate) => candidate.id === id);
      if (!node) return `- ${id}（未写入）`;
      const lens = node.lens ? ` · ${node.lens}` : "";
      const questions = node.nextQuestions.length
        ? `\n  后续问题：${node.nextQuestions.join("；")}`
        : "";
      return `- ${node.id} ${node.question}${lens}\n  摘要：${node.summary || "（尚未形成）"}${questions}`;
    })
    .join("\n");
}

function makeNode(input: {
  id: string;
  lineId: string;
  question: string;
  lens?: string;
  refs: SourceRef[];
}): ReadingNode {
  return {
    id: input.id,
    lineId: input.lineId,
    question: input.question,
    ...(input.lens ? { lens: input.lens } : {}),
    summary: "",
    bodyPath: bodyPathFor(input.id),
    sourceRefs: [...input.refs],
    actions: [],
    nextQuestions: [],
    createdAt: new Date().toISOString(),
  };
}

function nextLineId(graph: ReadingGraph): string {
  let highest = -1;
  for (const node of graph.nodes) {
    const match = /^L(\d+)$/.exec(node.lineId);
    if (match) highest = Math.max(highest, Number(match[1]));
  }
  return `L${highest + 1}`;
}

function sessionRecord(
  created: CreatedSession,
  role: "leader" | "study" | "compare",
  model: string | undefined,
  label: { nodeId?: string; lineId?: string },
) {
  return {
    sessionId: created.sessionId,
    role,
    ...(label.nodeId ? { nodeId: label.nodeId } : {}),
    ...(label.lineId ? { lineId: label.lineId } : {}),
    sessionFile: created.sessionFile,
    ...(model ? { model } : {}),
    createdAt: new Date().toISOString(),
  };
}

/** Relations worth keeping: only those naming a node this run actually has. */
function validRelations(graph: ReadingGraph, nodeId: string, drafts: RelationDraft[]) {
  const known = new Set(graph.nodes.map((node) => node.id));
  return drafts
    .filter((draft) => draft.toNodeId && known.has(draft.toNodeId) && draft.toNodeId !== nodeId)
    .map((draft) => ({
      from: nodeId,
      to: draft.toNodeId as string,
      kind: draft.kind,
      explanation: draft.explanation,
    }));
}

function mergeSourceRefs(nodes: ReadingNode[]): SourceRef[] {
  const seen = new Set<string>();
  const out: SourceRef[] = [];
  for (const node of nodes) {
    for (const ref of node.sourceRefs) {
      const key = `${ref.docId}|${ref.outlineNode ?? ""}|${ref.pageStart ?? ""}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(ref);
    }
  }
  return out;
}

export class ReadingEngine {
  private readonly options: EngineOptions;
  private readonly openRuns = new Map<string, OpenRun>();
  private readonly methods = new Map<ActionId, string>();
  private readonly submitSinks = new WeakMap<CreatedSession["session"], SubmitSink>();
  private runtime: ReadingRuntime | null = null;
  private progress: Progress = QUIET;
  private activeRunId: string | null = null;

  constructor(options: EngineOptions) {
    this.options = options;
  }

  /** Attribute everything until `endRequest` to one request. */
  beginRequest(progress: Progress): void {
    this.progress = progress;
  }

  endRequest(): void {
    this.progress = QUIET;
  }

  private get persona(): { leader: string; study: string } {
    return this.options.persona ?? DEFAULT_PERSONA;
  }

  private note(line: string): void {
    this.progress.note(line);
  }

  private async model(): Promise<ReadingRuntime> {
    if (this.runtime) return this.runtime;
    this.runtime = await buildRuntime({
      ...(this.options.handover ? { handover: this.options.handover } : {}),
      agentDir: this.options.agentDir,
      cwd: this.options.workspace,
    });
    this.note(`model: ${this.runtime.label}`);
    return this.runtime;
  }

  private async method(action: ActionId): Promise<string> {
    const cached = this.methods.get(action);
    if (cached) return cached;
    const text = await loadMethod(action);
    this.methods.set(action, text);
    return text;
  }

  // -- opening and resuming ---------------------------------------------------

  /** Ingest the sources and create the run's entry node; the leader opens on first use. */
  async openReading(sourceInputs: string[], question: string, options: OpenOptions): Promise<ReadingRun> {
    const at = new Date().toISOString();
    const runId = newRunId(question);
    const runDirectory = resolveRunDirectory(options.workspace, options.runsDirectory, runId);
    const paths = await ensureRunDirectories(runDirectory);

    const sources: ReadingSource[] = [];
    const outlines = new Map<string, Outline>();
    for (const input of sourceInputs) {
      this.note(`adding ${input}`);
      const added = await this.options.bridge.add(input, undefined, runDirectory);
      const outline = await this.options.bridge.outline(added.docId);
      outlines.set(added.docId, outline);
      sources.push({
        sourceId: added.docId,
        docId: added.docId,
        title: added.title,
        pages: added.pages,
        ...(added.origin ? { origin: added.origin } : {}),
        ...(outline.nodes?.length ? { outlineMode: outline.mode } : {}),
        addedAt: at,
      });
      this.note(`  ${added.title}: ${added.pages} pages, ${outline.nodes?.length ?? 0} sections`);
    }

    const refs: SourceRef[] = sources.map((source) => ({
      sourceId: source.sourceId,
      docId: source.docId,
      title: source.title,
      ...(source.origin ? { origin: source.origin } : {}),
    }));
    // N0 is the reader's own question, not a reading result: no summary, no action, no
    // model call. It exists so a first action has a node to continue from.
    const root = makeNode({ id: "N0", lineId: "L0", question, refs });
    const graph = createGraph({ runId, question, workspace: options.workspace, rootNode: root, sources });
    await writeNodeFile(paths, root);
    await saveReaderFile(paths, this.options.readerIntent);
    await saveGraph(paths, graph);

    this.openRuns.set(runId, {
      runId,
      runDirectory,
      paths,
      graph,
      outlines,
      readerIntent: this.options.readerIntent ?? "",
      leader: null,
      sharedMap: "",
      studySessions: new Map(),
    });
    return { runId, runDirectory, graph };
  }

  /** Reopen a run from disk. graph.json and the session index are the only inputs. */
  async resumeReading(runDirectory: string): Promise<ReadingRun> {
    const paths = runPaths(runDirectory);
    if (!existsSync(paths.graphPath)) {
      throw new Error(`No reading run at ${runDirectory}: graph.json does not exist.`);
    }
    const graph = await loadGraph(paths);
    const existing = this.openRuns.get(graph.runId);
    if (existing) return { runId: graph.runId, runDirectory, graph: existing.graph };

    const outlines = new Map<string, Outline>();
    for (const source of graph.sources) {
      outlines.set(source.docId, await this.options.bridge.outline(source.docId));
    }
    const readerIntent = existsSync(paths.readerPath)
      ? (await readFile(paths.readerPath, "utf8")).replace(/^# 读者关切\s*\r?\n/, "").trim()
      : this.options.readerIntent ?? "";
    this.openRuns.set(graph.runId, {
      runId: graph.runId,
      runDirectory,
      paths,
      graph,
      outlines,
      readerIntent,
      leader: null,
      sharedMap: "",
      studySessions: new Map(),
    });
    return { runId: graph.runId, runDirectory, graph };
  }

  async openByDirectory(runDirectory: string): Promise<ReadingRun> {
    return this.resumeReading(runDirectory);
  }

  graphOf(runId: string): ReadingGraph | null {
    return this.openRuns.get(runId)?.graph ?? null;
  }

  private async run(runId: string): Promise<OpenRun> {
    const cached = this.openRuns.get(runId);
    if (cached) return cached;
    const runDirectory = resolveRunDirectory(this.options.workspace, this.options.runsDirectory, runId);
    await this.resumeReading(runDirectory);
    const loaded = this.openRuns.get(runId);
    if (!loaded) throw new Error(`No reading run ${runId}.`);
    return loaded;
  }

  private async leader(run: OpenRun): Promise<CreatedSession> {
    if (run.leader) return run.leader;
    const record = run.graph.sessions.find((session) => session.role === "leader");
    const runtime = await this.model();
    const map = renderSharedMap(run.graph);
    const created = await createLeaderSession({
      cwd: run.runDirectory,
      agentDir: this.options.agentDir,
      sessionDirectory: run.paths.sessionsDirectory,
      modelRuntime: runtime.runtime,
      model: runtime.model,
      systemPrompt:
        `${this.persona.leader}\n\n## 读者关切\n\n${run.readerIntent.trim() || "（尚未记录）"}\n\n` +
        `## 这次阅读\n\n- runId：${run.runId}\n- 问题：${run.graph.question}\n` +
        `- 目录：${run.runDirectory}\n\n## 材料\n\n${describeSources(run.graph.sources, run.outlines)}\n\n` +
        `${map}\n` +
        "这张地图由程序根据已写入的节点生成；节点摘要就是研读实际交回的理解。" +
        "读者问起某条路线时，先按地图回答，再决定要不要打开 nodes/ 下的正文或重读原文。\n\n" +
        "## 你手上的工具\n\n" +
        "- reading_act：在一个节点上执行阅读动作。细节补充用 continue；换视角时用 independent。\n" +
        "- reading_fork：用几个互补视角分出新的阅读线。\n" +
        "- reading_compare：等被选路线都写完后装入它们的完整笔记对读。\n" +
        "- reading_reread：带着新的理解回到旧材料重读。\n" +
        "- source_outline / source_read / source_find：你自己查看原文。\n\n" +
        "小的解释可以自己回答；需要真正的研读、分叉或对读时调用工具，等结果回来再向读者说明。\n" +
        "不要编造节点里没有的理解：图与节点正文由程序写入，你决定读什么、怎样串联。\n",
      tools: [...ROLE_TOOLS, ...LEADER_TOOL_NAMES],
      customTools: [...createSourceTools(this.options.bridge), ...this.leaderTools()],
      ...(record?.sessionFile && existsSync(record.sessionFile) ? { sessionFile: record.sessionFile } : {}),
    });
    run.leader = created;
    // The system prompt above already carries this map; keep the chat path from repeating it.
    run.sharedMap = map;
    if (created.resumed) this.note(`resumed leader session ${created.sessionId}`);
    if (!record) {
      run.graph.sessions.push(sessionRecord(created, "leader", runtime.label, {}));
      await saveGraph(run.paths, run.graph);
    } else if (record.sessionFile !== created.sessionFile) {
      // A session file that could not be reopened was replaced; keep the index honest.
      record.sessionFile = created.sessionFile;
      record.sessionId = created.sessionId;
      await saveGraph(run.paths, run.graph);
    }
    return created;
  }

  private leaderTools(): ToolDefinition[] {
    // `this` inside a tool's execute is the tool definition, not the engine.
    const engine = this;
    const act = defineTool({
      name: "reading_act",
      label: "执行阅读动作",
      description:
        "在一个节点上执行一次阅读动作，返回新节点 id 与摘要。" +
        "当前节点补充细节用 continue；换视角用 independent（会新建会话）；" +
        "重读用 reread 并给出 usingNodeIds。",
      parameters: Type.Object({
        action: Type.String({
          description: "overview|reconstruct|presuppose|transform|compare|translate|reread|generate",
        }),
        nodeId: Type.Optional(Type.String({ description: "在哪个节点上继续；缺省为当前节点" })),
        question: Type.Optional(Type.String({ description: "这次要问的问题；缺省沿用节点问题" })),
        lens: Type.Optional(Type.String({ description: "领域视角，例如“概念史”或“经济条件”" })),
        focus: Type.Optional(Type.String({ description: "本次焦点，例如“结尾几段”或某个术语" })),
        usingNodeIds: Type.Optional(
          Type.Array(Type.String({ description: "reread 时带回的新理解所在节点" })),
        ),
      }),
      async execute(_id, params) {
        const run = await engine.currentRun();
        const parent = findNode(run.graph, params.nodeId ?? run.graph.currentNodeId);
        const action = params.action as ActionId;
        const lens = params.lens?.trim() || undefined;
        const contextMode =
          action === "reread" ? "reread" : lens ? "independent" : "continue";
        const answer = await engine.act({
          runId: run.runId,
          nodeId: parent.id,
          action,
          question: params.question?.trim() || parent.question,
          ...(lens ? { lens } : {}),
          ...(params.focus?.trim() ? { focus: params.focus.trim() } : {}),
          contextMode,
          sourceRefs: parent.sourceRefs,
          ...(params.usingNodeIds?.length ? { usingNodeIds: params.usingNodeIds } : {}),
        });
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                runId: answer.runId,
                nodeId: answer.nodeId,
                bodyPath: answer.bodyPath,
                result: answer.result,
              }),
            },
          ],
          details: {},
        };
      },
    });

    const fork = defineTool({
      name: "reading_fork",
      label: "分叉阅读",
      description: "用几个互补的视角从指定节点分出独立阅读线，并行研读后返回各自的节点 id。",
      parameters: Type.Object({
        nodeId: Type.Optional(Type.String({ description: "从哪个节点分叉；缺省为当前节点" })),
        action: Type.Optional(Type.String({ description: "每条线要做的动作，缺省 reconstruct" })),
        lenses: Type.Array(Type.String({ description: "视角" })),
        questions: Type.Optional(
          Type.Array(Type.String({ description: "与 lenses 一一对应的问题；缺省沿用父节点问题" })),
        ),
      }),
      async execute(_id, params) {
        const run = await engine.currentRun();
        const parent = findNode(run.graph, params.nodeId ?? run.graph.currentNodeId);
        const action = (params.action ?? "reconstruct") as ActionId;
        const branches: BranchSpec[] = params.lenses.map((lens, index) => ({
          label: lens,
          question: params.questions?.[index]?.trim() || parent.question,
          lens,
          action,
          contextMode: "independent",
        }));
        const nodeIds = await engine.fork(run.runId, parent.id, branches);
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                runId: run.runId,
                nodeIds,
                nodes: leaderSummary(run.graph, nodeIds),
              }),
            },
          ],
          details: {},
        };
      },
    });

    const compare = defineTool({
      name: "reading_compare",
      label: "对读",
      description: "装入被选节点的完整笔记与相关原文，写一段对读，形成多父节点；节点来自 reading_fork。",
      parameters: Type.Object({
        nodeIds: Type.Array(Type.String({ description: "要被对读的节点" })),
        question: Type.Optional(Type.String({ description: "对读要回答的问题" })),
      }),
      async execute(_id, params) {
        const run = await engine.currentRun();
        if (params.nodeIds.length < 2) throw new Error("reading_compare needs at least two nodes.");
        const nodeId = await engine.compare(run.runId, params.nodeIds, params.question?.trim() || run.graph.question);
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({ runId: run.runId, nodeId, nodes: leaderSummary(run.graph, [nodeId]) }),
            },
          ],
          details: {},
        };
      },
    });

    const reread = defineTool({
      name: "reading_reread",
      label: "重读",
      description: "带着指定节点的新理解回到旧节点的材料重读，形成后继节点。",
      parameters: Type.Object({
        nodeId: Type.String({ description: "旧理解所在的节点" }),
        usingNodeIds: Type.Array(Type.String({ description: "要带回的新理解" })),
        focus: Type.Optional(Type.String({ description: "重读的焦点" })),
      }),
      async execute(_id, params) {
        const run = await engine.currentRun();
        const nodeId = await engine.reread(run.runId, params.nodeId, params.usingNodeIds, params.focus);
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({ runId: run.runId, nodeId, nodes: leaderSummary(run.graph, [nodeId]) }),
            },
          ],
          details: {},
        };
      },
    });

    return [act, fork, compare, reread];
  }

  /** The run the leader turn is about. Only one runs at a time in a process. */
  private async currentRun(): Promise<OpenRun> {
    const id = this.activeRunId;
    if (!id) throw new Error("No reading run is active for this leader turn.");
    return this.run(id);
  }

  // -- one study turn ---------------------------------------------------------

  private async study(
    run: OpenRun,
    node: ReadingNode,
    request: ActionRequest,
    role: "study" | "compare",
    priorSession: CreatedSession | null,
    context: Awaited<ReturnType<typeof buildActionContext>>,
    catalogue: string,
    sessionFile?: string,
  ): Promise<{ result: ActionResult; session: CreatedSession }> {
    const runtime = await this.model();
    // A reused SDK session keeps its tools and their closures. Reset its own sink rather
    // than reading a new sink that the existing reading_submit tool cannot write to.
    const sink: SubmitSink = (priorSession && this.submitSinks.get(priorSession.session)) || { value: null };
    sink.value = null;
    const prompt = renderActionPrompt(context, catalogue);
    const persona =
      role === "compare"
        ? `${this.persona.study}\n\n这次你承担对读：几条路线的完整笔记已经在你面前。` +
          "说明它们共同的结构、概念的对应、分歧最早出现在哪里，以及由此产生的新问题。" +
          "允许它们最终并不合成一个解释。"
        : this.persona.study;
    const spec = {
      cwd: run.runDirectory,
      agentDir: this.options.agentDir,
      sessionDirectory: run.paths.sessionsDirectory,
      modelRuntime: runtime.runtime,
      model: runtime.model,
      systemPrompt: persona,
      tools: ROLE_TOOLS,
      customTools: [...createSourceTools(this.options.bridge), createSubmitTool(sink)],
      ...(sessionFile ? { sessionFile } : {}),
    };

    const created = priorSession ?? (role === "compare" ? await createCompareSession(spec) : await createStudySession(spec));
    this.submitSinks.set(created.session, sink);
    if (!priorSession && created.resumed) this.note(`resumed ${role} session ${created.sessionId}`);
    const streamed = await runTurn(created.session, prompt, (delta) =>
      this.progress.text(role, delta, node.id),
    );

    let result = sink.value ? normalizeSubmit(sink.value, run.graph.sources, context.sourceRefs) : null;
    if (!result) {
      // The agent answered in prose. Keep its reading and say so on the note line rather
      // than presenting an empty node as a result.
      result = resultFromText(streamed, context.sourceRefs);
      if (result) this.note(`${node.id}: the agent replied in prose instead of calling reading_submit`);
    }
    if (!result || (!result.summary && !result.understandingMarkdown)) {
      // Say why, not just that: a turn cut off at the output limit looks identical to a model
      // that stayed silent, and the two need different fixes.
      const reason = lastStopReason(created.session);
      throw new Error(
        `${role} produced no reading for ${node.id} (${request.action}); ` +
          `${streamed.trim() ? "it wrote text but submitted nothing" : "the model returned no text"}` +
          `${reason ? ` — ${reason}` : ""}.`,
      );
    }
    return { result, session: created };
  }

  /** Build the successor node a new stage of reading will be written onto. */
  private async successor(
    run: OpenRun,
    parent: ReadingNode,
    request: ActionRequest,
    contextMode: "fork" | "compare" | "reread",
    refs: SourceRef[],
    parents: string[],
  ): Promise<ReadingNode> {
    const node = makeNode({
      id: nextNodeId(run.graph),
      lineId: nextLineId(run.graph),
      question: request.question,
      ...(request.lens ? { lens: request.lens } : {}),
      refs,
    });
    run.graph.nodes.push(node);
    await writeNodeFile(run.paths, node);
    for (const from of new Set([parent.id, ...parents])) addEdge(run.graph, from, node.id, contextMode);
    return node;
  }

  /** Write one study result onto a node and into the graph; never saves by itself. */
  private async record(
    run: OpenRun,
    node: ReadingNode,
    request: ActionRequest,
    result: ActionResult,
    session: CreatedSession,
    role: "study" | "compare",
  ): Promise<void> {
    const at = new Date().toISOString();
    await appendNodeSection(run.paths, node, request.action, request.contextMode, result.understandingMarkdown, at);
    applyResult(node, result, request.action, request.contextMode, at, session.sessionId);
    if (result.relations.length) {
      run.graph.relations.push(...validRelations(run.graph, node.id, result.relations));
    }
    if (!run.graph.sessions.some((record) => record.sessionId === session.sessionId)) {
      run.graph.sessions.push(
        sessionRecord(session, role, this.runtime?.label, { nodeId: node.id, lineId: node.lineId }),
      );
    }
    run.studySessions.set(node.lineId, session);
    run.graph.currentNodeId = node.id;
  }

  // -- operations -------------------------------------------------------------

  /** Run one action: continue the node, or write a successor for a new stage. */
  async act(request: ActionRequest): Promise<{ runId: string; nodeId: string; bodyPath: string; result: ActionResult }> {
    const run = await this.run(request.runId);
    const parent = findNode(run.graph, request.nodeId);
    const context = await buildActionContext({
      request,
      graph: run.graph,
      paths: run.paths,
      bridge: this.options.bridge,
      sources: run.graph.sources,
      methodInstructions: await this.method(request.action),
      readerIntent: run.readerIntent,
    });
    const catalogue = describeSources(run.graph.sources, run.outlines);
    const role = request.contextMode === "compare" ? "compare" : "study";

    let node = parent;
    let prior: CreatedSession | null =
      request.contextMode === "continue" ? (run.studySessions.get(parent.lineId) ?? null) : null;
    const savedSession = request.contextMode === "continue" && !prior
      ? run.graph.sessions.find((record) => record.sessionId === parent.sessionId)
      : undefined;
    if (request.contextMode === "independent" || request.contextMode === "compare" || request.contextMode === "reread") {
      const parents = (request.usingNodeIds ?? []).filter((id) => run.graph.nodes.some((n) => n.id === id));
      node = await this.successor(
        run,
        parent,
        request,
        request.contextMode === "independent" ? "fork" : request.contextMode,
        context.sourceRefs,
        parents,
      );
      prior = null;
    }

    const { result, session } = await this.study(
      run,
      node,
      request,
      role,
      prior,
      context,
      catalogue,
      savedSession?.sessionFile,
    );
    await this.record(run, node, request, result, session, role);
    await saveGraph(run.paths, run.graph);
    this.progress.graphChanged(run.runId, [node.id]);
    return { runId: run.runId, nodeId: node.id, bodyPath: node.bodyPath, result };
  }

  /** Branch into independent lines, in parallel, then write them one at a time. */
  async fork(runId: string, parentId: string, branches: BranchSpec[]): Promise<string[]> {
    if (!branches.length) throw new Error("fork needs at least one branch.");
    const run = await this.run(runId);
    const parent = findNode(run.graph, parentId);
    const catalogue = describeSources(run.graph.sources, run.outlines);

    // Create every node and its context before any model call, so the parent's map is
    // stable while the branches run.
    const plans = [];
    for (const branch of branches) {
      const node = makeNode({
        id: nextNodeId(run.graph),
        lineId: nextLineId(run.graph),
        question: branch.question,
        lens: branch.lens,
        refs: parent.sourceRefs,
      });
      run.graph.nodes.push(node);
      await writeNodeFile(run.paths, node);
      addEdge(run.graph, parent.id, node.id, "fork", branch.label);
      const request: ActionRequest = {
        runId,
        nodeId: parent.id,
        action: branch.action,
        question: branch.question,
        lens: branch.lens,
        contextMode: branch.contextMode,
        sourceRefs: parent.sourceRefs,
      };
      const context = await buildActionContext({
        request,
        graph: run.graph,
        paths: run.paths,
        bridge: this.options.bridge,
        sources: run.graph.sources,
        methodInstructions: await this.method(branch.action),
        readerIntent: run.readerIntent,
      });
      plans.push({ node, request, context });
    }

    const outcomes = await Promise.all(
      plans.map(async (plan) => this.study(run, plan.node, plan.request, "study", null, plan.context, catalogue)),
    );
    for (let index = 0; index < plans.length; index += 1) {
      const plan = plans[index];
      const outcome = outcomes[index];
      if (!plan || !outcome) continue;
      await this.record(run, plan.node, plan.request, outcome.result, outcome.session, "study");
    }
    await saveGraph(run.paths, run.graph);
    this.progress.graphChanged(runId, plans.map((plan) => plan.node.id));
    return plans.map((plan) => plan.node.id);
  }

  /** A comparison node with one parent per chosen route. */
  async compare(runId: string, nodeIds: string[], question: string): Promise<string> {
    if (nodeIds.length < 2) throw new Error("compare needs at least two nodes.");
    const run = await this.run(runId);
    const nodes = nodeIds.map((id) => findNode(run.graph, id));
    const first = nodes[0];
    if (!first) throw new Error("compare needs at least two nodes.");
    const { nodeId } = await this.act({
      runId,
      nodeId: first.id,
      action: "compare",
      question,
      contextMode: "compare",
      sourceRefs: mergeSourceRefs(nodes),
      usingNodeIds: nodeIds,
    });
    return nodeId;
  }

  /** Carry the chosen understanding back to older material. */
  async reread(runId: string, oldNodeId: string, usingNodeIds: string[], focus?: string): Promise<string> {
    const run = await this.run(runId);
    const old = findNode(run.graph, oldNodeId);
    const { nodeId } = await this.act({
      runId,
      nodeId: old.id,
      action: "reread",
      question: old.question,
      contextMode: "reread",
      sourceRefs: old.sourceRefs,
      usingNodeIds,
      ...(focus?.trim() ? { focus: focus.trim() } : {}),
    });
    return nodeId;
  }

  /** One natural-language turn with the leader. Tools let it call the same operations. */
  async chat(runId: string, message: string): Promise<string> {
    const run = await this.run(runId);
    const leader = await this.leader(run);
    // The SDK does not rebuild a system prompt when the graph changes, and the leader may
    // be resumed from disk. Hand it the current map as context before the turn whenever it
    // differs from the one this process last gave it (creation counts as having given it).
    const map = renderSharedMap(run.graph);
    if (map !== run.sharedMap) {
      await leader.session.sendCustomMessage(
        {
          customType: "reading_map",
          content: [{ type: "text", text: map }],
          display: false,
        },
        { deliverAs: "nextTurn" },
      );
      run.sharedMap = map;
    }
    this.activeRunId = runId;
    try {
      const reply = await runTurn(leader.session, message, (delta) => this.progress.text("leader", delta));
      return reply;
    } finally {
      this.activeRunId = null;
      // Tools may have run inside the turn; persist whatever they wrote.
      await saveGraph(run.paths, run.graph);
    }
  }

  /** The leader session, created if needed, so a CLI can stream a turn itself. */
  async leaderSession(runId: string): Promise<CreatedSession> {
    return this.leader(await this.run(runId));
  }

  dispose(): void {
    for (const run of this.openRuns.values()) {
      run.leader?.session.dispose();
      for (const session of run.studySessions.values()) session.session.dispose();
    }
    this.openRuns.clear();
  }
}

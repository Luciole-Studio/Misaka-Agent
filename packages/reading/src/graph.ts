/** The reading graph on disk: the run's only durable state.
 *
 * A run directory holds the graph, the notes a reader opens by hand, and the pi sessions
 * that produced them. Everything here is written by the engine; Python reads `graph.json`
 * and nothing else. Writes are atomic (temp file + rename) so a killed command leaves the
 * previous graph intact rather than a half-written one.
 */

import { existsSync } from "node:fs";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, join, relative, resolve } from "node:path";

import type {
  ActionResult,
  GenerationEdge,
  ReadingGraph,
  ReadingNode,
  ReadingRun,
  ReadingSource,
  RelationDraft,
  SourceRef,
} from "./types.js";
import { refLabel } from "./sources.js";

export interface RunPaths {
  runDirectory: string;
  graphPath: string;
  graphMarkdownPath: string;
  readingPath: string;
  readerPath: string;
  nodesDirectory: string;
  sessionsDirectory: string;
  sourcesDirectory: string;
}

export function runPaths(runDirectory: string): RunPaths {
  return {
    runDirectory,
    graphPath: join(runDirectory, "graph.json"),
    graphMarkdownPath: join(runDirectory, "graph.md"),
    readingPath: join(runDirectory, "reading.md"),
    readerPath: join(runDirectory, "reader.md"),
    nodesDirectory: join(runDirectory, "nodes"),
    sessionsDirectory: join(runDirectory, "sessions"),
    sourcesDirectory: join(runDirectory, "sources"),
  };
}

function two(value: number): string {
  return value < 10 ? `0${value}` : String(value);
}

/** A readable, collision-free run id. The question gives the slug; the clock orders runs. */
export function newRunId(question: string): string {
  const at = new Date();
  const stamp =
    `${at.getFullYear()}${two(at.getMonth() + 1)}${two(at.getDate())}` +
    `-${two(at.getHours())}${two(at.getMinutes())}${two(at.getSeconds())}`;
  const slug = question
    .replace(/[\\/:*?"<>|\s]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 24)
    .replace(/^-+|-+$/g, "");
  return slug ? `${stamp}-${slug}` : `${stamp}-reading`;
}

export async function ensureRunDirectories(runDirectory: string): Promise<RunPaths> {
  const paths = runPaths(runDirectory);
  for (const directory of [
    paths.nodesDirectory,
    paths.sessionsDirectory,
    paths.sourcesDirectory,
  ]) {
    await mkdir(directory, { recursive: true });
  }
  return paths;
}

/** Write via a sibling temp file and rename, so readers never see a partial file. */
export async function writeAtomic(path: string, content: string): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${process.pid}.tmp`;
  await writeFile(temporary, content, "utf8");
  await rename(temporary, path);
}

export function createGraph(input: {
  runId: string;
  question: string;
  workspace: string;
  rootNode: ReadingNode;
  sources: ReadingSource[];
}): ReadingGraph {
  const at = new Date().toISOString();
  return {
    version: 1,
    runId: input.runId,
    question: input.question,
    rootNodeId: input.rootNode.id,
    currentNodeId: input.rootNode.id,
    workspace: input.workspace,
    createdAt: at,
    updatedAt: at,
    sources: input.sources,
    nodes: [input.rootNode],
    generationEdges: [],
    relations: [],
    sessions: [],
  };
}

export async function loadGraph(paths: RunPaths): Promise<ReadingGraph> {
  const raw = await readFile(paths.graphPath, "utf8");
  const graph = JSON.parse(raw) as ReadingGraph;
  if (graph.version !== 1) {
    throw new Error(`${paths.graphPath} has graph version ${String(graph.version)}; this engine reads version 1.`);
  }
  return graph;
}

export async function loadRun(runDirectory: string): Promise<ReadingRun> {
  const absolute = resolve(runDirectory);
  const paths = runPaths(absolute);
  if (!existsSync(paths.graphPath)) {
    throw new Error(`No reading run at ${absolute}: ${paths.graphPath} does not exist.`);
  }
  const graph = await loadGraph(paths);
  return { runId: graph.runId, runDirectory: absolute, graph };
}

export function nextNodeId(graph: ReadingGraph): string {
  let highest = -1;
  for (const node of graph.nodes) {
    const match = /^N(\d+)$/.exec(node.id);
    if (match) highest = Math.max(highest, Number(match[1]));
  }
  return `N${highest + 1}`;
}

export function findNode(graph: ReadingGraph, nodeId: string): ReadingNode {
  const node = graph.nodes.find((candidate) => candidate.id === nodeId);
  if (!node) {
    const known = graph.nodes.map((candidate) => candidate.id).join(", ") || "none";
    throw new Error(`No node ${nodeId} in run ${graph.runId}; the graph has ${known}.`);
  }
  return node;
}

export function addEdge(
  graph: ReadingGraph,
  from: string | null,
  to: string,
  kind: GenerationEdge["kind"],
  label?: string,
): void {
  graph.generationEdges.push({ from, to, kind, ...(label ? { label } : {}) });
}

/** A short, human-facing line for one graph entry. */
export function nodeTitle(node: ReadingNode): string {
  const lens = node.lens ? `[${node.lens}] ` : "";
  return `${node.id} ${lens}${node.question}`;
}

function edgeLabel(edge: GenerationEdge): string {
  return edge.label ? `${edge.kind}: ${edge.label}` : edge.kind;
}

function escapeLabel(text: string): string {
  return text.replace(/"/g, "'").replace(/\s+/g, " ").slice(0, 80);
}

/** graph.md: the same DAG as graph.json, in Mermaid, with a link for every node. */
export function renderGraphMarkdown(graph: ReadingGraph): string {
  const lines: string[] = [
    `# 阅读图 · ${graph.runId}`,
    "",
    `问题：${graph.question}`,
    "",
    "```mermaid",
    "flowchart TD",
  ];
  for (const node of graph.nodes) {
    const lens = node.lens ? ` · ${node.lens}` : "";
    lines.push(`    ${node.id}["${escapeLabel(`${node.id} ${node.question}${lens}`)}"]`);
  }
  for (const edge of graph.generationEdges) {
    if (!edge.from) continue;
    lines.push(`    ${edge.from} -->|${escapeLabel(edgeLabel(edge))}| ${edge.to}`);
  }
  lines.push("```", "", "## 节点", "");
  for (const node of graph.nodes) {
    const lens = node.lens ? `视角：${node.lens}` : "";
    lines.push(`### [${node.id}] ${node.question}`);
    if (lens) lines.push("", lens);
    lines.push("", node.summary || "（尚未形成理解）", "");
    lines.push(`- 正文：nodes/${node.id}.md`);
    lines.push(`- 动作：${node.actions.map((action) => action.action).join(" → ") || "无"}`);
    if (node.sourceRefs.length) {
      lines.push(`- 材料：${refsLabel(node.sourceRefs)}`);
    }
    if (node.nextQuestions.length) {
      lines.push("- 后续问题：");
      for (const question of node.nextQuestions) lines.push(`  - ${question}`);
    }
    lines.push("");
  }
  if (graph.relations.length) {
    lines.push("## 内容关系", "");
    for (const relation of graph.relations) {
      lines.push(`- ${relation.from} ${relation.kind} ${relation.to}：${relation.explanation}`);
    }
    lines.push("");
  }
  lines.push(
    "> 生成边（continue / fork / compare / reread）表示理解从哪里来；内容关系不决定阅读顺序。",
    "",
  );
  return lines.join("\n");
}

/** Merge refs from the same document into one label.
 *
 * A result usually carries both a bare title (the whole document) and the pages it actually
 * quoted. Printing both makes `材料：` read as if the document were listed twice. */
export function refsLabel(refs: SourceRef[]): string {
  const bySource = new Map<string, SourceRef[]>();
  for (const ref of refs) {
    const list = bySource.get(ref.sourceId) ?? [];
    list.push(ref);
    bySource.set(ref.sourceId, list);
  }
  const labels: string[] = [];
  for (const list of bySource.values()) {
    const anchored = list.filter((ref) => ref.outlineNode || ref.pageStart !== undefined);
    const chosen = anchored.length ? anchored : list.slice(0, 1);
    for (const ref of chosen) {
      const label = refLabel(ref);
      if (!labels.includes(label)) labels.push(label);
    }
  }
  return labels.join("；");
}

/** The leader's working copy of the map: what has been understood and what is still open.
 *
 * Written into the leader's system prompt at session creation and re-sent before each chat
 * turn, because the SDK does not rebuild a system prompt when the graph changes. */
export function renderSharedMap(graph: ReadingGraph): string {
  const current = graph.nodes.find((node) => node.id === graph.currentNodeId);
  const lines: string[] = ["## 阅读地图（由程序写入，不是你的推测）", ""];
  lines.push(`- 当前所在：${current ? `${current.id} ${current.question}` : "（无）"}`);
  lines.push("");
  for (const node of graph.nodes) {
    const parents = graph.generationEdges
      .filter((edge) => edge.to === node.id && edge.from)
      .map((edge) => `${edge.from} ${edge.kind}`);
    const lens = node.lens ? ` · 视角：${node.lens}` : "";
    lines.push(`### ${node.id} ${node.question}${lens}`);
    lines.push("");
    if (parents.length) lines.push(`- 来自：${parents.join("、")}`);
    lines.push(`- 正文：nodes/${node.id}.md`);
    lines.push(node.summary ? `- 摘要：${node.summary}` : "- 摘要：（尚未形成）");
    lines.push("");
  }
  const open = graph.nodes.flatMap((node) =>
    node.nextQuestions.map((question) => `- ${node.id}：${question}`),
  );
  if (open.length) {
    lines.push("## 已经提出、还没有回答", "", ...open, "");
  }
  return lines.join("\n");
}

/** reading.md: what the reader asked, where the reading stands, and where to continue. */
export function renderReadingMap(graph: ReadingGraph): string {
  const current = graph.nodes.find((node) => node.id === graph.currentNodeId);
  const lines: string[] = [
    `# 阅读地图 · ${graph.runId}`,
    "",
    `## 问题`,
    "",
    graph.question,
    "",
    `## 当前所在`,
    "",
    current ? `- ${current.id}：${current.question}` : "- （无）",
    "",
    `## 材料`,
    "",
  ];
  if (!graph.sources.length) lines.push("- （还没有材料）");
  for (const source of graph.sources) {
    const origin = source.origin ? `（来源：${source.origin}）` : "";
    lines.push(`- ${source.sourceId} ${source.title} · ${source.pages} 页 ${origin}`);
  }
  lines.push("", "## 理解地图", "");
  for (const node of graph.nodes) {
    const parents = graph.generationEdges
      .filter((edge) => edge.to === node.id && edge.from)
      .map((edge) => `${edge.from} ${edge.kind}`);
    const origin = parents.length ? ` — 来自 ${parents.join("、")}` : "";
    lines.push(`- ${node.id} ${node.question}${origin}`);
    if (node.summary) lines.push(`  ${node.summary}`);
  }
  const open = graph.nodes.flatMap((node) => node.nextQuestions.map((question) => `${node.id}: ${question}`));
  if (open.length) {
    lines.push("", "## 继续问下去", "");
    for (const question of open) lines.push(`- ${question}`);
  }
  lines.push("", "## 打开", "", "- 图：graph.md", "- 节点正文：nodes/", "");
  return lines.join("\n");
}

export function renderReaderFile(intent: string | undefined): string {
  return [
    "# 读者关切",
    "",
    intent?.trim() || "（尚未记录；用户可直接说明关心的问题、术语与偏好。）",
    "",
  ].join("\n");
}

export async function writeNodeFile(paths: RunPaths, node: ReadingNode): Promise<void> {
  const path = join(paths.runDirectory, node.bodyPath);
  if (!existsSync(path)) await writeAtomic(path, nodeHeader(node));
}

export function nodeHeader(node: ReadingNode): string {
  const lens = node.lens ? `\n- 视角：${node.lens}` : "";
  return [`# ${node.id} · ${node.question}${lens}`, ""].join("\n");
}

export function actionHeading(action: string, contextMode: string, at: string): string {
  return `\n## ${action} · ${contextMode} · ${at}\n\n`;
}

/** Push a section's own headings one level down.
 *
 * The study agent often opens its answer with `# N1 · …`, which would sit at the same level
 * as the node title and make one file look like several documents. Only the body of the
 * section is demoted; fenced code blocks are left alone. */
export function demoteHeadings(body: string): string {
  let fenced = false;
  return body
    .split("\n")
    .map((line) => {
      if (/^\s*(```|~~~)/.test(line)) {
        fenced = !fenced;
        return line;
      }
      if (fenced) return line;
      return /^#{1,5}\s/.test(line) ? `#${line}` : line;
    })
    .join("\n");
}

/** Append one action's understanding to a node body and return the section written. */
export async function appendNodeSection(
  paths: RunPaths,
  node: ReadingNode,
  action: string,
  contextMode: string,
  body: string,
  at: string,
): Promise<string> {
  const path = join(paths.runDirectory, node.bodyPath);
  const section = actionHeading(action, contextMode, at) + demoteHeadings(body.trim()) + "\n";
  const existing = existsSync(path) ? await readFile(path, "utf8") : nodeHeader(node);
  await writeAtomic(path, existing + section);
  return path;
}

/** Fold one action result into a node, keeping the reader-facing summary current. */
export function applyResult(
  node: ReadingNode,
  result: ActionResult,
  action: string,
  contextMode: string,
  at: string,
  sessionId?: string,
): void {
  node.summary = result.summary || node.summary;
  node.actions.push({ action: action as ReadingNode["actions"][number]["action"], contextMode: contextMode as ReadingNode["actions"][number]["contextMode"], at, summary: result.summary });
  if (result.sourceRefs.length) node.sourceRefs = mergeRefs(node.sourceRefs, result.sourceRefs);
  const questions = new Set(node.nextQuestions);
  for (const question of result.nextQuestions) questions.add(question);
  node.nextQuestions = [...questions];
  if (sessionId) node.sessionId = sessionId;
}

export function mergeRefs(existing: SourceRef[], incoming: SourceRef[]): SourceRef[] {
  const seen = new Set(existing.map(refKey));
  const merged = [...existing];
  for (const ref of incoming) {
    const key = refKey(ref);
    if (!seen.has(key)) {
      seen.add(key);
      merged.push(ref);
    }
  }
  return merged;
}

function refKey(ref: SourceRef): string {
  return `${ref.sourceId}|${ref.outlineNode ?? ""}|${ref.pageStart ?? ""}|${ref.pageEnd ?? ""}`;
}

/** Semantic relations from one result, oriented at the node that produced them. */
export function relationsFrom(
  nodeId: string,
  drafts: RelationDraft[],
): ReadingGraph["relations"] {
  return drafts
    .filter((draft) => draft.toNodeId)
    .map((draft) => ({
      from: nodeId,
      to: draft.toNodeId as string,
      kind: draft.kind,
      explanation: draft.explanation,
    }));
}

/** Save the graph and the three files derived from it. */
export async function saveGraph(paths: RunPaths, graph: ReadingGraph): Promise<void> {
  graph.updatedAt = new Date().toISOString();
  await writeAtomic(paths.graphPath, `${JSON.stringify(graph, null, 2)}\n`);
  await writeAtomic(paths.graphMarkdownPath, renderGraphMarkdown(graph));
  await writeAtomic(paths.readingPath, renderReadingMap(graph));
}

export async function saveReaderFile(paths: RunPaths, intent: string | undefined): Promise<void> {
  await writeAtomic(paths.readerPath, renderReaderFile(intent));
}

/** Path stored in the graph, and the absolute path the CLI reports. */
export function bodyPathFor(nodeId: string): string {
  return `nodes/${nodeId}.md`;
}

export function absoluteBodyPath(runDirectory: string, node: ReadingNode): string {
  return join(runDirectory, node.bodyPath);
}

/** A run directory as the host named it, or `reading/<id>` under the workspace. */
export function resolveRunDirectory(workspace: string, runsDirectory: string | undefined, runId: string): string {
  return join(runsDirectory ?? join(workspace, "reading"), runId);
}

export function relativeToRun(runDirectory: string, path: string): string {
  return relative(runDirectory, path).split("\\").join("/");
}

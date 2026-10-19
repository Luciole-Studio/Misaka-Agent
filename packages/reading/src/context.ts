/** What a study agent is handed for one action, under one of four context modes.
 *
 * The reading map is the engine's; it is not poured wholesale into every agent. Each mode
 * selects a different relationship to what has already been read:
 *
 *  - ``continue``    the current line's own understanding and the same material, extended;
 *  - ``independent`` the shared material and the new lens, with no sibling conclusion;
 *  - ``compare``     the full notes of the chosen routes, side by side;
 *  - ``reread``      the old understanding and old material, plus the new understanding
 *                    that is being carried back.
 *
 * Independent really means independent: the caller must give that agent a new session.
 * Omitting a summary from a shared prompt while reusing the session is not independence.
 */

import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { join } from "node:path";

import type { SourceBridge } from "./sources.js";
import { refLabel } from "./sources.js";
import type { RunPaths } from "./graph.js";
import { findNode } from "./graph.js";
import type {
  ActionContext,
  ActionRequest,
  PriorUnderstanding,
  ReadingGraph,
  ReadingSource,
  SourceExcerpt,
  SourceRef,
} from "./types.js";

/** How much of one material window goes into the prompt before the agent asks for more. */
export const EXCERPT_CHARS = 8_000;
/** At most this many materials are pre-read; the rest are reachable through the tools. */
export const EXCERPT_SOURCES = 3;
/** Ordinary continuation uses a working excerpt; comparison and rereading carry full notes. */
const PRIOR_BODY_CHARS = 12_000;

export interface ContextInput {
  request: ActionRequest;
  graph: ReadingGraph;
  paths: RunPaths;
  bridge: SourceBridge;
  sources: ReadingSource[];
  methodInstructions: string;
  readerIntent: string;
}

function cut(text: string, limit: number): { text: string; cut: boolean } {
  const value = text.trim();
  return value.length <= limit ? { text: value, cut: false } : { text: value.slice(0, limit), cut: true };
}

/** Refs this action should start from: the request's own, else the node's, else all sources. */
function startingRefs(request: ActionRequest, graph: ReadingGraph, nodes: string[]): SourceRef[] {
  if (request.sourceRefs.length) return request.sourceRefs;
  const refs: SourceRef[] = [];
  for (const nodeId of nodes) {
    const node = graph.nodes.find((candidate) => candidate.id === nodeId);
    if (node) refs.push(...node.sourceRefs);
  }
  return refs;
}

async function excerptsFor(
  bridge: SourceBridge,
  sources: ReadingSource[],
  refs: SourceRef[],
): Promise<SourceExcerpt[]> {
  const byDoc = new Map<string, ReadingSource>();
  for (const source of sources) byDoc.set(source.docId, source);

  // One window per material, preferring the anchors this action starts from.
  const wanted = new Map<string, SourceRef | undefined>();
  for (const ref of refs) {
    if (!wanted.has(ref.docId)) wanted.set(ref.docId, ref);
  }
  for (const source of sources) {
    if (wanted.size >= EXCERPT_SOURCES) break;
    if (!wanted.has(source.docId)) wanted.set(source.docId, undefined);
  }

  const out: SourceExcerpt[] = [];
  for (const [docId, ref] of [...wanted].slice(0, EXCERPT_SOURCES)) {
    const source = byDoc.get(docId);
    if (!source) continue;
    try {
      const window = await bridge.read({
        docId,
        outlineNode: ref?.outlineNode ?? undefined,
        start: ref?.outlineNode ? undefined : ref?.pageStart ?? 1,
        end: ref?.outlineNode ? undefined : ref?.pageEnd ?? ref?.pageStart ?? 1,
        maxChars: EXCERPT_CHARS,
      });
      out.push({
        sourceId: source.sourceId,
        docId,
        title: source.title,
        label: ref ? refLabel(ref) : `p. ${window.pageStart}`,
        text: window.text,
      });
    } catch (error) {
      // A source the reader registered but whose pages cannot be read is reported in the
      // prompt as such; it must not abort the action that could otherwise use the others.
      out.push({
        sourceId: source.sourceId,
        docId,
        title: source.title,
        label: ref ? refLabel(ref) : "unreadable",
        text: `(this material could not be read: ${error instanceof Error ? error.message : String(error)})`,
      });
    }
  }
  return out;
}

async function priorFor(paths: RunPaths, graph: ReadingGraph, nodeIds: string[], complete: boolean): Promise<PriorUnderstanding[]> {
  const out: PriorUnderstanding[] = [];
  for (const nodeId of nodeIds) {
    const node = graph.nodes.find((candidate) => candidate.id === nodeId);
    if (!node) {
      // Naming a node that is not in the graph should stop the action, not silently pass an
      // empty understanding to the agent.
      findNode(graph, nodeId);
      continue;
    }
    const path = join(paths.runDirectory, node.bodyPath);
    let body = "";
    if (existsSync(path)) {
      try {
        body = await readFile(path, "utf8");
      } catch {
        body = node.summary;
      }
    }
    const text = complete ? (body || node.summary).trim() : cut(body || node.summary, PRIOR_BODY_CHARS).text;
    out.push({
      nodeId: node.id,
      question: node.question,
      ...(node.lens ? { lens: node.lens } : {}),
      summary: node.summary,
      body: text,
    });
  }
  return out;
}

/** Nodes whose understanding this mode carries into the action. */
function nodesForMode(
  request: ActionRequest,
  graph: ReadingGraph,
  sources: ReadingSource[],
): { nodeIds: string[]; refs: SourceRef[] } {
  const current = request.nodeId;
  switch (request.contextMode) {
    case "continue": {
      const node = graph.nodes.find((candidate) => candidate.id === current);
      const line = node
        ? graph.nodes.filter((candidate) => candidate.lineId === node.lineId).slice(0, 4)
        : [];
      const ids = line.length ? line.map((candidate) => candidate.id) : [current];
      return { nodeIds: ids, refs: startingRefs(request, graph, ids) };
    }
    case "independent": {
      // The material and the question, not the conclusions reached by other routes.
      const refs = startingRefs(request, graph, [current]).filter((ref) => ref.docId);
      return { nodeIds: [], refs };
    }
    case "compare": {
      const ids = request.usingNodeIds?.length ? request.usingNodeIds : [current];
      return { nodeIds: ids, refs: startingRefs(request, graph, ids) };
    }
    case "reread": {
      const ids = request.usingNodeIds?.length ? [...request.usingNodeIds, current] : [current];
      return { nodeIds: ids, refs: startingRefs(request, graph, ids) };
    }
    default: {
      const refs = request.sourceRefs.length
        ? request.sourceRefs
        : sources.map((source) => ({
            sourceId: source.sourceId,
            docId: source.docId,
            title: source.title,
            ...(source.origin ? { origin: source.origin } : {}),
          }));
      return { nodeIds: [current], refs };
    }
  }
}

/** Assemble the action package. Performs only reads; the graph is not modified here. */
export async function buildActionContext(input: ContextInput): Promise<ActionContext> {
  const { request, graph, paths, bridge, sources } = input;
  const selection = nodesForMode(request, graph, sources);
  const [sourceExcerpts, priorUnderstandings] = await Promise.all([
    excerptsFor(bridge, sources, selection.refs),
    priorFor(paths, graph, selection.nodeIds, request.contextMode === "compare" || request.contextMode === "reread"),
  ]);
  return {
    question: request.question,
    action: request.action,
    ...(request.lens ? { lens: request.lens } : {}),
    ...(request.focus ? { focus: request.focus } : {}),
    methodInstructions: input.methodInstructions,
    sourceRefs: selection.refs,
    sourceExcerpts,
    priorUnderstandings,
    readerIntent: input.readerIntent,
  };
}

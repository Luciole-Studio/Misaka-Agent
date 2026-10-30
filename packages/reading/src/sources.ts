/** The engine's only door to the corpus.
 *
 * This process never touches a material file itself. Everything -- ingest, outline, page
 * text, literal search -- is asked of the Python host over ``source_call``, which runs the
 * repository's own ``misaka.core.documents.index``. That keeps one extractor, one index,
 * and one set of page markers in the product instead of a second copy in TypeScript.
 */

import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type { Channel } from "./protocol.js";
import type { ReadingSource, SourceRef } from "./types.js";

/** What ``source_add`` returns after the host has ingested one material. */
export interface AddedSource {
  docId: string;
  pages: number;
  title: string;
  origin?: string;
}

export interface OutlineNode {
  nodeId: string;
  title: string;
  pageStart?: number;
  pageEnd?: number;
  depth?: number;
}

export interface Outline {
  mode: string;
  title?: string;
  /** Present only when the format carries an outline (the host degrades honestly otherwise). */
  nodes?: OutlineNode[];
  pages?: number;
  /** The host's own sentence about why there is no tree. */
  note?: string;
}

export interface ReadResult {
  docId: string;
  text: string;
  pageStart: number;
  pageEnd: number;
  /** True when the text stopped at ``maxChars`` and another call with an offset continues it. */
  truncated: boolean;
  nextOffset?: number;
}

export interface FoundPosition {
  doc_id: string;
  page: number;
  printed?: string | number | null;
  s: string;
}

export class SourceBridge {
  constructor(private readonly channel: Channel) {}

  /** Index one material. `runDirectory` is where the host copies a file (or URL) that lives
   *  outside the workspace, so the corpus still ingests from under the workspace. */
  async add(path: string, titleHint?: string, runDirectory?: string): Promise<AddedSource> {
    const raw = (await this.channel.callSource("source_add", {
      path,
      ...(titleHint ? { title: titleHint } : {}),
      ...(runDirectory ? { runDirectory } : {}),
    })) as Partial<AddedSource> | null;
    if (!raw || typeof raw.docId !== "string") {
      throw new Error(`The host returned no document id for ${path}.`);
    }
    return {
      docId: raw.docId,
      pages: Number(raw.pages ?? 0),
      title: raw.title || titleHint || path,
      ...(raw.origin ? { origin: raw.origin } : {}),
    };
  }

  async list(): Promise<ReadingSource[]> {
    const raw = (await this.channel.callSource("source_list", {})) as ReadingSource[] | null;
    return Array.isArray(raw) ? raw : [];
  }

  async outline(docId: string): Promise<Outline> {
    const raw = (await this.channel.callSource("source_outline", { docId })) as Outline | null;
    return raw ?? { mode: "unavailable" };
  }

  async read(params: {
    docId: string;
    outlineNode?: string | null;
    start?: number;
    end?: number;
    offset?: number;
    maxChars?: number;
  }): Promise<ReadResult> {
    const raw = (await this.channel.callSource("source_read", {
      docId: params.docId,
      ...(params.outlineNode ? { outlineNode: params.outlineNode } : {}),
      ...(params.start !== undefined ? { start: params.start } : {}),
      ...(params.end !== undefined ? { end: params.end } : {}),
      ...(params.offset !== undefined ? { offset: params.offset } : {}),
      ...(params.maxChars !== undefined ? { maxChars: params.maxChars } : {}),
    })) as Partial<ReadResult> | null;
    if (!raw || typeof raw.text !== "string") {
      throw new Error(`The host returned no text for ${params.docId}.`);
    }
    return {
      docId: params.docId,
      text: raw.text,
      pageStart: Number(raw.pageStart ?? params.start ?? 1),
      pageEnd: Number(raw.pageEnd ?? params.end ?? params.start ?? 1),
      truncated: Boolean(raw.truncated),
      ...(raw.nextOffset !== undefined ? { nextOffset: Number(raw.nextOffset) } : {}),
    };
  }

  async find(question: string, docId?: string, limit = 10): Promise<FoundPosition[]> {
    const raw = (await this.channel.callSource("source_find", {
      query: question,
      ...(docId ? { docId } : {}),
      limit,
    })) as FoundPosition[] | null;
    return Array.isArray(raw) ? raw : [];
  }
}

/** The page/outline anchor a SourceRef points at, for a human to reopen the material. */
export function refLabel(ref: SourceRef): string {
  const where: string[] = [];
  if (ref.outlineNode) where.push(ref.outlineNode);
  if (ref.pageStart !== undefined) {
    where.push(ref.pageEnd !== undefined && ref.pageEnd !== ref.pageStart
      ? `pp. ${ref.pageStart}-${ref.pageEnd}`
      : `p. ${ref.pageStart}`);
  }
  return where.length ? `${ref.title}, ${where.join(" ")}` : ref.title;
}

/** A compact catalogue for a prompt: one line per source, with the anchors usable in refs. */
export function describeSources(sources: ReadingSource[], outlines: Map<string, Outline>): string {
  if (!sources.length) return "(no material has been ingested for this run)";
  return sources
    .map((source) => {
      const outline = outlines.get(source.docId);
      const structure =
        outline?.nodes?.length
          ? `${outline.mode}, ${outline.nodes.length} sections`
          : outline?.mode ?? "pages only";
      return `- sourceId=${source.sourceId} docId=${source.docId} "${source.title}" (${source.pages} pages; ${structure})`;
    })
    .join("\n");
}

// -- the study agent's own tools ---------------------------------------------

/** Names an agent must list, in order, when the study session is created. */
export const SOURCE_TOOL_NAMES: readonly string[] = ["source_outline", "source_read", "source_find"];

/** Tools through which a study agent reads the material itself.
 *
 * The engine never opens a file: each one becomes a ``source_call`` that the Python host
 * answers with the repository's corpus, so page markers and extraction stay the same as
 * ``misaka doc``. Page errors are returned as the tool's error rather than thrown past the
 * SDK, and the agent decides whether to ask for a different window.
 */
export function createSourceTools(bridge: SourceBridge): ToolDefinition[] {
  const outline = defineTool({
    name: "source_outline",
    label: "查看材料目录",
    description:
      "取一份材料的目录。有结构树时给出章节 nodeId 与页段；没有结构时说明只能按页读。" +
      "泛读整篇或不确定该读哪一段时，先调用它。",
    parameters: Type.Object({
      docId: Type.String({ description: "材料目录里的 docId" }),
    }),
    async execute(_id, params) {
      const result = await bridge.outline(params.docId);
      return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }], details: {} };
    },
  });

  const read = defineTool({
    name: "source_read",
    label: "读取原文",
    description:
      "读取一段原文，返回带 --- pN --- 页标记的文本。可给章节 nodeId，也可直接给页段。" +
      "结果被截断时用 nextOffset 作为 offset 继续。引用原文只用这里返回的文本。",
    parameters: Type.Object({
      docId: Type.String({ description: "材料目录里的 docId" }),
      outlineNode: Type.Optional(Type.String({ description: "章节 nodeId（来自 source_outline）" })),
      start: Type.Optional(Type.Number({ description: "起始页，从 1 开始" })),
      end: Type.Optional(Type.Number({ description: "结束页（含）" })),
      offset: Type.Optional(Type.Number({ description: "续读偏移，来自上一次结果的 nextOffset" })),
      maxChars: Type.Optional(Type.Number({ description: "本次最多返回多少字，默认 12000" })),
    }),
    async execute(_id, params) {
      const result = await bridge.read(params);
      return {
        content: [
          {
            type: "text",
            text:
              result.text +
              (result.truncated && result.nextOffset !== undefined
                ? `\n\n(还有更多；用 offset=${result.nextOffset} 继续)`
                : ""),
          },
        ],
        details: { pageStart: result.pageStart, pageEnd: result.pageEnd },
      };
    },
  });

  const find = defineTool({
    name: "source_find",
    label: "检索原文位置",
    description:
      "按字面短语查它在哪一页出现，返回位置列表。只用于定位，找到后用 source_read 读上下文。",
    parameters: Type.Object({
      query: Type.String({ description: "要查找的字面短语" }),
      docId: Type.Optional(Type.String({ description: "限定在某一份材料内" })),
      limit: Type.Optional(Type.Number({ description: "最多返回多少处，默认 10" })),
    }),
    async execute(_id, params) {
      const found = await bridge.find(
        params.query,
        params.docId,
        params.limit === undefined ? 10 : Math.max(1, Math.trunc(params.limit)),
      );
      return { content: [{ type: "text", text: JSON.stringify(found, null, 2) }], details: {} };
    },
  });

  return [outline, read, find];
}

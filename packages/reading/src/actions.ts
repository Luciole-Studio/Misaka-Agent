/** The eight reading methods, and the tool that collects what a study agent made of them.
 *
 * A method is not a template the engine fills in: it is a short piece of guidance handed to
 * the study agent *for this action only*, ahead of the material. The agent then reads the
 * text through the source tools and submits its own understanding through
 * ``reading_submit``. Nothing here writes prose on the agent's behalf.
 *
 * The method files live at ``packages/reading/methods/<action>/SKILL.md`` next to the
 * compiled ``dist/``, so a checkout reads them straight from disk.
 */

import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type {
  ActionContext,
  ActionId,
  ActionResult,
  ConceptNote,
  PriorUnderstanding,
  ReadingSource,
  RelationDraft,
  SourceRef,
} from "./types.js";

const METHOD_CACHE = new Map<ActionId, string>();

function methodsDirectory(): string {
  // dist/actions.js -> packages/reading/methods
  return join(dirname(fileURLToPath(import.meta.url)), "..", "methods");
}

/** The method's own guidance, read once per process. */
export async function loadMethod(action: ActionId): Promise<string> {
  const cached = METHOD_CACHE.get(action);
  if (cached) return cached;
  const path = join(methodsDirectory(), action, "SKILL.md");
  let text: string;
  try {
    text = await readFile(path, "utf8");
  } catch (error) {
    throw new Error(
      `The method description for "${action}" is missing at ${path}. ` +
        `Build the reading package from a full checkout (npm --prefix packages/reading run build).`,
    );
  }
  METHOD_CACHE.set(action, text);
  return text;
}

// -- the submit tool ----------------------------------------------------------

export interface SubmitSink {
  value: ActionResult | null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => text(item).trim()).filter(Boolean);
}

function notes(value: unknown): ConceptNote[] {
  if (!Array.isArray(value)) return [];
  const out: ConceptNote[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const term = text(record.term).trim();
    if (!term) continue;
    out.push({ term, note: text(record.note).trim() });
  }
  return out;
}

function relations(value: unknown): RelationDraft[] {
  if (!Array.isArray(value)) return [];
  const out: RelationDraft[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const kind = text(record.kind).trim();
    const explanation = text(record.explanation).trim();
    if (!kind || !explanation) continue;
    const toNodeId = text(record.toNodeId).trim();
    const toSummary = text(record.toSummary).trim();
    out.push({
      kind,
      explanation,
      ...(toNodeId ? { toNodeId } : {}),
      ...(toSummary ? { toSummary } : {}),
    });
  }
  return out;
}

/** Reconcile the refs a model named with the run's registered sources.
 *
 * A model knows the `sourceId` and a page or section anchor; the docId and title are ours
 * to fill. A ref naming a source that is not in this run is dropped rather than persisted
 * as a link the reader cannot follow.
 */
function refsFrom(value: unknown, sources: ReadingSource[], fallback: SourceRef[]): SourceRef[] {
  if (!Array.isArray(value) || value.length === 0) return fallback;
  const byId = new Map<string, ReadingSource>();
  for (const source of sources) {
    byId.set(source.sourceId, source);
    byId.set(source.docId, source);
  }
  const out: SourceRef[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const keys = [text(record.sourceId).trim(), text(record.docId).trim()].filter(Boolean);
    const source = keys.map((key) => byId.get(key)).find((candidate) => candidate !== undefined);
    if (source === undefined) continue;
    const outlineNode = text(record.outlineNode).trim();
    const start = Number(record.pageStart);
    const end = Number(record.pageEnd);
    out.push({
      sourceId: source.sourceId,
      docId: source.docId,
      title: source.title,
      ...(source.origin ? { origin: source.origin } : {}),
      ...(Number.isFinite(start) && start > 0 ? { pageStart: Math.trunc(start) } : {}),
      ...(Number.isFinite(end) && end > 0 ? { pageEnd: Math.trunc(end) } : {}),
      ...(outlineNode ? { outlineNode } : {}),
    });
  }
  return out.length ? dedupeRefs(out) : fallback;
}

function refKey(ref: SourceRef): string {
  return `${ref.sourceId}|${ref.outlineNode ?? ""}|${ref.pageStart ?? ""}|${ref.pageEnd ?? ""}`;
}

function dedupeRefs(refs: SourceRef[]): SourceRef[] {
  const seen = new Set<string>();
  return refs.filter((ref) => {
    const key = refKey(ref);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** Turn the tool's raw arguments into an ``ActionResult`` tied to this run's sources. */
export function normalizeSubmit(
  raw: unknown,
  sources: ReadingSource[],
  fallbackRefs: SourceRef[],
): ActionResult {
  const record = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return {
    summary: text(record.summary).trim(),
    understandingMarkdown: text(record.understandingMarkdown).trim(),
    concepts: notes(record.concepts),
    sourceRefs: refsFrom(record.sourceRefs, sources, fallbackRefs),
    relations: relations(record.relations),
    nextQuestions: stringList(record.nextQuestions),
  };
}

/** What to record when the agent answered in prose instead of calling the tool.
 *
 * The text is still the model's own reading of the material, so it is kept rather than
 * thrown away; the summary is its first sentence, and the caller is told on stderr that
 * the structured fields (concepts, relations) are absent for this action.
 */
export function resultFromText(body: string, fallbackRefs: SourceRef[]): ActionResult | null {
  const prose = body.trim();
  if (!prose) return null;
  const firstLine = prose
    .split(/\n+/)
    .map((line) => line.replace(/^#+\s*/, "").trim())
    .find((line) => line.length > 0) ?? "";
  return {
    summary: firstLine.slice(0, 160),
    understandingMarkdown: prose,
    concepts: [],
    sourceRefs: fallbackRefs,
    relations: [],
    nextQuestions: [],
  };
}

/** The in-process tool through which a study agent hands back its understanding. */
export function createSubmitTool(sink: SubmitSink): ToolDefinition {
  return defineTool({
    name: "reading_submit",
    label: "提交本次阅读结果",
    description:
      "提交本次阅读动作的结构化结果。完成对原文的阅读与写作后调用一次；" +
      "正文必须来自你实际读到的材料，并与你的重构、新问题区分清楚。",
    parameters: Type.Object({
      summary: Type.String({ description: "一句话说明这次理解推进到了哪里" }),
      understandingMarkdown: Type.String({
        description: "可独立阅读的 Markdown 正文；引用原文时标明材料与页/节位置",
      }),
      concepts: Type.Optional(
        Type.Array(
          Type.Object({
            term: Type.String({ description: "概念或术语" }),
            note: Type.String({ description: "本次理解中它指什么、起什么作用" }),
          }),
          { description: "这次理解涉及的概念及短解释，不必求全" },
        ),
      ),
      sourceRefs: Type.Optional(
        Type.Array(
          Type.Object({
            sourceId: Type.Optional(Type.String({ description: "材料目录里的 sourceId 或 docId" })),
            docId: Type.Optional(Type.String()),
            outlineNode: Type.Optional(Type.String({ description: "章节 nodeId（如有）" })),
            pageStart: Type.Optional(Type.Number()),
            pageEnd: Type.Optional(Type.Number()),
          }),
          { description: "正文实际引用的材料位置" },
        ),
      ),
      relations: Type.Optional(
        Type.Array(
          Type.Object({
            toNodeId: Type.Optional(Type.String({ description: "相关的既有节点，如 N1" })),
            toSummary: Type.Optional(Type.String({ description: "若没有节点，描述相关的那段理解" })),
            kind: Type.String({
              description: "corresponds | differs | shares-concept | depends-on | resonates | contradicts",
            }),
            explanation: Type.String({ description: "关系具体落在哪里" }),
          }),
          { description: "与其他理解的内容关系；不表示阅读顺序" },
        ),
      ),
      nextQuestions: Type.Optional(
        Type.Array(Type.String(), { description: "这次阅读自然引出的后续问题" }),
      ),
    }),
    async execute(_id, params) {
      sink.value = params as unknown as ActionResult;
      return {
        content: [{ type: "text", text: "已收到这次阅读结果。" }],
        details: {},
      };
    },
  });
}

// -- prompt assembly ----------------------------------------------------------

function priorSection(items: PriorUnderstanding[], heading: string): string {
  if (!items.length) return "";
  const blocks = items.map((item) => {
    const lens = item.lens ? `（视角：${item.lens}）` : "";
    return `### ${item.nodeId}${lens} ${item.question}\n\n${item.body.trim() || item.summary}`;
  });
  return `## ${heading}\n\n${blocks.join("\n\n")}\n\n`;
}

function excerptSection(context: ActionContext): string {
  if (!context.sourceExcerpts.length) return "";
  const blocks = context.sourceExcerpts.map(
    (excerpt) => `### ${excerpt.title} · ${excerpt.label}\n\n${excerpt.text.trim()}`,
  );
  return `## 相关原文（工具读取的开头窗口）\n\n${blocks.join("\n\n")}\n\n`;
}

/** The message a study agent receives for one action. */
export function renderActionPrompt(context: ActionContext, catalogue: string): string {
  const lens = context.lens ? `\n## 视角\n\n${context.lens}\n` : "";
  const focus = context.focus ? `\n## 本次焦点\n\n${context.focus}\n` : "";
  const reader = context.readerIntent.trim()
    ? `\n## 读者关切\n\n${context.readerIntent.trim()}\n`
    : "";
  return [
    `# 本次阅读动作：${context.action}`,
    reader,
    `\n## 本次问题\n\n${context.question}\n`,
    lens,
    focus,
    `\n## 方法说明\n\n${context.methodInstructions.trim()}\n`,
    `\n## 可用材料\n\n${catalogue}\n`,
    `\n${excerptSection(context)}`,
    priorSection(context.priorUnderstandings, "已有理解"),
    `## 做法\n`,
    `- 按方法说明读原文。需要更多内容时调用 source_outline / source_read / source_find；` +
      `引用原文只用这些工具返回的文本，并记下章节或页段。`,
    `- 写你自己的理解：明确区分原文说了什么、你如何重构、你提出的新问题。`,
    `- 完成后调用 reading_submit 提交 summary、understandingMarkdown 等字段，不要只在对话里回答。`,
  ]
    .filter((part) => part !== "")
    .join("\n");
}

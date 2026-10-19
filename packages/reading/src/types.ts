/** Shared content types for the reading action engine.
 *
 * These mirror the contract in docs/plans/reading-agent-20261018/02_IMPLEMENTATION_PLAN.md.
 * Keep the field names stable: the Python host and the CLI both read them from --json
 * output, and graph.json is a persisted format.
 */

export type ActionId =
  | "overview"
  | "reconstruct"
  | "presuppose"
  | "transform"
  | "compare"
  | "translate"
  | "reread"
  | "generate";

export const ACTION_IDS: readonly ActionId[] = [
  "overview",
  "reconstruct",
  "presuppose",
  "transform",
  "compare",
  "translate",
  "reread",
  "generate",
];

export function isActionId(value: string): value is ActionId {
  return (ACTION_IDS as readonly string[]).includes(value);
}

/** How an action receives prior understanding. */
export type ContextMode = "continue" | "independent" | "compare" | "reread";

export function isContextMode(value: string): value is ContextMode {
  return value === "continue" || value === "independent" || value === "compare" || value === "reread";
}

/** A source as the engine knows it: the corpus doc id plus where the excerpt came from. */
export interface SourceRef {
  sourceId: string;
  docId: string;
  title: string;
  origin?: string;
  pageStart?: number;
  pageEnd?: number;
  outlineNode?: string | null;
}

/** One registered source in a run. */
export interface ReadingSource {
  sourceId: string;
  docId: string;
  title: string;
  pages: number;
  origin?: string;
  outlineMode?: string;
  addedAt: string;
}

export interface OpenOptions {
  workspace: string;
  model?: string;
  roleProfile?: string;
  /** Directory that holds runs; Python passes the workspace reading folder. */
  runsDirectory?: string;
}

export interface ActionRequest {
  runId: string;
  nodeId: string;
  action: ActionId;
  question: string;
  lens?: string;
  focus?: string;
  contextMode: ContextMode;
  sourceRefs: SourceRef[];
  usingNodeIds?: string[];
}

export interface SourceExcerpt {
  sourceId: string;
  docId: string;
  title: string;
  label: string;
  text: string;
}

export interface PriorUnderstanding {
  nodeId: string;
  question: string;
  lens?: string;
  summary: string;
  body: string;
}

/** What the study agent is handed for one action. */
export interface ActionContext {
  question: string;
  action: ActionId;
  lens?: string;
  focus?: string;
  methodInstructions: string;
  sourceRefs: SourceRef[];
  sourceExcerpts: SourceExcerpt[];
  priorUnderstandings: PriorUnderstanding[];
  readerIntent: string;
}

export type RelationKind =
  | "corresponds"
  | "differs"
  | "shares-concept"
  | "depends-on"
  | "resonates"
  | "contradicts";

export interface RelationDraft {
  toNodeId?: string;
  toSummary?: string;
  kind: RelationKind | string;
  explanation: string;
}

export interface ConceptNote {
  term: string;
  note: string;
}

/** The structured result a study agent submits through the reading_submit tool. */
export interface ActionResult {
  summary: string;
  understandingMarkdown: string;
  concepts: ConceptNote[];
  sourceRefs: SourceRef[];
  relations: RelationDraft[];
  nextQuestions: string[];
}

export interface BranchSpec {
  label: string;
  question: string;
  lens: string;
  action: ActionId;
  contextMode: ContextMode;
}

export interface ReadingNode {
  id: string;
  lineId: string;
  question: string;
  lens?: string;
  summary: string;
  bodyPath: string;
  sourceRefs: SourceRef[];
  actions: NodeAction[];
  sessionId?: string;
  nextQuestions: string[];
  createdAt: string;
}

export interface NodeAction {
  action: ActionId;
  contextMode: ContextMode;
  at: string;
  summary: string;
}

/** Generation edge: how a node came to exist. */
export interface GenerationEdge {
  from: string | null;
  to: string;
  kind: "continue" | "fork" | "compare" | "reread";
  label?: string;
}

/** Content relation: what one understanding says about another. Never drives execution order. */
export interface SemanticRelation {
  from: string;
  to: string;
  kind: RelationKind | string;
  explanation: string;
}

export interface SessionRecord {
  sessionId: string;
  role: "leader" | "study" | "compare";
  nodeId?: string;
  lineId?: string;
  sessionFile: string;
  model?: string;
  createdAt: string;
}

export interface ReadingGraph {
  version: 1;
  runId: string;
  question: string;
  rootNodeId: string;
  currentNodeId: string;
  workspace: string;
  createdAt: string;
  updatedAt: string;
  sources: ReadingSource[];
  nodes: ReadingNode[];
  generationEdges: GenerationEdge[];
  relations: SemanticRelation[];
  sessions: SessionRecord[];
}

export interface ReadingRun {
  runId: string;
  runDirectory: string;
  graph: ReadingGraph;
}

/** Model handover from the Python host: complete request parameters, key in memory only. */
export interface ModelHandover {
  provider: string;
  id: string;
  api: string;
  baseUrl?: string;
  headers?: Record<string, string>;
  apiKey?: string;
  contextWindow?: number;
  maxTokens?: number;
  reasoning?: boolean;
}

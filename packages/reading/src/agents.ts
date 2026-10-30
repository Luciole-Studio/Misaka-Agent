/** Session construction: the leader, and the Sisters it calls in for a single action.
 *
 * One pi session per reading *line*, not per action. The leader keeps the whole run's
 * conversation; a study Sister keeps the line it was first created for; a compare Sister
 * is created for one comparison and owns both originals in its own prompt instead.
 *
 * Nothing here reaches the network by itself. Extensions, skills, prompt templates and
 * context files are all switched off: this process was not started by `pi`, it was started
 * by MISAKA, and the only system prompt it should receive is the one we write.
 */

import { DefaultResourceLoader, SessionManager, createAgentSession, type AgentSession, type ToolDefinition } from "@earendil-works/pi-coding-agent";

import type { ChatModel } from "./runtime.js";
import type { ModelRuntime } from "@earendil-works/pi-coding-agent";

export type AgentRole = "leader" | "study" | "compare";

/** Used when the Python host could not assemble the profile's own persona text.
 *
 * The host normally hands over the Last Order / Sister charter read from the profile, so
 * the TUI and the CLI read through the same character. This is the plain fallback. */
export const DEFAULT_PERSONA = {
  leader: [
    "你是这次共读的领读。",
    "你手里有读者的问题、材料目录和一张正在生长的阅读地图。",
    "你的工作不是替读者写完文章，而是决定此刻该读哪一段、用哪个动作、带着什么焦点，",
    "然后把各条路线交回来的理解串起来；需要深入某一处时，调用研读。",
  ].join("\n"),
  study: [
    "你是一位研读者，这次只负责一个阅读动作。",
    "你手上有原文工具（source_outline / source_read / source_find），可以按需展开材料；",
    "先把原话读准，再写你自己的重构、追问与生成。原文、你的解释、你提出的新问题要区分清楚。",
  ].join("\n"),
};

export interface SessionSpec {
  cwd: string;
  agentDir: string;
  sessionDirectory: string;
  modelRuntime: ModelRuntime;
  model: ChatModel;
  systemPrompt: string;
  tools: string[];
  /** In-process tools defined by this package, e.g. `reading_submit`.
   *
   * They are not registered by any extension, so the SDK filters them out unless both this
   * array and `tools` name them: `noTools: "all"` plus an allow-list is what activates them.
   * Leaving the list empty makes the model answer in prose instead of calling the tool. */
  customTools?: ToolDefinition[];
  /** Existing session file to reopen (real resume) instead of starting a new one. */
  sessionFile?: string;
}

async function openOrCreate(spec: SessionSpec): Promise<{ session: AgentSession; resumed: boolean }> {
  const loader = new DefaultResourceLoader({
    cwd: spec.cwd,
    agentDir: spec.agentDir,
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    systemPrompt: spec.systemPrompt,
  });
  await loader.reload();

  let manager: SessionManager;
  let resumed = false;
  if (spec.sessionFile) {
    try {
      manager = SessionManager.open(spec.sessionFile, spec.sessionDirectory, spec.cwd);
      resumed = true;
    } catch (error) {
      // A corrupt or missing session file must not take the reading graph down with it;
      // say what happened and continue with a fresh session in the same directory.
      process.stderr.write(
        `reading: could not reopen ${spec.sessionFile} (${error instanceof Error ? error.message : String(error)}); starting a new session\n`,
      );
      manager = SessionManager.create(spec.cwd, spec.sessionDirectory);
    }
  } else {
    manager = SessionManager.create(spec.cwd, spec.sessionDirectory);
  }

  const { session } = await createAgentSession({
    cwd: spec.cwd,
    agentDir: spec.agentDir,
    modelRuntime: spec.modelRuntime,
    model: spec.model as never,
    sessionManager: manager,
    resourceLoader: loader,
    noTools: "all",
    tools: spec.tools,
    ...(spec.customTools && spec.customTools.length ? { customTools: spec.customTools } : {}),
  });
  return { session, resumed };
}

export interface CreatedSession {
  session: AgentSession;
  sessionFile: string;
  sessionId: string;
  resumed: boolean;
}

export async function createLeaderSession(spec: SessionSpec): Promise<CreatedSession> {
  const { session, resumed } = await openOrCreate({ ...spec, tools: spec.tools });
  return {
    session,
    sessionFile: session.sessionFile ?? "",
    sessionId: session.sessionId,
    resumed,
  };
}

export async function createStudySession(spec: SessionSpec): Promise<CreatedSession> {
  const { session, resumed } = await openOrCreate(spec);
  return {
    session,
    sessionFile: session.sessionFile ?? "",
    sessionId: session.sessionId,
    resumed,
  };
}

export async function createCompareSession(spec: SessionSpec): Promise<CreatedSession> {
  const { session, resumed } = await openOrCreate(spec);
  return {
    session,
    sessionFile: session.sessionFile ?? "",
    sessionId: session.sessionId,
    resumed,
  };
}

/** Stream a turn's assistant text to `onDelta`, dropping thinking blocks entirely.
 *
 * The site models used here emit their reasoning as `thinking_delta`; only `text_delta`
 * belongs in a reading. Subscribing for the length of one turn and unsubscribing after
 * keeps the leader's later turns from double-reporting.
 */
export async function runTurn(
  session: AgentSession,
  prompt: string,
  onDelta: (delta: string) => void,
  signal?: AbortSignal,
): Promise<string> {
  let collected = "";
  const unsubscribe = session.subscribe((event) => {
    if (event.type !== "message_update") return;
    const inner = (event as { assistantMessageEvent?: { type?: string; delta?: string } }).assistantMessageEvent;
    if (inner?.type === "text_delta" && typeof inner.delta === "string") {
      collected += inner.delta;
      onDelta(inner.delta);
    }
  });
  const abortHandler = () => {
    void session.abort();
  };
  signal?.addEventListener("abort", abortHandler, { once: true });
  try {
    await session.prompt(prompt, { expandPromptTemplates: false });
    await session.waitForIdle();
    const lastAssistant = (session.messages as Array<{ role?: string; stopReason?: string }>)
      .findLast((message) => message.role === "assistant");
    if (lastAssistant?.stopReason === "error") {
      throw new Error(lastStopReason(session));
    }
  } finally {
    signal?.removeEventListener("abort", abortHandler);
    unsubscribe();
  }
  return collected;
}

/** Why the last assistant message stopped, for an error that says more than "no text".
 *
 * A reasoning model that spends its whole output budget thinking stops with `length` and
 * has no text at all; reporting that as the model returning nothing hides the real cause.
 */
export function lastStopReason(session: AgentSession): string {
  const messages = session.messages as Array<{ role?: string; stopReason?: string; errorMessage?: string }>;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role !== "assistant") continue;
    if (message.stopReason === "length") return "the response hit the output token limit (stopReason=length)";
    if (message.errorMessage) return `the provider reported: ${message.errorMessage}`;
    return message.stopReason ? `stopReason=${message.stopReason}` : "";
  }
  return "";
}

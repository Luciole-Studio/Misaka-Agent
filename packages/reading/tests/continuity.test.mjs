import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai/providers/faux";
import { buildActionContext } from "../dist/context.js";
import { runPaths } from "../dist/graph.js";
import { ReadingEngine } from "../dist/orchestrator.js";

// Only model transport is scripted. Actions, SDK tool calls, sessions, context and graph
// files use the actual implementation. No remote model, credentials or extra dependency.
const bridge = {
  async add() { return { docId: "article", pages: 1, title: "Article" }; },
  async outline() { return { mode: "pages", title: "Article", pages: 1 }; },
  async read() { return { docId: "article", text: "An argument and its conditions.", pageStart: 1, pageEnd: 1, truncated: false }; },
};

async function fixture(t, readerIntent) {
  const directory = await mkdtemp(join(tmpdir(), "misaka-reading-continuity-"));
  const faux = fauxProvider({ tokensPerSecond: 1_000_000_000 });
  const runtime = await ModelRuntime.create({
    authPath: join(directory, "auth.json"), modelsPath: null,
    refreshOnCreate: false, allowModelNetwork: false,
  });
  runtime.registerNativeProvider(faux.provider);
  const engines = [];
  const create = (intent) => {
    const engine = new ReadingEngine({ bridge, workspace: directory,
      agentDir: directory, ...(intent ? { readerIntent: intent } : {}) });
    engine.model = async () => ({ runtime, model: faux.getModel(), label: "faux/faux-1" });
    engines.push(engine);
    return engine;
  };
  const engine = create(readerIntent);
  const run = await engine.openReading(["article.txt"], "Read this argument", { workspace: directory });
  t.after(() => engines.forEach((item) => item.dispose()));
  return { directory, engine, run, faux, create };
}

function submit(faux, summary, observe) {
  const response = fauxAssistantMessage(fauxToolCall("reading_submit", {
    summary, understandingMarkdown: `## ${summary}\n\n${summary} body.`,
    concepts: [], sourceRefs: [], relations: [], nextQuestions: [`After ${summary}?`],
  }), { stopReason: "toolUse" });
  faux.setResponses([
    observe ? (context) => { observe(context); return response; } : response,
    fauxAssistantMessage(""),
  ]);
}

function request(runId, overrides = {}) {
  return { runId, nodeId: "N0", action: "reconstruct", question: "Read this argument",
    contextMode: "continue", sourceRefs: [], ...overrides };
}

test("a provider error is reported instead of a successful empty leader reply", async (t) => {
  const { engine, run, faux } = await fixture(t);
  faux.setResponses([fauxAssistantMessage([], { stopReason: "error", errorMessage: "401 Unauthorized" })]);
  await assert.rejects(engine.chat(run.runId, "Read this passage"), /401 Unauthorized/);
});

test("a continued reading records the second structured submission", async (t) => {
  const { engine, run, faux } = await fixture(t);
  submit(faux, "First reading");
  await engine.act(request(run.runId));
  submit(faux, "Second reading");
  const outcome = await engine.act(request(run.runId));
  assert.equal(outcome.result.summary, "Second reading");
  const body = await readFile(join(run.runDirectory, outcome.bodyPath), "utf8");
  assert.match(body, /First reading body/);
  assert.match(body, /Second reading body/);
  assert.deepEqual(engine.graphOf(run.runId).nodes[0].nextQuestions, ["After First reading?", "After Second reading?"]);
});

test("reopening a run continues the saved study session", async (t) => {
  const { engine, run, faux, create } = await fixture(t);
  submit(faux, "Before reopening");
  await engine.act(request(run.runId));
  const original = engine.graphOf(run.runId).nodes[0].sessionId;
  engine.dispose();
  const resumed = create();
  await resumed.resumeReading(run.runDirectory);
  submit(faux, "After reopening");
  await resumed.act(request(run.runId));
  const graph = resumed.graphOf(run.runId);
  assert.equal(graph.nodes[0].sessionId, original);
  assert.equal(graph.sessions.length, 1);
  const transcript = await readFile(graph.sessions[0].sessionFile, "utf8");
  assert.match(transcript, /Before reopening/);
  assert.match(transcript, /After reopening/);
});

test("an independent lens leaves the original node and reading line intact", async (t) => {
  const { engine, run, faux } = await fixture(t);
  submit(faux, "Original view");
  await engine.act(request(run.runId));
  const original = structuredClone(engine.graphOf(run.runId).nodes[0]);
  submit(faux, "Other view");
  const outcome = await engine.act(request(run.runId, {
    contextMode: "independent", lens: "Economic conditions", question: "Which conditions matter?",
  }));
  const graph = engine.graphOf(run.runId);
  assert.notEqual(outcome.nodeId, "N0");
  assert.deepEqual(graph.nodes[0], original);
  const other = graph.nodes.find((node) => node.id === outcome.nodeId);
  assert.equal(other.lens, "Economic conditions");
  assert.equal(other.question, "Which conditions matter?");
  assert.notEqual(other.lineId, original.lineId);
  assert.notEqual(other.sessionId, original.sessionId);
  assert.ok(graph.generationEdges.some((edge) => edge.from === "N0" && edge.to === other.id && edge.kind === "fork"));
});

test("comparison and rereading carry the full selected node notes", async (t) => {
  const { engine, run } = await fixture(t);
  const graph = engine.graphOf(run.runId);
  const complete = "Earlier explanation.\n".repeat(900) + "FINAL_PASSAGE_TO_COMPARE";
  await writeFile(join(run.runDirectory, "nodes/N0.md"), complete);
  for (const contextMode of ["compare", "reread"]) {
    const context = await buildActionContext({
      request: request(run.runId, { action: contextMode, contextMode, usingNodeIds: ["N0"] }),
      graph, paths: runPaths(run.runDirectory), bridge, sources: graph.sources,
      methodInstructions: "Read together", readerIntent: "",
    });
    assert.ok(context.priorUnderstandings[0].body.endsWith("FINAL_PASSAGE_TO_COMPARE"), contextMode);
  }
});

test("the reader intent saved by open is loaded in a fresh engine", async (t) => {
  const { engine, run, faux, create } = await fixture(t, "READER_WANTS_CONCEPT_HISTORY");
  engine.dispose();
  const resumed = create();
  await resumed.resumeReading(run.runDirectory);
  let handedOver = false;
  submit(faux, "Intent-aware reading", (context) => {
    handedOver = JSON.stringify(context).includes("READER_WANTS_CONCEPT_HISTORY");
  });
  await resumed.act(request(run.runId));
  assert.ok(handedOver, "the original reader intent must enter the action context");
});

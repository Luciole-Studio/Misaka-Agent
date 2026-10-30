/** The model runtime for one reading run.
 *
 * Two ways in, in this order:
 *
 * 1. The Python host hands over the complete request parameters it resolved from MISAKA's
 *    own model registry (provider, id, api, baseUrl, headers, key). We register a provider
 *    with the SDK and hand it the key *in memory* -- `registerProvider` alone does not
 *    satisfy the SDK's auth gate, `setRuntimeApiKey` does.
 * 2. Nothing handed over: fall back to whatever the SDK itself finds configured (auth.json,
 *    environment). If that is empty too, say so instead of producing an empty reading.
 *
 * The key never leaves this module: it is not returned, not logged, not persisted, and the
 * runtime is created with a read-only credential source so a stale auth.json cannot win.
 */

import { homedir } from "node:os";
import { join } from "node:path";

import { ModelRuntime } from "@earendil-works/pi-coding-agent";

import type { ModelHandover } from "./types.js";

/** The SDK's own model type. Taken from the runtime rather than deep-importing pi-ai. */
export type ChatModel = NonNullable<ReturnType<ModelRuntime["getModel"]>>;

export interface RuntimeOptions {
  handover?: ModelHandover;
  /** MISAKA's agent dir, so a site-level auth/models.json is honoured in the fallback path. */
  agentDir: string;
  /** Where project-local settings/extensions would be discovered. The run directory. */
  cwd: string;
  /** Optional `provider/id` preference for the fallback path. */
  modelHint?: string;
}

export interface ReadingRuntime {
  runtime: ModelRuntime;
  model: ChatModel;
  /** Human-readable `provider/id`, for graph.json and progress lines. Never contains the key. */
  label: string;
}

function zeroCost(): { input: number; output: number; cacheRead: number; cacheWrite: number } {
  return { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
}

/** Providers MISAKA's handover names in its own vocabulary; the SDK wants an Api id. */
function normaliseApi(api: string | undefined): string {
  const value = (api ?? "").trim();
  if (!value) return "anthropic-messages";
  // MISAKA's own model table calls the OpenAI chat API "openai"; the SDK's id is longer.
  if (value === "openai") return "openai-completions";
  if (value === "anthropic") return "anthropic-messages";
  return value;
}

/** The SDK's openai-completions client appends `/chat/completions` to the base URL it is
 *  given; it does *not* add `/v1`. MISAKA stores roots without the version segment, so add
 *  it here as a backstop and leave an already-versioned base URL alone. */
function normaliseBaseUrl(baseUrl: string | undefined, api: string): string | undefined {
  if (!baseUrl) return baseUrl;
  const trimmed = baseUrl.replace(/\/+$/, "");
  if (api !== "openai-completions") return trimmed;
  return trimmed.endsWith("/v1") ? trimmed : `${trimmed}/v1`;
}

async function fromHandover(options: RuntimeOptions, handover: ModelHandover): Promise<ReadingRuntime> {
  const providerId = handover.provider;
  const runtime = await ModelRuntime.create({
    modelsPath: null,
    authPath: join(options.agentDir, "auth.json"),
    refreshOnCreate: false,
    allowModelNetwork: false,
    modelsStore: undefined,
  });

  const api = normaliseApi(handover.api);
  const baseUrl = normaliseBaseUrl(handover.baseUrl, api);
  runtime.registerProvider(providerId, {
    name: providerId,
    ...(baseUrl ? { baseUrl } : {}),
    api: api as never,
    ...(handover.headers && Object.keys(handover.headers).length ? { headers: { ...handover.headers } } : {}),
    models: [
      {
        id: handover.id,
        name: handover.id,
        api: api as never,
        input: ["text"],
        reasoning: Boolean(handover.reasoning),
        contextWindow: handover.contextWindow ?? 131_072,
        maxTokens: handover.maxTokens ?? 8_000,
        cost: zeroCost(),
      },
    ],
  });

  // Required: without a resolved key the SDK refuses to send a request even when the
  // handover's headers already carry an Authorization line.
  if (handover.apiKey) {
    await runtime.setRuntimeApiKey(providerId, handover.apiKey);
  }

  const model = runtime.getModel(providerId, handover.id);
  if (!model) {
    throw new Error(
      `The host handed over ${providerId}/${handover.id}, but the runtime did not register that model.`,
    );
  }
  return { runtime, model, label: `${providerId}/${handover.id}` };
}

async function fromSdk(options: RuntimeOptions): Promise<ReadingRuntime> {
  const runtime = await ModelRuntime.create({
    authPath: join(options.agentDir, "auth.json"),
    refreshOnCreate: false,
    allowModelNetwork: false,
  });

  const hint = options.modelHint;
  if (hint && hint.includes("/")) {
    const [providerId, modelId] = hint.split("/", 2) as [string, string];
    const model = runtime.getModel(providerId, modelId);
    if (model) {
      const auth = await runtime.checkAuth(providerId);
      // `AuthCheck` is present only when the provider actually resolved credentials.
      if (auth) return { runtime, model, label: hint };
    }
  }

  const available = runtime.getAvailableSnapshot();
  const fallback = available.find((model) => model.input.includes("text"));
  if (fallback) {
    return {
      runtime,
      model: fallback,
      label: `${fallback.provider}/${fallback.id}`,
    };
  }

  const error = runtime.getError();
  throw new Error(
    [
      "No text model is available for the reading engine.",
      error ? `The SDK reported: ${error}` : "",
      `Looked in ${join(options.agentDir, "auth.json")} and the environment.`,
      "Pass --model <provider>/<model> so the host can hand over its request parameters,",
      "or authenticate a model with MISAKA first.",
    ]
      .filter(Boolean)
      .join("\n"),
  );
}

/** Pick the model runtime for this run. */
export async function buildRuntime(options: RuntimeOptions): Promise<ReadingRuntime> {
  if (options.handover) {
    try {
      return await fromHandover(options, options.handover);
    } catch (error) {
      // A handover that names a model we cannot register is a configuration error, not a
      // reason to silently speak through some other model. Report both facts.
      const detail = error instanceof Error ? error.message : String(error);
      throw new Error(`The model handed over by the host could not be used: ${detail}`);
    }
  }
  return fromSdk(options);
}

/** Only used by the CLI probe command; keeps the default agent dir in one place. */
export function defaultAgentDir(): string {
  return join(homedir(), ".pi", "agent");
}

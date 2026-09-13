import type { LanguageModel } from "ai"
import type { Judge, JudgeConfig, JudgeInput, JudgeResult } from "../types/judge"
import type { ProviderPrompts } from "../types/prompts"
import { buildJudgePrompt, parseJudgeResponse, getJudgePrompt } from "./base"
import { logger } from "../utils/logger"
import { cliComplete, cliLlmModelId } from "../utils/cli-llm"

/**
 * Judge backed by a subscription-authenticated CLI (Codex/Claude) instead of a
 * metered API. Same prompts as the SDK judges (LongMemEval per-type prompts via
 * `buildJudgePrompt`), verdict parsed by the shared `parseJudgeResponse`.
 */
export class CliJudge implements Judge {
  name = "cli"

  async initialize(_config: JudgeConfig): Promise<void> {
    logger.info(`Initialized CLI judge (${cliLlmModelId()})`)
  }

  async evaluate(input: JudgeInput): Promise<JudgeResult> {
    const prompt = buildJudgePrompt(input)
    const text = await cliComplete(prompt)
    return parseJudgeResponse(text)
  }

  getPromptForQuestionType(questionType: string, providerPrompts?: ProviderPrompts): string {
    return getJudgePrompt(questionType, providerPrompts)
  }

  getModel(): LanguageModel {
    // The CLI backend has no ai-sdk LanguageModel. The evaluate phase skips the
    // LLM-graded retrieval-metrics pass when the CLI backend is active, so this
    // is never called; throw loudly if that assumption is ever violated.
    throw new Error("CliJudge.getModel() is not available under the CLI LLM backend")
  }
}

export default CliJudge

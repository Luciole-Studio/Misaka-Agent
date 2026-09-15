import type { ProviderPrompts } from "../../types/prompts"

/**
 * hermes-lcm deliberately ships NO custom answer or judge prompt.
 *
 * For leaderboard-comparable LongMemEval_S QA accuracy the harness defaults are
 * the neutral baseline: the default answer prompt reasons over the raw JSON
 * search results (which already carry `content` + `metadata.date`), and the
 * judge falls through to LongMemEval's standard per-question-type prompts
 * (`getJudgePromptForType`). Handing hermes-lcm a bespoke tuned prompt would
 * inflate its numbers relative to providers scored under those defaults, so we
 * intentionally leave both undefined.
 */
export const HERMES_LCM_PROMPTS: ProviderPrompts = {}

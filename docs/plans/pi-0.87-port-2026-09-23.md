# pi 0.86.0–0.87.1 内核移植（2026-09-23）

规则（用户当日定）：**不和 misaka 的特性/模块冲突，就逐行一致地移植**；冲突处以 misaka 为准并留 `# MISAKA fork:`。

对照件：`@earendil-works/pi-ai`、`pi-agent-core`、`pi-coding-agent` 的 0.85.1 与 0.87.1 发布包
（`npm pack`，dist/ 逐文件 diff）。misaka 的 ai/ 在 0.85.1 之后已零散对齐过 pi main（`deferred_tools`、
`allowedFallbackModels`、`x-session-id`），所以每一块都按 misaka 现状核对，不按 0.85.1 全量搬。

本机 pi oracle：`@earendil-works/pi-coding-agent@0.87.1`（`/opt/homebrew/lib/node_modules`）。

## 变更清单与去向

状态：☐ 待做 ☑ 已做 ✗ 不移植（附理由） ◐ 部分

### A. pi-ai → `misaka/ai/`

| # | 上游 | 内容 | 状态 |
|---|---|---|---|
| A1 | `types.ts` | `SystemMessage`（content/sections/toolsAdded/toolsRemoved）、`ToolReference`、`Message` 并入 system、`TranscriptContext`、删 `ToolResultMessage.addedToolNames`、删 `deferredToolsMode`/`supportsToolReferences`、`MistralConversationsCompat`、`ModelPromptCache` | ☑ |
| A2 | `utils/transcript.ts`（新） | `createInitialSystemMessage`/`normalizeContext`/`getCurrentTools`/`getCurrentSystemMessage`/`getCurrentSystemPrompt`/`collapseSystemMessages`/`resolveTranscript`/`toToolDeclaration`/`declarationsEqual`/`getToolStateChanges`/`getDeclaredTools`/`hasToolRedefinitions`/`hasNonAdditiveToolChanges`/`resolveTranscriptTools` | ☑ |
| A3 | `utils/text.ts` | `getSystemMessageText`、`renderSystemMessageUpdate` | ☑ |
| A4 | `compat.ts` / `models.ts` | `stream`/`streamSimple` 入口先 `normalizeContext` | ☑ |
| A5 | `api/transform-messages.ts` | system 消息透传；夹在 tool call 与结果之间的 system 消息后置 | ☑ |
| A6 | `api/anthropic-messages.ts` | 从 transcript 取 prompt/tools；原生 `tool_addition`/`tool_removal`（beta）+ 占位 deferred 工具；后续 system 消息挂到下一条 assistant 前；`responseModel`；OpenRouter 会话亲和默认；Claude Code 版本 2.1.280；删 tool_reference 路径 | ☑ |
| A7 | `api/openai-responses-shared.ts` + `openai-responses.ts` + `azure` + `openai-codex-responses.ts` | 同上（Responses 系）；Codex 的 Off effort 发送；错误标 provider 名 | ☑ |
| A8 | `api/openai-completions.ts` | 同上；未知端点不发 strict；Mistral `reasoning_effort`；Baseten/OpenRouter 亲和头；image-only 消息不带空 text | ☑ |
| A9 | `api/google-shared.ts` + `google-generative-ai.ts` + `google-vertex.ts` | 同上；thinking level 按模型能力 | ☑ |
| A10 | `api/mistral-conversations.ts`、`bedrock-converse-stream.ts`、`pi-messages.ts` | 同上；Bedrock 1h 缓存计价 | ☑ |
| A11 | `utils/estimate.ts`、`event-stream.ts`、`overflow.ts`、`retry.ts` | 估算含 system 消息；drain 二次方修复；z.ai overflow；Cloudflare 520 / Azure 峰值重试 | ◐ event-stream 不移植：misaka 的 `EventStream` 本就是 `asyncio.Queue`（deque），没有 JS 数组 `shift` 的二次方问题；其余已做 |
| A12 | `providers/radius.ts` + `radius.models.ts`、`opencode-headers.ts`、`opencode*.ts` | 离线 Radius 目录；`x-opencode-session` | ☑ |
| A13 | `providers/meta.ts` + `meta.models.ts` + `auth/oauth/meta.ts` | Meta Muse provider + 登录 | ☑ |
| A14 | `utils/deferred-tools.ts` | 上游 0.87 已删（被 `transcript.ts` 取代），misaka 的 `deferred_tools.py` 同步删除 | ☑ |
| A15 | `providers/faux.ts` | ✗ 早前决定不移植（测试工具） | ✗ |

### B. pi-agent-core → `misaka/agent/`

| # | 上游 | 内容 | 状态 |
|---|---|---|---|
| B1 | `agent-loop.ts` | `declareToolChanges`、`prepareRequest`、`finishTurn` 取代 `shouldStopAfterTurn`、`explicitContinuation`、`normalizeContext`、删 `addedToolNames` | ☑ |
| B2 | `agent.ts` | `finishTurn`/`prepareRequest` 接线、`turn_end` 边界 | ☑ |
| B3 | `harness/messages.ts`、`harness/session/*`、`harness/tools/image.ts`、`harness/runtime/drive/*` | `messages.ts`（convertToLlm 放行 system）已做；misaka 没有 `harness/session/jsonl`、`harness/runtime/drive`、`harness/tools/image` 的对应件（misaka 的会话在 core/session_manager，图片在 core/tools），按 C 层处理 | ◐ |

### C. pi-coding-agent core → `misaka/core/`

| # | 上游 | 内容 | 状态 |
|---|---|---|---|
| C1 | `session-manager.ts` | `context_edit` 条目、system prompt / tool 变更条目、`appendCompaction(summary, null, …)`、canonical context | ☑ |
| C2 | `agent-session.ts` | SessionManager 为 context 之本、`refreshContext`、`emitBoundary`、cache warming 接入、图片限制、重试后遗弃尝试的 context_edit 省略 | ☑（cache warmer 已接入；`summarizeForBugReport` 随 C11 不移植；图片按模型 `inputLimits.images.resize` 已做） |
| C3 | `extensions/runner.ts` + `types` + `wrapper` + `loader` | `turn_end`/`agent_before_settle` 边界、`context_with_system`、`pi.on()` 退订、`agent_settled` 延迟、无 schema 工具拒注册 | ☑（runner/loader/moments/types 全部） |
| C4 | `compaction/compaction.ts` + `branch-summarization.ts` | 按模型预算、拆轮摘要（#9908）、超大尾部工具结果（#9740）、取消竞争（#9340/#9777） | ☑ |
| C5 | `system-prompt.ts` | 213 行差异 | ☑ |
| C6 | `cache-warmer.ts`（新）+ `cache-stats.ts` + `usage-totals.ts` | 提示缓存保温 | ☑ 新 `core/cache_warmer.py`（逐函数对照）；`cache-stats.ts` 的 usage 钩子 ✗——misaka 本就没有 cache-stats（cache-miss 提示此前未移植） |
| C7 | `settings-manager.ts` + `model-config.ts` + `model-registry.ts` + `model-runtime.ts` + `model-resolver.ts` + `provider-composer.ts` | `cacheWarming`、`compaction.modelOverrides`、`inputLimits`、`retry.maxAgentDelayMs`、`ctx.modelRegistry.stream()` | ☑（settings-manager、model_registry 的 inputLimits/promptCache schema 与 override 合并、resolver 默认 radius=balanced / xai=grok-4.7 / meta；`ctx.modelRegistry.stream()` 已加） |
| C8 | `sdk.ts` | 105 行差异 | ☑（`existing_session.messages` 走 initialState、cache warmer、请求选项） |
| C9 | `prompt-templates.ts`、`resource-loader.ts` | frontmatter 报警 | ☑（`LoadedPromptTemplates` 带 diagnostics，resource_loader 合并） |
| C10 | `tools/bash.ts`、`read.ts`、`edit.ts`、`write.ts`、`renderers/bash.ts` | 信号终止报错、时长格式、GIF 误判、strict-prefer 默认 | ☑（bash 信号退出码 128+n / "terminated without an exit code"、时长 m/s/h、四个内置工具 + office 默认 strict-prefer、read/tool-result/prompt 图片按模型 resize；`experimental.get_experimental_tool_sampling` 删） |
| C11 | `crash-log.ts`、`bug-report*.ts`、`radius.ts` | ✗ `/bug` 上传给 pi 开发者与 misaka 产品身份冲突；crash-log 视情况 | ✗ `/bug` 与 crash-log 不移植：把报告上传给 pi 的开发者与 misaka 的产品身份冲突；本地 crashes.json 的价值随之消失 |
| C12 | `session-export.ts`、`export-html/template.ts`、`keybindings.ts`、`slash-commands.ts`、`messages.ts`、`experimental.ts` | 小差异 | ☑（`serialize_session_branch`、usage 条目计入 `getUsageCostBreakdown`、keybinding 文案、runtime `refreshContext`、branch-summary/compaction 的 `normalize_context`；export-html template 未动：misaka 的模板与 pi 已分叉） |

### D. modes → `misaka/ui/tui/`、`cli/`

| # | 上游 | 内容 | 状态 |
|---|---|---|---|
| D1 | `modes/rpc/rpc-mode.ts` | steer/follow_up 过 `input` 处理器 | ✗ misaka 没有 rpc 模式；但 `steer`/`followUp` 已按上游改成先过 `input` 处理器（#8718 的实质） |
| D2 | `modes/interactive/*` | 编辑框边框里的 spinner、点击折叠、footer、`/session` 缓存诊断、`--mode` 校验、session 选择渐进加载 | ◐ 只做了随内核而来的部分：transcript 跳过 system 消息、`usage`(cache_warm) 条目与 `entry_appended` 渲染、`/session` 的 Cache Warming 段、设置面板的 Cache warming 项；**未移植**：spinner 进编辑框边框、点击折叠摘要/技能条目、session 选择器渐进加载与取消、thinking-drop 通知去重、bug 提示 |
| D3 | `cli/args.ts`、`main.ts`、`config.ts` | `--mode` 报错 | ☑ misaka 早已校验 `--mode`；`/resume` 精确 id 改走 `findById`；CLI 文件参数图片改由会话按模型缩放 |

### E. misaka 自己的使用方

| # | 位置 | 内容 | 状态 |
|---|---|---|---|
| E1 | `extensions/misaka_lcm` | `upstream_messages`/ingest 对 system 消息与 `context_edit` 的处理 | ☑ |
| E2 | `core/research`、`core/subagent`、`core/moa`、`core/network` | `Context.systemPrompt` / `addedToolNames` 的调用方 | ☑ |

## 施工台账

（按完成顺序追加）

- **C 层第一批（2026-09-23）**：`session_manager.py` — `SessionProjection`/`build_session_projection`/`_project_context_entry`、`context_edit` 条目（`appendContextEdit`）、`usage` 条目（`appendUsage`）、`appendCompaction` 记 `systemMessage` 且 `firstKeptEntryId=None` 表示 retain-none、保留区跳过 system 消息、`findById`、`find_most_recent_session` 按 mtime 序读头即停；`system_prompt.py` 整体重写为 sections（`normalize_build_system_prompt_options`/`build_system_prompt_sections`/`build_system_prompt_state`/`diff_system_prompt_sections`，**MISAKA fork**：身份前言 + 角色栈先于工具、无 pi docs 节、多一条 batching 规则、默认 loadout 含 office）；`extensions/runner.py` — `_snapshot_event_handlers`（派发中增删 handler 下次生效）、`user_bash` fail-closed、`emit_context` 两阶段（`context` 只见对话 + `_restore_system_messages`；`context_with_system` 见全文）、`emit_boundary`、`emit_cache_warming_decision`、`emit_before_agent_start` 改收 options 返回 `{messages, systemPromptOptions}`（block 仍是 MISAKA fork）；`extensions/loader.py` — `on()` 返回退订、无 schema 工具拒注册；`moments.py` 同步两阶段 context 与 options 版 before_agent_start；`compaction/compaction.py` — `estimate_projected_context_tokens`、`_find_projected_cut_point`（含恢复省略后缀规则）、投影版 `prepare_compaction`（LCM `contextMessages` 检查点仍是 fork）、`estimate_tokens` 支持 system、拆轮提示改为 continuation 措辞（#9908）、`# Conversation` 框架、`previousSummary ?? "No prior history."`；`settings_manager.py` — `compaction.modelOverrides`、`retry.maxAgentDelayMs`、`cacheWarming` 读写；`agent_session.py` — `_runSystemPromptOptions` 取代 `_baseSystemPrompt`/`_systemPromptOverride`、`_prepare_prompt_and_tool_loadout`、`_install_agent_request_projection`/`_install_agent_boundary_hooks`/`_install_agent_forced_prompt_projection`/`_restore_tools_from_transcript`、`_refresh_finalized_context`（`_entryIdsByMessage` 以 `id()` 为键并在每次刷新重建——**fork**：pydantic 消息不可弱引用）、boundary 草稿预览/提交、`_dispatch_turn_end_boundary`、`_run_before_settle_boundary`、`_deferredSettledActions`、`_agentRunAbortRequested`/`_finish_cancelled_retry`、`_omit_recovery_attempt`（重试/溢出恢复改为 context_edit 省略而非切 state）、`_check_compaction` 投影感知、`getContextUsage`/`getSessionStats` 投影版、compaction 取消按 signal 判定、`navigateTree` 压缩中拒绝；`model_registry.py` 加 `stream()` 且 `streamSimple` 先 `normalize_context`；`agent/proxy.py` 发送归一化 transcript。
- **E1/E2**：LCM `ingest.is_memory` 把 system 消息排除在归档之外；`session_entry_to_context_messages` 的 `contextMessages` 分支以条目上的 `systemMessage` 领头（**fork**）；TUI transcript 显式跳过 system；`agent/guards.py` 新 `finish_turn_from_stop_predicate`，`install_guards`/research window/skills review/subagent child 全部从 `shouldStopAfterTurn` 迁到 `finishTurn`。
- 门：`tests` + `misaka/extensions/misaka_lcm/tests` 3256 passed；改了 8 个测试以适配新 API（`AgentContext` 无 `systemPrompt`、`session.systemPrompt` 属性、sections 化的提示词、loop 先声明工具的 system 消息）。
- **C 层第二批**：`core/cache_warmer.py`（新；`CacheWarmer`/`get_cache_warming_delay_ms`/`get_prompt_cache_ttl_ms`/`is_replayable`/`format_*`，定时器用 `loop.call_later`，**fork**：pydantic 消息不可哈希，`isCurrent` 按 `is` 比较）+ `sdk.py` 的 `cache_warmer.start(...)`/`cache_context_is_current` + `agent_session` 的 `cacheWarmingStatus`/`setCacheWarmingMode`/settled/dispose 接线 + TUI（`/session` 段、设置项、`usage` 条目渲染）；`model_registry.py` 的 `_ModelInputLimitsSchema`/`_ModelPromptCacheSchema`/`_merge_input_limits`；`prompt_templates.py` 的 `LoadedPromptTemplates`；`tools/bash.py`（退出码、时长）、`tools/read.py`（按模型 resize）、`utils/tool_result_images.py`、`agent_session._normalize_prompt_images`（先 before_agent_start 再缩图）；`extensions/types.py` 加 `ContextWithSystemEvent`/`CacheWarmingDecisionEvent`/`BoundaryContext`/`AgentBeforeSettleEvent`/`BoundaryEventResult` 与 `on()` 重载；`agent_session._run_input_handlers`/`_queue_user_input`（steer/followUp 过 input 处理器）；`cli/engine.py` 用 `findById`、文件参数不再预先缩图。
- 新测试 `tests/test_pi_087_port.py`（12 项：transcript 折叠/补丁/工具状态差、anthropic 原生工具变更请求形状、loop 的工具声明、`finishTurn` continue/end、stop-predicate 桥、sections 与 diff、`context_edit` 投影、压缩条目的 system 头与 retain-none、modelOverrides 与重试上限、cache warming 经济学）。
- 最终门：3268 passed（3256 + 12）。

- **A 层（pi-ai → misaka/ai/）2026-09-23**：`types.py` 加 `SystemMessage`/`ToolReference`/`TranscriptContext`/`MistralConversationsCompat`/`ModelPromptCache`，删 `ToolResultMessage.addedToolNames`、`deferredToolsMode`、`supportsToolReferences`；新 `utils/transcript.py`（逐函数对照 `transcript.ts`）；`utils/text.py` 加 `get_system_message_text`/`render_system_message_update`；`stream.py`/`models_runtime.py` 入口先 `normalize_context`；`transform_messages.py` 放行 system 并把夹在工具调用与结果之间的 system 消息后置；十个 provider 全部改从 transcript 取 prompt/tools（anthropic 走原生 `tool_addition`/`tool_removal` + `__pi_deferred_placeholder__`，Responses 系走 `additional_tools`/`tool_search_*` 锚点，completions 走 Kimi 的 system+tools，google/bedrock 折叠，mistral/pi-messages 就地）；`estimate.py` 按 system 消息计数；`retry.py` 加 `maxAgentDelayMs` 上限（默认 60 s）+ 520/high demand；`overflow.py` z.ai 与 cerebras 分流；`radius_provider.py` 离线基线目录；`providers/opencode_headers.py`（新）；`utils/oauth/meta.py`（新，Meta Muse 设备码登录 + key mint）+ `metaProvider` + `META_API_KEY`；Claude Code 版本 2.1.280。**两处 `# MISAKA fork:`**：anthropic 的 beta 列表走客户端头而非请求 `betas`（原有结构），`native_tool_changes` 单独算一次传给 `create_client`；`TranscriptContext` 用独立类代替 TS 的 brand。
- **B 层（pi-agent-core → misaka/agent/）**：`types.py` — `AgentContext` 去 `systemPrompt`、`LlmContext` 删（StreamFn 收 `TranscriptContext`）、`AgentToolResult` 去 `addedToolNames`、`ShouldStopAfterTurnContext`→`AgentTurnContext`、新 `AgentTurnDecision`/`FinishTurn`/`PrepareRequestContext`/`AgentRequestUpdate`/`PrepareRequest`、`AgentLoopTurnUpdate.messages`、`AgentState.systemPrompt` 变只读属性（从 transcript 回放）且构造时用 `create_initial_system_message` 播种；`agent_loop.py` — `declare_tool_changes`/`_with_tool_changes`/`NO_CHANGES`、`prepareRequest`、`finishTurn`（错误/中止也调）、`explicit_continuation`、`normalize_context`；`agent.py` — `default_convert_to_llm` 放行 system、`PendingMessageQueue.peek`、`peekQueuedMessages`、`reset` 保留回放基线、`continue_` 的全-system 守卫、去 `shouldStopAfterTurn`；`harness/messages.py` 放行 system。
- **风险复核（2026-09-23，"确定不会导致 misaka 产生 bug 么"之后）**：逐个核对 transcript 改型后 misaka 自己那些把会话交给第二个模型的缝，找到并修了三处：(1) `core/moa/provider.py` 聚合器上下文原来从 `TranscriptContext` 读 `.systemPrompt`/`.tools`（必 `AttributeError`，被 provider 兜底成 error 事件——MoA 每轮都会失败），改为直接把 `agg_messages`（已含 system 头）包成 `TranscriptContext`；trace 序列化对 system 消息改用 `get_system_message_text`。(2) `core/subagent/fork.py` — `capture` 记 `session.systemPrompt`（本轮生效提示词，含 forced 文本）而非 `agent.state.systemPrompt`；`install` 的 stream 包装原来 `model_copy(update={systemPrompt, tools})` 在 `TranscriptContext` 上静默无效（精确 fork 退化成子 worker 自己的提示词），改为按 `_install_agent_forced_prompt_projection` 的做法把所有 system 消息折成一个父端头（内容=快照提示词，`toolsAdded`=wire_tools，时间戳沿用原头）。(3) `core/subagent/child.py` 校验器 `Agent(initialState={"messages": deepcopy(parent messages)})` — 父 transcript 现在以 system 消息领头，`AgentState` 只在首条不是 system 时才播种自己的 `systemPrompt`，所以校验器会跑在父提示词与父工具声明下；改为剔除 system 消息再播种。
  - 核过没问题的：LCM（`Replay.restore` 经 `is_memory` 排除 system，`contextMessages` 分支由条目的 `systemMessage` 领头；`session_context_prepare` 用 `self.systemPrompt` 属性）；`compaction/branch_summarization`、`compaction/utils.serialize_conversation`（无 system 分支即跳过）；`request_budget.context_token_upper_bound`（system 头带着提示词与工具声明一起进 JSON 上界）；`research/window.py` 的 streamFn 只做恢复；tree_selector/footer/export_html/sister_runtime/subagent resume/panel 只按 user/assistant/toolResult 分支，system 落到通用分支；anthropic 原生工具变更的 catalog 标志与上游 `providers/data/anthropic.json` 15/15 一致。
  - 新测试 `tests/test_pi_087_port_roundtrip.py`（5 项，真实 SDK 装配）：同一会话文件重开再跑一轮，**只有一个 system 头**，不重声明工具、不重打提示词补丁，两次请求的头逐字节相同；移植前写的旧会话文件（无 system 头）下一轮只产生一个头（`user, assistant, system, user, assistant`），发出的提示词等于本轮生效提示词、工具集等于 active tools；MoA 聚合器收到的 transcript 只有一个头且提示词/工具原样（在未修的 provider 上确认失败）；精确 fork 把三条 system 消息折成父端头；`AgentState` 播种规则。
  - 门：3273 passed, 1 skipped（3268 + 5）；`tests/test_subagent_native_startup.py` 的会话替身改为在 session 层提供 `systemPrompt`。

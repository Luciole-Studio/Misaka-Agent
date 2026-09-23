# Anthropic / s2a 推理档位与错误详情对齐

## 对照基线与范围

- Pi `main`：[`b03a367a4fbc02df81bfd96702d7a12c2d79aa45`](https://github.com/earendil-works/pi/tree/b03a367a4fbc02df81bfd96702d7a12c2d79aa45)，本轮开始时重新查询确认。
- 模型元数据：官方发布包 `@earendil-works/pi-ai@0.85.1`。本次涉及的八个模型，其 `thinkingLevelMap`、`forceAdaptiveThinking` 与 MISAKA 内建目录一致，无须手改生成目录。
- 范围：Anthropic 错误终止、推理级别配置/映射/请求预算、档位说明文案，以及本机自定义 s2a provider 的模型配置。不是整个 Pi 或所有 provider 的全功能对齐声明。

## 已移植的上游逻辑

| 本地位置 | Pi 对照位置 | 修正 |
|---|---|---|
| `ai/providers/anthropic.py::map_stop_reason`、流处理 | `packages/ai/src/api/anthropic-messages.ts::mapStopReason`、`message_delta` | 保留 `rawStopReason`；传递 `stop_details.explanation`；缺少 explanation 时使用上游原文；`sensitive` 和未知枚举的错误原文一致 |
| `ai/types.py::AssistantMessage` | `packages/ai/src/types.ts::AssistantMessage` | 增加可序列化的 `rawStopReason`；补上 Anthropic 流的 `pending` 状态，缺少最终 stop reason 不再当作成功 |
| `core/model_registry.py::_ThinkingLevelMapSchema` | `packages/coding-agent/src/core/model-config.ts::ThinkingLevelMapSchema` | 补上 `max` 的字符串/null 校验；原先合法值能穿透，但非法类型没有在此处校验 |
| `ai/providers/anthropic.py::build_params` | `packages/ai/src/api/anthropic-messages.ts::buildParams` | `thinkingLevelMap.off=null` 时不发送 `thinking.type=disabled` |
| `ai/providers/anthropic.py::stream_simple_anthropic` | `packages/ai/src/api/anthropic-messages.ts::streamSimple` | 加入思考预算后再次按剩余上下文限制 max tokens，再计算回答预留空间 |
| `ui/tui/interactive/components/thinking_selector.py` 及调用处 | `packages/coding-agent/src/modes/interactive/components/thinking-selector.ts::LEVEL_DESCRIPTIONS` | 七档说明逐字采用 Pi 文案，移除额外的 adaptive 说明分支 |

`get_supported_thinking_levels`、`clamp_thinking_level`、effort 映射及预算辅助函数的既有语义已一致，未为了“对齐”重写等价代码。沿用 Python 类型、命名及已存在的预算/上下文辅助函数；移植分支的报错和说明文字保持上游原文。

档位文案里的近似 token 数是 Pi 的说明文字，不代表 adaptive 模型实际使用固定预算。`minimal` 与 `low` 在 Anthropic adaptive 路径仍均映射为 `low`。

## 本机配置修正（不纳入 Git）

- 补入 Fable 5.1 和 Fable 5。
- 逐模型配置内建目录对应的 `thinkingLevelMap`。
- 把 provider 级统一 adaptive 改成模型级能力；Haiku 4.5 使用手动 token budget。
- 保持凭据、默认 provider/model、已有模型的费用配置及输出上限不变；新 Fable 条目使用 Pi 元数据和该自定义 provider 既有的 32,000 输出上限。
- 写入前验证配置并比较原文件 SHA-256；原配置备份为仅用户可读文件；`auth.json`、`settings.json` 写前写后哈希一致。
- 未修改或重启 s2a 服务，未启用自动模型 fallback，未上传个人配置。

### 反代兼容边界

Fable 5.1 和 Opus 5 的原生 Pi 元数据支持逐轮 effort 控制，但当前本地 s2a 链路实测对该请求返回 HTTP 400：`messages.1.output_config: Extra inputs are not permitted`。

因此只在这个自定义 provider 中显式保留 `supportsMidConvoEffort=false`，通过顶层 `output_config.effort` 发送档位；MISAKA 原生 Anthropic 目录与逐轮 effort 实现不变。符合 [Pi 的 opt-in 要求](https://github.com/earendil-works/pi/blob/b03a367a4fbc02df81bfd96702d7a12c2d79aa45/packages/coding-agent/docs/models.md#L395-L397)，没有把不支持的传输协议强行打开。

## 验收

- 新增 `tests/test_anthropic_pi_parity.py`：53 项测试，覆盖真实流处理入口、序列化、错误枚举、缺少终止事件、off/null、max 校验、上下文限额、八个模型共 49 个配置档位，以及原生逐轮/反代顶层两条 effort 路径。
- 聚焦与相邻回归：128 passed。
- 上游差分：抽取固定提交的原始 TypeScript 辅助函数执行，与 Python 对比 13 个模型配置、240 个预算案例、24 个终止原因案例，结果一致。不是只拿本地预期测试本地实现。
- 修正后的正式本机配置进行了 49 个模型/档位组合的真实请求：43 个正常完成且返回模型 ID 匹配；Fable 5 的 6 个组合仍由上游返回 `refusal`，但均正确显示具体 explanation，不再变成未知错误。
- Fable 5 的服务端限制不是这次客户端修复已解决的事项；没有通过更换模型隐藏失败。
- 最终全量测试：**2418 passed, 1 skipped, 144 subtests passed**（50.49 秒）。代码哈希在验收后再次核对，未发生漂移。
- Ruff、`git diff --check` 通过。

## 运行中的进程

本轮没有重启已有 MISAKA 会话。新进程读取完整修复；仅刷新模型配置不等于替换运行中已导入的 Python 模块。

本轮临时源码和差分验证记录位于 `/tmp/misaka-pi-model-compare-20260916/`；该目录及私人配置备份不属于仓库交付物。

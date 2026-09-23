# MISAKA 实时故障复核

核验时间：2026-09-20 18:40起，日本时间。只读观察活动运行 r_b55f2c1cf5；未发消息、重启、改卡或改模型。离线探针只写本临时目录，未调用任何模型或网络服务。

## 1. web_extract 整批失败仍标非错误：已确认的实现缺陷

全程扫描63份会话、3832条工具回执。在解析成功的web_extract回执中，12次results的每项均有error且content为空，但外层isError=false。最近一例为16:33:48 C3。

- [原始日志](/Users/makiko/.misaka/sessions/cards/t_7ee3b0/01a0bdb9-8429-7313-ab0b-f8cdbc01d3e4/subagents/agent-a0151479596d3a6d1.jsonl:41)
- 根因：[extract.py](/Users/makiko/Projects/misaka/misaka/core/web/extract.py:641)仅判断顶层success/error，没有汇总results中的逐项失败。
- 离线假返回探针复现：all_failed/partial_success/success目前全部isError=false。应区分全失败和部分成功，并保留逐项原因。
- 本轮未修。

## 2. 只读复合命令被统一判为路径不明确：现存权限能力限制

17:02 C4、17:13 C17、18:23红队子代理的只读检索/多文件读取触发Workspace safety requires approval。不是所有批准请求都是bug；Python脚本、网络CLI等本就未必可由静态规则判为只读。可确认的问题是安全的单命令与它们的只读组合被截然对待。

- 离线对照：rg检索允许，加上管道head即ask；cat单文件允许，两个cat以分号组合即ask；字面量花括号多文件也ask。
- 根因：[policy.py](/Users/makiko/Projects/misaka/misaka/core/subagent/policy.py:205)把复合shell语法统一视为需额外检查；[restricted-path检查](/Users/makiko/Projects/misaka/misaka/core/subagent/policy.py:635)在此直接给出路径歧义。
- 这是保守限制导致的可用性问题，不是文件真的越界。修正应扩充可证明只读的语法支持，或让提示词明确使用原生read/grep；不应全局放开shell。
- [红队实例](/Users/makiko/.misaka/sessions/cards/t_759180/01a0be1d-35c4-7b56-bc07-e4898793c860/subagents/agent-aaef890f767de95f4.jsonl:13)：2026-09-20T09:23:13.257Z，{'command': 'cat nodes/r_b55f2c1cf5/cards/{t_c2dbca/c5_iberia_americas.md,t_7da32d/c6_italy_germany.md,t_c17367/c7_orient_india.md,t_b09c54/c8_order_model.md}',
- [红队实例](/Users/makiko/.misaka/sessions/cards/t_759180/01a0be1d-35c4-7b56-bc07-e4898793c860/subagents/agent-a147e072adc0f8e17.jsonl:11)：2026-09-20T09:23:06.180Z，{'command': 'for p in t_3dc3c2 t_2c338c t_26cd47 t_75e9a5 t_d604cd; do ls nodes/r_b55f2c1cf5/cards/$p; done'}
- [红队实例](/Users/makiko/.misaka/sessions/cards/t_759180/01a0be1d-35c4-7b56-bc07-e4898793c860/subagents/agent-a147e072adc0f8e17.jsonl:23)：2026-09-20T09:23:33.474Z，{'command': "python3 - <<'PY'\nfrom pathlib import Path\njobs={'326b19e197a6':[(40,53)],'4dd061a66f88':[(60,88)],'e5dd2552be6d':[(117,143),(167,187)],'4b8f2a2c5
- [红队实例](/Users/makiko/.misaka/sessions/cards/t_759180/01a0be1d-35c4-7b56-bc07-e4898793c860/subagents/agent-a8c0d56804227fbd0.jsonl:11)：2026-09-20T09:23:04.923Z，{'command': 'find nodes/r_b55f2c1cf5/cards/{t_104134,t_7ee3b0,t_208a11} -type f; nl -ba nodes/r_b55f2c1cf5/synthesis.md', 'timeout': 20}
- [红队实例](/Users/makiko/.misaka/sessions/cards/t_759180/01a0be1d-35c4-7b56-bc07-e4898793c860/subagents/agent-a5e91174132ceea83.jsonl:9)：2026-09-20T09:23:02.920Z，{'command': 'find nodes/r_b55f2c1cf5/cards/{t_95ef51,t_bef693,t_f3c44f,t_f81a6f,t_b7b7b1} -type f; nl -ba nodes/r_b55f2c1cf5/synthesis.md', 'timeout': 10}

## 3. 参数解析诊断丢失：此前已发现，仍在

- [json_parse.py](/Users/makiko/Projects/misaka/misaka/ai/utils/json_parse.py:370)将ValueError/RecursionError统一为一条通用提示，并把arguments置空。会话缺少失败时原始片段/具体JSON错误定位。
- 16:34 LO edit失败，16:35重发成功；本项是诊断能力缺陷，不据此认定模型、代理或解析器谁制造了错误。

## 已观察异常，但不归为新MISAKA代码bug

- 红队18:20被服务商返回rawStopReason=refusal；[日志](/Users/makiko/.misaka/sessions/cards/t_759180/2026-09-20T09-19-40-484Z_01a0be1d-35c4-7b56-bc07-e4898793c860.jsonl:11)。18:21已有model_change切换到openai-codex/gpt-6-astra及用户“继续”，随后实际工具调用恢复。
- C10/C13/C14发生WebSocket closed 1006，后续恢复且卡片均已done；现有记录不证明客户端实现错误。
- 已完成卡收不到新定向消息是当前生命周期契约，回执明确提示通知LO，不把它当丢消息bug。
- 猜错文件路径、读超行号、python命令不存在、远端403/404和下载大小限制分别属于工具使用/环境/站点/既定限制，不把报错数等同bug数。
- 没有新增压缩守卫失败记录；旧子代理写权限和交付解析失败仍留历史日志，不列为新发生故障。

## 证据文件

- [离线探针结果](/private/tmp/misaka-bug-audit-20260920-1840/probe-result.json)
- [Web逐项失败记录](/private/tmp/misaka-bug-audit-20260920-1840/extract-failures.json)
- [工具错误记录](/private/tmp/misaka-bug-audit-20260920-1840/tool-errors.json)
- [提供商异常记录](/private/tmp/misaka-bug-audit-20260920-1840/provider-errors.json)
- [Board快照](/private/tmp/misaka-bug-audit-20260920-1840/board.json)

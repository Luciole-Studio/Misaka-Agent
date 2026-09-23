# MISAKA 问题总账 · 2026-09-20

留存时间：2026-09-20T19:50:34.697190+09:00（JST）。本文件是当时快照，不是持续监控。

## 30 秒总览

**21 项跟踪记录 = 17 项已确认产品问题 + 4 项另外跟踪。**

- **6 项待修**：会话误切记录、二进制混入目录、网页全失败假成功、参数错误诊断丢失、旁观流式内容缺失、前台阶段描述缺失。
- **11 项已有源码修复记录**：LO 档位、Sister 并发、子代理局部写权限、PDF 故障隔离、PDF 错误标记、子进程重复回收、Sister 档位、压缩误报、交付契约、Sessions 状态、LCM 长检索。
- **4 项另外跟踪**：只读 shell 分类限制、换供应商后的恢复边界、测试隔离问题、历史产物缺失。它们不是 4 个新证实的产品 bug。

**计数规则：按独立缺陷点分组，不按报错次数/文件数计数。** PDF 的故障隔离与错误标记是不同缺陷；最新一次研究暂停也包含两个不同缺陷。Sister 的多个档位覆盖入口合并成一组。旧 09-18 全面审计和提示词架构需求不混进本轮总数。

## 最新事故：先看这一条链

```text
PNG 产物被当 Markdown 扫标题 [U02]
  → 1,049,249 字符工具回包，二进制乱码进入历史
  → 字符串中出现合法 U+0085
  → fork 从磁盘重读历史，splitlines 错切记录 [U01]
  → InvalidSessionFileError，研究 driver 失败/对外提示暂停
```

**纠正：不是已经证实的半写入或文件截断。** 保存的快照按实际 LF 逐行解析 709 条全部合法；当前解析函数可复现用户看到的同一个报错。不要先删会话尾巴。

最新只读状态：2026-09-20T19:50:34.720879+09:00；run `r_b55f2c1cf5` 为 `failed/active`，driver_lock 为空；根节点 reviewing、首个子节点 queued 且 session_file 为空。匹配本 run 产物目录的卡片 19 张，状态计数 {"done": 19}。这不等于整项研究已经完成。

恢复前应处理 U01/U02，并在隔离副本验证已有分支占位、已接受的后续调查命令不会重复执行。当前进程不动，不直接改 DB/card/session。

## 一览表

| 编号 | 问题 | 状态 | 优先级 |
|---|---|---|---|
| U01 | 合法 JSONL 被 Unicode 分行误判为损坏 | 待修 | P1 |
| U02 | 工作区目录把 PNG 二进制当 Markdown 读入 | 待修 | P1 |
| U03 | 网页提取全部失败却返回成功标记 | 待修 | P2 |
| U04 | 工具参数解析失败时丢失定位信息 | 待修 | P2 |
| U05 | Sessions 旁观页看不到尚未完成的流式消息 | 待修 | P2 |
| U06 | 前台 Research 根节点缺少阶段/深度描述 | 待修 | P2 |
| F01 | 进入 Research 强制覆盖 LO 思考档位 | 已有源码修复记录 | P2 |
| F02 | Sister 并发名额误用 LO 并发上限 | 已有源码修复记录 | P2 |
| F03 | 子代理未继承当前任务输出目录的写权限 | 已有源码修复记录 | P1 |
| F04 | PDF 原生渲染共享状态缺少故障隔离 | 已有源码修复记录 | P1 |
| F05 | PDF 页图工具把失败当普通成功文本 | 已有源码修复记录 | P2 |
| F06 | 子进程被 asyncio 与进程树清理器重复等待回收 | 已有源码修复记录 | P2 |
| F07 | Sister 根会话默认 high 被覆盖为 low/off | 已有源码修复记录 | P2 |
| F08 | Compaction 把无关记账追加误判成上下文切换 | 已有源码修复记录 | P1 |
| F09 | 交付说明与交付文件名没有拆开 | 已有源码修复记录 | P1 |
| F10 | Sessions 混淆子代理状态与父卡片状态 | 已有源码修复记录 | P2 |
| F11 | LCM 长检索表达式撞上 SQLite 深度限制 | 已有源码修复记录 | P2 |
| L01 | 只读复合 shell 被保守权限分类挡住 | 待设计 | P2 |
| L02 | 切换 LO 供应商不等于自动复活已失败的 driver | 待设计 | P1 |
| T01 | 旧面板测试可能触及真实消息库 | 测试侧已有修复 | P1 |
| D01 | 历史产物有登记却找不到文件 | 待核实归因 | P2 |

## 逐项定位与验收

P1 = 可阻断研究/影响会话或数据边界；P2 = 工具、配置、显示或诊断问题。此处为本次维修排序，不是安全漏洞评级。

### U01 · 合法 JSONL 被 Unicode 分行误判为损坏

**已确认产品问题 · 待修 · P1**

- **现象：** 19:31 左右研究 r_b55f2c1cf5 创建后续分支时抛 InvalidSessionFileError，错误为 Unterminated string starting at: line 1 column 293 (char 292)。
- **原因：** 原生会话读取器对整个文件使用 str.splitlines()，把 JSON 字符串中合法的 U+0085 当成记录边界；不是实际 LF 处的坏 JSON。fork_session 重新从磁盘读取旧记录，触发此前内存执行未遇到的错误。
- **代码点：** [session_manager.py:1203](/Users/makiko/Projects/misaka/misaka/core/session_manager.py#L1203)–1219；[session_manager.py:533](/Users/makiko/Projects/misaka/misaka/core/session_manager.py#L533)–540；[planner.py:600](/Users/makiko/Projects/misaka/misaka/core/research/planner.py#L600)–613；[workflow.py:650](/Users/makiko/Projects/misaka/misaka/core/research/workflow.py#L650)–680
- **证据：** session-parse-proof.json：19:36:48 捕获 32,786,121 bytes；按 LF 分隔 709 条全部合法；splitlines 得 733 段，含 24 个 U+0085；原解析函数复现相同错误。第一个受影响物理行是 619，不是整个文件第 1 行。
- **处理：** 仅按 JSONL 的 LF 记录边界解析，保留严格 JSON 校验；不靠删除记录、改写用户历史或放松 strict 验证掩盖问题。
- **验收：** 同一文件按修复后的解析器通过；U+0085/U+2028/U+2029 字符串回归通过；真正截断尾记录仍按既有严格契约报错；隔离副本的原生 fork/resume 成功。
- **边界：** 本轮只读复现，没有修改会话或恢复研究。进入分支前的 DB 占位已经创建；后续恢复须验证复用占位而非重复派发。

### U02 · 工作区目录把 PNG 二进制当 Markdown 读入

**已确认产品问题 · 待修 · P1**

- **现象：** 18:31:53 的 misaka_research_view(workspace) 回包长达 1,049,249 字符，出现图片二进制乱码，随后进入原生历史及多个压缩记录。
- **原因：** _artifact_node 无条件用 UTF-8/errors=replace 打开所有产物，按 # 扫描标题；忽略登记中的 binary=true。随机二进制被生成伪标题，也带入触发 U01 的 U+0085。
- **代码点：** [workspace.py:24](/Users/makiko/Projects/misaka/misaka/workspace.py#L24)–39；[tools.py:59](/Users/makiko/Projects/misaka/misaka/core/research/tools.py#L59)–63
- **证据：** session-parse-proof.json：C1 的 tetlock_ocr/p16.png，artifact a_4b184a148f，PNG 魔数 89504e470d0a1a0a；独立读取生成 7 个伪标题/1471 字符/1 个 U+0085。来源登记 metadata.binary=true。
- **处理：** 目录保留二进制产物的文件节点/元信息，但只对可读 Markdown 文本生成标题树；限制单标题及总目录预算。不要把已有图片文件删除。
- **验收：** PNG/PDF 等产物目录不出现二进制内容；真实 Markdown 标题保持；大型文件和标题有界；现存含特殊字符的历史仍由 U01 正确读回。
- **边界：** U01 与 U02 是同一次事故里的两个独立根因，分别计数；修 U01 解决误报，修 U02 阻止污染继续发生。

### U03 · 网页提取全部失败却返回成功标记

**已确认产品问题 · 待修 · P2**

- **现象：** 12 批 web_extract 的每个结果都是 error 且 content 为空，但外层 isError=false。
- **原因：** 工具包装只检查顶层 success/error，不检查 results 内全部条目失败的情形。
- **代码点：** [extract.py:640](/Users/makiko/Projects/misaka/misaka/core/web/extract.py#L640)–654
- **证据：** tool-probe-result.json 独立假后端复现：all_failed / partial_success / success 都返回 isError=false；18:40 审计发现 12 批全失败、45 批至少一项失败，最后一批全失败在 16:33:48。
- **处理：** 按逐项结果汇总：全失败标为错误，部分成功保持可用并明确部分失败，保留原始逐项原因。
- **验收：** 全失败=true、全成功=false、部分成功不被误当全失败；空结果/坏 JSON 有明确语义。
- **边界：** 没有把网页拒绝访问等远端问题都归咎 MISAKA；本条是其错误标记确实错了。

### U04 · 工具参数解析失败时丢失定位信息

**已确认产品问题 · 待修 · P2**

- **现象：** 16:34:33 的 edit 返回 Invalid final tool arguments; resend a complete JSON object，16:35:00 重试成功。
- **原因：** finish_into 捕获异常后把 arguments 改为 {}，只保留通用提示，未在该记录保存解析位置/原因和可控的原始参数诊断信息。
- **代码点：** [json_parse.py:357](/Users/makiko/Projects/misaka/misaka/ai/utils/json_parse.py#L357)–377；[agent_loop.py:650](/Users/makiko/Projects/misaka/misaka/agent/agent_loop.py#L650)–670
- **证据：** LO 原生会话物理行 461、463–464；18:46 审计报告已记录。错误调用被调度层拦截，没有执行不完整的 edit。
- **处理：** 维持禁止执行残缺参数的规则；附解析错误类型、偏移、输入长度和有界脱敏摘要/受限诊断引用，保留关联 tool-call ID。
- **验收：** 畸形调用仍不执行；完整重试正常；日志足够区分截断/非对象/语法错误，又不外泄 TOKEN 等敏感参数。
- **边界：** 已确认的是诊断缺口；原始畸形参数来自模型、代理网关还是流组装，现有证据不足以归因。它与 U01 不是同一个解析器问题。

### U05 · Sessions 旁观页看不到尚未完成的流式消息

**已确认产品问题 · 待修 · P2**

- **现象：** 13:14–13:15 原窗口提取文本 23,693→25,967 字符，旁观 snapshot cursor 却保持 [8b441192,8]，不包含正在生成的 assistant。
- **原因：** snapshot 只返回 SessionManager 已完成 entries；cursor 仅 leaf/count。旁观消费者仅在这个 cursor 改变时更新，没有单独的临时流式状态。
- **代码点：** [session_control.py:145](/Users/makiko/Projects/misaka/misaka/core/session_control.py#L145)–152；[chat.py:265](/Users/makiko/Projects/misaka/misaka/cli/chat.py#L265)–283
- **证据：** early-live-audit.md 的 F1；本轮重新确认源码仍是相同路径。
- **处理：** 复用内存中的流式消息，增加临时视图及更新版本；保持原生消息完成后才持久化。
- **验收：** 原窗口与旁观页都能显示增量文本；完成后不重复；无把半条消息写入会话或 LCM 的副作用。
- **边界：** 不是所有“思考变少”的通用解释，只证实旁观路径；不代表供应商实际推理 token 减少。

### U06 · 前台 Research 根节点缺少阶段/深度描述

**已确认产品问题 · 待修 · P2**

- **现象：** 13:15 根 LO 正在 planning，SessionControl 返回 workflow={}。后台节点却有 node/depth/phase。
- **原因：** describe 默认为 dict；node_session 后台路径绑定了阶段描述，现有聊天根节点只创建 WindowLO，没有相同绑定。
- **代码点：** [session_control.py:46](/Users/makiko/Projects/misaka/misaka/core/session_control.py#L46)–58；[window.py:252](/Users/makiko/Projects/misaka/misaka/core/research/window.py#L252)–259；[workflow.py:1477](/Users/makiko/Projects/misaka/misaka/core/research/workflow.py#L1477)–1487
- **证据：** early-live-audit.md 的 F2；本轮重读当前源码确认未补齐。
- **处理：** 前后台复用同一节点描述绑定，并在研究退出时恢复原普通会话回调。
- **验收：** 同一研究阶段的前后台接口描述一致；结束后不遗留旧节点信息；不额外创建会话。
- **边界：** 这是观测信息缺失，并非实际退出 research。

### F01 · 进入 Research 强制覆盖 LO 思考档位

**已确认产品问题 · 已有源码修复记录 · P2**

- **现象：** 用户设 max，输入 /research 后变 high。
- **原因：** Research 多个入口显式覆盖 thinking，而非沿用 LO 默认/原生已保存档位。
- **代码点：** [planner.py:180](/Users/makiko/Projects/misaka/misaka/core/research/planner.py#L180)–201；[window.py:95](/Users/makiko/Projects/misaka/misaka/core/research/window.py#L95)–120；[node.py:398](/Users/makiko/Projects/misaka/misaka/core/research/node.py#L398)–418；[report.py:120](/Users/makiko/Projects/misaka/misaka/core/research/report.py#L120)–140
- **证据：** 13:08 thinking-settings 修复记录；13:10 启动的新 LO 在 13:15 现场仍为 max，见 early-live-audit.md。
- **处理：** 已删除 Research 强制 high，沿用默认设置、模型偏好和已有原生会话档位。
- **验收：** 普通/Research/原生 fork 保留期望默认和覆盖优先级。
- **已有回归：** [test_agent_model_runtime.py:189](/Users/makiko/Projects/misaka/tests/test_agent_model_runtime.py#L189)
- **边界：** 有后启动进程的现场佐证，但不等于所有供应商出站请求已逐字段抓包验证。

### F02 · Sister 并发名额误用 LO 并发上限

**已确认产品问题 · 已有源码修复记录 · P2**

- **现象：** 18 张任务卡只启动 4 个 Sister，用户预期 8；窗口数也不等于 Sessions 总数。
- **原因：** 每节点 Sister 槽位与 LO 分支并发复用同一上限，配置没有独立传到底层。
- **代码点：** [workflow.py:447](/Users/makiko/Projects/misaka/misaka/core/research/workflow.py#L447)–462；[runs.py:144](/Users/makiko/Projects/misaka/misaka/core/research/runs.py#L144)–151；[research.py:20](/Users/makiko/Projects/misaka/misaka/core/research/wiring/research.py#L20)–44
- **证据：** 14:21 sister-parallel 补丁与 focused-tests.log：125 passed；独立新增 sister_parallel，并贯通 CLI、聊天、保存与恢复。
- **处理：** 已拆开 --parallel（LO 节点）与 --sister-parallel（每 LO 的 Sister 卡片）；仍遵守全局/每 Sister 准入限制。
- **验收：** 设置 8 且满足所有准入条件时能调度 8 张；依赖/待帮助/未结算任务正确计槽；旧 run 不擅自改配置。
- **已有回归：** [test_research_sister_parallel.py:1](/Users/makiko/Projects/misaka/tests/test_research_sister_parallel.py#L1)
- **边界：** 并非设置一个 8 就强制永远开 8 个窗口；本条补丁未热更新当时旧 driver。

### F03 · 子代理未继承当前任务输出目录的写权限

**已确认产品问题 · 已有源码修复记录 · P1**

- **现象：** Sister 的 sub-agent 写自己卡片的产物也被批准墙拦截，出现多次 write/edit 权限失败。
- **原因：** 卡片已拥有的局部产物写权限未作为有效、可撤销的范围传给原生子代理。
- **代码点：** [todo.py:399](/Users/makiko/Projects/misaka/misaka/core/network/todo.py#L399)–416；[configuration.py:108](/Users/makiko/Projects/misaka/misaka/core/subagent/configuration.py#L108)–166；[policy.py:1040](/Users/makiko/Projects/misaka/misaka/core/subagent/policy.py#L1040)–1056
- **证据：** 14:45 runtime-repairs 补丁；185 passed/45 subtests 的隔离 smoke 覆盖此路径。
- **处理：** 已继承当前有效 generation/claim 的卡片 output_dir，仅 write/edit；失去所有权即撤销，路径/链接越界仍拦截。
- **验收：** 当前卡片内可写；其他卡片/受保护路径不可越权；claim 失效后不再继承。
- **已有回归：** [test_runtime_tool_repairs.py:109](/Users/makiko/Projects/misaka/tests/test_runtime_tool_repairs.py#L109)；[test_runtime_tool_repairs.py:140](/Users/makiko/Projects/misaka/tests/test_runtime_tool_repairs.py#L140)；[test_subagent_native_startup.py:176](/Users/makiko/Projects/misaka/tests/test_subagent_native_startup.py#L176)
- **边界：** 只解决已授权局部产物写入；不等同于放开任意 shell 或私有技能树。

### F04 · PDF 原生渲染共享状态缺少故障隔离

**已确认产品问题 · 已有源码修复记录 · P1**

- **现象：** PDF 页图工具在并发或坏文件场景暴露 PDFium 原生状态/崩溃风险。
- **原因：** 原实现在线程中直接使用同一进程的 PDFium，全局原生状态并未获得进程级隔离。
- **代码点：** [documents.py:135](/Users/makiko/Projects/misaka/misaka/core/documents/wiring/documents.py#L135)–152；[_pdf_render.py:1](/Users/makiko/Projects/misaka/misaka/core/documents/wiring/_pdf_render.py#L1)–47
- **证据：** 14:45 runtime-repairs 补丁与隔离并发 PDF、损坏文档回归。
- **处理：** 已移入短命渲染子进程，保留尺寸约束，设置超时，回收 native 资源并保留退出诊断。
- **验收：** 并发页图稳定；坏 PDF/渲染子进程故障不杀死 Sister 主会话。
- **已有回归：** [test_runtime_tool_repairs.py:31](/Users/makiko/Projects/misaka/tests/test_runtime_tool_repairs.py#L31)
- **边界：** 现场个别 PDF 失败不能全部断言由同一个原生竞争引起；本条确认的是已复现/修补的隔离缺陷。

### F05 · PDF 页图工具把失败当普通成功文本

**已确认产品问题 · 已有源码修复记录 · P2**

- **现象：** 文档不存在、非 PDF、页码越界、渲染异常等返回普通文本而不是错误回执。
- **原因：** 异常分支走 _text 返回，外层不能据此区分失败。
- **代码点：** [documents.py:264](/Users/makiko/Projects/misaka/misaka/core/documents/wiring/documents.py#L264)–293
- **证据：** 14:45 runtime-repairs task.patch；test_page_failures_are_real_tool_errors。
- **处理：** 已让失败进入真实工具错误通路，并验证 scale 为有限正数。
- **验收：** 无文档/坏参数/越界/渲染异常均 isError=true，正常页图保持成功。
- **已有回归：** [test_runtime_tool_repairs.py:47](/Users/makiko/Projects/misaka/tests/test_runtime_tool_repairs.py#L47)
- **边界：** 与 U03 同属标记问题，但分别位于 PDF 和 web_extract 两个实现；修一个不会自动修另一个。

### F06 · 子进程被 asyncio 与进程树清理器重复等待回收

**已确认产品问题 · 已有源码修复记录 · P2**

- **现象：** 原生子代理退出时可能出现 child watcher 状态丢失/未知退出状态。
- **原因：** psutil.wait_procs 和 asyncio watcher 可能等待同一个直接子进程，抢先收割退出状态。
- **代码点：** [runtime.py:70](/Users/makiko/Projects/misaka/misaka/core/subagent/runtime.py#L70)–100；[processes.py:202](/Users/makiko/Projects/misaka/misaka/core/platform/processes.py#L202)–250
- **证据：** 14:45 runtime-repairs 补丁与 watcher-baseline-red.log；延迟 watcher 回归。
- **处理：** 已让 asyncio 独占其直接 child 的 wait；进程树仍清理后代，保留 kill/wait 兜底。
- **验收：** 正常退出/忽略 TERM/慢 watcher 均保留真实退出码；后代清理仍生效。
- **已有回归：** [test_runtime_tool_repairs.py:202](/Users/makiko/Projects/misaka/tests/test_runtime_tool_repairs.py#L202)；[test_runtime_tool_repairs.py:241](/Users/makiko/Projects/misaka/tests/test_runtime_tool_repairs.py#L241)
- **边界：** 这是进程 watcher，不是 U05 的界面旁观者，两者不可合并。

### F07 · Sister 根会话默认 high 被覆盖为 low/off

**已确认产品问题 · 已有源码修复记录 · P2**

- **现象：** 用户设置 Sister 默认 high，卡片里变 low；托管根还可能套用通用 child 的 off。
- **原因：** 卡片启动强制 low，托管 Sister 根与一般子代理的默认策略混用。
- **代码点：** [worker.py:631](/Users/makiko/Projects/misaka/misaka/core/network/worker.py#L631)–639；[sister_runtime.py:350](/Users/makiko/Projects/misaka/misaka/core/network/sister_runtime.py#L350)–368
- **证据：** 15:17 final-repair-validation.md 的 Sister thinking 项。
- **处理：** 已移除强制低档；保留默认、模型偏好、已保存会话与显式 override；通用子代理策略不变。
- **验收：** 新卡片/恢复卡片/托管 Sister/显式覆盖的优先级分别正确。
- **边界：** 当时已运行的旧 Sister 不会因为磁盘改了而自动换档；不把所有 child 的 low 都报成这条 bug。

### F08 · Compaction 把无关记账追加误判成上下文切换

**已确认产品问题 · 已有源码修复记录 · P1**

- **现象：** Auto-compaction failed: Session, branch, model or settings changed during compaction。
- **原因：** 压缩等待期间追加的非上下文记账记录也改变 leaf/entries，旧检查一概拒绝发布，即使有效上下文未改变。
- **代码点：** [agent_session.py:1510](/Users/makiko/Projects/misaka/misaka/core/agent_session.py#L1510)–1534
- **证据：** 15:17 final-repair-validation.md 与 test_compaction_source_guard；记录型追加可独立复现。
- **处理：** 已仅容许原分支上连续的 custom/label/session_info 记账追加；真正消息、模型、分支、设置变化仍拒绝过期摘要，并报告变更字段。
- **验收：** 记账追加不误杀压缩；真实上下文变化绝不发布旧摘要；所有异步边界一致检查。
- **已有回归：** [test_compaction_source_guard.py:49](/Users/makiko/Projects/misaka/tests/test_compaction_source_guard.py#L49)；[test_compaction_source_guard.py:125](/Users/makiko/Projects/misaka/tests/test_compaction_source_guard.py#L125)
- **边界：** 早先现场日志没保存究竟哪个字段变了；不能断言用户每次看到该错误都属于误报。真实切模型时拒绝旧压缩结果仍是正确行为。

### F09 · 交付说明与交付文件名没有拆开

**已确认产品问题 · 已有源码修复记录 · P1**

- **现象：** C6/C9 交付字段的中文整段说明被当文件名，misaka_card_complete 卡住；C2 当时疑似。
- **原因：** 卡片契约同时承载 basename 与说明，提交门禁用未规范化整段字段定位文件。
- **代码点：** [card_contract.py:5](/Users/makiko/Projects/misaka/misaka/core/network/card_contract.py#L5)–39；[planner.py:1](/Users/makiko/Projects/misaka/misaka/core/research/planner.py#L1)–35；[report.py:1](/Users/makiko/Projects/misaka/misaka/core/research/report.py#L1)–45
- **证据：** t_7da32d→c6_italy_germany.md；t_bef693→c9_war_finance.md；t_104134→c2_naval_econ_data.md（当时疑似，不能追认）；15:17 修复记录。
- **处理：** 已共享拆分器，规范为文件名单独一行+说明；兼容已约定的旧中文冒号/Write 形式，保留严格交付文件校验。
- **验收：** 已支持的历史契约正常提交；缺文件、空文件、歧义、非法路径和符号链接越界仍拒绝。
- **已有回归：** [test_card_completion.py:1](/Users/makiko/Projects/misaka/tests/test_card_completion.py#L1)；[test_card_contract_defaults.py:1](/Users/makiko/Projects/misaka/tests/test_card_contract_defaults.py#L1)
- **边界：** 没有在这轮汇总中直接编辑卡字段或代替 Sister 完成卡片。

### F10 · Sessions 混淆子代理状态与父卡片状态

**已确认产品问题 · 已有源码修复记录 · P2**

- **现象：** 同一 session 的图标显示已结束/绿色，文字却仍 running。
- **原因：** child 生命周期与关联父卡片 task_status 被放到同一个状态表达中。
- **代码点：** [session_catalog.py:433](/Users/makiko/Projects/misaka/misaka/core/session_catalog.py#L433)–440；[panel.py:1718](/Users/makiko/Projects/misaka/misaka/ui/panel/panel.py#L1718)–1727
- **证据：** 15:17 final-repair-validation.md；独立 catalog/panel 测试。
- **处理：** 已拆分 child_status、parent_task_status；面板明确显示各自状态。
- **验收：** child done + parent card running 不再显示为 child running；卡片/原生/托管会话标签各自正确。
- **已有回归：** [test_session_catalog_child_status.py:36](/Users/makiko/Projects/misaka/tests/test_session_catalog_child_status.py#L36)；[test_session_catalog_child_status.py:70](/Users/makiko/Projects/misaka/tests/test_session_catalog_child_status.py#L70)
- **边界：** Sessions 是含子代理/历史记录的目录，不是窗口计数；记录比窗口多本身并非 bug。

### F11 · LCM 长检索表达式撞上 SQLite 深度限制

**已确认产品问题 · 已有源码修复记录 · P2**

- **现象：** 多关键词 LIKE 检索生成的 SQL 表达式过深，可能触发 expression tree too large。
- **原因：** 大量 OR/加分项左结合展开，深度线性增长，触及 SQLite 默认深度限制。
- **代码点：** [search_query.py:267](/Users/makiko/Projects/misaka/misaka/extensions/misaka_lcm/vendor/search_query.py#L267)–278；[dag.py:690](/Users/makiko/Projects/misaka/misaka/extensions/misaka_lcm/vendor/dag.py#L690)–710；[store.py:1470](/Users/makiko/Projects/misaka/misaka/extensions/misaka_lcm/vendor/store.py#L1470)–1535
- **证据：** 15:17 final-repair-validation.md；message/summary LIKE 回归；343 项 vendored core/embedding 检查通过（有原有导入弃用警告）。
- **处理：** 已构建平衡 OR 和整数求和表达式；保留全部词项与排序，而非截短检索词。
- **验收：** 长查询通过且最后一个关键词仍有效；排序、作用域、取消/超时不变。
- **已有回归：** [test_lcm_search_expression_depth.py:18](/Users/makiko/Projects/misaka/tests/test_lcm_search_expression_depth.py#L18)；[test_lcm_search_expression_depth.py:54](/Users/makiko/Projects/misaka/tests/test_lcm_search_expression_depth.py#L54)
- **边界：** 现场触发的原始长查询未留存；这里是已独立复现并修补的路径，不声称复现了完全同一条用户输入。

### L01 · 只读复合 shell 被保守权限分类挡住

**限制/体验问题 · 待设计 · P2**

- **现象：** 简单 rg/cat 可执行，加 | head、分号或花括号后进入 ask，后台子代理因此绕路或等待。
- **原因：** shell 复合语法被视为路径不明确，workspace guard 先于一般授权规则。
- **代码点：** [policy.py:200](/Users/makiko/Projects/misaka/misaka/core/subagent/policy.py#L200)–232；[policy.py:635](/Users/makiko/Projects/misaka/misaka/core/subagent/policy.py#L635)–650；[policy.py:1000](/Users/makiko/Projects/misaka/misaka/core/subagent/policy.py#L1000)–1028
- **证据：** tool-probe-result.json 六组只读命令对比；17:02 C4、17:13 C17、18:23 red-team 存在对应失败。
- **处理：** 优先复用原生 read/grep；若增强分类，仅承认可证明只读的有限复合结构，不全局放开 shell。
- **验收：** 常见只读组合不无谓 ask；变量展开、写操作、外部/受保护路径仍受限制。
- **边界：** 保守分类是明确存在的能力限制；任意 Python 或 shell 需要批准不自动构成权限 bug。

### L02 · 切换 LO 供应商不等于自动复活已失败的 driver

**限制/连续性边界 · 待设计 · P1**

- **现象：** 用户担忧额度耗尽后换供应商会不会中断 Research。
- **原因：** setModel 保留同一会话，但不替换已经发出的请求；配额错误可让 required phase 失败并释放 driver。模型设置变化不会自动重新调度已失败工作流。
- **代码点：** [agent_session.py:836](/Users/makiko/Projects/misaka/misaka/core/agent_session.py#L836)–875；[workflow.py:1578](/Users/makiko/Projects/misaka/misaka/core/research/workflow.py#L1578)–1585
- **证据：** 15:19 routing 审计、隔离 quota probe 及当前源码；没有为此向真实供应商发新请求。
- **处理：** 把“模型切换”和“失败后恢复”分开；恢复仍使用原生会话与保存的 run/卡片。若做自动恢复，须有明确同 run、幂等、审批和在途请求边界。
- **验收：** 耗尽额度后保留已完成卡片、计划和会话；指定 run 恢复不重复派发；明确告知旧在途请求和已启动 Sisters 不随 LO 一起换模型。
- **边界：** 没有保证任意供应商/任意时点无缝不断；这是一条必须说明的边界，不是已观测的本次新崩溃。

### T01 · 旧面板测试可能触及真实消息库

**测试隔离缺陷 · 测试侧已有修复 · P1**

- **现象：** 最初一次旧 pane 测试可懒初始化默认 messages.db，触发超过 7 天已投递消息的清理。
- **原因：** 测试 fixture 未完全隔离 HOME/mailbox/roster/session 路径，部分 SQLite fixture 没关闭连接。
- **代码点：** [test_card_pane_reuse.py:1](/Users/makiko/Projects/misaka/tests/test_card_pane_reuse.py#L1)–80；[test_sister_steering_by_mail.py:1](/Users/makiko/Projects/misaka/tests/test_sister_steering_by_mail.py#L1)–55
- **证据：** final-repair-validation.md 的 Live-runtime boundary and test-isolation incident；实际是否删除及删除几行没有证据。
- **处理：** 已修 fixture；最终组合验证使用独立 HOME/邮箱/roster/session/agent 目录，关闭自己持有的连接。
- **验收：** 隔离组合 507 passed/49 subtests under -W error；今后产品测试不会写默认用户数据路径。
- **边界：** 这是先前测试的已披露事故风险，不是用户运行时产品 bug；本轮汇总未运行该类产品测试。

### D01 · 历史产物有登记却找不到文件

**数据异常 · 待核实归因 · P2**

- **现象：** 13:12–13:16 快照中，两个旧 run 共 7 个已登记产物路径缺失。
- **原因：** 文件缺失已确认，删除原因未定；当前目录创建时间晚于旧 run，与工作区曾被清理/重建相容，但不足以归责程序。
- **证据：** early-live-audit.md：r_1a0d3ae829、r_17bb43afd3；后者 final_artifact=a_4fc744f5d9 指向异常 ni/Users/... 路径。当前 run 的 question 文件当时存在且 hash 正确。
- **处理：** 只读核对旧 artifact 登记、工作区迁移/清理历史与备份；确定原因前不改登记或伪造恢复。
- **验收：** 逐项记录 7 条的正确位置、是否可恢复及缺失原因；恢复操作另行明确授权。
- **边界：** 不是本轮 19 张卡已丢失，也不计为已证实 MISAKA 删除文件的 bug。

## 不重复算成新 bug 的现象

- **任务跑串：未发现证据。** 15:27–15:31 抽查 9 张已启动卡、29 个原生会话（9 根+20 子）、1374 次工具调用：18 张计划与 Board assignee 对齐；54 次 todo 无跨卡标记；72 次 write/edit 在自己的卡目录；136 条产物事件无外卡目标；91 条消息无 to_task/收件方错配。只证明这一抽样范围，不声称未来或全部阶段永不跑串。同一 sis 有多个不同 session 研究不同卡是正常设计。
- **红队供应商拒答：** 18:20:19 provider rawStopReason=refusal；18:21:34 换模型后继续，之后红队卡完成。该次拒答不当作 MISAKA 自己的权限 bug。
- **WebSocket 1006：** C10/C13/C14 15:41/15:46/15:58 曾断线，后来完成。记录为外部/连接异常，不能仅凭断线归责 MISAKA。
- **已结束卡的 SendMessage 拒绝、读取超过末尾、错误路径、python 不在 PATH、远端 403/404：** 留在原始审计证据中，但不按每条报错增加产品 bug 数。
- **misaka_research_assign 慢：** 生成较长参数/等待模型本身不证明派发引擎卡死；尚无独立已证实的新根因，不加数。
- **窗口少于 Sessions：** 后者含子代理/历史会话，不是“running 记录必须各占一个窗口”；真实调度上限问题算 F02，真实状态混淆算 F10。

## 修复状态与验证边界

1. “已有源码修复记录”不等于当前所有内存中的进程都加载了补丁。F01 有修复后新进程仍为 max 的现场证据；其余不做全量热生效保证。
2. 14:21 并发 focused：125 passed。14:45 runtime smoke：185 passed / 45 subtests。15:17 组合：507 passed / 49 subtests（隔离和主树均有记录）；vendored core/embedding：343 passed，带已有弃用警告。
3. 不能把重点测试通过说成全仓库全绿：较早扩大测试曾有 2 failed / 348 passed / 2 errors / 45 subtests；另一次 research 扩大测试曾有 2 failed / 394 passed / 103 subtests。失败清单留在原始日志，未逐项归因/复验的旧测试失败不擅自提升为新的运行时 bug。
4. 早先首次 pane 测试的真实消息库风险见 T01，不能宣称那次历史测试完全只读。本次汇总仅只读检查源码/日志/数据库，新增本台账和小型证据文件；没运行产品测试、没改产品代码、没向运行中的 MISAKA 发命令。

## 留存与证据索引

- [机器可读台账](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/register.json)：编号、状态、症状、根因、验收。
- [冻结的源码定位与 SHA256](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/source-anchors.json)：防止后续改代码导致本报告行号难追踪。
- [本次只读运行快照](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/runtime-status.json)：不含任务正文。
- [session-parse-proof.json](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/session-parse-proof.json) · SHA256 `5dcad86a64279f5f21da5975ff893eba95b31fd28a345b7849adf06fe4505238` · 来源 `/private/tmp/misaka-session-parse-proof-20260920.json`。
- [tool-probe-result.json](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/tool-probe-result.json) · SHA256 `837e3260c7687e232ac10f45d3d3bda01f324cbc31b135902ca0e45dbbbcaa77` · 来源 `/private/tmp/misaka-bug-audit-20260920-1840/probe-result.json`。
- [early-live-audit.md](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/early-live-audit.md) · SHA256 `e845a92434043a5af7c1b4888acc2d88154ea244fdf6227b7fb7836015236185` · 来源 `/private/tmp/misaka-live-audit-20260920/report.md`。
- [late-live-audit.md](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/late-live-audit.md) · SHA256 `b77a87645d6a7ee089fc1f4b7b18b5c237b82c0bf0c87930468ba17e08bb0e38` · 来源 `/private/tmp/misaka-bug-audit-20260920-1840/audit-report.md`。
- [focus-audit.md](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/focus-audit.md) · SHA256 `ab07edcb59e6ff2d3b96a879b7589d000643beab15a3a6b4829f25ad1606f6cb` · 来源 `/private/tmp/misaka-focus-audit-20260920/audit-report.md`。
- [final-repair-validation.md](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/final-repair-validation.md) · SHA256 `df0d0dc4f8e79e172b9cc3b6a4e1798ecef2cd56615e96dfa0995b483dd4bb8c` · 来源 `/private/tmp/misaka-final-bugfixes-20260920/validation.md`。
- [runtime-repair-smoke.log](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/runtime-repair-smoke.log) · SHA256 `d326cfd02f4eda62871d372d76e8b48a3341aab584b479610e47980b0a228b4d` · 来源 `/private/tmp/misaka-runtime-repairs-20260920/main-smoke.log`。
- [sister-parallel-focused.log](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/sister-parallel-focused.log) · SHA256 `22daf7add2f4fda99fe9200ee93753431db5c0de28cd46b6f4d776a2495fadd5` · 来源 `/private/tmp/misaka-sister-parallel-20260920/focused-tests.log`。

未复制完整 JSONL、运行数据库或私有技能正文。旧审计摘要内的 /private/tmp 链接仅是历史溯源；本台账引用的小型摘要/证明已复制到此持久目录。

### 仍只保存在临时目录的详细原始记录

- `/private/tmp/misaka-bug-audit-20260920-1840/`：逐工具失败、外部异常、原始过滤结果。
- `/private/tmp/misaka-live-audit-20260920/`：最初现场双采样与来源验证。
- `/private/tmp/misaka-focus-audit-20260920/`：任务归属与写入路径核验。
- `/private/tmp/misaka-thinking-settings-20260920/`、`/private/tmp/misaka-sister-parallel-20260920/`、`/private/tmp/misaka-runtime-repairs-20260920/`、`/private/tmp/misaka-final-bugfixes-20260920/`：各批补丁、基线、测试与 guarded apply 证据。

上述原始材料可能含较多本地研究内容，未整包复制进仓库；需要更深调查时从原始路径取证，并校对时间与作用范围。

## 20:08 追加：LO 刚才为什么停

- 原生会话 733–736 行：19:43:31–19:44:51 有四条 model_change，供应商在 anthropic 与 sub2api-claude 之间变化。
- 737 行：19:48:35 assistant stopReason=error，errorMessage 为 `Session, branch, model or settings changed during compaction`；不是正常完成。
- 738 行：19:50:43 后来有成功的 compaction 记录，但不会因此自动重新启动已结束的模型回合。
- 739/741 行：20:07:49 用户发“干完了么”，20:08:16 后 LO 又开始 read 工具调用；LO PID 13464 仍在。
- Research driver 与聊天回合分开：driver 仍停在 19:31:10 的 InvalidSessionFileError，状态 failed、stop_requested=0。聊天继续不表示正式研究 driver 恢复。
- F08 相关的新出现记录；切模型与报错时序吻合，但旧守卫没有输出具体变化字段，暂不把这次确定为“无关记账误报”。不新增一个重复根因编号。
- [本次最小证据](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/lo-stop-followup-2008.json)。本次未重启、恢复或修改 MISAKA 运行状态。

## 20:17 追加：write 输出达到上限，保护性拦截

- 原生会话 763 行，20:17:18：sub2api-claude/claude-opus-5，stopReason=length、rawStopReason=max_tokens、usage.output=32000；write 参数在持久化记录中为空对象。
- 764 行明确该 write 未执行，避免把可能截断的正文写入文件。这不是磁盘写失败、权限拒绝或会话 JSONL 分行错误。
- 765 行在 20:18:29 有 compaction；这本身不是 write 重试成功的证据。
- 处理方向：后续由 LO 分段完整提交写入/编辑，不让截断参数绕过执行门禁；具体思考与正文分别耗用多少，本次 usage.reasoning=null，不作伪精确拆分。
- 归类为已观察的输出额度事件；拦截本身按设计工作，暂不新增一个已证实产品 bug 编号。
- [最小证据](/Users/makiko/Projects/misaka/docs/audits/misaka-issue-register-2026-09-20/lo-write-output-limit-2017.json)；本轮没有代发重试、修改运行配置或操作当前 MISAKA。

## 21:18 第二轮审计更新

新增 U07（来源被登记为自身产物/红队评语，已复现），另记 D02（计划登记漂移）、Q01（159 个 CSV 标题错误）、Q02（URL 前缀串配）。累计 **18 项已确认产品问题 = 7 待修 + 11 有源码修复记录**；其他跟踪 7 项；合计 **25 条**。上方 21 条是 19:50 的历史快照，未静默改写。

[本轮详细报告](/Users/makiko/Projects/misaka/docs/audits/misaka-recheck-2026-09-20-2110.md)。19:34 用户明确要求停止继续研究、转写全局报告，后续写报告按用户指令，不另计擅自脱离工作流。

## 2026-09-21 追加：U01 / U02 已修到源码

直接替换错误逻辑，没有新增向后兼容开关、备用解析器或旧逻辑回退。

- **U01**：四处 JSONL 读取统一以 LF 为记录边界；严格损坏检测保留。原故障文件离线由报错变为正常读取 890 条记录。
- **U02**：二进制保留文件节点，只有有效文本 Markdown 展开标题；单产物扫描、标题数量和标题长度有界。原 PNG 从 7 个伪标题变成 0。
- 隔离副本与应用补丁后的主目录，各 **313 passed（warnings as errors）**；当前运行未重启、未恢复、未改会话/数据库。
- 状态是“源码已修”，不等于旧内存进程已加载新代码。旧时间快照仍保留，上文历史定位由冻结证据溯源。
- 当前累计 **18 项产品问题 = 5 待修（U03–U07） + 13 有源码修复记录**；其他跟踪 7 项；总计仍为 25 条。

[结构、修复点、边界和完整验证记录](/Users/makiko/Projects/misaka/docs/audits/misaka-u01-u02-repair-2026-09-21.md)。

## 2026-09-21 追加：U05 / U06 已修到源码

- **U05**：原生流式消息单独同步至 Sessions 活会话旁观页，不把半成品写入历史；完成、断连、退出清理临时展示。
- **U06**：WindowLO 统一管理三个入口的研究描述，区分整个研究与当前节点状态，按回调身份恢复；输入和所有权检查不变。
- 隔离副本及合入主目录各 **520 passed（warnings as errors）**。扩大组合中的旧 SQLite 测试连接泄露已在修复前后动态复现，未屏蔽警告，详情见报告。
- 当前运行未重启、未热更新；新快照字段需 owner 和旁观页都加载新源码。
- 累计 **18 项产品问题 = 3 待修（U03/U04/U07） + 15 有源码修复记录**；其他跟踪 7 项，总计仍为 25 条。

[修复、验证与边界记录](/Users/makiko/Projects/misaka/docs/audits/misaka-u05-u06-repair-2026-09-21.md)。

## 2026-09-21 追加：U03 已修；U04 候选待历史策略确认

- **U03**：仅修宿主结果分类；全失败进入工具错误通道，部分成功保留正文、逐页错误及资料路径。Hermes 抽取算法未变。隔离 U03 独立组合及主目录复验均 **2378 passed, 1 skipped，-W error**。
- **U04**：Pi 对齐候选已实现并通过组合 **3580 passed, 1 skipped**，但**未合入**。已复现删除 `argumentsError` 后旧历史校验失败，正在等待用户选择历史接入策略；新协议恢复测试通过不代表旧历史兼容。
- 不重启、不恢复当前 MISAKA，不修改真实会话或研究数据库。
- 当前 **18 项产品问题 = 16 有源码修复记录 + 2 待修（U04/U07）**；其他 7 项，合计 25 条。

[修复及未合入边界](/Users/makiko/Projects/misaka/docs/audits/misaka-u03-u04-repair-2026-09-21.md)。

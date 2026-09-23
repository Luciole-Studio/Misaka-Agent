# U05 / U06 源码修复 · 2026-09-21

## 结果与范围

U05、U06 已修到当前工作树。6 个产品文件、9 个测试文件（新增 3 个 / 25 项回归，其余为受影响接口的 fixture 适配）。
不保留旧状态字段或备用实现；未修改模型请求、工具执行、研究调度、输入路由、暂停恢复或 LCM 实现。

隔离副本及合入后的主目录，各 **520 passed，warnings as errors**。这是明确列出的 28 个文件组合，不是全仓库全绿声明。
首次扩大到 30 个文件时有旧测试连接泄露；已对照原始源码动态复现，见下方。

**当前运行未重启、未恢复或热更新，真实会话、研究数据库和用户成果未修改。**
新增快照字段要求 owner 和旁观页均加载本次源码；旧 owner 进程不会因磁盘源码变化自动获得新协议。未做当前研究的运行态验收。

## U05：传递原生流式状态，不创建第二份消息历史

[session_control.py:65](/Users/makiko/Projects/misaka/misaka/core/session_control.py#L65)
和 [session_control.py:155](/Users/makiko/Projects/misaka/misaka/core/session_control.py#L155)：

- 直接读取 `session.state.streamingMessage`，只展示 assistant 消息。
- 原有 `cursor/entries` 表示已提交的当前分支历史；新增独立 `stream_cursor/streaming`。
- 订阅原生消息事件只递增版本，不额外缓存、拼接或持久化正文；Control 关闭时退订。
- 历史未变时 `entries=None`，不随每个流式更新重传历史；当前消息未变时也不重传载荷。
- 新流式游标配 `streaming=None` 表示明确清空；相同游标配 None 表示没有更新。
- 流式游标包含会话及历史游标，历史切换或重建后会重新同步临时展示；原 owner/instance 栅栏保留。

[chat.py:189](/Users/makiko/Projects/misaka/misaka/cli/chat.py#L189)
和 [conversation.py:132](/Users/makiko/Projects/misaka/misaka/ui/tui/interactive/conversation.py#L132)：

- 沿用现有轮询与 `AssistantMessageComponent`；原地更新临时组件，而不是把半条消息塞进 entries。
- 正式历史更新时先移除临时组件，随后渲染正式消息及仍在生成的预览，避免重复。
- 断连或退出清理临时组件；重连重新获取当前预览，不自动发送草稿/输入或接管新 owner。
- 保留终端控制字符净化和思考块显示设置；纯保存历史的读取方式不变。

## U06：统一绑定和恢复研究描述

[node_description / WindowLO](/Users/makiko/Projects/misaka/misaka/core/research/window.py#L19)：

- 一个描述器统一读取现有 run/node：`run_id`、`node`、`depth`、`run_phase`、`run_status`、`node_phase`。
- 删除含义模糊的旧 `phase` 字段。根节点 closed 与整个研究 finalizing 可以同时正确显示。
- `WindowLO` 必需接收 `describe` 回调，复用原有 ExitStack 安装/恢复；WindowLO 不新增数据库所有权。
- 仅当当前回调仍是自己安装的回调时恢复，保留后来接管者的回调。
- 正常结束、异常、取消、清理异常、重复 close 均覆盖；描述恢复先于研究连接关闭。

三个入口共用：
- [workflow.py](/Users/makiko/Projects/misaka/misaka/core/research/workflow.py#L1483)：现有聊天前台根，绑定持续至分支波次和最终报告阶段。
- [window.py](/Users/makiko/Projects/misaka/misaka/core/research/window.py#L273)：命令行根 / 后台分支，移除独立绑定。
- [wiring/node.py](/Users/makiko/Projects/misaka/misaka/core/research/wiring/node.py#L95)：交互分支，移除重复 describe 安装/恢复。

`on_input`、`check_active`、runner ownership、driver 锁、任务派发和暂停恢复行为不变；只收拢状态描述生命周期。

## 回归证据

- U05 最初快照回归：旧实现 6 failed → 修复 6 passed；随后补入真实 SDK/AgentSession 的离线流式集成测试，最终 7 项通过。
- U05 实际 `follow_session` + 实际 Conversation：旧实现 2 failed → 修复 2 passed；覆盖思考/正文、去重、断连重连、草稿不代发、终端净化和新研究状态标题。
- U06 最终同一份新增测试：原始三源码 overlay 16 failed → 修复 16 passed。另含既有 7 项节点清理回归。
- [28 文件组合清单](/Users/makiko/Projects/misaka/docs/audits/misaka-u05-u06-repair-2026-09-21/focused-files.txt)：覆盖 Sessions、研究根/分支/恢复、提示词组装、并发派发、Board 边界、压缩、Sister 消息、窗口复用、LCM、原生子代理和模型路由。
- [隔离组合](/Users/makiko/Projects/misaka/docs/audits/misaka-u05-u06-repair-2026-09-21/focused.log)：520 passed in 49.58s。
- [主目录复验](/Users/makiko/Projects/misaka/docs/audits/misaka-u05-u06-repair-2026-09-21/main-final.log)：520 passed in 33.80s。
- 范围内 Ruff、AST 解析和 diff 空白检查通过。独立只读审查未发现阻塞问题，不等于绝对零风险保证。

### 扩大回归中的旧测试泄露：未隐藏，也未混进本次修复

首次 30 文件组合结果为 **533 passed / 3 failed / 3 errors**；所有失败/错误均为未关闭 SQLite 连接触发的 ResourceWarning，并非业务断言失败。

独立原始源码 overlay 与修复源码使用相同两份原样测试，并在每例后主动 GC、启用分配栈追踪：两边均为 **19 passed / 15 teardown errors**，错误用例集合完全一致。

已证实的分配点：
- `tests/test_research_mode_notice.py:22`：board fixture 返回连接但不关闭。
- `tests/test_research_mode_notice.py:41`：独立 plain.db 连接不关闭。
- `tests/test_sister_notification_wakes_lo.py:16`：board fixture 返回连接但不关闭。

上述两测试及 `tasks.py` 的哈希均与本轮修复前相同；没有修改它们，也没有屏蔽警告。
最终 520 项组合明确排除这两个泄露 fixture 文件，其余原组合保持；这不应描述为所有测试全绿。

[对照归因](/Users/makiko/Projects/misaka/docs/audits/misaka-u05-u06-repair-2026-09-21/leak-attribution.md)
· [机器对照](/Users/makiko/Projects/misaka/docs/audits/misaka-u05-u06-repair-2026-09-21/leak-comparison.json)
· 原始完整日志保留在同目录。

## 隔离、合入和台账

测试使用独立 HOME/MISAKA/XDG/临时目录；新增原生流测试替换模型流并禁止外部网络，其他原生模型回归使用本地模拟服务，不调用真实供应商。

保存脏树基线后只应用本次补丁；apply 前检查目标 SHA 与 git apply --check，合入后 15 文件与受测副本逐字节一致，其他基线文件及 Git index 未变。未暂存、提交或推送。

[精确补丁](/Users/makiko/Projects/misaka/docs/audits/misaka-u05-u06-repair-2026-09-21/repair.patch)
· [应用证明](/Users/makiko/Projects/misaka/docs/audits/misaka-u05-u06-repair-2026-09-21/apply-proof.json)
· [定位与哈希](/Users/makiko/Projects/misaka/docs/audits/misaka-u05-u06-repair-2026-09-21/manifest.json)。

台账 U05/U06 标记“已有源码修复记录”，当前 **18 项产品问题 = 15 项源码已修 + 3 项待修（U03/U04/U07）**，其他跟踪 7 项，总计仍为 25 条。

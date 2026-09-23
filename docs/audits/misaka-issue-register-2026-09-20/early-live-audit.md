# MISAKA 实时只读审计 · 2026-09-20

观测窗口：13:12–13:16 JST（UTC+09:00）。这是当时快照，不是持续监控。

## 结论

当前 Research 在持续运行，未发现本轮崩溃、卡死、推理档位回退、数据库损坏或 LCM 丢记录。确认两个实时旁观/诊断显示缺口，以及一组原因未定的历史产物缺失；不把它们误称为当前研究执行失败。

## 实时状态与证据边界

- 主程序 PID 13462、daemon 13463、LO 13464；启动于 13:10:51–52。
- 实际工作区 `/Users/makiko/Documents/exam`，源码来自 editable 安装 `/Users/makiko/Projects/misaka`。
- 会话 `01a0bd02-7fde-7c0c-aaed-0aecfa828c3d`，研究 `r_b55f2c1cf5`，根节点 `b_3d6b51e2c9`。
- 13:15:56 进程自身 SessionControl 回应：`working`、`paused=false`、`error=""`、待处理消息 0、档位记录 `max`。
- 13:14:39 → 13:15:04 原窗口文本 23,693 → 25,967 字符（+2,274），内容 hash 改变，底栏均为 `claude-fable-5 • max`。字符数是终端提取文本总量，不是 token 计数或纯思考量。
- Board：active/active，根 planning，driver PID 与进程一致，租约续到 13:19:32。尚无已接受 Research action / Sister 卡片；规划尚未提交，不能据此判断派发失败。
- 上次 thinking 修复四个源码文件于 13:08:36 修改，早于当前进程启动；未发现启动后修改过 Python 源码。实时档位与修复吻合，但没有执行完整 Python 堆转储或逐字节核对所有已加载函数。

## F1 · 已确认 · Sessions 旁观窗口遗漏生成中的内容

**现场：** 原窗口持续输出，两个内存 snapshot 的 cursor 都是 `[8b441192, 8]`，没有正在生成的 assistant。原生 JSONL 的 9 行含 session header，因此对应 8 个 context entries，计数本身没有矛盾。

**链路：**
- [session_control.py:145–152](/Users/makiko/Projects/misaka/misaka/core/session_control.py#L145)：snapshot 只有 SessionManager entries，cursor 仅 leaf/count。
- [agent_session.py:2080–2081](/Users/makiko/Projects/misaka/misaka/core/agent_session.py#L2080)：message_end 才持久化完整消息。
- [chat.py:265–283](/Users/makiko/Projects/misaka/misaka/cli/chat.py#L265)：Sessions 旁观界面仅轮询 snapshot entries。

**影响：** 原窗正在输出时，旁观窗口可长时间没有新内容，看起来像卡住；这是旁观显示缺口，不是实际停止。没有打开新的旁观窗口，以免影响现场；结论来自实际接口响应与消费者代码的交叉验证。

**修法方向：** 复用现有运行中消息状态，单独暴露临时流式视图及更新标识，不提前写入半条消息、不改变原生 transcript/LCM 持久化规则。

## F2 · 已确认 · 前台 Research 根节点未发布实时阶段描述

**现场：** 正在规划的根 LO 返回 `workflow={}`。

**链路：**
- [session_control.py:58](/Users/makiko/Projects/misaka/misaka/core/session_control.py#L58)：describe 默认为空字典。
- [workflow.py:1477–1483](/Users/makiko/Projects/misaka/misaka/core/research/workflow.py#L1477)：现有聊天根节点只创建 WindowLO。
- [window.py:256–259](/Users/makiko/Projects/misaka/misaka/core/research/window.py#L256)：headless 路径才绑定 node/depth/phase 描述。
- [chat.py:269–272](/Users/makiko/Projects/misaka/misaka/cli/chat.py#L269)：旁观窗口依赖这个字段显示阶段与深度。

**影响：** 前台根 LO 的旁观/诊断接口缺阶段、深度信息；后台路径却能显示。研究本身仍正常运行。

**修法方向：** 两条路径共用根/节点描述绑定，并在研究结束后恢复普通会话描述；无需新建另一套 Research 会话。

## D1 · 已确认的数据异常 · 历史产物登记悬空，原因未定

- 当前研究 question 文件存在，SHA256 与 Board 登记一致。
- 历史 `r_1a0d3ae829` 和 `r_17bb43afd3` 的 7 个登记产物路径缺失；包括后者 `final_artifact=a_4fc744f5d9` 对应的 `ni/Users/makiko/Documents/exam/final/r_17bb43afd3-partial.md`。
- 当前 final/nodes/.git 的创建时间为 13:11:12，晚于历史运行，符合工作区曾清理或重建的情况；证据不指明是谁删除，也不证明 MISAKA 误删。
- 影响旧报告打开/旧研究恢复；尚无证据显示损坏了本次运行。没有恢复或修改历史数据。

## 已排除的误判与日志结果

- Board 与项目 LCM `PRAGMA quick_check=ok`。
- LCM 当前 5 条记录与 JSONL 第 5–9 行 custom_message 逐条一致；重复 0、FTS 5、无 assistant 完成记录。正在流式生成而尚未落盘不等于丢消息。
- 当前未见思考/签名误入 LCM 索引，亦无压缩 checkpoint 或 embedding inflight。
- 当前进程已打开的 `~/.misaka/agent/misaka.log`、`~/.misaka/net.sock.log` 和当前 mat-debug 日志为 0 字节。应用日志只记录 WARNING 及以上，空日志不代表模型没有输出。
- `panel-crash.log` 最后修改为 09-18 10:32:01；`misaka-warnings.log` 中 Executor shutdown 也是 09-18 的旧记录，不作为本次错误。
- 历史 `failed / active` 保留失败前工作阶段；单独的 Operation aborted 不证明崩溃。另一个历史 run 接受计划后没有 start action，默认等待批准，不能据此报派发故障。

## 方法和未覆盖范围

只使用 OS 进程/打开文件信息、经过源码核实的只读 AF_UNIX 请求（ping/panes.status/pane.extract/session snapshot），以及 SQLite URI `mode=ro`、`PRAGMA query_only=ON` 的短连接查询。

未修改项目代码、配置、运行数据库或会话；未发输入、暂停、恢复、停止、重启或模型请求。未注入调试器。当前还未进入 Sister 执行、分支、审核、最终报告阶段，本次结果不为这些后续阶段背书。原始 provider 请求/响应流未抓取，界面 max 和 session 记录不等于对实际出站包逐字段抓包验证。

## 机器可读证据

- [实时两次采样](/private/tmp/misaka-live-audit-20260920/readonly-runtime-samples.json)
- [最终实时快照](/private/tmp/misaka-live-audit-20260920/final-runtime-state.json)
- [代码来源证据](/private/tmp/misaka-live-audit-20260920/runtime-source-provenance.json)
- [持久会话元数据](/private/tmp/misaka-live-audit-20260920/transcript-metadata.json)

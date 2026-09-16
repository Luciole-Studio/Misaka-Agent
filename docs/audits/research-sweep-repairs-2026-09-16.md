# Research 第二轮修复与验收

日期：2026-09-16。修复范围：[第二轮排查的 RF-01～RF-06](research-sweep-2026-09-16.md)。

## 实现

| 问题 | 修复落点 | 保留的行为 / 验收要点 |
|---|---|---|
| RF-01 Office 漏接 skill 守卫 | `tools/office.py::prepare_office_input`；`skills/wiring/skills.py::guard_live_skills` | 执行和守卫共用解析器及已有 `output_paths/resolve_ops`；主输出、副输出、符号链接均检查。通过 `updatedInput` 传递已解析 ops，后续不再读另一版 @ops 文件。普通 Office 写入保持可用。 |
| RF-02 旧 owner 推进工作流 | `research/runs.py::check_owner/owned_txn/set_node/acquire_driver`；`workflow.py`、`context.py`、`report.py` | driver/node 身份与阶段许可分开；状态、派卡、依赖、上下文及报告发布在写事务内校验原身份。重新读取状态不接收新身份。接管 driver 的事务同时作废旧 node key，保留 PID/identity 给原有清理流程。root 已 closed 后的最终评审仍可派发。 |
| RF-03 取消后残留 Sister | `workflow.py::_expand/_drive_tasks/_halt_scope`；`research/node.py::PaneRunner.stop`；`ui/panel/daemon.py::card.stop` | 根、子节点及最终评审共用停止/结算路径，覆盖派卡后尚未进入 drive 的取消。清理等待重复取消结束。停止请求携带 generation、claim、driver/node 身份，接收端重新校验；新 owner 的卡不被旧请求关闭。面板返回进程身份，调用者等待原进程结束。 |
| RF-04 文件与 DB 发布分裂 | `research/runs.py::write_text/recover_publications`；`research/publication.py`；`platform/tasks.py` 的事务完成回调 | 先取得 writer，再发布；临时同目录 journal 保留旧字节，外层提交/回滚后清理或恢复。保存点失败只回滚本次尝试，保留外层此前的写入。进程中断后由写入口或 resume 按数据库摘要恢复；只读查询不执行恢复。复用现有 atomic 文件写入，不更改产物路径，不往 DB 存放研究正文。 |
| RF-05 接受后附件被重新认定 | `network/dispatch.py::prepare_submission/accept_state`；`network/todo.py`；`workflow.py::_register_task_artifacts` | 附件摘要在短提交事务前计算，随 generation/claim CAS 冻结；交互会话把摘要计算放到线程。忽略调用者伪造的摘要。登记时内容变化、删除或移出 workspace 明确报错，事务不留下部分登记/已结算标记。旧提交缺摘要时明确标记 `submission_digest_verified=false`，不伪称历史已验证。 |
| RF-06 Responses 恢复调用丢失 | `ai/providers/openai_responses_shared.py` | 按调用 ID 查找或追加到最终消息；结束事件使用该调用的真实位置，不借用其他块的参数。验收包含最终消息序列化往返及实际工具调度，而不只检查事件。 |

## 测试与边界

正式回归文件：`tests/test_research_sweep_repairs.py`，共 **48 项**。原诊断入口 `scripts/audit/test_research_faults_20260916.py` 转为加载同一组用例，避免维护两份断言。

新增覆盖包括：

- driver/node 接管、模型线程返回时接管、两张派卡之间接管、接管时作废延迟 spawn key、closed root 的最终评审正常路径；
- Office 主输出 / PDF 副输出、符号链接、@ops 在检查后变化、实际普通文件写入；
- 面板根取消、正常 stop、重复取消、派卡后取消、接收停止命令时 owner/generation/claim 已变化；
- 两个独立 WAL 连接的 writer 锁冲突、提交失败、外层回滚、SQL 回滚、关闭连接回滚、原生连接上下文提交/回滚；
- 同一路径多次写入、内层发布失败被捕获后外层提交或回滚；调用者自建 sqlite 连接使用共享事务入口；
- **真实子进程 `os._exit`**：文件替换后但 DB 未提交，以及 DB 已提交但 journal 未清理，两种恢复结果；
- 只读 SQLite 查询不恢复/改写文件；附件被修改、删除、路径移出工作区、伪造摘要及无摘要旧格式；
- Responses 正常事件序列和缺 added 的恢复分支，最终消息、事件索引、历史序列化和工具执行一致。

### 对上一轮诊断的更正

1. owner 用例原来在检查状态后，错误地断言“产物列表为空”；fixture 创建 run 时本来就有 question 产物。改为比较执行前后的完整产物行，确保原文件不被改写、没有额外发布。原来的“旧 owner 改阶段”失败仍是有效证据。
2. 修改后的附件登记选择明确抛出完整性错误，而不是默默跳过。正式用例明确断言该错误、没有登记伪造版本、没有 `research_v2_settled` 标记。这是加强失败契约，不是删除正确性断言。

### 执行命令

```sh
# 在仓库根目录；使用临时 HOME。
AUDIT_HOME=$(mktemp -d)
HOME="$AUDIT_HOME" PYTHONPATH=. .venv/bin/python -W error -m pytest -q \
  -o asyncio_default_fixture_loop_scope=function tests/test_research_sweep_repairs.py

HOME="$AUDIT_HOME" PYTHONPATH=. .venv/bin/python -W error -m pytest -q -rs \
  -o asyncio_default_fixture_loop_scope=function
```

最终执行数字与合回核对记录见本文末尾。所有失败诊断均加入默认 `tests/`，不以 xfail 隐藏。

## 明确未宣称的事项

- 这轮修复的是确认的六类边界问题，没有重新进行上游源码归属比对，也没有宣称产品级一比一对齐或零 bug。
- 真实模型完整 Research、真实面板多进程长时运行、浏览器 live fixture 未执行。面板 IPC 使用样本；数据库、文件和发布中断子进程使用真实实现。
- 进程中断恢复测试不是硬件断电或磁盘损坏证明。journal 损坏、外部改写或磁盘 I/O 故障会留下明确恢复错误，而不是悄悄认定完整性通过。
- 原生 Board 连接支持直接 commit/rollback、连接上下文及关闭；外部自建 sqlite 连接的多步骤写入应使用已有 `tasks.write_txn`。未受管理的外部 BEGIN 在改文件前被明确拒绝，不承诺替外部连接拦截它未来的 rollback。

## 工作区保护

在 `/tmp/misaka-sweep-fix-20260916-nHylH4/worktree` 隔离修改，基于开始时的完整 dirty snapshot；未重置、提交或覆盖其他会话的 skill 命令相关修改。
未操作用户正在运行的 Research、真实面板、Board 数据或个人 Hermes skills。
证据目录保留 baseline SHA、隔离测试日志、增量补丁及合回逐文件核对。


## 最终验收记录

| 项目 | 结果 |
|---|---|
| 新增正式回归，连续三次 | 每次 **48 passed**（0.78s / 0.67s / 1.24s） |
| 原诊断命令入口 | **48 passed**，0.66s |
| 最终隔离快照完整测试，`-W error` | **2365 passed、1 skipped、144 subtests passed**，49.78s |
| 合回后的主目录完整测试，`-W error` | **2365 passed、1 skipped、144 subtests passed**，49.75s |
| 本轮涉及 Python 文件 Ruff / 编译 | 通过 |
| `git diff --check` | 通过 |
| SHA 防覆盖与合回比对 | 17 个涉及文件逐字节一致；其余起始基线文件未改变 |

唯一跳过项：`tests/web/test_vault_browser_live.py:23`，需要显式配置一次性浏览器 fixture 可执行文件。
本轮开始时已有 2317 个默认用例（包含并行会话的 skill 命令相关测试），新增 48 个后为 2365；不把并行会话的覆盖计入本轮贡献。

中间完整测试发现过新连接回调使重复 `close()` 报错，已经修回幂等，原有资源清理用例和最终完整测试均通过。
嵌套发布测试还覆盖了“内层失败被捕获、外层最终回滚”，确保恢复 journal 不会被提前清理。

**状态：RF-01～RF-06 已修复、已合回，离线回归验收通过；真实模型 / 真实面板端到端验收未执行。**

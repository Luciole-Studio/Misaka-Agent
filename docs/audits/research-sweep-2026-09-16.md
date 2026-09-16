# 第二轮 Bug 排查：未修复问题与可执行证据

后续状态：六类问题的修复与正式回归见 [第二轮修复验收](research-sweep-repairs-2026-09-16.md)。下文保留发现时的未修复状态与证据；诊断中的初始产物断言更正见后续报告。

日期：2026-09-16。基线 HEAD：`d346db6b9820a1f779e693250c84aea6d69da23d`，叠加上一轮已落地修改。

## 结论

**确认 6 类残留问题。此次没有修改生产代码，没有操作现有 Research、Board 数据或真实面板，没有读取/修改个人 Hermes skills。**

这不是“六个静态猜测”：15 个故障/正常对照用例中，8 个正确性断言失败、7 个正常对照通过；在第一份快照上连续运行三次，结果一致。
之后检测到并行会话的 skill 命令命名修改，保留它们并创建新的冻结快照，重新验证，避免拿旧版本结论冒充当前版本。

上一轮的工具命令所有权检查、参数校验、来源包保护等修改仍然有效，但**跨入口覆盖有遗漏**。
以下根因在修复前的本地 HEAD 也有相应缺口；没有证据把它们说成上一轮新引入的回归。

## 覆盖范围

- 全仓清点及 AST：710 个非 assets/vendor Python 文件，231,628 行，语法错误 0；这是自动扫描规模，不是宣称逐行语义验收了 23 万行。
- 全仓定向静态扫描：未定义名称、闭包捕获、可变缺省、异常链等规则；25 条候选，经人工分流，不直接当作 25 个运行 bug。
- 深查：Research 阶段推进/审批/派卡/停止、Board 接受与 Research 登记、文件发布、Skills 通用写入守卫、Office 执行路径、Responses 最终消息、会话清理、Web HTTP 池与浏览器生命周期。
- 真实数据库与真实文件 I/O 用于复现；模型、面板 IPC、进程存活、skill 根目录发现使用临时样本边界。
- 未做：真实模型完整 Research 实跑、真实面板多进程长时压测、真实浏览器样本测试、全部 TUI/子代理/文档算法的逐路径证明、上游仓库重新比对。

## 确认问题

### RF-01 / P1：Office 漏接 live-skill 写入保护

位置：`misaka/core/skills/wiring/skills.py:235-246`，`misaka/core/tools/office.py::execute`。

- 守卫只识别 write/edit/bash/powershell；office 可以创建/覆盖 Markdown，判定却直接返回不拦截。
- 用临时 profile/skills/fixture/SKILL.md：write 被拦且内容不变；同样目的地的 office 实际写入成功，内容从 before 变为 after，未经过 skill_manage。
- 这违反当前代码声明的“live skill 只经管理入口审批、扫描、登记而变更”。是 MISAKA 跨模块治理遗漏，不是 Office 正常写文件功能本身有错。
- 本测试没有使用用户的真实 skill 路径；不据此推断个人 skills 已遭改动。

修复方向：把所有写入入口接入共享的受保护目的地判定；Office 复用已有 output_paths/resolve_ops，覆盖主文件、PDF 副输出和 @ops。授权判定和执行使用同一份已解析参数，不只加一个 office 名称判断就结束。

### RF-02 / P1：工具命令受 owner 限制，工作流直接写入仍可越过限制

位置：`misaka/core/research/workflow.py:221-244,630-674`。

- `_expand` 每圈读取最新 node 覆盖传入快照，随后直接投影计划文件、修改阶段；没有用最初捕获的 node/driver 身份保护这些写入。
- `_submit_tasks` 每次只检查 stop_requested；接替 driver 后没有停止旧派卡循环。
- 复现 1：替换 driver 或 node key，旧调用仍把接替者的 planning 改成 waiting_input，并发布计划产物。
- 复现 2：第一张卡创建后替换 driver，旧调用仍创建第二张卡。不是只有界面显示错了。
- 原始 owner 不变的两个正常对照均通过。

修复方向：运行身份应始终是最初的 driver/node key，最新状态只能用于观察；所有阶段/派卡持久化共用“验证原 owner + 条件写入”的事务约束。最终评审允许 root 已 closed，故应区分执行身份校验和阶段许可，不机械地把现有 `_owned` 塞进所有调用。

### RF-03 / P1：取消面板根研究任务时，仍运行的 Sister 没收到停止

位置：`misaka/core/research/workflow.py:1420-1446`；`misaka/core/research/node.py::PaneRunner`；`misaka/core/research/wiring/research.py::cleanup`。

- 正常 stop 走 `_drive_tasks` 的 `_stop_active`，会发送 card.stop。
- 会话清理实际采用 request_stop + task.cancel。CancelledError 分支只做 reconcile/停止未启动任务，没有停止当前面板 Sister；finally 只对具备 close 的 runner 清理，而 PaneRunner 没有 close。
- 完整 `workflow.run` 复现：真实 Board 行、真实 PaneRunner 与 reconcile，IPC/活进程探测用样本；正常 stop 发出 card.stop，取消分支没有发出，Sister 行仍 running。
- 样本使用有效存活 claim，真实 reconcile 正确保留它；“回收死 worker”不等于“停止活 worker”。面板关闭单个 LO 也不是关闭其他独立 Sister pane 的同义词。

修复方向：根/节点退出都应完成各自拥有的 card 的停止与结算，再释放 driver；复用现有停止逻辑，带实际任务 generation/owner 身份并等待完成。旧 owner 的取消尤其不得杀掉接替者的新卡。

### RF-04 / P2：文件已替换、DB 登记失败，旧产物与摘要不一致

位置：`misaka/core/research/runs.py:1066-1097`，共享入口 `write_text`。

- 当前顺序是先原子替换文件，再读/写 research_artifacts；文件系统原子替换没有使数据库更新也成为原子操作。
- 用两个独立 WAL 连接：一个持有 writer transaction，另一个发布文件。数据库更新报 locked，但原文件已被 after 覆盖，登记 SHA 仍对应 before。
- 无锁正常对照一致；失败对照违反“发布失败保留先前有效版本”的断言。按登记摘要读取该文件会出现完整性错误。
- 这是公共发布入口的文件/DB 一致性缺口；此用例没有证明每一种 resume 都永久失败，也没有把故障注入等同于已经发生的线上事故。

修复方向：先暂存字节与摘要，在取得数据库 writer/owner 条件后执行可恢复发布；普通 DB 提交失败应恢复原版本或留下明确待完成记录。区分普通错误回滚和进程崩溃恢复，不用扩大 busy_timeout 或吞异常掩盖文件/登记分裂。

### RF-05 / P2：提交接受至 Research 登记之间的附件变化没有被识别

位置：`misaka/core/network/dispatch.py:319-323,362-369`；`misaka/core/research/workflow.py:442-456`。

- submitted 事件冻结了内容声明和路径列表，没有附件摘要。
- `settle_done_tasks` 稍后从磁盘读附件并重新计算 SHA，作为该代已接受产物登记。
- 复现：通过真实 accept_state 接受文件，登记前改变临时文件字节；登记成功，保存的是变化后的 SHA。文件未变的正常对照通过。
- 上一轮来源包的 SHA 修复只保护“已有冻结 SHA 之后”；这个更早的窗口没有可比较的接受时摘要。
- 这是条件触发的证据一致性缺口，不是说所有已提交研究材料都被改过。

修复方向：提交时准备有摘要的附件清单，随 ownership CAS 一并记录；后续登记只能核对，不把新内容重新命名为旧接受版本。摘要计算放在短写事务外。历史没有摘要的提交须明确按旧格式处理，不把事后算出的值冒充提交时摘要。

### RF-06 / P2（异常流兼容分支）：Responses 恢复的调用没有进入最终消息

位置：`misaka/ai/providers/openai_responses_shared.py:645-657`。

- 收到 function_call 的 output_item.done、此前没有 added/current block 时，代码显式构造 ToolCall 并发出 toolcall_end，却没有追加到 output.content。
- 复现：正常 added + done 路径有调用；仅 done 的既有恢复分支发出结束事件，最终消息 content 仍为空，调用消失。
- 上一轮确实测过“有/无 start 的最终参数验证”，但只断言事件里的参数，漏断言最终消息。这是测试覆盖缺口。
- 此项只确认防御性恢复分支缺陷；没有声称标准远程服务经常省略 added。其上游是否同错，本轮未重新核对。

修复方向：恢复的新 ToolCall 先进入最终消息，再以正确位置发事件；回归同时检查事件、最终消息、实际调度与历史往返，不只查看 toolcall_end。

## 验证与复现

新增诊断脚本：[test_research_faults_20260916.py](../../scripts/audit/test_research_faults_20260916.py)。
**脚本断言的是修好后应满足的契约，目前应报失败。它不在默认 tests/ 发现范围，未把红色用例伪装成通过或 xfail。**

```sh
# 从仓库根目录运行；不会使用真实 Research/面板/个人 skill 数据。
AUDIT_HOME=$(mktemp -d)
HOME="$AUDIT_HOME" PYTHONPATH=. .venv/bin/python -W error -m pytest -q \
  -c pyproject.toml -o asyncio_default_fixture_loop_scope=function \
  scripts/audit/test_research_faults_20260916.py
```

| 检查 | 结果 |
|---|---|
| 故障注入＋正常对照，三次重复 | 每次 8 failed、7 passed；8 个失败归并为上述 6 个根因 |
| 含并行修改的冻结快照，诊断重跑 | 8 failed、7 passed，1.46s；确认问题仍存在 |
| 主目录现有完整测试 | 2275 passed、1 skipped、144 subtests passed，48.16s |
| 含并行修改的冻结快照，现有完整测试 | 2275 passed、1 skipped、144 subtests passed，50.37s |
| 新诊断脚本 Ruff | 通过 |
| AST 扫描 | 710 个文件、0 syntax errors |

唯一默认测试跳过项仍是显式浏览器样本可执行文件未配置。2275 比上一轮 2272 多的 3 项来自并行会话新增的 skill 命令命名测试，不冒充本轮新增覆盖。
**既有完整测试仍绿、新边界断言稳定失败，说明旧测试没有覆盖这些失败条件；不是本轮修复验收通过。**

## 未升级为确认运行 bug 的静态候选

- 15 条 B023：涉及当前循环内同步执行的 walk/sort 回调，没有据此认定存在延迟闭包错页。
- 5 条 F821：CaptionEntry/StyleCluster 的延迟类型注解缺少名称导入；目前没有发现正常执行路径解析这些注解并失败，不把它们冒充生产 NameError。
- 5 条 B904：异常链写法问题，未从告警推出运行失败。

## 工作区保护与证据

本轮新增的仓库文件只有本报告与显式诊断脚本；没有修改上一轮生产代码、回归测试、报告，也没有覆盖并行会话修改。
审计期间观测到的并行变更：`misaka/core/skills/wiring/skills.py` 的命令命名逻辑，以及 `tests/test_skill_command_names.py`。

证据目录：`/tmp/misaka-sweep-20260916-YAMEom/`。
保存了 baseline.json、inventory.json、static-candidates.json、parallel-changes.json、snapshot.patch、snapshot-hashes.json、重复失败日志/JUnit XML、冻结快照及完整测试日志。
`snapshot/` 是保留并行改动后的隔离快照；旧的上一轮修复工作树没有被改写。临时目录不是长期档案，诊断代码和核心结论已留在仓库。

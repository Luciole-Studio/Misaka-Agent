# Research 缺陷修复与验收

日期：2026-09-16。生产基线：`d346db6b9820a1f779e693250c84aea6d69da23d`。

## 结论与边界

按用户确认的复核结论处理原清单 18 项：修复可复现错误，对策略、派生产物及低优先级输入边界只做窄修，不把它们全部改造成新功能。
本轮为自动化代码验收；未重新运行付费模型驱动的真实 Research，也未重启、停止、恢复或修改现有运行状态。
没有读取、迁移、上传或改动用户正在使用的 Hermes skills。

生产修改共 20 个 Python 文件；新增 3 个测试文件，114 个测试用例。没有新增依赖、数据库迁移或修改上游 vendor。
上游归属沿用本轮之前的固定版本差分结果，见 [上游核查](research-bugs-upstream-2026-09-15.md)；本报告不声称全产品一比一对齐。

## 原清单逐项处置

| 编号 | 模块与问题 | 本轮实现与保留的契约 |
|---|---|---|
| 1 | Skills shell 守卫误报 | 仅放行简单 Bash `printf`/`echo` 中可明确辨认的纯字面量；真实动态语法、嵌套解释器及受保护路径仍受限。PowerShell 保持原保守策略；提示说明“目标不可验证”，不再一律断言访问了 skill。 |
| 2 | AI 最终参数退化后仍执行 | 增量预览仍容许部分 JSON；执行前必须是完整对象。损坏参数携带内部错误标记并返回工具错误回执，正常同批调用继续；保留空流/空对象、完整字符串转义修复。覆盖 Anthropic、Bedrock、Mistral、OpenAI Completions/Responses、proxy/Pi 事件桥；内部标记保留在历史，typed Context 上行时移除。此项是明确的 MISAKA 行为修复，不伪称上游移植。 |
| 3 | Research runner 释放与延迟 spawn 回复竞态 | 释放时同时消费 runner key；未认领的非空 key 不算完成，延迟回复不再复活已释放 runner；更换 owner 后旧 release 无效。 |
| 4 | 一个节点澄清令兄弟失去写入资格 | 节点先进入 waiting_input，已运行兄弟继续结算，停止补充待启动节点；整层收束后才令 run 全局等待。根节点内联路径同样保存等待状态。 |
| 5 | 派生索引写失败把 done 反转为 failed | 索引 OSError 记录警告，最终/部分产物保留；异常分支重读当前状态，不以旧快照反转 done。InterruptedError 仍传播，不吞用户停止。 |
| 6 | 来源 URL 去 query 后错配文档 | 按含 query 的 URL 匹配，保留已登记的明确别名；两个文件争用同一别名时标为未解析，不任取一个。 |
| 7 | 来源清单沿用过期 SHA | 读取与放置时核实实际 SHA；已登记来源变化则标未解析，既不冒充旧证据，也不把登记 SHA 偷换成新值。 |
| 8 | 本次 run token 包含无关任务 | 复用 budget 账本解析，只统计该 run 的 LO 记账身份与关联 Research tasks；全局预算与上限不变。 |
| 9 | async 中同步 spawn 阻塞；取消丢失晚到 handle | 复用 settle_thread_call 在线程执行并保留取消所有权；拿到 handle 后先登记，再传播取消并等清理完成。启动过程中停止/换 driver 后不再继续补充节点。 |
| 10 | 已完成 Research 回答被后续聊天错误污染 | 固定第一份完成的阶段回答；后续排队聊天的 error/aborted/length 不覆盖它；真实停止与预算 lease 检查保留。 |
| 11 | max_depth 接受 bool/截断 float | API 边界拒绝 bool 和浮点数；整数及 CLI 整数字符串仍可用，0–12 原范围不变。这是参数类型校验，不涉及研究中的数学计算，也不改历史记录。 |
| 12 | 来源文件名碰撞通过硬链接覆盖原件 | 名称持续去重；EEXIST 不降级为覆盖式 copy；跨设备使用排他创建，复制/关闭失败删除自己新建的残片并保留原件。 |
| 13 | Board accept 的 card → DB 反序 | 关联 Research 的只读查询移至 card 锁前；generation/status 文件屏障不动，不靠加大 timeout 修复。原死锁证据局限于共享连接交错测试，独立 WAL 连接是通过的对照，未声称线上一定死锁。 |
| 14 | Bundle 未接入既有私有材料限制 | 复用 check_material_read，保护范围内材料不进入来源包；仍限工作区内真实文件。测试使用临时虚构私有材料，不触碰个人目录。 |
| 15 | 旧 driver/节点仍能提交阶段或审批动作 | 同时验证构造工具时捕获的 driver 与 node epoch；审批 revise/start/withdraw/skip 的检查与写入放在同一事务。follow-up 工具不再借新节点快照获得权限。 |
| 16 | 旧审批循环继续运行或 finally 改新 owner | 循环检测 owner；注销工具按对象身份，状态清理带 driver/node 条件；初始进度通知取消也进入清理，不覆盖接替者状态/回调。 |
| 17 | 重建删除自引用 bundle 路径却标成有效 | 保留来源包“可删除、从头重建”的设计；此类自引用明确列未解析，说明应引用原始文件。原始文件引用可正常重建，不承诺永久 bundle 路径。 |
| 18 | session 清理期间取消遗留环境变量 | node_session 与共享 platform.run_session 都等自己持有的清理完成；外层 finally 恢复原环境，再传播取消，覆盖重复取消。 |

同时补充两处已有约束的工具 schema 描述：dependencies 是同一计划的 local_id；ready 需要 red_team。
没有新增“每个 Sister 必须调用一次 Agent”的全局配额规则；特定任务的指令传递与引擎通用配额不是同一个契约。

## 验证

最终生产代码冻结后的隔离工作树完整测试：

```text
2272 passed, 1 skipped, 144 subtests passed in 48.15s
```

合回主目录后使用同一命令再次完整执行：`2272 passed, 1 skipped, 144 subtests passed in 49.21s`。
主目录 Ruff 与最终文件基线检查也通过：24 个合回文件逐字节相同，其余原有 tracked 文件未变，原上游报告保留。

命令（Python 使用主项目既有虚拟环境；HOME、缓存、DB 与材料都隔离）：

```sh
HOME=/tmp/misaka-repair-20260915-rRMvKg/home \
UV_CACHE_DIR=/tmp/misaka-repair-20260915-rRMvKg/uv-cache \
PYTHONPATH=. /Users/makiko/Projects/misaka/.venv/bin/python -W error -m pytest -q -rs \
  -o asyncio_default_fixture_loop_scope=function
```

- 唯一跳过：`tests/web/test_vault_browser_live.py:23`，需要显式提供可抛弃浏览器样本的可执行程序。未用用户日常浏览器代替。
- 新回归：`tests/test_research_edge_repairs.py`、`tests/test_research_deep_repairs.py`、`tests/test_research_repair_contracts.py`；涵盖正常/异常、共享/独立 WAL 连接、取消/换 owner、合法/损坏参数等对照。
- 修改范围 Ruff、23 个 Python 文件 AST、`git diff --check` 通过。
- 实际构建 `misaka-0.15.2.tar.gz` 和 wheel；wheel 安装到临时 target 后，从该安装位置导入 12 个相关模块并运行参数/深度校验 smoke，通过。
- 开发途中新增 Bedrock 测试曾写错 helper 名称，wheel 路径断言曾未归一化 macOS `/tmp` 与 `/private/tmp` 别名；均只修正测试/验收脚本，最终重新执行通过，未据此修改生产行为。

## 合回与证据

采用隔离工作树、原文件 SHA256 基线、`git apply --check`、应用前再次核对 SHA、合回后逐文件字节比较。
保留主目录原有的未跟踪上游核查报告；没有 reset/restore、提交或全仓格式化。

临时证据目录：`/tmp/misaka-repair-20260915-rRMvKg/`。
核心文件：`baseline-sha256.txt`、`acceptance-isolated.log`、`ruff-final.log`、`build-final.log`、`wheel-smoke.log`、`repairs.patch`、`merge-verification.json`、`acceptance-main.log`。
临时目录不是长期档案；稳定结论与回归代码保留在仓库。本报告的验收范围不包括真实远程模型、实时浏览器 fixture 或生产 run 的再次端到端运行。

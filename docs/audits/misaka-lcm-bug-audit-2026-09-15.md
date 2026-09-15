# MISAKA LCM：项目缓存地毯式回归验收

## 结论与范围

本轮在上一轮项目缓存 fork 上继续审计，确认并修复 **11 类宿主边界问题**。只改应用层、适配层及测试；本轮 `vendor/`、`native/` 源码与审计起点逐字节一致，原有核心完整性门禁继续通过。

审计链路：项目归属 → 工具/上下文组装 → 原生 checkpoint/显式 carry → 恢复/清理/再次恢复 → 真实原生子进程 → 多进程缓存租约 → 后台线程和连接池 → CLI → wheel 冷启动。未接触实际运行的用户任务或 Hermes 服务。

本报告补充并更新 [上一轮项目缓存验收](misaka-lcm-project-cache-2026-09-15.md)，不是承诺所有可能输入都没有 bug。

## 已复现并修复

| # | 级别 | 确认的问题 | 最小修复与回归 |
|---|---|---|---|
| 1 | P1 | 语义工具 deadline 已返回，但后台线程仍使用 SQLite；退出会提前关闭连接/删库 | `host/execution.py` 在原有 deadline 宿主接缝跟踪 worker 所属引擎；工具调用和主动召回组装均绑定 owner；退出先排空。保留原版 deadline、容量信号量、取消语义。真实线程超时后继续查询真实数据库，证明退出等待它完成；覆盖容量拒绝、零 deadline、排空超时 |
| 2 | P1 | 语义检索池中的 VectorStore 超过引擎生命周期，遗留已删数据库的连接/矩阵 | 最后本进程 owner 退出时，持有原有池锁/查询锁，仅关闭该项目的池条目。两个真实 VectorStore 验证 A 已关闭而 B 不受影响 |
| 3 | P2 | A 项目关闭失败，导致 B 项目也不清缓存 | `close_all()` 按项目独立判定；A 保留故障 owner/租约，B 正常清理。故障注入验证 |
| 4 | P1 | carry 目标未立即记项目；恢复会话的子代理继承启动目录而非原项目 | carry 写原生 `lcm-project` 元数据；子进程使用父会话持久化项目归属，执行 cwd 不变。真实 native child + 本地 HTTP provider 验证异目录恢复 |
| 5 | P1 | `/new --carry` 的 DAG 可检索，但当前模型上下文为空 | 使用原版 `_assemble_context` 组装已选 carry 节点，再沿既有提示隔离/Replay 接缝写原生 checkpoint；不另写摘要算法。直接复现修复前上下文 `[]`，验证修复后及删缓存恢复后均可见摘要 |
| 6 | P1 | 归档 importer 冒充实时会话 owner：覆盖进程引擎注册、改写共享生命周期当前 session | 归档读取器不注册为 live owner，原版生命周期绑定在其私有内存 SQLite 上运行。验证真实源 owner 注册和目标会话 lifecycle 均保持不变 |
| 7 | P1 | 显式清理 raw rows 后，同进程重复恢复复用旧数字句柄、仅按文字/hint 找到失效 checkpoint 节点 | 复用映射前分批检查源行仍存在；checkpoint 节点同时核对深度和完整当前来源身份。连续 3 轮真实 clean → resume → expand 验证 |
| 8 | P2 | 图像 token 校准仍写宿主全局 JSON，超出项目缓存生命周期 | 仅在现有 host 文件读写接缝关闭跨启动持久化；原版进程内校准继续工作。真实 usage calibration 返回有效结果，同时无全局文件写入 |
| 9 | P2 | 宿主工具异常被封装成成功的 native tool result | 已知 host exception 设置 `isError: true`；不根据上游正文关键词猜错误。遍历全部 15 个工具验证项目归属和异常 envelope |
| 10 | P2 | argparse 支持 `--app`，但宿主只识别 `--apply`，导入前未初始化新一代 ID 区间 | 复用原版 parser 的 `.apply`；验证导入前 `sqlite_sequence > 2**32`，保持项目路径限制/原版参数行为 |
| 11 | P2 | 退出无限等待 rollup/assertion/FTS；A 项目还会等待 B 的 FTS | 每个引擎后台排空共享 25 秒预算；只等本项目 FTS。到期保留活跃引擎/租约，不关闭活跃连接、不误删缓存。三类 timeout 和真实跨项目 FTS 阻塞分别回归 |

核心算法、工具输入 schema、native session 格式、Board/Sisters/Research 协议没有改动。新增原生 session 元数据仍走已有 custom entry，不替换用户历史。

## 验收证据

隔离副本：`/tmp/misaka-lcm-work/audit-2feqokha`。日志目录：`/tmp/misaka-lcm-work/`。

- 先红后绿的复现日志：`audit-red.log`、`audit-red2.log`、`audit-red3.log`、`audit-red4.log`、`audit-red5.log`。退出预算的测试额外引入新 timeout 参数；无限等待路径同时由原实现无参 wait/join 和跨项目实际阻塞证明。
- 完整主套件：**2065 passed, 1 skipped, 144 subtests**，`audit-main-final.log`。
- 严格主套件（仅排除下述既有连接泄漏文件）：**2060 passed, 1 skipped, 130 subtests**，`audit-strict-final.log`。
- 核心/项目/接续/子进程/Sisters 邻接验收：**77 passed, 12 subtests**，使用 `-W error`，`audit-accept2.log`。
- 并发/退出压力检查：**10 轮 × 7 项通过**，`audit-stress-1.log` 至 `audit-stress-10.log`。每轮含 6 个真实 LCM 引擎进程共享同一项目：6 条原文全部入库，前 5 个退出不删库，最后一个退出才删；另含跨进程 SIGKILL 恢复、超时 worker 和跨项目 FTS 隔离。
- 完整 vendored 套件：**3008 passed, 20 failed, 2 skipped, 12 xfailed**，`audit-vendor.log`。采用上一轮相同隔离宿主 alias harness，并禁止外部网络；失败 node ID 集合与上一轮 `vendor-fixed.log` **完全相同**。其中 18 项是既有 Hermes 宿主/平台/适配断言差异，另 2 项要求任意全局 externalization env 路径，与项目自有路径契约冲突。原测试断言没有修改，也没有将失败计为通过。
- Scoped Ruff、`git diff --check` 通过。离线 sdist → wheel 构建通过；376 个 LCM 打包文件逐字节核对，包含 manifest/skill，旧包名不可导入，独立目录冷启动 status 显示 `misaka-lcm` / `project-runtime`，退出后无内容缓存。见 `audit-build.log`、`audit-wheel.log`。

### 重跑命令

```sh
.venv/bin/python -m pytest tests -q
.venv/bin/python -m pytest tests/test_project_lcm.py tests/test_lcm_replay_noop.py \
  tests/test_subagent_native_startup.py tests/test_sister_context_governance.py -q -W error
.venv/bin/python -m pytest tests -q -W error --ignore=tests/test_research_method_handoff.py
.venv/bin/ruff check misaka/core/subagent/runtime.py misaka/extensions/misaka_lcm/host \
  tests/test_project_lcm.py tests/test_subagent_native_startup.py
uv build --offline
```

完整严格主套件单独排除的 `test_research_method_handoff.py`，是上一轮已在原始基线复现的 SQLite 连接未关闭 ResourceWarning；本轮不改该无关文件。普通完整主套件包含它。

## 明确边界

- 正常最后 owner 退出删除的是 **LCM 内容缓存**，不是 MISAKA 原生历史。恢复时以原生会话为来源重建。
- SIGKILL/崩溃不会执行退出清理；残留在下一次项目 LCM 启动、确认无活跃 owner 后清理。后台排空超时也保留缓存，避免活跃线程访问已关闭数据库。25 秒约束是每个引擎的后台排空预算，不是整个进程退出的硬实时承诺。
- `.misaka/lcm.gate` / `lcm.activity.sqlite` 只保存同步状态，不含任务正文；它们不是自动销毁的内容缓存。
- 图像 token 校准值在进程内共享且不写盘；它们是 token 成本估计，不是可召回会话内容。
- 未跑付费/真实远端模型、真实 OAuth、长时间交互 GUI 或其他操作系统验收。实际清理的是逻辑文件删除，不承诺 SSD 安全擦除、系统备份或进程外痕迹清除。


## 合入后复验与实际清理

- 11 个目标文件经原始 SHA-256 守卫、`git apply --check` 后合入；合入时另 1891 个已有文件和 git index 字节未变。没有 staging/commit，也没有覆盖并行会话的 Office/Web 等修改。
- 主工作区完整套件 **2111 passed, 1 skipped, 144 subtests**，`audit-published-main.log`。比隔离副本增加的 46 项来自其间并行改动，未丢弃这些改动。
- 主工作区严格套件 **2106 passed, 1 skipped, 130 subtests**，仅排除前述既有 SQLite 泄漏测试文件，`audit-published-strict.log`。
- 主工作区专项/邻接 **77 passed, 12 subtests**，`-W error`，`audit-published-accept.log`。主工作区 scoped Ruff 通过。
- 从合入后的主工作区重新离线构建并冷启动 wheel，376 个 LCM 文件逐字节一致，所有打包/命名/退出门禁通过，见 `audit-published-build.log`、`audit-published-wheel.log`。
- 确认无该文件的打开者后，仅删除旧全局 `~/.misaka/agent/cache/image_token_costs.json`（36 字节）。清理前后原生 session JSONL 清单/哈希完全相同（当前 1 个文件）；旧 `~/.misaka/lcm.db` 和 `lcm-large-outputs` 仍不存在。没有清理 Hermes 服务、其他缓存、Board 或用户项目文件。

### 上游 20 项失败的归类（均无本轮新增）

| 数量 | 测试文件 | 实际差异 |
|---|---|---|
| 3 | `test_benchmarking_cli.py` | vendored repo 的根路径与测试 cwd 不同，提前触发 output-outside-repo 检查；预期的更内层 export 错误文字未到达 |
| 1 | `test_db_bootstrap_fts.py` | 既有 U05 仅在真正验证通过后清故障标记；旧断言要求未验证也清标记 |
| 2 | `test_dependency_contract.py` | 测试预期 9 个外部导入；MISAKA 宿主登记后实际为 10 个 |
| 2 | `test_import_lossless_claw.py` / `test_ingest_protection.py` | 旧断言要求任意全局 externalization env 路径；本 fork 使用项目内容目录；宿主实际导入/恢复测试验证 payload 没有丢失 |
| 4 | `test_lcm_engine.py` | caplog 仅对 `hermes_lcm.engine` 设置 INFO，实际日志属于 canonical MISAKA 模块；功能状态断言已经通过，日志文字捕获断言失败 |
| 1 | `test_packaging_install.py` | 旧入口要求 Hermes 插件 git checkout 身份；MISAKA fork 的 wheel/冷启动另有实际验证 |
| 2 | `test_path_containment.py` / `test_path_security.py` | macOS 路径 resolve 为 `/private/var/...`，旧测试用未 resolve 的 `/var/...` 做字符串前缀断言 |
| 5 | `test_storage_permissions.py` | 既有权限隔离 worker 在子进程执行；测试在父进程 monkeypatch `os.open` 的竞态注入未进入该 worker。它们不是本轮修复或通过的竞态验收 |

完整失败 node ID 保留在 `audit-vendor.log`；与上一轮 `vendor-fixed.log` 的集合差集两边均为空。维持这些失败可见，不改上游断言压绿。

## 补充：记忆能力与实际可检索内容

用户随后提供的新会话状态为 `distinct_lcm_any_sessions=1`、原文 2 条、摘要 0 条；另一个旧 ID 位于 `state_only_sessions`，数据源明确是 `misaka-session-catalog`。这表示原生历史目录保留了旧会话登记，不表示旧正文仍在当前 LCM 库。只依据该状态也没有证据判断项目里不存在文件。

**契约没有偷偷改为每会话隔离：** 同一项目当前缓存仍可包含多个会话，并允许检索已载入的会话。项目隔离/退出清空与禁止跨会话检索是两种不同要求；后者未实施。本次没有关闭召回、删原生历史或更改核心。

修正仅限宿主语义：

- `recall_guideline()` 明确新会话不自动导入旧归档、最后 owner 正常退出清理，并要求分清工具能力、原生目录 ID 与实际已载入证据；移除“重启后直接搜索旧证据”的含混说法，先恢复/接续/导入才有内容可搜。
- `runtime_identity` 增加 `history_scope=loaded-project-cache` 和 `native_session_catalog_contains=session-identifiers-not-conversation-content`；原有字段、工具名和输入参数保留。
- 宿主 skill 同步说明：`lcm_status` 不是读取原生历史正文，也不是扫描项目文件。
- 新回归真实创建旧摘要与原生归档 → 退出清缓存 → 创建新会话；目录仍报 2 个 ID，而 LCM 只有当前 1 个会话，旧正文全库检索无命中，原归档逐字节不变。

专项/邻接验证 **78 passed, 12 subtests**（`-W error`，`status-accept-final.log`）；Ruff、核心 342 文件字节门禁、离线构建和 376 文件 wheel 冷启动检查通过。本次未读取/改动用户正在运行的数据库、会话文件，也没有重启用户窗口。已启动进程不会自动热加载这次宿主提示；下次启动使用新提示。没有把无真实 provider 实测的提示改动称为“保证模型不再误述”。

本补充的隔离完整主套件：**2158 passed, 1 skipped, 144 subtests**，`status-main.log`。

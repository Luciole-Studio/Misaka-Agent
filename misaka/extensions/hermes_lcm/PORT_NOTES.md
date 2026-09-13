# Hermes-LCM 移植契约

更新：2026-09-10。固定插件 `8d1b1e6d3d63f5fc7b209e8d7ec1dc9b814f2e54`（1.0.0-rc.1），固定 Hermes 宿主 `610576454a40e9b0ff5e53ebaa89f37aacce7d36`。

**源码覆盖已闭合；不等于整个 Hermes 宿主逐行为完全相同。** 当前接线、测试与未关闭项见 [本轮验收](../../../docs/audits/hermes-lcm-finish-2026-09-10.md) 和 [施工计划](../../../docs/plans/hermes-lcm-port-plan.md)。没有增加独立记忆服务，也没有默认开启跨会话主动召回。

## 已登记的改动

除下表之外，vendored 文件与固定插件逐字节一致。Python 改动带 `# misaka:`；JSON 用清单登记（JSON 不支持注释）。原版测试不改断言。

| 文件 | 位置 | 类别 | 原因 | 再同步动作 |
|---|---|---|---|---|
| `__init__.py` | 首个宿主导入 | 宿主导入适配 | 提供 Hermes ContextEngine ABC；其余原版入口、能力声明、策略和 preanswer 钩子保留 | 重放单行导入，校验真实 Hermes ABC |
| `aux_session.py` | host DB probes | 宿主目录适配 | 原生宿主没有 Hermes state.db，None 表示未提供该数据库；显式 rollover 和辅助祖先检查不得对 None 调用 exists | 保留原版分支判断和 native carry 回归 |
| `engine.py` | rollup schedule、assertion worker、get_tool_schemas；frontier、condensation、rollover | 宿主接缝；上游错误修复 U03 | Pi 会话树压缩只凝练和组装当前 checkpoint 范围内的节点，未选中分支仍可显式检索；默认范围为 None 时保持原版整 session 行为。每个排队任务单独捕获会话 ContextVars；取消 assertion batch 时释放原版 slot 并记录失败供下次重试。纯 schema getter 改 staticmethod，注册工具不启动无主数据库引擎。U03：用完整摘要前沿和深度统计做组装/凝练判断，迁移计数不取分页样本；保留原版排序、预算和凝练门槛 | 若上游已覆盖则删除；否则重放这些接缝和 U03，不复制 schema 列表或另写摘要选择器 |
| `tools.py` | `_run_within_deadline`；lcm_doctor | 宿主线程适配；上游错误修复 U03 | deadline worker 同样需要调用会话的模型和配置；不改变期限、信号量或失败语义。U03：来源完整性检查覆盖所有节点，不凭前 1000 个节点宣告全部有效 | 同上，保留 worker 的上下文传递和大 DAG 来源回归 |
| `compaction.py` | compress 节点计数日志 | 上游错误修复 U03 | 使用现有 SQL COUNT，而非对限量节点查询取 len，避免大 DAG 的日志少计 | 上游修正后移除；保留真实压缩日志回归 |
| `config.py` | from_env、Hermes YAML defaults | 宿主配置适配 | 接受显式 host_config；MISAKA 传只读全局 settings，不读取另一个 Hermes 安装的 config.yaml。默认 None 仍保留原版配置来源与优先级 | 重放显式宿主配置入口；保留原版默认路径回归 |
| `dag.py` | add_node、retain-depth | 宿主取消与会话树适配 | 可选 before_publish 检查点在摘要 INSERT 前和提交前检查取消；取消则回滚该次 INSERT。宿主可临时设置节点范围，只有成功提交的节点加入范围；U07 在 retain-depth 删除中间节点之前，将仍存活且直接引用这些节点的父摘要改连到相同原文；保留节点 ID、原文引用和其他 DAG 边；直接遍历 DAG，不依赖原文表存在，不改 schema。原版 leaf/condensed 统一经过此处，不在网络请求期间持有写事务 | 上游有等价取消接缝时采用上游；保留 SQLite 发布中止回归 |
| `reconcile.py` | _get_store_id_map_for_messages | 宿主会话树适配 | 可选 active_store_ids 限定当前分支的原文来源，避免同文兄弟分支串线。None 保持原版匹配规则；来源集合由宿主按 append-only 原文顺序和真实 SessionManager 分支计算 | 重放最小候选过滤接缝；保留同文分支来源与原版重放测试 |
| `sqlite_util.py` | permission worker | 上游错误修复 U02 | 在同一进程打开再关闭 DB/WAL/SHM 会撤销 SQLite 的 POSIX 锁；保留原版 fd/no-follow/identity 检查，把权限写入放到无 SQLite 连接的短命进程。已是 0600 的文件只做 metadata 校验，不启动进程 | 上游修正后移除；保留持锁加固、第二写者隔离与多进程压测 |
| `externalize.py` | `_stream_json_read_string` | 上游错误修复 U01 | JSONDecoder 返回字符偏移，原版却与 UTF-8 字节长度比较，中文等内容被误判损坏 | 上游修正后移除本地差异；保留 Unicode 正向、损坏输入和公开调用链回归 |
| `diagnostics.py` | state_db_path_for_engine | 宿主目录 | 优先采用原 owner 的 native catalog；不猜测相邻 Hermes DB | 保留只读诊断测试 |
| `lifecycle_state.py` | get_fragmentation_stats | 宿主目录 | 宿主提供 session ID 集合，原版统计和原路径 fallback 不变 | 保留目录缺失/陈旧 ID 对账 |
| `import_lossless_claw.py` | imports/config | 运维入口 | 使用包内依赖与 MISAKA LCM 路径；U04 显式 closing 关闭只读源连接，事务/收据不改 | 原版导入测试 |
| `backfill_externalized_tool_outputs.py` | imports/config | 运维入口 | 使用包内依赖与 MISAKA sidecar 根；DB 保持只读；U04 关闭 apply/rollback 的只读连接 | 原版 apply/rollback/dry-run 测试 |
| `state_embedding_backfill.py` | imports/main | 运维入口 | 使用包内依赖，main 可接显式 argv | 原版状态向量批次/账本测试 |
| `db_bootstrap.py` | FTS fast/deep/repair paths | 上游错误修复 U05/U06 | 节流/异步/unchecked/只补 trigger 时保留损坏标记；只在实际 deep pass 或 rebuild 后清除，避免抹掉刚写入的后台告警；跨进程 scan claim 在 BEGIN IMMEDIATE 后重检 | 保留确定性线程交错和原版 FTS 测试，上游修复后删除本地差异 |
| `dependency-contract.json` | external_imports.misaka | 依赖登记 | 声明本移植实际使用的 native package API；不改原版依赖扫描器 | native validator 完整扫描 |


U07：retain-depth 删除中间摘要导致仍存活摘要的原文链路丢失。U01：JSON 字符偏移与 UTF-8 字节偏移混用。U02：同进程额外关闭 SQLite 文件会撤销 POSIX 文件锁。U03：要求全集的消费者把分页查询当全集。U05：未验证的启动快速路径会抹掉后台 FTS 损坏标记。U06：跨进程 scan claim 缺少持写锁后的重检，可能重复启动扫描。U04：sqlite3 Connection 的上下文管理器只结束事务，不关闭连接；历史导入和外部化回填入口现用 `contextlib.closing`。这些是保留的上游缺陷修复，不为追求字节相同重新复制错误。历史证据分别见 `docs/audits/hermes-lcm-2026-09-08.md`、`hermes-lcm-upgrade-2026-09-08.md`、`hermes-lcm-frontier-2026-09-09.md`。

## 一份策略，一套原会话

- `vendor/` 覆盖固定插件全部 261 个独立文件：62 个顶层 Python 模块和原版 scripts、benchmarking、测试、fixtures、文档与发布参照。独立安装/发布资产只是参照，不成为 MISAKA 的发布入口。MIT 文本在 `LICENSE.hermes-lcm`。
- `tests/hermes_lcm_vendor/` 的相对链接指向 vendor 测试实体；连同资源共 350 个本地映射路径。`UPSTREAM_COVERAGE.json` 没有漏记路径。
- `native/` 是固定 Hermes 源码的完整模块或精确符号选取。`HERMES_NATIVE.json` 登记选取、导入替换和 native IO 接缝；同步脚本从固定 git 对象重建并逐字节验证。包括 ContextCompressor、ABC、摘要 dispatch、micro 算法、压力与 usage anchor、图像校准、辅助路由/恢复/时限/并发、冷却策略、reasoning、39 个 provider profile 模块及缓存身份。
- `host/` 只负责原生 SessionManager、ModelRegistry/AuthStorage、配置、取消、路径、工具与运维入口。没有第二个 agent/session owner；辅助请求不创建 LO/Sister 会话。

## 原生执行链

| 边界 | 当前接线 |
|---|---|
| 请求前压力 | `sdk.transform_context` → `AgentSession.prepareContextMessages` → `session_context_prepare` → `context_engine.prepare`。传原生 system/tools；固定 Hermes `_preflight_request_tokens` 使用 usage anchor、图像价格和清理后回放，LCM 自己判 should_compress/preflight |
| 压缩与恢复 | LCM 的 fresh tail、chunk、full sweep、预算与 assemble 保留；完整结果发布到原生 compaction checkpoint。原文 append-only；来源/leaf/发布代次检查成功后才采用。重开、tree、fork 从选中 checkpoint 重建；相同文字不充当来源身份 |
| 原生 fallback | ignored/stateless 路径调用固定 ContextCompressor；失败/超预算后备仍由原版分支决定。取消不转摘要重试或另一引擎。nativeMetadata 保存在现有 checkpoint details，不进入 provider wire |
| 生命周期 | 每 `(database, session)` 一把锁、一份引擎；注册锁不跨 LLM 等待。start/model/tree 绑定 native 身份；普通 new 执行 end/reset/retain-depth 后关闭旧实例；显式 /new --carry 先保存原生来源凭据，再由原版 rollover 转移保留摘要及同一 engine；quit/stop 等 owner 排空。attach/detach 不关闭执行者 |
| preanswer | 仍在 native context 出口附加，但昂贵的原版检索按 append-only 人类/工作单 ingress ID 缓存，一轮工具往返只算一次；同文新输入重新计算。失败不在同轮反复付费。注入是副本，不写原始会话。**有意偏离（2026-09-11）**：原版把 recall policy 全文拼在每条用户消息末尾（Hermes 只有这一个接缝，且要保系统提示词缓存）；misaka 把它挂成 15 个 `lcm_*` 工具共同的 `promptGuidelines`（系统提示词去重后只出现一次，同样是稳定前缀），用户消息只在真检索到证据时才追加证据摘要。原因：一个字的用户消息后面跟两千字策略，模型把整条当成系统说明或注入，把用户的话丢了（真实运行两次复现） |
| usage | 原生 message_end 捕获 input/output/cacheRead/cacheWrite；恢复从选中 replay 的同模型/同 provider 最后已记录用量重建 anchor；模型切换清除 anchor。图像学习价格按模型/endpoint 绑定 |
| 辅助配置 | 五类 LCM task 读取全局 settings.json 的 auxiliary，只读快照贯穿尝试。auto 继承主模型，不自动换廉价模型；原版优先级、reasoning/extra_body、profile 参数保留。生成的 headers 只进 HTTP headers，不进入 JSON body |
| 辅助恢复 | 固定 sync transient/额度/参数梯级；native OAuth 凭据刷新带 rejected-token fence，单次 grant 兑换开始后先持久化再响应取消；原版池选择/冷却接同一 auth.json，Nous 模型/付费访问自愈；原版 task/main/discovery fallback、上下文窗筛选和冷却策略。fallback 自己解析认证，不继承失败 task 的 key/endpoint；没有支持的 native transport 不伪装成另一个模型 |
| 时间与取消 | 原版任务 semaphore、idle/no-progress/硬上限、稳定 owner cache scope。每物理请求独立资源 ID；取消后 join provider producer，再清理资源；SDK 内重试不与外层重试叠乘 |
| 诊断 | native catalog 的 session 集合进入原版 fragmentation 统计，不猜相邻 Hermes state.db，不建第二套 session 索引 |
| 运维 | 顶级 `misaka lcm` 和可选 `/lcm`；import、externalize-backfill、state-embedding-backfill 是原版脚本薄入口，其余命令走原 dispatcher。只读 DB 的历史 sidecar 回填不先创建 runtime engine |

## 路径与开关

LCM 原文派生库默认 `~/.misaka/lcm.db`；`LCM_DATABASE_PATH` 优先于 MISAKA 的数据库配置。大结果 `lcm-large-outputs/` 和提取 `lcm-extractions/` 默认在该 DB 旁。显式 operator 路径按其原版 grammar。研究正文/证据仍在工作区，不新建 `.misaka/runs`。

算法参数只用原版 `LCM_*`；删除旧 summary/retrieval/embedding 产品别名和临时 `os.environ` 修改。`MISAKA_LCM_DB` 是产品路径配置，不是算法别名。`LCMConfig.from_env(host_config=...)` 接收 MISAKA 全局 settings，不读 Hermes YAML、profile secrets 或 auth.json。

保留原版各项默认值；主动召回、preanswer、assertions、rollup、externalization、embeddings 分别按其开关运行，不把默认关闭说成没有功能。工具权限上限、敏感串清理和不可信来源围栏继续在 native 边界执行。

## Native 宿主适配与保留差异

- 显式 `/new --carry` 已接通原版 rollover；receipt 使用原生 compaction checkpoint，外来 raw rows 仍归原 archive。原库缺失或数字 ID 被复用时明确失败，不把摘要冒充恢复的原文。fork 仍复制选中 native 历史，不移动父 owner。
- 原版多凭据池、Nous OAuth/服务自愈、完整 reasoning metadata 和 ACP 已接入；`/login PROVIDER --account NAME` 保存命名原生凭据，配置池仅引用这些键，没有第二个凭据库。
- U07 保留节点的 raw lineage 修复登记于上表；U08 ACP EOF 先排空、U09 非对象 catalog JSON → unknown 的精确源改动登记于 HERMES_NATIVE.json；U10 原版最终重抛第一次错误的修复在 native recovery，双进程对照逐条列出。证据见本轮验收。
- 不移植整个 Hermes AIAgent/gateway、MoA preset、Codex AppServer 或另一套全局认证。Codex 使用已有 native Responses；未注册 provider 的认证/transport 仍须由原生 provider 提供，不凭 wire profile 冒充可用。原版六个 AIAgent/SessionDB 专属测试保留 skip，不记为通过。
- 每组 F01–F16 的 native 与原版证据见本轮账本；双进程对照覆盖明确的 replay/carry/recovery 矩阵和两种 tokenizer 环境，不宣称遍历所有状态。真实 provider/OAuth、实际 Copilot CLI、跨 OS／长时间 GUI 验收未执行。

## 验证门禁

- `tests/test_hermes_lcm_*.py`：native wire、session/checkpoint、权限/来源、取消、并发、运维回归。
- `tests/hermes_lcm_vendor/`：原版插件测试；原版 xfail 不改断言压绿。4 个平台/依赖契约适配 skip 加原版自己的 skip 明列在 harness 和验收记录。
- `tests/hermes_lcm_native/`：13 个原版宿主测试文件；6 个宿主边界 skip 明列，不算 native 通过。
- `scripts/lcm_sync_check.py`：插件全路径、登记差异、native 源码/测试/许可证。
- 升级步骤和命令见 `docs/plans/hermes-lcm-sync.md`。本轮测试只用隔离目录；没有付费模型调用、用户 DB 回填或 live research 状态修改。

取消不是跨 SQLite/JSONL 的全轮事务：已提交的原文/合法 DAG 节点保留；未采用的 checkpoint 不晋升。上游 12 个设计 xfail 保留；另一个 strict xfail 单列 U05 的旧错误断言（要求未验证的节流路径清掉故障标记），没有修改原版测试文本。

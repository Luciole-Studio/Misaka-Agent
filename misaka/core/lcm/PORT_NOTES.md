# hermes-lcm 移植记录

上游 pin 见 `UPSTREAM_COMMIT`。本文件是**再同步契约**:`vendor/` 下每一处偏离上游的
改动都必须在此登记,升级时逐条重放。没有记录的漂移 = 缺陷。

施工蓝图:`docs/plans/hermes-lcm-port-plan.md`。
**升级上游按 `docs/plans/hermes-lcm-sync.md` 做**;`scripts/lcm_sync_check.py` 是它的巡检工具
(逐文件比 sha,报未登记的漂移,退出码 1 = 有漂移)。

## 允许的改动只有四类

| 类 | 说明 | 再同步动作 |
|---|---|---|
| ① 导入适配 | 上游包内绝对导入 `hermes_lcm.X` 改为相对导入 | 新版若仍是绝对导入,同样改 |
| ② 宿主接缝 | 对 Hermes 宿主 API 的引用改为经 `host/` 注入 | 比对新版该处是否变形 |
| ③ 安全补丁 | 安全必需的最小修补 | 检查上游是否已自行修复,是则弃用本条 |
| ④ 上游缺陷 | 上游 bug 的最小修补 | 附 issue/PR 编号;上游合入后弃用本条 |

每处改动在代码行尾标 `# misaka: <一句话>`,并在下表登记。

## 已登记的改动

| 文件 | 行 | 类 | 原因 | 再同步动作 |
|---|---|---|---|---|
| — | — | — | **无。P6 结束时 `vendor/` 下 60 个上游 `.py` 全部与 pin 版本逐字节一致(`cmp -s` 逐个验证)** | 每次再同步后重跑 `cmp -s` |

唯一的宿主接缝(D6 第②类)没有落在任何上游文件里,而是落在两个 misaka 自己的文件上,
所以不占登记表:

- `vendor/__init__.py` — 上游的 `__init__.py` 是 Hermes 插件入口,**不 vendored**(见"刻意不移植"),
  所以这个文件从头到尾是 misaka 写的。它 import `host/context_engine_abc`,后者把
  `agent.context_engine` 注册进 `sys.modules`,于是 `vendor/engine.py:21` 的
  `from agent.context_engine import ContextEngine` 原样成立。
- `host/context_engine_abc.py` — `ContextEngine` ABC 的桩,只覆盖 `LCMEngine` 真正用到的面
  (四个抽象方法、十个类属性、七个只继承不覆盖的钩子、两个被 `super()` 调用的方法)。

  这个桩是**唯一一处"照着上游写"而不是"从上游拷"的东西**,也因此是唯一会**无声漂移**的地方:
  真 ABC(`~/.hermes/hermes-agent/agent/context_engine.py`)改了默认值或签名,`cmp` 是发现不了的。
  再同步时把两个类都 import 进来机械比一遍(`__abstractmethods__` 集合、十个类属性的值与类型、
  `inspect.signature` 的形参名/kind/默认值、`get_status()` 与 `on_session_reset()` 的实际输出),
  比读一遍可靠。桩"故意不覆盖"的五个方法(`on_session_start` `on_session_end` `get_tool_schemas`
  `handle_tool_call` `update_model`)成立的前提是 `LCMEngine` 全部覆盖且不 `super()` ——
  这条前提用 `grep -rn 'super()' vendor/*.py` 复核:整个闭包里只应有 `engine.py` 的
  `on_session_reset`(:3449)和 `get_status`(:3752)两处打到 ABC。

## 已 vendored 的文件

`vendor/` 下 60 个上游模块 = **P1 的 30 个文件 + 它们 import 闭包外溢出的 25 个 + `command.py`
+ P6 补拷的 4 个叶子**。闭包用脚本从 P1 的 30 个种子出发递归展开(顶层 import 与函数内 import 都算),
不是手数的;补拷的 4 个不在那个闭包里(没有闭包内模块 import 它们),是 P6 按范围直接拷的。

| 期 | 文件 |
|---|---|
| P1(在范围内) | `config` `presets` `chunking` `fresh_tail` `message_analysis` `message_content` `message_patterns` `session_patterns` `search_query` `guidance` `tokens` `sqlite_util` `db_bootstrap` `store` `dag` `lifecycle_state` `maintenance` `reset_state` `runtime_identity` `engine` `compaction` `reconcile` `placeholder_ledger` `aux_session` `bypass` `engine_registry` `escalation` `extraction` `model_routing` `sanitize` |
| P2(闭包外溢) | `schemas` `tools` `retrieval_core` `diagnostics` |
| P3(闭包外溢) | `ingest_protection` `externalize` |
| P4(闭包外溢) | `rollup_store` `rollup_builder` `rollup_periods` `occurrence_time` |
| P5(闭包外溢) | `embedding_provider` `vector_store` |
| P6(闭包外溢) | `adaptive_retrieval` `answer_contract` `assertion_extraction` `assertion_rebuild` `assertion_state` `assertion_store` `evidence_compiler` `evidence_pack` `query_view_store` `reasoning` `requirements_compiler` `trajectory_store` |
| P6(在范围内,补拷) | `preanswer_evidence` `selective_compiler` `selective_recall` `host_evidence` |
| P7 | `codex_routing`(闭包外溢) `command`(见下) |

`command.py` 不在闭包里(没有任何闭包内模块 import 它),是**为了让上游 11 个测试文件能跑**
而额外拷进来的:它的 import 面整个落在闭包内,零第三方依赖,拷进来不接线的代价是零。

上游 60 个顶层模块现在**一个不缺**。P6 补拷的那 4 个是最后的空缺:它们各自只被上游的插件入口
(不移植)或测试直接 import,所以 P1 的闭包够不到;它们自己的 import 面(`evidence_compiler`
`evidence_pack` `reasoning` `model_routing` + `agent.auxiliary_client` 那个已有接缝)整个落在
已 vendored 的集合里,补拷后不需要再扩闭包。

## host/ 适配层(P1 接线了什么)

`host/` 是 misaka 写的新代码(ruff + deadcheck 照常检查),`vendor/` 一个字节没动。

| 文件 | 职责 |
|---|---|
| `context_engine_abc.py` | Hermes `agent.context_engine.ContextEngine` 的桩(P0/A 号) |
| `switch.py` | `context_engine` 选谁 + 磁盘上的 db 是哪一版 schema(`messages.conversation_id` 一列定乾坤) |
| `config_bridge.py` | `MISAKA_LCM_*` → `LCM_*`;上游名优先,别名只在上游名缺席时临时铺进 `os.environ` 再撤掉 |
| `llm.py` | `agent.auxiliary_client.call_llm` 桩 → `misaka.platform.session.run_text`;缺席时上游各处自带确定性回退 |
| `ingest.py` | misaka `AgentMessage` → 上游 OpenAI 形状(内容拉平成文本、时间戳 ms→s、`tool_calls` 缺省给 `[]` 而不是 `None`) |
| `context_engine.py` | 压缩缝:pi 定边界,引擎按边界摘要 |
| `extension.py` | 事件订阅(ingest + 压缩 + P3 的 `context`);工具从 P2 起由 `tools.py` 注册 |
| `migrate.py` | D4 迁移器,挂在 `misaka lcm migrate [--apply]` |
| `tools.py` | P2:上游 15 个 `lcm_*` schema → misaka `ToolDefinition`,统一走 `handle_tool_call` + `fence.refence`;目录行(`promptSnippet`)由 `platform.toolkit` 从 description 派生,不手抄;`withheld(kind, engine)` 按会话类型扣留——运维三件套(`lcm_status/inspect/doctor`)只给 foreground,`lcm_query_state` 只在 `LCM_ASSERTIONS_ENABLED` 时给,其余十一个每种会话都有 |
| `fence.py` | P2:misaka 的不可信围栏,重新盖回 LCM 交给模型的东西上 |
| `externalize.py` | P3 保护层的两条 misaka 专属缝:活动上下文换 stub(`context` 事件)+ 老库补外部化(`misaka lcm externalize-backfill`) |
| `rollups.py` | P4 时间记忆的两件事:每轮压缩后的 `nudge`(misaka 一次会话只绑一次)+ `misaka lcm rollups [--rebuild]` |
| `embed.py` | P5:把上游自己的 `/lcm embed warmup\|backfill` 转发到 `misaka lcm embed` |
| `preanswer.py` | P6:上游 `pre_llm_call` 的预答证据钩子 → `context` 事件的第二个处理器 |
| `operations.py` | P7:上游的 `status` `doctor` `rotate` `preset` 四个运维口 → `misaka lcm`;doctor 后面补一段四族体检 |

**压缩缝的关键决定**:pi 给的是「要被替换的那一段 + `firstKeptEntryId`」,上游给的是「整张消息表进、整张出」。
桥接方式是**把引擎的 fresh tail 钉死成 pi 保留的那一段**(`fresh_tail_count = 保留条数`,同时临时把
`fresh_tail_max_tokens=0` / `leaf_chunk_tokens=1` / `dynamic_leaf_chunk_enabled=False`),于是
「上游认为该压的 raw backlog」= 「pi 要丢的那一段」,一条不多一条不少。摘要文本从引擎装配出的
active context 里按上游自己的 `[... Summary (dN, node M)]` 前缀取回。

喂给引擎的消息表是 `build_session_context(branch).messages`——**含 pi 的 compactionSummary 那条**。
这不是疏忽:上游 `_is_replayed_context_scaffold_message` 认得自己的摘要脚手架(不再入库、不再被重新摘要),
而 `_ingest_cursor` 正好把它算作 `len(compressed)` 的一部分,replay 它才是让游标和 store 对齐的那一步。

## 集成审查后加固的三处(host/,vendor 未动)

| 处 | 问题 | 修法 |
|---|---|---|
| `migrate.py` | **旧 db 的 `-wal` 会把迁移无声撤销。** SQLite 按**文件名**找预写日志,日志帧不带来源标识:`os.replace` 换掉 `lcm.db` 后,原地留下的 `lcm.db-wal` 会被下一个打开它的进程重放到新库上,`integrity_check` 仍报 `ok`,但库里是旧 schema 的旧数据。实测可复现(删掉 `-shm` 后必现)。 | 迁移前 `wal_checkpoint(TRUNCATE)` 把日志折回文件;换名后删掉 `-wal`/`-shm`;**另有进程还开着该库时直接拒绝迁移**(判据:自己的读写连接关掉后 `-shm` 仍在 = 别人还持有) |
| `migrate.py` | 重建中途失败会留下 `lcm.db.rebuilt-<ts>` 残留文件,且 `%H%M%S` 秒级命名让同一秒内的重试**接着往残留文件里追加**,计数翻倍后永久拒绝迁移 | 纳秒命名 + `finally` 里 `unlink`(换名成功后是 no-op) |
| `extension.py` | pi 把一轮压缩**丢弃**时(压缩中止 → `session_compact_failed`),引擎已经提交了自己那半边、`_ingest_cursor` 已越过它摘要掉的那一段;下一次 ingest 会把整段**重复入库**(实测 160 → 325 行、125 条重复) | 订阅 `session_compact_failed`,处理器重新 `on_session_start` 绑定该会话——上游自己修复游标的机制(`_schedule_ingest_cursor_reconciliation`)。修后 200 行、0 重复 |
| `context_engine.py` | **上游 FTS 引导有多进程竞态**(misaka 是多进程写同一个 db,上游是单进程宿主):两个进程首次创建同一个库时,`ensure_external_content_fts` 抛 `table "messages_fts" already exists`,引擎构造失败 → 该会话静默无 LCM。实测 72 次冷启动竞态中 2 次触发 | 构造失败时重开一次(输的那个再看一眼即可,赢的那个已经建完)。144 次冷启动竞态 0 失败。**这是上游缺陷,值得给上游提 issue**;host 侧重试不需要改 vendored 文件,所以不占 D6 登记表 |

D3 双进程压测已补:`tests/test_hermes_lcm_host.py::test_two_processes_ingest_and_compact_one_database_without_losing_a_message`
(两个进程并发 ingest + compact 同一个库,断言 integrity ok、每会话行数精确、FTS 同步、各出一个 DAG 节点)。
手工加压到 8 进程 × 120 条同样干净。

## 围栏保全(P2/B,host 侧,vendor 未动)

`host/fence.py` 把 misaka 的 `prompt_guard.untrusted` 围栏在 LCM 出口重新补上(蓝图 §4 P2 的
安全项)。蓝图原本设想给 vendored `store.py` 加一列(D6 第③类);实现时改为**零 vendored 改动**:
染色标记本来就在内容里(`untrusted()` 把哨兵写进文本,store 原样保存,body 里的哨兵被换成
`UNTRUSTED-DATA-ESCAPED` 仍含该子串),血统用上游自己的 `summary_nodes.source_ids` 递归即可。
所以不占 D6 登记表。

但它**依赖三条上游形状**,再同步时要复核(变了就是静默失效 = 洞重新打开):

| 依赖 | 位置 | 变了会怎样 |
|---|---|---|
| `messages.content` 逐字保存原文 | `store.append` | 哨兵被改写后检测不到 → 摘要出口不再加围栏 |
| `summary_nodes.source_ids` / `source_type` 的 JSON 形状 | `dag.py:149`、`source_message_ids` 的递归 CTE | 递归查不到源行 → 摘要被当成可信 |
| `SummaryDAG.connection` 是公开只读属性 | `dag.py:171` | 取不到连接时 `is_tainted` fail-closed(全部加围栏),不会漏,但会误伤 |
| `lcm_rollup_sources(rollup_id, node_id)` | `db_bootstrap.py:730` | `lcm_recent` 的 rollup 模式只报 `rollup_id`,查不到就没有血统可追 |
| 外部化占位符含 `xternalized ` 与 `; ref=` | `externalize.py:538-565`、`:1114` | 占位符改词 → 外部化行重新被判成干净 |

### 攻击审查(P2 后)补的四个洞

| 洞 | 触发条件 | 修法 |
|---|---|---|
| `host/tools.py` 出口根本没接 `refence` | 默认,15 个工具全裸 | 接线 + `test_the_registered_tool_fences_what_it_hands_back` 走注册后的 `execute` |
| `lcm_recent` rollup 模式只报 `rollup_id`,不报 store/node | `LCM_TEMPORAL_ROLLUPS_ENABLED=true` | `_ROLLUP_KEYS` + `lcm_rollup_sources` 解析回 node |
| 源行被删,摘要节点判干净 | `delete_session_messages` / 任何删行 | 血统里**任一**源行不在了就 fail-closed(LEFT JOIN),节点/rollup 解析不出来同理 |
| 外部化把哨兵搬进旁路文件,行里只剩 stub | `LCM_LARGE_OUTPUT_EXTERNALIZATION_ENABLED=true` | stub 与哨兵同列为「看不透的内容」,一律不担保 |

仍然开着(条件与代价都写在报告里):`context_engine.compact()` 注入 live context 的压缩摘要
不过围栏(仓主决策:给每轮压缩摘要加围栏 = 模型对自己的历史长期降级信任);以及 fence.py 只能
恢复围栏、不能发明围栏——misaka 侧没过 `untrusted()` 就进模型的外部内容它无从知道。

### P3 补的第五个洞:旁路文件本身

外部化把内容搬出数据库之后,**引用它的工具答案里既没有 store 行也没有节点**——
`lcm_expand(externalized_ref=...)`、`lcm_describe(externalized_ref=...)`、
`lcm_grep(content_scope='externalized')` 三处都只报一个 `ref` 文件名。P2 的 `_collect`
只认 store/node/rollup 三类 id,于是这三条路径整个绕过围栏判定。

`MARKER in output` 这条捷径挡不住它:`untrusted()` 的哨兵在文本**两端**,而这两个工具
返回的是调用方选定的一个切片——`lcm_expand(content_offset=5000)` 与 grep 的 match 片段
都取自正文中段。实测:一个被围栏的页面外部化后,`content_offset=5000` 取回的 100 token
里既没有哨兵也没有任何 id,`refence` 原样放行。

修法(host 侧,vendor 未动):`_REF_KEYS = {externalized_ref, externalized_refs, ref}`
进 `_collect`,`is_tainted` 见到任一 ref 直接判脏。**不读旁路文件去看有没有哨兵**——
有界读会漏掉界外的哨兵,而漏判正是要防的方向;何况指向它的那一行 stub 早就被 P2 判脏了,
ref 判干净会自相矛盾。代价是外部化载荷取回一律带围栏,而这一族默认关。

新增依赖(与上表同类,再同步时要复核):

| 依赖 | 位置 | 变了会怎样 |
|---|---|---|
| 工具答案用 `externalized_ref` / `externalized_refs` / `ref` 命名旁路载荷 | `tools.py:2680`、`:5248`、`:5362`、`:5436` | 换个键名 → 三条路径重新绕过围栏 |

**这条判据的已知代价(P3/P4/P5 合流审查实测,未改)**:`lcm_inspect` 的清单里有
`externalized_refs`(`tools.py:6377`),那是一份**只有文件名与字节数、不含任何载荷内容**的
盘点。于是一旦这一族开着且库里有过外部化,`lcm_inspect` 的整个答案就永远带围栏——不是
"某一次结果降级",而是一个纯诊断工具长期降级。fence.py 自己的文案说过这正是要避的成本。
放着不动是**故意的**:收窄一条 fail-closed 的安全判据去换一个诊断工具的观感,不是合流
审查该单方面做的取舍,留给仓主定。`lcm_status` 不引用任何 id,实测仍然干净。

## 保护层(P3,host 侧,vendor 未动)

`ingest_protection` 与 `externalize` 全部在 `store.append` / `engine._ingest_messages`
内部自洽:`MessageStore` 构造时收下 `ingest_protection_config`,每条消息进
`messages.content` 之前先过 `redact_sensitive_value` 再过外部化。host 不需要接任何写路径,
只需要三件上游宿主替它做、misaka 没有的事。

| 处 | 上游怎么来的 | misaka 怎么办 |
|---|---|---|
| 旁路文件放哪 | `hermes_home` | **`config_bridge` 显式给 `LCM_LARGE_OUTPUT_EXTERNALIZATION_PATH` = `<lcm.db 所在目录>/lcm-large-outputs`** |
| 活动上下文里换成 stub | `ContextEngine.compress` 返回整张表,宿主照单替换 | `host/externalize.stub_replay` 挂 `context` 事件(`transformContext`) |
| 老库补外部化 | `scripts/backfill_externalized_tool_outputs.py`(未 vendored) | `host/externalize.plan/run` + `misaka lcm externalize-backfill [--apply] [--limit N]` |

**落盘路径这一条是个真缺陷,不只是配置口味**:`hermes_home` 留空时,
`LCMEngine` 走 `~/.hermes/lcm-large-outputs`(**另一个 install 的数据目录**,本机上真有 315 个
文件),而 `MessageStore.__init__` 的回退是 `str(self.db_path.parent)`。两边不一致 = 写的人
和读的人找的不是同一个目录,`lcm_expand(externalized_ref=...)` 对着确实存在的载荷报
"not found"。实测复现过(载荷落进 `~/.hermes/`,已清理)。配置里显式给出的路径在
`get_large_output_storage_dir` 里优先级高于 `hermes_home` 两侧,所以一处设置同时按住两头。

`stub_replay` 的决策整个是上游 `_stub_large_tool_results_for_active_replay` 的:两个开关、
token 阈值、受保护的 fresh tail(默认 32 条)、"模型刚要求展开的那个载荷不动"的豁免、
结构化内容保持块形状。host 只做**按 `toolCallId` 回接**(不按下标——`convert_to_llm` 会丢掉
`excludeFromContext` 的消息,两张表不等长)。它和 `host/` 的其余五个事件处理器一样**跑在
工作线程上**(`extension._off_loop`):上游从 live config 读受保护尾长,而
`context_engine._host_driven_boundary` 会在一次压缩期间钉住那个字段——挡住这个窗口的是
`context_engine.ENGINE_LOCK`(压缩、工具、这两个 `context` 处理器共用一把),不再是"同一
条线程"。曾经的理由("那个窗口里没有 `await`")只在钉住期间不阻塞时成立,而压缩恰恰要
`run_coro` 同步等一整轮辅助模型——那期间 `tools.py` 的工作线程照常运行。

`externalize-backfill` 复用 `store.gc_externalized_tool_result`(上游自己缩写已外部化行的
那个写),它自带 role/pinned/幂等三道判据,并且在同一个事务里 `before_commit` 归档 chunk
偏移。`--apply` 成功后跑一次 `VACUUM`:不跑的话行确实缩了,但 SQLite 把腾出来的页留着自用,
文件大小不变、备份照样拷那几兆——命令的卖点就没兑现。前后都要先
`wal_checkpoint(TRUNCATE)` 再量文件,否则"之前"量到的是空壳、算出来是负数。

### 上游 `api_key` 模式的一个已知缺口(未修,vendor 不动)

`_SENSITIVE_PATTERN_CATALOG["api_key"]` 用 `\b(?:api[_-]?key|...)\b` 开头。下划线是词字符,
所以 `ANTHROPIC_API_KEY=sk-...` 里 `API_KEY` 前面没有词边界,**这条形式不脱敏**。实测五种写法:
`api_key = "..."` ✅、`"api_key": "..."` ✅、`Authorization: Bearer ...` ✅、
`password = "..."` ✅、`export ANTHROPIC_API_KEY="..."` ❌。
这是上游缺陷(D6 第④类的候选),值得给上游提 issue;在上游修好之前 misaka 不自己改
vendored 目录,`tests/test_hermes_lcm_externalize.py` 只对已覆盖的写法立断言。

## 时间记忆(P4,host 侧,vendor 未动)

rollup 四件套(`rollup_store` `rollup_builder` `rollup_periods` `occurrence_time`)、
`lcm_recent` 工具、`lcm_rollups` 一族的表与触发器全在 vendor 里,`engine.py` 自己就会
按开关调用它们。所以 P4 接的东西只有一处半:

| 处 | 做了什么 |
|---|---|
| `host/rollups.py::nudge` | **上游只在 `_bind_lifecycle_state`(即 `on_session_start`)里排一次维护 pass,没有第二个钩子。** Hermes 网关频繁重绑;misaka 一个会话绑一次然后跑几小时,期间每轮压缩都发布新 summary 节点、把覆盖那些天的 rollup 打成 stale,却没有任何东西去消费。所以 `host/context_engine.compact()` 出摘要之后补一次 `nudge`(上游自己的 `_schedule_rollup_maintenance`,按 (库, scope) 去重、受 `LCM_ROLLUP_BUILDS_PER_PASS` / `LCM_ROLLUP_MAINTENANCE_BUDGET_MS` 限额)。关的时候是 no-op |
| `host/rollups.py::status` / `rebuild` | `misaka lcm rollups` / `--rebuild`。status 走**只读连接**(`RollupStore` 一构造就建表,查状态不能是"表出现"的原因);rebuild 按 summary 节点覆盖的 UTC 天播种(`upsert_stale_many`,一个 scope 一个事务)再循环 `run_rollup_maintenance` 直到没有 stale |

`rebuild` 存在的理由是**在已有历史的库上打开这一族**:触发器是打开后才装的,之前发布的
summary 节点没有留下 invalidation 事件,没有任何东西会自己排队 —— 不 rebuild 就永远回退 leaf。

**`rebuild` 的循环按"这一轮有没有改变什么"收尾,不按"这一轮有没有开工"**(合流审查改的)。
`run_rollup_maintenance` 返回的是 **builds_started**,而一个日 rollup 还没 ready 的聚合期
会被**每一轮**挑中、开工、再 defer —— 所以"这轮开工数为 0 才停"永远停不下来,只能撞
`_MAX_PASSES=400` 的上限,路上每轮最多两次辅助模型调用。改成比对 `status != 'ready'` 的
行数:健康库答案不变,卡住的库一轮就收手并把 scope 报进 `exhausted`(CLI 打
"still had work when the pass budget ran out; run it again")。
`tests/test_hermes_lcm_rollups.py::test_rebuild_gives_up_on_a_period_that_will_never_build`
守着这条,断言的是**尝试次数的上限**,因为那是有价钱的那个数。

### 依赖的上游形状(再同步时复核)

| 依赖 | 位置 | 变了会怎样 |
|---|---|---|
| `_bind_lifecycle_state` 里那段 `temporal_rollups_enabled` 判断 + `_schedule_rollup_maintenance` | `engine.py:1748-1755` | 上游若改成"压缩后也排一次",`nudge` 就多余了(仍然无害,scheduler 去重);若改名,`nudge` 会静默失效 —— 那时 rollup 只在会话绑定时更新 |
| `LCMEngine._config` / `._dag` | 全 host 层已依赖 | 同 P1 |
| `lcm_rollups` / `lcm_rollup_sources` / `lcm_rollup_invalidations` 的列名 | `db_bootstrap.py:711-760` | `status` 的只读查询和压测断言按名字读 |
| `summary_nodes.earliest_at/latest_at` = **入库时钟**(`messages.timestamp` 即 `ingested_at`),不是宿主消息时间戳 | `store.append:402`、`store.get_time_bounds:987` | rollup 按"这段历史是哪天**入的库**"归档,不是"消息自称哪天"。misaka 的测试要造跨天历史,唯一办法就是把 store 的时钟拨过去 |

### 多进程复核(D3):三条加固机制逐条的结论

上游这一族过了对抗审查,但审查的是**一个进程若干线程**。misaka 是多进程写同一个
`lcm.db`,所以逐条重看"跨进程还成立吗":

| 机制 | 跨进程结论 | 依据 |
|---|---|---|
| 构建令牌带不可重用 nonce | **成立**。`upsert_building` 的 `INSERT ... ON CONFLICT DO UPDATE` 在一个写事务里递增 `generation` 并换 `lease_nonce`,两个进程拿到的令牌必然不同 | 全部状态在库里,没有一个字节在进程内 |
| 迟到的构建者不能覆盖更新的状态 | **成立**。`mark_ready` / `mark_failed` / `defer_incomplete` / `resolve_no_source` 的 `WHERE` 全带 (`rollup_id`, `generation`, `lease_nonce`, `status='building'`) 四元 CAS,`rowcount==0` 即被抢占 | 同上;`reclaim_stale_building` 也递增 generation,所以崩溃进程回来也发布不了 |
| 删源致所有覆盖期 stale | **成立,而且比进程内更强**。invalidation 是 `summary_nodes` 上的**触发器 + 表**,装一次全库有效:一个**把这一族关掉**的姐妹进程删/改节点,事件照样入 outbox,开着的那个进程下一次 pass 会消费 | `db_bootstrap.ensure_temporal_rollup_invalidation_triggers` |
| 读侧不越过未消费的变更 | **成立**。`_recent_ready_rollups` 先查 `has_pending_invalidations(scope)`,有就整窗回退 leaf | `tools.py:2005-2011` |
| 进程内 scheduler / `try_acquire_rollup_operator_lease` | **不跨进程**(`_ROLLUP_MAINTENANCE_SCHEDULER` 是模块级)。两个进程会重复构建同一期 → **重复花一次辅助模型的钱**,不产生错数据(上面的 CAS 兜底)。所以 `host/rollups.rebuild` 干脆不去拿那把 operator lease:它只能把本进程挡在本进程外面 | — |

### 复核中发现的一个上游缺陷(可用性,非正确性;未修,vendor 不动)

`RollupStore.mark_ready` 是**这一族唯一一个"事务内先读后写"的路径**:Python `sqlite3` 的
隐式 `BEGIN` 落在那句 `DELETE FROM temp.lcm_rollup_publish_sources` 上(实测
`in_transaction` 从 False 变 True),之后的 `LEFT JOIN summary_nodes` 取的是 DEFERRED 快照,
最后那句 `UPDATE lcm_rollups` 要把读事务升级成写事务 —— 期间只要**另一个连接提交过**,
SQLite 立刻返回 `SQLITE_BUSY_SNAPSHOT`(`database is locked`),**`busy_timeout` 对快照冲突
不生效**,所以 30 秒的 `busy_timeout` 一点忙都帮不上。

后果:`build_day` 捕获后走 `mark_failed`,该期变 `failed` 并吃 30 秒退避,下一轮 pass 重建。
**没有脏写、没有错数据、没有丢历史,只是那一期晚 30 秒**;`lcm_recent` 期间照常回退 leaf。

实测(两进程 × 各三个构建者疯狂 hammer,无思考时间):6 轮里 3 轮出现,每轮 1-2 期。
真实使用是"每轮压缩排一次、彼此隔几秒",触发面小得多。

上游修法是一行:`RollupStore._init_db` 里给自己的连接 `isolation_level="IMMEDIATE"`
(或把校验查询挪进一个 `BEGIN IMMEDIATE` 事务)。**值得给上游提 issue**;misaka 不自己改
vendored 文件 —— 它是自愈的可用性问题,不值得为它打破 `vendor/` 零差异。

压测:`tests/test_hermes_lcm_rollups.py::test_two_processes_building_one_batch_of_rollups_leave_it_self_consistent`
(两进程 × 各三个并发构建者抢同一批 rollup;先断言竞态**当场**留下的状态自洽 —— 无 `building`
残留、无租约泄漏、`summary` 必是某一个进程的整句而非拼接、失败只可能是锁竞争或聚合等日;
再让竞争结束后的单进程收尾,断言每期 `ready`、血统与库里现存节点完全一致、日 rollup 的源集
恰好等于那天的节点、无孤儿 `lcm_rollup_sources`、outbox 排空)。

## 刻意不接线的 vendored 文件

| 文件 | 原因 |
|---|---|
| P2–P7 那 26 个闭包外溢文件 | 依赖闭包要求它们**可导入**,不要求它们**被接线**。host 适配层按分期做:P2 工具面、P3 保护层、P4 时间记忆、P5 语义、P6 证据层、P7 运维面 |
| `codex_routing.py` | 蓝图 §5.4:misaka 的模型目录负责上下文窗,永久 vendored-inert |
| `command.py` | Hermes 的 `/lcm` 斜杠命令实现。**P7 后已不在这张表里的多数条目之列**:P5 起 `host/embed.py` 转发 `embed`,P6 起 `host/assertions.py` 转发 `assertions rebuild`,P7 起 `host/operations.py` 转发 `status` `doctor` `rotate` `preset`。仍不转发的只有 `backup`(misaka 自己有一个)、`rollups`(`host/rollups.py` 要的是只读状态与重建循环,不是上游那个绑定会话的 `rebuild <kind> [date]`)与 `help`(讲的是 `/lcm` 不是 `misaka lcm`) |

## 语义检索(P5,host 侧,vendor 未动)

上游这一族的实现整个在 `vendor/embedding_provider.py` + `vendor/vector_store.py` 里,
`lcm_grep` 的 `semantic` / `hybrid` 两条路也整个在 `vendor/tools.py` 里 —— P2 已经把
`engine.handle_tool_call` 接上了,所以 P5 要接的**只有开关和运维口**,检索路径一行没写。

| 处 | 接了什么 |
|---|---|
| `host/config_bridge.py` | `MISAKA_LCM_RETRIEVAL_MODE=hybrid` → `LCM_EMBEDDINGS_ENABLED=true`;`MISAKA_LCM_EMBEDDING_MODEL` → `LCM_EMBEDDING_MODEL` + `LCM_EMBEDDING_PROVIDER=fastembed` |
| `host/embed.py` | `misaka lcm embed warmup\|backfill [--apply] [--limit N]` → `vendor/command.handle_lcm_command("embed ...")` |
| `misaka/cli/app.py` | `lcm` 子命令加一个 op(`embed`)和一个旗标(`--limit`) |

三条决定:

1. **provider 名和 model 名捆绑迁移。** 迷你实现只有 FastEmbed 一个后端,`MISAKA_LCM_EMBEDDING_MODEL`
   写的必然是 FastEmbed 模型名,所以别名同时补 `LCM_EMBEDDING_PROVIDER=fastembed`——但**只在
   `LCM_EMBEDDING_MODEL` 本身缺席时**。否则用户设了 `LCM_EMBEDDING_MODEL=voyage-4-lite` 却忘了
   provider,上游本该报"两个都要设",别名却会把它变成一个错的 provider。voyage / ollama 走上游名。
2. **默认仍是关的。** `lcm_retrieval_mode` 的默认值是 `fts`、`lcm_embedding_model` 的默认值是空,
   两半都映射到"什么都不加"。实测:默认配置下 `mode=semantic`/`hybrid` 返回
   `degraded_to_fts=true, degraded_reason="semantic retrieval is disabled"`,
   `misaka lcm embed warmup` 答 `status: disabled`,`backfill` 答 `status: refused`。
3. **`command.py` 只转发 `embed`。** 其余运维口(`status` `doctor` `rotate` `preset` ...)是 P7 的,
   `misaka lcm` 在那之前维持自己那几个 op。转发而不重写的理由是估算:dry-run 打的是
   token 数和费用,自己写一版算错了是钱。

`host/embed.py` 依赖两条上游形状,再同步时复核:`handle_lcm_command` 的 `"embed warmup"` /
`"embed backfill [flags]"` 词法,以及 `engine._config` / `engine._store.db_path` 两个属性名
(`command.py` 全篇都从这两个取)。

### 迷你实现的 `semantic.py` 与将来的收敛

迷你实现自带 137 行 `semantic.py`(FastEmbed + 自建 `summary_embeddings` 表 + Python 里
逐行余弦扫描),**没有删**:迷你实现仍是 `context_engine=lcm` 的默认实现,删了它默认路径就瘸了。
两者不共存于同一个库——`host/switch.py` 用 `messages.conversation_id` 一列判 schema,
一个库只可能是其中一版,所以不存在"两套向量表打架"。

| | 迷你 `semantic.py` | 上游 `vector_store.py` |
|---|---|---|
| 表 | `summary_embeddings(node_id, model, vector, norm)` | `lcm_embedding_meta` + 按 identity 分档的向量表 |
| 身份 | 一个 `model` 字符串 | `(provider, model, revision, dim, dtype, byteorder, task)` 七元组 |
| 后端 | 只有 FastEmbed | voyage / ollama / fastembed |
| 检索 | 全表扫,Python 循环算余弦 | 有界候选窗 + numpy 可选加速 + 二值预筛 + 两段 KNN |
| 写入 | 查询时顺手 backfill | 独立的 `embed backfill`,带租约、可续跑、对"远端是否收下"诚实 |
| 降级 | 抛 `SemanticUnavailable` | `degraded_to_fts` + 原因,永远还是给得出全文结果 |

收敛路径:P7 把 `misaka lcm` 的运维口整个换成上游的之后,`context_engine=lcm` 这个值本身
就没有存在理由了(它唯一的作用是保住旧库不被上游 schema 撞坏,而那时 `migrate` 已经跑过)。
那一步里 12 个迷你文件连同 `semantic.py` 一起删,`CFG["lcm_retrieval_mode"]`/
`CFG["lcm_embedding_model"]` 退化成纯别名(现在已经是了)。**在那之前不要动它。**

### 可选依赖 `lcm-semantic`

`pyproject` 的 `lcm-semantic = ["fastembed"]` 正好对上上游的 `fastembed` provider,
装上即通,不新增任何 misaka 依赖。它顺带把 **numpy** 带进环境——上游 `vector_store.py`
在有 numpy 时走矩阵路径、没有时走纯 stdlib 的有界扫描,两条路上游都测。副作用是
vendored 套件里 8 个「缺 numpy」的跳过变成了通过(见"vendored 测试"一节的新数字)。
`fastembed` 的模型下载只在 `misaka lcm embed warmup` 里发生,查询路径永远
`local_files_only=True`——没 warmup 过就 `degraded_to_fts`,不会在一轮对话中间下 130 MB。

## 证据层与预答钩子(P6/B,host 侧,vendor 未动)

V4 的编译器族(`answer_contract` `requirements_compiler` `evidence_compiler` `evidence_pack`
`selective_recall` `selective_compiler` `query_view_store` `reasoning` `adaptive_retrieval`
`trajectory_store`)全在 vendor 里,`lcm_compile_evidence` / `lcm_evidence_pack` /
`lcm_compute` / `lcm_query_state` / `lcm_retrieve` 五个工具 P2 就已经经
`engine.handle_tool_call` 接上了。所以 P6/B 要接的只有**上游插件入口里那段
`pre_llm_call` 钩子**,外加它暴露出来的一个围栏洞。

### 实测:五个工具在开关打开后确实工作

| 工具 | 开关 | 实测结果 |
|---|---|---|
| `lcm_compile_evidence` | 无(纯确定性,读调用方给的 ref) | `mode=auto` 出 `direct_fact {value: 4200, unit: "usd", exact_ref: "lcm:2:93-101"}` |
| `lcm_evidence_pack` | 同上 | `evidence[]` 带 `exact_ref` + `store_id` + `span_start/end` + `quote` + 观察时间/发生时间两套 |
| `lcm_compute` | 同上 | 缺 `operands` 时 `status=fallback`,`provenance.stages` 完整 |
| `lcm_query_state` | `LCM_ASSERTIONS_ENABLED` | 关:`{"status": "disabled", ...}`;开:正常应答 |
| `lcm_retrieve` | `LCM_ADAPTIVE_RETRIEVAL_ENABLED` | 关:`{"status": "disabled", "enable_with": ...}`;开:`start` 正常开一次 episode |

前三个**没有开关**,因为它们不检索、不调模型:只读调用方已经引用了的行。这不是遗漏。

### `host/preanswer.py`:上游 `__init__.py:91-311` 在 misaka 侧的重建

上游钩子返回 `{"context": recall_policy + "\n\n" + brief}`,宿主拿它当 system context。
misaka 的对应缝 `harn.on("context", ...)`(`transformContext`)拿到的是**消息表**,所以
brief 作为**一条追加的 user 消息**落地,表里其它对象一个不动(`is` 相同)。

四处**故意不照抄**,理由都在模块文档串里:

| 上游 | misaka | 为什么 |
|---|---|---|
| 恒返回 `recall_policy` 字节 | 关的时候返回 `None` | 这条缝上没有 policy blob;返回 `None` = 消息表原样,连对象都不换 |
| `enabled_toolsets` 里有没有 `context_engine` | 不判 | Hermes 的 payload 键;misaka 按卡权限词汇表管工具,引擎要么注册了要么没有 |
| `_engine_bound_session_id` / `_ensure_engine_bound_to_session` | `context_engine.start(ctx)` | 绑定归 `host/ingest` + `host/context_engine` 管,和 `externalize.stub_replay` 一个写法 |
| `payload["question_date"]` / `["question_as_of"]` | 只有回退那一半 | 见下 |

### question_date 的来源(决定)

上游 `_hook_question_date` 两条路:先取 payload 的 `question_date`/`question_as_of`,
取不到就用 `conversation_history` 里**最后一条 user 消息的 timestamp**,按 UTC 折成
`YYYY-MM-DD`。misaka 的 `context` 事件只有 `{"type", "messages"}`,**没有 payload 键**,
所以只有第二条路 —— 而那正好是描述"活的一轮"的那一半:显式 anchor 在上游也只有 benchmark
桥会喂(冻结日期复现搜索字节)。

实现:`ingest.upstream_messages(messages)` 反向找第一条 `role == "user"`,
`datetime.fromtimestamp(ts, tz=UTC).date().isoformat()`。**UTC 而不是本地时区**是被
`answer_contract.normalize_question_date` 逼的:它拒绝时区含糊的日期,anchor 和 contract
不同步 = 编不出 brief。时间戳缺席/不可读时 anchor 留空,**不拿墙钟兜底** —— 相对时间的问题
(「昨天」)在没有 anchor 时上游会拒答,拿墙钟兜底等于把一轮旧对话悄悄改了日期。

### 两个处理器共用 `context` 缝:顺序是硬约束

`extension.register` 现在往 `harn.on("context", ...)` 挂两个:
`transform_context`(P3 的 `externalize.stub_replay`)在前,`preanswer_context` 在后。

`ExtensionRunner.emit_context` 按注册顺序把每个处理器的 `messages` 串给下一个,而上游的
active-replay stubbing 受保护的 fresh tail 是**从给它的那张表的末尾数回来的**
(`engine.py:5294`:`eligible_end = len(messages) - protected_tail_count`)。所以先追加
brief 会把那条边界往前推一位,**掀开一条本该受保护的真消息**。
`tests/test_hermes_lcm_preanswer.py::test_appending_first_would_push_a_message_out_of_the_protected_tail`
把这条实测出来:同一张表,`fresh_tail_count=2` 时 stub 不动;末尾多一条,index 2 的
tool result 就被换成 `[Externalized tool output: ...]`。

反向不成立(brief 只追加、不改已有条目),所以正确顺序只有一个,并且有测试守着。

### 围栏交互:P6 打开的第五个洞(已修,host 侧)

**洞**:`requirements_compiler._deliver` 把水合过的候选**过滤成五个键**
(`exact_ref` `quote` `role` `session_date` `origin`)—— `store_id` 不在其中。于是
`lcm_compile_evidence` 的整个答案(`direct_fact`、`evidence[]`、`novel_exact_refs[]`、
`computation.citations[]`)以及预答 brief 的正文,**只用 `lcm:<store_id>:<start>-<end>`
字符串指认来源**,`fence._collect` 的键表一个也够不着。`MARKER in output` 这条捷径同样挡不住:
quote 是行正文**中段**的一个切片,围栏的两个哨兵不在里面。

实测(修之前):一个被围栏的页面入库,`lcm_compile_evidence(mode=auto)` 返回
`direct_fact {value: 4200, unit: "usd", exact_ref: "lcm:2:93-101"}`,`refence` **原样放行** ——
从敌意页面里抽出来的一个数,穿着系统自己的嗓音进模型。

**修法**(host 侧,vendor 未动):`fence.cited_rows(text)` 用一条正则
`\blcm:(\d+):\d+-\d+\b` 从**渲染后的文本**里读回 store 行,`refence` 把它并进 `stores`,
`preanswer._guarded` 用同一个函数守 brief。

从文本读而不是从字段读,是因为**文本是拼写唯一稳定的地方**:上面四种键名是同一条引用的四个
形状,下一个上游版本会有第五个。多读的代价是安全方向的 —— 一条被敌意行**引用**的 ref 会解析
到那一行,而那一行本来就得被担保。

实测(修之后):被围栏页面 → brief 带围栏;干净行 → brief 不带围栏(`lcm_evidence_pack`
本来就带 `store_id`,行为不变)。

| 新增依赖(再同步时复核) | 位置 | 变了会怎样 |
|---|---|---|
| exact ref 拼作 `lcm:<store_id>:<start>-<end>` | `requirements_compiler.py:354`、`evidence_pack.py:428`、`selective_recall.py` | 换格式 → 证据族的答案重新绕过围栏 |
| `_deliver` 过滤掉 `store_id` | `requirements_compiler.py:1953` | 上游哪天把 `store_id` 加回去,这条正则就成了冗余(仍无害) |

### 成本安全

`LCM_PREANSWER_EVIDENCE_ENABLED` 是 `inject` 读的**第一个**东西,关的时候连一次
`handle_tool_call` 都不发(`test_the_switch_is_read_before_anything_is_looked_up` 用一个
会抛的 `handle_tool_call` 桩证明)。整个测试文件的辅助模型桥是**一个会抛 `AssertionError`
的 `call_llm`**,29 条测试全绿即"这条缝一次模型都没调"。

这一族里唯一会调辅助模型的是 `selective_compiler` 的 selector(`vendor` 里全文 grep
`auxiliary_client`,证据族只有 `selective_compiler.py:288` 和未接线的 `host_evidence.py:245`),
它自己有 `LCM_SELECTIVE_COMPILER_ENABLED`,默认关,而且只在 `legacy_selective` 模式下、
路由已经判定这一问需要超出 baseline 之后才可能被调到。开启后 fail-open:selector 超时
只丢 brief,不丢这一轮。

开着的时候成本也是有界的:baseline 是**一次** `lcm_recall`(`limit=25`),
`requirements_compiler` 的 `max_retrieval_calls=2`、`max_added_context_tokens=850`,
超预算上游宁可丢 brief 也不截断。

### 研究流接缝(本期不实现)

misaka 的研究台账和这一族回答的是同一个问题(手上到底有什么在案的东西),整合要先定
「引用格式谁说了算」「研究 claim 要不要变成 LCM 断言」——都不是接缝问题,蓝图 §4 P6 明写
本期不做。注释落在 `host/preanswer.py` 模块文档串末尾:`_brief` 是唯一产出文本的地方
(台账段落将来并在它旁边),`_baseline` 是唯一决定编译器能看见什么的地方。

## 运维面(P7,host 侧,vendor 未动)

上游 `/lcm` 有 9 个子命令,`misaka lcm` 到 P6 末已有 10 个 op。逐个对完之后**实际缺的只有两个**
(`rotate` 与 `preset`),另有一处**分流**是本期真正的问题。

| 处 | 做了什么 |
|---|---|
| `host/operations.rotate` | `misaka lcm rotate [SESSION_ID] [--apply]` → `/lcm rotate [apply]`。上游转的是**它宿主当前打开的那个会话**,CLI 一个都没有,所以会话是参数;省略时列出库里的会话(`store.scan_session_cleanup_stats`,按消息数降序,最多 20 个)而不是干答一句 `no_active_session`。**绑定前先查这个会话在不在库里**:绑定会把找不到的会话**建**出来,一个拼错的名字会给只读预览留下一条空 lifecycle 行,正是上游 doctor 的 `empty_lifecycle_rows` 要报的碎片 |
| `host/operations.preset` | `misaka lcm preset show\|suggest\|apply [NAME] [--apply]` → `/lcm preset ...` |
| `host/operations.report` | `misaka lcm status` / `doctor` 的**分流**:`switch.selected()` 为真就走上游的 `_status_text`/`_doctor_text`,否则返回 `None`,CLI 保留迷你实现自己那份 |
| `host/operations._families` | 上游 doctor 后面补一段四族体检(下面单列) |

### 分流:两套实现,两种报告,不强行统一

`misaka lcm status` 到 P6 末走的一直是**迷你实现**的 `maintenance.py`——数行数、数节点。移植版被选中
的时候那份报告是错的对象:它对 rollup、嵌入、断言、外部化一无所知,而这些正是移植版存在的理由。

分流判据是 `switch.selected()`(即 `context_engine` 的值),**不是**磁盘上的 schema。理由:
schema 说的是"这个文件能被谁打开",选择说的是"谁在压缩这个装置的会话"。一个装了移植版 schema
却选了 `lcm` 的库,它的会话是迷你实现在压——报告就该是迷你实现的。四种组合的实测结果:

| `context_engine` | 磁盘 schema | 结果 |
|---|---|---|
| `hermes-lcm` | ported | 上游 `_status_text` / `_doctor_text` + 四族体检 |
| `hermes-lcm` | mini | `report()` 返回一句拒绝(`misaka lcm migrate --apply`),不打开库 |
| `lcm` | mini | `report()` 返 `None`,CLI 走迷你实现(P1 起就有的路径) |
| `lcm` | ported | 同上,落到 P1 那条"迁移过的库给计数"的兜底 |

**两套输出形状不统一是刻意的**(蓝图之外、本期决定):上游那份有六十来个字段,迷你那份有五行,
把它们压成同一张表等于扔掉上游报告的大部分。取而代之是 CLI 在第一行打一句
`# ported hermes-lcm engine | misaka lcm doctor` / `# pre-port mini implementation | ...`,
让"在看哪一套"这个问题一眼有答案。

### doctor 的四族体检:上游为什么不报,host 为什么补

`_doctor_text`(480 行)覆盖 schema、integrity、两张 FTS、payload 风险扫描、**外部化载荷**、
敏感串脱敏、lifecycle 碎片、source 血统。它**不覆盖** rollup、嵌入、断言——这三族都比它晚,
上游至今没回头给它加。`lcm_inspect` 里有 `_temporal_rollups_status`,但那是工具面,不是 doctor。

所以 host 在上游 doctor 后面接一段 `families:`,四族各一行:开关状态 + 计数 + 深查那一族的命令。
读的都是现成的东西——rollup 用 `host/rollups.status()`(只读连接,不能是表出现的原因),
嵌入/断言用引擎**自己已经开着的**那个连接跑几条 `SELECT`(`SELECT` 不会建表,所以查状态
仍然不会改变状态)。表不存在 = 那一族在这个库里从没打开过,一律当"空"读,不当错误读。

两个判据值得留意:

- 嵌入这一族"开着但还没 warmup"是唯一会让操作者**静默无效**的状态(检索照常回退全文),
  所以它单独一行(`no profile registered yet`),不靠一个 0 计数暗示。
- 断言只报**不等于** `CURRENT_EXTRACTION_VERSION` 的版本行:那才是"该重建了"的信号,
  "全都是当前版本"上面的总数已经说过了。

### `preset apply` 的 `--apply`:唯一一处不原样转发上游文本

上游 `preset apply` 在**任何模式下都不写配置**:不带 `--dry-run` 时它答
`status: denied / error: preset apply is preview-only for now; pass --dry-run`。
把这句原样转给 misaka 操作者,他会去找一个 `misaka lcm` 根本没有的 `--dry-run` 旗标
(这边 dry run 是默认)。所以 `preset apply` 一律附 `--dry-run` 转发,`--apply` 时在末尾补一句
host 自己的话说明"没有东西可提交"。**这是四个转发口里唯一一处对上游文本的加工**,而且只是追加,
不改上游写的任何一个字节。

其余三个口的文本原样打出,包括里面提到的 `/lcm X`——README 的运维节说明了读作 `misaka lcm X`。
翻译它需要对上游文本做字符串替换,那是再同步时最容易无声出错的一类改动,不值得为观感付。

### 依赖的上游形状(再同步时复核)

| 依赖 | 位置 | 变了会怎样 |
|---|---|---|
| `handle_lcm_command` 的 `status` / `doctor` / `rotate [apply]` / `preset show\|suggest\|apply [name] [--dry-run]` 词法 | `command.py:4991-5068` | 词法改名 → 对应 op 落到上游的 help 文本上(不报错,但也不干活) |
| `engine.rotate_active_session` 只认 `on_session_start` 绑过的会话 | `engine.py:6347` | 上游若改成接收 session 参数,`operations.rotate` 的绑定这一步就多余了 |
| `store.scan_session_cleanup_stats()` 的四元组 `(session_id, message_count, token_total, node_count)` | `store.py:867` | 列变了 → 会话列表解包失败 |
| `lcm_embedding_profile` / `lcm_embedding_vectors` / `lcm_chunk_vectors` / `lcm_assertions` 的列名 | `db_bootstrap.py:1295-1339`、`:1890` | 体检那几行的计数静默变 0(表不存在被当成"没开过") |
| `assertion_store.CURRENT_EXTRACTION_VERSION` | `assertion_store.py:34` | 它一动,库里所有行就都成了"superseded",体检会提示重建——这正是想要的行为 |

### 蓝图 §4 P7 里本期**没**做的

- **聊天内 `/lcm` 扩展命令**:`misaka lcm` 这条 CLI 已经把全部运维口接完了,聊天内再开一条
  是第二个入口,不是第二份能力。蓝图列了它,但它不属于"缺口";要做的时候
  `host/operations.py` 的四个函数就是现成的实现。
- **`benchmarking/` 与 `scripts/`**:蓝图 §5.6 明写"留作 P7 可选";没移植,所以
  `test_stress_release_check.py` 等几个上游测试仍在下面那张 skip 表里。
- **`codex_routing.py`**:蓝图 §5.4,永久 vendored-inert,本期确认不变。

## 刻意不移植的上游文件

| 文件 | 原因 |
|---|---|
| `__init__.py`(554 行) | Hermes 插件入口(能力嗅探 + `ctx.register_tool` + 斜杠命令注册)。蓝图 §5.1/§5.2:它的每一段都由 `host/` 重建。`vendor/__init__.py` 是 misaka 自己的包初始化,不是它的拷贝 |
| `plugin.yaml` | Hermes 插件清单。misaka 不读它;vendored 进来只会让 `lcm status` 报告一个 misaka 并不具备的插件身份。代价是 `runtime_identity` 报 `plugin_version: unknown`,6 个上游测试因此跳过(见下表) |
| `README.md` / `docs/` / `.github/` / `scripts/` / `benchmarking/` / `skills/` | 蓝图 §5.5/§5.6;`skills/` 与 `scripts/` 的必要部分在 P2/P7 各自决定 |

## vendored 测试

`tests/hermes_lcm_vendor/` 有上游 84 个 `test_*.py` 里的 **64 个**(逐字节,`cmp -s` 验证),
外加上游的 `tests/fixtures/`。结果:**2407 passed / 21 skipped / 12 xfailed,零红。**
(P5 装上可选 extra `lcm-semantic` 之后的数字;numpy 随 fastembed 进来,原先 8 个
「缺 numpy」的跳过变成了通过——见下。P6 补拷 4 个模块解锁了 4 个测试文件、36 个用例,
2371 → 2407,跳过数不变。没装 extra 时按同样的 +36 平移:2393 passed / 35 skipped。)

`tests/hermes_lcm_vendor/conftest.py` 是 misaka 的文件(不是上游拷贝),做三件事:

1. `sys.modules["hermes_lcm"] → vendor` 包别名,并**把每个子模块也预先注册成同一个对象**。
   只别名包不够:`from hermes_lcm import db_bootstrap` 会经包的 `__path__` 把文件**再导入一遍**,
   一个源文件两个活模块对象,而上游测试靠 monkeypatch 模块级名字来观察调用,间谍装在
   另一份拷贝上就永远看不见。
2. 日志器树同理由包名派生:上游 `logging.getLogger(__name__)`,测试用字面量
   `caplog.at_level("INFO", logger="hermes_lcm.engine")` 取回来,所以把这个名字指向同一个
   Logger 对象。
3. `SKIP_UPSTREAM_TESTS`:25 个"上游能跑、这个检出跑不了"的测试,连原因一起列在那里
   (不改测试文件,不整文件丢弃)。分类:插件入口 6、`plugin.yaml` 6、README 2、`scripts/` 3、
   缺 numpy 6、macOS `/var`→`/private/var` 2。后 8 个在上游自己的检出里也是红的
   (同一 venv、同一 8 个 node id,已核对)。

   **其中「缺 numpy」那 6 个是有条件的**(P5 改):numpy 不是 misaka 的依赖,但可选 extra
   `lcm-semantic` 的 fastembed 会把它带进来。带进来了,那 6 条就不再是关于环境的论断,
   无条件跳过就成了藏红——所以 conftest 先 `import numpy` 再决定跳不跳。其余 19 条无条件。

### 再同步时怎么证明"一个测试都没被藏起来"

跳过一个测试和改一个测试是同一件事,所以 `SKIP_UPSTREAM_TESTS` 需要一条能重跑的证明,
而不是一句承诺。两步,都是机械的:

1. **同一 venv 跑上游的同一批 64 个文件**当基线:

   ```
   cd ~/.hermes/plugins/hermes-lcm && PYTHONPATH=~/.hermes/hermes-agent \
     <misaka>/.venv/bin/python -m pytest -q $(那 64 个文件)
   ```

   当前 pin 上、装了 `lcm-semantic` 的结果:**2 failed / 2424 passed / 2 skipped / 12 xfailed**,
   那 2 个 node id 正是 `_SYMLINK`——即"上游在这个环境里也红"的那一类。
   (没装 extra 时是 **8 failed / 2410 passed / 10 skipped / 12 xfailed**,多出来的 6 个红是 `_NUMPY`。)

2. **对账**:上游 `2424 passed + 2 failed = 2426`;这里 `2407 passed + 19 skipped = 2426`。
   两边的 `2 skipped` / `12 xfailed` 也逐项相同。**总数相等**就是"没有测试凭空消失"的证明;
   对不上就是有文件或用例被悄悄丢了。(没装 extra 时两边都是 2418 = 2410+8 = 2393+25。)

把 `SKIP_UPSTREAM_TESTS` 整个停用再跑一遍(临时插件把 skip marker 摘掉即可),应当**恰好**
红 19 个(没装 extra 时 25 个)、且 node id 与表里逐条对齐:一个都不多(说明没有藏红),
一个都不少(说明没有多跳过本来能过的测试)。上游修好某条后,对应条目会从"红"变"绿",那时删掉它。

**暂缓的 20 个上游测试文件**:

每一行的"缺什么"都是**实测**得来的:把文件拷进来跑一遍,记下第一个错。13 个文件在
collect 阶段就 `ImportError`(缺模块),11 个能 collect 但**全部**用例失败(缺磁盘上的资产)。
没有任何一个文件是因为 vendored 引擎本身的逻辑而失败——这是"暂缓"与"藏红"的分界线,
再同步时应重跑这张表确认它仍然成立。

| 文件 | 缺的东西(实测) |
|---|---|
| `test_import_lossless_claw.py`(145) | `scripts/import_lossless_claw.py`;蓝图 §5.3 明确不移植 |
| `test_packaging_install.py`(38) | `scripts/install.sh`(+ 插件清单 + 插件入口) |
| `test_historical_externalization_backfill.py`(33) | `scripts/backfill_externalized_tool_outputs.py`(P3/P7) |
| `test_host_capability.py`(10) | 插件入口 `__init__.py`(按路径加载);蓝图 §5.1 不移植 |
| `test_stress_release_check.py`(9) | `scripts/lcm_stress_check.py` **和** `import benchmarking`(P7) |
| `test_release_workflow.py`(6) | 上游 `.github/`(workflows + release-notes)**和** `docs/operator-guide.md` |
| `test_tool_contracts.py`(4) | `plugin.yaml`(+ 上游 README)(P2 决定) |
| `test_recall_guidance.py`(3) | `skills/hermes-lcm/SKILL.md` **和** 插件入口 `__init__.py`(P2) |
| `test_benchmarking_cli.py`(11) | `scripts/lcm_benchmark.py`——**不是** `import benchmarking`,与下面五个不同因 |
| `test_threshold_full_sweep_benchmark.py`(1) | `scripts/benchmark_threshold_full_sweep.py`——同样**不是** `benchmarking/` |
| `test_benchmarking_fixtures/replay/report/steady_state/types.py`(5 个) | `import benchmarking`(P7 可选) |
| `test_h3_composition_replay.py` `test_h5_state_semantic_replay.py` `test_longmemeval_harness.py` `test_state_embedding_backfill_cli.py` | `import benchmarking` |
| `test_int8_two_stage_knn.py` | `import numpy`;P5 的决定是**不为它新增依赖**——numpy 只在可选 extra `lcm-semantic` 里搭 fastembed 的车进来,不是 misaka 的依赖,所以这个文件仍然暂缓 |

(这张表原本有 24 行。P6 补拷 `preanswer_evidence` / `selective_compiler` / `selective_recall` /
`host_evidence` 之后,`test_preanswer_evidence.py` `test_selective_compiler.py`
`test_selective_session_bundle.py` `test_host_supplied_evidence.py` 四行删除——36 个用例
原样拷进来,**一个字节没改就全绿**,不需要任何 skip 条目。)

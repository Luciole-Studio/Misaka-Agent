# hermes-lcm 移植记录

上游 pin 见 `UPSTREAM_COMMIT`。本文件是**再同步契约**:`vendor/` 下每一处偏离上游的
改动都必须在此登记,升级时逐条重放。没有记录的漂移 = 缺陷。

施工蓝图:`docs/plans/hermes-lcm-port-plan.md`。

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
| — | — | — | **无。P1 结束时 `vendor/` 下 56 个上游 `.py` 全部与 pin 版本逐字节一致(`cmp -s` 逐个验证)** | 每次再同步后重跑 `cmp -s` |

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

`vendor/` 下 56 个上游模块 = **P1 的 30 个文件 + 它们 import 闭包外溢出的 25 个 + `command.py`**。
闭包用脚本从 P1 的 30 个种子出发递归展开(顶层 import 与函数内 import 都算),不是手数的。

| 期 | 文件 |
|---|---|
| P1(在范围内) | `config` `presets` `chunking` `fresh_tail` `message_analysis` `message_content` `message_patterns` `session_patterns` `search_query` `guidance` `tokens` `sqlite_util` `db_bootstrap` `store` `dag` `lifecycle_state` `maintenance` `reset_state` `runtime_identity` `engine` `compaction` `reconcile` `placeholder_ledger` `aux_session` `bypass` `engine_registry` `escalation` `extraction` `model_routing` `sanitize` |
| P2(闭包外溢) | `schemas` `tools` `retrieval_core` `diagnostics` |
| P3(闭包外溢) | `ingest_protection` `externalize` |
| P4(闭包外溢) | `rollup_store` `rollup_builder` `rollup_periods` `occurrence_time` |
| P5(闭包外溢) | `embedding_provider` `vector_store` |
| P6(闭包外溢) | `adaptive_retrieval` `answer_contract` `assertion_extraction` `assertion_rebuild` `assertion_state` `assertion_store` `evidence_compiler` `evidence_pack` `query_view_store` `reasoning` `requirements_compiler` `trajectory_store` |
| P7 | `codex_routing`(闭包外溢) `command`(见下) |

`command.py` 不在闭包里(没有任何闭包内模块 import 它),是**为了让上游 11 个测试文件能跑**
而额外拷进来的:它的 import 面整个落在闭包内,零第三方依赖,拷进来不接线的代价是零。

上游 60 个顶层模块里**没有** vendored 的 4 个,都是 P6 的叶子,闭包够不到:
`host_evidence` `preanswer_evidence` `selective_compiler` `selective_recall`。

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
| `extension.py` | 事件订阅(ingest + 压缩);P1 不注册任何工具 |
| `migrate.py` | D4 迁移器,挂在 `misaka lcm migrate [--apply]` |

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

## 刻意不接线的 vendored 文件

| 文件 | 原因 |
|---|---|
| P2–P7 那 26 个闭包外溢文件 | 依赖闭包要求它们**可导入**,不要求它们**被接线**。host 适配层按分期做:P2 工具面、P3 保护层、P4 时间记忆、P5 语义、P6 证据层、P7 运维面 |
| `codex_routing.py` | 蓝图 §5.4:misaka 的模型目录负责上下文窗,永久 vendored-inert |
| `command.py` | Hermes 的 `/lcm` 斜杠命令实现;misaka 走 `misaka lcm` CLI(P7) |

## 刻意不移植的上游文件

| 文件 | 原因 |
|---|---|
| `__init__.py`(554 行) | Hermes 插件入口(能力嗅探 + `ctx.register_tool` + 斜杠命令注册)。蓝图 §5.1/§5.2:它的每一段都由 `host/` 重建。`vendor/__init__.py` 是 misaka 自己的包初始化,不是它的拷贝 |
| `plugin.yaml` | Hermes 插件清单。misaka 不读它;vendored 进来只会让 `lcm status` 报告一个 misaka 并不具备的插件身份。代价是 `runtime_identity` 报 `plugin_version: unknown`,6 个上游测试因此跳过(见下表) |
| `README.md` / `docs/` / `.github/` / `scripts/` / `benchmarking/` / `skills/` | 蓝图 §5.5/§5.6;`skills/` 与 `scripts/` 的必要部分在 P2/P7 各自决定 |

## vendored 测试

`tests/hermes_lcm_vendor/` 有上游 84 个 `test_*.py` 里的 **60 个**(逐字节,`cmp -s` 验证),
外加上游的 `tests/fixtures/`。结果:**2357 passed / 35 skipped / 12 xfailed,零红。**

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

### 再同步时怎么证明"一个测试都没被藏起来"

跳过一个测试和改一个测试是同一件事,所以 `SKIP_UPSTREAM_TESTS` 需要一条能重跑的证明,
而不是一句承诺。两步,都是机械的:

1. **同一 venv 跑上游的同一批 60 个文件**当基线:

   ```
   cd ~/.hermes/plugins/hermes-lcm && PYTHONPATH=~/.hermes/hermes-agent \
     <misaka>/.venv/bin/python -m pytest -q $(那 60 个文件)
   ```

   当前 pin 上的结果:**8 failed / 2374 passed / 10 skipped / 12 xfailed**,
   那 8 个 node id 正是 `_NUMPY`(6)+ `_SYMLINK`(2)——即"上游在这个环境里也红"的那一类。

2. **对账**:上游 `2374 passed + 8 failed = 2382`;这里 `2357 passed + 25 skipped = 2382`。
   两边的 `10 skipped` / `12 xfailed` 也逐项相同。**总数相等**就是"没有测试凭空消失"的证明;
   对不上就是有文件或用例被悄悄丢了。

把 `SKIP_UPSTREAM_TESTS` 整个停用再跑一遍(临时插件把 skip marker 摘掉即可),应当**恰好**
红 25 个、且 node id 与表里逐条对齐:一个都不多(说明没有藏红),一个都不少(说明没有
多跳过本来能过的测试)。上游修好某条后,对应条目会从"红"变"绿",那时删掉它。

**暂缓的 24 个上游测试文件**:

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
| `test_int8_two_stage_knn.py` | `import numpy`,misaka 不新增第三方依赖(P5 决定) |
| `test_host_supplied_evidence.py` `test_preanswer_evidence.py` `test_selective_compiler.py` `test_selective_session_bundle.py` | 分别 import `host_evidence` / `preanswer_evidence` / `selective_compiler` / `selective_recall`——正好是上面"没有 vendored 的 4 个"。P6 一并拷入即解锁 |

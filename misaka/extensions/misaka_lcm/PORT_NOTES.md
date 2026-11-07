# MISAKA LCM fork 契约

## 2026-11-07：行说清是谁说的，检查点记住自己的摘要

改动在 `host/`，外加 `core/moments.py`、`core/network/messages.py`、`core/wiring.py` 各一小处；`vendor/`、`native/` 不变。

- **归档角色。** `convertToLlm` 把所有自定义消息变成 user 回合，对模型是对的，对上游是错的：摘要标签、自动焦点、保留任务目标、自动召回的查询词都把 user 行读成用户在提要求。真实会话里约 2,700 条通知和邮件对 1,041 条工作指令，一条待办提醒就成了摘要焦点。`ingest.archive_role` 在唯一入口改写：不是任务消息（`is_task_message`，即 `moments.TURN`）的，写成 `notice`；带 `moments.SENDERS` 的写成 `agent:<发信人>`。上游读不到 `user`，也就不用逐处改；`[{role.upper()}]` 的标签自己变成 `[NOTICE]`、`[AGENT:10032]`。
- **角色只在归档里。** 发给模型的是 `Replay.restore` 还原的原始消息，带来源的行还原，不带来源的内部角色行在 `restore` 里报错，发不出去。`Replay` 和 `upstream_messages` 都走 `_rows`，所以两边角色一致。
- **邮件不算任务。** 发信工具写明 "A message does not authorize new work"，邮件也按不可信内容处理，所以 Last Order 发给 Sister 的信是 `agent:last-order`，不升级成 `user`，否则不可信内容可能被保留成任务目标。
- **旧邮件是 `notice`。** 2026-11-07 之前的邮件 details 里没有发信人。文字里的 `<from>` 还在，这里不解析。
- **`lcm_grep` 的 `role` 参数是唯一和上游合约不同的地方。** `agent:<name>` 不是封闭列表，所以去掉 enum，`ingest.install` 放行这两种角色。多人同批的邮件角色里是逗号分隔的全部发信人，精确过滤查不到它们（真实 568 批里 23 批）。
- **`source` 分得清节点了。** `SessionSpec.node` 让研究节点的 Last Order 写成 `last-order@<节点>`，和卡片的 `name@<card>` 同一形式。
- **`expand_hint` 不再当查重键。** 恢复节点时提示取自检查点的 `frame`，也就是摘要器写的那句。过去这里写 `MISAKA session compaction ...` 同时用来再次找到节点，分叉自己压缩时就把这串文字画进了帧。pi 原生压缩的导入记录没有 `frame`，仍用这串文字，`_messages` 靠它判断是否已导入。
- **检查点记住自己的摘要。** `contextSummaries` 过去按“这行没有来源”判断，这只对本轮刚写的摘要成立。只重放的检查点现在按位置带上上一个检查点的标记，做法和 `nativeMetadata` 相同。
- **`misaka lcm repair` 修旧检查点。** 56 个重放检查点没有这个字段，摘要显示成用户消息（B30）。修复只用检查点自己记录的 `frame` 精确定位，不读文字猜，匹配不全的不动；默认只报告，`--apply` 才写，并把原文件复制到 `state/lcm-repair-<时间>/`，运行中的会话跳过。这个命令要手动运行。
- **缓存格式升到 3。** 行的角色变了，旧缓存要重建。
- 回归在 `tests/test_lcm_archive_roles.py`、`tests/test_lcm_fork_frames.py`。

## 2026-11-07：摘要不带输出上限

改动在 `host/llm.py`，`vendor/`、`native/` 不变。

- **为什么。** 上游的上限按正文算（第一级是预算的 2 倍，第二级等于预算，预算最低 2000；汇总节点的预算最低 1000），但 adaptive 思考和正文共用这个上限。`thinkingLevelMap.off` 是 `null`，思考关不掉，请求发的是 `effort: high`。真实重放 36 次有 27 次停在 `length`，其中 2 次只有思考没有正文。
- **两种后果。** 正文被截断时，`_call_llm_for_summary` 不看停止原因，直接当成摘要；正文为空时不算异常，也不留日志，连续两次就让路由熔断 5 分钟，这段时间该进程的压缩都退回原文截断。
- **做法和 Hermes 自己的压缩器一样。** `native/context_compressor.py` 不传 `max_tokens`，注释写的就是这个原因。`install` 换掉 `_call_llm_for_summary`，不换 `agent.auxiliary_client`：`lcm_expand_query` 也用 `compression` 这个任务名，它的 `max_tokens` 是对调用方承诺的回答上限。
- **不用另外拒收截断。** 不传上限时实际发的是模型上限（128000），"必须比原文短"的检查还在。
- 回归在 `tests/test_lcm_summary_limit.py`。

## 2026-11-06：图片留在会话文件里，不再进缓存

改动在 `host/images.py`、`host/context_engine.py`、`host/storage.py`，`vendor/`、`native/` 不变。

- **为什么。** 上游把内联图片写成库旁的 payload 文件，因为 LCM 是它的宿主唯一的记录。misaka 的记录是会话文件，这就成了第二份拷贝：`my-research` 的缓存 5.4 GB 里有 5.1 GB 是这些图片，而 111 次真实 LCM 调用没有一次读回过。模型的上下文由 `ingest.Replay` 从会话取回图片，摘要器本来也只看到占位符，所以都不受影响。代价是 LCM 工具取不回图片，要回会话文件取。
- **入库时换成标记。** `images.install` 包住 `protect_message_for_ingest`，所有写入都经过它（`store` 自己导入了这个名字，要一起换）。
- **所有比较也要用同一个标记。** `NativeLCMEngine` 覆盖 `_message_replay_identity` 和 `_session_end_prefix_compare_content`。压缩靠内容把消息对应回行，只修对账入口时，45 个真实会话的摘要静默少覆盖 8% 的 entry，不报错。
- **标记必须对 `fence` 保持不透明。** 写成 `Externalized ...; ref=` 的样子，但不是上游能加载的占位符。写成普通文字时，图片行上的摘要会失去 fence，而 agent 转写的图片内容就在摘要里。
- **缓存格式升到 2。** 旧行存的是 payload 引用，和新标记比不上，会话结束要多花 20–40 秒。首个独占的进程发现旧格式就丢弃重建；有别的进程在用时不动，等下一个首占者。

## 2026-11-06：分叉保留继承的摘要帧

改动在 `host/context_engine.py` 和 `host/tools.py`，`vendor/`、`native/` 不变。

- **帧不再改写，编号在工具入口翻译。** 帧上的编号是画出它时的数据库编号，分叉把父会话的前沿导入成自己的节点，缓存重建又给所有节点新编号，而上游工具只打开本会话当前的编号。过去 `_checkpoint_nodes` 把帧改成新编号，这有四个代价：每次都把整份上下文写进会话文件（`test` 的 48 个会话一轮约 34 MB）；提示词缓存断掉；摘要器写的详情提示被换成 `MISAKA session compaction …`；而且只能成功一次，因为 `LCM replay update` 不记录 `scaffolds`（真实数据 56 个全是空的），下次重建时找不到位置，帧就再也打不开（真实 `test` 会话缓存清空后 0/65 能打开）。现在帧一字不改，`_checkpoint_nodes` 只按检查点记录把“帧显示的编号 → 本会话节点”记进内存，`local_nodes` 在工具入口翻译，`_guarded_summary` 读帧时也先翻译。上游只用正则识别帧，不读里面的编号。`details.lcm.scaffolds` 现在只有 `contextSummaries` 用，但已安装的旧版本读会话时还要它，所以继续写。
- **检查点记录实际显示的帧。** `compact` 写 `records` 时用 `_frame_shown`：引擎这轮没有重画这个节点时帧还是保留的那份，记录也要写这一份，否则重开后编号就对不上了。判断看引擎有没有画出自己的帧，不看保留的帧在不在没有来源的行里：replay update 里每一行都有来源。分叉自己压缩后，上游重画的帧用本地编号。
- **这不等于缓存已经命中。** 用真实分叉实测（s2a，Haiku 5.5）：父请求到首个分叉读 0，因为阶段工具不同，`tools` 在缓存前缀最前面；第二个兄弟分叉也只读到约 4.6 万，因为继承内容末尾没有缓存断点。在该位置补一个断点后读到 249,248、写 5,645（真实记录读 53,276、写 198,098）。那个断点属于 `misaka/ai/api/anthropic_messages.py`，不在本次改动内。

## 2026-11-06：缓存跨运行保留，库与会话对不上时按会话文件重建（R38、R27 收尾）

改动全在 `host/`，`vendor/`、`native/` 不变。

- **缓存不再随进程退出删除。** 此前最后一个进程退出就清空，已结束的卡片从此搜不到，`my-research` 磁盘上 210 个会话而库里 0 行；重置过的库里旧 `store_id` 失效（10-25 t_178ad8）；一次运行的进程同时退出时还排队抢 gate（10-27 39 次 30 秒超时）。现在 `storage.release` 只放开租约，不碰 gate。这推翻 2026-09-15 的“退出即删”，但“会话文件才是永久记录、缓存随时可丢”不变。代价是占磁盘，见下面图片一节的改动。清理用 `misaka lcm clear`（`storage.clear`，有进程在用时拒绝）；只读的 `misaka lcm status` 打开的空缓存随即清掉，不留痕。
- **重新绑定时不再靠上游对账。** `_messages` 用 `entry_rows` 的绑定直接算出库里有多少行是会话的前缀（`_rows_held`），把游标设到准确位置。上游 `_reconcile_ingest_cursor_from_store` 只能推断：库和会话对不上时实测 170 秒以上未结束或让回合失败（R38），单条消息的会话证不出游标，每次重开多一行（上游 #631 同族）。库里的行不是干净前缀（未绑定、同位置两行、不在存档里）就删掉该会话的行和节点，从会话文件重建；入库仍失败再重建一次，第二次才报错。误判的代价只是重建一次：`store_id` 变，内容不丢。判定在真实实时写入的库（`final-test` 62 个会话）上 0 误判。`/tree` 留下的分支不满足前缀，会重建一次，之后行就是完整存档。
- **旧检查点兼容。** 2026-10-26 之前写的检查点把重试已删除的 503 空回复记成来源，这类条目没有行，`_checkpoint_nodes` 现在跳过被 `context_edit` 删除的引用。`my-research` 10 个重建失败的会话因此恢复。
- **删除的会话从缓存消失。** `entry_rows` 新增 `session_files`，绑定时记下会话文件路径；项目第一个进程打开时，文件已不存在的会话连行带节点删除。不用会话目录判断，目录可能不含子代理会话，会误删。
- **打不开的库自动换新。** 退出不再清理之后，损坏的库不会自己消失：打开前先探测（`_readable`），打不开且没有其他进程在用就换一个空缓存。
- 回归在 `tests/test_lcm_self_repair.py`。

## 2026-10-28：图片按张计价（U11）

- `vendor/tokens.py` 的 `count_message_tokens` 把结构化内容整段 `json.dumps` 后按文本计，图片的 base64 也算进去：一张 670 KB 的页面截图算成 475,853 token（实际一两千）。每轮首请求前的 `should_compress_preflight` 用它判断，LO 的 1M 窗口在真实用量 33% 时就压缩了；图片更多时还会被判为超出组装上限、强制溢出恢复（R44）。上游 hermes-lcm 至 a13e709 未改，也没有 issue。
- 改法照搬 Hermes 自己的估算器（`native/model_metadata.py` `estimate_messages_tokens_rough`）：图片部件按 `_count_image_tokens` 数张数，乘以 `current_image_token_cost()`（宿主在 `host/execution.py` 按模型绑定学到的单价，未校准时 1500）；文本照旧，计数前用 `_wire_message_shadow` 把图片换成同样的占位。只有列表内容走这条路，纯文本和无图列表的计数与上游逐一相同。这是新的 vendor→native 接缝：按 `__init__.py` 的先例用绝对路径导入，三个名字登记在 `vendor/dependency-contract.json` 的 `external_imports.misaka`。
- 回归在 `tests/test_lcm_image_tokens.py`：真实尺寸的图片按单价计、跟随学到的单价、无图内容计数与上游相同、大图不再触发 preflight。
- 上游 `vendor/tests/test_lcm_core.py::TestTokens::test_count_message_tokens_normalizes_content_parts` 断言的正是旧行为（带图片的部件列表与它的 JSON 文本计数相同），与 U05 的旧断言同类：原版测试文本不改，这一条在本 fork 下失败是预期的。

## 2026-10-17：表层——会话身份、工具文案、结果出口、未启用的工具

核心源码未改：`vendor/`、`native/` 逐字节不变，改动全在 `host/`、`__init__.py`、技能文档和打包配置。

- **会话身份写入 source 列。** 绑定引擎时传给上游的 `platform` 从常量 `misaka` 改为会话自己的地址（`host/context_engine.py`、`host/carry.py` 两处）：`last-order`、Sister 名字，卡片会话加 `@<卡号>`（`momo@t_3fa9c1`），子代理加 `/<agent id>`（父会话 `Agent` 调用返回的 id；子代理与父会话同名，不加就分不开）。上游把它写进每行的 `source` 列，所以每条命中都带上是谁说的，`lcm_grep(source=...)` 的精确过滤随之可用。地址由 `activate(spec)` 从 `SessionSpec` 取得（`misaka_lcm.identity`），经 `storage.ProjectContext.lcm_source` 传到引擎；`misaka lcm` CLI 没有会话，仍为 `misaka`。上游对 platform 的其余用途不变：会话匹配键（`LCM_IGNORE_SESSION_PATTERNS`、`LCM_STATELESS_SESSION_PATTERNS` 现在能按地址写）、前台重绑记账、辅助会话 bypass 集合；引擎只比较它在同一会话内是否变化，不判断取值。重放对账（`reconcile.py`）不看 source。
- **工具文案。** 上游说明里有 7 处指向 Hermes 的 `session_search`（misaka 没有这个工具），`lcm_recall` 自称搜索"agent 的全部记忆、所有对话、所有时间"，`source`/`conversation_id` 参数举的是 cli/discord/telegram 和 Discord 频道。`host/tools.py` 的 `_REWRITES` 按上游原句逐句改写或删除，作用于工具说明、参数说明和召回规则；原来只替换工具说明里的 "Hermes"，参数说明里的 "Hermes-tracked" 漏掉了。每条原句由测试核对仍逐字存在于上游，换上游版本时改了措辞会直接失败，而不是悄悄漏改。召回规则前言改写为一段：缓存的真实范围，以及按 `source` 找别的会话的完整做法（原来的做法要求 `session_id`，Sister 拿不到）。
- **结果出口 `_deliver`。** 所有工具结果先过这里再进 `fence.refence`：（1）JSON 以 `ensure_ascii=False` 重新序列化，中文不再是 `\uXXXX`（一个汉字 6 个字符）；含孤立代理项、无法编码为 UTF-8 时保留上游转义。（2）单次结果上限 25k token（`RESULT_BUDGET_TOKENS`），按模型实际收到的文本计：连同 fence 的外框，以及内容里每个标记被 defang 后加长的部分——抓了大量网页的会话，一页里可能有上百个标记。上游只限每一项（行数、每行字符、命中数），不限乘积，默认 `lcm_load_session` 一页就可能是 100 行 × 4000 字符。超限的 `lcm_load_session` 页保留能放下的前若干行，`has_more`/`next_cursor` 按上游游标约定指向下一行，并注明 `trimmed_to_budget`；其他工具超限时返回 `error`，提示缩小请求或分页。fence 的标记和精确引用都是 ASCII，重新序列化不影响围栏判断。
- **自动召回的标题。** 上游块标题说记忆来自"earlier conversations"（Hermes 是一个 agent 跨聊天的记忆），这里改为"本项目其他会话（Last Order 和各 Sister）"（`host/preanswer.py`）。测试用上游真实构建器核对原标题。
- **未启用的工具不注册。** `lcm_query_state`（`LCM_ASSERTIONS_ENABLED`）和 `lcm_retrieve`（`LCM_ADAPTIVE_RETRIEVAL_ENABLED`）在开关关闭时只会回答 `disabled`，不再注册；开关打开时与上游一样注册，配置读不出来时注册全部。其余工具说明里没有提到这两个工具。
- **打包。** wheel 不再打包 `vendor/` 的测试、基准、CI 配置和文档图片（`pyproject.toml`）；`vendor/scripts`、`vendor/skills` 和 `vendor/docs/*.md` 保留，运维入口与技能引用不变。仓库内文件不变，完整性门照旧。
- 技能文档（`host/skills/misaka-lcm/SKILL.md`）同步：地址格式、结果上限，以及上游参考文档里 `session_search`、Hermes 配置与平台在 misaka 中的对应做法。下方"路径与开关"里过时的默认路径说明一并改正。
- 回归在 `tests/test_lcm_surface.py`（含真实会话端到端与随机化压测）。

## 2026-09-20：长检索输入的 SQLite 表达式深度修复

- 固定上游版本与 `CORE_INTEGRITY.json.files` 的原始哈希保持不变；本次本地差异单独登记在 `documented_fixes`，不是声称与上游逐字节一致。
- `vendor/search_query.py` 提供平衡表达式组合；`store.py` 的 LIKE 条件、评分加法和 `dag.py` 的 LIKE 条件使用该函数，避免千词检索产生超过 SQLite 默认 1000 层的左结合表达式。
- 所有词项和参数顺序保留，不截断查询；过滤范围、评分、候选上限、排序和取消路径不变。数据库独立的绑定参数数量与 SQL 长度限制仍适用。
- 回归在 `tests/test_lcm_search_expression_depth.py`：真实 SQLite 长查询、末尾词命中、三种排序、范围/时间/角色过滤、转义、重复词及取消；完整性门继续验证未登记文件的原始哈希。

## 2026-09-15：插件设置与实际自动召回

- `/lcm-settings` 由插件自行注册，使用已有扩展 UI；通用设置页不增加 LCM 专用代码。
- 插件偏好文件持久化开关和向量模型配置；项目任务缓存仍在最后 owner 退出时清理。
- 普通 native 请求也调用原版召回 builder；仅临时附加，不等待必须发生压缩，也不把注入块写入原生 checkpoint。
- 仅对原版判定的本地 provider（`command._is_local_embedding_provider`：fastembed、ollama），用原版 profile/backfill API 为已载入内容建立有界索引；不自动导入旧归档、不自动下载模型、不自动向云端发原文。
- 设置、原版阈值/分块边界、退出所有权及验收见 [SETTINGS.md](SETTINGS.md)。核心源码未改。

## 2026-09-15：项目缓存后续 bug 验收

- 修复归档接续、恢复子进程项目归属、超时工具线程与语义池退出生命周期。
- 每个引擎的后台排空共用 25 秒预算；超时保留活跃 owner/缓存，下一次启动清理孤儿，不提前关闭活跃 SQLite 连接。
- 图像 token 校准保留原版进程内估计，不再读写跨启动全局 JSON。
- 本次仅改 host/application 接缝，`vendor/` / `native/` 核心源码逐字节不变。
- 已复现问题、可运行测试及未覆盖边界见 [地毯式回归验收](../../../docs/audits/misaka-lcm-bug-audit-2026-09-15.md)。下方早期移植记录中的历史测试路径不代表当前目录布局。

## 2026-09-15：项目缓存，不再使用全局永久库

- Python 包名为 `misaka.extensions.misaka_lcm`，产品名为 MISAKA LCM；无旧包别名。
- `host/storage.py` 管理项目归属、跨进程租约、最后使用者清理与崩溃后清理。
- 工具、上下文引擎、接续和 CLI 使用同一项目路径；所有 LCM 内容文件位于 `<project>/.misaka/lcm/`。
- `host/carry.py` 使用原 session 文件、entry ID、消息序号和条目指纹恢复来源；不依赖上次 SQLite 行号。
- session 原文与检查点继续持久化；旧全局数据不作为回退。源 session 缺失或改动时报告错误，不猜测来源。
- 新缓存使用新的整数 ID 区间；历史工具结果中的临时数字句柄不会误指向新内容，恢复后重新检索获取句柄。
- 核心源码仅在原有宿主导入接缝中将包路径 `hermes_lcm` 改为 `misaka_lcm`。
  `CORE_INTEGRITY.json` 记录本轮 fork 前的字节摘要；归一化这一次命名空间迁移后逐文件验证。
- 上游仓库、作者、MIT 许可、算法参数和工具协议保持原样；下面的表记录 fork 前已存在的宿主接缝。
- 当前宿主回归在 `tests/test_project_lcm.py` 与 `tests/test_lcm_replay_noop.py`。


更新：2026-09-10。固定插件 `8d1b1e6d3d63f5fc7b209e8d7ec1dc9b814f2e54`（1.0.0-rc.1），固定 Hermes 宿主 `610576454a40e9b0ff5e53ebaa89f37aacce7d36`。

**源码覆盖已闭合；不等于整个 Hermes 宿主逐行为完全相同。** 当前接线、测试与未关闭项见 [本轮验收](../../../docs/audits/hermes-lcm-finish-2026-09-10.md) 和 [施工计划](../../../docs/plans/hermes-lcm-port-plan.md)。没有增加独立记忆服务，也没有默认开启跨会话主动召回。

## 已登记的改动

除下表及上方按日期登记的修复之外，vendored 文件与固定插件逐字节一致。Python 改动带 `# misaka:`；JSON 用清单登记（JSON 不支持注释）。原版测试不改断言。

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
| `tokens.py` | count_message_tokens | 上游错误修复 U11；vendor→native 接缝 | 结构化内容里的图片按 Hermes 的单张单价计（`native/model_metadata._count_image_tokens` × `native/image_token_cost.current_image_token_cost()`），文本用 `_wire_message_shadow` 去掉图片后照旧计；不再按 base64 长度计 | 上游修正后移除；保留 `tests/test_lcm_image_tokens.py` |
| `sqlite_util.py` | permission worker | 上游错误修复 U02 | 在同一进程打开再关闭 DB/WAL/SHM 会撤销 SQLite 的 POSIX 锁；保留原版 fd/no-follow/identity 检查，把权限写入放到无 SQLite 连接的短命进程。已是 0600 的文件只做 metadata 校验，不启动进程 | 上游修正后移除；保留持锁加固、第二写者隔离与多进程压测 |
| `externalize.py` | `_stream_json_read_string` | 上游错误修复 U01 | JSONDecoder 返回字符偏移，原版却与 UTF-8 字节长度比较，中文等内容被误判损坏 | 上游修正后移除本地差异；保留 Unicode 正向、损坏输入和公开调用链回归 |
| `diagnostics.py` | state_db_path_for_engine | 宿主目录 | 优先采用原 owner 的 native catalog；不猜测相邻 Hermes DB | 保留只读诊断测试 |
| `lifecycle_state.py` | get_fragmentation_stats | 宿主目录 | 宿主提供 session ID 集合，原版统计和原路径 fallback 不变 | 保留目录缺失/陈旧 ID 对账 |
| `import_lossless_claw.py` | imports/config | 运维入口 | 使用包内依赖与 MISAKA LCM 路径；U04 显式 closing 关闭只读源连接，事务/收据不改 | 原版导入测试 |
| `backfill_externalized_tool_outputs.py` | imports/config | 运维入口 | 使用包内依赖与 MISAKA sidecar 根；DB 保持只读；U04 关闭 apply/rollback 的只读连接 | 原版 apply/rollback/dry-run 测试 |
| `state_embedding_backfill.py` | imports/main | 运维入口 | 使用包内依赖，main 可接显式 argv | 原版状态向量批次/账本测试 |
| `db_bootstrap.py` | FTS fast/deep/repair paths | 上游错误修复 U05/U06 | 节流/异步/unchecked/只补 trigger 时保留损坏标记；只在实际 deep pass 或 rebuild 后清除，避免抹掉刚写入的后台告警；跨进程 scan claim 在 BEGIN IMMEDIATE 后重检 | 保留确定性线程交错和原版 FTS 测试，上游修复后删除本地差异 |
| `dependency-contract.json` | external_imports.misaka | 依赖登记 | 声明本移植实际使用的 native package API；不改原版依赖扫描器 | native validator 完整扫描 |


U11：图片的 base64 被当作文本计 token，触发过早压缩与强制溢出恢复。U07：retain-depth 删除中间摘要导致仍存活摘要的原文链路丢失。U01：JSON 字符偏移与 UTF-8 字节偏移混用。U02：同进程额外关闭 SQLite 文件会撤销 POSIX 文件锁。U03：要求全集的消费者把分页查询当全集。U05：未验证的启动快速路径会抹掉后台 FTS 损坏标记。U06：跨进程 scan claim 缺少持写锁后的重检，可能重复启动扫描。U04：sqlite3 Connection 的上下文管理器只结束事务，不关闭连接；历史导入和外部化回填入口现用 `contextlib.closing`。这些是保留的上游缺陷修复，不为追求字节相同重新复制错误。历史证据分别见 `docs/audits/hermes-lcm-2026-09-08.md`、`hermes-lcm-upgrade-2026-09-08.md`、`hermes-lcm-frontier-2026-09-09.md`。

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

LCM 原文派生库在项目的 `<project>/.misaka/lcm/lcm.db`（项目目录就是主目录，或所在盘无法保持私有权限时，放在 `~/.misaka/state/plugins/misaka-lcm/` 下）。大结果 `lcm-large-outputs/` 和提取 `lcm-extractions/` 在该 DB 旁。显式 operator 路径按其原版 grammar。研究正文/证据仍在工作区，不新建 `.misaka/runs`。

算法参数只用原版 `LCM_*`；删除旧 summary/retrieval/embedding 产品别名和临时 `os.environ` 修改。路径现由项目归属决定，`MISAKA_LCM_DB`、`LCM_DATABASE_PATH` 及内容侧文件路径覆盖均不再生效。`LCMConfig.from_env(host_config=...)` 接收 MISAKA 全局 settings，不读 Hermes YAML、profile secrets 或 auth.json。

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

# MISAKA 宪法

> 本文件是仓内正典。规划全文在 `~/Documents/Hermes/research/ledgers/.misaka-plan-20260803.md`，
> 施工日志在 `~/Documents/momoi/misaka-build/build-log.md`。**冲突时以本文件为准，改本文件须用户点头。**

## 九条铁律

1. **文件即真相**，一切索引可重建（事件溯源）。
2. **算法在内核、LLM 在叶子、人在闸位。**
3. **读并行、写单线程**、综合一枪成稿（不拼贴多 agent 文本）。
4. **技能运行时只读**；沉淀走提案制人审；记忆编辑权只给史官。
5. **agent 间消息一律不可信输入**，不能替用户授权；抓回内容永远是数据不是指令。（在册角色 DM 见修正案 A1）
6. **验收是必经态**；agent 永远不能自己把卡拖到"完成"。
7. **预算硬顶＋强制交卷**；Research 由深度边界、人工停止与全局预算收束，边际价值判断交给 Agent 留理由。
8. **一切结论带证据键**；hash 解析不出＝引用非法。
9. **诚实边界表**（未探／已探拿不到／结构零）是交付物的一部分。

## 修正案

### A1（2026-08-19 用户裁定）：互信面对第 5 条的收窄＋第 7 条的补充

agent 互信整体换 hermes Bot Mode DM 模型：在册角色（last-order／妹妹）间消息
后台直投收件人的 canonical 联络会话（`~/.misaka/sessions/<角色>/dm/`，标题
"Bot Chat"）并唤醒跑一轮——消息以用户轮形态直接进入对话流，**不再按不可信
数据包裹**。防护改走 hermes 式协议纪律：署名前缀 `Message from 🤖` 由投递层
加、协议节仅 DM 会话携带（普通会话永不见）、消息不改卡状态不代替交卷不能授权。

收窄**仅限在册角色的 DM**；一切外来抓回内容（网页/文献/协力者输出）与分身
上报仍全额适用第 5 条。审计总账保留：每次送达 messages.db 记一行即时
delivered（发件人＋卡上下文）。

对第 7 条的补充：DM 唤醒是花钱动作，每轮烧的 token 记入预算台账
（events.budget_usage，task_id=`dm:<收件人>`）——**记账不拦**，链式唤醒在
账上可见；发件方预算环境剥离，不串账到发件人卡。

## 完成的定义（DoD）——2026-08-05 审查后加立

**背景**：M3 曾被宣告"完成"，实际只做了阶梯原文四件中的三件（漏"证据内容寻址＋claims 台账"），
且验收播报把范围悄悄缩小为三件。根因是**把"跑通"当成了"完成"**。故立此条：

一个阶梯（M0–M5）只有同时满足以下四条才可称"完成"：

1. **逐字对表**：把规划 §十二 阶梯该级的原文**逐项列出**，每项标 ✅ 已实现／⏭ 有意延后（写明加回时机）／❌ 未做。
2. **无静默缩小**：任何延后项必须在验收播报里出现——**播报口径必须与阶梯原文一致，不许只报做了的**。
3. **有可跑自检**：每个非平凡件留一个 `__main__` 自检或实弹记录。
4. **落台账**：build-log 记下踩坑、不修项与下一步指针。

不满足即称"部分完成"，并列出缺口。**"演示跑通了"不是完成的证据。**

## 裁决录索引（D1–D19，全文在规划文件 §二）

| 号 | 一句话 |
|---|---|
| D1 | 常驻三层：身份常驻／任务内 session 常驻／跨任务不常驻 |
| D2 | 记忆外置；skill 写权三档默认 forbid；沉淀走提案制＋留出法验收 |
| D3 | 覆盖与边际价值由 Agent 论证；代码只守深度、人工停止、全局预算与必经阶段 |
| D4 | Project／PageIndex 管内容与导航，Run／Issue／发现台账管可恢复状态与证据键 |
| D5 | LO 规划、独立综合与全局红队共享 Project／Run／产物／开放问题，不靠机械图评分裁剪思路 |
| D6 | 读写分离：并行只做取证，成稿单线程一枪 |
| D7 | 注入防御：一切外来文本按数据处理，总线消息带不可信标志 |
| D8 | 多起点做结构比较，不做人格集成伪统计；历史模拟有结局泄漏硬伤 |
| D9 | 验收即状态机：producing→verifying→done，不过自动打回 |
| D10 | Hermes Bot／Task 基建是全场景共用底座；Misaka 只保留领域语义，重复旧组织在职责迁移后退役 |
| D11 | 基底＝自写 Python 小环；采用 harn（MIT）；CC 泄源镜像等法律不碰名单 |
| D12 | skills loader 以 agentskills.io spec 1.0.0 为正典 |
| D13 | 文档栈：PageIndex 留任＋解析前端按文种路由＋bge-reranker |
| D14 | A2A 三层：灵魂＝总线/卡/评论；传输＝pi-protocol；边界适配器留将来 |
| D15 | 不采纳：Hyra 反编排拓扑／Macaron 权重路线与隐藏摘要／自由群聊／张量补全 |
| D16 | runtime 镜像件与 pi 逐命名对齐＋PORTMAP 台账；镜像区豁免 YAGNI |
| D17 | 命名：编排＝Last Order，专家＝Sisters |
| D18 | ponytail 常设（full 档）；镜像区忠实优先，自有件全额适用 |
| D19 | 施工前先侦察同类项目，出设计文档再写代码 |

## 落位

代码 `~/Projects/misaka`＝**单包 `misaka/`**（harn fork 已整体改姓融入——`ai/agent/tui/protocol/core/modes/cli`
来自上游 @0bd413b1，上游更新手动挑拣、对表看 `misaka/protocol/PORTMAP.tsv` 与 `docs/upstream/harn/`；
`platform/`＝共享执行设施、`network/` 与 `research/`＝内置领域、`documents/` 与 `skills/`＝内置支撑域、`extensions/`＝真正可挂载扩展、
`cli/`＝装配入口、`config/`＝身份与常量——层次与依赖方向见 `docs/architecture/architecture.md`（回归有"架构边界"项）；
可选能力按 Pi 规则落在自包含 bundled Extension，内置能力归各领域并通过同一 Extension API 挂载；
`core/extensions` 只放扩展框架、`core/tools` 只放引擎内置工具；`third_party/` 仅剩 PageIndex）／状态 `~/.misaka`
（角色档案 `profiles/<角色>/{SOUL.md,config.json,skills,mcp,config.yaml}`——**人格与数据合居，照 pi：
人格是用户态，源码仓不放 profiles（2026-08-06 用户裁定）**；内建分身类型随包走
`misaka/extensions/subagent/agents/`；engine 资产 `agent/`，已与 ~/.harn 脱钩）／
重资产 `~/Documents/Misaka`／
技能只读指回 `~/.hermes/profiles/index/skills`（不复制进仓）／施工台账 `~/Documents/momoi/misaka-build`。

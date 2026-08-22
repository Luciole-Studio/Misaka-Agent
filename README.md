# MISAKA — 怎么启动

命令就叫 **`misaka`**（照 harn 同款：入口在包内，安装时生成命令）。加一行别名即可全局用：

```bash
alias misaka='~/Projects/misaka/.venv/bin/misaka'
```
**不用 source 任何环境变量**（模型密钥按 `~/.harn/agent/models.json` 的定义从
`~/.hermes/.env` 实时读取）。

## 默认入口：多格子面板

```bash
misaka
```

光敲 `misaka` 进**多格子面板**（herdr 形态）：当前 shell 目录就是 Workspace；自动拉起守护进程、开一个属于该 Workspace 的编排官格子。
顶行是**标签栏**（agent 名标签，点标签切页）；左侧边栏分三区（每区超高出滚动条，
滚轮可滚）：**Sisters 名册**——在跑的标 ●、点击即聚焦，没在跑的标 ○、**点开即在
当前页平铺启动她**（每列两格，列满往右）；**Windows 窗口列表**——活格子＋呼吸圆点
（真在跑才亮）；最下面 **Projects 课题**——当前 Workspace 的课题（置顶★在前、已归档沉底标灰），
**点击展开/收回**附属卡列表：在跑的卡点击即聚焦，**跑过的卡点击展开她的会话现场**
（`card-shell --resume`，只接续不重发合同，看/手聊不动看板；在跑/验收中的卡
拒绝展开——现场有人在写），没跑过的提示无现场；
**右键课题弹菜单**：置顶/归档/删除（有卡挂着拒删，目录软删进 .trash 可反悔）。
主区是**页内 BSP 分屏**。**鼠标点标签/侧栏/格子＝聚焦**。
前缀键 `ctrl+b`（herdr 同款键位）：`1-9` 切标签页｜`n/p` 轮换页｜`c` 新页｜
`v` 页内分屏（右边劈新格子）｜`z` 缩放｜`t` **召唤全局树**（有就聚焦、没有在当前页右侧劈出树窗，见下）｜
`m` 鼠标捕获开关（**关掉才能用终端原生选区复制**）｜
`x` 关当前格子（直关）｜`d` 分离（格子照跑）｜`?` 帮助。布局（标签页＋分屏树）归**守护进程**
所有（herdr 同款：客户端是瘦的），面板退出重进、换个终端接回来，分屏都还在。**撞键提醒**：输入框里 `ctrl+b`＝光标左移——想左移按两下 `ctrl+b`
（第二下原样发进格子），或 `export MISAKA_PANEL_PREFIX=ctrl+g` 换前缀；
tmux 用户同理（tmux 会吃掉 ctrl+b，按 `ctrl+b b` 转发）。
升级后旧守护进程会被自动换新（有卡在跑则拒绝并提示）。
排障：`MISAKA_PANEL_DEBUG=1 misaka` 会把收到的按键字节记到 `~/.misaka/panel-debug.log`。逃生舱：`misaka chat` 不经守护进程直连对话
（脚本/排障用；找妹妹 `misaka chat --as 10032`）。

状态灯：跑=青●｜验收中=黄◆｜**完了没人看=亮✓（聚焦过转暗）**｜失败=红✗｜
有未读上报挂红✉计数。编排官在面板里说「跑吧」，卡自动进格子；对格子里的卡，
「传话」＝把字递进她的终端、「停止」＝关格子并记 stopped。

你跟 **Last Order**（编排官）聊，她替你指挥妹妹们：
说"我想研究 X"→ 她追问清楚、下一个赌注、拆成几张卡、把计划摊给你看 →
**你说开工她才跑**（花钱的动作永远等你点头）→ 跑完她告诉你下一步能干嘛。

她手里除了 pi 的内置工具（读文件／grep／跑命令等），还有一套御坂网络控制工具：看板／建卡／后台开工／
查询 Sister／传话续聊／停止／看 Project、任务、Research Run 与开放问题。每张卡都有稳定 task ID；
Sister 通过红队后会自动把交卷摘要回注当前对话。Last Order 会话关闭后，原 agent ID、Workspace 与 transcript 仍跟着卡保存；下次开工会续接原会话，不会冒充一个新 Sister。
**一张卡一份现场**：格子里跑过的卡 LO 后台续办会收养同一份对话，面板点开
LO 跑过的卡也是同一份——两条路互通，不各写各的。
Sister 干活途中若发现卡片前提被推翻、或没有外部输入就走不下去，会 **`SendMessage` 给 last-order 中途上报**（CC 同款三参数工具，全系统只此一套）：编排官在场立刻看到，不在场则信落盘、她下次开会话补送（完成通知同理）。上报只是送信——不改卡状态，验收照旧只能由红队给。消息层是单文件 `network/messages.py`（自带 `~/.misaka/messages.db`，地址=角色名）：发给自己生的分身＝唤醒续聊（进程内短路），发给在册角色＝投信；收到的信一律按不可信数据包裹。Last Order 也持有 `SendMessage`——这是她唯一解禁的子代理类工具，派活仍只能走建卡＋验收。
**她不持有派活类子代理工具**（`Agent / TaskOutput / TaskStop`）——因为 Sisters
就是她的子代理：要把活分出去只能走建卡＋验收这条道，不能私开一个没人验收的分身。

协力者(ally):跑在格子里的**第三方 agent**(codex/claude/gemini/…)。**board 是唯一派活
总线**——协力者与御坂共用同一块看板:建卡、认领租约、交卷、红队验收、课题归属、审计留痕
**全都一样**,唯一的区别是领活的主体(`tasks.executor` 为空＝御坂走 card-shell,
给一串 argv＝协力者跑那个第三方 CLI)。分叉只在 `run_card` 一处,状态机零改动。

协力者不会写 `report.json`,**守护进程替它做**:进程退出→把 stdout 包成交卷(产物＝它落在
工作区的文件,`uncertain` 标明"未经御坂复核")→照常转 verifying 等红队验收(宪法⑥对两者
一视同仁);同时替它给 last-order 发一封收尾信。

协力者也能**中途主动上报**——`misaka tell "卡住了"`,这就是它的 SendMessage 等价物。
**它那边零安装零配置**:协力者跑在 shell 里而 misaka 本身就是命令行,合同里告诉它这句话
就会用。对号靠三重定位:环境变量(`MISAKA_ALLY`/卡号,守护进程起格子时塞好)→工作目录
反查(cwd 落在卡上记录的 workspace/output_dir 里就查板拿 assignee；多卡共享同一路径时拒绝猜测，防冒名)
→都认不出就**拒发**(宁可不发也不冒名顶替,宪法②)。格子还会继承守护进程的库路径,
免得各写各的库、信送进另一个 `messages.db`。

工具与代码**两条线分家**：第三方适配与会话工具留在 `extensions/ally/`，
通用任务状态归 `platform/tasks.py`，只有派发边界进入 `network/`：
`misaka_ally_card` 建卡 / `misaka_ally_dispatch` 派活 / `misaka_ally_message` 传话
(打进它终端,它不认信箱) / `misaka_ally_output` 看输出 / `misaka_ally_stop` 停卡 /
`misaka_ally_list` 看所有格子跑着什么 / `misaka_ally_start`·`misaka_ally_close`
起停交互会话。只给 Last Order,Sisters 白名单里没有(机制隔离)。

派活走各家 CLI 的**非交互模式**(`codex exec` / `claude -p` / `gemini -p`)——**进程退出
＝确定的完成信号**,stdout 是干净文本,不用刮屏判闲。**零厂商知识**:调用命令由 LO 建卡时
给全(它读一次 `<cmd> --help` 就会),跟着卡走存在 `executor` 字段里,misaka 不维护任何
"各家 CLI 怎么调"的全局表。面板里,格子中跑着第三方 agent 时它作为**临时御坂**出现在
Sisters 名册(★在跑/☆闲着,主题色),**进程一结束就消失**。你在格子的 shell 里手敲起来的
**只认名单里那几家**(否则 vim/htop 也会混进名册;名单唯一真源 `~/.misaka/allies.json`,
守护进程首跑自动生成,默认 claude 与 codex,要加别家直接编辑 `{"commands": ["claude","codex","gemini"]}`,
改文件即生效不用重启);
LO 自己起的协力者不受此限。外部额度不在 misaka 记账内,
所以建卡外的每个动作(派活/起停)都带确认闸。
除她之外的角色（包括分身）都可用那几个工具继续拆分工作。

课题(Project)是 Workspace 内的持久容器。`misaka project <名>` 在当前目录创建
`.misaka/projects/<project_id>/{PROJECT.md,pageindex/,runs/}`；显示名可重复，稳定 ID 才是身份。
`/research` 由 Last Order 根据下一条问题自动命名并新建 Project，不要求人先填。建卡时
`--project <ID>` 归属它（唯一显示名也可）；不给就是当前 Workspace 内未分类。
课题状态:`misaka project` 只列当前 Workspace(置顶★在前、已归档标注);`--pin/--unpin` 置顶、
`--archive/--unarchive` 归档(目录是课题的"真相",库里只存状态位)、`--delete` 删除
(有卡挂着拒删,加 `--with-cards` 连卡一起删;目录软删进 `<workspace>/.misaka/trash/projects/` 可反悔)。

删除:课题软删(目录进 `.trash` 可反悔),卡**硬删**(连事件与预算记录一并抹——
board 保持干净)。命令 `misaka task <卡号> --delete`、`misaka project <名> --delete`;
面板 Projects 区右键课题/卡片弹菜单同款操作。**权限**:删除工具暴露给人与编排官
Last Order(带确认闸,宪法③),**Sisters 拿不到**(工具白名单只有 Agent/TaskOutput/
SendMessage/TaskStop,机制隔离)。在跑/验收中的卡先 `misaka net stop` 再删。

名册维护在对话里就能做：`/create [编号]` 走配置向导新建御坂（编号→一句话人格→钉模型，
落到 `~/.misaka/profiles/sisters/<编号>/`，`/sister <编号>` 即可切过去）；
`/remove [编号]` 除名（不给编号会列名册挑；有活卡在跑时拒绝；板上历史卡与工作区原样保留）。
命令行同款：`misaka create 10033`（缺的字段会问，`--desc`/`--model` 直接给值免问）、
`misaka remove 10033 [--yes]`。

## 全局树与微观代办（谁在干什么，一眼看全）

面板里按 `ctrl+b t` 召唤**全局树**（已有就聚焦；等价命令 `misaka tree --watch`，2 秒实时重画）：

```
▌课题「苏联档案」 ｜Research r_ab12 wave 2 active 开放问题2 分工：10032＝档案与文献
├ ● t_a3f2 核对入藏簿（10032·running）  代办1/3 ▶联系档案馆
│    ⚠ 交叉核对搬迁清单：馆方没回信   ← 卡壳浮出来（相谈）
│    [~] 联系档案馆                  ← 有分身干的条目成锚行
│       ⇢ ● dd44 分身·联系馆方（running）
└ ✓ t_9c01 分析搬迁性质（10032·done）  代办5/5
```

四层：**课题 → 卡＝宏观代办 → 微观代办 → 分身**。宏观代办就是板上的卡
（状态机归 Last Order）；微观代办是每张卡里 Sister 自己的子任务树——她拿到的
`misaka_todo` 工具**卡号烧死在闭包里**，只能写自己这张卡。约定：开工先拆（琐碎卡
可不拆）、推进随手标 doing/done、卡壳标 blocked＋一句原因（⚠ 直接浮上树）。
光干活不记账会被系统提醒（唠叨不拦活）；**硬闸只有一道**：交卷时 doing 必须清零，
否则打回。交卷里的心虚点（`uncertain`）原样进入产物与材料图，供综合官和全局红队审查；
足以影响结论的缺口由红队写成持久 `research_issues`，再由分支 LO 决定是否派复查卡，不做机械打分或自动扩卡。
编排官侧同款视角：`misaka_tree` 看树快照、`misaka_sister_peek` 窥视某卡现场尾巴
（内容按不可信数据包裹，完成与否仍以看板与红队验收为准）。

## LCM（无损上下文：压缩不再销毁历史）

长会话触发压缩时，misaka 不再用一次性摘要抹掉历史：**原文全部落
`~/.misaka/lcm.db`（FTS5 可检索），老内容压成分层摘要（近期→全程弧线→长期），
活动上下文＝摘要前缀＋保护尾巴**。移植自 hermes-lcm（`docs/design/lcm.md`，
上游 6.4 万行精读）。要点：

- **零操作接入**：LO 与全部 sis 会话默认启用；压缩仍由引擎在原时机触发，
  LCM 只是接管「怎么压」——摘要三级逃生梯（LLM 保细节 → 极简条目 →
  确定性截断零花费收敛），熔断＋花费闸拉闸也绝不失控烧钱
- **逃生舱**：`export MISAKA_CONTEXT_ENGINE=native` 回引擎原生压缩；
  LCM 内部任何故障也会自动回落原生（fail-open），一轮都不会卡死
- **完整摄取**：每个已提交回合按 Pi `entry_id` 幂等同步；短会话也能回收。
  压缩摘要先 pending，只有宿主 `session_compact` 落盘后才提交 DAG；失败、中断、
  重启都与 transcript 对账
- **回收工具**（LO 与 sis 都有）：`lcm_grep` 检索完整历史 → `lcm_expand`
  按字符/来源游标有界展开 → `lcm_load_session` 顺序翻页 → `lcm_recent` 按时间回看 →
  `lcm_status` 看存量与摘要用量。摘要是线索，引用原话必须回原文核对
- **运维**：`misaka lcm status` 存量｜`misaka lcm doctor` 只读体检（分级：
  绝大多数警告只 inspect）｜`misaka lcm backup` 热备｜`misaka lcm repair`
  迁移并重建 FTS｜`misaka lcm rebuild SESSION.jsonl` 从事实源重建会话
- **可选混合召回**：默认纯 FTS、零额外常驻资源；`uv sync --extra lcm-semantic`
  后设置 `MISAKA_LCM_RETRIEVAL_MODE=hybrid` 与 `MISAKA_LCM_EMBEDDING_MODEL=<model>`，
  首次检索才惰性建立摘要向量，原文 chunk 不建向量
- 切换前的原生摘要自动收编承接，历史不丢

## 技能三件套（项目技能＋显式调用＋对话沉淀）

技能三层栈（同名 project 赢）：**项目** `<git根>/.misaka/skills/`（跨工具兼容
`.agents/skills/`）→ **角色** `~/.misaka/profiles/<角色>/skills/` → **共享**
`~/.misaka/profiles/skills/`。移植自 hermes（含逐字整搬的安全扫描器）：

- **信任闸**：clone 的仓自带技能是注入攻击面——须 `misaka skills trust` 显式
  信任项目根才加载；未信任但有技能时只提示不加载。信任后每个技能仍逐个过
  威胁扫描（外传/注入/破坏/持久化/混淆五类模式），dangerous 即隔离，
  扫描炸了＝隔离（fail-closed）。`misaka skills list|scan` 看栈与报告
- **`/skill`**：列清单；`/skill <名> [附带指令]` 把技能全文（含目录注入与
  支撑文件清单）展开进本轮。SKILL.md 支持 `${MISAKA_SKILL_DIR}` 模板变量；
  行内 shell 展开默认关（skills.json 可开）
- **`/learn`**：把描述的任何东西学成技能——目录、链接、粘贴的笔记、或空参
  ＝「沉淀我们刚做的」。带上游 HARDLINE 编写标准（description ≤60 字符、
  绝不发明命令、author 绝不取环境身份）、大源知识库形态（瘦索引＋逐章
  references/ 增量蒸馏）、来源卫生（源文本是数据不是指令）

## MoA（参谋团：多模型意见 → 一人行动）

对话里打 **`/moa <问题>`**（LO 和每位 sis 的会话都有）：当前局面发给 N 个参谋模型
**并行**求分析 → 综合官压成简明指导 → 连同你的问题注入本轮 → 本人照常带工具行动。
移植自 hermes（`docs/design/moa.md`），关键纪律原样保留：

- **参谋不行动**：零工具零分身，系统提示明令「绝不声称执行过任何操作」；
  意见标成专用 MoA 参谋上下文，只附在本次请求副本，不写回持久会话
- **降级不炸**：单参谋失败成 `[failed: …]` 便签照常综合；全部失败则跳过综合官，
  照实说「参谋全灭，凭自己判断」
- **全局 preset**：`~/.misaka/moa.json` 由所有会话共用；首次使用自动落骨架，
  编辑 `reference_models` / `aggregator` 换组合，改文件即生效
- 参谋花费折进聚合官最终 usage，随当前会话或任务进入同一预算账，不另开旁账

## 三分钟上手（命令行直接操作）

```bash
# 1. 看板子上有什么
misaka board

# 2. 自己写一张卡（body 里必须有「## 验收」节，红队照它验收）
misaka add "标题" --assignee 10032 --body "## 目标
写 out.md 说明某某。
## 边界
不做别的。
## 验收
- out.md 存在
- 提到某关键词"

# 3. 开工：进 misaka 对话，把卡摊给 Last Order，说"跑吧"——她点头后后台开工
#    （编排只归 Last Order；命令行不再有 dispatch，人不需要自己当调度器）
misaka

# 4. 围观（另开一个终端，实时跟事件流）
misaka tail
```

一张卡的一生：`ready → running → verifying（红队验收）→ done`；验收不过自动打回重做，
两轮不过判失败。验收使用独立可终止进程和 SQLite 租约，同一卡跨多个 Last Order 会话也只验、
只通知一次。**产物必须是工作区内真实的相对路径普通文件**；绝对路径、`../`、越界软链、
目录以及交卷时报了却不存在的文件一律不算完成。

## 普通任务编排

```bash
# 一句话目标 → Last Order 拆成卡（会先下一个"赌注"，再拆）
misaka plan "你的研究目标"

# 开工：进 misaka 对话说"跑吧"，Last Order 使用公共 Task/Bot 基建调度
misaka
misaka synth --title "报告名"   # 综合成 REPORT.md（再进对话开工执行）
```

## 深研模式（/research）

只有人在 Last Order 输入框中键入 `/research`，或在终端运行 `misaka research`，才会启动；
Agent 工具表只有只读查看，没有 start/stop/expand 入口。

```text
/research [深度]
# 下一条普通消息输入研究问题；LO 自动命名并创建 Project
/research status [RUN_ID|课题名]
/research stop [RUN_ID]
/research resume [RUN_ID] [补充说明]
```

CLI 聊天窗口退出（双击 `Ctrl-C` 或 `Ctrl-D`）会持久停止本窗口启动的 Research Run，
并终止其运行中、排队中和依赖等待中的任务，不留到后台继续执行。

收到下一条消息后，LO 先命名并编写 Project，再创建持久 Research Run。根 LO 只产出研究设计，不把原问题答案送入聊天；
独立方法红队审查后，各 Sister 按能力档案被选择，并各自先做 preflight，再使用原有 Agent Loop 执行。
每波结束后至少两位 Sister 独立综合，全局红队检查事实、逻辑、因果、范围、方法、偏见和未决分歧。
实质且可补证的断裂点会各开一个新 LO 分支 Session，读取祖先 Session/产物导航后重新规划。

调度是按深度停止的分波并行 best-first，而不是机械 BFS。入口唯一研究边界是深度：

```bash
misaka research "你的研究问题" --depth 3
misaka research --resume RUN_ID
```

- `深度` / `--depth`：最大扩展分支深度；不填默认 3。
- 任务、方法、兵法、Sister 和综合视角由 LO 根据问题决定，不要求人类预填。
- 系统级全局 token 预算、并发容量和单次调用故障超时仍正常工作，但不裁剪研究规划。

所有计划、preflight、任务产物、证据台账、红队报告、分歧矩阵、分支上下文、综合和最终审计都落在
`<workspace>/.misaka/projects/<project_id>/runs/<run_id>/`。SQLite 保存状态和谱系，进程退出后可恢复。证据可靠性由 Agent 写出
来源独立性、距离、方法匹配、反证、范围和裁决理由；程序只核引文确实存在，不用机械分数代替判断。
能补证的分歧继续研究，不能补证的解释/规范分歧在最终报告保留，不做模型投票。

```bash
misaka net status              # 守护进程概况（不在会自动拉起）
misaka net run-card <卡id>     # 把一张 ready 卡放进格子（伪终端）里跑
misaka net panes               # 列格子；read/send 围观与打字；close 关格子
misaka net stop                # 停守护进程（所有格子一并关闭）
```

格子=守护进程独占的伪终端：你关终端、断网，格子里的妹妹照跑；交卷/超时由守护进程
盯着转看板状态，验收照旧走红队。崩溃恢复只重建普通格子——**跑卡的格子绝不自动复跑**
（花钱等你点头）。快照在 `~/.misaka/net.json`，套接字 `~/.misaka/net.sock`。
（第②期将把 `misaka` 默认入口换成多格子面板；设计移植自 herdr，Apache-2.0。）

## 检查与评测

```bash
misaka immune                     # 预算与机器钩子闸一览
python eval/placebo.py            # 显式判官突变评测；不进入产品工作流

./eval/run_regression.sh            # 全量回归。改任何代码前后都跑
```

## 网格扫描（穷举式覆盖审计）

```bash
misaka basemap --load                    # 首次：灌 65 格分类法（OCM/CAP/JEL）
misaka survey "你的命题" --scheme OCM     # 逐格判"这格与命题通不通"
```

## 前置条件（都在跑就行，不用管）

| 件 | 检查 | 挂了会怎样 |
|---|---|---|
| sub2api（Claude 池） | `curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8964/v1/messages` 有响应即可 | 所有 LLM 步骤报 503。改用 Gemini：`export MISAKA_PROVIDER=google MISAKA_MODEL=gemini-3.5-flash MISAKA_FORCE_MODEL=gemini-3.5-flash` |
| 项目 venv | `.venv/bin/misaka board` 能出结果 | 一切都跑不了。重建：`uv venv .venv --python 3.13 && uv pip install --python .venv -e .` |
| PageIndex PDF 树（可选） | `uv sync --extra pageindex` | 未装时文献仍会入库，只降级为按页导航 |

## 东西放在哪

- 代码 `~/Projects/misaka`——**只有一个包 `misaka/`**（harn fork 已整体改姓融入：
  `ai/ agent/ tui/ protocol/ core/ modes/ utils/ compat/` 来自上游；`platform/`=共享执行设施，`network/`=御坂网络与任务执行，
  `research/`=研究 Workflow、证据台账与 basemap，`documents/`=文献正典，`skills/`=技能域，
  `observability/`=只读投影，`extensions/`=真正可挂载扩展，`app/composition.py`=唯一装配根，`cli/`=入口；
  层次与依赖方向见 `docs/architecture/architecture.md`；上游文档在 `docs/upstream/harn/`，符号对表在 `misaka/protocol/PORTMAP.tsv`）
- 内置领域与 Extension 分开：`network/` 负责看板与 Sister 控制面，
  `extensions/subagent/` 负责递归 Agent，`network/messages.py` 是统一消息层（`SendMessage`＋落盘信箱），
  `documents/tools.py` 与 `network/switch.py` 是内置能力，`extensions/mcp.py` 是扩展；
  `core/extensions/` 只保留加载器、Runner 和协议，`core/tools/` 只保留引擎内置工具
- 公共任务状态与 Research Run 谱系 `~/.misaka/board.db`｜Project、PageIndex 与 Research 产物在 `<workspace>/.misaka/projects/<project_id>/`｜Task 会话、`report.json`、附件与日志在 `~/.misaka/tasks/<task_id>/`｜信箱 `~/.misaka/messages.db`｜底图 `~/.misaka/basemap.db`｜协力者手敲识别名单 `~/.misaka/allies.json`｜自由对话会话 `~/.misaka/sessions/<角色>/`｜角色档案 `~/.misaka/profiles/<角色>/`｜**共同魂** `~/.misaka/profiles/MISAKA.md`
- engine 运行时资产（models.json/auth/主题/设置）`~/.misaka/agent/`（已与 ~/.harn 脱钩，真文件）
- 普通 Task 的 Workspace 与产物就是建卡时冻结的 shell 当前目录；Research Task 的产物进入所属 Run 的 `tasks/<task_id>/work/`
- 规矩看 `CONSTITUTION.md`；施工日志在 `~/Documents/momoi/misaka-build/build-log.md`

## 常用旋钮（环境变量，都可不设）

`MISAKA_PROVIDER` / `MISAKA_MODEL` / `MISAKA_FORCE_MODEL`（换供应商）｜
`MISAKA_TOKEN_CAP`（token 上限，超 85% 进 Beast Mode 强制交卷，超 100% 停派新卡）｜
`MISAKA_MAX_CONCURRENT_SISTERS`（Sister 并发，默认 4）｜
`MISAKA_MAX_CONCURRENT_JUDGES`（独立红队进程并发，默认 2）｜
`MISAKA_JUDGE_TIMEOUT`（红队超时，默认 600 秒）

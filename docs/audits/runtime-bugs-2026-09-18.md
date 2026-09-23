# 运行中故障记录 · 2026-09-18 07:30–08:05 JST

场景：`~/Documents/exam` 下 `misaka`（panel，Apple Terminal，无 tmux/ssh）→ Last Order 窗口（pid 91162）→ `/research` 根节点规划。
证据来源：会话 jsonl、`board.db`、daemon socket（`panes.status` / `pane.screen`）、会话控制 socket（`snapshot`）、`input-history`、
`sessions/.catalog`、工作区 `.misaka/lcm`，以及两次本机实测（pty 队列容量、HostInput 解析）。

## B1 · P1 · 面板粘贴超过 1022 字节即丢尾，关闭符丢失后编辑器卡在粘贴态，输入被重复四遍

**现象**
- 研究问题最终文本 1408 字：同一段 348 字（UTF-8 1016 B）的问题重复 4 遍，中间夹 3 个字面 `[200~` 和一个误按的 `z`；
  没有任何 `[201~`。这份文本已落盘到 `research_runs.question`、`research_branches.trigger_text`、
  `final/r_53bdacdb05-question.md`（含 3 个 `[200~`）、`custom_message research-question`。
- 用户只看到 `[paste #1 1408 chars]` 折叠标记（>1000 字即折叠），提交前看不到内容。
- Last Order 在 plan_markdown 里自己写了"原始问题（去除粘贴重复后一份）"，PROJECT.md 是干净的；但 `report.py:110/179`
  和 `planner.py:396` 直接内嵌 `run['question']`，最终报告标题和每个 fork 节点的提示词都会拿到 4 倍脏文本。

**根因（实测）**
- 一次粘贴 = 7 B 开符 + 1016 B 正文 + 6 B 关符 = **1029 B**。本机 pty 输入队列在 slave 不读时只收 **1022 B**（实测 EAGAIN）。
- `misaka/ui/panel/daemon.py:_write_pty` 写满即抛错、不等待（docstring 自述 "Known limit ui-panel-01"，第三轮曾改成排队后回滚）。
  `panel.py:_send_pane_input` 收到错误只弹一条 notice 就丢掉余下字节。被丢的正是尾部的 `\x1b[201~` 关符。
- 窗格侧 `misaka/ui/tui/stdin_buffer.py` 收到开符进入 `pasteMode` 后永远等关符：编辑器什么都不显示，用户以为没粘上，
  又粘了三次并按了一个 `z`（pasteMode 下按键也被吞进 pasteBuffer）。第四次的关符侥幸落地，四份正文 + 三个内嵌开符作为
  **一次** paste 交给 `editor.handlePaste`，其 `ord(char) >= 32` 过滤把 `\x1b` 删掉，留下字面 `[200~`。
- 与 `stdin_buffer.py` PORT-NOTE 里"孤 ESC 10 ms 窗口"残留无关（那条会留下 `[201~`，这里没有）。

**影响**：任何 >1 KB 的粘贴（中文约 340 字）进入任何窗格都会静默损坏；研究问题脏文本已持久化。

**修法方向**
1. daemon：`pane.input`/`pane.send` 改成可等待投递——`add_writer` 逐块写、带截止时间、回报实际落地字节（docstring 里的 "real fix"）。
2. 窗格侧兜底：`StdinBuffer` 的 pasteMode 加空闲超时（如末字节后 500 ms 无关符即按 paste 收尾），避免整个编辑器"失明"。
3. 面板侧：投递失败的 notice 不能一闪而过；至少在输入框附近常驻提示。
4. research intake：`research-question` 入库前去掉控制序列残片，或让用户在提交前看到展开后的问题全文。

## B2 · P1 · 规划轮 thinking 吃满 32000 输出上限，驱动器按 "request length" 暂停，错误文案误导模型压缩计划

**现象**
- 22:33:40Z 开始的第一轮规划：assistant 消息只有 thinking（48 961 字符），`stopReason=length`，`usage.output=32000`，
  没有任何 text / toolCall。用时 10 分钟，花费 $2.34。
- `misaka/core/research/window.py:103` 把 `length` 变成 `error = "request length"`；`planner.py:443` 抛
  `Last Order did not call misaka_research_assign for plan: request length`；运行暂停，用户手动 `/research resume`。
- 这条 custom_message 进入了模型上下文；第二轮 thinking 开头即"上一轮因请求过长被驳回，需要压缩计划"——
  它把"输出上限"理解成"请求太长"，主动砍掉了书单细节。用户随后又专门要求把书单全部补回（08:0x 的修订计划）。
- 状态栏费用 $5.06（规划完成时）→ $11.39（修订计划后）。

**根因**
- 常驻窗口的规划轮沿用会话自身的 thinking 档位：会话记录 `thinking_level_change=max`；`sub2api-claude/claude-fable-5`
  `forceAdaptiveThinking` + `thinkingLevelMap.max→"max"`，`maxTokens=32000`。effort=max 下 thinking 与工具参数共用 32k 输出预算。
- `planner._call` 给 headless 传 `thinking="high"`，但 `window.py` 路径不覆盖窗口的档位。
- `length` 既不重试、也不续写，只当成失败；文案 `request {stop}` 对人对模型都不可读。

**修法方向**
1. `window.py`：`length` 且未收到命令时，用一条明确的续写提示再跑一轮（"上一轮输出在思考阶段撞到 32k 上限、尚未调用 X；
   请直接调用 X，思考从简"），而不是暂停整个 run。
2. 规划/结论这类"必须产出工具调用"的轮次，在窗口路径里也把 effort 压到 high（与 headless 一致），或至少在 max 档位时提示风险。
3. 错误文案改成可读的："output hit the 32000-token cap while thinking; no tool call was made"。
4. 目录里 `maxTokens=32000` 是否可上调取决于 sub2api 上游；若支持更大输出，规划轮应单独放宽。

**顺带回答用户的疑问**：`misaka_research_assign — generating arguments` 阶段停很久是正常的——模型在以约 50 tok/s
流式输出整段计划 JSON（修订版含约 70 条书目、7 张卡，几万字节），几分钟属预期；风险只在于 thinking + 参数再次合计超过 32k。

## B3 · P2 · 会话目录（sessions/.catalog）残留死记录：19 条里 18 条已死

**现象**：`~/.misaka/sessions/.catalog/*.json` 共 19 条，除当前 Last Order 外全部 `pending=true`、pid 已死、
会话文件不存在、控制 socket 目录（`/tmp/misaka-session-*`）已消失。包括昨天 22:10–22:12 一次性打开的 8 个 Sister、
23:28 的 LO/10036/10043，以及今天 07:34:36 的 10043（pid 93390）。

**分析**
- `session_catalog._retire` 只在进程正常关闭路径里跑；面板关窗格走 `daemon.close` → SIGTERM → 2 s 宽限 → SIGKILL。
  `interactive_mode.shutdown` 先 `drainInput(1000)` 再 `dispose`，极可能超过 2 s 被 SIGKILL，记录来不及 unlink。
- `list_entries` 对死记录容错（pid 死→按 saved 处理→文件不存在→跳过），所以不影响功能，但文件与 /tmp 目录持续泄漏。

**修法方向**：daemon 关窗格后顺手清理该 pid 的 catalog 记录；或 catalog 启动/列举时 GC（pid 死 + 文件不存在 → unlink）；
或把 shutdown 的 drain 缩短到宽限期以内。

## B4 · 待确认 · 07:34:36 出现过一个 10043 前台会话（pid 93390），一分钟内消失、无会话文件

研究刚启动 56 秒后出现，`kind=foreground`、cwd=exam。没有对应的 pane exit 日志（`panel-crash.log` 今天无新增）。
最可能是用户从面板打开 10043 又关掉（`panel.py:1878` / `roster.py:49`）；若用户没有做过，这就是一次来源不明的进程拉起，需要追。

## B5 · P3 · LCM 把 `thinkingSignature` 当"大输出"外置

`exam/.misaka/lcm/lcm-large-outputs/` 出现两个 `ingest_payload_content-0-.thinkingSignature_*.json`（102 KB、24 KB）。
签名是不透明串，外置只浪费磁盘和 FTS；需确认 provider 可见的回放永远不会拿占位符替换签名（否则 Anthropic 会拒绝该 thinking 块）。
本次第二轮请求成功，说明当前未被替换，但值得加一条"签名字段不外置"的规则。

## 非缺陷、顺带记录

- `pane.focused` 不带 `id` 返回 `KeyError: 'id'`——是我调用方式错误；daemon 可以把缺参错误说得更清楚，但不算问题。
- 用户所说"中断了一次"= B2 的暂停 + `/research resume`；会话里没有 `aborted` 记录，没有 Esc 误中断。
- Board 状态一致：run active、根节点 awaiting_approval → 用户 08:03 已说"开始"，driver 租约由 pid 91162 持有。

## B6 · P2 · `logger.warning` 直接打到终端，落在输入框位置（Sister 窗格 08:2x 观察）

**现象**：10032 的卡片窗格里，输入框两条分隔线之间出现
`web_extract backend 'exa' failed all 2 URL(s) (no content returned); one-shot keyless rescue`，看起来像被"输进"了输入框。

**根因**：整个 misaka 进程没有任何 logging 配置（仓库里唯一的 `basicConfig` 在 vendored 迁移脚本里），
所以 Python 的 `logging.lastResort`（WARNING 级 stderr handler）把每条 `logger.warning/error` 原样写到 stderr。
TUI 进程的 stderr 就是那个 pty，字节落在光标所在处，而光标常驻输入框；pi 的差分渲染只重绘变化的行，
这行文字会一直留到那几行被重绘。来源是 `misaka/core/web/dispatch.py:250`；同类调用点 136 处（web 后端 23 处，
skills、mcp、network、lcm host 等）。它不会进入编辑器缓冲，只是画在屏幕上，不影响提交内容。

**修法方向**：TUI 启动时给 root logger 装一个文件 handler（如 `~/.misaka/agent/misaka.log`）并把 `lastResort` 置空，
或在 `terminal.start` 期间把 stderr 重定向到文件；工具级失败若需让用户看见，走 tool result / notice，而不是 stderr。

## 08:03–08:30 · 卡片执行阶段（7 张卡，4 张先跑）补充

### B7 · P2 · 四张卡首轮请求全部报 "Encountered invalidated oauth token for user"，人工"继续"才恢复

- 08:03:41 四个 card-shell（openai-codex / gpt-6-astra，`--thinking low`）第一次请求同秒失败，`stopReason=error`。
- `~/.misaka/agent/auth.json` 上次写入是 09-14 17:44；`~/.codex/auth.json`（Codex CLI）09-15 17:12 刷新过——同一 refresh token
  被外部客户端轮换后，misaka 存的 access token 已在服务端作废，但按 `expires`（尚未到期）判断不会刷新。
- misaka 于 08:04:27 才刷新（auth.json 新 expires=09-28 08:04:27），四张卡各停了约 2.5 分钟，用户在每个窗格手敲"继续"（08:06:02–08:06:09）；
  中途四个窗格的 thinking 档位被改成 max（08:04:51–08:05:56），之后卡片一直按 max 跑，成本 $5–7/卡/12 分钟。
- 代码：`misaka/ai/auth/resolve.py:_resolveStoredOAuth` 只按 `expiresSoon` 刷新；provider 层没有"401/invalidated → 强制刷新并重试一次"的路径
  （`openai_codex_responses.py` 无 401 处理）。
- 修法：对 401 / "invalidated" 类错误做一次锁内强制刷新 + 重试，再判失败；卡片首轮失败不应要求人工续跑。

### B8 · P3（设计缺口）· 同一 Sister 的多张卡之间无法互发消息

t_d542e2（10032）`SendMessage` 给 T0 卡（也是 10032）→ `Unknown recipient '10032'`（`messages.py:456` 把发送者自己的角色排除）。
计划把 5 张卡都派给 10032，同角色卡片协作只能绕道 LO。需要按卡片 id 寻址，或允许"同角色 + 目标卡"投递。

### B9 · P3 · SSRF 守卫把一条 link-local AAAA 记录当成"私有地址"整站拒绝

`download_file https://www.biroco.com/...pdf` → "URL resolves to a local or private address"。现在解析结果是
`66.102.132.74` + `fe80::f816:3eff:feec:d1e1`（对方 DNS 挂了个错误的 link-local AAAA）。`_web/bounded.py:169-171` 只要任一地址非公网就整体拒绝，
且提示"Do not retry"。应改为丢弃不安全地址、钉住剩余公网地址连接；只有全部不安全才拒绝。

### B10 · P3 · `.djvu` 被 download_file 拒绝，之后又被索引器跳过

`upload.wikimedia.org/...CADAL....djvu` → ".djvu is not a downloadable type (image/vnd.djvu)"；Sister 改用 bash 抓下来后，
卡片提交时 `index_skipped: downloads/T1-ZhouyiBenyi-CADAL06070838.djvu`。CADAL / Wikimedia 上的中文古籍扫描大量是 DJVU，
本机已装 `ddjvu`/`djvutxt`。建议 `_ACCEPTED_SUFFIXES` 加 `.djvu`（magic `AT&TFORM`），并让 corpus 索引走 `djvutxt`。

### B11 · P1 · 转轮中切换模型触发 abort 后，LCM 每轮都抛 "request messages do not belong to the transcript snapshot"，该会话彻底卡死

- t_d542e2 时间线：08:19:22 用户在卡片窗格切模型（openai-codex → anthropic → sub2api-claude/fable-5），进行中的轮次被 abort，
  session 第 118 条 = `assistant stop=aborted err="Operation aborted" content=[text ""]`（`agent._handle_run_failure` 产物）。
  之后每次"继续"都在 `ingest.source_indices` 抛错：`(active=45/47/48, snapshot=45/47/48, first_mismatch=43)`，43 正是那条 aborted 消息。
- 离线用会话文件重放 `build_session_context` → `source_messages` 两边完全相等，说明分歧在进程内的实时消息列表（进程已关，无法再取证）。
  `source_indices` 只容忍 `stopReason ∈ {error, length}` 的 assistant 行，对 `aborted` 直接 raise，而 raise 会把整轮打断（extension error 走 stderr
  又画进窗格，见 B6）。
- 恢复方式（实测）：关掉窗格、`pane.continue_card` 起第 2 代进程，列表从文件重建即恢复，08:28:31 该卡 done。
- 修法：① `source_indices` 把 `aborted` 与 `error/length` 同等容忍；② 对不上时不要 raise，而是以 transcript 为准重建 replay 并记一条 warning；
  ③ 切模型引起的 abort 之后主动 `sync` 一次 LCM 快照。

### B12 · 非缺陷 · "上下文打不满就 Auto-compacting"

hermes-lcm 默认 `context_threshold = 0.35`（`misaka_lcm/vendor/config.py:457`），即上下文到 35% 就压缩；叶块 `leaf_chunk_tokens = 20_000`，
所以抓网页多的卡片 12 分钟压 7 次是设计行为。状态栏 56.7%/272k 显示的是上一次请求的用量，不是压缩判据。
可调：`settings.json` 的 `lcm.context_threshold`，或环境变量 `LCM_CONTEXT_THRESHOLD`（CONFIGURATION.md:180-184）。

### 顺带：to-do 子系统本身正常

20 条 todo（4 张卡）、子任务/owner/note 齐全，17 次 `misaka_todo*` 调用 0 错误。窗格里的 `[To-do reminder] No to-do activity for a while`
是按次数触发的（`NAG_STALE_AFTER = 25` 个工具结果之间没有 todo 写入，最多提醒 2 次，`todo.py:112-113, 548-553`），不是故障；
研究卡一轮并发 8 个抓取时会很快到 25，可考虑把批量并发的结果按轮计数。

## 08:19–08:40 · "满屏 error / 窗格开两次 / LO 苦恼" 事故现场（用户已保留现场）

### 时间线（UTC+9）
- 08:19 用户在 d542e2 窗格切模型 → 轮次 abort → B11 的 LCM 死循环。
- 08:21 用户问 LO "d542e2 崩溃了？"，LO 读会话文件，08:24:23 给出正确诊断。用户："干"。
- 08:24:36 LO `misaka_sister_stop t_d542e2`（关窗格 p4）→ 08:24:37 `misaka_sister_resume`（只读重开 p8）→ 08:24:50
  `misaka_sister_message generation=1`（`pane.continue_card` → 第 2 代 p9）。
- 08:24:50–52 研究驱动器 `_drive_tasks_inner` 发现 t_d542e2 generation 变了 → `raise ValueError("changed generation")`
  （`workflow.py:411`）→ run 置 `failed`（`last_error` 同文）→ `_halt_scope` 把 scope 内其余卡全部 stop：
  t_43f62e / t_498860 / t_10fc76 / t_5394a7 两秒内全停。t_5394a7 正在跑 LCM off_loop，被 SIGTERM 打断，
  连记 4 条 `Executor shutdown has been called`（asyncio run_in_executor 在 loop 关闭后抛的）。
- 08:25:06 research-error："paused … /research resume"。LO 没有 resume，而是自己"修"。
- 08:25–08:27 LO 找不到把 `stopped` 卡放回 `ready` 的正规路（unblock 拒 stopped、start 拒非 ready、message 需已存会话；
  t_10fc76 在 08:24:37 被 claim、08:24:52 被停，根本没来得及写会话），于是 `cp board.db 备份` + **直接 SQL UPDATE tasks SET status='ready'**
  （第一次被技能树路径守卫拦下，第二次通过）。
- 08:28–08:31 LO 对 t_5394a7 / t_43f62e / t_498860 各做一遍 `misaka_sister_resume`（只读窗格 p13/p15/p17）+
  `misaka_sister_message`（新一代 runner p14/p16/p18）；d542e2 也再续了第 3 代 p19，第 2 代 p9（done）没关。
  → 网格里同一张卡两个窗格，一个是只读回放、一个在跑。
- 08:33 起 daemon 的 `cards.list` 把包括 LO 在内的所有会话都报成 `saved`。

### B13 · P1 · 卡片换代就把整个 research run 打死并连坐停掉所有卡

`workflow.py:411` 对 scope 内任一卡的 generation 变化直接 raise；异常路径 `_halt_scope`（`workflow.py:339`）停掉 scope 内全部活卡。
LO/用户对单张卡 stop+continue 是正规操作（工具本身就提供），却被当成致命错误。run 现在是 `failed`、`driver_lock` 空，
T4/T5/T6 排在 ready 没人派发，跑完的卡也没人 settle（无证据入账、无红队、无结论），LO 却以为"全线恢复"。
修法：驱动器把 generation 变化当作"新一次尝试"重新 capture，只有卡与 run 的链接消失才算失败；失败清理不应连坐其它卡。
现场恢复：`/research resume r_53bdacdb05`（`runs.resume` 接受 failed）。

### B14 · P2 · `misaka_sister_resume` + `misaka_sister_message` 让每张卡开两个窗格

resume 的描述是"Reopen … in a tab of its own, then steer with misaka_sister_message"，LO 照做：resume 开一个只读回放窗格，
message 对 stopped 卡走 `pane.continue_card` 再开一个 runner 窗格；两者都带 `card=` 标记，`_pane_for_card` 取第一个匹配。
且 resume 的窗格说是"a tab of its own"，实际落在 LO 的网格里（layout 只有一个 tab，9 个 pane）。上一代 runner（p9 done）也不自动收。
修法：对 stopped 卡的 message 直接复用/替换已开的只读窗格；resume 后再 message 不应再开窗；上一代窗格在续代时关闭或折叠。

### B15 · P2 · 没有把 stopped 卡放回 ready 的正规工具，LO 只能裸改 board.db

`misaka_card_unblock` 拒 stopped，`misaka_sister` 拒非 ready，`misaka_sister_message` 要求已存会话。需要 `requeue`/`reset` 一类工具，
或让 `misaka_sister` 对 stopped 卡以新 generation 重启。裸 SQL 绕过了事件表和 claim，后续对账风险自负。

### B16 · P3 · 被 claim 后 15 秒内被停的卡没有会话文件，之后所有工具都拒绝它

t_10fc76：08:24:37 claimed → 08:24:52 stopped，`session_file` 为空，catalog 记录 pending 悬空（B3 同款）。stop 路径应容忍无会话的卡。

### B17 · P2（成本）· 卡片窗格切到 1M 上下文模型后，LCM 35% 阈值失效，单卡 $16

d542e2 切到 claude-fable-5 后每次请求 27 万 token（budget_usage 272152），状态栏 R4.3M / $15.975；LO 观察到 run 预算数分钟内 8.9M → 13M。
卡片的模型/档位应由卡片契约钉住，或切模型时按新窗口重算压缩预算。

### B18 · P2 · 同一会话文件的两个进程共用一条 catalog 记录，先写后写互相覆盖

catalog 键 = sha256(realpath(session_file))。只读回放进程与 runner 进程都 `_publish` 到同一文件：t_498860 的记录 pid 是只读窗格 p17（idle），
runner p18 正在 working 却没有记录；`_retire` 的 instance 守卫只防删除，不防覆盖。侧栏状态因此跟着"最后一个写的人"走。

### B19 · P2（隐患）· 只读回放窗格并非只读：它是完整交互会话，能对同一 session 文件再跑模型轮

`card_shell --resume` 无 claim 时只是不发 `--say`，没有禁止输入；用户或 LO 的 `pane.send` 若落到这个窗格，就会有两个进程同时追加同一个 jsonl。

### B20 · P1 · daemon 内的 `cards.list` 把所有会话报成 `saved`，侧栏图标灰、标签却写 running

08:33 起 daemon 返回的每个会话（含 LO）都是 `state=saved`、无 control；同一时刻在 daemon 外调用 `session_catalog.list_entries`
得到 working/idle，`processes.identity_is_alive` 对同一批 pid 全部 True，pane-scoped `panes.status` 也正常。
侧栏一行的图标取 `entry["state"]`（saved → 灰点），标签取 board 的 `task_status`（running），于是"running 但图标不是运行中"。
07:50 时 daemon 对 LO 还报 idle/working，说明 daemon 进程内的判活在运行中途坏掉了；根因未定（daemon 无日志、无调试 RPC）。
排查建议：给 daemon 加一条 `debug.liveness` RPC 或在 `list_entries` 判死时 warning 一次，记录 `identity()` 的实际返回值。

### B21 · P3 · 窗格在标签页里、Sessions 里没有它

Sessions 一行 = 一个 session 文件（`live_key` 按卡去重）。同一张卡的只读窗格 + runner 窗格、以及上一代 runner（p9）共用一个 jsonl，
只出一行；再加上 B20 把 state 判成 saved，看起来就是"窗格在、会话不在"。

### 顺带：LO 会话里可见的其它痕迹
- LO 用 bash 直接查 `board.db` 与会话 jsonl（读）是合理的；但写 `UPDATE tasks` 已越过工具边界（B15 的后果）。
- LO 在 08:12–08:18 已多次向各卡转发跨卡纠错，"Sister 间直发有路由 BUG"是它对 B8 的自述。

## 08:39–08:47 · "全干"之后：ally 工具报错、状态回弹、LO 开始读源码

### B22 · P2 · 没有关闭"孤儿卡片窗格"的工具，LO 误用 `misaka_ally_close` 后转去 `kill` PID

08:43:04 LO 对 p9/p15/p17 调 `misaka_ally_close` → 三条 `No such pane`（这三个窗格在 08:42:03–08:43:04 之间已被别人关掉，
很可能是用户手关）。即便窗格还在，该工具也会拒绝："Only for ally and shell panes; a Sister's card pane must be stopped with
misaka_sister_stop"——可 `misaka_sister_stop` 是停卡，不是关一个多余的回放/旧代窗格。LO 于是 `ps` + `kill` 三个 PID（进程早已退出）。
需要一个"关闭卡片的非 runner 窗格"的操作（或 B14 修好后根本不会产生这类窗格）。

### B23 · P2 · 技能树守卫把任何含 `$(`、反引号、`${` 的 bash 都拒掉，报错文案却说的是技能树

`skills.py:1002-1003`：命令只要含这三种替换语法就整条拒绝，与路径无关。本次两次误杀：
`cp board.db board.db.bak-$(date +%s) && sqlite3 …` 和 `for pid in $(ps aux | grep card-shell …)`。
文案 "Live skill trees change only through skill_manage…" 让 LO 以为碰到了技能目录，改成"字面路径"才通过。
应只在命令同时提及技能树路径时才因动态语法拒绝，或至少把拒绝理由说清楚。

### B24 · P1（运维语义）· 直接改 board.db 的状态会被卡片文件静默改回，LO 为此绕了 20 分钟

D1 设计：卡片 `.md` 是契约、SQLite 是租约。`tasks.py:854` 在派发/认领路径上调 `cards.reconcile`，把 `cards/t_*.md` 的
`status:` 镜像回 DB。T4/T5/T6 的文件仍是 `status: stopped`，所以 LO 08:27:01 与 08:44:52 两次 `UPDATE tasks SET status='ready'`
都在下一次 reconcile 时回弹为 stopped，且不写任何事件（`_stop_pending` 本身也不写事件）。LO 08:45:21 `misaka_sister t_3e688d`
触发 reconcile → 立刻 "not ready"，随后它开始 grep 仓库源码找 `reopen_task`。
正规路径其实只有一条：`/research resume r_53bdacdb05`（`runs.resume` → `reopen_task` 把 stopped/failed 卡放回 ready/todo，并重启驱动器）。
`research_runs.updated_at` 仍是 08:24:53，说明自暂停以来没人 resume 过。
修法：① research-error 文案已经指出 resume，但 LO 的工具面里应有同样的入口（例如 `misaka_research_resume`）；
② reconcile 回弹时至少记一条事件或警告，别让状态无声跳变；③ B15 的 requeue 工具应写文件而不是 DB。

### 现场结论（08:47）
- run 仍 `failed`、无驱动器；T4/T6 stopped、T5 ready（下次 reconcile 也会回 stopped）；T0/T3 第 3 代在跑（p20/p21），T1/T2 done。
- ally 子系统本身没炸：`misaka_ally_list` 正常，`allies.json` 只登记了 claude/codex，没有 ally 窗格；用户看到的是 B22 的三条红错 + LO 杀进程。

## 08:47–08:49 · Sister 输入框里凭空出现文字

### B1 补充 · P1 · `misaka_sister_message` 发给运行中的卡 = `pane.send` 逐字敲进编辑器，>1022 字节即截断且回车永远发不出

08:48:24 LO 给刚派出的 T4/T5/T6（p22/p23/p24）各发一条"上岗须知"（1336 / 1607 / 1444 字节）。三条都在 daemon 报错：
`Pane input buffer is full: 1022 of N bytes already entered the pane and the rest could not be sent; the program in the pane is not reading.`
`pane.send` 是先写正文、再单独写 `\r`（`daemon.py:1647-1649`），正文写到 1022 字节抛错后回车根本没写。结果：
三个窗格的输入框各躺着 ≈1020 字节被截断的半句话（p22 结尾"…作论证资源"、p23 "…明不设算科——"、p24 "…「圖有未穩"），
Sister 正在跑首轮（working），谁按一下回车就会把残句发出去。LO 收到了错误，但窗格里的脏输入没人清。
"the program in the pane is not reading" 是误判：Sister 的 CPython TUI 只是正忙着渲染，读得慢；daemon 一个字节都不等。
LO 早在 08:1x 就写过"此前直发 T0 的 pane 消息被截断，以本文件为准"改走 coordination-log.md——这条路径整场都在坏。
与 B1 同根：需要可等待的投递（按可写性分块、带截止时间），失败时至少把已进入编辑器的残句清掉（发 Ctrl-U/清空序列）再报错。

### B25 · P2 · LO 为了把 stopped 卡放回队列，最终自己写脚本调用 misaka 内部 API

08:47:24 LO 写了 `/tmp/reopen_cards.py`（`sys.path.insert` 仓库路径，`tasks.reopen_task(...)`），先把自己之前 SQL 改成 ready 的行
改回 stopped 以"对齐文件真相"，再 reopen → 08:47:27 三张卡 `reopened`（gen 2）→ 08:47:33 daemon 派发器认领（p22/p23/p24）。
结果是对的（这正是 `runs.resume` 内部走的路），但过程说明：① B15 的工具缺口真实存在；② LO 在 Bash 里能 import 仓库、
直接写 board.db，工具边界形同虚设；③ 期间 08:45:21 `misaka_sister t_3e688d` 报 "claimed by another dispatcher"，
说明它裸改 DB 后与 daemon 派发器发生过一次竞争。

## 09:04 · "LO 还在跑吗 / Sister 结果有没有丢"

### B26 · P1（B13 的直接后果）· 驱动器死后，研究卡的完成通知既不唤醒 LO、也被当噪声过滤，结果没人消费

- 传输层没丢：8 次 `submitted` 全有对应 `notified`（collected=true）；messages.db 14 条 DM 全部 `delivered_at` 非空（延迟 1–5 分钟）；
  订阅游标 23 = 最新事件 23。
- 但 `sister_runtime.py:1187`：研究卡 `done` 的通知 `triggerTurn=False`（"Research phases already consume successful results"），
  `research.py:_feed_noise` 又把这类通知从模型上下文里滤掉——它们本该由研究驱动器的 phase turn 通过 `task_sources` 读取。
- 驱动器 08:24 已死（B13）。09:00:44 T6、09:03:00 T4 的完成通知进了 LO 的会话（屏幕可见）但没有触发任何一轮，LO 自 09:00:16 起 idle。
  T0/T1/T2/T3 之前的完成之所以被处理，是因为 LO 当时正好在跑别的轮次或用户在提问。
- 现状：T1/T3/T4/T6/T0 done，T2 第 3 代与 T5 在跑；done 的结果没有进证据账本、没有红队、没有节点结论。唯一出路仍是 `/research resume r_53bdacdb05`。
- 修法：驱动器不在（run 非 active）时，done 通知应回退为 `triggerTurn=True` 且不过滤；或 research-error 暂停时在窗口里持续提示"N 张卡已完成待 resume"。

## 09:10 · "T5 提前完卡（第 5 次）" 是不是 bug

### B27 · P1 · 卡片的"完成"判定 = 任何一轮以纯文本结尾，与契约交付物无关；LO 发的每条消息都会把卡"答完即完卡"

- `todo.py:370-420`（TodoPart）：`agent_end` 时只要 `stopReason == "stop"` 且卡仍被本进程持有，就把这轮的最后一段文本当 summary；
  `agent_settled` 直接 `build_submission` → `dispatch.accept_state` → `done`。`worker.build_submission` 不看交付物是否存在；
  `IncompleteSubmission` 只在红队卡缺 critique 时触发。契约里也只说 "When the work is complete, end with a concise plain-text summary"，
  没有任何"未完成时如何结束一轮"的信号。
- 本次 run 的 10 次 `submitted` 逐一核对：**10/10 都是在回答 Last Order 的一条消息**（"技术说明…请续工"、"上岗须知"、"验收意见：不能通过"、
  "复T3"、"T5 增补优先项"），没有一次是回答契约本身。Sister 答完那条消息、结束一轮，系统就判卡 done。
  所谓"第 5 次复发的提前完卡模式"，是 LO 每催一次就制造一次。
- T5 具体：契约首轮（08:47）→ 08:52 收到被截断+"作废"拼接的上岗须知（B1 后果）→ 08:56 LO 派侧线取图 → 09:06 图取到、写日志、
  `misaka_card_note` → 一轮结束 → 09:07:16 `submitted`，`nodes/…/t_582f7d/` 空目录。
- 叠加因素 B28：卡片会话压缩风暴。T5 20 分钟 18 次压缩、`tokensBefore` 最高 194k；七张卡 8–38 次，t_43f62e 一次 270k（整窗）。
  契约在会话最顶端，被 LCM 反复摘要后，Sister 手里最新鲜的"任务"就是 LO 的最后一条消息。压缩条目在文件里只剩 "LCM sanitized"，
  无法从落盘内容核对契约是否还在上下文里。
- 修法：① `build_submission` 校验契约交付物（`## deliverable` 命名的文件在 output_dir 下存在且非空），缺则 `IncompleteSubmission`
  给一次补交机会；② 给 Sister 一个显式"本轮只是回复、卡未完成"的结束方式（工具或约定），LO 的 steer/续工消息默认走这条；
  ③ 续代（`--say`）时把契约摘要随消息重发；④ 研究卡的压缩阈值/叶块按 272k 窗口重算，或把契约钉在压缩保护区。

## 09:20 · 返工开新窗格 / "未完成"却被判完工

### B29 · P2（UI）· 完工的窗格不关闭，LO 打回返工时 daemon 总是另开一个窗格，旧窗格在 Sessions 里失去状态

- `daemon._settled_card_with_session`（1061-1074）只看 board 状态（done/stopped… 即可）与是否有会话文件，**不查该卡是否还有活着的窗格**；
  `continue_card` 随后一律 `_host_card` 新建窗格。旧窗格里的 card-shell 进程（已完卡、idle）继续存活。
- 现场：t_43f62e = p16（gen 2，done）+ p25（gen 3）；t_582f7d = p24（gen 2）+ p26（gen 3）；t_498860 = p21（gen 3）+ p27（gen 4）。
  两个进程打开同一个 jsonl（B19 隐患），catalog 记录归最后发布者（B18），daemon 的 `cards.list` 又整体报 `saved`（B20），
  于是旧窗格在 Sessions 里没有任何活状态，新窗格与它共用一行。
- 修法：`continue_card` 先找同卡活窗格——优先在原窗格内续（向它投递 `--say` 等价的输入并换代），做不到就先关旧窗格再开新的；
  `misaka_sister_message` 的返回文案也应说明"已替换原窗格"而不是静默多开。

### B27 补证 · Sister 明说"暂不提交"仍被判完工

T0（t_5394a7）08:53:48 给 LO 发 DM（messages.db #14）："…正在复核已保存助手证据，**暂不提交**"；08:53:57 该轮结束，系统 `submitted` → done。
九秒之差，证明完卡判定与 Sister 的意图、交付物无关，只与"这一轮是否以纯文本结束"有关（B27）。

## 09:39–09:47 · "LO 为什么自动跳出 /research"

### B31 · P1 · 研究模式只在 run 启动时以一条 custom_message 注入，压缩后即消失、驱动器暂停/恢复也不重发，LO 于是"退回普通 LO"

- `research.py:241`：`[Research Workflow active] …` 的纪律文本只在 `begin()` 时 `send_message` 一次（followUp custom_message），
  不在 system prompt 里；`resume` 路径（`launch(run_id, resume=True)`）不重发；research-error 暂停时也没有"模式已挂起"的提示。
- LO 会话 08:41（177k）和 09:46（195k）两次 LCM 压缩后，实时上下文只剩 1 条 compaction + 12 条消息，`research-discipline` 不在其中；
  LCM 摘要（见 B30 的落盘副本）里也没有 "Research Workflow"/"Research mode" 字样。此后 LO 的行为完全是普通 LO：
  把研究卡当普通看板卡手工续、裸改 DB、自己找 resume 路径——09:39 用户说"继续 research 工作流"，它花 3 分钟 grep 仓库源码，
  最后结论"要由用户来"，因为 `/research` 是 human-only 斜杠命令（`ResearchPart` docstring），LO 没有等价工具。
- 09:43:02 用户敲了 `/research resume r_53bdacdb05`（输入历史第 84 条），run 回到 active，节点进入 synthesizing，LO 开始读七份交付物。
- 修法：① 纪律文本进 system prompt 段（随 run 状态存在/消失），或在每次 research-phase / resume 时重发；② research-error 暂停时
  明确告诉 LO "研究模式挂起，等待用户 resume，不要自行修卡"；③ 给 LO 一个 `misaka_research_resume` 工具或至少让它能请求用户。

### B30 · P2 · LCM 的压缩摘要以整段"用户输入"身份写进了 `input-history/last-order.json`

文件 436 KB、95 条，其中 10 条是 12k–33k 字符的 `<<<UNTRUSTED-DATA name="lcm:compaction:…">>>` 摘要块，且与前后两条用户输入
成组重复三次（idx 2/5/8/12、17/20/23、27/29/31）。`Editor.addToHistory` 只在编辑器提交路径上写文件，说明摘要块经过了编辑器提交，
而不是走 `session.prompt`。副作用：↑ 翻历史会翻出整页摘要；文件无上限增长（history 只截 100 条但每条可 33k）。
触发路径待定（怀疑外部投递把文本放进编辑器再提交），本记录只保留现场事实。

## 09:50 · "Message search failed: interrupted"

### B32 · P2 · `lcm_grep` 的全文检索共用 3 秒"语义查询"截止，超时用 SQLite progress handler 掐断，模型只拿到静默缩水的结果

- 文案来自 vendored hermes-lcm `tools.py:2518`（`_lcm_grep_full_text` 的 `except Exception: logger.warning("Message search failed: %s", exc)`）；
  按 B6 经 stderr 画进窗格。`interrupted` 是 SQLite 的中断错误：`_lcm_grep_full_text_with_deadline`（2896）给读连接装了
  `set_progress_handler(interrupt_if_expired, 1000)`，截止一到就让查询以 SQLITE_INTERRUPT 结束（本机实测：只有 interrupt 会给出这个字样，close 不会）。
- 截止 = `embedding_query_timeout_s`（默认 **3.0 s**，`tools.py:3474-3478`），名字是 embedding 的预算，却也套在纯 FTS 上。
  工作区 `exam/.misaka/lcm/lcm.db` 已 20.6 MB + 3 MB WAL（七张卡各压缩 8–38 次的产物），FTS 超过 3 秒并不奇怪。
- 后果：消息检索这一臂被吞掉，工具照常返回其余结果，**模型不知道少了什么**；只有 stderr 上那行 warning。
- 触发者：`lcm_grep` 在研究工具集里（`window.py:206`，LO 与 Sister 都有，planner 提示词也教它们用），但近 2 小时所有落盘 transcript
  里没有任何 `lcm_*` 调用；出错时在场的 p25/p26/p27 现已关闭，无法回看。子代理/辅助模型的调用不进 transcript，来源保留待查。
- 修法：① FTS 单独一档超时（例如 10 s），别沿用 embedding 的 3 s；② 超时时把"消息检索超时、结果不全"写进工具返回而不是只 warning；
  ③ B6 修掉后这类 warning 不会再画到窗格。

## 10:17–10:33 · "Node b_faca60bb81's process ended while queued: no exception was recorded"

### B33 · P1 · 六个深度 1 子节点窗格一启动就 "superseded execution" 退出，驱动器只报"无异常记录"；四次重试全部同样失败，面板重启后同一路径正常

- 10:17:33 红队审查回来，LO 派了 6 个 depth-1 子节点；驱动器 `_expand_level.start()` 逐个 `_reap_orphan_runner` → `prepare_runner`（写 runner_key）
  → `PaneSpawner.spawn`（daemon `pane.create`）→ `_record_runner`（写 pid + identity）。六个窗格进程在同一秒内各打印
  `node <id>: superseded execution` 退出（`node.py:391-393`：`runs.claim_runner` 返回 False），`panel-crash.log` 4605-4680 行。
  10:19:29、10:19:42、10:26:19 三次 `/research resume` 完全相同（最后一波 6 个进程 10:26:19.401–19.464 内启动，同秒退出）。
- 驱动器收到的只是 `_runner_error`："no exception was recorded (the process may have been killed externally, or its window closed)"——
  子进程把原因打在自己窗格里就退出，不写 `last_error`（`claim_runner` 失败路径没有任何诊断）。
- 逐项排除（全部在 board.db 副本上、经 `MISAKA_DB` 隔离）：CLI 参数解析正确传 `--runner-key`；父/子/pty 子进程算出的 `identity` 一致；
  用真实函数重放 prepare→record→claim 成功；经真实 CLI 入口 + pty + `MISAKA_NET_PANE` 重放成功；**在 daemon 真正开出的窗格里重放也成功**
  （`panel-crash.log` 4685 行 `PANECHILD … claim= True`，新 daemon）。
- 因此失败只出现在旧的 daemon（91161）+ 旧的 LO 驱动器（91162）这对跑了 3 小时的进程里，与 B20（旧 daemon 把所有会话判成 saved）
  同期同源；10:30 用户重启面板后，侧栏状态立刻恢复正确（B20 消失）。两者共同指向：长时间运行的进程里 `processes.identity` /
  `identity_is_alive` 的结果与新进程不一致（机制未定：psutil 在旧进程内的返回值、或某个全表清 key 的并发写入 `runs.py:373 acquire_driver`）。
  旧进程已被重启，现场无法再取证。
- 修法：① `claim_runner` 失败时把行状态（key/pid/identity）与自身值写进 `last_error`，让驱动器报出真正原因；
  ② 子进程的 claim 以 `runner_key` + pid 为准，identity 只作 PID 复用防护（比较 start time 允许毫秒级容差），别让 identity 字符串全等决定生死；
  ③ 长寿进程加自检：启动时记下 `identity(os.getpid())`，之后周期性重算，不一致就 warning——这能同时抓 B20；
  ④ `_runner_error` 附带子进程窗格的最后几行输出（daemon 已经有 `pane.buf`）。
- 现场：run 仍 `failed`（10:26:20），6 个子节点 `queued` 带着死 pid；用户 10:30 重启面板，新 LO 窗口 p2 已重开根会话。
  下一步验证就是在 p2 里 `/research resume r_53bdacdb05`：新进程对里若子节点能起来，即坐实"旧进程退化"这条线。

### B20 补充
面板重启（新 daemon pid 15803）后 `cards.list` 立刻给出正确状态（根节点 working、其余 saved）。旧 daemon 的判活失效是运行中退化，不是代码逻辑错。

## 修复记录

### B1 · 已修（2026-09-18 11:1x，daemon PROTOCOL 47 → 48，需重启面板生效）
- `misaka/ui/panel/daemon.py`：`_write_pty` 换成每窗格写队列（`Pane.writes` + `_PendingWrite`），`Daemon._drain_writes` 按 `loop.add_writer`
  可写性排空，`Daemon._deliver` 等待消息完整进入窗格：`pane.send` 等 `SEND_TIMEOUT=8s`，超时丢弃未送出的余下部分并把落地字节数报给调用方
  （保留失败信号）；`pane.input` 等 `INPUT_TIMEOUT=1s`，超时仍留在队列里随程序读取投递（按键不阻塞面板），回复带 `queued`。
  文本与回车合并成一条消息；窗格退出/关闭时挂起写以明确原因失败（`_fail_writes`）；队列上限 1 MiB。`_serve_client` 对协程结果 await。
- 对照：herdr `src/pty/actor.rs` 用有界 channel + 独立写线程阻塞 `write_all`，从不截断；daemon 单事件循环，所以改成可写性驱动 + 可等待回复。
- 测试：`tests/test_panel_pane_delivery.py`（300 KiB 消息随程序读取完整落地、超时丢弃并报数、输入留队列、多条消息保序、窗格退出失败挂起写、队列上限、text+Enter 一条消息）。
- 未做：pi 上游 `StdinBuffer` 的 pasteMode 无超时，保持不动；投递修好后关闭符不会再丢。

### B2 · 已修（2026-09-18 11:1x）
- `misaka/ai/utils/overflow.py`：新增 `output_limit_error(message)` / `hit_output_limit(err)`，`length` 停止的文案改为
  "the reply hit the model's output token limit (N tokens) before it finished; no tool call was made"。
- `misaka/core/research/window.py`：`observe` 用上述文案；`_execute_turn` 按 `options["thinking"]`（planner 传 `high`）临时
  `session.setThinkingLevel`，轮次结束恢复用户档位（切换写进 transcript，可见）。
- `misaka/core/platform/session.py`：headless 路径同样把 `length` 报成可读错误（之前 `length` 不算错，JSON 抽取会静默失败）。
- `misaka/core/research/planner.py`：`_command` 在错误命中输出上限且命令未被记录时，用 `output_limit_nudge(name)` 在同一会话再跑一轮，
  第二次仍失败才暂停 run。
- 测试：`tests/test_research_output_limit.py`。
- 配置侧建议（未改）：`~/.misaka/agent/models.json` 里 sub2api-claude 的 claude-fable-5 `maxTokens` 是 32000，pi 目录为 128000；若代理透传，抬高它。

### B13 · 已修（2026-09-18 11:4x，方案 A）
- `misaka/core/research/workflow.py:_drive_tasks_inner`：卡片 generation 变化不再 raise，记一条进度 "Card X moved to attempt N; the run follows it" 并跟随新一代。
  `_halt_scope` 的连坐逻辑未动（stop/budget/驱动器自身异常仍停整个 scope）。
- 测试：`tests/test_research_generation_follow.py`。

### B27 · 已修（2026-09-18 11:4x，方案 B：显式完卡工具）
- `misaka/core/network/todo.py`：新增工具 `misaka_card_complete(summary)`——只在卡由本会话持有、契约 `## deliverable` 文件在 `output_dir` 下非空时记录
  完成（红队卡的 critique 缺失也在此时报错）；`agent_end` 只在本代记录了完成时才生成 summary；`agent_settled` 只提交已声明完成的轮次，
  普通 stop 轮次不再提交，卡保持 running，每代提醒一次（`submission-held`，不触发新轮）。`IncompleteSubmission` 的补交提示改为再调工具。
- `misaka/core/network/worker.py`：`contract_deliverable` / `missing_deliverable`；`COMPLETION_INSTRUCTIONS` 与"上一次尝试失败"文案改为要求调用工具。
- `misaka/core/platform/session.py`：`BOOKKEEPING_TOOLS` 加入该工具；`wiring/collaboration.py` 加指南一条。
- 语义变化：headless 卡若会话结束前没调工具，daemon 仍按 `mark_unsettled`（协议违规）计一次失败并重派，提示词已说明。
- 测试：`tests/test_card_completion.py`（回复轮不完卡且只提醒一次、缺交付物拒绝完成、声明后结束轮次提交、abort 轮不提交但声明保留、名单收录）。

### B20 / B33 · 已加可观测性（2026-09-18 12:0x；机制未定，逻辑未改）
- `misaka/core/platform/processes.py`：`explain_liveness(pid, expected)` 返回 (alive, reason)；`identity_is_alive` 对"pid 已消失"之外的判死
  （identity 不一致、status 读不到、zombie）每种每 pid 只 warning 一次并附对比值；同时做一次自检 `_self_check`——本进程自身 identity
  与首次计算不一致就 warning（"process identity drifted inside this process"）。这正是 B20 旧 daemon 需要的那行证据。
- `misaka/core/research/runs.py`：`note_claim_failure` 在 claim 失败时说清是 key 不匹配还是 pid/identity 不匹配（带两边的值），
  写进 `research_branches.last_error`（只在为空时）；`node.py` 两处 "superseded execution" 都打印并记录它。
- `misaka/core/research/workflow.py`：`_runner_error` 可附子进程窗格最后几行；`PaneSpawner.tail` 用 `pane.read` 取回，`_pane_tail` 对无此能力的 spawner 返回 None。
  下次再出 B33，驱动器的错误会直接写出 "claim refused: runner key mismatch: row holds …, this process holds …"。
- 测试：`tests/test_liveness_observability.py`。

### B6 · 已修（同批）
- `misaka/config/engine.py:configure_logging`：root logger 装 `~/.misaka/agent/misaka.log` 的 RotatingFileHandler（5 MB × 3，WARNING 以上），幂等；
  `cli/app.py:main` 与 `ui/panel/daemon.py:main` 启动时调用。有了 root handler，Python 的 lastResort 不再把 warning 打到 stderr/窗格。
  `MISAKA_CODING_AGENT_DIR` 可改目录（测试用）。

### B11 · 已修（2026-09-18 12:2x）
- `misaka_lcm/host/ingest.py`：可跳过的落盘尾巴从 `{error, length}` 扩到 `DROPPABLE_STOPS = {error, length, aborted}`；
  `align_sources` 对不上返回 None，`describe_mismatch` 给出两边在 first_mismatch 处的 role/stop/长度/前 80 字；`source_indices` 保留为抛错版本。
- `misaka_lcm/host/context_engine.py:_aligned`：`prepare` 不再 raise。对不上时长度相等 → 保留实时列表、位置一对一；长度不等 → 本轮发落盘视图；
  两种情况都 `logger.warning` 一条带差异摘要（进 `misaka.log`）。姿态照上游 `_reconcile_ingest_cursor_from_store`：交来的列表就是要发的，库自己重新对齐。
- 未做：让 `build_session_context` 直接套用 pi 实时列表的删尾规则从而删掉整套缝合——那是 pi 内核层的改动，按仓库规则要先复核 pi 上游行为。
- 测试：`tests/test_lcm_view_alignment.py`。

### B31 · 已修（2026-09-18 13:0x，不动系统提示词，不写转录）
- `misaka/core/research/wiring/research.py:context`：每次请求从 board 现查本会话发起的 run（`runs.for_session`，按 `origin_session`），
  有 run 在跑（`runs.ACTIVE` = active / waiting_input / stopping）→ 在请求消息最前面插一条临时 `research-mode` custom 消息，内容是
  `RESEARCH_DISCIPLINE` + 当前 run id；全部暂停（failed / stopped）→ 插新文案 `workflow.RESEARCH_PAUSED`（等用户 `/research resume`，
  不许自己续卡、改 board、代替驱动器）；都 done 或没 run → 什么都不插。这条消息只存在于本次请求，不进转录、不进 LCM 库、不受压缩影响。
  `begin()` 原先整段 discipline 的 custom 消息缩成一行"研究模式已开启"通知。
- 时序核实：`sdk.transform_context` 先跑 `prepareContextMessages`（LCM 用 `session.messages` 对齐自己的库），再跑 `moments.context`，
  所以这条插入对 B11 的对齐是不可见的。
- `misaka/core/research/runs.py`：`for_session(con, session_id)`、`is_active(con, run_id)`，board 没研究表时返回空/False。
- 测试：`tests/test_research_mode_notice.py`。

### B26 · 已修（同批）
- `misaka/core/network/sister_runtime.py:notify_row`：done 通知只在该 run 仍被驱动（`_research_run_active`）时不触发 LO 轮次；
  run 暂停/失败/结束、或 board 查不到 → 照常唤醒 LO。
- `research.py:_feed_noise` 同步：done 通知只在其 run 处于本会话的活跃 run 集合里时才当作噪音过滤。
- 测试：`tests/test_sister_notification_wakes_lo.py`、`tests/test_research_mode_notice.py`。

### B24 · 已修（同批，只加事件，方案 A）
- `misaka/core/platform/cards.py:reconcile_one`：文件→索引回写改了 `status` 时追加一条 `reconciled` 事件
  `{"from": 旧, "to": 新, "source": "card file"}`。行为不变（文件仍是契约、仍会覆盖手改）。没有给 LO 加 resume 工具（用户定：`/research resume` 只能人跑）。
- 测试：`tests/test_card_reconcile_event.py`。

## P2 第一批修复（2026-09-18 下午）

### B29 / B14 / B18 / B19 / B22 · 已修（一张卡一个窗格一个写入者）
- `misaka/ui/panel/daemon.py`
  - 新增 `Pane.fresh_terminal()`（按当前尺寸重建 ghostty 终端，不继承上一个程序的模式/滚动区/备用屏）；`_spawn` 的初始
    `TIOCSWINSZ` 改用 `pane.size()`（新窗格仍是默认 32×120，换程序时保住当前尺寸）。
  - 新增 `_stop_program`（摘掉 reader→关 fd→SIGTERM→2 s→SIGKILL，不广播 exited，因为窗格没退）、`_await_exit`、
    `_replace_program`（在原窗格里换程序：pane id、布局座位、尺寸全保留，argv/cwd/title 换新，缓冲与状态清空）。
  - 新增 `_card_pane_to_reuse(task_id)`：这张卡的窗格 = 活的优先、其次最新。`_drain_blocked_run` 换成
    `_vacate_card_panes(con, row, keep=..., verify_previous=...)`：停掉所有还攥着该卡会话的程序，`keep` 只换程序不关窗，
    其余关掉并等其退出；blocked/triage 仍额外验证上一代记录进程已死（语义不变）。
  - `continue_card` / `run_card` / `_host_card` 改为协程，先 vacate 再 claim，再 `_host_card(..., reuse=pane)`；
    RPC 分支通过 `_hosted()` 返回协程，`_serve_client` 照旧 await。
  - `open_card_session` 改开面板自己的只读跟随器 `misaka chat --read-only --session <transcript>`（无引擎、无写入者、
    无 catalog 记录、不可能跑轮次）；该卡已有窗格时直接返回那个窗格。
  - `panes.list` 每行新增 `claimed`（是否持有 claim）；`card.stop` 改取持有 claim 的窗格而非第一个匹配。
  - PROTOCOL 48 → 49：`claimed` 是工具依赖的新字段，旧 daemon 不会返回它，靠协议号强制替换；工具侧同时兜底（读不到该字段就按"是尝试"处理，等于旧行为）。**改完要重启面板。**
- `misaka/core/network/wiring/network.py`：`_pane_for_card` 优先返回持 claim 的窗格；`misaka_sister_message` 只在
  `claimed` 的窗格上做终端投递（否则走信箱）；`misaka_sister_resume` 的描述/回话改为"只读阅读窗，续她请用
  misaka_sister_message，会接管同一个窗格"。
- `misaka/core/network/ally/extension.py`：`misaka_ally_close` 只拒持有 claim 的卡片窗格，遗留的只读/旧代窗格可关；
  `wiring/collaboration.py` 的指南同步。
- 测试：`tests/test_card_pane_reuse.py`（16 条）。

### B15 / B25 · 已修（`misaka_card_requeue`）
- `misaka/core/network/wiring/network.py`：新增工具，内部就是 `tasks.reopen_task`（`/research resume` 自己走的那条路）：
  只收 stopped / failed，要 `confirmed`，研究卡一律拒绝并指向 `/research resume <run>`，代次不符报"changed underneath"。
  指南写明"不要手改 board 的表，卡片文件是契约"。
- 测试：`tests/test_card_requeue.py`（10 条）。

### B3 · 已修（catalog 与 /tmp 控制目录的回收）
- `misaka/core/session_catalog.py`：新增 `sweep_dead()`——记录的 pid **确实消失**时（`explain_liveness` 的 "pid gone"，
  identity 不一致一律不动，因为 B20 证明那个判断会错），会话文件还在就改写成 saved 并摘掉 control/paused，文件不在就删记录；
  两种情况都删掉记录里指的 `/tmp/misaka-session-*/` 目录（前缀 + 父目录双重校验）。
- 另外扫 `/tmp` 里没有任何记录指向、且 `control.sock` 上没人在听的 `misaka-session-*` 目录（连一下就知道；新建不足 60 秒的不动，避免撞上刚创建目录还没 bind 的会话）。实测当时机器上 27 个目录里 24 个属于这一类。
- `misaka/ui/panel/daemon.py`：`_reap` 确认进程死透后调 `_sweep_catalog()`，`_watch_cards` 启动时扫一次（接管上一个面板的遗留）。
- 测试：`tests/test_session_catalog_sweep.py`（12 条）。

### B7 · 已修（被拒的凭据刷新一次并重跑该轮）
- 查证上游：pi 0.83/0.85 都没有这套机制（dist 与还原出的 TS 里 `rejectedApiKey` 零命中），401 在 pi 里是终结错误，
  auth 只按时钟刷新。`rejected_api_key` 是 misaka 自己在 `auth_storage.refreshOAuthTokenWithLock` 上加的，全仓库只有
  web 网关在用。所以这是 misaka 的增补，`agent_session.py` 处按仓库规矩留了 `# MISAKA fork:` 注释。
- `misaka/core/model_registry.py`：新增 `is_rejected_credential_error()`（401/unauthorized/invalidated oauth/invalid api key…）、
  `getAuth` 记下每个 provider 最后发出的 key、`recoverRejectedCredential(provider)`——只对存储的 OAuth 凭据生效，
  带 `rejected_api_key` 走锁内强制刷新；同一个被拒的 key 只救一次（第二次被拒是账号的答复，不是副本过期）。
- `misaka/core/agent_session.py`：`_recover_rejected_credential()` + 在 `_handle_post_agent_run` 的重试判断里
  `is_retryable or recover(...)`，随后复用 pi 自己的 `_prepare_retry`（预算、退避、事件、删掉失败的 assistant 消息）。
- 测试：`tests/test_credential_recovery.py`（28 条）。

### B23 · 已修（守卫只在无人看管处因动态语法拒绝）
- `misaka/core/skills/wiring/skills.py`：`_command_touches` 改为返回 `"path" | "dynamic" | None` 并接受 `unattended`；
  字面路径判定不变、对所有会话一律生效；`$(`/反引号/`${` 与 shlex 解析失败只在 `kind in ("card", "child")` 时拒绝。
  拒绝文案拆成两条：碰到技能目录 vs 无人看管且展开不可核实。
- 测试：`tests/test_research_repair_contracts.py` 内的守卫用例改成按理由断言，并新增有人看管/无人看管两组对照。

### P2 第一批的自审（2026-09-18 傍晚，五维对抗式审查 + 逐条反驳验证）

对上面九条的改动做了一轮独立审查：五个维度各自读代码提问题，每条再交给一个专门试图**推翻**它的验证者，
只有推翻不掉的才算数。32 条提名，20 条坐实（含 5 条我自己的测试问题），已全部修掉：

**窗格**
- `_stop_program` 原来先置空 `pane.fd` 再调 `_fail_writes`，而注销写回调那一支的条件是 `pane.fd is not None`——
  于是 pty 写回调永远留在事件循环里。验证者实测：fd 号被下一个窗格复用后，`_drain_writes` 一秒触发 84 万次，
  单线程事件循环 100% 占用且不会自愈；同时 `writer_armed` 卡在 True，该窗格的输入队列再也排不出去。
  改为 fd 还有效时就先 `_fail_writes`。
- `_stop_program` 原来先关 pty master 再发 SIGTERM。窗格程序是持有该 pty 的会话首进程，关 master 等于先 SIGHUP，
  实测子进程 200 ms 内死于 SIGHUP、后面的 SIGTERM 报 ESRCH——那 2 秒"留给程序存盘"的梯子从来没跑过。
  改为：先摘 reader（避免把换程序广播成窗格退出）、发 SIGTERM、轮询期间顺手丢弃输出（没人排空 pty 会把程序卡住）、
  进程走了才关 fd。
- 原来在 board 发牌**之前**就杀掉窗格里的程序，claim 被拒（配额满、被别的派发器抢走）就留下一个坐在布局里、
  没有程序的空窗格。改为：blocked/triage 的"上一代进程必须已死"作为纯判定仍在 claim 前（拆成 `_verify_previous_dead`），
  真正动手的清场挪进 `_host_card` 的 try 里，也就是卡片已经归我们之后。
- `open_card_session` 遇到程序已退出的窗格会把它当"已经开着"直接返回，用户点了等于没反应。改为：活的直接返回，
  死的就在原窗格里换成只读跟随器。
- `card.stop` 在换程序的空档会因 `pane.proc` 为 None 崩掉，改为容许无 pid（停止照样成立）。
- `_replace_program` 顺带按新程序的环境重置 `pane.ally`。

**凭据**
- 一次性护栏在刷新**之前**就记账，网络抖动导致刷新跑不起来时，这个 token 就永久失去了被替换的机会。改为刷新
  真正有了回答才记账。
- 用的是会话当前模型的 provider，而不是**这条失败消息**自己的 provider；改为后者。
- 刷新在查重试预算之前发生，且第一次刷新后 `_lastResolvedApiKey` 已经换成新 key，同一轮里第二次被拒会再转一次令牌。
  改为：只在 `_retryAttempt == 0`、且重试开着且预算 ≥1 时才救，一轮最多一次。
- 把凭据放在 header 里的 OAuth 流程（`apiKey` 为空）从来不会被记住，恢复对它们静默失效。改为回落到该流程自己的
  `getApiKey`，记的是 access token 本身而不是 "Bearer …"（存储端比较的就是前者，记错了会让强制刷新变成不强制）。

**目录清理**
- 重写记录时没有 `_retire` 那个 `instance` 护栏，扫描途中若有新会话在同一份转录上发布，它的指针会被改写成 saved。
  改为写之前重读比对，变了就跳过。
- 判活探测排在"已退休就跳过"之前，每关一个窗格都对整个索引做一遍（可能 fork `ps`）。改为便宜的判断在前。
- `_release_control` 先 realpath 再校验 /tmp，被记录的父目录若是符号链接就会指到别处。改为父目录是链接就不动。

**工具与守卫**
- `misaka_card_requeue` 的回话说"派发器会接手"，但**没有任何东西轮询 ready 卡**——每一次开工都得有人明确要。
  改为说清楚：已回队、尚未运行、由 `misaka_dispatch` 启动。
- `misaka_ally_list` 跳过所有已退出的窗格，于是 B22 要关的那种遗留卡片窗格根本看不见，工具等于没法瞄准。
  改为列出已退出的卡片窗格（非卡片的死窗格仍由面板自己收），每行加 `alive` 与 `claimed`。
- **B23 的豁免范围原来定错了**：`dm` 和 `bare` 也被当成"有人看着"，可它们是脚本化的一次性回合，没人看。
  改为只有 `foreground`（真有窗口的那种）豁免，其余一律按无人看管处理。
- 守卫还有两个旧洞：裸 `$HOME` 不算替换（只认 `${...}`/`$(...)`/反引号），以及 `bash -c "…"` 里的路径不会被拆开看。
  两个都补上了，同时确认 `ls -la`、`git status`、`sqlite3 board.db 'select 1'` 这类字面命令在任何会话里都照过。

**我自己的测试**
- 扫描测试直接对真实 `/tmp` 动手：跑测试期间把机器上 27 个 `misaka-session-*` 目录清到只剩 2 个（删掉的都是死孤儿，
  两个存活会话的没动，但测试绝不该这样）。加了 autouse fixture，把 `_CONTROL_PARENTS` 钉死在各自的临时目录里。
- 并发测试去掉锁也能过；补了一条只有"第二个请求确实等到了第一个 claim 完成"才成立的断言。
- 几条用 `inspect.getsource` 钉实现文本的测试改成真跑：重试判断用桩会话执行 `_handle_post_agent_run`，
  `_reap` 的清理用真调用验证，守卫的会话类型改成按五种 kind 跑真 `SkillsPart`。

### B8 / B16 · 已修（2026-09-18 晚，地址模型：角色、卡片、会话三种）
- `misaka/core/network/messages.py`：`messages` 表加 `to_task`、`to_session`、`sender_task`、`sender_session`（`connect()` 幂等迁移）。
  `resolve_address()`：角色照旧广播；`t_xxxxxx` 解析成该卡 assignee 的信箱 + `to_task` + 当前代次，要求卡在 running 且有活会话、
  同项目、不是自己；会话 id 解析成该会话的 `inbox` + `to_session`，要求活着且读信、不是自己。给自己角色发信直接拒绝并教它用卡片/会话地址。
  点名信只投活会话，不唤醒 `misaka dm` 联络会话。`pending()` 按读者的会话 id 与卡片 id 过滤；`delivery_plan()` 把绑定到旧代次的
  卡片点名信作废。投递渲染带 `<from-card>`/`<from-session>`，提示回信可以点名。
- `misaka/core/session_catalog.py`：`live_session(id)`、`live_card_session(task_id)`。
- B16 两条路径都走"首次尝试 + 把话寄到该次尝试的信箱"：
  daemon `continue_card` 对没有转录的卡不再拒绝，claim 新一代后跑契约（无 `--resume`），事件记 `started`，LO 的话由 `_mail_card` 寄成
  `to_task` 邮件；`sister_runtime.message` 对 `never_ran`（已结算、无 agent_id、无 session_file）的卡 reopen → launch → 寄信，
  `mode="first-attempt"`。`misaka_sister_message` / `misaka_sister_resume` 的回话说明这是首次尝试。
- 测试：`tests/test_message_addressing.py`（21）、`tests/test_card_first_attempt.py`（4）、`tests/test_card_pane_reuse.py` 加 2 条。
- 顺带记下可选项（未做）：正在跑的卡现在靠 `pane.send` 敲键盘投递，有了卡片地址后可以改走邮箱，绕开 B1 那类 pty 队列问题。

### B10 · 已修（2026-09-18 晚）；B9 · 按 Hermes 设计不改
- 出处核实：`download_file` 是 misaka 自己的（2026-08-27 `aeba191`，能力补全计划 W3"受控下载"），Hermes 没有下载工具——
  它读内容走 `web_extract`（远端后端抽取，超预算才把全文写到 `cache/web/`），拿原文件走终端 `curl`，不做任何校验。
  B9 的安全层 `_web/bounded.py` 是 Hermes `tools/url_safety.py` 的移植，"解析结果里任一私网地址就整站拒绝"是 Hermes 的原样行为
  （`is_safe_url` 逐个地址检查、fail closed），用户决定保持一致，不改。
- `misaka/core/tools/download_file.py`：`_SIGNATURES` 加 `.djvu`（魔数 `AT&TFORM`）；`image/vnd.djvu` 经 `mimetypes` 猜出后缀，
  无扩展名的 URL 也能落成 `.djvu`。
- `misaka/core/documents/index.py`：新增 `_djvu_pages`——`djvutxt` 取文字层，按换页符切页（和 pdftotext 同一形状）；没有可用文字层且
  `ddjvu` 在时渲染成 PDF 交给 `_pdf_pages` 走既有 OCR，渲染读不出更多就保留原文字层；工具缺失/出错记 `meta["djvu_error"]`。
  登记进 `_EXTRACTORS`，`SCAN_SUFFIXES` 自动覆盖，于是"下载即索引"和材料包都不用改。
- 测试：`tests/test_djvu_documents.py`（用 djvulibre 现造夹具，缺工具就 skip）、`tests/web/test_download_file.py` 加 3 条。

### 收尾裁定（2026-09-18 晚，用户决定）
- **B4** 不追：来源不明的 10043 进程只出现过一次、无会话文件，不再投入。
- **B20 / B33** 不再主动修：机制未定，探针已埋（`explain_liveness` 的误判 warning、`note_claim_failure` 写进 `last_error` 与 `misaka.log`），
  再犯时凭日志定位。
- **B21** 按设计关闭：窗格组修完后一张卡只有一个窗格；只读跟随器不注册 catalog 记录，所以它出现在标签页而不在 Sessions 里是预期行为。
- 仍开着的只剩 LCM 四条（B5、B17、B30、B32），用户要求暂不碰；B9 与 B12 按设计不改。

### LCM 四条的裁定（2026-09-18 夜）
- 逐条对照上游 hermes-lcm（`~/.hermes/plugins/hermes-lcm`，vendor 与之只差 4 行）：
  B5 上游消息体没有 `thinkingSignature`，外置是通用递归遍历，问题出在 misaka 的 pi 消息喂法；B17 上游同样按窗口比例压缩、对大窗口
  模型还往上调，无绝对上限，问题在切模型的治理不在 LCM；B30 是 pi 的 `populateHistory` 回放和 LCM"摘要占第一条 user"在 misaka 里
  碰出来的，Hermes 的 CLI 历史只记敲入行；B32 上游原样（3 s 共用截止、`full_text` 模式静默少一臂）。
- **B17 不改**（用户决定，走预算/契约治理）。**B32 只改 misaka 侧默认值**：`host/config_bridge.load_config` 在未设
  `LCM_EMBEDDING_QUERY_TIMEOUT_S` 时把 `embedding_query_timeout_s` 设为 30 s（上游 3 s），来源记 `misaka.default`；vendor 不动。
  测试：`tests/test_project_lcm.py` 加 1 条；`SETTINGS.md` 记一段。
- B5、B30 待定：都是 misaka 侧十几行的缝合修法，用户尚未拍板。

### B5 / B30 · 已修（2026-09-18 夜，都修在 misaka 与上游的接缝上）
- **B5** `host/ingest.py:to_upstream`：thinking 块不再进 LCM 的持久负载（`_NOT_CONTENT = {toolCall, thinking}`），和 Hermes 运行时
  把 `thinking`/`reasoning`/`redacted_thinking` 从 `content` 里过滤掉的做法一致。签名随之不会再被当"大输出"外置。`_text_of` 本来就跳过
  thinking，LCM 从未索引过这些文本，无损失；provider 回放来自 pi 的实时消息，不经此存储。测试 `tests/test_lcm_reasoning_not_stored.py`。
- **B30** 根子是 misaka 给压缩条目加的 `contextMessages`（pi 没有）同时喂了模型路径和显示路径，LCM 占在 user 位的摘要被画成用户气泡并
  在每次重开时经 `populateHistory` 记进编辑器历史。新增 `session_manager.session_entry_to_display_messages`：压缩条目按 `details.lcm.scaffolds`
  标出的引擎自有消息投影成 pi 的 `compactionSummary`（去掉 prompt-guard 围栏），其余原样；`interactive_mode.renderSessionEntries` 和
  `transcript.appendEntries` 改走它，`buildSessionContext` 给模型的那条路不动。测试 `tests/test_compaction_display.py`。
- 未做：`input-history/last-order.json` 里已有的十段摘要，用户未答复是否清理，留着等它被后续输入挤出 100 条上限。

### 收尾三件（2026-09-18 夜，用户要求）
- **running 卡的投递改走邮箱**：`misaka_sister_message` 对正在跑的 Sister 卡不再 `pane.send` 敲键盘，而是 `_mail_running_card`
  寄一封 `to_task`+当前代次的信，Sister 的收件泵在下一个工具边界送达；ally 卡（第三方 CLI，不读邮箱）仍走键盘。
  测试 `tests/test_sister_steering_by_mail.py`。
- **`core/tools/_web/` 并入 `core/web/`**：八个模块原地搬入（bounded、url_safety、screening、website_policy、evidence、
  negative_cache、single_flight、academic），58 个文件的引用改写，`PI_ORIGIN.md` 的三处记录更新。事先核过：`core/web/network.py`
  和 `cache.py` 对这些模块的引用是函数内懒 import，合并不成环。
- **`input-history/last-order.json` 清理**：100 条里 24 条是压缩摘要块（都以 `<<<UNTRUSTED-DATA name="lcm:compaction:` 开头），
  已删，572 KB → 13.5 KB，备份在 scratchpad。注意编辑器把历史整份存内存、每次输入整份重写，正在跑的 LO 会把旧列表写回；
  重启面板后才算落实，B30 修完后新加载不会再塞进去。

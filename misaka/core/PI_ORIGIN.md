# core/ 里什么是 pi 的，什么是 misaka 的

`misaka/core/` 是 misaka 的内核：pi 的 coding-agent 内核（`packages/coding-agent/src/core/`，
对照件为上游仓库 `v1.3.0` 标签的 `packages/coding-agent/src/core/`；2026-09-23 从 0.85.1 基线移植了 0.86.0–0.87.1 的内核改动，台账见 `docs/plans/pi-0.87-port-2026-09-23.md`，2026-10 又从 0.87.1 经 0.99.1、1.0.0 移植到 1.1.0，台账见 `docs/plans/pi-0.99-port-2026-09-30.md`；2026-10-21～22 按分区从 1.1.0 经 1.2.0 移植到 1.3.0（ai、agent、tui、mcp、codemode、chord、durable、env 与 coding-agent 各区全部对齐），台账见 `docs/plans/pi-1.3-port-2026-10-21.md`）加上 misaka 自己的产品能力。
两者按**目录/文件**分得开，按**目录名**分不开——这份清单就是那条线。改 pi 文件之前先看
`docs/audits/pi-kernel-compat-audit.md` 的去向总表；改了要在改处留 `# MISAKA fork:` 注释说明理由
（先例：`system_prompt.py:96`）。新增 misaka 子树要登记到下面。

同名规则：pi 用 kebab-case（`agent-session.ts`），misaka 用 snake_case（`agent_session.py`），一一对应。

## pi 来源（一字不动，或已标注分叉）

pi 1.3.0 `src/core/` 顶层 59 个文件里 55 个与 misaka 同名对应；**未移植**的 4 个见末节。另有 5 个顶层文件是
misaka 自己的（见「misaka 新增」）。逐目录的上游改动地图（1.0.0、1.1.0 两轮）存在 `docs/plans/pi-maps/`。

子目录：

| 目录 | 说明 |
|---|---|
| `tools/` | pi 的内置工具；misaka 加了 4 个文件与 1 个子目录（下表） |
| `extensions/` | pi 的扩展**协议**（types/loader/runner/wrapper），不是扩展本身；pi 的 `jiti-loader`、`jiti-static-loader`、`virtual-modules` 是 TypeScript 的模块加载，Python 无对应物 |
| `compaction/` | pi 的压缩 |
| `export_html/` | pi 的会话导出；`template.{html,css,js}` 与 `vendor/` 的 marked、highlight.js 照搬自 1.3.0（1.1.0→1.3.0 只有 `template.js` 变了） |

## 用户目录：和 pi 的有意分叉（2026-09-21，对齐 pi 时**不要**带回来）

pi 把全局文件放在 `~/.pi/agent/`，这一层的真实作用是让全局目录不和 `$HOME` 下的项目目录 `<cwd>/.pi/`
重合。misaka 改用一个平铺的 home（`misaka/config/home.py`：唯一的根 `MISAKA_HOME`、一张登记表），
用 Hermes 的做法隔开两者：`home.project_dir()` —— home 永远不是项目目录。台账见
`docs/plans/home-governance-2026-09-21.md`。对齐 pi 时注意：

- `get_agent_dir()` 保留，返回 **home 根**。pi 代码自己拼的 `settings.json` / `models.json` /
  `keybindings.json` / `themes` / `prompts` / `extensions` 因此无需改动，别改回 `<home>/agent`。
- pi 放在 agent 目录、但不属于"用户编辑"类的几样，这里各走登记表：`auth.json` → `credentials/`，
  `trust.json`、`models-store.json` → `state/`，日志 → `logs/`，`bin` → `cache/`。
  `ProjectTrustStore` 和 `ModelRegistry` 只在传入的目录/文件**就是 home 那份**时才这么放；
  嵌入方自己的引擎目录仍按 pi 的方式放在其设置旁。
- 凡是 pi 写 `join(cwd, CONFIG_DIR_NAME, ...)` 的地方，这里一律是 `home.project_dir(cwd)`，可能为 `None`：
  `settings_manager.FileSettingsStorage`、`package_manager.resolve`、`project_trust`、`prompt_templates`。
  pi `trust-manager` 里对 `~/.agents/skills` 的特判，在这里由 `project_dir` 统一承担。
- 已删除、不要恢复：`PI_CODING_AGENT_DIR` / `PI_CODING_AGENT_SESSION_DIR` 的对应物（布局环境变量只剩
  `MISAKA_HOME`）、`config/migrations.py`（pi 的旧版迁移，misaka 从未发布过那些旧布局）。
- `tests/test_home_governance.py` 会拦住带回来的字面量路径、`CONFIG_DIR_NAME` 和旧环境变量。

## 每个 agent 各自的推理强度：和 pi 的有意分叉（2026-09-27，对齐 pi 时**不要**带回来）

pi 只有一个全局 `defaultThinkingLevel`（加按模型的 `modelThinkingLevels`）。misaka 让 Last Order 和每个
Sister 各有一档：

- `settings_manager.ROLE_KEYS` 含 `defaultThinkingLevel`；`setDefaultThinkingLevel` 在角色会话里写该角色的
  `settings.json`，角色之外写全局。`getRoleThinkingLevel` 读角色自己那一档。
- 新会话与换模型时的取值顺序：显式（`--thinking`、卡片 model 的 `:level`）→ 会话自己记录的 → 角色自己的
  → 全局 `modelThinkingLevels` → 全局 `defaultThinkingLevel`。第三步 pi 没有，`sdk.py` 与
  `agent_session._getThinkingLevelForModelSwitch` 各有一处 `# MISAKA fork:`。
- `interactive_mode._apply_thinking_level` 先存默认值再切会话：角色文件写不进去时，文件和会话都不变
  （pi 的全局写不会在这里失败）。设置面板的推理强度项也走这条路。
- `tests/test_role_thinking_level.py` 钉住以上行为。

## misaka 新增

### 顶层文件

| 文件 | 职责 |
|---|---|
| `wiring.py` | 会话装配：`SessionSpec`；`TOOL_MODULES` + `tools_for`——只贡献工具的 core 模块走 pi 的 SDK 门 `customTools`（与内置工具同一张表，来源 `<sdk:>`），不进扩展清单；`PART_MODULES` + `parts_for`——还要被内核在时刻叫到的 core 模块，做成会话的 **part**（`part(spec)` 返回带 `tools`、`commands` 与时刻方法的对象），工具同样走 `customTools`，时刻由内核经 `moments.py` 直接调用，斜杠命令由内核的 `prompt()` 直接执行；`build_extensions`——只剩进程入口注入的捆绑扩展（`bundled`），core 一条都不贡献；`PROVIDER_MODULES` + `core_providers`——core 自己的 provider（moa），由注册表在每次重载时并入；`assemble` 返回**惰性**的 `Assembly`（第一次读才构造：worker 是先装配、后在环境窗口里写 `os.environ` 的，part 和当年的扩展工厂一样要在环境就位后再建） |
| `moments.py` | 内核对 misaka 子系统的直接调用：`Moments` 持有会话的 parts，在 pi 内核标出的时刻（session_start/shutdown/compact/compact_failed、before_agent_start、agent_end、session_before_compact、tool_call/tool_result、input、context）先于 ExtensionRunner 叫它们，折叠规则与 runner 相同——对应 pi 内核自己在时刻里做事的写法（`_checkCompaction` 那种直接调用），而不是把自己挂到 runner 上。part 在时刻之外要用的会话 API 也是 pi 现成的：`sendCustomMessage`/`sendUserMessage`（经 `Moments.send_message`/`send_user_message` 按 runner 的顺序调度）、`getActiveToolNames`、`registerCustomTools`/`refreshTools`（晚到的工具走 `customTools` 那扇门）。自 pi 1.2 起 `before_agent_start` 也在通知唤醒的 run（`triggerTurn` 的自定义消息）里触发，事件带 `messageType`；只为用户回合做的事分两种判断：board 收件只看 `messageType == "user"`（研究阶段的 TURN 消息不收件），SubagentStart 钩子用 `opens_turn(event)`（用户提示，或 `details[TURN]` 为真的自定义消息）。`CoreCommand` 是 part 的斜杠命令：`getSlashCommands` 以 source `core` 列出，`prompt()` 在扩展命令之前直接执行。接入点：`AgentSessionConfig.parts` ← `sdk`/`services` 的 `parts` 选项 ← `cli/engine.py` `parts=` ← `Assembly.parts`；改处都有 `# MISAKA fork:` |
| `session_catalog.py` | 面板的会话清单：先找会话文件，再标注它们的属主（生命周期记录只是指针与状态，不是第二份记录） |
| `session_control.py` | 让终端接到拥有该会话的 `AgentSession` 上（本地套接字），不另开第二个写会话的进程 |
| `session_overrides.py` | 一个会话用来代替全局设置的值：研究运行自己的压缩阈值与单次输出上限，按会话 id 存放 |
| `moa/` | Mixture-of-Agents 虚拟 provider（原 `extensions/moa/`，2026-09-03 用户决定进 core）。core provider 走 `core/wiring.py` 的 `PROVIDER_MODULES` 通道：`ModelRuntime.create` 调 `core_providers(runtime)` 把它并进 runtime，和 pi 的内置 provider 一样在每个 runtime 里，它的 `refreshModels` 在每次刷新时按当下的凭据重算预设；只发布聚合器 provider 已配置凭据的预设（裸注册表保持为空，`No models available` 照常触发）；会话侧两个时刻是 `MoaPart` |

### `tools/` 里的 misaka 文件

`_common.py` `download_file.py` `office.py` `web_fetch.py`，以及子目录 `_office/`（`office` 工具读写 Office 文档的实现，来源见其 `ORIGIN.md`）。`powershell.py` 自 1.1.0 起是 pi 的工具。抓取工具的安全层原在 `tools/_web/`，2026-09-18 并入 `core/web/`。

### `extensions/` 里的 misaka 文件

`startup_sections.py`

### 子目录（2026-09-03 由 `misaka/` 顶层与 `misaka/extensions/` 迁入）

| 目录 | 原位置 | 内容 |
|---|---|---|
| `platform/` | `misaka/platform/` | 地板：board 的 SQLite、卡片、预算、进程树、会话适配器、提示围栏、工具注册接缝、管理工具名单 |
| `network/` | `misaka/network/` + 5 个壳 + `extensions/last_order/ally/` + `misaka/observability/board.py` | board 的协调层；`wiring/` 是它的工具面（messages/todo/roster/roster_admin/network）；`ally/` 是经 ACP 驱动别家的 agent 当执行者（运行器 `card`、MCP 桥 `bridge`）；`board.py` 是终端渲染（`misaka board` 与 `extensions/observe.py` 共用） |
| `research/` | `misaka/research/` + `extensions/last_order/research.py` | 研究流程；`wiring/research.py` 是 Last Order 的 `misaka_research_view` |
| `subagent/` | `extensions/sisters/subagent/` | 子代理运行时（runtime/child/policy/hooks/agents 与内置定义） |
| `skills/` | `misaka/skills/` + `extensions/skills.py` | 分层技能索引，**整体替换**了 pi 的 `core/skills.ts`（审计 A-10）；`wiring/skills.py` 是三个工具与守卫 |
| `web/` | `extensions/web/` | 搜索后端、注册表、分发、抓取、抽取，以及抓取工具用的安全层（`bounded`、`url_safety`、`screening`、`website_policy`、`evidence`、`negative_cache`、`single_flight`、`academic`，2026-09-18 自 `tools/_web/` 并入） |
| `documents/` | `misaka/documents/` + `extensions/documents.py` | 语料索引与工作区；`pageindex/` 是 vendored 的 PageIndex（自带 MIT 许可） |
| `ask_user/` | `extensions/ask_user/` | `AskUserQuestion` 工具 |

### 壳（`wiring/`）的约定

原 `misaka/extensions/` 里的每个模块只是一个入口，真代码在别的包里。迁入时壳跟着体走，
放在体包的 `wiring/` 子目录、**basename 不变**。体包里已有同名模块（`network/messages.py` 与壳 `messages.py`）
是子目录存在的原因。壳的入口是 `part(spec)`（只贡献工具的是 `register(harn)`，`harn` 是 `ToolCollector`）；
自包含的（web、subagent、ask_user）入口留在包的 `__init__.py`。
`part(spec)` 返回的对象：`tools`（`ToolDefinition` 列表）、`commands`（`CoreCommand` 列表）、可选的 `attach(session)`
（拿到会话，之后直接调会话 API）、以及它需要的时刻方法 `async (event, ctx)`。`context` 是时刻名，part 不得拿它当属性名。

**core 不在扩展清单里**：它的工具走 `customTools`（来源 `<sdk:>`），时刻由内核直接调（`moments.py`），命令由 `prompt()` 直接执行——
和 pi 的 core 从不把自己挂到 ExtensionRunner 上是同一件事。启动屏的「Extensions」一区只剩用户装的和捆绑的；
`misaka/extensions/` 下的捆绑扩展都是 pi 意义上的内置扩展（`BUILTIN = True`，加载为 `builtin:<name>`），和 pi 的一样不上这一区，
由 `extensions` 设置里的 `-builtin:<name>` 关掉。

## `misaka/extensions/` 现在是什么

pi 意义上的**捆绑扩展**——自包含、只靠扩展 API、拔了无残留——按 misaka 原有的三层放：

```
extensions/<module>             每个角色：llama/、mcp/、codemode/ 与 tool_search/（pi 自带的四个；后两个 ROLES 只给 Sisters）、misaka_lcm/（LCM 上下文引擎：vendor/ 是 hermes-lcm 上游原样，host/ 是对 pi 事件的适配；关掉它 pi 原生压缩照常）、vision/（给看不见图的模型读图）、coverage/（coverage_scan 工具，并经 resources_discover 自带 coverage-maps 技能）
extensions/last_order/<module>  只有 Last Order 的槽位：reading/（阅读模式）
extensions/sisters/             其他角色的槽位（subagent 是 C 类，在 core/subagent，仅对 Sisters 暴露）
```

`extensions/__init__.py` 的 `discover(spec)` 扫文件夹、按文件夹定角色、按 `SESSION_KINDS` 门控——这是 misaka 加在 pi 之上的
（pi 的 `src/extensions/index.ts` 是无角色的平铺数组）。**`core/` 对这个包零引用**：进程入口（`cli/app.py`、
`cli/subagent_child.py`、`cli/research_node.py`）调 `cli/bootstrap.install()`，把 `discover` 赋给 `core/wiring.py` 的 `bundled`，
`build_extensions(spec)` 把它的结果排在 core 条目之前——对应 pi `main.ts` 把 `builtInExtensions` 排在最前。
misaka 自己的捆绑扩展照 pi 的规矩也是内置扩展：能关，不能删。pi 的捆绑扩展彼此 import（`mcp` 取 `codemode`、`tool-search`
的工具名，`codemode` 用 `tool-search` 的排序器），`interactive-mode` 也 import `mcp/config`，删掉文件夹不是它支持的用法。
内置扩展带的技能以扩展路径为名字空间（`builtin:coverage:coverage_maps`），与内联扩展的 `<inline:name>:…` 同理（`core/skills/layers.py`）。
pi 没有在 core 里起会话的进程，misaka 有（daemon 里的卡片、子代理子进程），所以需要这个注入点；这是此处唯一的创新。
裸的内核入口（`engine.main()` 无 options，只有测试走）不装钩子，会话里没有捆绑扩展。
这条零引用由 `tests/test_extension_boundary.py` 守住（2026-09-29 查出子代理运行时直接 import 了 LCM 的存储模块，
拔掉 LCM 后所有子代理一启动就失败）。扩展若要让它在子进程里的那份副本知道只有父会话知道的事，就在父进程收
`subagent_start` 事件（`core/subagent/runtime._extension_environment`，与 hermes 在父进程发的同名 hook 同位），
把变量写进事件的 `env`；core 自己的变量随后写入，同名时以 core 为准。
同一个测试还守着另外两条：core 自己的文字（提示词、帮助）不点名任何扩展的工具或功能——工具怎么用写在工具自己的
`promptGuidelines` 里，研究模式在自定义 SYSTEM.md 下把在场所有工具的说明补回（`research/prompting.system_context`）；
进程入口只用发现包本身（`discover`、`commands`），不 import 某个扩展（安装向导例外，用户 2026-09-29 认可）。
扩展要提供顶层命令就在模块里定义 `COMMAND` 与 `command(argv)`（`misaka lcm` 即如此）；压缩结果里哪些是引擎自己
写的摘要由 `CompactionResult.contextSummaries` 声明，core 不读任何扩展自己的 `details`。

pi 的 `pi install npm:/git:` 与 `~/.pi/agent/extensions/` 安装通道 misaka 未暴露（审计 A-11、A-1），
`docs/plans/install-uninstall-design-2026-09-03.md` 是那件事的设计稿。

## pi 有而 misaka 未移植的 `core/` 顶层文件

`bug-report` 与 `bug-report-upload`（pi 的 `/bug`：把报告上传到 pi 的 Radius 网关；misaka 没有这个功能，`/debug` 写本地文件）、
`index`（barrel，misaka 用 `__init__.py`）、`skills`（被 `skills/` 整体替换）。`trust-manager` 是移植了的（`trust_manager.py`）。
1.2.0 新增的 `keybindings-schema`、`key-id-schema`、`settings-defaults`、`settings-schema` 都已移植（`keybindings_schema.py`、`key_id_schema.py`、`settings_defaults.py`、`settings_schema.py`）；`settings_schema.py` 描述的是 misaka 的 settings.json（含 misaka 自己的各节），`keybindings_schema.py` 列的是 misaka 的键位（没有 `tui.altScreen.*`）。`scripts/generate_schemas.py`（pi `scripts/generate-schemas.ts`）由它们和 `modes/interactive/theme/theme_schema.py` 生成 `misaka/schemas/{models,settings,keybindings,theme}.schema.json`，`$id` 指向 misaka 仓库的发版标签，不指 pi.dev；同一脚本把内置主题 `dark.json`/`light.json` 的 `$schema` 改写成该版本的 theme.schema.json，发版改版本号后跑一次即可。

## core/ 之外的 pi 来源

`misaka/ai/`、`misaka/agent/`、`misaka/ui/tui/` 之外，下面这些包也逐行对照 pi 1.3.0（规则同上：分叉处标 `# MISAKA fork:`）：

| misaka | pi |
|---|---|
| `misaka/mcp/` | `packages/mcp` |
| `misaka/codemode/` | `packages/codemode`。运行时按 1.3.0 拆分：`runtime/execution.py`（宿主侧执行）、`runtime/vm.py`（VM 驱动）、`runtime/host.py`（只剩 worker 启动）、`runtime/worker.py`（worker 是线程，misaka 分叉）；QuickJS wasm 经 `quickjs_wasi/`（quickjs-wasi 3.6.2 的 Python 移植）跑在 wasmtime 上。**未移植**：`InlineCodemodeSandbox`（进程内 VM 会堵住 asyncio 循环）、`RemoteCodemodeSandbox` 与 `cloudflare`/`portable` 入口（只为 Cloudflare Durable Object 而设，misaka 没有这层边界） |
| `misaka/chord/` | `packages/chord`（1.3.0 重写后的 cell/object/host 运行时；`chord/node` 是 Python 分叉） |
| `misaka/durable/` | `packages/durable`。分叉：图片处理用 Pillow 代替 Photon（wasm），在线程里跑而非 Node worker，输出按 Photon 的行为对齐（16 位灰度 PNG、JPEG 4:4:4）；`structuredOutputSchema` 用 chord 的 TypeBox 替身 `chord/_typebox.py` 校验；`images/cloudflare`、`storage/sqlite/cloudflare` 只为 Cloudflare Workers 而设，未移植；本地执行环境的文件监视（`durable/env/node_watch.py`）在 macOS 上与 Node 一致：目录用 FSEvents（每个事件循环共用一个流）、文件用 kqueue |
| `misaka/env/` | `packages/env`（纯 Python）。分叉：守护进程的 list/pty 是 Python 实现——gitignore 匹配器代替 Rust 的 `ignore` crate（与上游守护进程逐例一致），pty 经启动器起程序，用 `select.poll` 等待（`select` 作后备） |
| `misaka/extensions/{mcp,codemode,tool_search,llama}/` | `coding-agent/src/extensions/` 的同名捆绑扩展（`builtin:<name>`） |

`chord`、`durable`、`env` 三个包 misaka 的任何入口都不可达（只有它们自己的测试用；`tests/test_stack_boundary.py` 守着这条边界）。
pi 1.3.0 的 coding-agent 也已不依赖它们；2026-10-21 用户裁定仍保留并逐函数跟进上游。
pi 1.2.0 删掉的实验性服务端/客户端/插件栈（`src/experimental`、`src/cli/experimental`、`packages/{client,protocol,server}`
与示例插件 `pi-example-plugin`），misaka 的对应物 `misaka/{experimental,cli/experimental,client,protocol,server}/` 与
`examples/` 已于同日随之删除，不要恢复。

## 旧路径 → 新路径（给 `docs/` 里 27 份历史文档对照用；历史文档不改）

```
misaka/platform/                       → misaka/core/platform/
misaka/network/                        → misaka/core/network/
misaka/research/                       → misaka/core/research/
misaka/skills/                         → misaka/core/skills/
misaka/documents/                      → misaka/core/documents/
misaka/observability/board.py          → misaka/core/network/board.py
misaka/app/composition.py              → misaka/core/wiring.py
misaka/extensions/__init__.py:discover  留在原地（捆绑扩展的发现）；core 由 misaka/core/wiring.py 的 TOOL_MODULES / PART_MODULES 点名
misaka/extensions/web/                 → misaka/core/web/
misaka/extensions/misaka_lcm/          留在原地：2026-09-03 用户决定 LCM 只是一个扩展（曾短暂迁入 core/lcm，同日迁回）
misaka/extensions/sisters/subagent/    → misaka/core/subagent/
misaka/extensions/last_order/ally/     → misaka/core/network/ally/
misaka/extensions/ask_user/            → misaka/core/ask_user/
misaka/extensions/mcp.py               → misaka/core/mcp.py（2026-10-03 删除：MCP 现为 misaka/mcp + misaka/extensions/mcp，逐行移植 pi（现对照 1.3.0），归 pi 侧规则）
misaka/extensions/skills.py            → misaka/core/skills/wiring/skills.py
misaka/extensions/documents.py         → misaka/core/documents/wiring/documents.py
misaka/extensions/{messages,todo,roster}.py
                                       → misaka/core/network/wiring/{同名}.py
misaka/extensions/last_order/{network,roster_admin}.py
                                       → misaka/core/network/wiring/{同名}.py
（coverage 留在 extensions/；agent_state、fork_split → core/network/wiring/panel.py（PanelPart，2026-09-03）；
 peek → core/network/wiring/network.py 的 misaka_sister_peek + sister_runtime.peek/transcript_tail；observe → TUI 内置 /board）
misaka/extensions/last_order/research.py
                                       → misaka/core/research/wiring/research.py
```

## 待办（迁移时看到、没顺手做的）

- `core/agent_session.py:235` 懒 import `core.skills.wiring.skills.parse_skill_invocation_message`：内核 import 一个 wiring 模块。
  解析器与写侧 `build_skill_message` 共用五个脚手架常量，干净拆分要把写侧一起挪成 `core/skills/invocation.py`。
- ~~`core/web/` 与 `core/tools/_web/` 是两个 web 目录；后者是 misaka 加进 pi 工具树的，可以并入前者。~~ 2026-09-18 已并入。

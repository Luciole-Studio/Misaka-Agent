# core/ 里什么是 pi 的，什么是 misaka 的

`misaka/core/` 是 misaka 的内核：pi 的 coding-agent 内核（`packages/coding-agent/src/core/`，
本机对照件为 `@earendil-works/pi-coding-agent` 0.83.0 的 `dist/core/`）加上 misaka 自己的产品能力。
两者按**目录/文件**分得开，按**目录名**分不开——这份清单就是那条线。改 pi 文件之前先看
`docs/audits/pi-kernel-compat-audit.md` 的去向总表；改了要在改处留 `# MISAKA fork:` 注释说明理由
（先例：`system_prompt.py:96`）。新增 misaka 子树要登记到下面。

同名规则：pi 用 kebab-case（`agent-session.ts`），misaka 用 snake_case（`agent_session.py`），一一对应。

## pi 来源（一字不动，或已标注分叉）

顶层 40 个文件与 pi `dist/core/` 同名对应；其中 pi 有而 misaka **未移植**的 10 个见末节。

子目录：

| 目录 | 说明 |
|---|---|
| `tools/` | pi 的内置工具；misaka 加了 4 个文件与 1 个子目录（下表） |
| `extensions/` | pi 的扩展**协议**（types/loader/runner/wrapper），不是扩展本身 |
| `compaction/` | pi 的压缩 |
| `export_html/` | pi 的会话导出（`template` 未移植） |

## misaka 新增

### 顶层文件

| 文件 | 职责 |
|---|---|
| `wiring.py` | 会话装配：`SessionSpec`；`TOOL_MODULES` + `tools_for`——只贡献工具的 core 模块走 pi 的 SDK 门 `customTools`（与内置工具同一张表，来源 `<sdk:>`），不进扩展清单；`PART_MODULES` + `parts_for`——还要被内核在时刻叫到的 core 模块，做成会话的 **part**（`part(spec)` 返回带 `tools`、`commands` 与时刻方法的对象），工具同样走 `customTools`，时刻由内核经 `moments.py` 直接调用，斜杠命令由内核的 `prompt()` 直接执行；`build_extensions`——只剩进程入口注入的捆绑扩展（`bundled`），core 一条都不贡献；`PROVIDER_MODULES` + `core_providers`——core 自己的 provider（moa），由注册表在每次重载时并入；`assemble` 返回**惰性**的 `Assembly`（第一次读才构造：worker 是先装配、后在环境窗口里写 `os.environ` 的，part 和当年的扩展工厂一样要在环境就位后再建） |
| `moments.py` | 内核对 misaka 子系统的直接调用：`Moments` 持有会话的 parts，在 pi 内核标出的时刻（session_start/shutdown/compact/compact_failed、before_agent_start、agent_end、session_before_compact、tool_call/tool_result、input、context）先于 ExtensionRunner 叫它们，折叠规则与 runner 相同——对应 pi 内核自己在时刻里做事的写法（`_check_compaction` 那种直接调用），而不是把自己挂到 runner 上。part 在时刻之外要用的会话 API 也是 pi 现成的：`sendCustomMessage`/`sendUserMessage`（经 `Moments.send_message`/`send_user_message` 按 runner 的顺序调度）、`getActiveToolNames`、`registerCustomTools`/`refreshTools`（晚到的工具走 `customTools` 那扇门）。`CoreCommand` 是 part 的斜杠命令：`getSlashCommands` 以 source `core` 列出，`prompt()` 在扩展命令之前直接执行。接入点：`AgentSessionConfig.parts` ← `sdk`/`services` 的 `parts` 选项 ← `cli/engine.py` `parts=` ← `Assembly.parts`；改处都有 `# MISAKA fork:` |
| `pi_manifest.py` `provider_display_names.py` `session_export.py` `settings_diagnostics.py` | 移植期加的小件 |
| `mcp.py` | MCP 客户端与按角色的服务器配置（原 `extensions/mcp.py`） |
| `moa/` | Mixture-of-Agents 虚拟 provider（原 `extensions/moa/`，2026-09-03 用户决定进 core）。core provider 走注册表的 `PROVIDER_MODULES` 通道：`model_registry._reloadLegacy` 每次重载末尾调 `core/wiring.core_providers(configured)` 把它并进表，和 pi 的内置 provider 一样在每个注册表里；只发布聚合器 provider 已配置凭据的预设（裸注册表保持为空，`No models available` 照常触发）；会话侧两个时刻是 `MoaPart` |

### `tools/` 里的 misaka 文件

`_common.py` `download_file.py` `powershell.py` `web_fetch.py` `_web/`

### `extensions/` 里的 misaka 文件

`startup_sections.py`

### 子目录（2026-09-03 由 `misaka/` 顶层与 `misaka/extensions/` 迁入）

| 目录 | 原位置 | 内容 |
|---|---|---|
| `platform/` | `misaka/platform/` | 地板：board 的 SQLite、卡片、预算、进程树、会话适配器、提示围栏、工具注册接缝、管理工具名单 |
| `network/` | `misaka/network/` + 5 个壳 + `extensions/last_order/ally/` + `misaka/observability/board.py` | board 的协调层；`wiring/` 是它的工具面（messages/todo/roster/roster_admin/network）；`ally/` 是把别人的 CLI 当 Sister 跑；`board.py` 是终端渲染（`misaka board` 与 `extensions/observe.py` 共用） |
| `research/` | `misaka/research/` + `extensions/last_order/research.py` | 研究流程；`wiring/research.py` 是 Last Order 的 `misaka_research_view` |
| `subagent/` | `extensions/sisters/subagent/` | 子代理运行时（runtime/child/policy/hooks/agents 与内置定义） |
| `skills/` | `misaka/skills/` + `extensions/skills.py` | 分层技能索引，**整体替换**了 pi 的 `core/skills.ts`（审计 A-10）；`wiring/skills.py` 是三个工具与守卫 |
| `web/` | `extensions/web/` | 搜索后端、注册表、分发、抓取、抽取；`tools/_web/` 是抓取工具用的安全层 |
| `documents/` | `misaka/documents/` + `extensions/documents.py` | 语料索引与工作区；`pageindex/` 是 vendored 的 PageIndex（自带 MIT 许可） |
| `ask_user/` | `extensions/ask_user/` | `AskUserQuestion` 工具 |

### 壳（`wiring/`）的约定

原 `misaka/extensions/` 里的每个模块只是一个入口，真代码在别的包里。迁入时壳跟着体走，
放在体包的 `wiring/` 子目录、**basename 不变**。体包里已有同名模块（`network/messages.py` 与壳 `messages.py`）
是子目录存在的原因。壳的入口是 `part(spec)`（只贡献工具的是 `register(harn)`，`harn` 是 `ToolCollector`）；
自包含的（web、subagent、ally、ask_user）入口留在包的 `__init__.py`。
`part(spec)` 返回的对象：`tools`（`ToolDefinition` 列表）、`commands`（`CoreCommand` 列表）、可选的 `attach(session)`
（拿到会话，之后直接调会话 API）、以及它需要的时刻方法 `async (event, ctx)`。`context` 是时刻名，part 不得拿它当属性名。

**core 不在扩展清单里**：它的工具走 `customTools`（来源 `<sdk:>`），时刻由内核直接调（`moments.py`），命令由 `prompt()` 直接执行——
和 pi 的 core 从不把自己挂到 ExtensionRunner 上是同一件事。启动屏的「Extensions」一区只剩用户装的和捆绑的；
`misaka/extensions/` 下的捆绑扩展各自用 `HIDDEN` 决定显示与否（pi 藏了它的 llama）。

## `misaka/extensions/` 现在是什么

pi 意义上的**捆绑扩展**——自包含、只靠扩展 API、拔了无残留——按 misaka 原有的三层放：

```
extensions/<module>             每个角色：llama/（pi 自带的 provider）、hermes_lcm/（LCM 上下文引擎：vendor/ 是 hermes-lcm 上游原样，host/ 是对 pi 事件的适配；拔掉它 pi 原生压缩照常）、agent_state fork_split（面板集成）、coverage observe
extensions/last_order/<module>  只有 Last Order：peek
extensions/sisters/             其他角色的槽位（subagent 是 C 类，在 core/subagent，仅对 Sisters 暴露）
```

`extensions/__init__.py` 的 `discover(spec)` 扫文件夹、按文件夹定角色、按 `SESSION_KINDS` 门控——这是 misaka 加在 pi 之上的
（pi 的 `src/extensions/index.ts` 是无角色的平铺数组）。**`core/` 对这个包零引用**：进程入口（`cli/app.py`、
`cli/subagent_child.py`、`cli/research_node.py`）调 `cli/bootstrap.install()`，把 `discover` 赋给 `core/wiring.py` 的 `bundled`，
`build_extensions(spec)` 把它的结果排在 core 条目之前——对应 pi `main.ts` 把 `builtInExtensions` 排在最前。
pi 没有在 core 里起会话的进程，misaka 有（daemon 里的卡片、子代理子进程），所以需要这个注入点；这是此处唯一的创新。
裸的内核入口（`engine.main()` 无 options，只有测试走）不装钩子，会话里没有捆绑扩展。

pi 的 `pi install npm:/git:` 与 `~/.pi/agent/extensions/` 安装通道 misaka 未暴露（审计 A-11、A-1），
`docs/plans/install-uninstall-design-2026-09-03.md` 是那件事的设计稿。

## pi 有而 misaka 未移植的 `core/` 顶层文件

`cache_stats` `index`（barrel，misaka 用 `__init__.py`）`model_config`（内联进 `model_registry.py`）
`model_runtime` `provider_composer` `radius` `remote_catalog_provider` `runtime_credentials`
`skills`（被 `skills/` 替换）`trust_manager`。逐条理由在 compat 审计。

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
misaka/extensions/hermes_lcm/          留在原地：2026-09-03 用户决定 LCM 只是一个扩展（曾短暂迁入 core/lcm，同日迁回）
misaka/extensions/sisters/subagent/    → misaka/core/subagent/
misaka/extensions/last_order/ally/     → misaka/core/network/ally/
misaka/extensions/ask_user/            → misaka/core/ask_user/
misaka/extensions/mcp.py               → misaka/core/mcp.py
misaka/extensions/skills.py            → misaka/core/skills/wiring/skills.py
misaka/extensions/documents.py         → misaka/core/documents/wiring/documents.py
misaka/extensions/{messages,todo,roster}.py
                                       → misaka/core/network/wiring/{同名}.py
misaka/extensions/last_order/{network,roster_admin}.py
                                       → misaka/core/network/wiring/{同名}.py
（agent_state fork_split coverage observe peek 留在 extensions/，见上节）
misaka/extensions/last_order/research.py
                                       → misaka/core/research/wiring/research.py
```

## 待办（迁移时看到、没顺手做的）

- `core/agent_session.py:235` 懒 import `core.skills.wiring.skills.parse_skill_invocation_message`：内核 import 一个 wiring 模块。
  解析器与写侧 `build_skill_message` 共用五个脚手架常量，干净拆分要把写侧一起挪成 `core/skills/invocation.py`。
- `core/web/` 与 `core/tools/_web/` 是两个 web 目录；后者是 misaka 加进 pi 工具树的，可以并入前者。

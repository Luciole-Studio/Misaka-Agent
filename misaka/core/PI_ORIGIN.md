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
| `wiring.py` | 会话装配：`SessionSpec` + `REGISTRY` + `build_extensions`。pi 的 `src/extensions/index.ts` 是硬编码数组，这里是自报 `ROLES`/`SESSION_KINDS` 的清单 |
| `pi_manifest.py` `provider_display_names.py` `session_export.py` `settings_diagnostics.py` | 移植期加的小件 |
| `mcp.py` | MCP 客户端与按角色的服务器配置（原 `extensions/mcp.py`） |
| `coverage.py` | `coverage_scan` 工具（原 `extensions/coverage.py`） |

### `tools/` 里的 misaka 文件

`_common.py` `download_file.py` `powershell.py` `web_fetch.py` `_web/`

### `extensions/` 里的 misaka 文件

`startup_sections.py`

### 子目录（2026-09-03 由 `misaka/` 顶层与 `misaka/extensions/` 迁入）

| 目录 | 原位置 | 内容 |
|---|---|---|
| `platform/` | `misaka/platform/` | 地板：board 的 SQLite、卡片、预算、进程树、会话适配器、提示围栏、工具注册接缝、管理工具名单 |
| `network/` | `misaka/network/` + 7 个壳 + `extensions/last_order/ally/` + `misaka/observability/board.py` | board 的协调层；`wiring/` 是它的工具面（messages/todo/roster/roster_admin/observe/peek/network）；`ally/` 是把别人的 CLI 当 Sister 跑；`board.py` 是终端渲染 |
| `research/` | `misaka/research/` + `extensions/last_order/research.py` | 研究流程；`wiring/research.py` 是 Last Order 的 `misaka_research_view` |
| `subagent/` | `extensions/sisters/subagent/` | 子代理运行时（runtime/child/policy/hooks/agents 与内置定义） |
| `skills/` | `misaka/skills/` + `extensions/skills.py` | 分层技能索引，**整体替换**了 pi 的 `core/skills.ts`（审计 A-10）；`wiring/skills.py` 是三个工具与守卫 |
| `web/` | `extensions/web/` | 搜索后端、注册表、分发、抓取、抽取；`tools/_web/` 是抓取工具用的安全层 |
| `lcm/` | `extensions/hermes_lcm/` | LCM 上下文引擎；`vendor/` 是 hermes-lcm 上游原样（`UPSTREAM_COMMIT` 钉版本，`PORT_NOTES.md` 是再同步契约），`host/` 是适配 |
| `documents/` | `misaka/documents/` + `extensions/documents.py` | 语料索引与工作区；`pageindex/` 是 vendored 的 PageIndex（自带 MIT 许可） |
| `panel/` | `extensions/agent_state.py` `extensions/fork_split.py` | 会话这一侧的面板协议；面板本身在 `misaka/ui/panel/` |
| `ask_user/` | `extensions/ask_user/` | `AskUserQuestion` 工具 |

### 壳（`wiring/`）的约定

原 `misaka/extensions/` 里的每个模块只是一个 `activate(spec)` 入口，真代码在别的包里。迁入时壳跟着体走，
放在体包的 `wiring/` 子目录、**basename 不变**——`core/wiring.py` 用 basename 当描述符名，
这样 `build_extensions` 产出的名单与迁移前逐字相同。体包里已有同名模块（`network/messages.py` 与壳 `messages.py`）
是子目录存在的原因。自包含的（web、subagent、lcm、ally、ask_user）activate 留在包的 `__init__.py`。

**core 不在启动屏的「Extensions」里出现**（`core/wiring.py`：`misaka.core.*` 的条目一律 `hidden`），和 pi 的内置工具一样；
那一区留给用户自己装的东西。`misaka/extensions/` 下的捆绑扩展各自用 `HIDDEN` 决定（pi 藏了它的 llama）。
这只是显示：运行时对它们和对 pi 捆绑的 llama 一视同仁——settings 禁不掉、`--no-extensions` 丢不掉、不需要项目信任、
钩子都由 ExtensionRunner 派发（pi 内核唯一的钩子通道，内置工具没有钩子所以用不到它）。

## `misaka/extensions/` 现在是什么

pi 意义上的**捆绑扩展**：`llama/`（pi 自己唯一捆绑的那个）与 `moa/`。`extensions/__init__.py` 就是 pi 的
`src/extensions/index.ts`——`builtInExtensions = ({name, factory, hidden}, …)`，一字不差；把它接进会话的是
`cli/engine.py`（misaka 的 `main.ts`）：`[*builtInExtensions, *调用方给的]`。**`core/` 对这个包零引用**，和 pi 一样。
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
misaka/extensions/__init__.py:discover → misaka/core/wiring.py:build_extensions
misaka/extensions/web/                 → misaka/core/web/
misaka/extensions/hermes_lcm/          → misaka/core/lcm/            （描述符名 hermes_lcm → lcm）
misaka/extensions/sisters/subagent/    → misaka/core/subagent/
misaka/extensions/last_order/ally/     → misaka/core/network/ally/
misaka/extensions/ask_user/            → misaka/core/ask_user/
misaka/extensions/mcp.py               → misaka/core/mcp.py
misaka/extensions/coverage.py          → misaka/core/coverage.py
misaka/extensions/agent_state.py       → misaka/core/panel/agent_state.py
misaka/extensions/fork_split.py        → misaka/core/panel/fork_split.py
misaka/extensions/skills.py            → misaka/core/skills/wiring/skills.py
misaka/extensions/documents.py         → misaka/core/documents/wiring/documents.py
misaka/extensions/{messages,todo,roster,observe}.py
                                       → misaka/core/network/wiring/{同名}.py
misaka/extensions/last_order/{network,peek,roster_admin}.py
                                       → misaka/core/network/wiring/{同名}.py
misaka/extensions/last_order/research.py
                                       → misaka/core/research/wiring/research.py
```

## 待办（迁移时看到、没顺手做的）

- `core/agent_session.py:235` 懒 import `core.skills.wiring.skills.parse_skill_invocation_message`：内核 import 一个 wiring 模块。
  解析器与写侧 `build_skill_message` 共用五个脚手架常量，干净拆分要把写侧一起挪成 `core/skills/invocation.py`。
- `core/web/` 与 `core/tools/_web/` 是两个 web 目录；后者是 misaka 加进 pi 工具树的，可以并入前者。

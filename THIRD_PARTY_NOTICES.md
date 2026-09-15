# 第三方声明

MISAKA 自身以 Apache License 2.0 发布（全文见仓库根目录的 `LICENSE`）。
下面登记的第三方代码各自保留上游许可证：MIT/ISC 的部分可以并入 Apache-2.0 的发行物，
herdr 的部分本就是 Apache-2.0。

MISAKA 的部分文件移植自其它项目。移植代码所附带的许可证义务随代码一起转移，
因此这些声明必须随本软件的所有副本分发。逐文件的声明保留在各自的文件头部，
本文件是它们的汇总索引。

## Pi（coding agent）

- 上游：https://github.com/earendil-works/pi ，npm 包 `@earendil-works/pi-coding-agent`
- 许可证：MIT。上游 npm 包在任何层级都不含 LICENSE 文件，许可证由其 `package.json` 的
  `"license": "MIT"` 字段声明；本条即为 MIT 所要求的版权与许可声明的保留。
- 作者：Mario Zechner
- 对照件：`@earendil-works/pi-coding-agent` 0.83.0 的 `dist/`
- 本仓库位置：`misaka/ai/**`、`misaka/agent/**`、`misaka/ui/tui/**`，以及 `misaka/core/`
  的内核部分。逐目录、逐文件的归属清单是 `misaka/core/PI_ORIGIN.md`。
- 说明：MISAKA 的内核是 Pi 的 Python 移植，是本仓库最大的单一上游来源。分叉处在代码里
  以 `# MISAKA fork:` 注释标出并说明理由。Pi 树内自带许可证头的两个文件
  （`utils/ansi.ts`、`tui/src/stdin-buffer.ts`）另见下面的 ansi-regex 与 OpenTUI 两条。

## ansi-regex / strip-ansi

- 上游：https://github.com/chalk/ansi-regex 、https://github.com/chalk/strip-ansi
- 许可证：MIT
- 版权：Copyright (c) Sindre Sorhus <sindresorhus@gmail.com> (https://sindresorhus.com)
- 本仓库位置：`misaka/utils/ansi.py`（完整 MIT 文本在该文件头部）
- 说明：经由 Pi 的 `packages/coding-agent/src/utils/ansi.ts` 移植；正则、快路径与
  TypeError 文案与上游一致。

## OpenTUI

- 上游：https://github.com/anomalyco/opentui
- 许可证：MIT
- 版权：Copyright (c) 2025 opentui
- 本仓库位置：`misaka/ui/tui/stdin_buffer.py`（完整 MIT 文本在该文件头部）
- 说明：经由 Pi 的 `packages/tui/src/stdin-buffer.ts` 移植；转义序列的缓冲与切分
  逻辑来自上游，字节层增量解码与定时器回环是本仓库为 Python 运行时所加。

## hermes-lcm

- 上游：https://github.com/stephenschoettler/hermes-lcm
- 许可证：MIT
- 版权：Copyright (c) 2026 Stephen Schoettler
- 本仓库位置：`misaka/extensions/misaka_lcm/vendor/`（完整 MIT 文本在该目录的 `LICENSE.hermes-lcm`）
- pin：`8d1b1e6d3d63f5fc7b209e8d7ec1dc9b814f2e54`（1.0.0-rc.1），见
  `misaka/extensions/misaka_lcm/UPSTREAM_COMMIT`
- 本地 fork 名为 **MISAKA LCM**；上游测试在 `vendor/tests/`，MISAKA 适配层在 `host/`。
  固定源码与已登记的宿主接缝见 `PORT_NOTES.md`；本轮核心字节校验见 `CORE_INTEGRITY.json`。

## PageIndex

- 上游：https://github.com/VectifyAI/PageIndex
- 许可证：MIT
- 版权：Copyright (c) 2025 Vectify AI
- 本仓库位置：`misaka/core/documents/pageindex/`（完整 MIT 文本在该目录的 `LICENSE.PageIndex`）

## herdr

- 上游：https://github.com/herdrdev/herdr
- 许可证：Apache License 2.0（与 MISAKA 整体相同，全文见仓库根目录的 `LICENSE`）
- 版权：Copyright herdr contributors
- pin：`v0.8.2`；`geometry.py` 的逐函数对照基准是 `v0.8.0`
- 本仓库位置：`misaka/ui/panel/`
- 说明：面板是 herdr 客户端的 Rust → Python 移植。`geometry.py` 是布局数学的逐函数移植；
  `panel.py` 移植了 `tabs.rs` 的 `render_tab_bar`、`layout.rs` 的 BSP 分割树与
  `app/input/mouse.rs` 的鼠标处理；`daemon.py` 沿用 herdr 的 agent 状态判定、滚动度量
  与 resize 驱动语义。

### 按 Apache-2.0 第 4(b) 条声明的改动

- 配色不采用 herdr 的 Catppuccin，改为跟随终端背景的内置深/浅主题。
- 窗格里跑的是 MISAKA 的 CPython pi-tui 而非 herdr 的 bun pi；两者渲染速度相差一到两个
  数量级，拖拽与 resize 路径因此按本仓库的渲染节奏重写（`resize_hold`、`_drive_resize`、
  视口渲染 `_render_tail`）。
- 新增 herdr 没有的 grid 布局（`geometry.grid_tree`）与 `{grid}` 放置，用于把 sister/ally
  分格到同一标签页。
- 背景轮询、拖拽期间的轮询抑制，以及 Python 侧的增量解码，都是本仓库为 CPython 运行时所加。

## ghostty

- 上游：https://github.com/ghostty-org/ghostty
- 许可证：MIT
- 版权：Copyright (c) 2024 Mitchell Hashimoto, Ghostty contributors
- 本仓库位置：`misaka/ui/panel/lib/`（完整 MIT 文本在该目录的 `LICENSE.ghostty`）
- 说明：面板的终端仿真器是 ghostty 的 VT 库。随仓库分发的是预编译产物
  `libghostty-vt-<os>-<arch>.<ext>`（macOS 与 Linux 各两个架构）。源码取自 herdr 所
  vendored 的那一份：herdr `v0.8.2` 的 `vendor/libghostty-vt`，ghostty 提交 `c5a21edfc`，
  含 herdr 的 grapheme clustering 补丁——因此这些二进制同时受本条与上面的 herdr 条目约束。
  构建方法与 `MISAKA_GHOSTTY_VT` 覆盖路径见该目录的 `README.md`。
  `misaka/ui/panel/ghostty.py` 是本仓库自有的 ctypes 绑定，不是上游代码。

## 上游整体移植

`misaka/ai/**`、`misaka/core/**`、`misaka/agent/**`、`misaka/ui/tui/**` 的大部分是
Pi（coding agent）的 Python 移植；`misaka/ui/panel/**` 是 herdr 的移植。
`misaka/extensions/misaka_lcm/vendor/**` 原样收录自 hermes-lcm，
`misaka/core/documents/pageindex/**` 原样收录自 PageIndex，两者各自保留上游的许可证文件。

> 维护提示：新增一处从外部项目移植的代码时，把上游的许可证头随代码一起搬进来，
> 并在本文件登记一条。判断依据是上游文件自己是否带许可证头——例如 Pi 树内恰好
> 只有 `utils/ansi.ts` 与 `tui/src/stdin-buffer.ts` 两个文件带头，它们对应的移植
> 就都需要登记。

## Hermes Web 和供应商 SDK 协议适配

- Hermes 源码：https://github.com/nousresearch/hermes-agent ，MIT，Copyright (c) 2025 Nous Research。
- 位置：`misaka/core/web/`、`misaka/core/tools/_web/`；许可证文本：`misaka/core/web/LICENSE.hermes-agent`。
- 对照基准：`990473a79c6b0396b0a648fdd85ee8f7a5c267d3`（本批所用源码与审计 f03 快照一致）。
- Web 增量对照：`62e5f466565ee56351e4483ead8e62f9e782f8b3`。本批移植 provider 提取时限语义及五个 browser vault 工具；不表示整个 Hermes 产品已完整移植。
- Vault：`misaka/core/web/browser/vault/`，来自该增量提交的 `agent/vault_store.py`、`agent/vault_login_classifier.py`、`agent/vault_backends/` 与 `tools/browser_vault_tool.py`。许可证为同目录 `LICENSE`；逐文件来源及 SHA256 见 `PROVENANCE.json`。宿主桥接、masked TUI、权限、资源清理及额外边界修复是 MISAKA 原生适配。
- Web 回归：`tests/web/` 保留本地历史回归及选取的 Hermes vault 测试；`tests/web/fixtures/hermes_990473a/` 为固定基准源码夹具，保留原文及同目录 MIT 许可证，见 `tests/web/README.md`。
- Parallel `parallel-web==0.4.2`：重试协议适配，MIT，Copyright 2026 Parallel；文本：`misaka/core/web/LICENSE.parallel-web`。
- Firecrawl `firecrawl-py==4.17.0`：重试、错误信封及 scrape 默认参数适配，MIT，Copyright (c) 2024 Sideguide Technologies Inc.；文本：`misaka/core/web/LICENSE.firecrawl-py`。
- SDK wheel 仅用于离线源码/差分核查，未作为新增运行依赖或整包 vendoring；版本与 SHA 见 `docs/audits/web-tools-vs-hermes-2026-09-09/provider-parity-evidence/sdk-manifest.json`。


## Hermes Skill algorithms and native tests

`misaka/core/skills/vendor/` and `tests/skills_hermes_native/` contain selected
Hermes Agent code from NousResearch, pinned at
`f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140` (MIT). The original license is
`misaka/core/skills/vendor/LICENSE`. Exact source hashes, extracted symbols,
and host patches are in `vendor/PROVENANCE.json`; regenerate/check with
`scripts/skills_vendor_sync.py <upstream-checkout> [--write]`.
The MISAKA scope, scanner enforcement, transactions and lifecycle adapters
are not represented as verbatim upstream code.

## CCB subagent implementation reference

- Reference: https://github.com/claude-code-best/claude-code
- Pin: `77a7934e15d69da13879112ed7db695c9ee7a52a`.
- Native ports/adapters: `misaka/core/subagent/`; background Bash host integration in
  `misaka/core/tools/bash.py`, `misaka/core/moments.py`, and `misaka/core/agent_session.py`;
  the source semantic-boolean conversion in `misaka/utils/values.py`.
- MCP OAuth lifecycle: `misaka/core/subagent/mcp_auth.py` references the pinned
  `src/services/mcp/auth.ts` and uses the installed official Python MCP SDK for
  the protocol. Secure storage and cancellation ownership reuse MISAKA helpers;
  loopback port binding, file-backed metadata and role isolation are native adaptations.
- Live permission mode: native session/part state plus existing child JSONL IPC,
  using the pinned `packages/builtin-tools/src/tools/AgentTool/runAgent.ts`
  `agentGetAppState` mode-precedence rule.
- This is the reconstructed CCB reference, not an official Anthropic source release.
  Its README license section says the project is for study/research and that Claude
  Code rights belong to Anthropic. The pinned archive contains no root LICENSE file;
  this notice does not assign it an MIT or other open-source license.
- Behavioral source pointers are retained in the ported modules. Native adaptations
  preserve MISAKA role/project ownership, Pi sessions, and the Hermes Skill/LCM hosts.

## shell-quote (selected lexer port)

- Source: https://github.com/ljharb/shell-quote, npm `shell-quote@1.8.3`
  (CCB's pinned `bun.lock`).
- `misaka/core/subagent/_shell_parser.py` ports the default-escape,
  preserving-variable callback path of `parse.js`; not a new JS runtime dependency.
- MIT text: `misaka/core/subagent/LICENSE.shell-quote`.

## yaml (selected core scalar schema)

- Source: https://github.com/eemeli/yaml, npm `yaml@2.8.3` (CCB's pinned `bun.lock`).
- `misaka/core/subagent/_frontmatter.py` ports its core scalar recognizers through
  MISAKA's existing ruamel YAML host, with private lexical adapters. This is not
  a vendored JS parser or an added runtime dependency. Pi/Hermes YAML is untouched.
- ISC text: `misaka/core/subagent/LICENSE.yaml`.

## FrontierAgent（Office）

- 上游：https://github.com/ApodexAI/FrontierAgent ，Apache License 2.0。
- 本轮源码对照固定于 `9e533db6f6c34d16037ee5ec964c479d0eb51cde`；这不是原样 vendoring 或全产品一比一移植声明。
- 位置：`misaka/core/tools/_office/`、`misaka/core/documents/office/`，以及 `tools/office.py`、`tools/read.py` 的 Office 接入。
- 许可证全文、源文件 SHA256 和改动边界：`misaka/core/tools/_office/LICENSE.frontier-agent`、`PROVENANCE.json`、`ORIGIN.md`。
- 来源：上游 `_writer_*`、`_reader_*`、`create_file.py`、`read_file.py`；其自身注明功能设计参考 Mercor-Intelligence/archipelago（Apache-2.0），寻址和参数设计由 FrontierAgent 实现。
- 本地改动包含引用友好的文本渲染、工作区归属、权限和错误信封、线程／写入队列、暂存回滚、独立转换配置、缺陷修正；不包含上游 PDF OCR、视觉模型网关和沙箱基础设施。

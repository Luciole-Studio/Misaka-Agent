# 第三方声明

MISAKA 自身以 Apache License 2.0 发布（全文见仓库根目录的 `LICENSE`）。
下面登记的第三方代码各自保留上游许可证：MIT/ISC 的部分可以并入 Apache-2.0 的发行物，
herdr 的部分本就是 Apache-2.0。

MISAKA 的部分文件移植自其它项目。移植代码所附带的许可证义务随代码一起转移，
因此这些声明必须随本软件的所有副本分发。逐文件的声明保留在各自的文件头部，
本文件是它们的汇总索引。

## Pi（coding agent）

- 上游：https://github.com/earendil-works/pi ，npm 包 `@earendil-works/pi-coding-agent`
- 许可证：MIT
- 版权：Copyright (c) 2025 Mario Zechner
- 许可证全文：`misaka/LICENSE.pi`，取自上游仓库 `v1.1.0` 的 `LICENSE`（与 `v0.87.1`、`v1.3.0` 一字不差；npm 包本身不带 LICENSE 文件）
- 对照件：上游仓库 `v1.3.0` 标签的源码（`packages/*/src`）与 `@earendil-works/pi-ai` 1.3.0 发布包
- 模型目录数据：`misaka/ai/providers/data/` 是 `@earendil-works/pi-ai` 1.3.0 发布包中 `dist/providers/data` 的原样拷贝，
  用 `scripts/refresh_model_catalog.py` 按清单校验后更新
- 本仓库位置：`misaka/ai/**`、`misaka/agent/**`、`misaka/ui/tui/**`、`misaka/mcp/**`（Pi 的
  `packages/mcp`）、`misaka/codemode/**`（Pi 的 `packages/codemode`；其中 `quickjs_wasi/` 见下面的 quickjs-wasi 条）、
  `misaka/extensions/{mcp,codemode,tool_search}/**`，`misaka/{chord,durable,env}/**`
  （Pi 的同名 packages；`chord/loader.py` 与 `chord/manager.py` 是 Python 分叉），以及 `misaka/core/` 的内核
  部分；这些目录自 2026-10-21 起对照 Pi 1.3.0。`misaka/core/` 逐目录、逐文件的归属清单是 `misaka/core/PI_ORIGIN.md`。
  仓库里另有一处 Pi 来源：`tests/` 中移植的上游测试（含 faux provider 与 suite 测试夹具）。
- 说明：MISAKA 的内核是 Pi 的 Python 移植，是本仓库最大的单一上游来源。分叉处在代码里
  以 `# MISAKA fork:` 注释标出并说明理由。Pi 树内自带第三方许可证头的文件另见下面各条：
  `utils/ansi.ts`（ansi-regex）、`tui/src/stdin-buffer.ts`（OpenTUI）、`mcp/src/oauth/{types,discovery,flow}.ts`
  （MCP TypeScript SDK）、`tui/src/oklab.ts`（Oklab）、`core/export-html/vendor/*.min.js`（marked、highlight.js）。

## quickjs-wasi 与其中的 QuickJS-NG、wasi-libc（经 Pi 的 packages/codemode）

- 上游：https://github.com/vercel-labs/quickjs-wasi ，npm 包 `quickjs-wasi` 3.6.2（Pi 1.3.0 的 codemode 依赖它，版本未变）
- 许可证：quickjs-wasi 为 MIT（Copyright (c) 2026 Vercel, Inc.）；其中的 QuickJS-NG 0.17.0 为 MIT
  （Copyright (c) 2017-2026 Fabrice Bellard 等）；wasi-libc 按其多许可证中的 MIT 使用，所含 musl 为 MIT、
  cloudlibc 为 BSD-2-Clause、dlmalloc 为 CC0
- 许可证全文：`LICENSES/quickjs-wasi.txt`（四份上游声明的原文汇编）
- 本仓库位置：`misaka/codemode/quickjs_wasi/quickjs.wasm`（npm 包里的 `quickjs.wasm` 原样拷贝，
  sha256 `d4c9375f2b1ca4dc95f72c8aa2982a7a9951ac8011490d79c6582df732b4bbd9`）与
  `misaka/codemode/quickjs_wasi/__init__.py`（完整 MIT 文本在该文件头部）
- 说明：`__init__.py` 把 quickjs-wasi 的 `src/index.ts` 与 `src/wasi-shim.ts` 中 Pi 的 codemode worker
  用到的部分移植成 Python，宿主的 WebAssembly 换成 wasmtime；分叉处在文件头部列出。

## TypeBox（经 Pi 的 packages/chord）

- 上游：https://github.com/sinclairzx81/typebox ，npm 包 `typebox` 1.3.27（Pi 1.3.0 的 chord 依赖它）
- 许可证：MIT
- 版权：Copyright (c) 2017-2026 Haydn Paterson
- 本仓库位置：`misaka/chord/_typebox.py`（完整 MIT 文本在该文件头部）
- 说明：chord 用到的 `Type` 构造器与 `Value.Check/Errors/Clean/Default/Clone` 的 Python 移植（联合分支的优先级排序、
  字素计数、交叉类型求值、错误文案与路径同上游）；分叉处在文件头部列出。

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

## MCP TypeScript SDK（经 Pi 的 packages/mcp）

- 上游：https://github.com/modelcontextprotocol/typescript-sdk ，v1.29.0 的 `src/client/auth.ts`
- 许可证：MIT
- 版权：Copyright (c) 2024 Anthropic, PBC
- 许可证全文：`LICENSES/modelcontextprotocol-typescript-sdk.txt`（Pi `packages/mcp/LICENSES/` 的原样拷贝）
- 本仓库位置：`misaka/mcp/oauth/types.py`、`misaka/mcp/oauth/discovery.py`、`misaka/mcp/oauth/flow.py`
- 说明：Pi 把 SDK 的 OAuth 发现、动态注册与授权码流程改写成无依赖的结构校验版本并保留了
  上游头部；MISAKA 再从 Pi 的 TypeScript 移植到 Python，三个文件的文档字符串保留 Pi 的头部原文。

## cross-spawn 及其依赖（经 Pi 的 packages/mcp）

- 上游与许可证：cross-spawn 7.0.6（MIT，Copyright (c) 2018 Made With MOXY Lda）、
  which 2.0.2 与 isexe 2.0.0（ISC，Copyright (c) Isaac Z. Schlueter and Contributors）、
  path-key 3.1.1 与 shebang-regex 3.0.0（MIT，Copyright (c) Sindre Sorhus）、
  shebang-command 2.0.0（MIT，Copyright (c) Kevin Mårtensson）
- 许可证全文：`LICENSES/cross-spawn-and-dependencies.txt`
- 本仓库位置：`misaka/mcp/transports/_cross_spawn.py`
- 说明：Pi 的 stdio 传输用 cross-spawn 拉起服务器进程；MISAKA 把它在 Windows 上解析命令、
  处理 shebang 与 cmd.exe 转义的那部分移植为 Python，转义规则以 node 跑 cross-spawn 7.0.6
  产出的向量校验（`tests/fixtures/mcp/cross_spawn_vectors.json`）。

## proper-lockfile 与 retry（经 Pi 的 coding-agent）

- 上游与许可证：proper-lockfile 4.1.2（MIT，Copyright (c) 2018 Made With MOXY Lda）、
  retry 0.12.0（MIT，Copyright (c) 2011 Tim Koschützki、Felix Geisendörfer）
- 许可证全文：`LICENSES/proper-lockfile-and-retry.txt`
- 本仓库位置：`misaka/utils/lockfile.py`
- 说明：Pi 的 coding-agent 用 proper-lockfile 锁文件（MISAKA 里是 MCP 的 OAuth 扩展）；MISAKA 把它的
  `lib/lockfile.js`、`lib/adapter.js`、`lib/mtime-precision.js` 与 retry 的退避参数那一部分
  逐行改写为 Python（以 `mkdir` 为原子加锁、陈旧锁接管、定时刷新、ELOCKED/ECOMPROMISED/ENOTACQUIRED）。

## jsdiff（经 Pi 的 packages/durable）

- 上游：https://github.com/kpdecker/jsdiff ，npm 包 `diff` 8.0.4
- 许可证：BSD-3-Clause
- 版权：Copyright (c) 2009-2015, Kevin Decker
- 许可证全文：`LICENSES/jsdiff.txt`
- 本仓库位置：`misaka/utils/jsdiff.py`
- 说明：Pi 的 durable 编辑工具用 `diffLines` 与补丁生成；MISAKA 移植其中的 Myers 行差异
  （`diff/base.js`、`diff/line.js`）与 `patch/create.js`，只含工具用到的部分；输出以 node 跑真实的
  `diff` 8.0.4 录下的用例校验（`tests/fixtures/jsdiff_cases.json`）。

## grok-mermaid（经 Pi 的 coding-agent）

- 上游：https://github.com/xl0/grok-mermaid ，npm 包 `grok-mermaid` 0.2.3
- 许可证：Apache-2.0
- 版权：Copyright 2023-2026 SpaceXAI；Copyright 2026 Alexey Zaytsev
- 许可证全文：`LICENSES/grok-mermaid.txt`
- 本仓库位置：`misaka/ui/tui/vendor/grok_mermaid/`
- 说明：Pi 的 TUI 用它把 Markdown 里的 Mermaid 代码块画成终端里的 Unicode 框线图；MISAKA 逐文件移植全部源码
  （解析、布局、画布、宽度表、源码框）。改动：边的 `from` 字段改名 `from_`（Python 关键字），模型类型改为
  dataclass。输出以 node 跑真实的 grok-mermaid 0.2.3 录下的结果逐字节校验（`tests/fixtures/grok_mermaid_oracle.json`）。

## marked（经 Pi 的 packages/tui）

- 上游：https://github.com/markedjs/marked ，npm 包 `marked` 18.0.11
- 许可证：MIT
- 版权：Copyright (c) 2018+, MarkedJS；Copyright (c) 2011-2018, Christopher Jeffrey
- 许可证全文：`LICENSES/marked.txt`
- 本仓库位置：`misaka/ui/tui/vendor/marked/`
- 说明：Pi 的 TUI 用 marked 把 Markdown 切成词元再自己渲染；MISAKA 逐行移植其中的词法部分
  （`rules`、`helpers`、`Tokenizer`、`Lexer`、`Hooks` 与 `Marked` 的词法接口），不含 HTML 的
  `Parser`/`Renderer`。正则保留 JavaScript 源码，经 `misaka/utils/js_regexp.py` 按 JavaScript 语义编译；
  输出以 node 跑真实的 marked 18.0.11 录下的词元校验（`tests/fixtures/marked_18_0_11_tokens.json.gz`）。
- 另一处：`misaka/core/export_html/vendor/marked.min.js` 是 marked v18.0.5 的压缩构建，取自 Pi 1.1.0（1.3.0 未改）的
  `core/export-html/vendor/` 原样拷贝，文件头保留上游版权行；HTML 导出页在浏览器里用它渲染 Markdown。

## highlight.js（经 Pi 的 export-html）

- 上游：https://github.com/highlightjs/highlight.js ，v11.9.0
- 许可证：BSD-3-Clause
- 版权：Copyright (c) 2006, Ivan Sagalaev 及其他贡献者
- 许可证全文：`LICENSES/highlight.js.txt`
- 本仓库位置：`misaka/core/export_html/vendor/highlight.min.js`
- 说明：Pi 1.1.0（1.3.0 未改）`core/export-html/vendor/` 中压缩构建的原样拷贝，HTML 导出页用它给代码块着色；文件头保留
  上游的版本与许可证行。

## Oklab（经 Pi 的 packages/tui）

- 上游：https://bottosson.github.io/posts/oklab/ 与 https://bottosson.github.io/posts/colorpicker/ 的参考实现
- 许可证：MIT
- 版权：Copyright (c) 2021 Björn Ottosson
- 本仓库位置：`misaka/ui/tui/colors.py` 的 `rgbToOklab` 一段（许可文本在该段注释里）
- 说明：经由 Pi 的 `packages/tui/src/oklab.ts` 移植，只取 sRGB 到 Oklab 的换算。

## get-east-asian-width（经 Pi 的 packages/tui）

- 上游：https://github.com/sindresorhus/get-east-asian-width ，npm 包 `get-east-asian-width` 1.6.0
- 许可证：MIT
- 版权：Copyright (c) Sindre Sorhus <sindresorhus@gmail.com> (https://sindresorhus.com)
- 许可证全文：`LICENSES/get-east-asian-width.txt`
- 本仓库位置：`misaka/ui/tui/vendor/get_east_asian_width/`
- 说明：Pi 的 TUI 用它量字符的显示宽度；MISAKA 逐文件移植 `index.js`、`lookup.js`、`lookup-data.js`
  与 `utilities.js`，取代原先的 wcwidth 依赖。

## Unicode 17 RGI 表情序列（经 Pi 的 packages/tui）

- 上游：https://github.com/node-unicode/unicode-17.0.0 ，npm 包 `@unicode/unicode-17.0.0` 2.0.7
- 许可证：MIT
- 版权：Copyright Mathias Bynens <https://mathiasbynens.be/>
- 许可证全文：`LICENSES/unicode-17.0.0.txt`
- 本仓库位置：`misaka/ui/tui/vendor/unicode_rgi_emoji.py`
- 说明：Pi 用 `/^\p{RGI_Emoji}$/v` 判断一个字素是不是表情；Python 的正则没有字符串属性，
  MISAKA 把该包 `Sequence_Property/RGI_Emoji` 的 3,953 个序列收成一张查找表。

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
  `libghostty-vt-<os>-<arch>.<ext>`（macOS、Linux 与 Windows 各两个架构）。源码取自 herdr 所
  vendored 的那一份：herdr `v0.8.2` 的 `vendor/libghostty-vt`，ghostty 提交 `c5a21edfc`，
  含 herdr 的 grapheme clustering 补丁——因此这些二进制同时受本条与上面的 herdr 条目约束。
  macOS 与 Linux 的构建以 `-Dsimd=true` 静态链入了下面两个库，它们的声明见下两条。
  构建方法与 `MISAKA_GHOSTTY_VT` 覆盖路径见该目录的 `README.md`。
  `misaka/ui/panel/ghostty.py` 是本仓库自有的 ctypes 绑定，不是上游代码。

## Highway（随 ghostty 二进制分发）

- 上游：https://github.com/google/highway ，ghostty 固定的提交 `66486a10623fa0d72fe91260f96c892e41aceb06`（1.2.0）
- 许可证：Apache-2.0 或 BSD-3-Clause 二选一，本仓库按 BSD-3-Clause 分发
- 版权：Copyright (c) The Highway Project Authors
- 许可证全文：`misaka/ui/panel/lib/LICENSE.highway`
- 位置：静态链入 macOS 与 Linux 的 `libghostty-vt` 二进制，仓库里没有它的源码

## simdutf（随 ghostty 二进制分发）

- 上游：https://github.com/simdutf/simdutf ，ghostty 收录的 5.2.8
- 许可证：Apache-2.0 或 MIT 二选一，本仓库按 MIT 分发
- 版权：Copyright 2021 The simdutf authors
- 许可证全文：`misaka/ui/panel/lib/LICENSE.simdutf`
- 位置：静态链入 macOS 与 Linux 的 `libghostty-vt` 二进制，仓库里没有它的源码

## portable-pty（经 herdr）

- 上游：https://github.com/wezterm/wezterm 的 `pty` crate，herdr `v0.8.2` 收录在 `vendor/portable-pty`
- 许可证：MIT
- 版权：Copyright (c) 2018 Wez Furlong
- 许可证全文：`misaka/ui/panel/LICENSE.portable-pty`
- 本仓库位置：`misaka/ui/panel/conpty.py`，移植了 `src/win` 下的 `psuedocon.rs`、`conpty.rs`、
  `procthreadattr.rs` 与 `WinChild`，即 Windows 上窗格的 ConPTY 后端

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
- Web 能力选择修复：移植 `010a45097e49fff00c32db954f56a2f9d6d08f17`（#113017）的共享选择判断及回归场景；仅配置 search/extract 之一时，另一能力继续原有自动检测。MISAKA 无旧 `use_gateway` 配置，保留原生 `backend: nous`；不改上游的凭证优先级或追加能力过滤。
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
On 2026-10-24 how skills run was aligned with Hermes `bda3a363e9`: a skill is
served and run from its own directory (no execution copies, no card snapshots,
no guard on skill trees or on shell expansions); the file tools refuse the
skills hub cache as Hermes `agent/file_safety.py` does
(`misaka/core/web/evidence.py`); `_is_community_hub_skill` is copied from
Hermes `agent/skill_preprocessing.py` into `misaka/core/skills/preprocessing.py`.
One difference stays: the home's own write rule (`misaka/core/platform/home_guard.py`),
which Hermes does not have, still keeps a card's or a sub-agent's `write`/`edit`/`office`
out of the home outside `shared/`, so skills in the home and in a role change there through
`skill_manage`; a skill whose real path lies in `shared/` (a link into it) can be written
by those tools too.

## Hermes Bot Mode 联系会话

- Hermes 源码：https://github.com/nousresearch/hermes-agent ，MIT，Copyright (c) 2025 Nous Research；许可证文本：`misaka/core/web/LICENSE.hermes-agent`。
- `misaka/cli/dm.py`：Bot Mode 私信模型的移植（永久 Bot Chat 会话、署名前缀、协议段；2026-08-19 按当时的 `tools/bot_mode_probe.py`）。2026-10-24 按 `bda3a363e9695e3f63fce4a7ca85ba17b5fecb61` 对齐联系轮的语义：网关的无活动超时（`gateway/run_turn.py`、`gateway/run.py` 的 `_watch_gateway_turn_inactivity`）代替 600 秒墙钟，一次按策略的重试，失败回告发信方（`tools/bot_mode_dm.py` 的 `_wait_reply_main` 措辞）；发信的窗口都已关闭时停下这一轮（上游把投递作为发信会话的后台进程起，`_spawn_delivery`，并随该会话收掉，`agent/client_lifecycle.py` 的 `_close_task_resources`）。
- `misaka/core/network/bot_failure_reasons.py`：同一提交的 `tools/bot_failure_reasons.py` 原文节选（原因码、分类器、重试策略）。

## CCB subagent implementation reference

- Reference: https://github.com/claude-code-best/claude-code
- Pin: `77a7934e15d69da13879112ed7db695c9ee7a52a`.
- Native ports/adapters: `misaka/core/subagent/`; background Bash host integration in
  `misaka/core/tools/bash.py`, `misaka/core/moments.py`, and `misaka/core/agent_session.py`;
  the source semantic-boolean conversion in `misaka/utils/values.py`.
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
- 本地改动包含引用友好的文本渲染、工作区归属、权限和错误信封、线程／写入队列、暂存回滚、独立转换配置、缺陷修正；不包含上游 PDF OCR 服务调用、视觉模型网关和沙箱基础设施。
- `misaka/agent/guards.py` 的 `NoProgressGuard` 移植自 `workflows/agent_team/observers/no_progress_guard.py`（对照 `e4cb2397109c957a2f99c3a5d850b9fc479dbef6`）：阈值、"连续轮次里至少三次新建子代理"的前提、新鲜实质报告清零、退场式派活加速停止与提示冷却照上游；上游四个工具按 `misaka/core/platform/vocabulary.py` 的 `NO_PROGRESS_VOCABULARY` 对应到 `Agent`/`SendMessage`/`TaskOutput`/`TaskStop`，前台 `Agent` 自带报告；提示与停止语为中文转述。
- `misaka/core/documents/index.py` 的逐页 OCR 判定（`_route` 及其阈值：无文字、乱码比例、图片覆盖面积）参照上游 `plugins/tools/_reader_pdf.py` 的逐页路由规则重新实现；图片规则收窄为"图片为主且文字稀少"，数学字体与矢量图规则未采用，理由见该段注释。
- 插图标注（`index.py` 的 `figures`：图片占比、logo 宽度与笔画数阈值）参照上游 `_reader_pdf.py` 的 auto 模式标注；为无视觉模型代读图片时所用的转写提示词（`core/web/vision.py` 的 `TRANSCRIBE`）取自上游 `_reader_core.py` 的 `_VISION_PROMPT`；代读由 MISAKA 的 `vision` 扩展在请求前统一完成。幻灯片的查看沿用上游的路径：`needs_vlm` 标记（上游 "needs VLM"）指出要看的幻灯片，经 LibreOffice 导出 PDF（`soffice.export_pdf`，即上游 `_export_pdf`）再渲染该页。

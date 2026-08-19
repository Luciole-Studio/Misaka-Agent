# third_party 钉版记录

| 仓 | 上游 | commit | 用途 |
|---|---|---|---|
| PageIndex | github.com/VectifyAI/PageIndex | d5c4e62c20172ce400aef84545dfba3a0580b9ae | 文档结构层（树导航），只调用不改 |

## 代码移植（含拷贝，非 third_party 目录承载）

- **hermes-lcm**（github.com/stephenschoettler/hermes-lcm，MIT）——`misaka/orchestration/lcm/`
  是其数据层与压缩链的移植（2026-08-18 起，设计 docs/design/lcm.md）：
  `search_query.py` 逐字整搬；store/dag/tokens 为骨架移植（schema 与算法忠实、
  接口面按 misaka 削减）。上游 v0.21.0-rc2 时期。
- **hermes-agent**（github.com/NousResearch/hermes-agent，MIT）——
  `misaka/orchestration/skills_guard.py` 是其 tools/skills_guard.py 的逐字整搬
  （仅改头注释＋加自检段）；`skill_layers.py` 是 agent/skill_utils.py
  项目技能层/信任闸/隔离段的严格移植；`extensions/moa.py`＋`orchestration/moa.py`
  是其 MoA（agent/moa_loop.py 等）的忠实移植（2026-08-18，设计 docs/design/moa.md）；
  `misaka/cli/dm.py`＋messages.py 的直投分支是其 Bot Mode DM 模型
  （apps/desktop hermes-bots 插件＋tools/bot_mode_probe.py）的落地移植
  （2026-08-19，宪法修正案 A1）。
- **dsh-trace-compare**（github.com/lamost423/dsh-trace-compare @7b8a28c，MIT）——
  `misaka/orchestration/trace_verdict.py` 是其 src/client/verdict.js 的逐行 Python
  翻译（阈值/文案逐字，tests/contract/test_trace_parity.py 用
  tests/fixtures/dsh_verdict.mjs＝上游原文做 node 对拍钉一致）；`trace_lanes.py`
  是其 buildLane/buildData/compressTimeline（maze-upload.html）＋live-data.ts
  语义的合体移植（输入端换 misaka 引擎 v3 事件流，toolCallId 精确配对）。
  TUI 渲染是本仓自写（2026-08-20 起，只做 CLI 终端——用户裁定）。

## 设计移植（不含代码拷贝）

- **herdr**（github.com/herdrdev/herdr，Apache-2.0）——`misaka/net/` 是其核心设计的
  Python 重写（守护进程独占伪终端／瘦客户端／结构快照恢复／套接字单例）；
  `misaka/cli/herdr_ui.py` 是其客户端布局的逐函数移植（ui.rs / ui/sidebar.rs /
  ui/tabs.rs 的几何算法，每个函数注明源码行号）。Apache-2.0 允许衍生；
  面板渲染是本仓自写、只复用其几何语义（2026-08-11，其 0.8.0 时期）。

## 已离开 third_party 的（2026-08-06 fork 内联）

- **harn**（github.com/secemp9/harn @0bd413b1，MIT）——**已整体改姓融入 `misaka/` 单包**
  （2026-08-06 二段式：先内联为 engine/，后与研究系统合为一包）。上游更新须手动挑拣；
  内联时带上了全部 9 组本地补丁（全量 diff 存档：momoi/misaka-build/harn-local-diff-full-20260805.patch），
  之后又叠加了 TUI 对表审计修复批（见 build-log Step-22）。上游文档在 `docs/harn-upstream/`。
- **pi-mono**（github.com/badlogic/pi-mono @686f193e，MIT）——语义上游图纸，不是代码，已移出仓外。
  需要对表时按 commit 重取：`git clone https://github.com/badlogic/pi-mono && git checkout 686f193e`。
  符号对照台账在 `misaka/protocol/PORTMAP.tsv`。

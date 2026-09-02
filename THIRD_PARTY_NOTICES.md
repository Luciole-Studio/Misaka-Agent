# 第三方声明

MISAKA 的部分文件移植自其它项目。移植代码所附带的许可证义务随代码一起转移，
因此这些声明必须随本软件的所有副本分发。逐文件的声明保留在各自的文件头部，
本文件是它们的汇总索引。

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

## 上游整体移植

`misaka/ai/**`、`misaka/core/**`、`misaka/agent/**`、`misaka/ui/tui/**` 的大部分是
Pi（coding agent）的 Python 移植。`misaka/extensions/hermes_lcm/vendor/**` 原样
收录自 hermes-lcm，`misaka/documents/pageindex/**` 原样收录自 PageIndex，两者各自
保留上游的许可证文件。

> 维护提示：新增一处从外部项目移植的代码时，把上游的许可证头随代码一起搬进来，
> 并在本文件登记一条。判断依据是上游文件自己是否带许可证头——例如 Pi 树内恰好
> 只有 `utils/ansi.ts` 与 `tui/src/stdin-buffer.ts` 两个文件带头，它们对应的移植
> 就都需要登记。

# Research 已知缺陷：上游差分核查

日期：2026-09-15。MISAKA 基线：`d346db6b9820a1f779e693250c84aea6d69da23d`。

## 范围与结论

针对此前 18 项已确认缺陷中涉及外部来源的第 ①、②、⑨、⑭ 项，检查来源、
出错调用链，以及是否存在可直接移植的正确上游实现。不是全产品一致性审计。

本次符合“本地偏离且上游没有此缺陷”的修复候选为 **0**；未修改生产代码。
这不代表已有缺陷解决。其余 Research／Board 自有缺陷不因底层使用 Pi 而成为上游缺陷。

## 固定来源

| 来源 | 固定版本 | 检查文件 |
|---|---|---|
| Pi 声明基线 v0.83.0 | `845d6ff1f6643aba440341cce877ce1c43ebbc39` | `packages/ai/src/utils/{json-parse,validation}.ts`、`packages/ai/src/api/anthropic-messages.ts`、`packages/agent/src/agent-loop.ts` |
| Pi 本次查询的 main | `8a7b0c03dfb702663acafb6dc29f8acaa4ffe391` | 同上 |
| Hermes Skills | `f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140` | `tools/{skills_tool,terminal_tool,terminal_tool_guards,approval}.py`，本地 vendor 来源清单 |
| Hermes Web 增量基线 | `62e5f466565ee56351e4483ead8e62f9e782f8b3` | `tools/web_tools.py` |
| herdr v0.8.2 | `9eb521456ac0d19d3ab3d9d7cea3cca10baa8a4c` | `src/api/client.rs`、`src/cli.rs`；客户端文件与远端固定提交逐字节相同 |

源码使用 GitHub API 的 raw 响应下载到临时目录；没有读取用户 Hermes 的 skill 内容。
Pi 的 Anthropic 流处理已位于 `src/api/anthropic-messages.ts`，不是同名 provider 注册文件。

## ② 参数解析与执行：存在差异，但换回上游不能修复

上游两版 `json-parse.ts` 字节相同。最终 `content_block_stop` 仍调用
`parseStreamingJson(block.partialJson)`，随后发出 `toolcall_end`。
解析器允许部分恢复，完全失败则返回 `{}`。工具调度仅校验得到的参数对象，
不以原始参数是否完整、是否丢失内容作为拒绝执行条件。

### 实际运行的对照

运行上游原始解析器与验证器；通过 TypeScript AST 原样提取以下 agent-loop 函数，
只补本地测试用 import/export，不改函数体：

- `prepareToolCallArguments`
- `prepareToolCall`
- `executePreparedToolCall`
- `createErrorToolResult`

依赖与两版各自 package.json 对齐：`partial-json@0.1.7`，
基线 `typebox@1.3.7`、main `typebox@1.3.27`。依赖只放 `/tmp`。
MISAKA 侧运行真实 `StreamingArgs`、诊断记录、`execute_tool_calls`。
工具仅记录内存数组，没有文件写入、网络调用或实际模型请求。

| 样本 | Pi 两版 | MISAKA |
|---|---|---|
| `not-json`，字段可选 | `{}` 并执行 | `{}` 并执行 |
| `{"note":"an "unescaped" quote"}`，字段可选 | `{"note":"an "}` 并执行 | `{}` 并执行 |
| 同一引号样本，note 必填 | 截断后的 note 通过校验并执行 | `{}` 被必填校验阻断 |
| 截断且含无效转义的字符串 | 恢复部分字符串并执行 | `{}` 并执行 |
| 有效对象、空对象、空流、普通截断字符串、完整控制字符／转义样本 | 与本地相同 | 与上游相同 |

10 个样本 × 3 个实现，共 30 次对照；两版 Pi 的结果全部相同，
每版与 MISAKA 有 7 个相同、3 个不同。测试覆盖的是解析／准备／执行边界，
不是远程 Anthropic 服务全链路。流结束路径另经源码确认。

因此“不同”已成立，但“上游无此 bug”不成立：上游同样可能执行损坏或不完整的参数。
盲目替换还会把上述必填字段原本能拦住的样本变成截断执行。
`StreamingArgs` 的本地增量解析节流、诊断、兼容 start-block 内联参数均保持原状。

## ① Skill shell 守卫：本地策略，不是 vendor 漂移

误拦位置：`misaka/core/skills/wiring/skills.py::_command_touches`。
它对 `$(`、反引号和 `${` 直接返回受保护路径访问，是 MISAKA 分层技能／只读沙箱的本地策略。
Hermes terminal 走其 approval／危险命令与 Tirith 检查链；并非这个本地路径守卫的直接替代实现。
移除本地守卫以模仿 Hermes 会丢失 MISAKA 的技能隔离契约，故没有进行这种替换。

额外核验：PROVENANCE.json 中当前存在的 50 个源码条目都符合登记 SHA256。
这证明符合已登记的本地移植产物，不表示与上游原文件字节相同。
清单还登记了 4 个当前不存在的测试路径，未将其计为通过，也未改动清单。

## ⑨ 面板阻塞：同步上游接口与本地异步调用方式

herdr `ApiClient::request_value` 本身同步连接、写请求、`BufReader::read_line` 等回复；
`cli::send_request` 同步调用它。MISAKA `ui/panel/client.py::request` 同样是同步接口。

本地出错链是 `research/workflow.py` 的 async 启动函数直接调用
`PaneSpawner.spawn` → 同步 socket request。herdr 的该客户端没有一个“原样搬来即可
避免阻塞 MISAKA asyncio loop”的异步实现。需要修 MISAKA 调用方的异步边界及取消所有权，
不宜宣称为上游代码回移。

## ⑭ 私有材料打包：本地跨模块漏检查

上游 `tools/web_tools.py` 的缓存／截断逻辑不是 MISAKA 的 Research 来源包。
本地 `web/evidence.py::check_material_read` 已定义私有材料保护；
`research/bundle.py` 打包路径没有使用它。问题属于本地模块之间遗漏既有保护，
没有从 Hermes Web 下载一个同等来源打包器即可修复的对应关系。

## 验证与产物

临时证据目录：`/tmp/misaka-upstream-bugs-20260915/`。

- `fetch-manifest.json`、`extra-manifest.json`：下载来源及摘要；首次 terminal 文件请求
  返回 HTTP 429，随后单独重试成功，没有将失败响应当源码。
- `pi-base-results.json`、`pi-current-results.json`、`local-results.json`：30 次对照输出。
- `extract.mjs`、`cases.json`、`local_probe.py`、各 Pi 目录的 `probe.ts`／`extraction.json`：复现。
- `skill-vendor-integrity.json`：源码摘要及缺失测试路径状态。
- `existing-tests.log`：47 passed、14 subtests passed。

既有回归命令：

```sh
PYTHONPATH=. .venv/bin/python -W error -m pytest -q \
  -o asyncio_default_fixture_loop_scope=function \
  tests/test_audit_fixes.py \
  tests/test_research_node_control_cleanup.py \
  tests/test_research_method_handoff.py
```

初次未显式指定 fixture loop scope 时，pytest-asyncio 的配置弃用警告在 `-W error`
下阻止启动；仅为测试命令补上该选项后通过，未修改项目配置。
未重启、恢复、停止或操纵任何 MISAKA research 进程／数据库。

# U01 / U02 源码修复记录 · 2026-09-21

## 结论

两项根因已修到当前工作树。**直接替换错误逻辑，没有新增兼容开关、备用解析器、旧逻辑回退或数据迁移。**

- 5 个产品文件、2 个回归测试文件；59 项新增回归。
- 隔离副本和应用补丁后的主目录，各自 **313 passed，warnings as errors**。
- 当前运行的 MISAKA 未重启、未恢复 Research、未改会话或数据库；源码已修不代表旧内存进程已经更新。
- 既有脏树改动保留；未暂存、提交或推送。

## 项目结构与修复点

### U01：合法正文被当成 JSONL 分隔符

原生序列化使用 `ensure_ascii=False`，允许正文包含 U+0085 / U+2028 / U+2029。
`str.splitlines()` 却把这些合法正文字符当成记录边界，截出半段 JSON。

主调用链：Research `_dispatch_children` → `planner.fork_session` → `SessionManager.open` →
`load_entries_from_file` → `_parse_jsonl_entries`。同一原生解析器还服务内存只读打开、fork、会话列表和旁观读取。
因此不是研究状态机要另造一套恢复器，而是读取规则本身错了。

统一按 `split("\n")` 读记录，修正四个真实读取点：

| 位置 | 覆盖功能 |
| --- | --- |
| [session_manager.py:1203](/Users/makiko/Projects/misaka/misaka/core/session_manager.py#L1203) | 原生会话打开、列表、分支与恢复的公共解析 |
| [subagent/runtime.py:696](/Users/makiko/Projects/misaka/misaka/core/subagent/runtime.py#L696) | 子代理原生会话恢复读取 |
| [sister_runtime.py:120](/Users/makiko/Projects/misaka/misaka/core/network/sister_runtime.py#L120) | Sister 尾部预览，防止合法消息被静默丢弃 |
| [research/bundle.py:323](/Users/makiko/Projects/misaka/misaka/core/research/bundle.py#L323) | 根据会话追踪已查阅来源，防止合法工具记录被漏读 |

真正损坏的 JSON、非对象记录继续被 strict 路径拒绝；不靠删除正文或宽松解析蒙混过去。
CRLF 是正常记录格式，不是旧逻辑兼容分支。原本按文件 `readline()` 读取的 header 路径无需改动。
普通文本排版中的 `splitlines()` 不属于 JSONL 解析，不机械替换。

### U02：二进制进入工作区目录

登记层 `_register_task_artifacts` 已有 `metadata.binary=true`，错误发生在消费侧：
`workspace._artifact_node` 无视类型，用 `errors="replace"` 读任意文件，再把偶然出现的 `#` 当标题。

[workspace.py:30](/Users/makiko/Projects/misaka/misaka/workspace.py#L30) 是根节点和研究分支共用的产物目录构建点。
修这一处，同时覆盖实时 `misaka_research_view(view="workspace")` 与 `_refresh_workspace_index` 导出的目录。

新规则：
1. 所有产物保留文件节点、路径、标题及类型；不删除 PNG/PDF/办公文件。
2. 只给 `.md` / `.markdown` 生成标题预览；尊重 `binary` 标记；异常元数据仅保留文件节点。
3. 严格 UTF-8 解码；二进制控制字符、读取异常不生成部分伪标题。
4. 每个产物最多扫描 **262,144 个字符**（额外读取 1 字符判断截断），最多 **120 个标题**，每标题最多 **200 字符**。
5. 不展示被扫描边界切断的半行；有截断时明确标注；定位保持物理行号。

这不是全工作区所有节点的全局 token 预算；PageIndex、PROJECT.md 和整体导航规模未在本修复中重构。
只清除有问题的二进制解码入口，没有保留“先试新规则、失败再用旧扫描”的绕路。

## 真实事故输入的离线验证

2026-09-21 13:21:15 +09:00 读取输入字节，AST 提取旧/新纯函数离线对照；未创建应用会话或连接运行数据库。

| 输入 | 修复前 | 修复后 |
| --- | --- | --- |
| 原 LO JSONL，53,665,401 bytes | 复现相同 `Unterminated string ... column 293 (char 292)` | **890 条记录全部读出，无解析错误** |
| 原 `tetlock_ocr/p16.png` | 7 个伪标题 | **0 个伪标题，文件路径仍保留** |

[输入哈希与结果](/Users/makiko/Projects/misaka/docs/audits/misaka-u01-u02-repair-2026-09-21/live-input-proof.json)。
原文件未改写，未复制完整私有会话/数据库进仓库；数字仅代表该次输入快照。
历史中已经存在的乱码内容没有擅自删除；U01 使合法历史可读，U02 阻止目录继续生成这类内容。

## 验证与应用

- U01 新回归：旧实现 20 failed / 11 passed → 新实现 31 passed。
- U02 最初回归：旧实现 12 failed / 3 passed；随后补齐边界，最终新增 28 项全部通过。
- 最终组合包含两套新测试，以及办公产物、研究 sweep/edge/contracts/deep、子代理原生启动、会话分支/目录、压缩守卫、研究根生命周期测试。
- [隔离副本结果](/Users/makiko/Projects/misaka/docs/audits/misaka-u01-u02-repair-2026-09-21/isolated-final.log)：313 passed in 17.68s。
- [主目录结果](/Users/makiko/Projects/misaka/docs/audits/misaka-u01-u02-repair-2026-09-21/main-final.log)：313 passed in 18.28s。
- 范围内 Ruff、AST 解析及 diff 空白检查通过；这是相关组合回归，不是全仓库全测声明。
- 两次测试均使用独立 HOME、MISAKA/XDG 状态路径及测试临时目录；原生启动测试用本地模拟服务，不请求真实供应商。
- 保留原文件 SHA 基线，先 `git apply --check`，再复核 SHA 应用；7 个文件与受测副本逐字节一致，原快照中其他文件和 Git index 未变。
- [应用记录](/Users/makiko/Projects/misaka/docs/audits/misaka-u01-u02-repair-2026-09-21/apply-proof.json) · [独立补丁](/Users/makiko/Projects/misaka/docs/audits/misaka-u01-u02-repair-2026-09-21/repair.patch) · [源码定位与证据哈希](/Users/makiko/Projects/misaka/docs/audits/misaka-u01-u02-repair-2026-09-21/manifest.json)。

## 状态边界

台账 U01、U02 改为“已有源码修复记录”。18 项已确认产品问题中，**13 项有源码修复、5 项待修（U03–U07）**；其他跟踪 7 项，总计仍是 25 条。

本次没有自动恢复失败的 driver，也没有热加载旧进程；后续若运行恢复，仍须检查已有分支占位和卡片的复用，不能把离线 fork 测试等同于当前研究已经恢复。

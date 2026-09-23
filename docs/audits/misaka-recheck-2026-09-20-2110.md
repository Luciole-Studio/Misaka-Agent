# MISAKA 第二轮结果与运行核查 · 2026-09-20 21:09–21:18 JST

记录生成：2026-09-20T21:19:32.229227+09:00。只读检查运行数据及用户现有产物；离线探针只使用临时目录、AST 提取的纯函数与假存储，不创建应用会话，不发网络或模型请求。

## 30 秒结论

**新增 1 个已复现产品缺陷 U07；另发现 1 项登记漂移 D02、2 项交付索引缺陷 Q01/Q02。** 不把这四类全称为四个新引擎 bug。

- 新增产品缺陷：引用他人的文件，被系统归成自己的交付物；红队甚至把被评原稿登记成 critique。
- 登记漂移：LO 15:19 修交付字段时直接改了 plan.json，未同步已登记 SHA；严格读取会拒绝。
- 索引问题：159 条标题出现错误转义文本；URL 前 70 字符匹配会混淆不同条约。
- 原来的输出截断后已有恢复：LO 分段写稿，20:44:40 正常 end_turn。报告实际存在，不是只口头说完成。

## 本轮覆盖范围

| 检查 | 结果 |
|---|---|
| 当前 run | r_b55f2c1cf5，driver failed/active，错误仍是 19:31 的 InvalidSessionFileError |
| 卡片 | 19 张全部 done（18 张研究 + 1 张红队） |
| 原生会话扫描 | 63 份，3968 条工具回执；逐物理 LF 解析无坏 JSON |
| 新近错误窗口 | 18:40 后：2 条工具失败（write、office，都是 32000 输出截断），1 次此前已记录的压缩错误 |
| 已登记产物 | 155 条全部存在，逐件 SHA256 核验；154 条一致，1 条计划文件不一致 |
| CSV 文件清单 | 478 行，覆盖当前 downloads 全部非 .DS_Store 文件；无缺文件、重复路径或字节数不符 |
| 两份报告 | 原稿 132765 bytes；polished 版 137372 bytes；表格列数检查未见不一致 |
| 文档定位 | 两份报告共 42 次 doc 引用、14 个独立 doc ID；元数据存在、哈希前缀有效、指定页码未越界 |

“定位有效”不等于引文内容和所有历史论断已被逐项外部核验；本轮没有上网重做历史研究。CSV 478 是 downloads 清单数量，不包含所有卡内中间文件的完整通读保证。

## 先纠正一个状态判断：这次写报告是用户决定，不是擅自退出

原生会话 703 行（19:34:28），用户明确说：**不继续了，直接开始全局总结写报告吧**。716 行进一步要求用全局所有产物、原材料和成果。

因此：
- Research driver 仍是 failed，不能称“正式研究流程正常完成”。
- 后续依据既有成果写报告，是明确用户指令；不能再把它描述成 LO 擅自离开 Research。
- 806 行（20:44:40）最终助手回合 rawStopReason=end_turn、stopReason=stop，非异常停止。
- 文件 `report-polished.md` 修改于 21:08，晚于这份 LO 会话的最后回合；本轮只核查该文件，不把其后续编辑归因给 LO。

## U07 · 引用来源被冒充为当前卡产物/红队评语 · P1 · 未修

### 现场

当前发现 5 条额外登记把其他卡目录的文件记在引用方的 task_id 下：

| 被引用文件 | 原作者卡 | 被登记为谁的产物 | 错误类别 |
|---|---|---|---|
| c17_victory_paths.md | C17 / t_a8401d | 红队 / t_759180 | critique |
| c2_gaps.csv | C2 / t_104134 | C3 / t_7ee3b0 | task_output |
| c11_invasion_england.md | C11 / t_2c338c | C17 / t_a8401d | task_output |
| c12_russia_problem.md | C12 / t_26cd47 | C17 / t_a8401d | task_output |
| c16_britain_under_defeat.md | C16 / t_b7b7b1 | C17 / t_a8401d | task_output |

每个原作者自己的登记仍在；不是物理文件被移动。最明确的错误是 `a_02fb8b58d1`：红队引用的 C17 原稿被标作红队 critique。

### 根因链

1. [worker.py:595–602](/Users/makiko/Projects/misaka/misaka/core/network/worker.py#L595)：build_submission 把 findings 的 source_file 追加进同一个 artifacts 数组，失去“输入来源/自身输出”区分。
2. [workflow.py:512–538](/Users/makiko/Projects/misaka/misaka/core/research/workflow.py#L512)：只判断路径在 nodes/final 下；未按本卡 output_dir 区分归属。再按当前卡角色统一标 task_output/critique。
3. [report.py:67–81](/Users/makiko/Projects/misaka/misaka/core/research/report.py#L67)：报告材料组装把 kind=critique 的 Markdown 正文放进红队评语集合。因此分类错误会传播，不只是一个不好看的标签。

### 复现与处理

AST 提取当前真实注册函数，在隔离目录放一个 writer 的 report.md，由 reviewer 将其作为材料提交，函数把它注册为 reviewer 的 critique；探针已通过。未对 live DB 执行注册。

修法应保留跨卡引用，但分开来源依赖和自有产物；最低限度按本卡 output_dir 分类，引用别卡文件保留真正拥有者/来源关系，不再次当本卡 output 或 critique。验收覆盖普通卡、红队、最终审查及同源多卡引用。

**这不是任务派发跑串的证据。** 全程扫描未见 write/edit 指向其他卡 ID；系统自己在提交与登记阶段混淆了引用和生产。没有扫描并语义证明所有任意 shell 都不会跨卡写入，不做绝对保证。

## D02 · plan.json 与登记哈希不一致 · 数据/维修后状态漂移

- 产物 a_2ea3413351 登记 SHA：`067f8671f39038b4962ed684261cb144d8f34772062573081796d10556765082`。
- 当前文件 SHA：`11cf6f7777a1bc8b5ceca53d7499c76f7128f744ed9d5f8af842ccd0160b9c01`。
- 同目录备份 `plan.json.bak-deliverable-fix` 与登记 SHA 完全一致。
- 原生 LO 会话 229 行（15:19:23）：为修复 F09，LO 的 bash 脚本备份并重写 18 张卡的 deliverable 字段；磁盘 mtime 同时吻合。对照 research_actions.plan，变化是这 18 个交付字段，任务分工未发生相应变化。
- 调用 AST 提取的 [runs.artifact_text](/Users/makiko/Projects/misaka/misaka/core/research/runs.py#L1233) 读取当前文件，确实报 `artifact ... changed since it was registered`。

因此不是正常保留了多版本登记，也不是已经证明底层文件随机损坏。LO 直接改受登记管理的文件，绕开了原本的原子发布/登记通路。

处理方向：明确计划修改入口，统一磁盘投影、已接受计划与登记状态；先确认这次交付字段转换意图，再经受控发布更新。不要无条件重算哈希把所有篡改都合法化。本轮未修改文件、计划或登记。

## Q01 · CSV 的 159 条标题被错误处理转义字符 · 交付产物缺陷

生成点：LO 会话 759 行（20:09:56），即时 Python 脚本用正则抓引号中的 title，然后替换反斜杠 u，而未按前置信息格式解码。

例：原始标题正常解码为 `Gallica | Vérification de sécurité`；CSV 却写成 `Gallica | V u00e9rification de s u00e9curit u00e9`。

478 行中 159 行包含此类错误转义文本。部分源标题原本就带 U+FFFD 替代符；正确解码也不能凭空还原原站已经丢失的字母。另有 120 字符裁剪，标题不总是完整书目信息。

修法：使用已有前置信息解析器读取标题并保留原字符；若需短标题，另列完整标题。对照测试包含带重音字母、引号、Unicode 符号和原本损坏的标题。

这是 LO 临时生成索引脚本/交付质量的问题，不把它伪装成已证实 Web 缓存模块的错误；原缓存标题按正常解码是正确的。

## Q02 · 用 URL 前 70 字符当引用匹配，混淆不同材料 · 交付来源关系缺陷

生成点：LO 会话 761 行（20:10:10），代码取 `url.split('//')[-1][:70]`，只要该前缀出现在卡材料中，就把卡加入 referenced_by_cards。

最明确反例：
- 法普提尔西特条约 **1807-07-09** 与法俄提尔西特条约 **1807-07-07**，来自同站同栏目，前 70 字符相同，实为不同文书。
- C1 的来源文件匹配到法俄条约 URL，CSV 却同时把法普条约那份缓存记作 C1 引用。
- C13、C6、C16 中也发现反向串配。

本轮找到 12 对“没有精确文件名/完整 URL 匹配、只有截短前缀匹配”的材料—卡关系。其中部分是同页查询参数/尾斜杠别名，不能全判作 12 次引用错误；但不同条约这一组足以确认算法会误配。

修法：使用完整规范化 URL/明确 doc ID/文件路径，不靠固定长度前缀；同页别名需有可核映射。区分“文件或来源清单中出现”“正文明确引用”“实际阅读”，不能把 SOURCES.md 中出现升级成逐件通读证据。

## 旧问题的新证据，不重复加数

- **U02 污染的代价扩大**：19:50、20:07、20:08 的四次 compaction 视图仍保留约 1,118,875 字符的工作区回包。20:08:18 的 tokensBefore 为 872172，20:08:34 为 908016；20:09:07 后大回包才离开保留窗口。它不是“一压缩就消失”。这些是日志统计，不把供应商 usage 当独立精确 tokenizer 核验。
- **截断后来恢复**：write（20:17）和 office（20:31）两次都输出 32000 tokens 后被保护性拦截。之后多个 write/拼接/edit 成功，最终原稿 SHA `492b6b6790169ce9c8141c4ef1c50b91e69ae31e0b5509a55c5bedae0e0f3749`（与 snapshot.json 一致）。
- **U01 仍在**：逐物理 LF 合法不改变 splitlines 解析器问题；本轮没有通过重写 JSONL 规避。
- 新近窗口未见更多 Sister 工具崩溃；不据此声称全项目无 bug。

## 已核实正常 / 暂不归责

- 19 张卡的注册产物当前都存在；无本 run 的新增产物丢失。
- 478 行 CSV 的路径、文件大小、覆盖 downloads 范围均正确；159 是标题问题，不是 159 个文件丢了。
- doc ID 与页码定位通过；两份报告的 Markdown 表格列数一致。原稿有一处 `downloads/James_1837_v3_ETH` 是表中对相关 PNG 组的简称，不能把它直接算作丢文件。
- 原稿/润色版没有 Markdown 可点击链接，主要使用路径、doc ID 与行页文本。这是可用性限制，不等于引用都不存在。
- 本轮不审定每一项拿破仑史实，也不因报告篇幅长就背书“学术级质量”。

## 留存

- [全量本轮结构快照](/Users/makiko/Projects/misaka/docs/audits/misaka-recheck-2026-09-20-2110/snapshot.json)：运行、63 会话、155 产物哈希、CSV 文件检查、报告基本结构。
- [离线探针结果](/Users/makiko/Projects/misaka/docs/audits/misaka-recheck-2026-09-20-2110/probes.json)：错误归属可复现、计划严格读取失败、159 个坏标题的样例、12 个前缀匹配候选。
- [事件定位](/Users/makiko/Projects/misaka/docs/audits/misaka-recheck-2026-09-20-2110/event-anchors.json)：用户改方向、计划修改、CSV 生成脚本、压缩体积。
- [源码定位与哈希](/Users/makiko/Projects/misaka/docs/audits/misaka-recheck-2026-09-20-2110/source-anchors.json)。
- [文档定位核查](/Users/makiko/Projects/misaka/docs/audits/misaka-recheck-2026-09-20-2110/document-locators.json)。
- [探针源码，仅供审阅](/Users/makiko/Projects/misaka/docs/audits/misaka-recheck-2026-09-20-2110/probe-source.txt)。

没有修改产品代码、配置、运行 DB、用户报告、CSV 或会话。没有重启、恢复、发消息给 LO/SIS，没有运行用户生成的研究脚本。只新增审计文档和小型证据；不复制完整数据库/会话/私有技能。

# Office 对照与修复：2026-09-15

最新状态见 [Office 全链路 bug 扫查](office-bug-sweep-2026-09-15.md)：新增 46 个边界用例、
22 项修复及带中文的真实 LibreOffice 验收。下文数字和样本结果保留为此前验收历史；
尤其此前英文样本的视觉通过不代表中文字体通过，后续扫查已发现并修复 macOS PDF 中文缺字。

## 独立验收后的修复与复验

此前独立验收新增的三类缺陷已修复；早期“LibreOffice 应用缺失”是当时的环境状态，
用户随后完成安装，本轮已用 **LibreOffice 26.8.0.3** 真实运行，不再以包装脚本存在作为通过依据。

- 提交后备份／暂存清理失败：保留已提交成功状态，另报清理警告和残留路径；
  清理异常不覆盖原始发布错误。失败回执改为“未发布”，避免把残留暂存文件称为已删除。
- DOCX 脚注／尾注：分别加载所属 part 的关系表；同名 relationship ID 不再串用正文链接，
  注释缺少关系表时也不回退到正文。正文、注释段落和注释表格共用正确的局部关系表。
- DOCX 简单域：保留 `w:fldSimple` 中已保存的显示文字；不执行或重新计算域指令。
- 读取缓存加入渲染版本：升级后同一原件不再命中旧解析结果；不删除用户缓存目录，
  不擅自重建已入库的文档索引或修改已有引用记录。
- 新增 `tests/office/test_office_acceptance_edges.py`：覆盖提交清理失败、暂存清理失败、
  主错误保留、公开工具成功信封、脚注／尾注关系冲突及缺失、日期域显示文字。
- 本轮原生／实机样本：DOCX、XLSX、PPTX 均导出单页 PDF；实际提取内容正确，
  三页均转成 PNG 逐页目视检查，无样本内可见的裁切、重叠或缺字。
- 真实 XLSX 缓存：`SUM(A2,3)` 在 A2=2 时为 5，编辑 A2=7 后为 10；
  `=1/0` 正确回报 `Values!B3 #DIV/0!`。doc/xls/ppt 往返后正文可读，读取不修改旧格式原件。
- 最终 Office + 顶层相邻回归：**519 passed，144 subtests passed**；此前未修改的
  独立验收脚本 **4 passed**。作用域 Ruff、离线 sdist/wheel 构建、25 个相关文件的产物字节核对通过。
- 固定上游差分仍为 **57/57 操作成功、31/57 包阶段相同、67/68 读取辅助结果相同**，
  差异性质未改变。复验日志、实机样本与页面 PNG 保存在 `/tmp/misaka-office-fix-NYrxZf/`。

以上是本次故障与样本范围的通过，不是复杂模板／任意域计算／所有字体与排版的无缺陷承诺。
下方早期测试数量与环境失败记录保留为审计历史，不表示当前仍缺少 LibreOffice。

## 结论与范围

本报告回答 Office 工具及其读取链路与 FrontierAgent 的对齐，不是整个 MISAKA/Hermes/Claude Code 的全产品对齐。
**不是源码逐字节一比一，也不是任意文档零缺陷承诺。** 56 个命名格式操作及 `format_cells` 别名已执行对照；
PDF 导出另外测试。仍保留宿主、引用、权限和资源限制的明确差异。

固定上游：[ApodexAI/FrontierAgent@9e533db6](https://github.com/ApodexAI/FrontierAgent/tree/9e533db6f6c34d16037ee5ec964c479d0eb51cde)。
逐文件来源及哈希：`misaka/core/tools/_office/PROVENANCE.json`；模块映射：同目录 `ORIGIN.md`。
没有读取或上传个人 Hermes skill 内容；没有清理用户的 `.misaka` 或改变运行中的 Research 状态。

## 双向能力表

| 功能 | 当前结论 | 落点／验证 |
|---|---|---|
| DOCX 13 个写入操作 | 全部存在并执行；保留按锚点寻址 | `_office/docx.py`；操作逐步保存／重开 |
| XLSX 26 个操作＋format_cells 别名 | 全部存在并执行；含图表、条件格式、验证、命名区域、打印、隐藏表 | `_office/xlsx.py`；操作逐步保存／重开 |
| PPTX 14 个操作 | 全部存在并执行；含复制／删除、图表、图片、备注 | `_office/pptx.py`；操作逐步保存／重开 |
| 文本 create/append/replace_text | 对齐；txt/md/csv/tsv/json/jsonl/html/htm；replace 别名保留 | `_office/text.py` |
| inline／JSON string／@file／shorthand | 接通；路径字段按 schema 解析而非改写正文 | `office.py`、`paths.py` |
| 完整操作参数对模型可见 | 补齐从上游移植的操作契约及高级参数 | `_office/schema.py` |
| DOCX 读取 | 本地 XML 实现，非 pandoc 等价实现；顺序、表格、脚注、尾注、链接、格式注记 | `documents/office/docx.py`；DOCM 和内容控件补修 |
| XLSX 读取 | 坐标、区域、公式/R1C1、格式、合并、表、透视表元信息、图表、cell_range | `documents/office/xlsx.py` |
| PPTX 读取 | 形状／表格／图表／连接线／分组／备注；纯视觉页提示补齐 | `documents/office/pptx.py` |
| 旧 doc/xls/ppt | 文档库和普通 read 现在共用转换链；原件不修改 | `office.render`、`index._legacy_office_pages` |
| 公式重算与错误回报 | 暂存重算；修改输入也重新计算；错误位置回报；重保存保真警告 | `_recalculate`、`formula_errors` |
| PDF 导出 | 默认命名、相对路径、批次回滚、第二路径权限／锁补齐 | `run_ops`、subagent policy |
| 真实 LibreOffice 运行 | **后续安装后样本复验通过**；早期包装脚本失效记录保留 | 见顶部复验与末尾历史实机边界 |
| 上游 PDF OCR／VLM 网关／多图片批读 | 不在此次 Office 移植范围；不宣称已对齐 | 保留 MISAKA 独立 PDF／图像体系 |
| 上游 read_file.save_to | 刻意不移植为 read 的副作用 | MISAKA read 保持只读接口 |

操作存在、样本通过和任意复杂模板完全保真是三件不同的事。原生 XML 读取不是 pandoc 全部转换行为的等价证明。

## 已复现并修复的问题

1. **错误信封**：STOPPED 批次原先被工具层返回为成功；现在抛出工具错误，保留失败位置和回滚说明。
2. **PDF 暂存泄漏**：默认输出曾命名成 `.office-xxx.pdf`；后续操作失败还会留下／覆盖 PDF。现在先暂存所有输出，普通失败回滚。
3. **发布异常被掩盖**：最后 `os.replace` 失败后访问第 N+1 个操作，错误变成 IndexError；现在保存原异常。
4. **取消与并发**：后台线程尚在写文件时曾释放队列锁；现在等待线程结束，所有输出使用排序后的真实路径锁。
5. **嵌套路径与权限**：图片曾按进程 cwd 而不是工具 workspace 查找；PDF 路径不经过子代理路径检查；现在共享 schema 路径解析。
6. **重算假成功**：先把源文件复制到转换输出位置，soffice 空跑也能看见旧文件并判成功；现在要求独立目录中的新输出，并复查缓存。
7. **重算时机和错误说明**：保存改动后先提交原件再重算；修改公式输入不触发重算；不回报公式错误／重保存风险。现在重算在提交前，补移植错误扫描和风险提示。
8. **透视表全局副作用**：读 XLSX 时永久替换 openpyxl 全局解析器，影响后续写入并丢失实际缓存；改为每次读取实例内的代理。保留轻量缓存定义中的源范围／字段名，只跳过大体量缓存记录；含真实透视缓存的读后写回归通过。
9. **旧格式入口断裂**：普通 read 没有旧格式转换路径，文档库却有；现在共用，不把二进制当 UTF-8 内容。
10. **额外格式承诺不实**：宏文件写入存在 VBA 丢失／扩展名与内部类型不符；写入收敛到上游三种无宏格式。DOCM 读取改为直接 XML，原件不改。
11. **归档序号**：1000 之后只解析前三位导致覆盖；改为完整整数、独占创建、数字排序和写入后保留数量控制；操作名不再能成为路径片段。
12. **图表系列丢失**：categories_col 位于中间／末列时丢掉前方数列；现在仅排除分类列。
13. **上游共有问题**：删除幻灯片不释放包关系；PPTX RichText strike 被忽略；Word 修改链接文字或增加链接损坏其他运行样式；文本替换负 count 回执计数错误。均加定向修复／测试，不照搬已知错误。
14. **防护不完整**：只扫 XML 前 64 KiB，漏掉长前导和 UTF-16/32 实体声明；改为有包大小界限的全量流式扫描。
15. **遗漏提示**：纯视觉 PPTX 页列表和转换保真提示补齐；不把文本读取称作视觉验证。

多文件发布不是跨文件同时可见的数据库事务，也不抗突然断电；如果文件系统连回滚也拒绝，报告恢复副本位置并保留副本。
取消会等待已经开始的保存完成，不表示撤销已完成写入。已存在超链接上的再套链接会明确报错，而不是生成嵌套关系或损坏原文。

## MISAKA 比上游额外的缺省／限制

| 项目 | MISAKA | 上游／理由 |
|---|---|---|
| Office render 缓存 | 256 项；workspace/.office-cache；最旧项淘汰 | 上游 `/workspace/.readdoc_cache` 无同样条数上限 |
| 意图归档 | 每目标 200 条；workspace/.office-intent；底层无 workspace 时保留 CFG 回退 | 上游共用 `/workspace/.office_writer_intent`，无同样每目标保留策略 |
| OOXML 解压 | 单成员 16 MiB、合计 64 MiB；实体扫描拒绝异常包 | 本地资源保护，不是 Office 格式标准；合法大文件也可能触发 |
| 网格展开 | 200,000 单元格 | 本地限制；上游无同样阈值，超出需 cell_range |
| 格式注记回声 | DOCX/PPTX 局部样式 200 字符；DOCX 图像 alt 120 字符 | 引用友好渲染的本地阈值，非无损格式转录 |
| read 分页 | 1 起始行号；默认 2000 行／50 KiB | 上游 0 起始字符 offset/max_chars；API 不同 |
| 文档库分页 | 先按 sheet/slide/heading 分块，再按约 3000 字符分页 | 宿主文档定位体系，不复制上游返回窗口 |
| @ops 文件 | 限工作目录内（真实路径检查） | 上游按沙箱挂载目录限制；MISAKA 本地额外读边界 |
| 隐藏／veryHidden sheet | 展开并标记 | 上游默认不展开，需按范围读取；保留原用户选择 |
| LibreOffice | 可选、300 秒转换超时、每调用独立 profile、macOS app fallback | 上游也依赖 soffice；操作时限不同，部分重算会写全局宏配置 |
| 批次 | 整批暂存、普通失败回滚；第二写入路径加锁 | 上游逐步原地写，错误前的操作保留 |
| 依赖 | python-docx/openpyxl/python-pptx 随项目声明安装 | 上游遇缺包会尝试 pip；本地不在工具调用中安装依赖 |
| prompt | 不强制所有文件先走 office；不强制字体白名单／所有数字必须公式 | 保留当前用户确认的 MISAKA 规则，不复活旧限制 |

没有一概删除这些限制：资源／权限边界和引用格式是原生适配，不应通过取消保护来制造“100%”标签。
尺寸阈值、分页和隐藏表行为仍意味着行为不是一比一；上表不是隐藏的默认值。

## 验证

- 恢复旧 Office 回归并适配当前 Research 布局、声明型 ledger、prompt 和 workspace 归档；没有回退现有治理代码。
- Office 套件当前 **333 passed**，包括所有操作逐步保存、重开，错误、缓存、分页、引用、透视缓存、取消与多文件回滚。
- 固定上游差分 **57/57 操作执行成功**；**31/57 阶段包内容一致**（排除创建／修改时间）。其余阶段的差异累积自：XLSX 显式 ARGB alpha 和 PPTX 删除时清理上游残留部件。不是 26 个缺操作。
- XLSX/PPTX 读取辅助函数扩展到真实公式／图表／透视表样本后，**67/68 结果逐值一致**；另 1 项仅为 MISAKA 的透视表提示多给了具体 sheet/range，掩码和元信息一致。早期简单样本为 68/68，最终采用更严格样本记录，不删除差异。
- 复现脚本：`scripts/audit/office_upstream.py`，要求上游 commit 和源文件 SHA256 均匹配；不联网、不自动安装、不运行其沙箱 main。
- 相邻验收：Office 加当前顶层测试 **489 passed，144 subtests passed**。第一次打包测试因隔离 HOME 的空构建缓存失败；仅在隔离目录预热构建依赖后，离线打包测试通过。
- `uv build` 已成功生成 sdist 和 wheel；作用域 Ruff 通过。
- 完整差分 JSON：同目录 `office-frontieragent-2026-09-15/comparison.json`。
- 测试文件不再使用 importorskip 隐藏必要依赖；HOME／缓存／意图日志落在测试临时目录。

### 实机边界

检测到 `/opt/homebrew/bin/soffice` 是 Homebrew 26.2.2 的包装脚本，调用
`/Applications/LibreOffice.app/Contents/MacOS/soffice`，而应用已不存在。
实机 DOCX→PDF 尝试得到明确失败，整批回滚通过；**未把“可找到命令名”记作软件可用，也未把 mock 测试记作渲染成功**。
因此真实 PDF 内容／视觉布局、真实 Excel 公式缓存和旧格式往返仍待可用 LibreOffice 验证。
未擅自恢复安装、修改用户的应用或切换正在运行的 Office 文档。

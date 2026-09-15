# Office 全链路 bug 扫查：2026-09-15

## 结论与范围

本轮沿此前 Office 验收继续排查：公开 `office` / `read` 入口、四类写入器、
OOXML 读取、路径与子代理权限、缓存／分页、批次发布／回滚、LibreOffice 和构建产物。
**新增 46 个边界用例，形成下列 22 项修复；46 是测试参数化后的数量，不是 bug 数量。**
这不是 MISAKA 全产品审计，也不是任意复杂模板／所有平台的零缺陷保证。

固定对照仍为 FrontierAgent `9e533db6f6c34d16037ee5ec964c479d0eb51cde`。
复用既有实现与依赖；Word 的跨 run 替换算法抽到 `_runs.splice_runs` 供 PPTX 共用，
没有引入第二套替换框架或运行时依赖。上游共有缺陷按本地回归修复，不为字节一致保留错误。

## 已复现并修复

路径缩写：W = `misaka/core/tools/_office/`，R = `misaka/core/documents/office/`。
定向测试在 `tests/office/test_office_sweep.py`；原有 Office 测试继续保留。

| # | 故障与修复 | 代码落点／边界证据 |
|---|---|---|
| 1 | XLSX 显式 `type:text` 仍被推断成公式／错误码；强制保留字符串类型 | W/xlsx.py `_assign`；set_cell、set_range 的 `=1+1`、`#N/A`、`00123` |
| 2 | 增量单元格格式重置无关对齐和边框；复制现有样式，仅更新请求属性 | W/xlsx.py；保留 wrap、rotation、未指定边框 |
| 3 | A1→R1C1 正则改写引号内文本、sheet 名、非合法坐标名称；改用现有 Tokenizer 的范围 token | R/xlsx.py `_r1c1`；字面量、结构化引用、ZZZ1/A0 保持原样 |
| 4 | 公式缓存检查漏掉命名空间前缀，并误报计算结果为空字符串；改为 XML 语义及单元格类型判断 | W/xlsx.py `cache_empty`，R/xlsx.py `_has_formula` / `_missing_cache` |
| 5 | 稀疏工作表按最大坐标展开，限制检查前就可能耗尽资源；公式、元信息和错误扫描仅访问已存单元格 | R/xlsx.py `_stored_cells`，W/xlsx.py `formula_errors`；XFD1048576 稀疏边界 |
| 6 | 超大查询／表／透视区域提前构建巨大坐标集；限制前不再展开全网格 | R/xlsx.py `_region_mask` 等；guarded range / iter_rows 测试 |
| 7 | 含单引号和感叹号的 sheet 范围解析失败；按最后一个 ! 分割并还原双单引号，校验正序有效坐标 | R/xlsx.py `_range` |
| 8 | 首块有 readout 前言时，续页漏掉 sheet／slide 标题；寻找第一个非代码块标题 | R/paging.py `resume_context` |
| 9 | 异类或较短代码围栏被误认为结束，内容伪装成分页标题；跟踪围栏字符、长度和闭合后缀 | R/paging.py `_fenced_spans` |
| 10 | LibreOffice 空跑可把旧目标误报为新结果；每次转换使用独立暂存目录，验证新非空文件后发布 | R/soffice.py `convert`；同源输出拒绝，失败保留旧输出 |
| 11 | Word 合并单元格重复替换，嵌套表格不访问；按正文 XML 顺序遍历段落一次 | W/docx.py `_paragraphs` |
| 12 | Word `count:0` 变成全部替换；区分未传、零、负数 | W/docx.py replace_text；零次 no-op |
| 13 | 幻灯片副本丢失备注／背景，且与原页共用可变图表和工作簿；复制内容并重建关系，克隆 chart/package 部件 | W/pptx.py `_duplicate`；保存重开后修改副本数据，原页仍为 1、副本为 9 |
| 14 | PPTX `slide:0` 当成未传，修改整套幻灯片；显式判 None 后走页号校验 | W/pptx.py replace_text |
| 15 | CSV 读取丢掉超过表头宽度的列，并重写引号／内嵌换行；原始数据正文保留，仅另加元信息 | R/xlsx.py `render_csv` |
| 16 | 写入已有 OOXML 未复用包大小／实体检查；在解析暂存副本前 precheck | W/__init__.py `run_ops`；明确 create+overwrite 仍可重建坏包 |
| 17 | `@ops` 在权限检查与实际执行间可变；读取并验证一次，把同份 inline ops 交给后续执行 | subagent/policy.py `_snapshot_office_input`、child.py；PreToolUse 后和 PermissionRequest 改写都覆盖 |
| 18 | 数字等非字符串图片路径被路径权限收集忽略，写入器却转成字符串；统一路径解析时拒绝类型不符 | W/paths.py `resolve_ops` |
| 19 | 同长度修改并恢复 mtime 后命中旧缓存；加入 dev/ino/ctime，渲染版本升级至 3 | R/cache.py `_key`；不删除用户缓存、不自动重建已有文档索引 |
| 20 | 部分重算成功仍有空缓存时，遗漏错误与重保存保真风险；成功转换后始终扫描，并单独标明重算未完成 | W/__init__.py `run_ops` |
| 21 | macOS headless 的 svp 后端导出 PDF 中文缺字；默认选择原生 osx 字体后端 | R/soffice.py `_run`；保留 --headless、独立 profile 和显式环境覆盖 |
| 22 | PPTX 文本替换不跨样式 run、不进入表格／分组；复用跨度替换并递归访问文本容器 | W/pptx.py `_paragraphs`，W/_runs.py `splice_runs`；保留尾部斜体，不跨 soft break／field 拼接 |

另加入发布各阶段故障注入：文档和多个 PDF 的原有／新建组合，在可回滚失败时恢复原有文件、移除新输出。
这不是跨文件同时可见或突然断电时的事务保证。`@ops` 快照也不是通用文件系统 TOCTOU／符号链接竞态的完全解决。

## 实机发现：PDF 存在不等于中文正确

LibreOffice **26.8.0.3**：最初 DOCX 生成方框字，XLSX/PPTX 中文漏字；只检查返回码、
PDF 页数和非空文本会误判成功。相同样本分别测试 svp、未设置后端、显式 osx，
本机只有最后一种正确使用系统 CJK 字体，遂修复 macOS 默认值，而不是删掉中文测试。

新增持久化验收脚本 `scripts/audit/office_smoke.py`，通过公开 Office 和 read 工具执行：

- DOCX / XLSX / PPTX 导出共 **4 页 PDF**；Poppler 按版面提取检查中文短语，逐页 PNG 目视检查。
- XLSX 真实 `SUM(A2,3)` 从 5 随输入编辑变为 10；错误回执包含 `Values!B3 #DIV/0!`；
  文本 `=literal` 保持字面值，空字符串公式可计算。
- PPTX 两页保留备注和背景，图表及嵌入工作簿部件独立。
- doc/xls/ppt 转换往返可读；普通读取及单独 PDF 导出前后源文件哈希不变。

**仍有明确边界**：样本 PPTX PDF 中，PDFium 文本提取返回 `中文 收验`，Poppler 的空间顺序
与实际图像均为 `中文验收`。这是仍存在的 PDF 提取顺序问题，不是本轮已修复的缺字问题；
脚本把两种结果都记入 `results.json`，没有隐藏或改写 PDFium 输出。
PPTX 分组的原生读取仍标记 `not-decomposed, vlm`；写入测试用重新打开的原生对象确认修改，
不把分组读取称作完全展开。复杂 SmartArt／高级共享部件的编辑独立性、跨平台字体全集不在通过证明内。
已生成的旧 PDF 不会自动重新导出，已入库索引也不会被本轮擅自改写。

## 验证记录

- 隔离快照 `pytest tests`：**2140 passed，1 skipped，144 subtests passed**。
  唯一跳过项是需显式可丢弃浏览器 fixture 的 Web vault live test，不是 Office 测试。
- 回写主目录后，连同其他会话新增测试复验：**2157 passed，1 skipped，144 subtests passed**。
  首次主目录回归的离线打包 fixture 因隔离 HOME 下构建缓存／uv 路径不匹配失败；
  指定已有 UV_CACHE_DIR 和 `/opt/homebrew/bin` PATH 后，单测及全量复验通过，未修改该并发测试。
- Office 专项 **390 passed**，其中定向新增用例 **46 passed**；作用域 Ruff 通过。
- 固定上游差分：**57/57 操作双方成功；30/57 包阶段相同；67/68 读取辅助结果相同**。
  包差异不是失败率：XLSX 保留原样式改变 styles.xml，并延续到后续累计操作；
  PPTX 复制新增原先丢失的备注和独立图表／工作簿部件，删除页也有已记录的关系清理差异。
  唯一 reader 差异仍为透视表重读提示补齐具体 sheet/range。
- 离线 wheel / sdist 构建通过，分别核对 26 个相关运行时／来源／许可证文件与修复源码字节一致；
  构建使用本机 `/opt/homebrew/bin/uv` 及已有缓存。
  PATH 优先的另一版本 uv 读取同一离线缓存失败，未因此新增项目依赖或改动配置。
- 详细原始日志、差分、实机样本、页面 PNG、发布校验：
  `/tmp/misaka-office-sweep-k06MDb/`（临时证据目录，不是项目运行状态）。

## 复现命令

在隔离 HOME、TMPDIR、cache 的项目副本执行，输出目录必须是新路径：

```sh
.venv/bin/python -m pytest tests/office -q
.venv/bin/python -m pytest tests -q -ra
PYTHONPATH=. .venv/bin/python scripts/audit/office_upstream.py UPSTREAM_CHECKOUT NEW_DIFF_DIR
PYTHONPATH=. .venv/bin/python scripts/audit/office_smoke.py NEW_SMOKE_DIR --render
```

实机脚本需要 LibreOffice、pdftotext；`--render` 另外需要 pdftoppm。
没有访问或上传个人 Hermes skills；没有清理用户 `.misaka`、修改用户文档、干预运行中的 Research。
修复从隔离副本按基线哈希核对后逐文件回写，保留其他会话的并发修改；没有提交或推送。

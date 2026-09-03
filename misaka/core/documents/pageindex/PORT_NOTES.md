# PageIndex 移植记录

上游 pin 与范围见 `UPSTREAM.md`。本文件是**再同步契约**：`flash/` 下每一处**语义**偏离上游的
改动都必须在此登记，升级时逐条重放。没有记录的语义漂移 = 缺陷。

巡检工具：`scripts/pageindex_sync_check.py --upstream <PageIndex 检出>`（退出码 1 = 有未登记漂移）。

## 与 hermes-lcm 的契约差别

`misaka/extensions/hermes_lcm/vendor/` 的契约是**逐字节相同**。这里不是，而且是有意的：

vendored 树在收录时过了本仓库自己的 lint 规范化（import 排序、`UP`/`C4`/`SIM`/`RUF` 等自动修复），
所以 79 个文件里几乎每一个都与上游有字节差异，而含义完全一样。对它做字节比较只会报出 72 个文件、
并且什么也说明不了——这正是 2026-09-02 审计遇到的情况。

因此判定基准是**lint 等价类**：把上游与本地各跑一遍同一套 ruff 自动修复到不动点，之后仍有差异的
才是真改动。`pageindex_sync_check.py` 就是这么做的，并额外逐模块比对公开名（改名或漏掉导出是
lint 永远产生不了的）。

## 2026-09-02 审计的核对结论

三种独立方法一致：

1. **lint 等价**：上游与本地各跑同一套 ruff 自动修复后**同时收敛**到一致，说明本地改动整个落在
   规范化等价类里。
2. **公开面**：14 个子包的 `dir()` 公开名逐个相同。
3. **行为**：三份真实 PDF 各跑一遍 `extract_toc`，输出 JSON 一致。

也就是说，除下表登记的一项外，`flash/` 的行为与 pin 处的上游相同。上游默认分支当时领先 pin
34 个提交——那是新闻不是缺陷，升级时按下表重放。

## 登记表

| 文件（相对 `flash/`） | 类别 | 说明 | 再同步动作 |
|---|---|---|---|
| `api.py` | ② 宿主接缝 | MISAKA 自写的适配器：不跑上游的 LLM 摘要与 optimize 两遍，`workers` 透传给 `extract_toc` | 比对新版上游 `api.py` 的签名与返回形状是否变形；本文件不照抄上游 |
| `heading_detection/candidates.py` | ① 规范化残余 | 上游在函数体内 `from ..labels import extract_structural_number`，本地提到模块顶部 | 新版若仍是函数内 import，同样提；若上游改成顶部则本条弃用 |
| `heading_detection/page_scan.py` | ① 规范化残余 | 同上：`is_caps_heavy`、`OutlineNode` 的函数内 import 提到顶部 | 同上 |
| `heading_detection/style_detectors.py` | ① 规范化残余 | 同上：`alignment_code` 等 8 个名字的函数内 import 提到顶部 | 同上 |
| `heading_detection/text_checks.py` | ① 规范化残余 | 同上：`extract_structural_number` 提到顶部 | 同上 |
| `model/rects.py` | ① 规范化残余 | 上游空类体里的 `pass` 被删（类体已有 docstring） | 同上 |
| `outline_assembly/cliques.py` | ① 规范化残余 | 同上：`..model` 与 `..stats` 的函数内 import 提到顶部 | 同上 |

上面六条"规范化残余"是 2026-09-03 用 `pageindex_sync_check.py` 查出来的：审计当时的判定是两棵树
在 lint 等价类里同时收敛，那个结论**基本**成立——把上游与本地各跑一遍规范化之后，残差是每个文件
一到二十行，全部是"函数内 import 提到模块顶部"这一种形状（`model/rects.py` 另有一处空类体的
`pass`）。它们语义等价，但 ruff 的自动修复产生不了"把函数内 import 提到顶部"，所以它是人做的、
应当登记的改动，而不是工具的输出。

**为什么上游要把 import 放进函数体**值得下一次升级时留意：常见理由是规避循环导入。本地提到顶部
之后目前 import sweep 与全部测试都通过，说明这些模块之间当前没有环；若某次上游新增依赖让环出现，
这六条要改回函数内 import。

未 vendored 的上游内容（刻意，不算漂移）：`pageindex/` 下的 16 个顶层模块（`client.py`、
`cloud_api.py`、`local_api.py`、`page_index_classic.py`、`tree_optimize.py` 等）、`README.md`、
`assets/`。MISAKA 只用 `flash/` 这条确定性的 PDF 大纲流水线。

## 允许的改动类别

| 类 | 说明 | 再同步动作 |
|---|---|---|
| ① 规范化 | 本仓库 lint 规则的自动修复结果 | 无需逐条记；对新版重跑同一套规则即可 |
| ② 宿主接缝 | 对上游公开 API 的引用改为 MISAKA 的适配形式 | 比对新版该处是否变形 |
| ③ 安全补丁 | 安全必需的最小修补 | 检查上游是否已自行修复，是则弃用本条 |
| ④ 上游缺陷 | 上游 bug 的最小修补 | 附 issue/PR 编号；上游合入后弃用本条 |

②③④ 三类每处在代码行尾标 `# misaka: <一句话>` 并在上表登记。① 不必标——它由工具判定，
不由人记忆。

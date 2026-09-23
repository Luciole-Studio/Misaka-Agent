<h1 align="center">
  <img src="assets/logo.svg" alt="" width="96" height="96"><br>
  MISAKA
</h1>

<p align="center"><strong>面向人文与社会科学的多智能体研究系统。</strong></p>

<p align="center"><a href="README.md">English</a> · 简体中文 · <a href="README.ja.md">日本語</a></p>

拥有规划总控智能体与常驻子智能体，基于树形数据结构治理思维过程的多智能体人文社科研究工具。

<p align="center">
  <img src="assets/setup.png" alt="misaka setup — 环境自检与模型配置" width="820">
</p>

## 一次运行是怎么走的

干活的是两个角色。

| 角色 | 职责 |
|---|---|
| **Last Order** | 协调者。把问题变成计划，再变成任务卡，最后写结论。 |
| **Sisters** | 执行者。每人领一张卡，在自己的进程里用自己的工具完成它。 |

一次运行分五步。

1. **起草计划。** Last Order 拟出思路，拿给你看。
2. **计划审批。** 直接输入 `/research`，在深度、LO 并发、每个 LO 的 Sister 卡片数、
   追加轮次之后选择 **Require approval（需批准）** 或 **Automatic（自动）**。
   需批准时，和 Last Order 像平常聊天那样讨论，认可计划后再开工；自动模式接受计划后
   直接推进，但必要的澄清仍会询问你。选择随本次研究保存，根节点、分支、追加轮次与恢复共用。
   settings.json 的 `research.plan_approval` 提供默认值（true；false 为自动）；直接 `/research 问题`
   和命令行 `misaka research` 使用该默认值。命令行需要审批时，会显示接入根节点对话的方法。
3. **发卡。** 计划变成任务卡，Sisters 领走并行开工，受每个 LO 的 Sister 卡片上限（`--sister-parallel`，默认 4）约束。
   每位 Sister 先用普通散文说清自己打算怎么做，然后就在同一个会话里做完。没有另起的
   计划文件，也没有要填的 JSON 交接格式。
4. **按需追加轮次。** Last Order 可以不急着收尾，再派 Sisters 出去一轮。
   `--followups N` 限定第一批卡回来之后她还能追加几轮，默认 2。和你讨论计划从不计入
   这个额度。每一轮沿用本次研究选择的计划审批模式。
5. **结论与红队。** 结论综合所有轮次写成，然后红队上来攻击它。若审查没发现实质问题，
   或者已经触到深度上限，就直接记录，不花一次模型调用。
   
你不想深究的分支可以由你决定跳过。该节点不做研究直接关闭，理由留档，等最终裁定时一并考虑。

研究有两项独立的并发设置：`--parallel N` 限制同时运行的 LO 节点数（默认 4），
`--sister-parallel N` 限制每个 LO 的活动 Sister 卡片数（默认 4），同一 Sister 的多个
会话也分别占槽。两项设置随研究记录保存，恢复和分支节点共用；旧研究缺少新字段时仍按 4。
实际并发还受全局／单个 Sister 准入额度和任务依赖约束；这不是所有窗口或嵌套子代理的数量。
例如 `/research --parallel 4 --sister-parallel 8 问题`，或
`misaka research --parallel 4 --sister-parallel 8 "问题"`。直接输入 `/research` 也可分别选择。

## 产出物长什么样

成果写进你选定的项目里，一个节点一个文件夹。

```
your-project/
├── nodes/<node>/              计划、结论、审查
│   ├── cards/<card>/          每位 Sister 的产出
│   ├── SOURCES.md             结论所依据的材料清单
│   └── sources/               被引用的文件本身
└── final/<run>-<file>         问题、综述、草稿、最终报告
```

`sources/` 里是硬链接，所以不占额外空间，原件也绝不会被移动。只有在无法建立链接时
才改为复制。这些包是派生物：不登记、不索引、不提交。

Git 历史是可选的，而且很轻：节点关闭时一次提交，运行结束时一次。交付不需要 worktree，
也不需要 merge。

## 安装

MISAKA 不在 PyPI 上，那边的 `misaka` 是个不相干的包。请从本仓库安装。

```sh
# 按你实际要用的供应商挑 SDK
pip install "misaka[anthropic] @ git+https://github.com/Luciole-Studio/Misaka-Agent.git"

# 或者从检出目录装
git clone https://github.com/Luciole-Studio/Misaka-Agent.git
cd Misaka-Agent && pip install ".[anthropic]"

# 开发环境
uv venv .venv --python 3.13 && uv sync
```

可选组件：`anthropic`、`openai`、`google`、`bedrock`、`mistral`，或用 `providers`
一次装齐五个。`pageindex` 增加 PDF 大纲提取，`browser` 增加浏览器工具。

有两样 MISAKA 不会替你装：

- **git** 是必需的。`misaka init` 会创建项目仓库，被采纳的结果会提交进去。
  用 `xcode-select --install` 或 `apt install git` 装上。
- **ripgrep** 和 **fd** 是 `grep` 与 `find` 两个工具的后端。用
  `brew install ripgrep fd` 或 `apt install ripgrep fd-find` 装，也可以把二进制
  直接放进 `~/.misaka/cache/bin`。

## 第一次运行

```sh
misaka setup
```

向导会检查环境，保存一份供应商凭据和默认模型并发一次测试请求，创建你的第一批
Sisters，询问是否装 PDF 组件，有 key 的话固定一个网络搜索后端，最后初始化项目文件夹。
每一节都可以单独重跑，比如 `misaka setup model`。没配凭据时直接运行 `misaka`，
它自己会把向导叫起来。

`misaka update` 告诉你这份安装是否落后于仓库的 `main` 分支，`--apply` 把它快进上去。
它跟分支而不是 release tag，遇到快进不了的 checkout 就报出原因而不替你解决。

反过来的一端是 `misaka uninstall`：它会先列出 `~/.misaka` 下每一项装了什么、占多少，
再删掉全部（凭据、看板、上下文引擎的记忆、各种缓存）。你的项目文件夹一个都不碰，
而且会把它们列出来说明这一点。卸载包本身是安装器的事，命令会打印给你。

全新安装默认走 `anthropic` / `claude-sonnet-4-5`。想手动给凭据：

```sh
export ANTHROPIC_API_KEY=sk-ant-...   # 所有内置供应商都认环境变量
misaka auth check                     # 逐个供应商检查，走的是会话用的同一套解析
```

在聊天里，`/login` 会把 OAuth token 或 API key 存进 `~/.misaka/credentials/auth.json`，
权限 0600。`/model` 打开模型选择器，从里面选会存成所有 Sister 的默认值；
`/model <名字>` 只切换你眼前这一个会话。

## 命令

```sh
misaka                 # 面板；被管道接走时退化为纯聊天
misaka chat            # 和 Last Order 对话
misaka research "..."  # 开一次研究运行
misaka board           # 任务板
misaka doc add x.pdf   # 索引一份文档
misaka create          # 新增一位 Sister
misaka web status      # 当前搜索后端与凭据
```

其余的用 `misaka --help` 看：`task`、`tell`、`dm`、`net`、`skills`、`bundles`、
`moa`、`lcm`、`auth`、`remove`、`uninstall`、`update`。

## 面板

<p align="center">
  <img src="assets/tui.png" alt="misaka 面板 — 空间、会话与 Sisters 名册，旁边是可交互的 Last Order" width="820">
</p>

在终端里直接跑 `misaka` 会打开一个多窗格面板。研究分支 fork 出去后会拿到自己的标签页：
节点进程把它的 Last Order 作为交互窗口跑在那里，她的 Sisters 在旁边分格排开。你在那个
标签页里输入的每一句，都是那位 Last Order 的一轮对话。节点关闭后窗口不会消失，你可以
继续问她都查到了什么。运行中途关掉它等于结束该节点，`/research resume` 可以重试。

命令行运行没有面板，节点是后台进程。用 `misaka chat --attach --session PATH` 接进去，
你的输入直达原主人，中间不隔第二个模型。回车可以给忙碌的会话补充指示，也可以在空闲的
会话上起一轮。`/pause` 让会话停在下一个请求、工具或工作流边界上，`/resume` 放行。
已经在跑的工具和智能体不会被中断，关掉附着的窗口也只是断开而已。

## 文档与网络

`doc_add`、`doc_find`、`doc_read`、`doc_outline`、`doc_page_image`、`doc_verify`
让智能体拥有一个可以引用、并能回头核对原文的语料库。`misaka doc` 是同一套东西的命令行入口。

网络搜索开箱即用，不需要任何配置，由一组免密钥供应商轮转支撑。要固定后端或加 key：

```sh
misaka web set backend tavily
misaka web set env.TAVILY_API_KEY tvly-...   # 以 0600 写入 ~/.misaka/.env
```

导出的环境变量始终压过配置文件。`misaka web setup` 提供供应商与档位选择器，
凭据输入是隐藏的。发现、启用/停用、重载和当前限额见 `misaka web --help`。

智能体还有一套常规工具：`bash`、`read`、`write`、`edit`、`grep`、`find`、
`web_fetch`、`download_file`，以及读写 `.docx` / `.xlsx` / `.pptx` 的 `office`。

## 上下文引擎

长会话使用 **MISAKA LCM**：基于固定版本
[hermes-lcm](https://github.com/stephenschoettler/hermes-lcm) 的项目级 fork。
压缩、摘要和检索算法保持原实现；应用层将缓存放在 `<项目>/.misaka/lcm/`，
同项目的代理共用，其他项目隔离。最后一个使用者退出后清理缓存；异常退出的残留在下次启动时清理。

原始 session 与采纳后的压缩检查点继续保存，恢复会话时重建 LCM；接续引用的源 session 也应保留。
这里清理的是 LCM 缓存，不是会话历史、Board 或项目成果。
LCM 自己配置的脱敏、忽略、保留与 GC 策略依然生效——这里不承诺原始数据被无条件永久保留。
算法参数沿用上游的 `LCM_*` 原名，不设产品别名，`misaka lcm --help` 暴露的是原始的操作
语法。移植了源码不等于上游宿主的每一处行为都被复现，差异记录在
`misaka/extensions/misaka_lcm/PORT_NOTES.md`。

## 配置

全局配置都在唯一的根目录 `~/.misaka/` 下（用 `MISAKA_HOME` 可整体挪走），LCM 临时缓存位于项目工作目录。算法参数的环境变量压过配置文件。

| 位置 | 内容 |
|---|---|
| `settings.json` | 所有设置，按节存放：pi 自己的键（`defaultProvider`、`defaultModel`…）、`allies`、`skills`、`moa`、`web` |
| `credentials/auth.json` | 保存的凭据，权限 0600 |
| `models.json` | 自定义供应商与模型，例如 OpenAI 兼容网关 |
| `MISAKA.md`、`skills/`、`subagents/` | 所有角色共享的身份、技能、子代理类型 |
| `profiles/last_order/` | Last Order 的人格、技能与 MCP 配置 |
| `profiles/sisters/<id>/` | 每位 Sister 一个目录 |

最常用的几个设置：

| 设置 | 默认值 | 含义 |
|---|---|---|
| `defaultProvider` / `defaultModel` | `anthropic` / `claude-sonnet-4-5` | 没有自己钉模型的角色所用的供应商和模型 |
| `network.max_concurrent_sisters` | 空闲内存 / 256 MiB，取 4–12 | 本机同时运行的卡数 |
| `research.token_cap` | `0`，关闭 | 任务板上显示并强制执行的 token 预算 |
| `research.plan_approval` | `true` | 计划是否要等你点头 |
| `lcm.context_threshold` | `0.35` | 上下文用到多少比例时 LCM 开始压缩 |

**全部设置记在 [CONFIGURATION.md](CONFIGURATION.md)**，按控制对象分组。值不合法时命令会停下，
并报出设置名和它的值。环境里的 `MISAKA_*` 名字都是 MISAKA 设给自己子进程用的，不是设置；
只有 `MISAKA_HOME` 归你。供应商密钥、插件旋钮这类"别的代码从环境里读"的东西放 `~/.misaka/.env`。

## 排查

聊天里的 `/debug` 会把渲染出的屏幕和整段对话写进
`~/.misaka/logs/misaka-debug.log`，权限 0600，并打印路径。这是唯一的诊断开关，
没有任何 debug 环境变量。

## 建立在什么之上

MISAKA 的内核是 [pi](https://github.com/earendil-works/pi) 的 Python 移植，面板是
[herdr](https://github.com/herdrdev/herdr) 的移植。它收录了
[hermes-lcm](https://github.com/stephenschoettler/hermes-lcm) 做上下文管理、
[PageIndex](https://github.com/VectifyAI/PageIndex) 做 PDF 结构提取，以及
[ghostty](https://github.com/ghostty-org/ghostty) 的 VT 库作为每个窗格背后的终端仿真器。

完整索引在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)：什么来自哪里、
锁在哪个提交、改了什么。

## 许可证

[Apache License 2.0](LICENSE)。第三方组件各自保留原有许可证，全部登记在
THIRD_PARTY_NOTICES.md 中。

# 入门指南

[English](getting-started.md) · 简体中文 · [日本語](getting-started.ja.md)

这一页带你从零开始，一直到拿到第一份研究报告。全程大约十分钟，大部分时间花在设置向导上。

**你需要**一台 Mac、Linux 或 Windows 10/11 电脑（x86_64 或 ARM64）、一个终端（Windows 上用 Windows Terminal），以及至少一个模型服务商的使用权限：API 密钥，或者可以登录使用的订阅，比如 ChatGPT 或 GitHub Copilot。Claude 账号也能登录，但这部分用量由 Anthropic 按 token 另计为额外用量，不走套餐额度。

除本页外，其余文档目前只有英文版。

## 1. 安装 MISAKA 需要的工具

| 工具 | 用途 | macOS | Debian / Ubuntu | Windows |
|---|---|---|---|---|
| [uv](https://docs.astral.sh/uv/) | 安装 MISAKA；你的 Python 低于 3.12 时，它会另装一个 | `brew install uv` | 见 uv 官网 | `winget install astral-sh.uv` |
| git | 项目及其历史 | `xcode-select --install` | `sudo apt install git` | `winget install Git.Git` |
| ripgrep、fd | 搜索文件 | `brew install ripgrep fd` | `sudo apt install ripgrep fd-find` | `winget install BurntSushi.ripgrep.MSVC`, `winget install sharkdp.fd` |
| poppler | 读取 PDF | `brew install poppler` | `sudo apt install poppler-utils` | `winget install oschwartz10612.Poppler` |
| ocrmypdf（可选） | 扫描版 PDF | `brew install ocrmypdf tesseract-lang` | `sudo apt install ocrmypdf tesseract-ocr-chi-sim tesseract-ocr-jpn` | 见 [OCRmyPDF 的 Windows 安装说明](https://ocrmypdf.readthedocs.io/en/latest/installation.html#installing-on-windows) |
| DjVuLibre（可选） | DjVu 文件 | `brew install djvulibre` | `sudo apt install djvulibre-bin` | `winget install DjVuLibre.DjView` |
| LibreOffice（可选） | 旧版 `.doc`、`.xls`、`.ppt` 文件；显示幻灯片，或以 EMF/WMF 保存的 Word 图表 | `brew install --cask libreoffice` | `sudo apt install libreoffice` | `winget install TheDocumentFoundation.LibreOffice` |

OCR 那一行会装好英文、中文和日文，这是 MISAKA 默认识别扫描件时用的语言。必需的工具如果缺了，设置向导会告诉你缺哪个、怎么装。Windows 上 `winget install` 之后要新开一个终端，新装的工具才找得到。

## 2. 安装 MISAKA

```sh
uv tool install "misaka[providers] @ git+https://github.com/Luciole-Studio/Misaka-Agent.git"
```

请从这个地址安装；PyPI 上叫 `misaka` 的包是另一个项目。
每个 GitHub release 也提供各架构的预编译包，内含 uv、git、ripgrep、fd 与 poppler。macOS 与 Linux：
`curl -fsSL https://raw.githubusercontent.com/Luciole-Studio/Misaka-Agent/main/scripts/install.sh | sh`。
Windows PowerShell：
`irm https://raw.githubusercontent.com/Luciole-Studio/Misaka-Agent/main/scripts/install.ps1 | iex`。
Homebrew 与 winget 见 [release/README.md](../release/README.md)。

`pip install` 和 `pipx install` 也接受同样的写法。方括号里的部分用来选择可选组件：

| 可选组件 | 增加的功能 |
|---|---|
| `providers` | 所有模型服务商的 SDK。只想装一家，就写 `anthropic`、`openai`、`google` 或 `bedrock`（OpenRouter、Kimi Code 和其他兼容 OpenAI 的服务用 `openai`；Mistral 不需要 SDK）。 |
| `browser` | 操控网页浏览器的工具 |
| `lcm-semantic` | 按意思检索过去的对话 |

同时装几个：`"misaka[providers,browser] @ git+https://..."`。

## 3. 运行设置向导

为你的研究建一个文件夹，在里面运行向导：

```sh
mkdir my-research
cd my-research
misaka setup
```

向导一共八步。在菜单里，回车保留当前显示的值，左方向键退回上一步，Esc 退出；已经完成的部分会保留。以后再运行一次，就是回顾现有的设置，只改你选中的地方。

| 步骤 | 你要做的 |
|---|---|
| **Environment**（环境） | 不用做什么。它会检查第 1 步里的工具。 |
| **Model & provider**（模型与服务商） | 选择给谁设置模型（所有人的默认，或某一个 agent），选服务商，然后通过浏览器登录或粘贴 API 密钥，再选一个模型。向导会发一个很小的测试请求，密钥不能用的话现在就能发现。 |
| **Sisters** | 选 Sister 们用的模型，然后创建你的第一批专家。接受 `10032`，用几个词写下她的专长，再加一位：两位 Sister 可以互相审查。 |
| **Skills**（技能） | 不用做什么。它会告诉你技能放在哪里。 |
| **Documents**（文档） | 看看哪些格式能读。缺 PDF 章节目录功能的话，它会提议帮你装上，或者打印出安装命令。 |
| **Web search**（网页搜索） | 按回车即可。搜索不用任何设置就能用。 |
| **Research**（研究） | 了解一次研究是怎么进行的。可以顺手粘贴一个免费的 [OpenAlex](https://openalex.org/rest-api) 密钥，让文献扫描更稳定。 |
| **Project**（项目） | 确认文件夹。它会成为一个带 `PROJECT.md` 的 git 仓库。如果你已经有 PDF 或笔记，可以让向导建立索引（默认从 `sources/` 读取）。 |

向导最后会汇总哪些已经可用、下一步该运行什么。

> **你的 shell 里已经有 `ANTHROPIC_API_KEY` 之类的密钥？** 那么直接运行 `misaka` 就会跳过向导、直接打开。请自己运行一次 `misaka setup`，或者至少创建一位 Sister：`misaka create 10032 --desc "History and social research"`。研究至少需要一位。

## 4. 打开 MISAKA

```sh
misaka
```

这会打开面板，协调者 Last Order 在她的窗口里等你。像平时聊天一样跟她打字就行。

第一天需要知道的三件事：

- **按键帮助。** 先按 `ctrl+b` 再按 `?`，查看面板的按键。在聊天输入框里，`/hotkeys` 列出编辑按键，`/` 列出所有命令。[面板指南](guide/panel.md)解释了整个屏幕。
- **退出会停掉一切。** 关闭面板（或它所在的终端窗口）会关掉 Last Order 和所有 Sister。研究会边进行边保存，所以之后可以[接着跑](guide/research.md#following-a-run)。
- **普通聊天。** 面板跑不起来的地方，`misaka` 会打开和 Last Order 的普通聊天。研究在那里也能进行，其他节点在后台运行，[研究指南](guide/research.md#runs-started-from-the-shell)讲了怎么连上它们。

## 5. 提出你的第一个问题

在 Last Order 的窗口里输入：

```text
/research
```

选项器会问九个问题：研究可以走多深、同时跑多少、计划要不要等你同意，还有其他几项。每一题都按回车，就会选第一个选项，得到一次两层深的快速研究；最后一屏请你确认。然后像普通消息一样输入你的问题，用你自己的话、任何语言都可以：

```text
清朝官员为什么在 1905 年废除科举？当时有谁反对？
```

接下来会发生的事：

1. **Last Order 写出计划**给你看：这个问题真正在问什么、哪位 Sister 负责哪一部分、谁来当红队。
2. **你们商量。** 可以要求修改，也可以直接说没问题。你同意之后她才开工。
3. **Sister 们开始工作。** 每一位都在 Last Order 旁边的窗格里打开，你可以看着，也可以走开。
4. **结论接受质疑。** 红队 Sister 先找结论的错误，再找它没有走的可能和留下的缺口。Last Order 逐条回应，补上缺口。真正不同的可能会开成新的研究，各自一个标签页，每份计划同样等你同意。
5. **报告送达。** Last Order 把报告写成一篇研究论文，经过审查，送达时会告诉你。

一次研究会发出大量模型调用，允许走得越深越广，调用就越多。想先便宜地试一试，就把深度和节点数设小一些。`/research stop` 可以提前结束，已经得到的结果仍会写出来。

## 6. 查看结果

所有东西都在你的项目文件夹里：

```text
my-research/
├── final/<run>-final.md     报告
├── final/<run>-sources/     报告引用的每一个文件
└── nodes/<node>/            每一项研究：计划、Sister 的工作、结论、批评
```

用任何 Markdown 阅读器打开报告。它的注释标明每一处出处，项目里有对应文件的也会一并注明；附录列出这次研究走过的每一条线索、答案承担的代价，以及没有走的路。

## 接下来

| 想要 | 请读（英文） |
|---|---|
| 引导研究、调整深度和费用、接着跑 | [Research runs](guide/research.md) |
| 增加 Sister、给她们技能、请 Claude Code 或 Codex 加入 | [The team](guide/team.md) |
| 熟悉面板 | [The panel](guide/panel.md) |
| 用别的模型，或者本机上的模型 | [Models](guide/models.md) |
| 把你自己的资料库交给 agent | [Documents and the web](guide/sources.md) |
| 解决问题 | [Troubleshooting](guide/troubleshooting.md) |

## 更新

```sh
misaka update            # 我的版本落后了吗？
misaka update --apply    # 更新到最新
```

`--apply` 会再确认一次，然后用你当初安装时的工具来更新，并保留你装过的可选组件。更新前它会先把你的设置、凭据和任务看板复制一份到 `~/.misaka/state/backups/`。如果有研究或任务卡正在运行（包括正在等你同意计划的研究），它会停下来，请你先让它们跑完或者停掉。

你的数据会随每个版本一起往前带，而旧版本读不了新版本写下的数据。想退回旧版本的话，也要把那份备份一起恢复。

Windows 上的 0.18.1 和 0.18.2 没法自己更新：关掉所有 MISAKA 窗口，运行一次 `uv tool upgrade misaka`。从 0.18.3 起，Windows 上也可以直接用 `misaka update --apply`。

## 卸载

```sh
misaka uninstall --dry-run   # 列出会删除什么
misaka uninstall             # 询问要删除什么
```

默认保留数据：删掉程序，`~/.misaka/` 留着，重新安装后可以接着用。`--full` 连数据一起删除（删除前会提议备份），`--data` 只删除数据。请先关闭面板。你的项目文件夹会保留报告、资料和设置；`--full` 只会删掉 MISAKA 在其中留下的缓存。

在 Windows 上还要关掉其他所有 MISAKA 窗口，因为 Windows 删不掉正在运行的程序。程序会在这条命令退出后随即删除。

# Getting started

English · [简体中文](getting-started.zh-CN.md) · [日本語](getting-started.ja.md)

This page takes you from nothing installed to your first research report. It takes about ten
minutes, most of it the setup wizard.

**You will need** a Mac, a Linux machine or a Windows 10/11 PC (x86_64 or ARM64), a terminal (on
Windows, Windows Terminal), and access to at least one model provider: an API key, or a subscription you sign in
with, such as ChatGPT or GitHub Copilot. A Claude account can sign in too; Anthropic bills that use
per token as extra usage, outside your plan's limits.

## 1. Install what MISAKA needs

| Tool | What for | macOS | Debian / Ubuntu | Windows |
|---|---|---|---|---|
| [uv](https://docs.astral.sh/uv/) | installs MISAKA, and a Python 3.12 if yours is older | `brew install uv` | see uv's site | `winget install astral-sh.uv` |
| git | projects and their history | `xcode-select --install` | `sudo apt install git` | `winget install Git.Git` |
| ripgrep, fd | searching files | `brew install ripgrep fd` | `sudo apt install ripgrep fd-find` | `winget install BurntSushi.ripgrep.MSVC`, `winget install sharkdp.fd` |
| poppler | reading PDFs | `brew install poppler` | `sudo apt install poppler-utils` | `winget install oschwartz10612.Poppler` |
| ocrmypdf (optional) | scanned PDFs | `brew install ocrmypdf tesseract-lang` | `sudo apt install ocrmypdf tesseract-ocr-chi-sim tesseract-ocr-jpn` | [OCRmyPDF's Windows guide](https://ocrmypdf.readthedocs.io/en/latest/installation.html#installing-on-windows) |
| DjVuLibre (optional) | DjVu files | `brew install djvulibre` | `sudo apt install djvulibre-bin` | `winget install DjVuLibre.DjView` |
| LibreOffice (optional) | old `.doc`, `.xls`, `.ppt` files; showing a slide, or a Word chart stored as EMF/WMF | `brew install --cask libreoffice` | `sudo apt install libreoffice` | `winget install TheDocumentFoundation.LibreOffice` |

The OCR line installs English, Chinese and Japanese, the languages MISAKA reads scans in by
default. If something required is missing, the setup wizard tells you which tool and how to
install it. On Windows, open a new terminal after `winget install` so it sees the new tools.

## 2. Install MISAKA

```sh
uv tool install "misaka[providers] @ git+https://github.com/Luciole-Studio/Misaka-Agent.git"
```

Install from this address; the package called `misaka` on PyPI is a different project.
A GitHub release also ships one prebuilt archive per architecture, with uv, git, ripgrep, fd and
poppler inside it. macOS and Linux:
`curl -fsSL https://raw.githubusercontent.com/Luciole-Studio/Misaka-Agent/main/scripts/install.sh | sh`.
Windows PowerShell:
`irm https://raw.githubusercontent.com/Luciole-Studio/Misaka-Agent/main/scripts/install.ps1 | iex`.
Homebrew and winget are described in [release/README.md](../release/README.md).

`pip install` and `pipx install` accept the same requirement. The part in brackets chooses the
optional pieces:

| Extra | Adds |
|---|---|
| `providers` | every model provider's SDK. To install one only, use `anthropic`, `openai`, `google` or `bedrock` (OpenRouter, Kimi Code and other OpenAI-compatible services use `openai`; Mistral needs no SDK). |
| `browser` | tools that drive a web browser |
| `lcm-semantic` | searching past conversations by meaning |

Several at once: `"misaka[providers,browser] @ git+https://..."`.

## 3. Run the setup wizard

Make a folder for your research and run the wizard in it:

```sh
mkdir my-research
cd my-research
misaka setup
```

The wizard goes through eight steps. In its menus, Enter keeps the value shown, the left arrow
goes back a step, and Esc leaves; what you finished is kept. Running it again later reviews what
you have and changes only what you choose.

| Step | What you do |
|---|---|
| **Environment** | Nothing. It checks the tools from step 1. |
| **Model & provider** | Choose whose model to set (everyone's default, or one agent's), pick a provider, and sign in through the browser or paste an API key. Pick a model. The wizard sends one tiny test request, so a key that does not work shows up now. |
| **Sisters** | Choose the model Sisters run on, then create your first specialists. Accept `10032`, give her a specialty in a few words, and add a second one: two Sisters can review each other. |
| **Skills** | Nothing. It shows where skills live. |
| **Documents** | See what can be read. If PDF outlines are missing, it offers to add them, or prints the command that does. |
| **Web search** | Press Enter. Search works without any setup. |
| **Research** | Read how a run behaves. Optionally paste a free [OpenAlex](https://openalex.org/rest-api) key, which keeps literature scans reliable. |
| **Project** | Confirm the folder. It becomes a git repository with a `PROJECT.md`. If you already have PDFs or notes, let the wizard index them (from `sources/` by default). |

The wizard ends with a summary of what works and what to run next.

> **Already have a key such as `ANTHROPIC_API_KEY` in your shell?** Then plain `misaka` opens
> straight away, without the wizard. Run `misaka setup` yourself, or at least create a Sister:
> `misaka create 10032 --desc "History and social research"`. Research needs at least one.

## 4. Open MISAKA

```sh
misaka
```

This opens the panel with Last Order, the coordinator, waiting in her window. Type to her as you
would in any chat.

Three things to know on day one:

- **Help with keys.** Press `ctrl+b`, then `?`, for the panel's keys. In the chat box, `/hotkeys`
  lists the editor's keys and `/` lists every command. [The panel](guide/panel.md) explains the
  screen.
- **Quitting stops everything.** Closing the panel (or its terminal window) shuts down Last Order
  and the Sisters. A research run is saved as it goes, so you can
  [resume it](guide/research.md#following-a-run) later.
- **Plain chat.** Where the panel cannot run, `misaka` opens a plain chat with Last Order.
  Research works there too; its other nodes run in the background, and
  [the research guide](guide/research.md#runs-started-from-the-shell) shows how to reach them.

## 5. Ask your first question

In Last Order's window, type:

```text
/research
```

A picker asks nine questions: how deep the research may go, how much may run at once, whether
plans wait for your approval, and a few more. Enter on each takes the first choice, which makes a
quick two-level run; a last screen asks you to confirm. Then type your question as an ordinary
message, in your own words and in any language:

```text
Why did Qing officials abolish the civil service examinations in 1905, and who opposed it?
```

What happens next:

1. **Last Order writes a plan** and shows it to you: what the question is really asking, which
   Sister takes which part, and who will play red team.
2. **You talk it over.** Ask for changes, or say it looks good. She starts once you agree.
3. **The Sisters work.** Each opens in a pane beside Last Order, so you can watch, or walk away.
4. **The conclusion is challenged.** A red-team Sister looks for what is wrong with it, then for
   the possibilities it passed over and the gaps it left. Last Order answers every objection and
   fills the gaps. Real alternatives open as research of their own, each in a tab, and each
   plan waits for you the same way.
5. **The report arrives.** Last Order writes it as a research article, has it reviewed, and
   tells you when it is delivered.

A research run makes many model calls, and the deeper and wider it may go, the more it makes.
For a cheaper trial, keep the depth and the number of nodes small. `/research stop` ends a run
early and still writes what it found so far.

## 6. Read the result

Everything is in your project folder:

```text
my-research/
├── final/<run>-final.md     the report
├── final/<run>-sources/     every file the report cites
└── nodes/<node>/            each piece of research: plan, Sisters' work, conclusion, critique
```

Open the report in any Markdown reader. Its notes cite each source, with the file in the project
where there is one, and its appendices list every line of inquiry the run followed, the costs the
answer accepts and the paths it did not take.

## Where to go next

| To | Read |
|---|---|
| steer a run, change depth and cost, resume | [Research runs](guide/research.md) |
| add Sisters, give them skills, bring in Claude Code or Codex | [The team](guide/team.md) |
| find your way around the panel | [The panel](guide/panel.md) |
| use other models, or a model running on your machine | [Models](guide/models.md) |
| give the agents your own library | [Documents and the web](guide/sources.md) |
| fix a problem | [Troubleshooting](guide/troubleshooting.md) |

## Updating

```sh
misaka update            # am I behind?
misaka update --apply    # bring me up to date
```

`--apply` asks once more, then updates through the tool you installed with and keeps your
extras. First it saves a copy of your settings, credentials and board in
`~/.misaka/state/backups/`. While a research run or a card is running (a run waiting for your
approval counts), it stops and asks you to let them finish or stop them first.

Your data moves forward with each version, and an older version cannot read what a newer one has
written. To go back to an older version, restore that saved copy as well.

On Windows, 0.18.1 and 0.18.2 cannot update themselves: close every MISAKA window and run
`uv tool upgrade misaka` once. From 0.18.3 on, `misaka update --apply` works there too.

## Uninstalling

```sh
misaka uninstall --dry-run   # show what would be removed
misaka uninstall             # asks what to remove
```

Keeping your data is the default: the program goes and `~/.misaka/` stays, so a reinstall picks
up where you left off. `--full` removes your data too, after offering a backup, and `--data`
removes only the data. Close the panel first. Your project folders keep your reports, sources and
settings; `--full` removes only MISAKA's caches inside them.

On Windows, close every other MISAKA window too, since Windows cannot remove a program that is
running. The program goes as soon as the command exits.

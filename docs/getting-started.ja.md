# はじめに

[English](getting-started.md) · [简体中文](getting-started.zh-CN.md) · 日本語

このページでは、何もインストールしていない状態から、最初の研究報告を受け取るまでを案内します。所要時間は十分ほどで、その大半はセットアップウィザードです。

**必要なもの**は、Mac、Linux、または Windows 10/11 のマシン（x86_64 / ARM64）、ターミナル（Windows では Windows Terminal）、そして少なくとも一つのモデルプロバイダを使える状態です。API キーか、ChatGPT や GitHub Copilot のようにサインインして使うサブスクリプションがあれば始められます。Claude のアカウントでもサインインできますが、その利用は Anthropic によってトークン単位の追加利用として課金され、プランの利用枠には含まれません。

このページ以外のドキュメントは、今のところ英語版のみです。

## 1. MISAKA に必要なツールを入れる

| ツール | 用途 | macOS | Debian / Ubuntu | Windows |
|---|---|---|---|---|
| [uv](https://docs.astral.sh/uv/) | MISAKA のインストール。Python が 3.12 より古ければ別に用意します | `brew install uv` | uv のサイトを参照 | `winget install astral-sh.uv` |
| git | プロジェクトとその履歴 | `xcode-select --install` | `sudo apt install git` | `winget install Git.Git` |
| ripgrep、fd | ファイル検索 | `brew install ripgrep fd` | `sudo apt install ripgrep fd-find` | `winget install BurntSushi.ripgrep.MSVC`, `winget install sharkdp.fd` |
| poppler | PDF の読み取り | `brew install poppler` | `sudo apt install poppler-utils` | `winget install oschwartz10612.Poppler` |
| ocrmypdf（任意） | スキャンした PDF | `brew install ocrmypdf tesseract-lang` | `sudo apt install ocrmypdf tesseract-ocr-chi-sim tesseract-ocr-jpn` | [OCRmyPDF の Windows 向け手順](https://ocrmypdf.readthedocs.io/en/latest/installation.html#installing-on-windows)を参照 |
| DjVuLibre（任意） | DjVu ファイル | `brew install djvulibre` | `sudo apt install djvulibre-bin` | `winget install DjVuLibre.DjView` |
| LibreOffice（任意） | 古い `.doc`、`.xls`、`.ppt` ファイル、スライドの表示、EMF/WMF で保存された Word の図表 | `brew install --cask libreoffice` | `sudo apt install libreoffice` | `winget install TheDocumentFoundation.LibreOffice` |

OCR の行では英語・中国語・日本語が入ります。MISAKA がスキャン画像を読むときの既定の言語です。必須のツールが足りないときは、どれが足りず、どう入れればよいかをセットアップウィザードが教えてくれます。Windows では `winget install` のあと、新しいターミナルを開くと入れたツールが見つかります。

## 2. MISAKA をインストールする

```sh
uv tool install "misaka[providers] @ git+https://github.com/Luciole-Studio/Misaka-Agent.git"
```

必ずこのアドレスからインストールしてください。PyPI の `misaka` という名前のパッケージは別のプロジェクトです。
各 GitHub リリースにはアーキテクチャごとのビルド済みアーカイブもあり、uv、git、ripgrep、fd、poppler が入っています。macOS と Linux：
`curl -fsSL https://raw.githubusercontent.com/Luciole-Studio/Misaka-Agent/main/scripts/install.sh | sh`。
Windows PowerShell：
`irm https://raw.githubusercontent.com/Luciole-Studio/Misaka-Agent/main/scripts/install.ps1 | iex`。
Homebrew と winget は [release/README.md](../release/README.md) を見てください。

`pip install` や `pipx install` でも同じ書き方が使えます。角かっこの中で追加機能を選びます：

| 追加機能 | 加わるもの |
|---|---|
| `providers` | すべてのモデルプロバイダの SDK。一社だけなら `anthropic`、`openai`、`google`、`bedrock` のいずれかを指定します（OpenRouter、Kimi Code や OpenAI 互換のサービスは `openai`。Mistral は SDK が要りません）。 |
| `browser` | ウェブブラウザを操作するツール |
| `lcm-semantic` | 過去の会話を意味で検索する機能 |

複数まとめて：`"misaka[providers,browser] @ git+https://..."`。

## 3. セットアップウィザードを実行する

研究用のフォルダを作り、その中でウィザードを実行します：

```sh
mkdir my-research
cd my-research
misaka setup
```

ウィザードは八つのステップで進みます。メニューでは、Enter で表示中の値のまま進み、左矢印キーで一つ前に戻り、Esc で終了します。終えた部分は保存されます。あとでもう一度実行すると、今の設定を見直しながら、選んだところだけを変更できます。

| ステップ | すること |
|---|---|
| **Environment**（環境） | 何もしなくて構いません。ステップ 1 のツールを確認します。 |
| **Model & provider**（モデルとプロバイダ） | 誰のモデルを設定するか（全員の既定か、特定のエージェントか）を選び、プロバイダを選んで、ブラウザでサインインするか API キーを貼り付けます。続けてモデルを選びます。ウィザードがごく小さなテストリクエストを送るので、使えないキーはこの時点でわかります。 |
| **Sisters** | Sister たちが使うモデルを選び、最初の専門家を作ります。`10032` をそのまま受け入れ、専門分野を短く書き、もう一人加えましょう。Sister が二人いれば互いに検証できます。 |
| **Skills**（スキル） | 何もしなくて構いません。スキルの置き場所を表示します。 |
| **Documents**（文書） | どの形式が読めるかを確認します。PDF の章立て機能がなければ、追加を提案するか、そのためのコマンドを表示します。 |
| **Web search**（ウェブ検索） | Enter を押すだけです。検索は設定なしで使えます。 |
| **Research**（研究） | 研究がどう進むかを確認します。無料の [OpenAlex](https://openalex.org/rest-api) キーを貼り付けておくと、文献スキャンが安定します（任意）。 |
| **Project**（プロジェクト） | フォルダを確認します。ここが `PROJECT.md` を持つ git リポジトリになります。手元に PDF やメモがあれば、ウィザードに索引を作らせましょう（既定の読み込み先は `sources/`）。 |

最後に、何が使えるようになったか、次に何を実行すればよいかがまとめて表示されます。

> **シェルにすでに `ANTHROPIC_API_KEY` などのキーがある場合**は、`misaka` を実行するとウィザードを飛ばしてすぐに開きます。自分で `misaka setup` を実行するか、少なくとも Sister を一人作ってください：`misaka create 10032 --desc "History and social research"`。研究には最低一人必要です。

## 4. MISAKA を開く

```sh
misaka
```

パネルが開き、コーディネーターの Last Order が自分のウィンドウで待っています。ふつうのチャットと同じように話しかけてください。

最初の日に知っておきたいこと：

- **キーの確認。** `ctrl+b` を押してから `?` で、パネルのキー一覧が出ます。チャットの入力欄では、`/hotkeys` で編集キーの一覧、`/` でコマンドの一覧が出ます。画面全体の説明は[パネルのガイド](guide/panel.md)にあります。
- **終了するとすべて止まります。** パネル（またはそのターミナルウィンドウ）を閉じると、Last Order も Sister たちも止まります。研究は進みながら保存されるので、あとで[再開](guide/research.md#following-a-run)できます。
- **ふつうのチャット。** パネルが動かない環境では、`misaka` は Last Order とのふつうのチャットを開きます。研究もそこで行えます。ほかのノードはバックグラウンドで動き、そこへのつなぎ方は[研究のガイド](guide/research.md#runs-started-from-the-shell)にあります。

## 5. 最初の問いを出す

Last Order のウィンドウで次のように入力します：

```text
/research
```

選択画面で九つの質問が出ます。研究をどこまで深く進めるか、同時にどれだけ動かすか、計画があなたの了承を待つかどうか、ほかにいくつか。すべて Enter で答えると、それぞれ最初の選択肢が選ばれ、二層までの手早い研究になります。最後の画面で確認を求められます。そのあと、ふつうのメッセージとして問いを入力します。自分の言葉で、どの言語でも構いません：

```text
清の官僚はなぜ 1905 年に科挙を廃止したのか。誰が反対したのか。
```

このあと起きること：

1. **Last Order が計画を書き**、あなたに見せます。問いが本当に何を問うているか、どの Sister がどの部分を担当するか、誰がレッドチームを務めるか。
2. **話し合います。** 修正を頼んでも、そのままでよいと伝えても構いません。あなたが了承すると始まります。
3. **Sister たちが作業します。** それぞれ Last Order の隣のペインに開くので、見ていても、席を外しても大丈夫です。
4. **結論が検証されます。** レッドチームの Sister がまず誤りを探し、次に結論が選ばなかった可能性と残した欠落を探します。Last Order は異議に一つずつ答え、欠落を埋めます。実質的に別の可能性は、それぞれ新しい研究としてタブで開き、その計画も同じようにあなたの了承を待ちます。
5. **報告が届きます。** Last Order が報告を研究論文として書き、審査を経て、届いたことを知らせます。

研究は多くのモデル呼び出しを行い、深く広く進めるほど呼び出しも増えます。まず安く試すなら、深さとノード数を小さくしてください。`/research stop` で途中で終えても、それまでにわかったことは書き出されます。

## 6. 結果を読む

すべてプロジェクトフォルダに入っています：

```text
my-research/
├── final/<run>-final.md     報告
├── final/<run>-sources/     報告が引用したすべてのファイル
└── nodes/<node>/            研究の一つひとつ：計画、Sister の成果、結論、批評
```

報告は Markdown が読めるアプリなら何でも開けます。注ではそれぞれの出典を示し、プロジェクト内にファイルがあればそれも記します。付録には、この研究がたどった筋のすべて、答えが引き受けた代償、選ばなかった道が並びます。

## 次に読むもの

| したいこと | 読むもの（英語） |
|---|---|
| 研究を導く、深さと費用を変える、再開する | [Research runs](guide/research.md) |
| Sister を増やす、スキルを与える、Claude Code や Codex を迎える | [The team](guide/team.md) |
| パネルに慣れる | [The panel](guide/panel.md) |
| ほかのモデルや、手元のマシンで動くモデルを使う | [Models](guide/models.md) |
| 手元の資料をエージェントに渡す | [Documents and the web](guide/sources.md) |
| 問題を解決する | [Troubleshooting](guide/troubleshooting.md) |

## アップデート

```sh
misaka update            # 最新版から遅れていないか
misaka update --apply    # 最新版にする
```

`--apply` はもう一度確認してから、インストールに使ったツールでアップデートし、追加機能もそのまま残します。その前に、設定、認証情報、タスクボードのコピーを `~/.misaka/state/backups/` に保存します。研究やカードが動いているとき（計画の了承待ちの研究も含む）は、いったん止まり、それらを終わらせるか止めるよう求めます。

データはバージョンごとに引き継がれますが、古いバージョンは新しいバージョンが書いたデータを読めません。古いバージョンに戻すときは、保存したコピーも一緒に戻してください。

Windows の 0.18.1 と 0.18.2 は自分でアップデートできません。MISAKA のウィンドウをすべて閉じてから、`uv tool upgrade misaka` を一度実行してください。0.18.3 からは Windows でも `misaka update --apply` が使えます。

## アンインストール

```sh
misaka uninstall --dry-run   # 何が削除されるかを表示
misaka uninstall             # 何を削除するかを尋ねる
```

既定ではデータを残します。プログラムだけを削除し、`~/.misaka/` は残るので、再インストールすれば続きから使えます。`--full` はデータも削除し（その前にバックアップを勧めます）、`--data` はデータだけを削除します。先にパネルを閉じてください。プロジェクトフォルダには報告、資料、設定が残ります。`--full` が削除するのは、その中の MISAKA のキャッシュだけです。

Windows では、ほかの MISAKA のウィンドウもすべて閉じてください。Windows は実行中のプログラムを削除できないためです。プログラムはこのコマンドが終わるとすぐに削除されます。

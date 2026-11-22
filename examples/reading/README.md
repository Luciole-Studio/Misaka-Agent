# Reading example: two texts on leaving home

Two public-domain texts and one question, used by the [reading guide](../../docs/guide/reading.md)
and by `scripts/run_reading_demo.sh` (`.ps1` on Windows).

| Text | Language | Where it comes from |
|---|---|---|
| 鲁迅《娜拉走后怎样》 | Chinese | [Wikisource](https://zh.wikisource.org/wiki/%E5%A8%9C%E6%8B%89%E8%B5%B0%E5%BE%8C%E6%80%8E%E6%A8%A3) — a 1923 lecture on what happens to Nora after she leaves |
| Henrik Ibsen, *A Doll's House* | English | [Project Gutenberg #2542](https://www.gutenberg.org/files/2542/2542-0.txt) |

`sources.json` records the provenance, the links, the language, the reading question and the
licence note. It is a catalogue, not a copy of the texts: nothing here downloads at checkout, and
no material is committed. `misaka read open` downloads a URL once, into that run's own
`sources/` folder inside your project, and indexes it there.

The Wikisource page title is in traditional characters (`娜拉走後怎樣`); the percent-encoded URL
above is that title, and the site can display it in simplified characters.

Run the whole chain from the repository root:

```sh
scripts/run_reading_demo.sh path/to/a/new/folder
```

or on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_reading_demo.ps1 path\to\a\new\folder
```

The folder is where the run is written (`reading/<run-id>/` inside it). Use a scratch folder; the
downloads, the graph and the node bodies are yours, not the repository's.

## Licences

- 鲁迅 died in 1936; the lecture is in the public domain. The Wikisource page is a transcription
  offered under CC BY-SA 4.0 — keep the link when you reuse the page.
- Project Gutenberg texts are public domain in the United States. Outside it, the file's own
  header and licence apply; keep them with the file.

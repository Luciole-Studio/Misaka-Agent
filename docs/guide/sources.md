# Documents and the web

Agents work from two kinds of material: the documents you index into a project, and what they
find on the web. Both end up as files in the project, so a conclusion can cite them.

## Your documents

Each project keeps its own document index in `<project>/.pageindex/`. Files must be inside the
project folder to be indexed.

```sh
misaka doc scan sources/            # index every readable file in a folder
misaka doc add sources/report.pdf   # index one file
misaka doc list                     # indexed documents and their IDs
misaka doc find "exact words"       # where a phrase occurs, by document and page
misaka doc verify "a quotation" --doc DOC_ID
misaka doc tree DOC_ID              # a document's outline
```

What can be indexed: PDF, DjVu, EPUB, HTML, Word, Excel and PowerPoint (`.docx`, `.xlsx`,
`.pptx`), CSV, Markdown, plain text, BibTeX and TeX. Old `.doc`, `.xls` and `.ppt` files are read
through LibreOffice when you name them one by one; a folder scan passes over them, and over data
files such as JSON and logs.

- **Outlines.** With the `pageindex` extra installed, long PDFs get an outline (chapters and
  sections with their page ranges), so an agent can read a chapter instead of guessing at page
  numbers. Without it, documents are navigated by page. `--no-tree` skips the outline.
- **Scanned PDFs.** A PDF with no text layer is read by OCR when `ocrmypdf` is on your PATH
  (`brew install ocrmypdf` or your package manager). `documents.ocr_langs` in `settings.json`
  sets the languages (default `eng+chi_sim+jpn`).

Agents use the same index through their tools: `doc_list`, `doc_outline`, `doc_read` (by outline
node or page range), `doc_find` (literal text), `doc_page_image` (a PDF page as an image, for
figures, tables, maps and scans) and `doc_add`. `doc_verify` finds the page and character offset
where a quotation occurs and returns a hash for it. It tells you where the words are, not whether
they support the claim: that judgement stays with the red team and Last Order. On an OCR page, a
match means the quotation matches what OCR read.

Files that agents download or produce during a card are indexed as they arrive.

## Office files

Agents read `.docx`, `.xlsx` and `.pptx` with the `read` tool and create or edit them with the
`office` tool, so a deliverable can be a Word report, a spreadsheet or a slide deck as well as
Markdown.

## The web

| Tool | What it does |
|---|---|
| `web_search` | searches the web, passing operators such as `site:` and `filetype:` to backends that support them |
| `web_extract` | extracts the content of up to five pages through the configured extraction service |
| `web_fetch` | reads one public page and returns its text |
| `download_file` | saves a file (a PDF, a dataset) into the project's downloads folder |
| `x_search` | searches public posts on X through xAI, when you have set it up |
| `browser_*` | drive a browser, with the `browser` extra and a configured browser |

Search works with no configuration. Until you set up a search service, and whenever a configured
one fails, MISAKA uses the free public tiers of Exa, Parallel, Firecrawl and Keenable in turn.
`misaka web set keyless_fallback false` turns that off.

`misaka web` opens an interactive menu (`misaka setup web` is the same menu):

```sh
misaka web                                        # the menu in a terminal; status when piped
misaka web status                                 # what is configured and ready, without network calls
misaka web --profile ~/.misaka/profiles/sisters/10032   # one Sister's own overrides
```

The menu covers the search and extraction services, free, paid or automatic tiers, turning
providers on and off, credentials, browser connections, proxies, the cache, blocklists, timeouts
and X search. Opening it sends no requests and writes nothing; login, installs and online checks
happen only when you choose them. Each confirmed change is saved at once, so cancelling later
does not undo it.

Where the settings go:

- Vendor keys are written to `~/.misaka/.env` (owner-only); the rest goes into the `web` section
  of `settings.json`.
- A Sister's overrides go into her own profile. They never copy shared secrets, and removing an
  override brings the shared value back.
- A variable exported in your shell wins over both files, even when it is exported empty.
- Automatic routing prefers the credentials you already have; it does not mean free-only.

For scripts, `misaka web setup PROVIDER`, `set KEY VALUE`, `unset KEY`, `providers`, `enable` and
`disable` do the same things without the menu; `misaka web --help` lists them all.

The web tools refuse private and loopback addresses unless `web.allow_private_urls` is true;
cloud metadata endpoints stay blocked either way.

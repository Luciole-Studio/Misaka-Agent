# Documents and the web

Agents work from three kinds of material: the documents you index into a project, what they find
on the web, and scans of the scholarly literature. All of it ends up as files in the project, so
a conclusion can cite it and you can check it.

## Your documents

Each project keeps its own document index, in a `.pageindex/` folder inside the project. Run
`misaka doc` from the project folder itself, where the agents look for the index; the files you
index must be inside the project too.

```sh
misaka doc scan sources/            # index every readable file in a folder
misaka doc add sources/report.pdf   # index one file
misaka doc list                     # indexed documents and their IDs
misaka doc find "exact words"       # where a phrase occurs (the first 10 matches)
misaka doc verify "a quotation" --doc DOC_ID
misaka doc tree DOC_ID              # a document's outline
```

**What can be indexed.** A folder scan collects PDF, DjVu, EPUB, HTML, Word, Excel and PowerPoint
(`.docx`, `.xlsx`, `.pptx`, and their macro-enabled forms), CSV and TSV, Markdown, plain text,
reStructuredText, BibTeX and TeX. Named one by one, MISAKA also reads old `.doc`, `.xls` and
`.ppt` files (through LibreOffice), JSON, YAML and log files; a folder scan passes over those.

What reading them needs:

| Files | Needs |
|---|---|
| PDF | `pdftotext` from poppler (`brew install poppler`); a built-in reader is the fallback |
| scanned PDF | MinerU (`uv tool install mineru`), which fetches its models (about 1.4 GB) on first use, with `tesseract` to stand sideways pages up; or `ocrmypdf` with the language data: `brew install ocrmypdf tesseract-lang`, or `ocrmypdf` and `tesseract-ocr` with your language packs on Linux |
| DjVu | DjVuLibre (`djvused`, and `ddjvu` for pages that need OCR); scanned DjVu goes through OCR as well |
| old `.doc`, `.xls`, `.ppt` | LibreOffice |

- **Scanned PDFs.** Each page is checked on its own, and a page goes through OCR when it has no
  text of its own (a download stamp or a running head repeated on most pages does not count), when
  its text is garbled (a broken font encoding), or when it is mostly a picture with little text on
  it. A page that has a text layer keeps it, and what OCR reads on it is added after it, so a
  scanned facsimile under a typeset heading loses neither. Pages that needed OCR and did not get
  it -- no OCR program, or a failed run -- are marked unread: `doc_list` counts them, and
  `doc_read` names each one with the reason. Pages OCR read and found no text on (blank scans,
  pictures, maps) are named by `doc_read` too, but not counted as unread. A document is turned
  away only when, after OCR, fewer than a fifth of its pages carry text.
- **Which program reads a scan.** MinerU reads where it is installed, `ocrmypdf` otherwise;
  `documents.ocr_engine` in `settings.json` names one instead. MinerU reads vertical Chinese and
  Japanese, which `ocrmypdf` mostly cannot, and needs no language named. It runs on this machine,
  25 pages at a time, so a failed run costs only those pages. Where `tesseract` is installed,
  pages lying on their side are stood up first, which handwriting needs; a page that cannot be
  judged is read as it lies, and an upright page is left alone. It reads at its strongest by
  default, handwriting included, at about 12 to 19 seconds a page on an Apple M4, and a document
  is ready when all of it is read: a 400-page scan takes one to two hours. `documents.mineru_tier`
  set to `basic` takes about a tenth of the time, reads print almost as well and handwriting and
  old Japanese much worse. For `ocrmypdf`, `documents.ocr_langs` sets the languages (default
  `eng+chi_sim+jpn`); each needs its language data installed, or OCR fails and the message says
  why. Changing these reads new documents that way; a document already indexed keeps its text.
- **An outline for a scan.** A scanned PDF states no outline: it has no bookmarks and no text
  layer to lay out, so `doc_outline` had only its pages. Where MinerU read it, the headings it
  marks on the pages stand in, read in a pass of their own. Such an outline is sparser than a
  publisher's and not always right -- against the bookmarks of three scanned books it found 2, 7
  and 7 of 23, 25 and 48, and on one of them about half of what it offered was a title page's
  lines or a running head. A PDF that states an outline of its own keeps it.

- **Documents indexed by an older version.** Running `misaka doc add` or `scan` again on a PDF or
  DjVu indexed before per-page OCR re-reads it when its stored pages show it may need it: pages
  that printed something the old version did not read get OCR, and a document whose pages were
  numbered out of step with the file is renumbered. An EPUB with pictures whose text has no
  `![...]` markers is read again too, so its pictures are named. Only the pages whose text changed are
  rewritten, and the command lists how many; `doc_read` and `doc_verify` say so on those pages,
  because a quotation located there earlier may have pointed at different text.
- **Outlines.** Long PDFs get an outline (chapters and
  sections with their page ranges), so an agent can go straight to a chapter. Plain-text and Markdown documents get theirs from their own headings (`CHAPTER XII`,
  `LIVRE III`, a title set in capitals, `#` in Markdown), with no extra needed. Outlines are made
  for documents of 20 pages or more. Running `misaka doc add` or `scan` again on a document
  indexed earlier fills its outline in. `--no-tree` skips the outline.
- **Figures.** A map, a chart or a plate is not in a page's text, so `doc_read` names the figures
  on the pages it returns: an image on a PDF page (covering more than 8% of it, and wider than a
  logo), a drawing of more than 100 strokes (a chart, a map or a ruled table), and, in a scanned
  book, a page with far less text than the book's pages usually carry. `doc_outline` lists the
  pages that have them. `doc_page_image(doc_id, page, figure=N)` shows one figure alone, at the
  resolution its labels need; without `figure` it shows the whole page, of a PDF or a DjVu.
  In a deck, the slides whose meaning is in a picture or a drawing are named, and
  `doc_page_image(doc_id, page)` shows the slide (LibreOffice renders it; hidden slides have no
  image). A Word document or an EPUB marks each picture where it stands, `![alt]`, and
  `doc_page_image(doc_id, page, figure=N)` shows the N-th picture on that page as the file
  stores it (a Word chart pasted as EMF or WMF needs LibreOffice). A
  model without vision cannot be shown an image at all, so for it the page or the figure is read
  in words by the team's vision model (`vision.model`, see [Models](models.md)).
  A value read off a figure is a reading, not a quotation: `doc_verify` cannot locate it.
- **Pages.** In a PDF or DjVu file a page is a page of the file, counted from 1 -- not always the
  number printed on it (front matter in Roman numerals, a scanned cover, an article whose journal
  pagination starts at 1361). Where the printed number is known, `doc_read`, `doc_find` and
  `doc_verify` show it beside the file's page (`p351 (printed p. 338)`): from the PDF's own page
  labels, or else read off the page heads and feet, but only where many pages in a row agree on
  it. The file's page stays the locator a citation records. EPUB, HTML, text and Office files
  have none, so MISAKA cuts them into pages of about 3,000 characters at paragraph breaks, and
  a citation such as `p12` points into that cut.

Agents use the same index through their tools: `doc_list`, `doc_outline`, `doc_read` (by outline
node or page range), `doc_find` (literal text), `doc_page_image` (a page of a PDF, a DjVu or a deck,
or one figure or embedded picture, as an image, for figures, tables, maps, slides and scans) and `doc_add`. `doc_verify` finds the page and character offset
where a quotation occurs and returns a hash for it; given the page it is cited on, it says when
the quotation is on another page instead. It shows where the words are; whether they
support the claim is for the red team and Last Order to judge. On an OCR page, a
match means the quotation matches what OCR read. In a research run, every quotation a finding
cites from a document is looked for on its page whenever Last Order, the red team or the
bundle reads it: one on another page, or not in the indexed text at all, is marked there (and
listed in `SOURCES.md` under "Quotations not where they are cited"), never refused.

What joins the index by itself:

- a file an agent downloads with `download_file`, when it is of a kind a folder scan collects;
- a card's deliverables and the files it cites, once the card is accepted as done;
- a research node's texts when the node closes.

From a card's folder that means what stands directly in it or under `sources/` and `reports/`,
and PDF, DjVu, EPUB and Office files anywhere in it. Other text in its subfolders stays out; index
one such file with `doc_add` if it should be found.

Pages saved by the web tools (under `downloads/pages/`) stay files and are not indexed.

## Office files

Agents read `.docx`, `.xlsx`, `.pptx`, CSV and TSV with the `read` tool (a spreadsheet by cell
range too), and old `.doc`, `.xls` and `.ppt` through LibreOffice. The `office` tool creates and
edits Word, Excel and PowerPoint files, as well as text, Markdown, CSV, JSON and HTML, so a
deliverable can be a Word report, a spreadsheet or a slide deck as well as Markdown.

## The web

| Tool | What it does |
|---|---|
| `web_search` | searches the web, passing operators such as `site:` and `filetype:` to services that support them |
| `web_extract` | extracts the content of up to five pages through an extraction service |
| `web_fetch` | reads one public page and returns its text; a link to an arXiv or PubMed paper goes to its full readable version |
| `download_file` | saves a file (a PDF, a dataset) of up to 64 MB into the project's `downloads/` folder |
| `x_search` | searches public posts on X through xAI, when you have set it up |
| `browser_*` | drive a web browser, when a browser is set up (below) |

Every page the web tools read is saved under `downloads/pages/`, with the address it came from,
so a conclusion can cite it.

### Search works with no setup

Until you set up a search service, MISAKA uses the free public tiers of Exa, Parallel, Firecrawl
and Keenable. Each request goes to the next one in turn and moves on to another when one is
rate-limited. The same free tiers extract pages. When a service you did set up fails, that one
request is tried once on the free tiers.

- `misaka web set keyless_rescue false` stops that second try.
- `misaka web set keyless_fallback false` turns the free tiers off altogether. With no service set
  up, agents then have no `web_search` or `web_extract` at all.

The services built in are Brave (free), DuckDuckGo (`ddgs`), Exa, Firecrawl, Keenable, Nous,
Parallel, Perplexity, SearXNG, Tavily and xAI; several of them also extract pages.

### Setting it up

`misaka web` opens an interactive menu (`misaka setup web` is the same menu):

```sh
misaka web                                        # the menu in a terminal; status when piped
misaka web status                                 # what is configured and ready, without network calls
misaka web --profile ~/.misaka/profiles/sisters/10032   # one Sister's own overrides
```

The menu covers the search and extraction services, free, paid or automatic tiers, turning
services on and off, credentials, browsers, proxies, the cache, the website blocklist, timeouts
and X search. Logins, installs and online checks happen when you choose them, and each change is
saved as soon as you confirm it.

Where the settings go:

- Credentials (API keys, tokens, passwords) are written to `~/.misaka/.env`, readable only by
  you. Everything else, including endpoints, proxies and certificate bundles, goes into the `web`
  section of `settings.json`.
- A Sister's overrides go into her own profile; removing one brings the shared value back.
- A variable exported in your shell wins over both files, even when it is exported empty.
- Automatic routing uses the services you have credentials for first.

For scripts, `misaka web setup PROVIDER`, `set KEY VALUE`, `unset KEY`, `providers`, `enable` and
`disable` do the same things without the menu; `misaka web --help` lists them all.

### Browsers

The `browser_*` tools need the `browser` extra and a browser route. The local, remote-debugging
and cloud routes also need the `agent-browser` command-line tool, which `misaka web
browser-install` (or the menu's installs) puts in place; the Camofox route needs neither.

### Safety and caching

- The web tools refuse private and loopback addresses unless `web.allow_private_urls` is true;
  cloud metadata addresses stay blocked either way.
- A URL that carries an API key or token is refused.
- `web.website_blocklist` (off by default) keeps agents off the domains you list, for
  `web_fetch`, `web_extract` and `download_file`.
- Searches and extracted pages are cached for 20 minutes (`cache_ttl_minutes`; `cache_enabled`
  turns it off).

## The literature: coverage scans

Before dividing a question into cards, Last Order (and the red team, when looking for what a
conclusion missed) can check where the question sits in the scholarly literature:

- **`coverage_scan`** cuts the question into facets and counts, in
  [OpenAlex](https://openalex.org)'s titles and abstracts, how the literature on each facet, on
  all of them together and on each pair spreads across fields, topics, languages and kinds of
  publication. The counts point to where to look; whether a field matters is for the research
  to judge.
- **The `coverage-maps` skill** is a set of maps of the dimensions a field usually covers, to
  check a plan against.

Both come with MISAKA. The scans send the question's search terms to OpenAlex. OpenAlex limits
anonymous use, so a free key keeps scans reliable: paste it in the setup wizard's research step,
or put `OPENALEX_API_KEY=...` in `~/.misaka/.env`.

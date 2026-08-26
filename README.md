# MISAKA

A multi-agent research system for the humanities and social sciences. Last Order (the coordinator) breaks a research question into task cards; Sisters (worker agents) execute them in their own processes and hand back `report.json`; accepted results are committed to the project's git repository and indexed into a document corpus.

## Install

```sh
uv venv .venv --python 3.13
uv sync --group dev            # product + test tooling
uv sync --extra pageindex      # PDF outline extraction (pypdfium2, PyPDF2, ...)
```

## Run

```sh
misaka                 # panel in a terminal, plain chat when piped
misaka chat            # talk to Last Order
misaka research "..."  # start a research run
misaka board           # the task board
misaka doc add x.pdf   # index a document
```

Configuration lives in `~/.misaka/` (`agent/models.json` for providers, `profiles/` for Sisters). `MISAKA_*` environment variables override defaults; see `misaka/config/product.py`.

## Check

```sh
make check             # tests, -W error, compileall, import sweep, wheel build
```

`misaka/documents/pageindex/flash` is vendored from [PageIndex](https://github.com/VectifyAI/PageIndex) (MIT); see `UPSTREAM.md` there.

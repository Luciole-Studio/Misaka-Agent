# PageIndex source slice

- Upstream: <https://github.com/VectifyAI/PageIndex>
- Commit: `d5c4e62c20172ce400aef84545dfba3a0580b9ae`
- License: MIT (`LICENSE.PageIndex`)

`flash/` is the deterministic PageIndex Flash PDF-outline pipeline. MISAKA keeps
the layout parser and bundled lookup data, replaces the public adapter, and omits
the upstream CLI, LLM summaries/optimization, examples, and generated results.

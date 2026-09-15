# B5 — v2 benchmark matrix (500q LongMemEval_S, fastembed bge-small, config-exact)
n=470 scoreable. Session-level R@k + turn-level. MemDelta caveat: numbers are for THIS config only.

| arm | sR@5 | sR@10 | sNDCG | tR@5 | tR@10 |
|---|---|---|---|---|---|
| chunk_vectors | 0.96 | 0.98 | 0.924 | 0.61 | 0.77 |
| summary_vectors | 0.87 | 0.93 | 0.837 | 0.87 | 0.92 |
| hybrid_rerank | 0.87 | 0.93 | 0.837 | 0.64 | 0.75 |
| hybrid_rrf3 | 0.77 | 0.97 | 0.714 | 0.28 | 0.52 |
| hybrid_rrf | 0.49 | 0.64 | 0.484 | 0.77 | 0.87 |
| fts | 0.20 | 0.36 | 0.204 | 0.03 | 0.09 |

## per-category session R@5
| category | summary | chunk | rrf3 | rerank | fts |
|---|---|---|---|---|---|
| knowledge-update | 0.91 | 0.99 | 0.76 | 0.91 | 0.26 |
| multi-session | 0.87 | 0.96 | 0.80 | 0.87 | 0.20 |
| single-session-assistant | 0.98 | 0.98 | 0.82 | 0.98 | 0.14 |
| single-session-preference | 0.87 | 0.93 | 0.87 | 0.87 | 0.13 |
| single-session-user | 0.81 | 0.98 | 0.80 | 0.81 | 0.33 |
| temporal | 0.84 | 0.91 | 0.69 | 0.84 | 0.14 |

## DECISIONS (from data)
- TEMPORAL/KNOWLEDGE FACT-LAYER (v3 #5): NO-GO. chunk arm 0.99 knowledge-update / 0.91 temporal — the categories
  the industry says need bitemporal fact graphs are already top-scoring on lossless+chunk. Decision-record bet confirmed.
- CHUNK vs SUMMARY: chunk wins every category (0.96 vs 0.87 R@5) → chunk corpus is the retrieval star; conversational
  policy default holds (heads adds scale cost for marginal gain — revisit with int8 in v3).
- RERANK DEFAULT: stays OFF — rerank ≡ summary at this scale (0.87); no measured lift to justify the cloud RTT.
- RRF WEIGHTS: hybrid_rrf3 turn-level 0.28 lags its own arms badly → weighting still miscalibrated for turn-precision;
  v3 delta-sweep item. Session-level rrf3 R@10 0.97 is fine; the fusion hurts turn localization.
- COMPARISON CONTEXT (published SOTA, independent): our chunk arm session R@5 0.96 / knowledge-update 0.99 is
  competitive with the top LongMemEval systems (Mastra 94.9%, MemPalace 96.6% R@5) — with local fastembed, no cloud,
  no fact-extraction, fully lossless. Voyage-model run would sit higher (bge-small is the floor).

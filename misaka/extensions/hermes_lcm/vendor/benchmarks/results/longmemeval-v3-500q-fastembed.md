# v3 confirmation (500q LongMemEval_S, fastembed bge-small, config-exact)
n=470 scoreable. Session-level R@k + turn-level. MemDelta caveat: numbers are for THIS config only.

Session-level metrics are byte-identical to the v2 500q matrix (`longmemeval-v2-500q-fastembed.md`)
— v3's storage options (int8/Matryoshka/binary-prescreen) are opt-in and don't touch the
float32 path this harness runs. Turn-level numbers for the hybrid arms (`hybrid_rrf`,
`hybrid_rerank`, `hybrid_rrf3`) differ from the v2 file: a harness measurement fix
(projecting turn keys from the fused SESSION ranking instead of RRF-fusing raw
per-arm turn keys, which diluted the turn budget with precise-but-irrelevant keys
from low-ranked non-evidence sessions) corrected a scoring bug in the harness itself,
not a retrieval regression. `hybrid_rrf3`'s v2-reported tR@5 0.28 was the clearest
symptom of that bug; it is now tR@5 0.78 / tR@10 0.97. `chunk_vectors`,
`summary_vectors`, and `fts` already scored at native/session granularity and are
unaffected — their numbers are identical to v2.

| arm | sR@5 | sR@10 | sNDCG | tR@5 | tR@10 |
|---|---|---|---|---|---|
| chunk_vectors | 0.96 | 0.98 | 0.924 | 0.61 | 0.77 |
| summary_vectors | 0.87 | 0.93 | 0.837 | 0.87 | 0.92 |
| hybrid_rerank | 0.87 | 0.93 | 0.837 | 0.87 | 0.92 |
| hybrid_rrf3 | 0.77 | 0.97 | 0.714 | 0.78 | 0.97 |
| hybrid_rrf | 0.49 | 0.64 | 0.484 | 0.49 | 0.65 |
| fts | 0.20 | 0.36 | 0.204 | 0.03 | 0.09 |

## per-category session R@5
Unchanged from v2 (session-level path is untouched by v3).

| category | summary | chunk | rrf3 | rerank | fts |
|---|---|---|---|---|---|
| knowledge-update | 0.91 | 0.99 | 0.76 | 0.91 | 0.26 |
| multi-session | 0.87 | 0.96 | 0.80 | 0.87 | 0.20 |
| single-session-assistant | 0.98 | 0.98 | 0.82 | 0.98 | 0.14 |
| single-session-preference | 0.87 | 0.93 | 0.87 | 0.87 | 0.13 |
| single-session-user | 0.81 | 0.98 | 0.80 | 0.81 | 0.33 |
| temporal | 0.84 | 0.91 | 0.69 | 0.84 | 0.14 |

## Production arm: lcm_recall (50q fastembed)
A 50-question subset scoring the actual `tools.lcm_recall` tool end-to-end (weighted
RRF over FTS/summary/chunk, the scope/recency prior, chunk-vs-FTS dedup, `include`
filtering) rather than a harness-reimplemented arm:

R@1 0.42, R@5 0.98, R@10 1.00.

R@1 is lower than `chunk_vectors`' own R@1 because of two production-path costs the
per-arm numbers don't carry: the always-on recency prior (boosts newer hits
regardless of relevance) and FTS-arm noise entering the fused ranking at rank 1 on
some questions. R@5/R@10 recover once the fused window widens past those noisy
top-1 picks.

## DECISIONS (from data)
- HARNESS FIX CONFIRMED: v2's `hybrid_rrf3` turn-level 0.28 was a harness measurement
  artifact (turn-key RRF-fusion diluting the turn budget with non-evidence-session
  keys), not a retrieval regression — corrected to 0.78/0.97 by projecting turn keys
  from the fused session ranking. Session-level numbers for every arm are unchanged
  by the fix.
- SCALE (v3 storage options): session-level retrieval quality on the local fastembed
  floor is unchanged by the int8/Matryoshka/binary-prescreen storage options — those
  are additive and default-off, and this 500q harness's per-question temp stores are
  far smaller than the real-archive scale they target. See the operator guide's
  [vector storage scale options](../../docs/operator-guide.md#vector-storage-scale-options-v3)
  for the C1 real-data bench (92,997-chunk archive) these options were built for.
- PRODUCTION ARM: `lcm_recall`'s 50q result (R@5 0.98, R@10 1.00) tracks
  `chunk_vectors`' session-level strength; R@1 0.42 is the recency-prior/FTS-noise
  cost of scoring the real tool end-to-end rather than a bare vector arm.

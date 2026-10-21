# packages/retrieval: hybrid retrieval

pgvector cosine + Postgres full-text, fused with reciprocal rank fusion, then a cross-encoder
reranker. Owner: retrieval agent. Implements the `search_evidence` and `search_video` tools to the
frozen contracts in `packages/agent/caselens_agent/schemas/tools/`.

## API

```python
from caselens_retrieval import EvidenceFilters, embed_query, search_evidence, search_video

out = search_evidence(
    conn,
    case_id,
    "gray sedan leaves with headlights off",
    EvidenceFilters(modalities=["pdf", "audio"]),
    top_k=8,
)
out.model_dump(mode="json")  # validates against the tool's output_schema
```

- `conn` is a psycopg connection already running as `caselens_app` with `app.user_id` set, so RLS
  applies on top of the explicit, bound `case_id = %s`. The case id comes from the runtime, never
  from tool input.
- Models: `EvidenceFilters`, `SearchEvidenceInput`, `SearchVideoInput`, `SearchEvidenceOutput`,
  `SearchVideoOutput`, the four locator models (`PdfLocator`, `AudioLocator`, `VideoLocator`,
  `RecordLocator`). Locators round-trip verbatim from `chunks.locator`: optional keys absent in
  the stored JSON stay absent. Filter times must carry a UTC offset (naive times are rejected
  instead of silently shifted).
- `embed_query(text)` gives the 768-dim BAAI/bge-base-en-v1.5 vector (same model as ingest).
- `caselens_retrieval.encoders.warm_up()` loads the embedder and reranker up front. A cold process
  otherwise pays about 7 s on its first search; call it at server start-up.
- Fakes for tests: pass `embedder=` / `reranker=` per call, or `set_default_embedder` /
  `set_default_reranker` process-wide. `rerank=False` returns RRF scores; `timings=SearchTimings()`
  is filled with embed / SQL / rerank milliseconds.

## Pipeline

1. Embed the query (no instruction prefix, see below).
2. Two candidate queries, each with all filters applied inside SQL (40 each):
   - `embedding <=> %s::vector` on the HNSW index, with `hnsw.ef_search = 100` and
     `hnsw.iterative_scan = relaxed_order` (pgvector 0.8) so filters cannot starve the result;
     rows are re-sorted by exact distance.
   - `tsv @@ websearch_to_tsquery('english', %s)` ranked by `ts_rank_cd`. The question's terms
     are ORed (`gray or sedan or march or 14`): bare websearch syntax ANDs them and found almost
     nothing for natural questions (recall@8 0.57 vs 0.82 with rerank).
3. Reciprocal rank fusion, `1 / (60 + rank)`, ties broken by best single rank then id.
4. Cross-encoder rerank of the fused top 30 (fastembed `TextCrossEncoder`, 2 threads, loaded once
   per process). Long chunks (whole PDF pages) are scored on their best-matching 48-word window,
   chosen by query-term overlap: the cross-encoder truncates at 512 tokens and its cost grows with
   length. Final `score` = reranker logit (RRF score when reranking is off).

Filters: `modalities`, `evidence_ids`; `entity_ids` keeps chunks with a mention of those
entities or of any entity joined to them by active `entity_links` (both directions, transitive);
`time_from` / `time_to` keep timed chunks whose `[t_start, t_end]` overlaps the window and
untimed chunks (PDF pages) only when an event in the window cites them via `source_chunk_ids`.

`search_video` is the same pipeline on `modality = video` (track summaries and scene captions),
with the time window as a hard filter. `track_id`, `start_ms`, `end_ms` and keyframes
(`frame_idx`, `bbox`) come from the locator; wall-clock times from `t_start` / `t_end`.
`image_uri` is always null for now: ingest does not write keyframe images to object storage yet.

## Configuration (CASELENS_* env)

| Variable | Default | Meaning |
| --- | --- | --- |
| `CASELENS_RERANK_MODEL` | `Xenova/ms-marco-MiniLM-L-6-v2` | fastembed cross-encoder, or `none` to disable |
| `CASELENS_EMBEDDING_MODEL` | `BAAI/bge-base-en-v1.5` | must match ingest |
| `CASELENS_QUERY_INSTRUCTION` | empty | prefix prepended to queries before embedding |
| `CASELENS_MODEL_CACHE_DIR` | `.cache/models` | model weights |
| `CASELENS_MODEL_THREADS` | `2` | ONNX threads per model |
| `CASELENS_RETRIEVAL_CANDIDATES` | `40` | per-list candidates before fusion |
| `CASELENS_RERANK_CANDIDATES` | `30` | fused candidates sent to the reranker |
| `CASELENS_RERANK_WINDOW_WORDS` | `48` | rerank window for long chunks (0 = full text) |
| `CASELENS_RERANK_BATCH_SIZE` | `1` | cross-encoder batch (1 is fastest on CPU: no padding) |

## Quality on the demo case

`tests/test_devdb_quality.py` (opt-in: `CASELENS_HEAVY=1`, dev DB with the ingested SYNTHETIC
demo case, read-only) runs 17 story questions whose gold targets are answer-key locators (same
file, overlapping bbox / time span, same row), plus 5 `search_video` questions. Run it as a script
to compare configurations. Measured on a CPU laptop, 2 threads:

| Config | recall@8 | MRR | CSV share of top 8 | rerank p50 |
| --- | --- | --- | --- | --- |
| RRF only | 0.61 | 0.45 | 0.25 | - |
| RRF only, bge query instruction | 0.71 | 0.66 | 0.27 | - |
| MiniLM-L-6 rerank, 48-word windows (default) | **0.82** | **0.81** | 0.19 | 0.74 s |
| MiniLM-L-6, bge query instruction | 0.74 | 0.76 | 0.24 | 0.80 s |
| MiniLM-L-6, 80-word windows | 0.81 | 0.76 | 0.21 | 0.97 s |
| MiniLM-L-6, full chunks, batch 64 | 0.77 | 0.69 | 0.24 | 3.6 s |
| MiniLM-L-12, 48-word windows | 0.82 | 0.81 | 0.21 | 1.5 s |
| bge-reranker-base, 48-word windows | 0.81 | 0.72 | 0.24 | 5.3 s |

The bge query instruction helps RRF alone but hurts after reranking, so it is off.
bge-reranker-base (named in the guide) is no better here and 7x slower, so MiniLM-L-6 is the
default. `search_video` hit@8 = 1.0, MRR 0.90. Embedding takes ~30 ms and the two SQL queries
~175 ms; most of the SQL time is the per-row RLS membership check on a sequential scan.
CSV rows (840 of 1014 chunks) take about a fifth of the top 8 and do not crowd out narrative
evidence once reranked.

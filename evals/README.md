# evals: eval harness

`make eval` (= `python -m caselens_evals`) runs the 10-question smoke eval; `make eval-full` runs all
50 gold questions and writes the results table into the root README. Owner: eval agent.

## Answer-quality harness

```
python -m caselens_evals run   [--smoke] [--ids q01,q16] [--category conflict] [--limit N]
                               [--model plan=<m>,synthesize=<m>] [--out DIR | --resume] [--retry-failed]
python -m caselens_evals score [DIR] [--no-judge] [--judge-model <m>]
python -m caselens_evals report [DIR ...] [--compare DIR DIR ...] [--readme]
python -m caselens_evals smoke | full
```

* `run` (`runner.py`): each gold question goes through `caselens_agent.runner.ask` in-process on the
  demo case as the seed lead (`caselens_app` + `caselens_readonly` connections, RLS applies, normal
  agent_runs + audit rows). Raw results go to `evals/results/<UTC ts>/runs.jsonl`: answer, tool
  calls, latency, model, cost, every `query_records` result as executed, and the text of every
  cited source (chunk text by chunk_id, record row by record_id). The file is appended per run, so
  a run cut off by the free-tier quota resumes where it stopped (`--out DIR` or `--resume`); it
  stops by itself after two model failures in a row. `--smoke` is the fixed subset: the first two
  items (by id) of each category. `--model` sets router overrides (`provider=` too).
  It then measures retrieval recall@10 (`retrieval_eval.py`, no LLM): `search_evidence(question,
  top_k=10)`, plus `search_video` when a required citation is video, per answered item.
* `score` (`judge.py`, `metrics.py`): one judge call per answer (`ModelRouter.complete_json(task=
  "judge")`, strict Pydantic output, cited source texts in `evidence_block`s) for correctness
  (correct 1 / partial 0.5 / wrong 0), per-claim citation support and, for conflict items, whether
  the planted conflict was surfaced. Refusals with no claims need no call. Verdicts are cached by
  run id in `judge.jsonl`, so re-scoring is free. Writes `scores.json` and `report.md`.
  **Exit code 1 when citation precision < 95%** (the CI gate).
* `report`: prints `report.md`; `--readme` replaces the block between
  `<!-- eval-results:start -->` / `<!-- eval-results:end -->` in the root README; several dirs add a
  model comparison table.
* `smoke` (also the bare command) / `full`: run + score (`full` also updates the README). Without a
  gold set, `CASELENS_DATABASE_URL`, an ingested demo case or a model key, the smoke command
  validates the gold set, prints `skipped` and exits 0 (CI has no DB or key);
  `CASELENS_EVAL_REQUIRE_LIVE=1` turns that into exit 2.

| Metric | Target | How it is measured here |
| --- | --- | --- |
| Answer correctness | >= 85% | judge vs `expected_answer` + `key_facts` |
| Citation precision | >= 95% | supported (claim, citation) pairs / all pairs; pairs the judge skips count as unsupported |
| Citation recall | >= 80% | required gold citations matched by any run citation (`gold.match_citation`) |
| Hallucination rate | <= 2% | claims with no supporting citation / all claims |
| Unanswerable refusal | 100% | answer status `insufficient_evidence` |
| Conflict detection | >= 80% | `answer.conflicts` non-empty AND the judge says it is the planted conflict |
| Retrieval recall@10 | >= 90% per modality | required citations hit in the top 10, before the agent; passes when every modality does |
| SQL accuracy | >= 90% | a `query_records` call with the gold operation whose executed result matches `sql.expected` (row_count, rows compared as values / instants) |
| p50 / p95 latency | < 8s / < 20s | `latency_ms` of the runs |
| Cost per question | < $0.05 | `agent_runs.cost_usd`; null is reported as "n/a (free tier)" |

## Component evals

`python -m caselens_evals.components [--only ocr,asr,video,er]` scores each ingest component of
the demo case against data_gen ground truth. It makes no LLM calls. It reads the dev DB as
`caselens_worker` on a read-only session (`CASELENS_DATABASE_URL`) and the stage artifacts in
MinIO (`CASELENS_S3_*`; without keys it falls back to chunks only). It writes
`evals/results/components-<UTC ts>/components.json` and `components.md`, and prints the table.

| Component | Gold | Prediction | Metric |
| --- | --- | --- | --- |
| OCR (`ws_pike.pdf`) | the text layer of the digital twin re-rendered by data_gen `render_digital`, in pipeline reading order | chunks in order, overlap removed | CER (raw, case/whitespace-normalized, whitespace-free, and an order-free narrative-body CER) |
| ASR (`aud_911`, `aud_interview`) | `<clip>.transcript.json` | chunks without `SPEAKER_n:`, plus the `asr/transcript.json` artifact | WER raw and number-normalized; diarization accuracy (best 1:1 speaker map, word-overlap ms) |
| Video | sidecar keyframe boxes (11 boxes over 5 clips) | `detect/detections.json`, `track/tracks.json` | per-class (vehicle/person) recall, precision and AP at IoU 0.5 (precision and AP are lower bounds because the labels are not exhaustive); labeled-track recall + temporal IoU |
| Entity resolution | story people/vehicles/phones/locations, matched to DB entities by surface-form rules | connected components of active `entity_links` | pairwise P/R/F1, worst false and missed merges |

The module docstrings in `caselens_evals/components/` give the exact matching rules and limits.
The math unit tests run by default. The run against the dev case is gated:
`CASELENS_HEAVY=1 pytest evals/tests/test_components.py`.

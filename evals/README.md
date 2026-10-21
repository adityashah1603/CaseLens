# evals: eval harness

Run it with `make eval` or `python -m caselens_evals`. It is a passing stub until the gold set exists.
Owner: eval agent.

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

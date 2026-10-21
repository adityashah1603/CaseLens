# workers: ingestion

Per-modality ingest pipelines (PDF, scanned PDF, audio, video, CSV). Owner: ingestion agent.
All evidence handled here is SYNTHETIC.

## Framework

- `ingest_jobs` is the job table and, locally, the queue (`PostgresQueue`, `FOR UPDATE SKIP LOCKED`).
  In prod a Cloud Tasks queue implements the same `Queue.enqueue` interface.
- A job is keyed on `(evidence_id, stage)`. The state machine is queued → running → succeeded | failed.
  Failed jobs retry with exponential backoff, and after 3 attempts they are dead-lettered.
  Each transition writes a system audit event: `user_id` is NULL, and it joins the hash chain (migration 0003).
- A stage's outputs, its `succeeded` mark, and the enqueue of the next stage commit in one transaction.
  Chunks are replaced per evidence, and derived artifacts use deterministic object keys
  (`cases/{case}/evidence/{id}/{stage}/...`), so retries never duplicate.
- Every job re-verifies the original's sha256 before it runs. A mismatch fails the job and audits
  `evidence.integrity_check`.
- `caselens_workers.pipelines.<type>` exposes a `Pipeline` (first stage, stage functions, successor map).
  The shared `embed` stage writes 768-dim bge-base-en-v1.5 vectors (fastembed, local).

## Run locally

```
make up && make migrate
python -m caselens_workers submit data_gen/output/riverside_lot --name "The Riverside Lot Homicide"
python -m caselens_workers run --once      # drain the queue (--pool cpu|gpu, --types pdf,csv)
python -m caselens_workers status
```

`submit` seeds a generated case as a case lead through `caselens_app` and RLS, the same path the API
upload will take. Workers connect as `caselens_worker`: with `CASELENS_WORKER_DATABASE_URL` set
(the dedicated `caselens_worker_login`, see `apps/api/README.md` "Database logins") the ingest loop
logs in with it, otherwise with `CASELENS_DATABASE_URL`; `submit` keeps using `CASELENS_DATABASE_URL`.

## Pipelines

| Type | Stages | Tools (free, local unless noted) | Chunk locator |
| --- | --- | --- | --- |
| csv | schema_infer → validate → load → register → embed | stdlib csv + Pydantic rows, upsert on `(evidence_id, row_id)` | `record` table + row_id (one chunk per row) |
| pdf (digital) | parse → layout → chunk → embed → extract | PyMuPDF blocks, ~500-token layout-aware chunks, 15% overlap | `pdf` page + bbox (points) |
| pdf (scanned) | parse → render → ocr → ocr_check → chunk → embed → extract | 200 DPI render, RapidOCR (ONNX); VLM fallback via the router below 0.6 confidence | `pdf` page + bbox + `ocr_confidence` |
| audio | normalize → asr → diarize → segment → embed → extract | ffmpeg 16 kHz mono, faster-whisper `base.en` (int8), Silero VAD, SpeechBrain ECAPA + agglomerative clustering | `audio` start/end ms + speaker |
| video | scene_detect → keyframes → detect → track → caption → embed → extract | PySceneDetect, YOLO + ByteTrack, VLM captions via the router | `video` start/end ms, frame, bbox, track |

- A `#` first line in a CSV is the SYNTHETIC notice and is skipped. Invalid rows fail the stage by row_id;
  nothing is silently dropped. Phones and plates from CSV rows become entities (exact pass) in `register`.
- `extract` (shared): a deterministic pass (phones → E.164, plates) plus a batched LLM pass through
  `caselens_agent.router` with strict JSON schemas. Evidence goes in delimited blocks. Mentions under 0.6
  confidence get `needs_review`. Event times resolve to wall clock in the case timezone
  (`CASELENS_CASE_TIMEZONE`, default America/Los_Angeles). An unmarked 12-hour time in a document is placed
  before the document's own time. Then entity resolution runs: exact keys, then fuzzy (rapidfuzz + pg_trgm),
  then LLM adjudication for the 0.75–0.92 band. Merges are insert-only `entity_links` (newer → older), so an
  analyst's revert sticks. Names with different numbers never merge.
- Without `CASELENS_GEMINI_API_KEY`, extraction runs only the deterministic pass and the stage summary says
  `llm: skipped`.
- Audio tuning: `CASELENS_AUDIO_*` settings (model size, VAD, clustering threshold).

## Gate check

`python -m caselens_evals.ingest_check --story riverside_lot` compares the ingested case with the data_gen
answer key and story. It reports per-file completion, unembedded chunks, locator coverage and story entities.

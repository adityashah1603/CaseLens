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
upload will take. Workers connect as `caselens_worker`.

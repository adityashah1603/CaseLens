# workers: ingestion

Per-modality ingest pipelines (PDF, scanned PDF, audio, video, CSV). Owner: ingestion agent.
Each stage is idempotent and keyed on `(evidence_id, stage)`. `python -m caselens_workers` runs the stub.

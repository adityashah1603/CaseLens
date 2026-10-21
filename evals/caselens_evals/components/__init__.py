"""Component evals: OCR CER, ASR WER + diarization, video detection/tracking, entity resolution.

Each compares the ingested SYNTHETIC case (read-only: Postgres chunks/entities/links and MinIO
stage artifacts) with data_gen ground truth. Run with `python -m caselens_evals.components`.
"""

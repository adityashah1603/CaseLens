"""Per-modality stage graphs and the code that runs one claimed job.

Each modality module `caselens_workers.pipelines.<type>` exposes `PIPELINE`. Stages run one at a
time per file; a stage's success enqueues the next stage (in the same transaction as its outputs).
"""

from __future__ import annotations

import importlib
import tempfile
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from caselens_workers import jobs
from caselens_workers.context import (
    EvidenceType,
    IntegrityError,
    StageContext,
    StageResult,
    load_evidence,
)
from caselens_workers.db import Conn
from caselens_workers.settings import WorkerSettings
from caselens_workers.storage import ObjectStore

StageFn = Callable[[StageContext], StageResult | None]

# Stages for the GPU pool in prod (CPU fallback locally), so a long video never blocks a PDF.
GPU_STAGES = frozenset({"asr", "diarize", "detect", "track", "caption"})


@dataclass(frozen=True)
class Pipeline:
    first: str
    stages: Mapping[str, StageFn]
    # Default successor of each stage; None ends the pipeline. A StageResult may override it.
    next: Mapping[str, str | None]

    def __post_init__(self) -> None:
        missing = {self.first, *self.next, *(n for n in self.next.values() if n)} - set(self.stages)
        if missing:
            raise ValueError(f"pipeline references unknown stages: {sorted(missing)}")


def load_pipeline(evidence_type: EvidenceType) -> Pipeline:
    module = importlib.import_module(f"caselens_workers.pipelines.{evidence_type}")
    pipeline: Pipeline = module.PIPELINE
    return pipeline


def all_stage_names(pipelines: Mapping[EvidenceType, Pipeline]) -> set[str]:
    return {name for p in pipelines.values() for name in p.stages}


def run_job(
    conn: Conn,
    job: jobs.JobMessage,
    pipelines: Mapping[EvidenceType, Pipeline],
    store: ObjectStore,
    settings: WorkerSettings,
    queue: jobs.Queue,
) -> str:
    """Run one claimed job to a terminal state for this attempt. Returns the job's new status."""
    try:
        evidence = load_evidence(conn, job.evidence_id)
        pipeline = pipelines[evidence.type]
        stage = pipeline.stages[job.stage]
        with tempfile.TemporaryDirectory(prefix=f"caselens-{job.stage}-") as tmp:
            ctx = StageContext(job, evidence, conn, store, settings, Path(tmp))
            with conn.transaction():
                result = stage(ctx) or StageResult()
                nxt = result.next_stage if result.next_stage else pipeline.next.get(job.stage)
                if nxt is not None:
                    if nxt not in pipeline.stages:
                        raise ValueError(f"stage {job.stage} chose unknown next stage {nxt!r}")
                    queue.enqueue(conn, job.case_id, job.evidence_id, nxt)
                jobs.mark_succeeded(conn, job, {"next_stage": nxt, **result.summary})
        return "succeeded"
    except Exception as exc:
        error = "".join(traceback.format_exception_only(exc)).strip()
        with conn.transaction():
            if isinstance(exc, IntegrityError):
                jobs.audit(
                    conn,
                    job.case_id,
                    "evidence.integrity_check",
                    {"evidence_id": str(job.evidence_id), "ok": False, "error": error[:500]},
                )
            return jobs.mark_failed(conn, job, error, settings.max_attempts)

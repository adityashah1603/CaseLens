"""CaseLens ingest worker CLI (local docker compose path).

python -m caselens_workers submit data_gen/output/riverside_lot --name "The Riverside Lot Homicide"
python -m caselens_workers run --once            # drain the queue, then exit
python -m caselens_workers run --pool gpu        # only ASR / diarize / detect / track / caption
python -m caselens_workers status
python -m caselens_workers requeue <evidence_id> <stage>
python -m caselens_workers relink-vehicles --case-id <case_id>   # description -> plate links
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import get_args

from caselens_workers import jobs
from caselens_workers.context import EvidenceType
from caselens_workers.db import connect, worker_connection
from caselens_workers.pipeline import GPU_STAGES, Pipeline, load_pipeline, run_job
from caselens_workers.settings import WorkerSettings
from caselens_workers.storage import object_store
from caselens_workers.submit import submit_case

EVIDENCE_TYPES: tuple[EvidenceType, ...] = get_args(EvidenceType)


def _pipelines(types: Sequence[str]) -> dict[EvidenceType, Pipeline]:
    return {t: load_pipeline(t) for t in EVIDENCE_TYPES if t in types}


def _stages_for_pool(pipelines: dict[EvidenceType, Pipeline], pool: str) -> set[str]:
    names = {name for p in pipelines.values() for name in p.stages}
    if pool == "gpu":
        return names & GPU_STAGES
    if pool == "cpu":
        return names - GPU_STAGES
    return names


def cmd_submit(args: argparse.Namespace, settings: WorkerSettings) -> int:
    if not settings.database_url:
        raise SystemExit("CASELENS_DATABASE_URL is not set")
    types = set(args.types.split(",")) if args.types else None
    pipelines = _pipelines(sorted(types) if types else EVIDENCE_TYPES)
    with connect(settings.database_url, settings.app_db_role) as conn:
        report = submit_case(
            conn,
            object_store(settings),
            Path(args.case_dir),
            args.name,
            {t: p.first for t, p in pipelines.items()},
            types,
        )
    print(f"case {report.case_id}: {len(report.uploaded)} uploaded, {len(report.skipped)} skipped")
    return 0


def cmd_run(args: argparse.Namespace, settings: WorkerSettings) -> int:
    pipelines = _pipelines(args.types.split(",") if args.types else EVIDENCE_TYPES)
    stages = _stages_for_pool(pipelines, args.pool)
    store = object_store(settings)
    queue = jobs.PostgresQueue()
    done = 0
    with worker_connection(settings) as conn:
        while True:
            with conn.transaction():
                job = jobs.claim_next(
                    conn, stages, settings.max_attempts, settings.stale_running_after_s
                )
            if job is None:
                if args.once:
                    break
                time.sleep(args.poll_s)
                continue
            started = time.monotonic()
            status = run_job(conn, job, pipelines, store, settings, queue)
            done += 1
            print(
                f"{job.stage:<12} {status:<13} {time.monotonic() - started:6.1f}s "
                f"evidence={job.evidence_id} attempt={job.attempt}",
                flush=True,
            )
            if args.max_jobs and done >= args.max_jobs:
                break
    print(f"ran {done} job(s)")
    return 0


def cmd_status(args: argparse.Namespace, settings: WorkerSettings) -> int:
    with worker_connection(settings) as conn:
        rows = conn.execute(
            "SELECT e.type, j.stage, j.status, count(*) FROM public.ingest_jobs j "
            "JOIN public.evidence e ON e.id = j.evidence_id "
            "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        ).fetchall()
        failed = conn.execute(
            "SELECT e.original_filename, j.stage, j.status, j.attempts, left(j.error, 300) "
            "FROM public.ingest_jobs j JOIN public.evidence e ON e.id = j.evidence_id "
            "WHERE j.status IN ('failed', 'dead_lettered') ORDER BY 1, 2"
        ).fetchall()
    for row in rows:
        print(f"{row[0]:<6} {row[1]:<12} {row[2]:<13} {row[3]}")
    for row in failed:
        print(f"! {row[0]} {row[1]} {row[2]} attempts={row[3]}: {row[4]}")
    return 0


def cmd_requeue(args: argparse.Namespace, settings: WorkerSettings) -> int:
    with worker_connection(settings) as conn, conn.transaction():
        ok = jobs.requeue(conn, uuid.UUID(args.evidence_id), args.stage)
    print("requeued" if ok else "no such job")
    return 0 if ok else 1


def cmd_relink_vehicles(args: argparse.Namespace, settings: WorkerSettings) -> int:
    from caselens_workers.resolve import relink_vehicles

    with worker_connection(settings) as conn, conn.transaction():
        made = relink_vehicles(conn, uuid.UUID(args.case_id))
    print(f"linked {made} vehicle description(s) to plates")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="caselens_workers", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("submit", help="seed a generated SYNTHETIC case directory")
    p.add_argument("case_dir")
    p.add_argument("--name", required=True)
    p.add_argument("--types", help="comma-separated subset of: pdf,audio,video,csv")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("run", help="claim and run ingest jobs")
    p.add_argument("--once", action="store_true", help="exit when the queue is empty")
    p.add_argument("--pool", choices=("all", "cpu", "gpu"), default="all")
    p.add_argument("--types", help="comma-separated subset of: pdf,audio,video,csv")
    p.add_argument("--max-jobs", type=int, default=0)
    p.add_argument("--poll-s", type=float, default=2.0)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="job counts by type / stage / status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("requeue", help="reset one stage to queued (e.g. after dead-lettering)")
    p.add_argument("evidence_id")
    p.add_argument("stage")
    p.set_defaults(func=cmd_requeue)

    p = sub.add_parser(
        "relink-vehicles",
        help="link vehicle descriptions to plates from existing mentions (no LLM, insert-only)",
    )
    p.add_argument("--case-id", required=True)
    p.set_defaults(func=cmd_relink_vehicles)

    args = parser.parse_args(argv)
    result: int = args.func(args, WorkerSettings())
    return result


if __name__ == "__main__":
    sys.exit(main())

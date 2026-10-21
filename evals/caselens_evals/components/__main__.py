"""Component evals CLI.

    python -m caselens_evals.components [--only ocr,asr,video,er] [--results evals/results]

Connects as caselens_worker on a read-only session (CASELENS_DATABASE_URL) and reads stage
artifacts from MinIO (CASELENS_S3_*; skipped when no key is set). Writes
`<results>/components-<UTC timestamp>/components.json` and `components.md`. No LLM calls.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from caselens_data_gen.story import load_story
from caselens_evals.components import asr, er, ocr, report, video
from caselens_evals.components.sources import DATA_GEN_OUT, CaseSource, find_case
from caselens_workers.db import worker_connection
from caselens_workers.settings import WorkerSettings
from caselens_workers.storage import ObjectStore, object_store

RESULTS_DIR = Path(__file__).resolve().parents[2] / "results"
COMPONENTS = ("ocr", "asr", "video", "er")


def run(
    src: CaseSource, story_slug: str, data_gen_out: Path, only: tuple[str, ...] = COMPONENTS
) -> dict[str, Any]:
    case_dir = data_gen_out / story_slug
    jobs: dict[str, Callable[[], dict[str, Any]]] = {
        "ocr": lambda: ocr.evaluate(src, story_slug),
        "asr": lambda: asr.evaluate(src, case_dir),
        "video": lambda: video.evaluate(src, case_dir),
        "er": lambda: er.evaluate(src, load_story(story_slug)),
    }
    results: dict[str, Any] = {"errors": {}}
    for name in only:
        try:
            results[name] = jobs[name]()
        except Exception:  # one component failing must not hide the others' numbers
            results["errors"][name] = traceback.format_exc(limit=3)
    return results


def write(results: dict[str, Any], results_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = results_dir / f"components-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "components.json").write_text(json.dumps(results, indent=2, default=str), "utf-8")
    (out / "components.md").write_text(report.render(results), "utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="caselens_evals.components", description=__doc__)
    parser.add_argument("--story", default="riverside_lot")
    parser.add_argument("--case", default="The Riverside Lot Homicide")
    parser.add_argument("--data-gen-out", type=Path, default=DATA_GEN_OUT)
    parser.add_argument("--results", type=Path, default=RESULTS_DIR)
    parser.add_argument("--only", default=",".join(COMPONENTS))
    args = parser.parse_args(argv)
    only = tuple(c for c in args.only.split(",") if c)
    unknown = set(only) - set(COMPONENTS)
    if unknown:
        parser.error(f"unknown component(s): {sorted(unknown)}")
    settings = WorkerSettings()
    store: ObjectStore | None = object_store(settings) if settings.s3_access_key else None
    with worker_connection(settings) as conn:
        conn.execute("SET default_transaction_read_only = on")
        src = CaseSource(conn, store, find_case(conn, args.case))
        results = run(src, args.story, args.data_gen_out, only)
    results = {
        "case": args.case,
        "story": args.story,
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "artifacts": "minio" if store is not None else "none (chunks only)",
        **results,
    }
    out = write(results, args.results)
    print(report.render(results))
    print(f"written to {out}")
    return 1 if results["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())

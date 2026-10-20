"""Week 2 gate check: is a generated SYNTHETIC case fully ingested?

    python -m caselens_evals.ingest_check --story riverside_lot --case "The Riverside Lot Homicide"

Compares the database with the data_gen ground truth (story + answer_key.json), which never
enters the app. Passes when every evidence file reached its last stage, every chunk is embedded,
the chunks cover the answer-key locators, and the story's people, vehicles and phones exist as
entities. Read-only; connects as caselens_worker.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from caselens_data_gen.story import load_story
from caselens_workers.db import Conn, worker_connection
from caselens_workers.resolve import normalize_name, normalize_phone, normalize_plate
from caselens_workers.settings import WorkerSettings

DATA_GEN_OUT = Path(__file__).resolve().parents[2] / "data_gen" / "output"
# Last stage per evidence type: the file is fully ingested once this stage succeeded.
FINAL_STAGE = {"pdf": "extract", "audio": "extract", "video": "extract", "csv": "embed"}
COVERAGE_TARGET = 0.95
ENTITY_TARGET = 0.9


@dataclass
class Report:
    files: dict[str, str] = field(default_factory=dict)  # filename -> complete | <problem>
    chunks: int = 0
    unembedded: int = 0
    coverage: dict[str, tuple[int, int]] = field(default_factory=dict)  # kind -> (hit, total)
    missing_facts: list[str] = field(default_factory=list)
    entities: dict[str, tuple[int, int]] = field(default_factory=dict)
    missing_entities: list[str] = field(default_factory=list)
    events: int = 0
    events_needing_review: int = 0
    links: Counter[str] = field(default_factory=Counter)

    def passed(self) -> bool:
        cov_hit = sum(h for h, _ in self.coverage.values())
        cov_total = sum(t for _, t in self.coverage.values())
        ent_hit = sum(h for h, _ in self.entities.values())
        ent_total = sum(t for _, t in self.entities.values())
        return (
            bool(self.files)
            and all(v == "complete" for v in self.files.values())
            and self.unembedded == 0
            and cov_total > 0
            and cov_hit / cov_total >= COVERAGE_TARGET
            and ent_total > 0
            and ent_hit / ent_total >= ENTITY_TARGET
        )


def _overlaps(a: list[float], b: list[float]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _covers(fact: dict[str, Any], chunk: dict[str, Any]) -> bool:
    want, got = fact, chunk
    if want["kind"] != got["kind"]:
        return False
    if want["kind"] == "pdf":
        if want["page"] != got["page"]:
            return False
        return not want.get("bbox") or not got.get("bbox") or _overlaps(want["bbox"], got["bbox"])
    if want["kind"] == "record":
        return bool(want["table"] == got["table"] and want["row_id"] == got["row_id"])
    w_end = want.get("end_ms") or want["start_ms"]
    g_end = got.get("end_ms") or got["start_ms"]
    return bool(want["start_ms"] <= g_end and got["start_ms"] <= w_end)


def check(conn: Conn, story_slug: str, case_name: str, out_dir: Path) -> Report:
    story = load_story(story_slug)
    case_dir = out_dir / story_slug
    manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
    answer_key = json.loads((case_dir / "answer_key.json").read_text(encoding="utf-8"))
    report = Report()

    row = conn.execute("SELECT id FROM public.cases WHERE name = %s", (case_name,)).fetchone()
    if row is None:
        raise SystemExit(f"case {case_name!r} not found")
    case_id: uuid.UUID = row[0]

    evidence = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT original_filename, id, type FROM public.evidence WHERE case_id = %s",
            (case_id,),
        ).fetchall()
    }
    jobs: dict[uuid.UUID, dict[str, str]] = defaultdict(dict)
    for ev_id, stage, status in conn.execute(
        "SELECT evidence_id, stage, status FROM public.ingest_jobs WHERE case_id = %s", (case_id,)
    ).fetchall():
        jobs[ev_id][stage] = status
    for entry in manifest["files"]:
        name = Path(entry["path"]).name
        if name not in evidence:
            report.files[name] = "not uploaded"
            continue
        ev_id, ev_type = evidence[name]
        bad = {s: st for s, st in jobs[ev_id].items() if st != "succeeded"}
        if bad:
            report.files[name] = ", ".join(f"{s}={st}" for s, st in sorted(bad.items()))
        elif jobs[ev_id].get(FINAL_STAGE[ev_type]) != "succeeded":
            report.files[name] = f"{FINAL_STAGE[ev_type]} not reached"
        else:
            report.files[name] = "complete"

    chunk_locators: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for ev_id, locator, embedded in conn.execute(
        "SELECT evidence_id, locator, embedding IS NOT NULL FROM public.chunks WHERE case_id = %s",
        (case_id,),
    ).fetchall():
        chunk_locators[ev_id].append(locator)
        report.chunks += 1
        report.unembedded += 0 if embedded else 1

    hits: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    for fact in answer_key["facts"]:
        kind = fact["locator"]["kind"]
        totals[kind] += 1
        found = evidence.get(Path(fact["evidence"]).name)
        if found and any(_covers(fact["locator"], c) for c in chunk_locators[found[0]]):
            hits[kind] += 1
        else:
            report.missing_facts.append(f"{fact['fact_id']} @ {fact['evidence']}")
    report.coverage = {k: (hits[k], totals[k]) for k in sorted(totals)}

    keys: dict[str, set[str]] = defaultdict(set)
    for etype, key, name in conn.execute(
        "SELECT type, normalized_key, canonical_name FROM public.entities WHERE case_id = %s",
        (case_id,),
    ).fetchall():
        if key:
            keys[etype].add(key)
        if (norm := normalize_name(name)) is not None:
            keys[f"{etype}:name"].add(norm)

    def tally(label: str, wanted: list[tuple[str, bool]]) -> None:
        hit = sum(1 for _, ok in wanted if ok)
        report.entities[label] = (hit, len(wanted))
        report.missing_entities += [f"{label}: {n}" for n, ok in wanted if not ok]

    people = [
        (p.name, any(normalize_name(n) in keys["person:name"] for n in (p.name, *p.aliases)))
        for p in story.people
    ]
    vehicles = [(v.plate, normalize_plate(v.plate) in keys["vehicle"]) for v in story.vehicles]
    phones = [(p.number, normalize_phone(p.number) in keys["phone"]) for p in story.phones]
    tally("person", people)
    tally("vehicle", vehicles)
    tally("phone", phones)

    ev = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE needs_review) FROM public.events "
        "WHERE case_id = %s",
        (case_id,),
    ).fetchone()
    if ev is not None:
        report.events, report.events_needing_review = ev
    for method, n in conn.execute(
        "SELECT method, count(*) FROM public.entity_links WHERE case_id = %s AND status = 'active' "
        "GROUP BY method",
        (case_id,),
    ).fetchall():
        report.links[method] = n
    return report


def render(report: Report) -> str:
    lines = ["SYNTHETIC case ingest check", ""]
    for name, state in sorted(report.files.items()):
        lines.append(f"  {'ok ' if state == 'complete' else 'BAD'} {name}: {state}")
    lines.append(f"chunks: {report.chunks} ({report.unembedded} without embedding)")
    for kind, (hit, total) in report.coverage.items():
        lines.append(f"answer-key coverage {kind:<6} {hit}/{total}")
    for label, (hit, total) in report.entities.items():
        lines.append(f"story entities {label:<7} {hit}/{total}")
    lines.append(f"events: {report.events} ({report.events_needing_review} need review)")
    lines.append(f"entity links: {dict(report.links)}")
    lines += [f"  missing fact: {m}" for m in report.missing_facts[:20]]
    lines += [f"  missing entity: {m}" for m in report.missing_entities]
    lines.append(f"GATE: {'PASS' if report.passed() else 'FAIL'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="caselens_evals.ingest_check", description=__doc__)
    parser.add_argument("--story", default="riverside_lot")
    parser.add_argument("--case", default="The Riverside Lot Homicide")
    parser.add_argument("--out", type=Path, default=DATA_GEN_OUT)
    args = parser.parse_args(argv)
    with worker_connection(WorkerSettings()) as conn:
        report = check(conn, args.story, args.case, args.out)
    print(render(report))
    return 0 if report.passed() else 1


if __name__ == "__main__":
    sys.exit(main())

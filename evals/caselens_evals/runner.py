"""Run the investigator agent on gold questions and store the raw results.

Each gold item becomes one in-process `caselens_agent.runner.ask` call on the demo case as the
seed lead (a `caselens_app` connection with `app.user_id` set, plus a `caselens_readonly` one
for semantic-layer SQL), exactly like the API. One JSON line per run goes to
`evals/results/<timestamp>/runs.jsonl`: the answer, tool calls, latency, model, cost, the
results of every `query_records` call (for SQL accuracy) and the text of every cited source
(for the judge). The file is appended as runs finish, so a run that stops when the free tier
runs out resumes where it left off.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from caselens_evals.gold import GoldItem

if TYPE_CHECKING:
    from caselens_agent.router import RouterSettings

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"
RUNS_FILE = "runs.jsonl"
META_FILE = "meta.json"
CATEGORIES = ("single_source", "multi_source", "structured", "conflict", "unanswerable")
SMOKE_PER_CATEGORY = 2  # 5 categories x 2 = the 10-question smoke subset
SEED_SUBJECT = "local-dev:seed-lead"
DEMO_CASE_NAME = "The Riverside Lot Homicide"
SOURCE_TEXT_LIMIT = 2000
SQL_ROWS_LIMIT = 200
# Stop early after this many runs in a row whose model calls failed (quota exhausted).
MAX_CONSECUTIVE_MODEL_FAILURES = 2

# Fixed statements per citable table (no SQL is built from strings).
RECORD_QUERIES: dict[str, str] = {
    "call_records": (
        "SELECT row_id, caller_number, callee_number, started_at, duration_s, call_type, "
        "cell_tower_id FROM public.call_records WHERE id = %s"
    ),
    "plate_reads": (
        "SELECT row_id, plate, plate_state, read_at, camera_id, location_name, confidence "
        "FROM public.plate_reads WHERE id = %s"
    ),
    "custody_records": (
        "SELECT row_id, item_id, description, collected_by, collected_at, collected_location, "
        "custody_chain FROM public.custody_records WHERE id = %s"
    ),
}


class SqlResult(BaseModel):
    """One `query_records` call as it executed in the run."""

    operation: str
    params: dict[str, Any]
    row_count: int
    rows: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False


class SourceText(BaseModel):
    """The text of one cited source, given to the judge as evidence."""

    filename: str | None
    kind: str
    text: str


class RunRecord(BaseModel):
    gold_id: str
    category: str
    question: str
    run_id: str | None = None
    status: str  # agent_runs.status (succeeded | failed) or "error" (ask raised)
    answer: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int | None = None
    model: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str | None = None
    error: str | None = None
    sql_results: list[SqlResult] = Field(default_factory=list)
    sources: dict[str, SourceText] = Field(default_factory=dict)  # citation id -> text
    evidence_files: dict[str, str] = Field(default_factory=dict)  # evidence id -> filename
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def model_failed(self) -> bool:
        return self.status != "succeeded"


Executor = Callable[[GoldItem], RunRecord]


def new_results_dir(base: Path = RESULTS_DIR) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = base / stamp
    path.mkdir(parents=True, exist_ok=False)
    return path


def latest_results_dir(base: Path = RESULTS_DIR) -> Path | None:
    dirs = sorted(p for p in base.glob("*") if (p / RUNS_FILE).exists())
    return dirs[-1] if dirs else None


def load_runs(out_dir: Path) -> dict[str, RunRecord]:
    """Runs keyed by gold id; when an item ran more than once, the latest line wins."""
    path = out_dir / RUNS_FILE
    runs: dict[str, RunRecord] = {}
    if not path.exists():
        return runs
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = RunRecord.model_validate_json(line)
            runs[rec.gold_id] = rec
    return runs


def append_run(out_dir: Path, record: RunRecord) -> None:
    with (out_dir / RUNS_FILE).open("a", encoding="utf-8") as fh:
        fh.write(record.model_dump_json() + "\n")


def smoke_subset(items: Sequence[GoldItem]) -> list[GoldItem]:
    """The fixed smoke set: the first two items (by id) of every category."""
    picked: list[GoldItem] = []
    for category in CATEGORIES:
        in_cat = sorted((i for i in items if i.category == category), key=lambda i: i.id)
        picked.extend(in_cat[:SMOKE_PER_CATEGORY])
    return picked


def select_items(
    items: Sequence[GoldItem],
    ids: Iterable[str] | None = None,
    category: str | None = None,
    limit: int | None = None,
    smoke: bool = False,
) -> list[GoldItem]:
    chosen = smoke_subset(items) if smoke else list(items)
    if ids:
        wanted = set(ids)
        unknown = wanted - {i.id for i in items}
        if unknown:
            raise ValueError(f"unknown gold ids: {', '.join(sorted(unknown))}")
        chosen = [i for i in chosen if i.id in wanted]
    if category:
        chosen = [i for i in chosen if i.category == category]
    return chosen[:limit] if limit is not None else chosen


@dataclass
class RunSummary:
    ran: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    stopped_early: bool = False


def run_items(
    items: Sequence[GoldItem],
    out_dir: Path,
    execute: Executor,
    retry_failed: bool = False,
    log: Callable[[str], None] = print,
) -> RunSummary:
    """Run every item not yet in `out_dir/runs.jsonl` (resumable). With `retry_failed`, items
    whose stored run failed are run again. Stops after repeated model failures (quota)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    done = load_runs(out_dir)
    summary = RunSummary()
    consecutive_failures = 0
    for item in items:
        prev = done.get(item.id)
        if prev is not None and not (retry_failed and prev.model_failed):
            summary.skipped.append(item.id)
            continue
        record = execute(item)
        append_run(out_dir, record)
        summary.ran.append(item.id)
        log(
            f"{item.id} [{item.category}] {record.status} "
            f"answer={(record.answer or {}).get('status')} {record.latency_ms} ms"
        )
        consecutive_failures = consecutive_failures + 1 if record.model_failed else 0
        if consecutive_failures >= MAX_CONSECUTIVE_MODEL_FAILURES:
            log("stopping: the model failed repeatedly (free-tier quota?); rerun to resume")
            summary.stopped_early = True
            break
    return summary


def write_meta(out_dir: Path, meta: Mapping[str, Any]) -> None:
    path = out_dir / META_FILE
    current: dict[str, Any] = {}
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
    current.update(meta)
    path.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")


def read_meta(out_dir: Path) -> dict[str, Any]:
    path = out_dir / META_FILE
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


# --- live execution (needs the dev DB and the model router) ---------------------------------


def parse_model_overrides(spec: str | None) -> dict[str, str]:
    """`plan=gemini-3-flash-preview,synthesize=...` -> router settings overrides. The key
    `provider` selects the LLM provider."""
    if not spec:
        return {}
    from caselens_agent.router import DEFAULT_MODELS

    tasks = set(DEFAULT_MODELS["fake"])
    overrides: dict[str, str] = {}
    for part in spec.split(","):
        key, sep, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not value or (key not in tasks and key != "provider"):
            raise ValueError(f"bad --model entry {part!r}; use task=model (tasks: {sorted(tasks)})")
        overrides["llm_provider" if key == "provider" else f"model_{key}"] = value
    return overrides


def router_settings(overrides: Mapping[str, str] | None = None) -> RouterSettings:
    """Router settings from the environment, with per-task model / provider overrides."""
    from caselens_agent.router import RouterSettings

    return RouterSettings().model_copy(update=dict(overrides or {}))


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=str))


def _row_text(columns: Sequence[str], row: Sequence[Any]) -> str:
    return "; ".join(f"{c}: {v}" for c, v in zip(columns, row, strict=True))


class LiveExecutor:
    """Runs `ask` on the dev DB. Use as a context manager (it owns two connections)."""

    def __init__(
        self,
        database_url: str,
        model_overrides: Mapping[str, str] | None = None,
        case_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> None:
        from caselens_agent.cli import connect_as
        from caselens_agent.router import ModelRouter

        self._url = database_url
        self.router = ModelRouter(router_settings(model_overrides))
        self.user_id = user_id or self._lookup_user()
        self.app_conn = connect_as(database_url, "caselens_app", self.user_id)
        self.readonly_conn = connect_as(database_url, "caselens_readonly", self.user_id)
        self.case_id = case_id or self._lookup_case()

    def _lookup_user(self) -> uuid.UUID:
        from caselens_agent.cli import connect_as

        # The seed lead's id; app.user_id is a placeholder until it is known.
        with connect_as(self._url, "caselens_app", uuid.UUID(int=0)) as conn:
            row = conn.execute(
                "SELECT id FROM public.users WHERE oidc_subject = %s", (SEED_SUBJECT,)
            ).fetchone()
        if row is None:
            raise RuntimeError("seed lead user not found; seed the demo case first")
        user_id: uuid.UUID = row[0]
        return user_id

    def _lookup_case(self) -> uuid.UUID:
        row = self.app_conn.execute(
            "SELECT id FROM public.cases WHERE name = %s", (DEMO_CASE_NAME,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"demo case {DEMO_CASE_NAME!r} not visible to the seed lead")
        case_id: uuid.UUID = row[0]
        return case_id

    def close(self) -> None:
        self.app_conn.close()
        self.readonly_conn.close()

    def __enter__(self) -> LiveExecutor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __call__(self, item: GoldItem) -> RunRecord:
        from caselens_agent.runner import ask
        from caselens_agent.tools import default_backends

        sql_results: list[SqlResult] = []
        backends = default_backends()
        real_query = backends.query_records

        def recording_query(conn: Any, case_id: uuid.UUID, args: Mapping[str, Any]) -> Any:
            out = real_query(conn, case_id, args)
            dumped = _jsonable(out)
            sql_results.append(
                SqlResult(
                    operation=str(dumped.get("operation", args.get("operation"))),
                    params=dict(args.get("params") or {}),
                    row_count=int(dumped.get("row_count", 0)),
                    rows=list(dumped.get("rows", []))[:SQL_ROWS_LIMIT],
                    truncated=bool(dumped.get("truncated", False)),
                )
            )
            return out

        backends.query_records = recording_query
        try:
            result = ask(
                self.app_conn,
                self.readonly_conn,
                self.case_id,
                self.user_id,
                item.question,
                router=self.router,
                backends=backends,
            )
        except Exception as exc:  # one broken run must not end the eval
            return RunRecord(
                gold_id=item.id,
                category=item.category,
                question=item.question,
                status="error",
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
        cost = self.app_conn.execute(
            "SELECT cost_usd, input_tokens, output_tokens FROM public.agent_runs WHERE id = %s",
            (result.run_id,),
        ).fetchone()
        return RunRecord(
            gold_id=item.id,
            category=item.category,
            question=item.question,
            run_id=str(result.run_id),
            status=result.status,
            answer=result.answer,
            tool_calls=[tc.as_json() for tc in result.tool_calls],
            latency_ms=result.latency_ms,
            model=result.model,
            stop_reason=result.stop_reason,
            cost_usd=None if cost is None or cost[0] is None else float(cost[0]),
            input_tokens=None if cost is None else cost[1],
            output_tokens=None if cost is None else cost[2],
            sql_results=sql_results,
            sources=self.source_texts(result.answer),
            evidence_files=self.evidence_files(result.answer),
        )

    def evidence_files(self, answer: Mapping[str, Any]) -> dict[str, str]:
        ids = sorted({str(c["evidence_id"]) for c in answer.get("citations", [])})
        if not ids:
            return {}
        rows = self.app_conn.execute(
            "SELECT id::text, original_filename FROM public.evidence WHERE id = ANY(%s::uuid[])",
            (ids,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def source_texts(self, answer: Mapping[str, Any]) -> dict[str, SourceText]:
        """The text behind every citation: the chunk text, or the cited record row."""
        files = self.evidence_files(answer)
        texts: dict[str, SourceText] = {}
        for cit in answer.get("citations", []):
            kind = str(cit.get("locator", {}).get("kind", "?"))
            text = ""
            if cit.get("source_type") == "chunk" and cit.get("chunk_id"):
                row = self.app_conn.execute(
                    "SELECT text FROM public.chunks WHERE id = %s", (cit["chunk_id"],)
                ).fetchone()
                text = str(row[0] or "") if row else ""
            elif cit.get("table") in RECORD_QUERIES and cit.get("record_id"):
                with self.app_conn.cursor() as cur:
                    cur.execute(RECORD_QUERIES[str(cit["table"])], (cit["record_id"],))
                    rec = cur.fetchone()
                    cols = [d.name for d in cur.description or []]
                text = f"{cit['table']} row: " + _row_text(cols, rec) if rec else ""
            texts[str(cit["id"])] = SourceText(
                filename=files.get(str(cit["evidence_id"])),
                kind=kind,
                text=text[:SOURCE_TEXT_LIMIT],
            )
        return texts

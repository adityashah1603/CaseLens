"""DB tests for the semantic layer compiler. Every row inserted here is SYNTHETIC."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from jsonschema import Draft202012Validator

from caselens_agent.contracts import load_tool_definitions
from caselens_semantic_layer import (
    OperationError,
    QueryRecordsOutput,
    RoleError,
    get_layer,
    readonly_transaction,
    run_operation,
)
from caselens_workers.db import Conn
from caselens_workers.testing import SeededEvidence, as_role

pytestmark = pytest.mark.db

KEMPER = "+13605550117"
HALE = "+13605550142"
OTHER = "+12535550166"
T0 = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
WINDOW = {"window_start": "2026-03-01T00:00:00Z", "window_end": "2026-03-31T00:00:00-07:00"}
OUTPUT_VALIDATOR = Draft202012Validator(
    load_tool_definitions()["query_records"]["output_schema"],
    format_checker=Draft202012Validator.FORMAT_CHECKER,
)


@dataclass
class Case:
    seeded: SeededEvidence
    next_row: int = 1

    @property
    def case_id(self) -> uuid.UUID:
        return self.seeded.case_id

    @property
    def user_id(self) -> uuid.UUID:
        return self.seeded.user_id

    def row(self) -> int:
        self.next_row += 1
        return self.next_row - 1


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "records.csv"
    path.write_text("# SYNTHETIC test data\ncol\n1\n", encoding="utf-8")
    return path


@pytest.fixture
def make_case(
    owner_conn: Conn, seed_evidence: Callable[..., SeededEvidence], csv_file: Path
) -> Callable[[], Case]:
    def make() -> Case:
        seeded = seed_evidence(csv_file, "csv")
        as_role(owner_conn, None)
        owner_conn.execute(
            "INSERT INTO public.case_members (case_id, user_id, role) VALUES (%s, %s, 'lead')",
            (seeded.case_id, seeded.user_id),
        )
        return Case(seeded)

    return make


def add_call(conn: Conn, case: Case, caller: str, callee: str, at: datetime) -> uuid.UUID:
    as_role(conn, None)
    row = conn.execute(
        "INSERT INTO public.call_records (case_id, evidence_id, row_id, caller_number, "
        "callee_number, started_at, duration_s, call_type, cell_tower_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, 60, 'voice', 'TWR-SYN-1') RETURNING id",
        (case.case_id, case.seeded.evidence_id, case.row(), caller, callee, at),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def add_plate(conn: Conn, case: Case, plate: str, at: datetime, location: str) -> uuid.UUID:
    as_role(conn, None)
    row = conn.execute(
        "INSERT INTO public.plate_reads (case_id, evidence_id, row_id, plate, plate_state, "
        "read_at, camera_id, location_name, latitude, longitude, confidence) "
        "VALUES (%s, %s, %s, %s, 'CA', %s, 'cam_syn', %s, 36.9, -123.4, 0.95) RETURNING id",
        (case.case_id, case.seeded.evidence_id, case.row(), plate, at, location),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def add_custody(conn: Conn, case: Case, item: str, at: datetime, by: str) -> None:
    as_role(conn, None)
    conn.execute(
        "INSERT INTO public.custody_records (case_id, evidence_id, row_id, item_id, description, "
        "collected_by, collected_at, collected_location, custody_chain) "
        "VALUES (%s, %s, %s, %s, 'SYNTHETIC item', %s, %s, 'Lot', 'sealed')",
        (case.case_id, case.seeded.evidence_id, case.row(), item, by, at),
    )


def run_as(
    conn: Conn, user_id: uuid.UUID, case_id: uuid.UUID, op: str, params: dict[str, Any]
) -> QueryRecordsOutput:
    as_role(conn, "caselens_readonly", user_id)
    out = run_operation(conn, case_id, op, params)
    OUTPUT_VALIDATOR.validate(out.model_dump(mode="json"))
    assert len(out.sources) == out.row_count or out.operation == "call_count_between"
    return out


@pytest.fixture
def call_case(owner_conn: Conn, make_case: Callable[[], Case]) -> Iterator[Case]:
    case = make_case()
    add_call(owner_conn, case, KEMPER, HALE, T0)
    add_call(owner_conn, case, HALE, KEMPER, T0 + timedelta(hours=1))
    add_call(owner_conn, case, KEMPER, OTHER, T0 + timedelta(hours=2))
    add_call(owner_conn, case, KEMPER, HALE, datetime(2026, 4, 2, tzinfo=UTC))  # out of window
    yield case


def test_calls_between_both_directions_with_citations(owner_conn: Conn, call_case: Case) -> None:
    out = run_as(
        owner_conn,
        call_case.user_id,
        call_case.case_id,
        "calls_between",
        {"phone_a": "(360) 555-0142", "phone_b": KEMPER, **WINDOW},
    )
    assert out.row_count == 2 and not out.truncated
    assert [r["caller_number"] for r in out.rows] == [KEMPER, HALE]
    assert out.rows[0]["started_at"] == "2026-03-10T12:00:00+00:00"
    assert out.columns == list(get_layer()["operations"]["calls_between"]["select"])
    assert [s.row_id for s in out.sources] == [1, 2]
    assert {s.table for s in out.sources} == {"call_records"}
    assert {s.evidence_id for s in out.sources} == {call_case.seeded.evidence_id}
    assert out.params["phone_a"] == HALE
    assert out.params["case_id"] == str(call_case.case_id)
    assert "%(case_id)s" in out.sql and HALE not in out.sql


def test_phone_normalization_is_equivalent(owner_conn: Conn, call_case: Case) -> None:
    variants = ["(360) 555-0142", "+13605550142", "360.555.0142", "360 555 0142"]
    outs = [
        run_as(
            owner_conn,
            call_case.user_id,
            call_case.case_id,
            "calls_between",
            {"phone_a": v, "phone_b": KEMPER, **WINDOW},
        )
        for v in variants
    ]
    assert all(o.rows == outs[0].rows and o.params == outs[0].params for o in outs)


def test_call_count_between(owner_conn: Conn, call_case: Case) -> None:
    out = run_as(
        owner_conn,
        call_case.user_id,
        call_case.case_id,
        "call_count_between",
        {"phone_a": KEMPER, "phone_b": "3605550142", **WINDOW},
    )
    assert out.columns == ["call_count"] and out.rows == [{"call_count": 2}]
    assert out.row_count == 1 and not out.truncated
    assert sorted(s.row_id for s in out.sources) == [1, 2]
    none = run_as(
        owner_conn,
        call_case.user_id,
        call_case.case_id,
        "call_count_between",
        {"phone_a": OTHER, "phone_b": HALE, **WINDOW},
    )
    assert none.rows == [{"call_count": 0}] and none.sources == []


def test_calls_for_phone(owner_conn: Conn, call_case: Case) -> None:
    out = run_as(
        owner_conn,
        call_case.user_id,
        call_case.case_id,
        "calls_for_phone",
        {"phone": KEMPER, **WINDOW},
    )
    assert out.row_count == 3
    assert [r["callee_number"] for r in out.rows] == [HALE, KEMPER, OTHER]


def test_case_scoping_and_rls(owner_conn: Conn, make_case: Callable[[], Case]) -> None:
    mine, theirs = make_case(), make_case()
    add_call(owner_conn, mine, KEMPER, HALE, T0)
    add_call(owner_conn, theirs, KEMPER, HALE, T0)
    add_call(owner_conn, theirs, HALE, KEMPER, T0)
    params = {"phone_a": KEMPER, "phone_b": HALE, **WINDOW}
    out = run_as(owner_conn, mine.user_id, mine.case_id, "calls_between", params)
    assert out.row_count == 1 and out.sources[0].evidence_id == mine.seeded.evidence_id
    # A user who is not a member of the other case sees nothing there (RLS), even by case id.
    blocked = run_as(owner_conn, mine.user_id, theirs.case_id, "calls_between", params)
    assert blocked.row_count == 0 and blocked.sources == []
    count = run_as(owner_conn, mine.user_id, theirs.case_id, "call_count_between", params)
    assert count.rows == [{"call_count": 0}]


def test_unset_user_sees_nothing(owner_conn: Conn, call_case: Case) -> None:
    as_role(owner_conn, "caselens_readonly", None)
    out = run_operation(
        owner_conn, call_case.case_id, "calls_for_phone", {"phone": KEMPER, **WINDOW}
    )
    assert out.row_count == 0


def test_plate_sightings_normalization(owner_conn: Conn, make_case: Callable[[], Case]) -> None:
    case = make_case()
    add_plate(owner_conn, case, "SYN4821", T0 + timedelta(minutes=5), "Route 9 at Marsh Rd")
    add_plate(owner_conn, case, "SYN4821", T0, "Riverside Lot entrance")
    add_plate(owner_conn, case, "SYN2290", T0, "Riverside Lot entrance")
    results = [
        run_as(
            owner_conn,
            case.user_id,
            case.case_id,
            "plate_sightings",
            {"plate": p, **WINDOW},
        )
        for p in ("syn 4821", "SYN4821", "syn-4821")
    ]
    assert all(r.rows == results[0].rows for r in results)
    out = results[0]
    assert out.params["plate"] == "SYN4821" and out.row_count == 2
    assert [r["location_name"] for r in out.rows] == [
        "Riverside Lot entrance",
        "Route 9 at Marsh Rd",
    ]
    assert out.rows[0]["confidence"] == 0.95 and out.rows[0]["latitude"] == 36.9
    assert {s.table for s in out.sources} == {"plate_reads"}


def test_plate_sightings_near_ilike_escaping(
    owner_conn: Conn, make_case: Callable[[], Case]
) -> None:
    case = make_case()
    add_plate(owner_conn, case, "SYN0001", T0, "Lot 50% Off")
    add_plate(owner_conn, case, "SYN0002", T0, "Lot 500 Off")
    add_plate(owner_conn, case, "SYN0003", T0, "Gate_A")
    add_plate(owner_conn, case, "SYN0004", T0, "GateXA")
    add_plate(owner_conn, case, "SYN0005", T0, "Calder River Rd at Riverside Lot entrance")

    def near(text: str) -> list[str]:
        out = run_as(
            owner_conn,
            case.user_id,
            case.case_id,
            "plate_sightings_near",
            {"location_name": text, **WINDOW},
        )
        return sorted(r["plate"] for r in out.rows)

    assert near("50%") == ["SYN0001"]
    assert near("gate_a") == ["SYN0003"]
    assert near("  riverside ") == ["SYN0005"]
    assert near("%") == ["SYN0001"]


def test_injection_attempt_stays_bound(owner_conn: Conn, call_case: Case) -> None:
    payload = "x' OR 1=1; DROP TABLE call_records; --"
    out = run_as(
        owner_conn,
        call_case.user_id,
        call_case.case_id,
        "plate_sightings_near",
        {"location_name": payload, **WINDOW},
    )
    assert out.row_count == 0
    assert payload not in out.sql and "DROP" not in out.sql
    assert out.params["location_name"] == "%" + payload.replace("_", r"\_") + "%"
    custody = run_as(
        owner_conn, call_case.user_id, call_case.case_id, "custody_chain", {"item_id": payload}
    )
    assert custody.row_count == 0
    as_role(owner_conn, None)
    still = owner_conn.execute("SELECT count(*) FROM public.call_records").fetchone()
    assert still is not None and still[0] >= 4


def test_custody_chain_order(owner_conn: Conn, make_case: Callable[[], Case]) -> None:
    case = make_case()
    add_custody(owner_conn, case, "ITEM-007", T0 + timedelta(hours=1), "Officer B (SYNTHETIC)")
    add_custody(owner_conn, case, "ITEM-007", T0, "Officer A (SYNTHETIC)")
    add_custody(owner_conn, case, "ITEM-008", T0, "Officer C (SYNTHETIC)")
    out = run_as(owner_conn, case.user_id, case.case_id, "custody_chain", {"item_id": " ITEM-007 "})
    assert [r["collected_by"] for r in out.rows] == [
        "Officer A (SYNTHETIC)",
        "Officer B (SYNTHETIC)",
    ]
    assert [s.row_id for s in out.sources] == [2, 1]
    assert {s.table for s in out.sources} == {"custody_records"}


def test_truncation(
    owner_conn: Conn, make_case: Callable[[], Case], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_case()
    for i in range(4):
        add_call(owner_conn, case, KEMPER, HALE, T0 + timedelta(minutes=i))
    monkeypatch.setitem(get_layer()["defaults"], "max_rows", 3)
    params = {"phone_a": KEMPER, "phone_b": HALE, **WINDOW}
    out = run_as(owner_conn, case.user_id, case.case_id, "calls_between", params)
    assert out.truncated and out.row_count == 3 and len(out.sources) == 3
    assert "LIMIT 4" in out.sql
    count = run_as(owner_conn, case.user_id, case.case_id, "call_count_between", params)
    assert count.rows == [{"call_count": 4}] and count.truncated and len(count.sources) == 3
    monkeypatch.setitem(get_layer()["defaults"], "max_rows", 4)
    full = run_as(owner_conn, case.user_id, case.case_id, "calls_between", params)
    assert not full.truncated and full.row_count == 4


def _setting(conn: Conn, name: str) -> str:
    row = conn.execute("SELECT current_setting(%s)", (name,)).fetchone()
    assert row is not None
    return str(row[0])


def test_readonly_transaction_settings(owner_conn: Conn, call_case: Case) -> None:
    as_role(owner_conn, "caselens_readonly", call_case.user_id)
    before = _setting(owner_conn, "statement_timeout")
    timeout = get_layer()["defaults"]["statement_timeout_ms"]
    with readonly_transaction(owner_conn, timeout):
        assert _setting(owner_conn, "statement_timeout") == f"{timeout // 1000}s"
        assert _setting(owner_conn, "transaction_read_only") == "on"
    assert _setting(owner_conn, "statement_timeout") == before
    with pytest.raises(psycopg.errors.QueryCanceled), readonly_transaction(owner_conn, 100):
        owner_conn.execute("SELECT pg_sleep(1)")


def test_run_operation_uses_layer_timeout(
    owner_conn: Conn, call_case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    import caselens_semantic_layer.compiler as compiler

    seen: list[int] = []
    real = compiler.readonly_transaction

    def spy(conn: Conn, timeout_ms: int) -> Any:
        seen.append(timeout_ms)
        return real(conn, timeout_ms)

    monkeypatch.setattr(compiler, "readonly_transaction", spy)
    run_as(
        owner_conn,
        call_case.user_id,
        call_case.case_id,
        "calls_for_phone",
        {"phone": HALE, **WINDOW},
    )
    assert seen == [get_layer()["defaults"]["statement_timeout_ms"]]


def test_writes_are_rejected(owner_conn: Conn, call_case: Case) -> None:
    as_role(owner_conn, "caselens_readonly", call_case.user_id)
    with (
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
        readonly_transaction(owner_conn, 5000),
    ):
        owner_conn.execute("DELETE FROM public.call_records")


def test_wrong_role_is_refused(owner_conn: Conn, call_case: Case) -> None:
    as_role(owner_conn, "caselens_app", call_case.user_id)
    with pytest.raises(RoleError):
        run_operation(owner_conn, call_case.case_id, "calls_for_phone", {"phone": HALE, **WINDOW})


@pytest.mark.parametrize(
    ("operation", "params", "message"),
    [
        ("drop_everything", {}, "unknown operation"),
        ("calls_for_phone", {"phone": HALE}, "missing required"),
        ("calls_for_phone", {"phone": HALE, "case_id": "x", **WINDOW}, "unknown param"),
        ("calls_for_phone", {"phone": "555-0142", **WINDOW}, "not a valid phone"),
        ("calls_for_phone", {"phone": "call me", **WINDOW}, "not a valid phone"),
        ("calls_for_phone", {"phone": 3605550142, **WINDOW}, "wrong type"),
        (
            "calls_for_phone",
            {"phone": HALE, "window_start": "2026-03-01T00:00:00", "window_end": "2026-03-02"},
            "param",
        ),
        (
            "calls_for_phone",
            {"phone": HALE, "window_start": "yesterday", "window_end": "2026-03-02T00:00:00Z"},
            "param 'window_start'",
        ),
        ("plate_sightings", {"plate": "SYN 4821 !!", **WINDOW}, "not a valid plate"),
        ("plate_sightings", {"plate": "SYN48211234567", **WINDOW}, "not a valid plate"),
        ("custody_chain", {"item_id": "   "}, "must not be empty"),
        ("custody_chain", {"item_id": "x" * 201}, "too long"),
        ("plate_sightings_near", {"location_name": "", **WINDOW}, "must not be empty"),
    ],
)
def test_bad_params_rejected(
    owner_conn: Conn, call_case: Case, operation: str, params: dict[str, Any], message: str
) -> None:
    as_role(owner_conn, "caselens_readonly", call_case.user_id)
    with pytest.raises(OperationError, match=message) as excinfo:
        run_operation(owner_conn, call_case.case_id, operation, params)
    assert "SYN 4821 !!" not in str(excinfo.value)


def test_naive_timestamp_rejected_after_schema(owner_conn: Conn, call_case: Case) -> None:
    from caselens_semantic_layer.params import normalize_value

    with pytest.raises(OperationError, match="UTC offset"):
        normalize_value("window_start", "timestamp", "2026-03-01T00:00:00")
    as_role(owner_conn, "caselens_readonly", call_case.user_id)
    with pytest.raises(OperationError):
        run_operation(
            owner_conn,
            call_case.case_id,
            "calls_for_phone",
            {
                "phone": HALE,
                "window_start": "2026-03-01T00:00:00",
                "window_end": "2026-03-02T00:00:00",
            },
        )

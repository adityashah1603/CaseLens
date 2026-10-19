"""Tests for migrations 0001-0003 (schema, RLS, grants, hash-chained audit log, admin).

Needs a real Postgres 16 + pgvector. Skipped unless CASELENS_TEST_DATABASE_URL is set to a
`postgresql+psycopg://` URL for a superuser on an EMPTY, throwaway database (the tests run
`alembic downgrade base` against it).

Every test seeds its own SYNTHETIC rows inside a transaction that is always rolled back.
All names, numbers and identifiers below are fictional SYNTHETIC sample values.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import errors, sql
from psycopg.rows import TupleRow
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.db

TEST_DB_ENV = "CASELENS_TEST_DATABASE_URL"
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
GENESIS_HASH = "0" * 64

EXPECTED_TABLES = frozenset(
    {
        "users",
        "cases",
        "case_members",
        "evidence",
        "ingest_jobs",
        "chunks",
        "entities",
        "entity_links",
        "mentions",
        "events",
        "conflicts",
        "call_records",
        "plate_reads",
        "custody_records",
        "audit_log",
        "agent_runs",
    }
)
HELPER_FUNCTIONS = frozenset(
    {
        "app_current_user_id",
        "app_case_role",
        "app_is_member",
        "app_can_bootstrap_case",
        "audit_log_chain",
        "audit_log_block_mutation",
        "app_is_admin",
        "audit_log_verify",
    }
)
HEAD_REVISION = "0003"

Conn = psycopg.Connection[TupleRow]


# ------------------------------------------------------------------------------------------
# Fixtures and helpers
# ------------------------------------------------------------------------------------------


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


def _libpq_url(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def database_url() -> str:
    url = os.environ.get(TEST_DB_ENV)
    if not url:
        pytest.skip(f"{TEST_DB_ENV} is not set")
    return url


@pytest.fixture(scope="module")
def migrated(database_url: str) -> str:
    cfg = _alembic_config(database_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return database_url


@pytest.fixture
def conn(migrated: str) -> Iterator[Conn]:
    with (
        psycopg.connect(_libpq_url(migrated)) as connection,
        connection.transaction(force_rollback=True),
    ):
        yield connection


def _scalar(conn: Conn, query: LiteralString, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def _column(conn: Conn, query: LiteralString, params: tuple[Any, ...] = ()) -> list[Any]:
    return [row[0] for row in conn.execute(query, params).fetchall()]


def _act_as(conn: Conn, role: str | None, user_id: uuid.UUID | None) -> None:
    """Switch the current transaction to `role` (None = superuser) with app.user_id set."""
    conn.execute("RESET ROLE")
    conn.execute(
        "SELECT set_config('app.user_id', %s, true)", ("" if user_id is None else str(user_id),)
    )
    if role is not None:
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))


def _assert_denied(conn: Conn, query: LiteralString, params: tuple[Any, ...] = ()) -> None:
    with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
        conn.execute(query, params)


def _public_tables(conn: Conn) -> set[str]:
    return set(
        _column(conn, "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'")
    )


@dataclass(frozen=True)
class Seed:
    lead_a: uuid.UUID
    analyst_a: uuid.UUID
    viewer_a: uuid.UUID
    lead_b: uuid.UUID
    case_a: uuid.UUID
    case_b: uuid.UUID
    evidence_a: uuid.UUID
    evidence_b: uuid.UUID
    call_a: uuid.UUID
    call_b: uuid.UUID


def _seed(conn: Conn) -> Seed:
    """Insert SYNTHETIC users, two cases, members, evidence, chunks and call records."""
    _act_as(conn, None, None)
    ids = {name: uuid.uuid4() for name in Seed.__dataclass_fields__}
    for key in ("lead_a", "analyst_a", "viewer_a", "lead_b"):
        conn.execute(
            "INSERT INTO users (id, oidc_subject, email, display_name) VALUES (%s, %s, %s, %s)",
            (ids[key], f"synthetic|{key}", f"{key}@example.test", f"SYNTHETIC {key}"),
        )
    for case, lead, name in (
        ("case_a", "lead_a", "SYNTHETIC Riverside Lot"),
        ("case_b", "lead_b", "SYNTHETIC Harbor Street"),
    ):
        conn.execute(
            "INSERT INTO cases (id, name, created_by) VALUES (%s, %s, %s)",
            (ids[case], name, ids[lead]),
        )
    for case, user, role in (
        ("case_a", "lead_a", "lead"),
        ("case_a", "analyst_a", "analyst"),
        ("case_a", "viewer_a", "viewer"),
        ("case_b", "lead_b", "lead"),
    ):
        conn.execute(
            "INSERT INTO case_members (case_id, user_id, role) VALUES (%s, %s, %s)",
            (ids[case], ids[user], role),
        )
    for case, ev, call, uploader, digit in (
        ("case_a", "evidence_a", "call_a", "lead_a", "a"),
        ("case_b", "evidence_b", "call_b", "lead_b", "b"),
    ):
        conn.execute(
            "INSERT INTO evidence (id, case_id, type, original_filename, content_type,"
            " size_bytes, gcs_uri, sha256, uploaded_by)"
            " VALUES (%s, %s, 'csv', 'synthetic_calls.csv', 'text/csv', 128, %s, %s, %s)",
            (ids[ev], ids[case], f"gs://synthetic/{ev}.csv", digit * 64, ids[uploader]),
        )
        conn.execute(
            "INSERT INTO chunks (case_id, evidence_id, modality, chunk_index, text, locator)"
            " VALUES (%s, %s, 'csv', 0, 'SYNTHETIC gray sedan seen near the lot',"
            ' \'{"kind": "record", "table": "call_records", "row_id": 1}\')',
            (ids[case], ids[ev]),
        )
        conn.execute(
            "INSERT INTO call_records (id, case_id, evidence_id, row_id, caller_number,"
            " callee_number, started_at, duration_s, call_type)"
            " VALUES (%s, %s, %s, 1, '+15550100001', '+15550100002',"
            " '2026-03-14T22:41:00Z', 60, 'voice')",
            (ids[call], ids[case], ids[ev]),
        )
    return Seed(**ids)


def _insert_evidence_as(conn: Conn, case_id: uuid.UUID, uploader: uuid.UUID) -> None:
    conn.execute(
        "INSERT INTO evidence (case_id, type, original_filename, content_type, size_bytes,"
        " gcs_uri, sha256, uploaded_by)"
        " VALUES (%s, 'pdf', 'synthetic_report.pdf', 'application/pdf', 10,"
        " 'gs://synthetic/report.pdf', %s, %s)",
        (case_id, uuid.uuid4().hex + uuid.uuid4().hex, uploader),
    )


# ------------------------------------------------------------------------------------------
# Schema lifecycle
# ------------------------------------------------------------------------------------------


def test_upgrade_creates_tables_and_downgrade_roundtrip(migrated: str) -> None:
    cfg = _alembic_config(migrated)
    with psycopg.connect(_libpq_url(migrated), autocommit=True) as c:
        assert _public_tables(c) >= EXPECTED_TABLES
        assert _scalar(c, "SELECT version_num FROM alembic_version") == HEAD_REVISION

        command.downgrade(cfg, "base")
        assert not (EXPECTED_TABLES & _public_tables(c))
        functions = set(
            _column(
                c,
                "SELECT proname FROM pg_catalog.pg_proc p"
                " JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'public'",
            )
        )
        assert not (HELPER_FUNCTIONS & functions)

        command.upgrade(cfg, "head")
        assert _public_tables(c) >= EXPECTED_TABLES


def test_rls_enabled_and_forced_on_every_table(conn: Conn) -> None:
    rows = conn.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_catalog.pg_class c"
        " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'alembic_version'"
    ).fetchall()
    assert {r[0] for r in rows} == EXPECTED_TABLES
    assert all(r[1] and r[2] for r in rows), rows


def test_wall_clock_columns_exist(conn: Conn) -> None:
    columns = set(
        conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND data_type = 'timestamp with time zone'"
        ).fetchall()
    )
    assert {("evidence", "recorded_at"), ("chunks", "t_start"), ("chunks", "t_end")} <= columns


def test_roles_exist_with_expected_attributes(conn: Conn) -> None:
    rows = {
        name: (can_login, bypass_rls)
        for name, can_login, bypass_rls in conn.execute(
            "SELECT rolname, rolcanlogin, rolbypassrls FROM pg_catalog.pg_roles"
            " WHERE rolname LIKE 'caselens_%%'"
        ).fetchall()
    }
    assert rows["caselens_app"] == (False, False)
    assert rows["caselens_readonly"] == (False, False)
    assert rows["caselens_worker"] == (False, True)
    assert rows["caselens_definer"] == (False, True)
    # Nobody may be a member of the definer role (it owns the SECURITY DEFINER helpers).
    assert (
        _scalar(
            conn,
            "SELECT count(*) FROM pg_catalog.pg_auth_members m"
            " JOIN pg_catalog.pg_roles r ON r.oid = m.roleid"
            " WHERE r.rolname = 'caselens_definer'",
        )
        == 0
    )


# ------------------------------------------------------------------------------------------
# Row-level security for the app role
# ------------------------------------------------------------------------------------------


def test_member_sees_only_own_case(conn: Conn) -> None:
    s = _seed(conn)
    _act_as(conn, "caselens_app", s.lead_a)
    assert _column(conn, "SELECT id FROM cases") == [s.case_a]
    assert _column(conn, "SELECT case_id FROM evidence") == [s.case_a]
    assert _column(conn, "SELECT case_id FROM chunks") == [s.case_a]
    assert _column(conn, "SELECT id FROM call_records") == [s.call_a]
    assert set(_column(conn, "SELECT user_id FROM case_members")) == {
        s.lead_a,
        s.analyst_a,
        s.viewer_a,
    }

    _act_as(conn, "caselens_app", s.lead_b)
    assert _column(conn, "SELECT id FROM cases") == [s.case_b]
    assert _column(conn, "SELECT case_id FROM chunks") == [s.case_b]


def test_unset_user_sees_nothing(conn: Conn) -> None:
    s = _seed(conn)
    _act_as(conn, "caselens_app", None)
    for query in (
        "SELECT count(*) FROM cases",
        "SELECT count(*) FROM case_members",
        "SELECT count(*) FROM evidence",
        "SELECT count(*) FROM chunks",
        "SELECT count(*) FROM call_records",
    ):
        assert _scalar(conn, query) == 0, query
    _assert_denied(
        conn,
        "INSERT INTO cases (name, created_by) VALUES ('SYNTHETIC orphan', %s)",
        (s.lead_a,),
    )


def test_evidence_insert_requires_lead_or_analyst(conn: Conn) -> None:
    s = _seed(conn)
    _act_as(conn, "caselens_app", s.viewer_a)
    with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
        _insert_evidence_as(conn, s.case_a, s.viewer_a)

    _act_as(conn, "caselens_app", s.lead_b)
    with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
        _insert_evidence_as(conn, s.case_a, s.lead_b)

    _act_as(conn, "caselens_app", s.lead_a)
    with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
        _insert_evidence_as(conn, s.case_a, s.analyst_a)  # uploaded_by must be self
    _insert_evidence_as(conn, s.case_a, s.lead_a)

    _act_as(conn, "caselens_app", s.analyst_a)
    _insert_evidence_as(conn, s.case_a, s.analyst_a)
    assert _scalar(conn, "SELECT count(*) FROM evidence") == 3


ENQUEUE_JOB: LiteralString = (
    "INSERT INTO ingest_jobs (case_id, evidence_id, stage) VALUES (%s, %s, %s)"
)


def test_ingest_jobs_enqueued_by_lead_or_analyst_only(conn: Conn) -> None:
    s = _seed(conn)
    for outsider in (s.viewer_a, s.lead_b):
        _act_as(conn, "caselens_app", outsider)
        _assert_denied(conn, ENQUEUE_JOB, (s.case_a, s.evidence_a, "schema_infer"))

    _act_as(conn, "caselens_app", s.analyst_a)
    conn.execute(ENQUEUE_JOB, (s.case_a, s.evidence_a, "schema_infer"))
    _assert_denied(  # status is not client-settable: new jobs are always queued
        conn,
        "INSERT INTO ingest_jobs (case_id, evidence_id, stage, status)"
        " VALUES (%s, %s, 'validate', 'succeeded')",
        (s.case_a, s.evidence_a),
    )
    _assert_denied(conn, "UPDATE ingest_jobs SET status = 'succeeded'")

    _act_as(conn, "caselens_app", s.lead_a)
    conn.execute(ENQUEUE_JOB, (s.case_a, s.evidence_a, "validate"))
    assert _column(conn, "SELECT DISTINCT status FROM ingest_jobs") == ["queued"]


def test_evidence_is_immutable_for_app(conn: Conn) -> None:
    s = _seed(conn)
    _act_as(conn, "caselens_app", s.lead_a)
    _assert_denied(
        conn, "UPDATE evidence SET gcs_uri = 'gs://synthetic/x' WHERE id = %s", (s.evidence_a,)
    )
    _assert_denied(conn, "DELETE FROM evidence WHERE id = %s", (s.evidence_a,))
    _assert_denied(
        conn,
        "INSERT INTO chunks (case_id, evidence_id, modality, chunk_index, locator)"
        " VALUES (%s, %s, 'csv', 9, '{\"kind\": \"record\"}')",
        (s.case_a, s.evidence_a),
    )


def test_case_bootstrap_lead(conn: Conn) -> None:
    s = _seed(conn)
    new_case = uuid.uuid4()
    _act_as(conn, "caselens_app", s.lead_b)
    conn.execute(
        "INSERT INTO cases (id, name, created_by) VALUES (%s, 'SYNTHETIC new case', %s)",
        (new_case, s.lead_b),
    )
    assert _scalar(conn, "SELECT count(*) FROM cases WHERE id = %s", (new_case,)) == 0
    conn.execute(
        "INSERT INTO case_members (case_id, user_id, role) VALUES (%s, %s, 'lead')",
        (new_case, s.lead_b),
    )
    assert _scalar(conn, "SELECT count(*) FROM cases WHERE id = %s", (new_case,)) == 1

    # A non-creator cannot bootstrap themselves into someone else's case.
    _act_as(conn, "caselens_app", s.viewer_a)
    _assert_denied(
        conn,
        "INSERT INTO case_members (case_id, user_id, role) VALUES (%s, %s, 'lead')",
        (new_case, s.viewer_a),
    )
    # A viewer cannot add members to their own case.
    _assert_denied(
        conn,
        "INSERT INTO case_members (case_id, user_id, role) VALUES (%s, %s, 'viewer')",
        (s.case_a, s.lead_b),
    )


def test_worker_bypasses_rls(conn: Conn) -> None:
    _seed(conn)
    _act_as(conn, "caselens_worker", None)
    assert _scalar(conn, "SELECT count(*) FROM chunks") == 2
    _assert_denied(conn, "SELECT count(*) FROM audit_log")


# ------------------------------------------------------------------------------------------
# Semantic-layer read-only role
# ------------------------------------------------------------------------------------------


def test_readonly_role_reads_member_records_only(conn: Conn) -> None:
    s = _seed(conn)
    _act_as(conn, "caselens_readonly", s.lead_a)
    assert _column(conn, "SELECT id FROM call_records") == [s.call_a]
    _assert_denied(
        conn,
        "INSERT INTO call_records (case_id, evidence_id, row_id, caller_number, callee_number,"
        " started_at, call_type)"
        " VALUES (%s, %s, 2, '+15550100003', '+15550100004', now(), 'sms')",
        (s.case_a, s.evidence_a),
    )
    _assert_denied(conn, "SELECT count(*) FROM chunks")
    _assert_denied(conn, "SELECT count(*) FROM cases")

    _act_as(conn, "caselens_readonly", None)
    assert _scalar(conn, "SELECT count(*) FROM call_records") == 0


# ------------------------------------------------------------------------------------------
# Audit log
# ------------------------------------------------------------------------------------------


def _audit_rows(conn: Conn) -> list[tuple[Any, ...]]:
    _act_as(conn, None, None)
    return conn.execute(
        "SELECT id, coalesce(user_id::text, ''), coalesce(case_id::text, ''), action,"
        " payload::text, payload_hash, prev_hash, row_hash, ts"
        " FROM audit_log ORDER BY id"
    ).fetchall()


def test_audit_log_hash_chain(conn: Conn) -> None:
    s = _seed(conn)
    assert _scalar(conn, "SELECT count(*) FROM audit_log") == 0
    conn.execute("SET LOCAL TimeZone = 'America/Los_Angeles'")  # trigger must not depend on it
    _act_as(conn, "caselens_app", s.lead_a)
    for action in ("case.view", "agent.ask"):
        conn.execute(
            "INSERT INTO audit_log (user_id, case_id, action, payload) VALUES (%s, %s, %s, %s)",
            (s.lead_a, s.case_a, action, '{"question": "SYNTHETIC gray sedan?"}'),
        )

    rows = _audit_rows(conn)
    assert len(rows) == 2
    assert rows[0][6] == GENESIS_HASH
    assert rows[1][6] == rows[0][7]
    for _id, user_id, case_id, action, payload, payload_hash, prev, row_hash, ts in rows:
        assert payload_hash == hashlib.sha256(payload.encode()).hexdigest()
        ts_utc = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        material = prev + user_id + case_id + action + payload_hash + ts_utc
        assert row_hash == hashlib.sha256(material.encode()).hexdigest()


def test_audit_log_is_append_only(conn: Conn) -> None:
    s = _seed(conn)
    _act_as(conn, "caselens_app", s.lead_a)
    conn.execute(
        "INSERT INTO audit_log (user_id, case_id, action) VALUES (%s, %s, 'case.view')",
        (s.lead_a, s.case_a),
    )
    _assert_denied(conn, "UPDATE audit_log SET action = 'tampered'")
    _assert_denied(conn, "DELETE FROM audit_log")
    _assert_denied(conn, "TRUNCATE audit_log")

    # Even the owner / superuser is blocked by the append-only triggers.
    _act_as(conn, None, None)
    for query in (
        "UPDATE audit_log SET action = 'tampered'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ):
        with pytest.raises(errors.InsufficientPrivilege, match="append-only"), conn.transaction():
            conn.execute(query)
    assert _scalar(conn, "SELECT count(*) FROM audit_log") == 1


def test_audit_log_client_cannot_set_hashes(conn: Conn) -> None:
    s = _seed(conn)
    _act_as(conn, "caselens_app", s.lead_a)
    _assert_denied(
        conn,
        "INSERT INTO audit_log (user_id, case_id, action, row_hash) VALUES (%s, %s, 'x', %s)",
        (s.lead_a, s.case_a, "f" * 64),
    )

    # Even a privileged writer that supplies every computed column gets them overwritten.
    _act_as(conn, None, None)
    conn.execute(
        "INSERT INTO audit_log (id, ts, user_id, case_id, action, payload_hash, prev_hash,"
        " row_hash) VALUES (999999, '2000-01-01T00:00:00Z', %s, %s, 'forged', %s, %s, %s)",
        (s.lead_a, s.case_a, "d" * 64, "e" * 64, "f" * 64),
    )
    row = conn.execute(
        "SELECT id, ts > now() - interval '1 hour', payload_hash, prev_hash, row_hash"
        " FROM audit_log"
    ).fetchone()
    assert row is not None
    assert row[0] != 999999
    assert row[1] is True
    assert row[2] == hashlib.sha256(b"{}").hexdigest()
    assert row[3] == GENESIS_HASH
    assert row[4] != "f" * 64


def test_audit_log_readable_only_by_case_lead(conn: Conn) -> None:
    s = _seed(conn)
    for user in (s.viewer_a, s.lead_a):
        _act_as(conn, "caselens_app", user)
        conn.execute(
            "INSERT INTO audit_log (user_id, case_id, action) VALUES (%s, %s, 'evidence.view')",
            (user, s.case_a),
        )
    _act_as(conn, "caselens_app", s.viewer_a)
    _assert_denied(
        conn,
        "INSERT INTO audit_log (user_id, case_id, action) VALUES (%s, %s, 'spoofed')",
        (s.lead_a, s.case_a),
    )

    _act_as(conn, "caselens_app", s.lead_a)
    assert _scalar(conn, "SELECT count(*) FROM audit_log") == 2
    for user in (s.viewer_a, s.analyst_a, s.lead_b):
        _act_as(conn, "caselens_app", user)
        assert _scalar(conn, "SELECT count(*) FROM audit_log") == 0
    _act_as(conn, "caselens_readonly", s.lead_a)
    _assert_denied(conn, "SELECT count(*) FROM audit_log")


# ------------------------------------------------------------------------------------------
# 0002: platform admin + in-flight agent run updates
# ------------------------------------------------------------------------------------------


def _make_admin(conn: Conn) -> uuid.UUID:
    _act_as(conn, None, None)
    admin = uuid.uuid4()
    conn.execute(
        "INSERT INTO users (id, oidc_subject, email, display_name, is_admin)"
        " VALUES (%s, 'synthetic|admin', 'admin@example.test', 'SYNTHETIC admin', true)",
        (admin,),
    )
    return admin


def _write_audit_rows(conn: Conn, s: Seed) -> None:
    for user, case in ((s.lead_a, s.case_a), (s.lead_b, s.case_b), (s.lead_a, None)):
        _act_as(conn, "caselens_app", user)
        conn.execute(
            "INSERT INTO audit_log (user_id, case_id, action, payload) VALUES (%s, %s, %s, %s)",
            (user, case, "case.view", '{"synthetic": true}'),
        )


def test_admin_reads_audit_across_cases(conn: Conn) -> None:
    s = _seed(conn)
    admin = _make_admin(conn)
    _write_audit_rows(conn, s)

    _act_as(conn, "caselens_app", admin)
    assert _scalar(conn, "SELECT count(*) FROM audit_log") == 3
    _act_as(conn, "caselens_app", s.lead_a)
    assert _scalar(conn, "SELECT count(*) FROM audit_log") == 1


def test_app_cannot_grant_admin(conn: Conn) -> None:
    s = _seed(conn)
    _act_as(conn, "caselens_app", s.lead_a)
    _assert_denied(conn, "UPDATE users SET is_admin = true WHERE id = %s", (s.lead_a,))
    _assert_denied(
        conn,
        "INSERT INTO users (id, oidc_subject, is_admin) VALUES (%s, 'synthetic|x', true)",
        (uuid.uuid4(),),
    )
    assert _scalar(conn, "SELECT public.app_is_admin()") is False


def test_audit_verify_is_admin_only_and_detects_tampering(conn: Conn) -> None:
    s = _seed(conn)
    admin = _make_admin(conn)
    _write_audit_rows(conn, s)

    _act_as(conn, "caselens_app", s.lead_a)
    _assert_denied(conn, "SELECT * FROM public.audit_log_verify()")

    _act_as(conn, "caselens_app", admin)
    assert conn.execute("SELECT * FROM public.audit_log_verify()").fetchone() == (True, 3, None)

    # Simulate an attacker with superuser rights who disables the append-only trigger.
    _act_as(conn, None, None)
    conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete")
    second = _scalar(conn, "SELECT id FROM audit_log ORDER BY id OFFSET 1 LIMIT 1")
    conn.execute("UPDATE audit_log SET payload = '{\"synthetic\": false}' WHERE id = %s", (second,))
    _act_as(conn, "caselens_app", admin)
    assert conn.execute("SELECT * FROM public.audit_log_verify()").fetchone() == (
        False,
        2,
        second,
    )


def test_agent_run_updatable_by_owner_only_while_running(conn: Conn) -> None:
    s = _seed(conn)
    run = uuid.uuid4()
    _act_as(conn, "caselens_app", s.viewer_a)
    conn.execute(
        "INSERT INTO agent_runs (id, case_id, user_id, question) VALUES (%s, %s, %s, %s)",
        (run, s.case_a, s.viewer_a, "SYNTHETIC: who was in the gray sedan?"),
    )

    _act_as(conn, "caselens_app", s.lead_a)
    other = conn.execute("UPDATE agent_runs SET latency_ms = 1 WHERE id = %s", (run,))
    assert other.rowcount == 0

    _act_as(conn, "caselens_app", s.viewer_a)
    _assert_denied(conn, "UPDATE agent_runs SET question = 'rewritten' WHERE id = %s", (run,))
    done = conn.execute(
        "UPDATE agent_runs SET status = 'succeeded', latency_ms = 4200 WHERE id = %s", (run,)
    )
    assert done.rowcount == 1
    frozen = conn.execute("UPDATE agent_runs SET latency_ms = 1 WHERE id = %s", (run,))
    assert frozen.rowcount == 0
    assert _scalar(conn, "SELECT latency_ms FROM agent_runs WHERE id = %s", (run,)) == 4200


# ------------------------------------------------------------------------------------------
# 0003: worker-written system audit events (user_id NULL)
# ------------------------------------------------------------------------------------------


def test_worker_appends_system_audit_events(conn: Conn) -> None:
    s = _seed(conn)
    admin = _make_admin(conn)
    _act_as(conn, "caselens_app", s.lead_a)
    conn.execute(
        "INSERT INTO audit_log (user_id, case_id, action) VALUES (%s, %s, 'evidence.upload')",
        (s.lead_a, s.case_a),
    )

    _act_as(conn, "caselens_worker", None)
    conn.execute(
        "INSERT INTO audit_log (case_id, action, payload) VALUES (%s, %s, %s)",
        (s.case_a, "ingest.stage_succeeded", '{"stage": "ocr", "synthetic": true}'),
    )
    _assert_denied(
        conn,
        "INSERT INTO audit_log (user_id, case_id, action) VALUES (%s, %s, 'spoofed')",
        (s.lead_a, s.case_a),
    )
    _assert_denied(conn, "SELECT count(*) FROM audit_log")
    _assert_denied(conn, "UPDATE audit_log SET action = 'tampered'")
    _assert_denied(conn, "DELETE FROM audit_log")

    _act_as(conn, "caselens_app", s.lead_a)
    rows = conn.execute("SELECT user_id, action FROM audit_log ORDER BY id").fetchall()
    assert rows == [(s.lead_a, "evidence.upload"), (None, "ingest.stage_succeeded")]
    _act_as(conn, "caselens_app", admin)
    assert conn.execute("SELECT * FROM public.audit_log_verify()").fetchone() == (True, 2, None)

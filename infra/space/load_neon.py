"""Prepare the hosted demo database (Neon, D-066) and load the demo case into it.

Steps, all idempotent (safe to re-run):

0. Preflight: the target owner login must be able to create the BYPASSRLS group roles that
   migration 0001 needs (BYPASSRLS + CREATEROLE, or superuser); otherwise stop with the
   fallbacks in infra/space/README.md.
1. Create the group roles exactly as migration 0001 does (so this owner holds ADMIN on them) and
   make the owner a member of caselens_definer, which `ALTER FUNCTION ... OWNER TO
   caselens_definer` and the GRANTs after it require of a non-superuser; then `alembic upgrade
   head`, with CREATE on schema public granted to caselens_definer for the upgrade only.
2. The API login `caselens_api_login`: LOGIN NOINHERIT, never superuser / BYPASSRLS / CREATEROLE
   / CREATEDB / REPLICATION (checked after), member of caselens_app + caselens_readonly only
   (SET, no INHERIT), as infra/docker/postgres/init/02-login-roles.psql does locally. The
   password is sent as a SCRAM-SHA-256 verifier computed here, so the plain text never reaches
   the server (or its statement log).
3. Copy the demo case from the local dev database with COPY, in dependency order, in one
   transaction: the users it references (emails dropped), the case, its non-demo members,
   evidence, ingest_jobs, chunks (with embeddings), entities, entity_links, mentions, events,
   call_records, plate_reads, custody_records. Never agent_runs, conflicts or audit_log: the
   hosted audit chain starts fresh. Then ensure the shared demo viewer (oidc_subject
   `demo:viewer`, is_demo) is a viewer of the case and the case is is_demo. Users, the case and
   evidence are upserted (evidence is immutable: existing rows are kept); the derived tables of
   the case are replaced.
4. Verify: row counts match the source, FORCE RLS is still on for every table.

Environment (never printed):
    CASELENS_PROD_OWNER_DATABASE_URL  target owner URL (Neon: *direct* endpoint, sslmode=require)
    CASELENS_PROD_API_DB_PASSWORD     password for caselens_api_login (at least 16 characters)
    CASELENS_DATABASE_URL             source: the local dev owner URL (read from .env if unset)

    .venv/Scripts/python infra/space/load_neon.py [--case-id UUID] [--skip-migrate]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import secrets
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from caselens_api.auth import DEMO_VIEWER_SUBJECT
from caselens_api.settings import Settings
from caselens_workers.settings import libpq_url

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "apps" / "api"
API_LOGIN = "caselens_api_login"
API_GROUPS = ("caselens_app", "caselens_readonly")
MIN_PASSWORD_LENGTH = 16
SCRAM_ITERATIONS = 4096

Conn = psycopg.Connection[tuple[Any, ...]]

# Same statements as migration 0001's _ROLES (its IF NOT EXISTS then skips them).
GROUP_ROLES = (
    ("caselens_app", sql.SQL("NOLOGIN NOBYPASSRLS")),
    ("caselens_readonly", sql.SQL("NOLOGIN NOBYPASSRLS")),
    ("caselens_worker", sql.SQL("NOLOGIN BYPASSRLS")),
    ("caselens_definer", sql.SQL("NOLOGIN BYPASSRLS")),
)

# Case-scoped tables the pipeline derives from evidence, in insert (FK) order.
DERIVED = (
    "ingest_jobs",
    "chunks",
    "entities",
    "entity_links",
    "mentions",
    "events",
    "call_records",
    "plate_reads",
    "custody_records",
)
# Columns that reference users (the users rows copied are exactly these, minus demo users).
USER_REFS = (
    ("cases", "created_by"),
    ("case_members", "user_id"),
    ("case_members", "added_by"),
    ("evidence", "uploaded_by"),
    ("entity_links", "created_by"),
    ("entity_links", "reverted_by"),
)


def log(message: str) -> None:
    print(message, flush=True)


def describe(url: str) -> str:
    """host/dbname as user, for logs (never the password)."""
    info = conninfo_to_dict(libpq_url(url))
    return f"{info.get('user')}@{info.get('host')}/{info.get('dbname')}"


def scram_verifier(password: str, iterations: int = SCRAM_ITERATIONS) -> str:
    """A PostgreSQL SCRAM-SHA-256 password verifier (RFC 5802 / 7677) for `password`."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


# ---- 0 + 1: preflight, group roles, migrations ---------------------------------------------


def preflight(conn: Conn) -> None:
    row = conn.execute(
        "SELECT current_user, r.rolsuper, r.rolcreaterole, r.rolbypassrls, "
        "current_setting('server_version_num')::int, "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_available_extensions WHERE name = 'vector') "
        "FROM pg_catalog.pg_roles AS r WHERE r.rolname = current_user"
    ).fetchone()
    if row is None:
        raise SystemExit("cannot read the target owner's role attributes")
    name, superuser, createrole, bypassrls, version, has_vector = row
    log(
        f"target owner {name}: superuser={superuser} createrole={createrole} "
        f"bypassrls={bypassrls}, server {version // 10000}, pgvector available={has_vector}"
    )
    if version < 160000:
        raise SystemExit("the target must be PostgreSQL 16 or newer")
    if not has_vector:
        raise SystemExit("the pgvector extension is not available on the target")
    if superuser:
        return
    # Probe instead of trusting the attributes alone (hosted Postgres may grant this through a
    # provider role): create a BYPASSRLS role inside a transaction that is rolled back.
    probe = sql.Identifier(f"caselens_probe_{secrets.token_hex(4)}")
    try:
        with conn.transaction(force_rollback=True):
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN BYPASSRLS").format(probe))
    except psycopg.Error as exc:
        raise SystemExit(
            "the target owner cannot create the BYPASSRLS roles migration 0001 needs "
            f"(caselens_worker, caselens_definer): {exc.diag.message_primary}. "
            "See infra/space/README.md, 'If the owner cannot create BYPASSRLS roles'."
        ) from exc
    log("target owner can create BYPASSRLS roles")


def ensure_group_roles(conn: Conn) -> None:
    for name, attributes in GROUP_ROLES:
        exists = conn.execute(
            "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (name,)
        ).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE ROLE {} {}").format(sql.Identifier(name), attributes))
            log(f"created role {name}")
    # A non-superuser owner must be a member of caselens_definer to hand it the SECURITY
    # DEFINER functions (SET) and to keep granting EXECUTE on them afterwards (INHERIT, i.e. the
    # function owner's privileges). Role attributes such as BYPASSRLS are never inherited, and
    # the owner already holds every privilege the definer has (SELECT on three of its tables).
    is_super = conn.execute(
        "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
    ).fetchone()
    if is_super is not None and not is_super[0]:
        conn.execute(
            sql.SQL("GRANT caselens_definer TO {} WITH INHERIT TRUE, SET TRUE").format(
                sql.Identifier(str(conn.info.user))
            )
        )


def migrate(owner_url: str, conn: Conn) -> None:
    """`alembic upgrade head`. For a non-superuser owner, `ALTER FUNCTION ... OWNER TO
    caselens_definer` also needs the new owner to hold CREATE on the schema, so caselens_definer
    gets it for the duration of the upgrade only (the migrations grant it USAGE alone)."""
    from alembic import command
    from alembic.config import Config

    row = conn.execute(
        "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
    ).fetchone()
    temporary_create = row is not None and not row[0]

    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    sa_url = owner_url
    if sa_url.startswith("postgresql://") or sa_url.startswith("postgres://"):
        sa_url = "postgresql+psycopg://" + sa_url.split("://", 1)[1]
    cfg.set_main_option("sqlalchemy.url", sa_url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    if temporary_create:
        conn.execute("GRANT CREATE ON SCHEMA public TO caselens_definer")
    try:
        command.upgrade(cfg, "head")
    finally:
        if temporary_create:
            conn.execute("REVOKE CREATE ON SCHEMA public FROM caselens_definer")
    check = conn.execute(
        "SELECT has_schema_privilege('caselens_definer', 'public', 'CREATE')"
    ).fetchone()
    if check is None or check[0]:
        raise SystemExit("caselens_definer still holds CREATE on schema public")
    log("migrations: at head")


# ---- 2: API login --------------------------------------------------------------------------


def _is_neon(conn: Conn) -> bool:
    return bool(conn.info.host and conn.info.host.endswith(".neon.tech"))


def ensure_api_login(conn: Conn, password: str) -> None:
    login = sql.Identifier(API_LOGIN)
    # Neon's control plane rejects pre-hashed verifiers ("only supports plaintext passwords")
    # and hashes the password itself; the connection is TLS (sslmode=require), so it is never
    # sent in the clear. Elsewhere send a SCRAM verifier so the server never sees plaintext.
    verifier = sql.Literal(password if _is_neon(conn) else scram_verifier(password))
    exists = conn.execute(
        "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (API_LOGIN,)
    ).fetchone()
    # CREATE ROLE defaults are NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
    # they are asserted below rather than restated (restating some needs extra privileges).
    if exists is None:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}").format(login, verifier))
        log(f"created login {API_LOGIN}")
    else:
        conn.execute(sql.SQL("ALTER ROLE {} LOGIN NOINHERIT PASSWORD {}").format(login, verifier))
        log(f"updated login {API_LOGIN} (password rotated)")
    for group in API_GROUPS:
        conn.execute(
            sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(
                sql.Identifier(group), login
            )
        )
    others = conn.execute(
        "SELECT g.rolname FROM pg_catalog.pg_auth_members AS am "
        "JOIN pg_catalog.pg_roles AS g ON g.oid = am.roleid "
        "JOIN pg_catalog.pg_roles AS u ON u.oid = am.member "
        "WHERE u.rolname = %s AND NOT (g.rolname = ANY(%s))",
        (API_LOGIN, list(API_GROUPS)),
    ).fetchall()
    for (group,) in others:
        conn.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(group), login))
        log(f"revoked {group} from {API_LOGIN}")
    row = conn.execute(
        "SELECT rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication, "
        "rolinherit, rolcanlogin, "
        "array(SELECT g.rolname FROM pg_catalog.pg_auth_members AS am "
        "      JOIN pg_catalog.pg_roles AS g ON g.oid = am.roleid "
        "      WHERE am.member = r.oid ORDER BY 1) "
        "FROM pg_catalog.pg_roles AS r WHERE rolname = %s",
        (API_LOGIN,),
    ).fetchone()
    if row is None or row[0] or row[1] or not row[2] or sorted(row[3]) != sorted(API_GROUPS):
        raise SystemExit(f"{API_LOGIN} does not have the expected least-privilege attributes")
    log(f"{API_LOGIN}: LOGIN NOINHERIT, no elevated attributes, member of {sorted(row[3])}")


# ---- 3: data -------------------------------------------------------------------------------


def columns(conn: Conn, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND is_generated = 'NEVER' "
        "ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def same_columns(src: Conn, dst: Conn, table: str) -> list[str]:
    cols = columns(src, table)
    if not cols or cols != columns(dst, table):
        raise SystemExit(f"public.{table} differs between source and target; migrate both")
    return cols


def copy_table(
    src: Conn,
    dst: Conn,
    table: str,
    select: sql.Composable,
    params: tuple[Any, ...],
    conflict: sql.Composable,
    cols: list[str],
) -> int:
    """COPY the source rows of `select` (yielding `cols`) into a temp table on the target, then
    INSERT them into public.<table> with `conflict` (an ON CONFLICT clause or empty)."""
    stage = sql.Identifier(f"load_{table}")
    col_list = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
    dst.execute(
        sql.SQL("CREATE TEMP TABLE {} (LIKE public.{}) ON COMMIT DROP").format(
            stage, sql.Identifier(table)
        )
    )
    with (
        src.cursor().copy(sql.SQL("COPY ({}) TO STDOUT").format(select), params) as out,
        dst.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN").format(stage, col_list)) as into,
    ):
        for block in out:
            into.write(block)
    cur = dst.execute(
        sql.SQL("INSERT INTO public.{} ({}) SELECT {} FROM {} {}").format(
            sql.Identifier(table), col_list, col_list, stage, conflict
        )
    )
    return cur.rowcount


def case_rows(table: str, cols: list[str]) -> sql.Composed:
    return sql.SQL("SELECT {} FROM public.{} WHERE case_id = %s").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in cols), sql.Identifier(table)
    )


def referenced_users(src: Conn, case_id: uuid.UUID) -> list[uuid.UUID]:
    ids: set[uuid.UUID] = set()
    for table, column in USER_REFS:
        key = "id" if table == "cases" else "case_id"
        rows = src.execute(
            sql.SQL("SELECT DISTINCT {} FROM public.{} WHERE {} = %s AND {} IS NOT NULL").format(
                sql.Identifier(column),
                sql.Identifier(table),
                sql.Identifier(key),
                sql.Identifier(column),
            ),
            (case_id,),
        ).fetchall()
        ids.update(uuid.UUID(str(r[0])) for r in rows)
    demo = src.execute(
        "SELECT id FROM public.users WHERE is_demo AND id = ANY(%s)", (list(ids),)
    ).fetchall()
    # Demo users of the dev database are only members; the hosted demo has its own.
    return sorted(ids - {uuid.UUID(str(r[0])) for r in demo})


def load_case(src: Conn, dst: Conn, case_id: uuid.UUID) -> dict[str, int]:
    nothing = sql.SQL("ON CONFLICT DO NOTHING")
    counts: dict[str, int] = {}

    users = referenced_users(src, case_id)
    user_cols = same_columns(src, dst, "users")
    overrides = {
        "email": sql.SQL("NULL::text"),
        "is_demo": sql.SQL("false"),
        "is_admin": sql.SQL("false"),
    }
    user_select = sql.SQL("SELECT {} FROM public.users WHERE id = ANY(%s)").format(
        sql.SQL(", ").join(
            sql.SQL("{} AS {}").format(overrides[c], sql.Identifier(c))
            if c in overrides
            else sql.Identifier(c)
            for c in user_cols
        )
    )
    copy_table(src, dst, "users", user_select, (users,), nothing, user_cols)
    counts["users"] = len(users)

    case_cols = same_columns(src, dst, "cases")
    updates = sql.SQL(", ").join(
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
        for c in ("name", "description", "status", "updated_at")
    )
    copy_table(
        src,
        dst,
        "cases",
        sql.SQL("SELECT {} FROM public.cases WHERE id = %s").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in case_cols)
        ),
        (case_id,),
        sql.SQL("ON CONFLICT (id) DO UPDATE SET {}").format(updates),
        case_cols,
    )
    dst.execute("UPDATE public.cases SET is_demo = true WHERE id = %s", (case_id,))
    counts["cases"] = 1

    member_cols = same_columns(src, dst, "case_members")
    member_select = sql.SQL("{} AND user_id = ANY(%s)").format(
        case_rows("case_members", member_cols)
    )
    copy_table(src, dst, "case_members", member_select, (case_id, users), nothing, member_cols)

    evidence_cols = same_columns(src, dst, "evidence")
    copy_table(
        src,
        dst,
        "evidence",
        case_rows("evidence", evidence_cols),
        (case_id,),
        nothing,
        evidence_cols,
    )

    for table in reversed(DERIVED):
        dst.execute(
            sql.SQL("DELETE FROM public.{} WHERE case_id = %s").format(sql.Identifier(table)),
            (case_id,),
        )
    for table in DERIVED:
        cols = same_columns(src, dst, table)
        counts[table] = copy_table(
            src, dst, table, case_rows(table, cols), (case_id,), sql.SQL(""), cols
        )

    viewer = dst.execute(
        "INSERT INTO public.users (oidc_subject, display_name, is_demo, is_admin) "
        "VALUES (%s, 'Demo visitor (read-only)', true, false) "
        "ON CONFLICT (oidc_subject) DO UPDATE SET is_demo = true, is_admin = false "
        "RETURNING id",
        (DEMO_VIEWER_SUBJECT,),
    ).fetchone()
    if viewer is None:
        raise SystemExit("could not create the demo viewer user")
    dst.execute(
        "INSERT INTO public.case_members (case_id, user_id, role) VALUES (%s, %s, 'viewer') "
        "ON CONFLICT (case_id, user_id) DO UPDATE SET role = 'viewer'",
        (case_id, viewer[0]),
    )
    # The demo viewer is a viewer of the demo case and of nothing else.
    dst.execute(
        "DELETE FROM public.case_members WHERE user_id = %s AND case_id <> %s",
        (viewer[0], case_id),
    )
    return counts


def verify(src: Conn, dst: Conn, case_id: uuid.UUID) -> None:
    for table in ("evidence", *DERIVED):
        query = sql.SQL("SELECT count(*) FROM public.{} WHERE case_id = %s").format(
            sql.Identifier(table)
        )
        a = src.execute(query, (case_id,)).fetchone()
        b = dst.execute(query, (case_id,)).fetchone()
        if a != b:
            raise SystemExit(f"public.{table}: source has {a}, target has {b}")
    weak = dst.execute(
        "SELECT c.relname FROM pg_catalog.pg_class AS c "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'alembic_version' "
        "AND NOT (c.relrowsecurity AND c.relforcerowsecurity)"
    ).fetchall()
    if weak:
        raise SystemExit(f"row-level security is not forced on: {[r[0] for r in weak]}")
    extra = dst.execute(
        "SELECT count(*) FROM public.evidence WHERE case_id = %s AND NOT (id = ANY(%s))",
        (
            case_id,
            [
                r[0]
                for r in src.execute(
                    "SELECT id FROM public.evidence WHERE case_id = %s", (case_id,)
                ).fetchall()
            ],
        ),
    ).fetchone()
    if extra and extra[0]:
        log(f"warning: the target has {extra[0]} evidence rows that the source no longer has")
    log("verified: row counts match the source; FORCE RLS on every table")


def demo_case(src: Conn, wanted: str | None) -> uuid.UUID:
    if wanted:
        return uuid.UUID(wanted)
    rows = src.execute("SELECT id FROM public.cases WHERE is_demo").fetchall()
    if len(rows) != 1:
        raise SystemExit(f"expected one is_demo case in the source, found {len(rows)}")
    return uuid.UUID(str(rows[0][0]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--case-id", help="case to copy (default: the single is_demo case)")
    parser.add_argument("--skip-migrate", action="store_true", help="skip steps 0-1")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="local .env")
    args = parser.parse_args(argv)

    owner_url = os.environ.get("CASELENS_PROD_OWNER_DATABASE_URL", "")
    password = os.environ.get("CASELENS_PROD_API_DB_PASSWORD", "")
    if not owner_url:
        raise SystemExit("set CASELENS_PROD_OWNER_DATABASE_URL (the target owner URL)")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(
            f"set CASELENS_PROD_API_DB_PASSWORD (at least {MIN_PASSWORD_LENGTH} characters)"
        )
    env_file = args.env_file if args.env_file.is_file() else None
    source_url = Settings(_env_file=env_file).database_url  # type: ignore[call-arg]
    if not source_url:
        raise SystemExit("CASELENS_DATABASE_URL (the local dev owner URL) is not set")
    if libpq_url(source_url) == libpq_url(owner_url):
        raise SystemExit("the source and target databases are the same")
    log(f"source {describe(source_url)} -> target {describe(owner_url)}")

    with psycopg.connect(libpq_url(owner_url), autocommit=True) as dst:
        if not args.skip_migrate:
            preflight(dst)
            ensure_group_roles(dst)
            migrate(owner_url, dst)
        ensure_api_login(dst, password)
    with (
        psycopg.connect(libpq_url(source_url)) as src,
        psycopg.connect(libpq_url(owner_url)) as dst,
    ):
        src.read_only = True
        case_id = demo_case(src, args.case_id)
        with dst.transaction():
            counts = load_case(src, dst, case_id)
        dst.execute("ANALYZE")
        dst.commit()
        verify(src, dst, case_id)
    log(f"loaded case {case_id}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

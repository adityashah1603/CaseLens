"""Platform admin (cross-case audit read + chain verification) and in-flight agent run updates.

Revision ID: 0002
Revises: 0001

CONTRACT: approved by the tech lead after the Week 1 freeze (2026-10-19). SYNTHETIC data only.

* users.is_admin: platform administrator flag. The app role has no INSERT/UPDATE grant on it,
  so only an operator (migrator / superuser) can make someone an admin.
* app_is_admin(): SECURITY DEFINER helper owned by caselens_definer (same pattern as 0001).
* audit_log_select_admin: admins may SELECT every audit row, across all cases and including
  rows with no case_id. Case leads keep their 0001 per-case read.
* audit_log_verify(): admin-only. Walks audit_log in id order and recomputes prev_hash links,
  payload_hash and row_hash with exactly the 0001 formula. Returns
  (valid, checked_rows, first_invalid_id); first_invalid_id is NULL when the chain is intact.
* agent_runs: the user who started a run may UPDATE its result columns while
  status = 'running'. Once it is 'succeeded' or 'failed', it is frozen: the USING clause no
  longer matches and the row becomes read-only.

Every statement is written out literally (no generated SQL).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UPGRADE = (
    # ---- platform admin ----------------------------------------------------------------
    "ALTER TABLE public.users ADD COLUMN is_admin boolean NOT NULL DEFAULT false",
    "GRANT SELECT (id, is_admin) ON public.users TO caselens_definer",
    """
    CREATE FUNCTION public.app_is_admin() RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
    AS $fn$
        SELECT coalesce(
            (SELECT u.is_admin FROM public.users AS u
              WHERE u.id = public.app_current_user_id()),
            false)
    $fn$
    """,
    "REVOKE ALL ON FUNCTION public.app_is_admin() FROM PUBLIC",
    "ALTER FUNCTION public.app_is_admin() OWNER TO caselens_definer",
    "GRANT EXECUTE ON FUNCTION public.app_is_admin() TO caselens_app",
    """
    CREATE POLICY audit_log_select_admin ON public.audit_log FOR SELECT TO caselens_app
        USING (public.app_is_admin())
    """,
    """
    CREATE FUNCTION public.audit_log_verify()
        RETURNS TABLE (valid boolean, checked_rows bigint, first_invalid_id bigint)
        LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
    AS $fn$
    DECLARE
        r              record;
        v_prev         text := pg_catalog.repeat('0', 64);
        v_payload_hash text;
        v_row_hash     text;
        v_count        bigint := 0;
    BEGIN
        IF NOT public.app_is_admin() THEN
            RAISE EXCEPTION USING
                MESSAGE = 'audit_log_verify requires a platform admin',
                ERRCODE = 'insufficient_privilege';
        END IF;
        FOR r IN SELECT a.id, a.ts, a.user_id, a.case_id, a.action, a.payload,
                        a.payload_hash, a.prev_hash, a.row_hash
                   FROM public.audit_log AS a ORDER BY a.id
        LOOP
            v_count := v_count + 1;
            v_payload_hash := pg_catalog.encode(
                public.digest(r.payload::text, 'sha256'), 'hex');
            v_row_hash := pg_catalog.encode(
                public.digest(
                    v_prev
                    || coalesce(r.user_id::text, '')
                    || coalesce(r.case_id::text, '')
                    || r.action
                    || v_payload_hash
                    || pg_catalog.to_char(r.ts AT TIME ZONE 'UTC',
                                          'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                    'sha256'),
                'hex');
            IF r.prev_hash::text <> v_prev
               OR r.payload_hash::text <> v_payload_hash
               OR r.row_hash::text <> v_row_hash THEN
                RETURN QUERY SELECT false, v_count, r.id;
                RETURN;
            END IF;
            v_prev := r.row_hash::text;
        END LOOP;
        RETURN QUERY SELECT true, v_count, NULL::bigint;
    END
    $fn$
    """,
    "REVOKE ALL ON FUNCTION public.audit_log_verify() FROM PUBLIC",
    "ALTER FUNCTION public.audit_log_verify() OWNER TO caselens_definer",
    "GRANT EXECUTE ON FUNCTION public.audit_log_verify() TO caselens_app",
    # ---- agent runs: the owner fills in results while the run is in flight ---------------
    """
    GRANT UPDATE (status, model, plan, tool_calls, answer, citations, latency_ms,
                  input_tokens, output_tokens, cost_usd)
        ON public.agent_runs TO caselens_app
    """,
    """
    CREATE POLICY agent_runs_update_own_running ON public.agent_runs
        FOR UPDATE TO caselens_app
        USING (user_id = public.app_current_user_id() AND status = 'running')
        WITH CHECK (
            user_id = public.app_current_user_id()
            AND public.app_is_member(case_id)
        )
    """,
)

_DOWNGRADE = (
    "DROP POLICY IF EXISTS agent_runs_update_own_running ON public.agent_runs",
    """
    REVOKE UPDATE (status, model, plan, tool_calls, answer, citations, latency_ms,
                   input_tokens, output_tokens, cost_usd)
        ON public.agent_runs FROM caselens_app
    """,
    "DROP FUNCTION IF EXISTS public.audit_log_verify()",
    "DROP POLICY IF EXISTS audit_log_select_admin ON public.audit_log",
    "DROP FUNCTION IF EXISTS public.app_is_admin()",
    "REVOKE SELECT (id, is_admin) ON public.users FROM caselens_definer",
    "ALTER TABLE public.users DROP COLUMN IF EXISTS is_admin",
)


def upgrade() -> None:
    for stmt in _UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _DOWNGRADE:
        op.execute(stmt)

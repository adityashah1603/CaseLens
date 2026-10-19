"""Initial CaseLens schema: tables, row-level security, roles, grants, hash-chained audit log.

Revision ID: 0001
Revises: (none)

CONTRACT: this migration is the frozen DB schema + RLS contract. Change only with tech-lead
approval. All data stored in this schema in this project is SYNTHETIC data only (fictional demo
cases); never load real case data or real personal data.

Security model (the database is the second wall behind the API's RBAC):

* Group roles (NOLOGIN, cluster-global, created idempotently; login users are granted
  membership by infra / docker compose, never here):
    - caselens_app       API request path. Subject to RLS on every table.
    - caselens_readonly  Semantic-layer SQL. SELECT only on call_records, plate_reads,
                         custody_records, entities; still subject to RLS.
    - caselens_worker    Ingest workers. BYPASSRLS; writes only the derived/pipeline tables.
    - caselens_definer   NOLOGIN, BYPASSRLS, nobody is a member. Owns the SECURITY DEFINER
                         helpers and the audit chain trigger so that they can read
                         case_members / cases / audit_log without recursing into the RLS
                         policies that call them (FORCE RLS also binds a non-superuser table
                         owner). It holds only SELECT on those three tables.
* Identity: `current_setting('app.user_id', true)::uuid`, read through app_current_user_id().
  Unset or empty -> NULL -> every policy evaluates to false/NULL -> no rows (default deny).
* RLS is ENABLEd and FORCEd on every table. Policies name their roles explicitly; any role
  without a matching policy sees nothing.
* Least-privilege GRANTs, column-scoped where an UPDATE/INSERT only needs some columns.
  evidence is immutable (no UPDATE/DELETE for anyone). audit_log has no UPDATE/DELETE grant for
  anyone, and triggers additionally block UPDATE / DELETE / TRUNCATE even for the owner.
* audit_log is hash-chained by a BEFORE INSERT trigger that serializes writers with an
  advisory lock and overwrites id, ts, prev_hash, payload_hash and row_hash, so clients can
  never choose them (the app role is not even granted INSERT on those columns). Exact formula
  (sha256(x) = lowercase hex of pgcrypto digest(x, 'sha256'); || is text concatenation):
      ts_utc       = to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                     e.g. 2026-03-14T22:41:05.123456Z (independent of TimeZone / DateStyle)
      prev_hash    = row_hash of the previous row by id; genesis row: 64 '0' characters
      payload_hash = sha256(payload::text)       -- jsonb text output is canonical
      row_hash     = sha256(prev_hash || coalesce(user_id::text, '') || coalesce(case_id::text, '')
                            || action || payload_hash || ts_utc)
  user_id / case_id render as lowercase hyphenated UUIDs (uuid::text). A verifier walks the rows
  in id order and recomputes both hashes plus the prev_hash links.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------------------------
# Extensions and roles
# --------------------------------------------------------------------------------------------

_EXTENSIONS = (
    "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public",
    "CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public",
)

_ROLES = (
    """
    DO $roles$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'caselens_app') THEN
            CREATE ROLE caselens_app NOLOGIN;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'caselens_readonly') THEN
            CREATE ROLE caselens_readonly NOLOGIN;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'caselens_worker') THEN
            CREATE ROLE caselens_worker NOLOGIN BYPASSRLS;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'caselens_definer') THEN
            CREATE ROLE caselens_definer NOLOGIN BYPASSRLS;
        END IF;
    END
    $roles$
    """,
    # Re-assert attributes in case the roles pre-existed with different ones.
    "ALTER ROLE caselens_app NOLOGIN NOBYPASSRLS",
    "ALTER ROLE caselens_readonly NOLOGIN NOBYPASSRLS",
    "ALTER ROLE caselens_worker NOLOGIN BYPASSRLS",
    "ALTER ROLE caselens_definer NOLOGIN BYPASSRLS",
)

# --------------------------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------------------------

_TABLES = (
    """
    CREATE TABLE public.users (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        oidc_subject  text NOT NULL UNIQUE CHECK (length(oidc_subject) > 0),
        email         text UNIQUE,
        display_name  text,
        is_demo       boolean NOT NULL DEFAULT false,
        created_at    timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE public.cases (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name         text NOT NULL CHECK (length(name) > 0),
        description  text,
        status       text NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'closed', 'archived')),
        is_demo      boolean NOT NULL DEFAULT false,
        created_by   uuid NOT NULL REFERENCES public.users (id),
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX cases_created_by_idx ON public.cases (created_by)",
    """
    CREATE TABLE public.case_members (
        case_id     uuid NOT NULL REFERENCES public.cases (id),
        user_id     uuid NOT NULL REFERENCES public.users (id),
        role        text NOT NULL CHECK (role IN ('lead', 'analyst', 'viewer')),
        added_by    uuid REFERENCES public.users (id),
        created_at  timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (case_id, user_id)
    )
    """,
    "CREATE INDEX case_members_user_id_idx ON public.case_members (user_id)",
    """
    CREATE TABLE public.evidence (
        id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id            uuid NOT NULL REFERENCES public.cases (id),
        type               text NOT NULL CHECK (type IN ('pdf', 'audio', 'video', 'csv')),
        original_filename  text NOT NULL CHECK (length(original_filename) > 0),
        content_type       text NOT NULL,
        size_bytes         bigint NOT NULL CHECK (size_bytes >= 0),
        gcs_uri            text NOT NULL,
        sha256             char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        uploaded_by        uuid NOT NULL REFERENCES public.users (id),
        recorded_at        timestamptz,
        chain_of_custody   jsonb NOT NULL DEFAULT '[]'::jsonb
                           CHECK (jsonb_typeof(chain_of_custody) = 'array'),
        created_at         timestamptz NOT NULL DEFAULT now(),
        UNIQUE (case_id, sha256),
        UNIQUE (id, case_id)
    )
    """,
    """
    CREATE TABLE public.ingest_jobs (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id      uuid NOT NULL REFERENCES public.cases (id),
        evidence_id  uuid NOT NULL,
        stage        text NOT NULL CHECK (length(stage) > 0),
        status       text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'dead_lettered')),
        attempts     integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        error        text,
        started_at   timestamptz,
        finished_at  timestamptz,
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now(),
        UNIQUE (evidence_id, stage),
        FOREIGN KEY (evidence_id, case_id) REFERENCES public.evidence (id, case_id)
    )
    """,
    "CREATE INDEX ingest_jobs_case_status_idx ON public.ingest_jobs (case_id, status)",
    """
    CREATE TABLE public.chunks (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id      uuid NOT NULL REFERENCES public.cases (id),
        evidence_id  uuid NOT NULL,
        modality     text NOT NULL CHECK (modality IN ('pdf', 'audio', 'video', 'csv')),
        chunk_index  integer NOT NULL CHECK (chunk_index >= 0),
        text         text,
        embedding    vector(768),
        t_start      timestamptz,
        t_end        timestamptz,
        tsv          tsvector GENERATED ALWAYS AS
                     (to_tsvector('english', coalesce(text, ''))) STORED,
        locator      jsonb NOT NULL CHECK (
                         jsonb_typeof(locator) = 'object'
                         AND locator ->> 'kind' IN ('pdf', 'audio', 'video', 'record')
                     ),
        created_at   timestamptz NOT NULL DEFAULT now(),
        CHECK (t_end IS NULL OR t_start IS NULL OR t_end >= t_start),
        UNIQUE (evidence_id, chunk_index),
        UNIQUE (id, case_id),
        FOREIGN KEY (evidence_id, case_id) REFERENCES public.evidence (id, case_id)
    )
    """,
    "CREATE INDEX chunks_case_t_start_idx ON public.chunks (case_id, t_start)",
    """
    CREATE INDEX chunks_embedding_hnsw_idx ON public.chunks
        USING hnsw (embedding vector_cosine_ops)
    """,
    "CREATE INDEX chunks_tsv_gin_idx ON public.chunks USING gin (tsv)",
    """
    CREATE TABLE public.entities (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id         uuid NOT NULL REFERENCES public.cases (id),
        type            text NOT NULL
                        CHECK (type IN ('person', 'vehicle', 'phone', 'location', 'weapon')),
        canonical_name  text NOT NULL CHECK (length(canonical_name) > 0),
        normalized_key  text,
        attributes      jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(attributes) = 'object'),
        created_at      timestamptz NOT NULL DEFAULT now(),
        UNIQUE (id, case_id)
    )
    """,
    """
    CREATE INDEX entities_case_type_key_idx ON public.entities (case_id, type, normalized_key)
    """,
    """
    CREATE INDEX entities_canonical_name_trgm_idx ON public.entities
        USING gin (canonical_name gin_trgm_ops)
    """,
    """
    CREATE TABLE public.entity_links (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id         uuid NOT NULL REFERENCES public.cases (id),
        from_entity_id  uuid NOT NULL,
        to_entity_id    uuid NOT NULL,
        method          text NOT NULL CHECK (method IN ('exact', 'fuzzy', 'llm', 'manual')),
        confidence      numeric CHECK (confidence >= 0 AND confidence <= 1),
        status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'reverted')),
        created_by      uuid REFERENCES public.users (id),
        created_at      timestamptz NOT NULL DEFAULT now(),
        reverted_at     timestamptz,
        reverted_by     uuid REFERENCES public.users (id),
        CHECK (from_entity_id <> to_entity_id),
        CHECK (status = 'active' OR reverted_at IS NOT NULL),
        FOREIGN KEY (from_entity_id, case_id) REFERENCES public.entities (id, case_id),
        FOREIGN KEY (to_entity_id, case_id) REFERENCES public.entities (id, case_id)
    )
    """,
    "CREATE INDEX entity_links_case_id_idx ON public.entity_links (case_id)",
    "CREATE INDEX entity_links_from_idx ON public.entity_links (from_entity_id)",
    "CREATE INDEX entity_links_to_idx ON public.entity_links (to_entity_id)",
    """
    CREATE TABLE public.mentions (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id       uuid NOT NULL REFERENCES public.cases (id),
        entity_id     uuid NOT NULL,
        chunk_id      uuid NOT NULL,
        surface_text  text NOT NULL,
        confidence    numeric CHECK (confidence >= 0 AND confidence <= 1),
        needs_review  boolean NOT NULL DEFAULT false,
        created_at    timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (entity_id, case_id) REFERENCES public.entities (id, case_id),
        FOREIGN KEY (chunk_id, case_id) REFERENCES public.chunks (id, case_id)
    )
    """,
    "CREATE INDEX mentions_case_id_idx ON public.mentions (case_id)",
    "CREATE INDEX mentions_entity_id_idx ON public.mentions (entity_id)",
    "CREATE INDEX mentions_chunk_id_idx ON public.mentions (chunk_id)",
    """
    CREATE TABLE public.events (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id           uuid NOT NULL REFERENCES public.cases (id),
        t_start           timestamptz NOT NULL,
        t_end             timestamptz,
        location_id       uuid,
        description       text NOT NULL,
        source_chunk_ids  uuid[] NOT NULL DEFAULT '{}',
        confidence        numeric CHECK (confidence >= 0 AND confidence <= 1),
        needs_review      boolean NOT NULL DEFAULT false,
        created_at        timestamptz NOT NULL DEFAULT now(),
        CHECK (t_end IS NULL OR t_end >= t_start),
        FOREIGN KEY (location_id, case_id) REFERENCES public.entities (id, case_id)
    )
    """,
    "CREATE INDEX events_case_t_start_idx ON public.events (case_id, t_start)",
    "CREATE INDEX events_location_id_idx ON public.events (location_id)",
    """
    CREATE TABLE public.agent_runs (
        id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id        uuid NOT NULL REFERENCES public.cases (id),
        user_id        uuid NOT NULL REFERENCES public.users (id),
        question       text NOT NULL CHECK (length(question) > 0),
        status         text NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'succeeded', 'failed')),
        model          text,
        plan           jsonb,
        tool_calls     jsonb NOT NULL DEFAULT '[]'::jsonb
                       CHECK (jsonb_typeof(tool_calls) = 'array'),
        answer         jsonb,
        citations      jsonb NOT NULL DEFAULT '[]'::jsonb
                       CHECK (jsonb_typeof(citations) = 'array'),
        latency_ms     integer CHECK (latency_ms >= 0),
        input_tokens   integer CHECK (input_tokens >= 0),
        output_tokens  integer CHECK (output_tokens >= 0),
        cost_usd       numeric(10, 6) CHECK (cost_usd >= 0),
        created_at     timestamptz NOT NULL DEFAULT now(),
        UNIQUE (id, case_id)
    )
    """,
    "CREATE INDEX agent_runs_case_created_idx ON public.agent_runs (case_id, created_at)",
    "CREATE INDEX agent_runs_user_id_idx ON public.agent_runs (user_id)",
    """
    CREATE TABLE public.conflicts (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id       uuid NOT NULL REFERENCES public.cases (id),
        agent_run_id  uuid,
        description   text NOT NULL,
        claim_a       jsonb NOT NULL CHECK (jsonb_typeof(claim_a) = 'object'),
        claim_b       jsonb NOT NULL CHECK (jsonb_typeof(claim_b) = 'object'),
        status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'reviewed')),
        created_at    timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (agent_run_id, case_id) REFERENCES public.agent_runs (id, case_id)
    )
    """,
    "CREATE INDEX conflicts_case_id_idx ON public.conflicts (case_id)",
    """
    CREATE TABLE public.call_records (
        id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id        uuid NOT NULL REFERENCES public.cases (id),
        evidence_id    uuid NOT NULL,
        row_id         integer NOT NULL CHECK (row_id >= 0),
        caller_number  text NOT NULL,
        callee_number  text NOT NULL,
        started_at     timestamptz NOT NULL,
        duration_s     integer CHECK (duration_s >= 0),
        call_type      text NOT NULL CHECK (call_type IN ('voice', 'sms')),
        cell_tower_id  text,
        UNIQUE (evidence_id, row_id),
        FOREIGN KEY (evidence_id, case_id) REFERENCES public.evidence (id, case_id)
    )
    """,
    "CREATE INDEX call_records_case_started_idx ON public.call_records (case_id, started_at)",
    "CREATE INDEX call_records_caller_idx ON public.call_records (case_id, caller_number)",
    "CREATE INDEX call_records_callee_idx ON public.call_records (case_id, callee_number)",
    """
    CREATE TABLE public.plate_reads (
        id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id        uuid NOT NULL REFERENCES public.cases (id),
        evidence_id    uuid NOT NULL,
        row_id         integer NOT NULL CHECK (row_id >= 0),
        plate          text NOT NULL,
        plate_state    text,
        read_at        timestamptz NOT NULL,
        camera_id      text,
        location_name  text,
        latitude       double precision CHECK (latitude BETWEEN -90 AND 90),
        longitude      double precision CHECK (longitude BETWEEN -180 AND 180),
        confidence     numeric CHECK (confidence >= 0 AND confidence <= 1),
        UNIQUE (evidence_id, row_id),
        FOREIGN KEY (evidence_id, case_id) REFERENCES public.evidence (id, case_id)
    )
    """,
    "CREATE INDEX plate_reads_case_plate_idx ON public.plate_reads (case_id, plate, read_at)",
    """
    CREATE INDEX plate_reads_case_location_idx ON public.plate_reads
        (case_id, location_name, read_at)
    """,
    """
    CREATE TABLE public.custody_records (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id             uuid NOT NULL REFERENCES public.cases (id),
        evidence_id         uuid NOT NULL,
        row_id              integer NOT NULL CHECK (row_id >= 0),
        item_id             text NOT NULL,
        description         text,
        collected_by        text,
        collected_at        timestamptz,
        collected_location  text,
        custody_chain       text,
        UNIQUE (evidence_id, row_id),
        FOREIGN KEY (evidence_id, case_id) REFERENCES public.evidence (id, case_id)
    )
    """,
    "CREATE INDEX custody_records_case_item_idx ON public.custody_records (case_id, item_id)",
    """
    CREATE TABLE public.audit_log (
        id            bigserial PRIMARY KEY,
        ts            timestamptz NOT NULL DEFAULT now(),
        user_id       uuid REFERENCES public.users (id),
        case_id       uuid REFERENCES public.cases (id),
        action        text NOT NULL CHECK (length(action) > 0),
        payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
        payload_hash  char(64) NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
        prev_hash     char(64) NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
        row_hash      char(64) NOT NULL UNIQUE CHECK (row_hash ~ '^[0-9a-f]{64}$')
    )
    """,
    "CREATE INDEX audit_log_case_ts_idx ON public.audit_log (case_id, ts)",
    "CREATE INDEX audit_log_user_ts_idx ON public.audit_log (user_id, ts)",
)

# Every table gets ENABLE + FORCE row-level security.
_ALL_TABLES = (
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
    "agent_runs",
    "conflicts",
    "call_records",
    "plate_reads",
    "custody_records",
    "audit_log",
)

# Pipeline-derived tables: members may read, caselens_worker (BYPASSRLS) writes. Exception:
# the app may INSERT queued ingest_jobs (lead/analyst) when evidence is uploaded.
_DERIVED_TABLES = (
    "ingest_jobs",
    "chunks",
    "entities",
    "mentions",
    "events",
    "call_records",
    "plate_reads",
    "custody_records",
)

# Tables the semantic-layer role may read.
_READONLY_TABLES = ("call_records", "plate_reads", "custody_records", "entities")

# --------------------------------------------------------------------------------------------
# SECURITY DEFINER helpers (owned by caselens_definer; search_path pinned; objects qualified)
# --------------------------------------------------------------------------------------------

_HELPER_FUNCTIONS = (
    """
    CREATE FUNCTION public.app_current_user_id() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
    AS $fn$
        SELECT nullif(pg_catalog.current_setting('app.user_id', true), '')::uuid
    $fn$
    """,
    """
    CREATE FUNCTION public.app_case_role(p_case_id uuid) RETURNS text
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
    AS $fn$
        SELECT m.role
          FROM public.case_members AS m
         WHERE m.case_id = p_case_id
           AND m.user_id = public.app_current_user_id()
    $fn$
    """,
    """
    CREATE FUNCTION public.app_is_member(p_case_id uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
    AS $fn$
        SELECT public.app_case_role(p_case_id) IS NOT NULL
    $fn$
    """,
    # Bootstrap check for the first lead of a new case: the caller created the case and the
    # case has no members yet (so a removed creator cannot re-add themselves later).
    """
    CREATE FUNCTION public.app_can_bootstrap_case(p_case_id uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
    AS $fn$
        SELECT EXISTS (
                   SELECT 1 FROM public.cases AS c
                    WHERE c.id = p_case_id
                      AND c.created_by = public.app_current_user_id()
               )
           AND NOT EXISTS (
                   SELECT 1 FROM public.case_members AS m WHERE m.case_id = p_case_id
               )
    $fn$
    """,
)

_HELPER_SIGNATURES = (
    "public.app_current_user_id()",
    "public.app_case_role(uuid)",
    "public.app_is_member(uuid)",
    "public.app_can_bootstrap_case(uuid)",
)

# --------------------------------------------------------------------------------------------
# Audit log: hash chain + append-only enforcement
# --------------------------------------------------------------------------------------------

_AUDIT_FUNCTIONS = (
    """
    CREATE FUNCTION public.audit_log_chain() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
    AS $fn$
    DECLARE
        v_prev text;
    BEGIN
        -- Serialize chain writers for the rest of the transaction (constant key 'CLAUDIT1').
        PERFORM pg_catalog.pg_advisory_xact_lock(4849535454534849);
        SELECT a.row_hash INTO v_prev FROM public.audit_log AS a ORDER BY a.id DESC LIMIT 1;
        -- Assign id and ts after taking the lock so id order == chain order == ts order.
        NEW.id := pg_catalog.nextval('public.audit_log_id_seq'::pg_catalog.regclass);
        NEW.ts := pg_catalog.clock_timestamp();
        NEW.payload := coalesce(NEW.payload, '{}'::jsonb);
        NEW.prev_hash := coalesce(v_prev, pg_catalog.repeat('0', 64));
        NEW.payload_hash := pg_catalog.encode(
            public.digest(NEW.payload::text, 'sha256'), 'hex');
        NEW.row_hash := pg_catalog.encode(
            public.digest(
                NEW.prev_hash::text
                || coalesce(NEW.user_id::text, '')
                || coalesce(NEW.case_id::text, '')
                || NEW.action
                || NEW.payload_hash::text
                || pg_catalog.to_char(NEW.ts AT TIME ZONE 'UTC',
                                      'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'sha256'),
            'hex');
        RETURN NEW;
    END
    $fn$
    """,
    """
    CREATE FUNCTION public.audit_log_block_mutation() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
    AS $fn$
    BEGIN
        RAISE EXCEPTION USING
            MESSAGE = 'audit_log is append-only: ' || TG_OP || ' is not allowed',
            ERRCODE = 'insufficient_privilege';
    END
    $fn$
    """,
    """
    CREATE TRIGGER audit_log_chain_bi BEFORE INSERT ON public.audit_log
        FOR EACH ROW EXECUTE FUNCTION public.audit_log_chain()
    """,
    """
    CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON public.audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION public.audit_log_block_mutation()
    """,
    """
    CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON public.audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION public.audit_log_block_mutation()
    """,
)

_FUNCTION_OWNERSHIP = (
    # The definer role must be able to read what the helpers / chain trigger read.
    "GRANT USAGE ON SCHEMA public TO caselens_definer",
    "GRANT SELECT ON public.case_members, public.cases, public.audit_log TO caselens_definer",
    "GRANT USAGE ON SEQUENCE public.audit_log_id_seq TO caselens_definer",
    *(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC" for sig in _HELPER_SIGNATURES),
    *(f"ALTER FUNCTION {sig} OWNER TO caselens_definer" for sig in _HELPER_SIGNATURES),
    "REVOKE ALL ON FUNCTION public.audit_log_chain() FROM PUBLIC",
    "ALTER FUNCTION public.audit_log_chain() OWNER TO caselens_definer",
    "REVOKE ALL ON FUNCTION public.audit_log_block_mutation() FROM PUBLIC",
    *(
        f"GRANT EXECUTE ON FUNCTION {sig} TO caselens_app, caselens_readonly, caselens_worker"
        for sig in _HELPER_SIGNATURES
    ),
)

# --------------------------------------------------------------------------------------------
# Row-level security policies
# --------------------------------------------------------------------------------------------

_RLS_ENABLE = tuple(
    stmt
    for table in _ALL_TABLES
    for stmt in (
        f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY",
    )
)


def _select_member_policy(table: str) -> str:
    roles = "caselens_app, caselens_readonly" if table in _READONLY_TABLES else "caselens_app"
    return (
        f"CREATE POLICY {table}_select_member ON public.{table} FOR SELECT TO {roles} "
        "USING (public.app_is_member(case_id))"
    )


_POLICIES = (
    # users: no case scope. The API resolves oidc_subject -> user before app.user_id is known,
    # so SELECT/INSERT are open to the app role (column grants limit INSERT); UPDATE own row.
    "CREATE POLICY users_select_app ON public.users FOR SELECT TO caselens_app USING (true)",
    "CREATE POLICY users_insert_app ON public.users FOR INSERT TO caselens_app WITH CHECK (true)",
    """
    CREATE POLICY users_update_own ON public.users FOR UPDATE TO caselens_app
        USING (id = public.app_current_user_id())
        WITH CHECK (id = public.app_current_user_id())
    """,
    # cases
    """
    CREATE POLICY cases_select_member ON public.cases FOR SELECT TO caselens_app
        USING (public.app_is_member(id))
    """,
    """
    CREATE POLICY cases_insert_creator ON public.cases FOR INSERT TO caselens_app
        WITH CHECK (created_by = public.app_current_user_id())
    """,
    """
    CREATE POLICY cases_update_lead ON public.cases FOR UPDATE TO caselens_app
        USING (public.app_case_role(id) = 'lead')
        WITH CHECK (public.app_case_role(id) = 'lead')
    """,
    # case_members
    """
    CREATE POLICY case_members_select_member ON public.case_members FOR SELECT TO caselens_app
        USING (public.app_is_member(case_id))
    """,
    """
    CREATE POLICY case_members_insert_lead ON public.case_members FOR INSERT TO caselens_app
        WITH CHECK (public.app_case_role(case_id) = 'lead')
    """,
    """
    CREATE POLICY case_members_insert_bootstrap ON public.case_members
        FOR INSERT TO caselens_app
        WITH CHECK (
            user_id = public.app_current_user_id()
            AND role = 'lead'
            AND public.app_can_bootstrap_case(case_id)
        )
    """,
    """
    CREATE POLICY case_members_update_lead ON public.case_members FOR UPDATE TO caselens_app
        USING (public.app_case_role(case_id) = 'lead')
        WITH CHECK (public.app_case_role(case_id) = 'lead')
    """,
    """
    CREATE POLICY case_members_delete_lead ON public.case_members FOR DELETE TO caselens_app
        USING (public.app_case_role(case_id) = 'lead')
    """,
    # evidence (immutable: no UPDATE/DELETE policy or grant)
    """
    CREATE POLICY evidence_select_member ON public.evidence FOR SELECT TO caselens_app
        USING (public.app_is_member(case_id))
    """,
    """
    CREATE POLICY evidence_insert_lead_analyst ON public.evidence FOR INSERT TO caselens_app
        WITH CHECK (
            public.app_case_role(case_id) IN ('lead', 'analyst')
            AND uploaded_by = public.app_current_user_id()
        )
    """,
    # derived tables: member read (app; readonly where granted). Worker bypasses RLS.
    *(_select_member_policy(table) for table in _DERIVED_TABLES),
    # ingest_jobs: the API enqueues stage jobs at upload (lead/analyst); workers update them.
    """
    CREATE POLICY ingest_jobs_insert_lead_analyst ON public.ingest_jobs
        FOR INSERT TO caselens_app
        WITH CHECK (public.app_case_role(case_id) IN ('lead', 'analyst'))
    """,
    # entity_links: member read; lead/analyst may revert (UPDATE limited by column grant).
    """
    CREATE POLICY entity_links_select_member ON public.entity_links FOR SELECT TO caselens_app
        USING (public.app_is_member(case_id))
    """,
    """
    CREATE POLICY entity_links_update_lead_analyst ON public.entity_links
        FOR UPDATE TO caselens_app
        USING (public.app_case_role(case_id) IN ('lead', 'analyst'))
        WITH CHECK (public.app_case_role(case_id) IN ('lead', 'analyst'))
    """,
    # conflicts / agent_runs: any member may read and insert.
    """
    CREATE POLICY conflicts_select_member ON public.conflicts FOR SELECT TO caselens_app
        USING (public.app_is_member(case_id))
    """,
    """
    CREATE POLICY conflicts_insert_member ON public.conflicts FOR INSERT TO caselens_app
        WITH CHECK (public.app_is_member(case_id))
    """,
    """
    CREATE POLICY agent_runs_select_member ON public.agent_runs FOR SELECT TO caselens_app
        USING (public.app_is_member(case_id))
    """,
    """
    CREATE POLICY agent_runs_insert_member ON public.agent_runs FOR INSERT TO caselens_app
        WITH CHECK (
            public.app_is_member(case_id)
            AND user_id = public.app_current_user_id()
        )
    """,
    # audit_log: app inserts rows for the current user; only the case lead may read them.
    """
    CREATE POLICY audit_log_insert_self ON public.audit_log FOR INSERT TO caselens_app
        WITH CHECK (user_id = public.app_current_user_id())
    """,
    """
    CREATE POLICY audit_log_select_lead ON public.audit_log FOR SELECT TO caselens_app
        USING (case_id IS NOT NULL AND public.app_case_role(case_id) = 'lead')
    """,
)

# --------------------------------------------------------------------------------------------
# Grants (least privilege). Nobody is granted UPDATE or DELETE on audit_log or evidence.
# --------------------------------------------------------------------------------------------

_GRANTS = (
    "GRANT USAGE ON SCHEMA public TO caselens_app, caselens_readonly, caselens_worker",
    # caselens_app
    "GRANT SELECT ON public.users TO caselens_app",
    "GRANT INSERT (id, oidc_subject, email, display_name) ON public.users TO caselens_app",
    "GRANT UPDATE (email, display_name) ON public.users TO caselens_app",
    "GRANT SELECT ON public.cases TO caselens_app",
    "GRANT INSERT (id, name, description, status, created_by) ON public.cases TO caselens_app",
    "GRANT UPDATE (name, description, status, updated_at) ON public.cases TO caselens_app",
    "GRANT SELECT, DELETE ON public.case_members TO caselens_app",
    "GRANT INSERT (case_id, user_id, role, added_by) ON public.case_members TO caselens_app",
    "GRANT UPDATE (role) ON public.case_members TO caselens_app",
    "GRANT SELECT ON public.evidence TO caselens_app",
    """
    GRANT INSERT (id, case_id, type, original_filename, content_type, size_bytes, gcs_uri,
                  sha256, uploaded_by, recorded_at, chain_of_custody)
        ON public.evidence TO caselens_app
    """,
    *(f"GRANT SELECT ON public.{table} TO caselens_app" for table in _DERIVED_TABLES),
    # status / attempts are not grantable: new jobs always start 'queued' with 0 attempts.
    "GRANT INSERT (id, case_id, evidence_id, stage) ON public.ingest_jobs TO caselens_app",
    "GRANT SELECT ON public.entity_links TO caselens_app",
    "GRANT UPDATE (status, reverted_at, reverted_by) ON public.entity_links TO caselens_app",
    "GRANT SELECT, INSERT ON public.conflicts TO caselens_app",
    "GRANT SELECT, INSERT ON public.agent_runs TO caselens_app",
    "GRANT SELECT ON public.audit_log TO caselens_app",
    "GRANT INSERT (user_id, case_id, action, payload) ON public.audit_log TO caselens_app",
    "GRANT USAGE ON SEQUENCE public.audit_log_id_seq TO caselens_app",
    # caselens_readonly (semantic layer)
    *(f"GRANT SELECT ON public.{table} TO caselens_readonly" for table in _READONLY_TABLES),
    # caselens_worker (BYPASSRLS): reads evidence, writes derived tables. entity_links is
    # insert-only for workers (merges are reversible links, never deleted or overwritten).
    "GRANT SELECT ON public.cases, public.evidence TO caselens_worker",
    *(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO caselens_worker"
        for table in _DERIVED_TABLES
    ),
    "GRANT SELECT, INSERT ON public.entity_links TO caselens_worker",
)


def upgrade() -> None:
    for stmt in (
        *_EXTENSIONS,
        *_ROLES,
        *_TABLES,
        *_HELPER_FUNCTIONS,
        *_AUDIT_FUNCTIONS,
        *_FUNCTION_OWNERSHIP,
        *_RLS_ENABLE,
        *_POLICIES,
        *_GRANTS,
    ):
        op.execute(stmt)


def downgrade() -> None:
    # Dropping a table drops its policies, triggers, indexes and grants. Reverse dependency order.
    for table in reversed(_ALL_TABLES):
        op.execute(f"DROP TABLE IF EXISTS public.{table}")
    for sig in (
        *_HELPER_SIGNATURES,
        "public.audit_log_chain()",
        "public.audit_log_block_mutation()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    op.execute(
        "REVOKE USAGE ON SCHEMA public "
        "FROM caselens_app, caselens_readonly, caselens_worker, caselens_definer"
    )
    for ext in ("pg_trgm", "pgcrypto", "vector"):
        op.execute(f"DROP EXTENSION IF EXISTS {ext}")
    # The group roles are intentionally left in place: roles are cluster-global (shared by every
    # database on the instance, and infra grants login users membership in them), so a
    # per-database downgrade must not drop them. They now own nothing and hold no privileges in
    # this database; upgrade() re-creates them idempotently.

"""Let ingest workers append system audit events (no user) to the hash-chained audit log.

Revision ID: 0003
Revises: 0002

CONTRACT: approved by the tech lead (option A, 2026-10-19). SYNTHETIC data only.

Background events have no human actor: ingest stages finishing or failing, retries,
dead-lettering, scheduled evidence re-hashing. They are written by caselens_worker with
user_id NULL and join the same hash chain (the 0001 BEFORE INSERT trigger computes the hashes).

* caselens_worker gets INSERT on (case_id, action, payload) only. user_id is not grantable, so
  a worker can never write an event in a user's name; the row's user_id is always NULL.
* caselens_worker still cannot SELECT, UPDATE or DELETE audit_log. It bypasses RLS, so this
  table grant is the whole control.

Every statement is written out literally (no generated SQL).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UPGRADE = (
    "GRANT INSERT (case_id, action, payload) ON public.audit_log TO caselens_worker",
    "GRANT USAGE ON SEQUENCE public.audit_log_id_seq TO caselens_worker",
)

_DOWNGRADE = (
    "REVOKE USAGE ON SEQUENCE public.audit_log_id_seq FROM caselens_worker",
    "REVOKE INSERT (case_id, action, payload) ON public.audit_log FROM caselens_worker",
)


def upgrade() -> None:
    for stmt in _UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _DOWNGRADE:
        op.execute(stmt)

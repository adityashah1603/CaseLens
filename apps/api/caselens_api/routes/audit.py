"""Audit log reads (case lead; platform admin across cases) and chain verification."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query

from caselens_api import models as m
from caselens_api.common import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    decode_cursor,
    encode_cursor,
    write_audit,
)
from caselens_api.db import Conn
from caselens_api.deps import AdminSession, LeadCase
from caselens_api.problems import ProblemError

router = APIRouter(prefix="/v1")

Cursor = Annotated[str | None, Query(max_length=512)]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Action = Annotated[str | None, Query(max_length=100)]
From = Annotated[datetime | None, Query(alias="from")]
To = Annotated[datetime | None, Query(alias="to")]

# RLS decides what is visible: a lead sees their case's rows, an admin sees every row.
_AUDIT_PAGE = """
SELECT a.id, a.ts, a.user_id, a.case_id, a.action, a.payload, a.payload_hash, a.prev_hash,
       a.row_hash
  FROM public.audit_log AS a
 WHERE (%(case_id)s::uuid IS NULL OR a.case_id = %(case_id)s::uuid)
   AND (%(action)s::text IS NULL OR a.action = %(action)s::text)
   AND (%(user_id)s::uuid IS NULL OR a.user_id = %(user_id)s::uuid)
   AND (%(time_from)s::timestamptz IS NULL OR a.ts >= %(time_from)s::timestamptz)
   AND (%(time_to)s::timestamptz IS NULL OR a.ts <= %(time_to)s::timestamptz)
   AND (%(after_id)s::bigint IS NULL OR a.id > %(after_id)s::bigint)
 ORDER BY a.id
 LIMIT %(limit)s
"""


def _page(
    conn: Conn,
    case_id: uuid.UUID | None,
    action: str | None,
    user_id: uuid.UUID | None,
    time_from: datetime | None,
    time_to: datetime | None,
    cursor: str | None,
    limit: int,
) -> m.AuditEntryList:
    after = decode_cursor(cursor, (int,))
    rows = conn.execute(
        _AUDIT_PAGE,
        {
            "case_id": case_id,
            "action": action,
            "user_id": user_id,
            "time_from": time_from,
            "time_to": time_to,
            "after_id": after[0] if after else None,
            "limit": limit + 1,
        },
    ).fetchall()
    items = [
        m.AuditEntry(
            id=r[0],
            ts=r[1],
            user_id=r[2],
            case_id=r[3],
            action=r[4],
            payload=r[5] if isinstance(r[5], dict) else {"value": r[5]},
            payload_hash=r[6].strip(),
            prev_hash=r[7].strip(),
            row_hash=r[8].strip(),
        )
        for r in rows[:limit]
    ]
    nxt = encode_cursor(rows[limit - 1][0]) if len(rows) > limit else None
    return m.AuditEntryList(items=items, next_cursor=nxt)


def _filters(values: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (str(v) if isinstance(v, uuid.UUID) else v.isoformat() if isinstance(v, datetime) else v)
        for k, v in values.items()
        if v is not None
    }


@router.get("/cases/{case_id}/audit", response_model=m.AuditEntryList)
def list_case_audit(
    case: LeadCase,
    action: Action = None,
    user_id: uuid.UUID | None = None,
    time_from: From = None,
    time_to: To = None,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> m.AuditEntryList:
    page = _page(case.conn, case.case_id, action, user_id, time_from, time_to, cursor, limit)
    with case.conn.transaction():
        write_audit(
            case.conn,
            case.user.id,
            case.case_id,
            "audit.view",
            {
                "filters": _filters(
                    {"action": action, "user_id": user_id, "from": time_from, "to": time_to}
                ),
                "returned": len(page.items),
            },
        )
    return page


@router.get("/admin/audit", response_model=m.AuditEntryList)
def list_all_audit(
    admin: AdminSession,
    case_id: uuid.UUID | None = None,
    action: Action = None,
    user_id: uuid.UUID | None = None,
    time_from: From = None,
    time_to: To = None,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> m.AuditEntryList:
    page = _page(admin.conn, case_id, action, user_id, time_from, time_to, cursor, limit)
    with admin.conn.transaction():
        write_audit(
            admin.conn,
            admin.user.id,
            None,
            "admin.audit.view",
            {
                "filters": _filters(
                    {
                        "case_id": case_id,
                        "action": action,
                        "user_id": user_id,
                        "from": time_from,
                        "to": time_to,
                    }
                ),
                "returned": len(page.items),
            },
        )
    return page


@router.get("/admin/audit/verify", response_model=m.AuditVerification)
def verify_audit(admin: AdminSession) -> m.AuditVerification:
    row = admin.conn.execute(
        "SELECT valid, checked_rows, first_invalid_id FROM public.audit_log_verify()"
    ).fetchone()
    if row is None:
        raise ProblemError(500, "The audit verifier returned no result.")
    result = m.AuditVerification(valid=row[0], checked_rows=row[1], first_invalid_id=row[2])
    with admin.conn.transaction():
        write_audit(admin.conn, admin.user.id, None, "admin.audit.verify", result.model_dump())
    return result

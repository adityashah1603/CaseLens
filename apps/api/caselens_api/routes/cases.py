"""/v1/me, /v1/cases and case membership."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Query, Response

from caselens_api import models as m
from caselens_api.common import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    cursor_params,
    decode_cursor,
    encode_cursor,
    parse_id,
    write_audit,
)
from caselens_api.db import Conn
from caselens_api.deps import LeadCase, SessionDep, ViewerCase
from caselens_api.problems import ProblemError, conflict, forbidden, not_found

router = APIRouter(prefix="/v1")

Cursor = Annotated[str | None, Query(max_length=512)]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]

# Demo-mode one-click questions (contract example), shown only on cases flagged is_demo.
DEMO_SUGGESTED_QUESTIONS = [
    "Who was in the gray sedan on March 14?",
    "Where was the gray sedan between 10 and 11pm on March 14?",
    "How many calls were made between the suspect's phone and the victim's phone that night?",
    "Do the witness statements agree on when the shots were heard?",
    "What is the chain of custody for item E-07?",
]

# ---- /me -----------------------------------------------------------------------------------

_MEMBERSHIPS = """
SELECT m.case_id, c.name, m.role
  FROM public.case_members AS m
  JOIN public.cases AS c ON c.id = m.case_id
 WHERE m.user_id = public.app_current_user_id()
 ORDER BY c.name, m.case_id
"""


@router.get("/me", response_model=m.Me)
def get_me(session: SessionDep) -> m.Me:
    rows = session.conn.execute(_MEMBERSHIPS).fetchall()
    return m.Me(
        user=m.User.model_validate(session.user.model_dump()),
        memberships=[m.Membership(case_id=r[0], case_name=r[1], role=r[2]) for r in rows],
    )


# ---- cases ---------------------------------------------------------------------------------

_CASE_SELECT = """
SELECT c.id, c.name, c.description, c.status, c.is_demo, c.created_by, c.created_at,
       c.updated_at, mb.role,
       (SELECT count(*) FROM public.evidence AS e WHERE e.case_id = c.id),
       GREATEST(
           (SELECT max(e.created_at) FROM public.evidence AS e WHERE e.case_id = c.id),
           (SELECT max(j.updated_at) FROM public.ingest_jobs AS j WHERE j.case_id = c.id),
           (SELECT max(r.created_at) FROM public.agent_runs AS r WHERE r.case_id = c.id)),
       (SELECT coalesce(jsonb_object_agg(s.status, s.n), '{}'::jsonb)
          FROM (SELECT j.status, count(*) AS n FROM public.ingest_jobs AS j
                 WHERE j.case_id = c.id GROUP BY j.status) AS s)
  FROM public.cases AS c
  JOIN public.case_members AS mb
    ON mb.case_id = c.id AND mb.user_id = public.app_current_user_id()
"""
_CASE_BY_ID = _CASE_SELECT + " WHERE c.id = %(case_id)s"
_CASE_PAGE = (
    _CASE_SELECT
    + """
 WHERE (%(c_ts)s::timestamptz IS NULL OR (c.created_at, c.id) < (%(c_ts)s, %(c_id)s::uuid))
 ORDER BY c.created_at DESC, c.id DESC
 LIMIT %(limit)s
"""
)


def _case(row: tuple[Any, ...]) -> m.Case:
    is_demo = bool(row[4])
    return m.Case(
        id=row[0],
        name=row[1],
        description=row[2],
        status=row[3],
        is_demo=is_demo,
        created_by=row[5],
        created_at=row[6],
        updated_at=row[7],
        my_role=row[8],
        evidence_count=row[9],
        last_activity_at=row[10],
        ingest_summary=m.IngestSummary.model_validate(row[11] or {}),
        suggested_questions=list(DEMO_SUGGESTED_QUESTIONS) if is_demo else [],
    )


def load_case(conn: Conn, case_id: uuid.UUID) -> m.Case:
    row = conn.execute(_CASE_BY_ID, {"case_id": case_id}).fetchone()
    if row is None:
        raise not_found("No such case.")
    return _case(row)


@router.get("/cases", response_model=m.CaseList)
def list_cases(
    session: SessionDep, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT
) -> m.CaseList:
    after = decode_cursor(cursor, (datetime, uuid.UUID))
    rows = session.conn.execute(
        _CASE_PAGE, {**cursor_params(after, ("c_ts", "c_id")), "limit": limit + 1}
    ).fetchall()
    items = [_case(r) for r in rows[:limit]]
    nxt = encode_cursor(rows[limit - 1][6], rows[limit - 1][0]) if len(rows) > limit else None
    return m.CaseList(items=items, next_cursor=nxt)


@router.post("/cases", response_model=m.Case, status_code=201)
def create_case(body: m.CaseCreate, session: SessionDep) -> m.Case:
    if session.user.is_demo:
        raise forbidden("The demo user cannot create cases.")
    conn, user_id = session.conn, session.user.id
    case_id = uuid.uuid4()  # generated here: no INSERT ... RETURNING before membership (D-026)
    with conn.transaction():
        conn.execute(
            "INSERT INTO public.cases (id, name, description, created_by) VALUES (%s, %s, %s, %s)",
            (case_id, body.name, body.description, user_id),
        )
        conn.execute(
            "INSERT INTO public.case_members (case_id, user_id, role, added_by) "
            "VALUES (%s, %s, 'lead', %s)",
            (case_id, user_id, user_id),
        )
        write_audit(conn, user_id, case_id, "case.create", {"name": body.name})
    return load_case(conn, case_id)


@router.get("/cases/{case_id}", response_model=m.Case)
def get_case(case: ViewerCase) -> m.Case:
    return load_case(case.conn, case.case_id)


_UPDATE_CASE = """
UPDATE public.cases
   SET name = CASE WHEN %(set_name)s THEN %(name)s ELSE name END,
       description = CASE WHEN %(set_description)s THEN %(description)s ELSE description END,
       status = CASE WHEN %(set_status)s THEN %(status)s ELSE status END,
       updated_at = now()
 WHERE id = %(case_id)s
"""


@router.patch("/cases/{case_id}", response_model=m.Case)
def update_case(body: m.CaseUpdate, case: LeadCase) -> m.Case:
    changes = body.model_dump(include=body.model_fields_set)
    with case.conn.transaction():
        case.conn.execute(
            _UPDATE_CASE,
            {
                "case_id": case.case_id,
                "set_name": "name" in changes,
                "name": body.name,
                "set_description": "description" in changes,
                "description": body.description,
                "set_status": "status" in changes,
                "status": body.status,
            },
        )
        write_audit(case.conn, case.user.id, case.case_id, "case.update", {"changes": changes})
    return load_case(case.conn, case.case_id)


# ---- members -------------------------------------------------------------------------------

_MEMBER_SELECT = """
SELECT mb.case_id, mb.user_id, mb.role, u.email, u.display_name, mb.added_by, mb.created_at
  FROM public.case_members AS mb
  JOIN public.users AS u ON u.id = mb.user_id
 WHERE mb.case_id = %(case_id)s
"""
_MEMBER_ONE = _MEMBER_SELECT + " AND mb.user_id = %(user_id)s"
_MEMBER_PAGE = (
    _MEMBER_SELECT
    + """
   AND (%(c_ts)s::timestamptz IS NULL
        OR (mb.created_at, mb.user_id) > (%(c_ts)s, %(c_id)s::uuid))
 ORDER BY mb.created_at, mb.user_id
 LIMIT %(limit)s
"""
)
_LEADS_FOR_UPDATE = """
SELECT user_id FROM public.case_members
 WHERE case_id = %s AND role = 'lead'
 FOR UPDATE
"""


def _member(row: tuple[Any, ...]) -> m.CaseMember:
    return m.CaseMember(
        case_id=row[0],
        user_id=row[1],
        role=row[2],
        email=row[3],
        display_name=row[4],
        added_by=row[5],
        created_at=row[6],
    )


def _load_member(conn: Conn, case_id: uuid.UUID, user_id: uuid.UUID) -> m.CaseMember | None:
    row = conn.execute(_MEMBER_ONE, {"case_id": case_id, "user_id": user_id}).fetchone()
    return None if row is None else _member(row)


@router.get("/cases/{case_id}/members", response_model=m.CaseMemberList)
def list_members(
    case: ViewerCase, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT
) -> m.CaseMemberList:
    after = decode_cursor(cursor, (datetime, uuid.UUID))
    rows = case.conn.execute(
        _MEMBER_PAGE,
        {"case_id": case.case_id, **cursor_params(after, ("c_ts", "c_id")), "limit": limit + 1},
    ).fetchall()
    nxt = encode_cursor(rows[limit - 1][6], rows[limit - 1][1]) if len(rows) > limit else None
    return m.CaseMemberList(items=[_member(r) for r in rows[:limit]], next_cursor=nxt)


def _demo_role_check(conn: Conn, user_id: uuid.UUID, role: str) -> None:
    """The shared demo login stays read-only on every case: never above viewer."""
    row = conn.execute("SELECT is_demo FROM public.users WHERE id = %s", (user_id,)).fetchone()
    if row is not None and row[0] and m.ROLE_RANK[role] > m.ROLE_RANK["viewer"]:
        raise forbidden("The demo user can only be a viewer.")


@router.post("/cases/{case_id}/members", response_model=m.CaseMember, status_code=201)
def add_member(body: m.CaseMemberCreate, case: LeadCase) -> m.CaseMember:
    conn = case.conn
    if body.user_id is not None:
        row = conn.execute("SELECT id FROM public.users WHERE id = %s", (body.user_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM public.users WHERE lower(email) = lower(%s) "
            "ORDER BY created_at LIMIT 1",
            (body.email,),
        ).fetchone()
    if row is None:
        raise not_found("No such user. They must sign in once before they can be added.")
    user_id: uuid.UUID = row[0]
    _demo_role_check(conn, user_id, body.role)
    if _load_member(conn, case.case_id, user_id) is not None:
        raise conflict("This user is already a member of the case.", "already-member")
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO public.case_members (case_id, user_id, role, added_by) "
                "VALUES (%s, %s, %s, %s)",
                (case.case_id, user_id, body.role, case.user.id),
            )
            write_audit(
                conn,
                case.user.id,
                case.case_id,
                "member.add",
                {"user_id": str(user_id), "role": body.role},
            )
    except psycopg.errors.UniqueViolation as exc:
        raise conflict("This user is already a member of the case.", "already-member") from exc
    member = _load_member(conn, case.case_id, user_id)
    if member is None:
        raise ProblemError(500, "The membership was not stored.")
    return member


def _would_remove_last_lead(conn: Conn, case_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    leads = {r[0] for r in conn.execute(_LEADS_FOR_UPDATE, (case_id,)).fetchall()}
    return user_id in leads and len(leads) == 1


@router.patch("/cases/{case_id}/members/{user_id}", response_model=m.CaseMember)
def update_member(user_id: str, body: m.CaseMemberUpdate, case: LeadCase) -> m.CaseMember:
    uid = parse_id(user_id, "member")
    conn = case.conn
    with conn.transaction():
        current = _load_member(conn, case.case_id, uid)
        if current is None:
            raise not_found("No such member.")
        _demo_role_check(conn, uid, body.role)
        if body.role != "lead" and _would_remove_last_lead(conn, case.case_id, uid):
            raise conflict("The case must keep at least one lead.", "last-lead")
        conn.execute(
            "UPDATE public.case_members SET role = %s WHERE case_id = %s AND user_id = %s",
            (body.role, case.case_id, uid),
        )
        write_audit(
            conn,
            case.user.id,
            case.case_id,
            "member.update",
            {"user_id": str(uid), "from_role": current.role, "to_role": body.role},
        )
    member = _load_member(conn, case.case_id, uid)
    if member is None:  # a lead who demoted themselves can still read the membership
        raise not_found("No such member.")
    return member


@router.delete("/cases/{case_id}/members/{user_id}", status_code=204)
def remove_member(user_id: str, case: LeadCase) -> Response:
    uid = parse_id(user_id, "member")
    conn = case.conn
    with conn.transaction():
        current = _load_member(conn, case.case_id, uid)
        if current is None:
            raise not_found("No such member.")
        if _would_remove_last_lead(conn, case.case_id, uid):
            raise conflict("The case must keep at least one lead.", "last-lead")
        write_audit(
            conn,
            case.user.id,
            case.case_id,
            "member.remove",
            {"user_id": str(uid), "role": current.role},
        )
        conn.execute(
            "DELETE FROM public.case_members WHERE case_id = %s AND user_id = %s",
            (case.case_id, uid),
        )
    return Response(status_code=204)

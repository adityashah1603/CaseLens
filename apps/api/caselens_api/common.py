"""Shared request helpers: ids, cursors, audit writes, case role checks."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from caselens_api.db import Conn
from caselens_api.models import ROLE_RANK
from caselens_api.problems import ProblemError, forbidden, not_found

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


def parse_id(value: str, what: str = "resource") -> uuid.UUID:
    """Path ids that are not UUIDs cannot exist: 404, like any unknown id."""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise not_found(f"No such {what}.") from exc


# ---- cursors -------------------------------------------------------------------------------


def encode_cursor(*values: Any) -> str:
    """Opaque keyset cursor: base64url of a JSON array of the last row's sort key."""
    plain = [v.isoformat() if isinstance(v, datetime) else str(v) for v in values]
    raw = json.dumps(plain, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _bad_cursor() -> ProblemError:
    return ProblemError(
        422,
        "The cursor is invalid.",
        errors=[{"loc": ["query", "cursor"], "msg": "invalid cursor"}],
    )


def decode_cursor(cursor: str | None, kinds: tuple[type, ...]) -> list[Any] | None:
    """Decode a cursor whose values have the given kinds (datetime, uuid.UUID, int, float,
    str). Returns None when no cursor was sent; 422 when it is malformed."""
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != len(kinds):
            raise ValueError("wrong cursor arity")
        out: list[Any] = []
        for kind, value in zip(kinds, values, strict=True):
            if not isinstance(value, str):
                raise ValueError("cursor values are strings")
            if kind is datetime:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    raise ValueError("naive cursor timestamp")
                out.append(parsed)
            elif kind is str:
                out.append(value)
            else:
                out.append(kind(value))
        return out
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise _bad_cursor() from exc


def cursor_params(cursor: list[Any] | None, names: tuple[str, ...]) -> dict[str, Any]:
    if cursor is None:
        return dict.fromkeys(names)
    return dict(zip(names, cursor, strict=True))


# ---- audit ---------------------------------------------------------------------------------

_AUDIT = "INSERT INTO public.audit_log (user_id, case_id, action, payload) VALUES (%s, %s, %s, %s)"


def write_audit(
    conn: Conn,
    user_id: uuid.UUID,
    case_id: uuid.UUID | None,
    action: str,
    payload: dict[str, Any],
) -> None:
    """Append one audit row as the current user (no RETURNING: only leads may read rows back).
    Call inside the transaction of the action it records."""
    conn.execute(_AUDIT, (user_id, case_id, action, Jsonb(payload)))


# ---- case roles ----------------------------------------------------------------------------


def case_role(conn: Conn, case_id: uuid.UUID) -> str | None:
    row = conn.execute("SELECT public.app_case_role(%s)", (case_id,)).fetchone()
    return None if row is None else row[0]


def require_role(conn: Conn, case_id: uuid.UUID, minimum: str) -> str:
    """404 for non-members (existence never leaks), 403 when the role is too low."""
    role = case_role(conn, case_id)
    if role is None:
        raise not_found("No such case.")
    if ROLE_RANK[role] < ROLE_RANK[minimum]:
        raise forbidden(f"This action needs the {minimum} role on this case; you are {role}.")
    return role

"""CaseLens API CLI (local development).

python -m caselens_api serve [--host 127.0.0.1] [--port 8000]   # uvicorn, 1 worker
python -m caselens_api dev-token <oidc_subject> [--hours 12]      # HS256 dev token (local only)
python -m caselens_api seed-dev-users [--case-name NAME]          # demo viewer + analyst

Dev tokens need CASELENS_ENVIRONMENT=local and CASELENS_DEV_AUTH_SECRET (32+ characters). They
are minted here only; no HTTP endpoint issues tokens.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from caselens_api.auth import DevTokenVerifier, TokenIdentity, resolve_user
from caselens_api.common import write_audit
from caselens_api.db import Conn, Database, set_user
from caselens_api.settings import Settings, get_settings

SEED_LEAD_SUBJECT = "local-dev:seed-lead"
DEMO_CASE_NAME = "The Riverside Lot Homicide"


@dataclass(frozen=True)
class DevUser:
    label: str
    subject: str
    display_name: str
    role: str


DEV_USERS = (
    DevUser("analyst", "local-dev:analyst", "Analyst (SYNTHETIC)", "analyst"),
    DevUser("viewer", "local-dev:demo-viewer", "Demo viewer (SYNTHETIC)", "viewer"),
)


def _dev_verifier(settings: Settings) -> DevTokenVerifier:
    if not settings.is_local:
        raise SystemExit("dev tokens are only available when CASELENS_ENVIRONMENT=local")
    if settings.dev_auth_secret is None:
        raise SystemExit("CASELENS_DEV_AUTH_SECRET is not set")
    return DevTokenVerifier(settings.dev_auth_secret.get_secret_value())


def cmd_dev_token(args: argparse.Namespace, settings: Settings) -> int:
    token = _dev_verifier(settings).mint(
        args.subject, hours=args.hours, email=args.email, name=args.name
    )
    print(token)
    return 0


def _ensure_member(
    conn: Conn, lead_id: uuid.UUID, case_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> str:
    row = conn.execute(
        "SELECT role FROM public.case_members WHERE case_id = %s AND user_id = %s",
        (case_id, user_id),
    ).fetchone()
    with conn.transaction():
        if row is None:
            conn.execute(
                "INSERT INTO public.case_members (case_id, user_id, role, added_by) "
                "VALUES (%s, %s, %s, %s)",
                (case_id, user_id, role, lead_id),
            )
            write_audit(
                conn, lead_id, case_id, "member.add", {"user_id": str(user_id), "role": role}
            )
            return "added"
        if row[0] != role:
            conn.execute(
                "UPDATE public.case_members SET role = %s WHERE case_id = %s AND user_id = %s",
                (role, case_id, user_id),
            )
            write_audit(
                conn,
                lead_id,
                case_id,
                "member.update",
                {"user_id": str(user_id), "from_role": row[0], "to_role": role},
            )
            return f"role {row[0]} -> {role}"
    return "already a member"


def cmd_seed_dev_users(args: argparse.Namespace, settings: Settings) -> int:
    """Through RLS as the seed lead: add the demo viewer and an analyst to the demo case."""
    verifier = _dev_verifier(settings)
    if not settings.database_url:
        raise SystemExit("CASELENS_DATABASE_URL is not set")
    db = Database(settings.database_url, settings.app_db_role, settings.readonly_db_role, 1, 1)
    db.pool.open(wait=True)
    try:
        with db.session(None) as conn:
            lead = resolve_user(conn, TokenIdentity(subject=SEED_LEAD_SUBJECT))
            users = {
                u.label: resolve_user(
                    conn, TokenIdentity(subject=u.subject, display_name=u.display_name)
                )
                for u in DEV_USERS
            }
            set_user(conn, lead.id)
            row = conn.execute(
                "SELECT c.id FROM public.cases AS c WHERE c.name = %s "
                "AND public.app_case_role(c.id) = 'lead' ORDER BY c.created_at LIMIT 1",
                (args.case_name,),
            ).fetchone()
            if row is None:
                print(
                    f"no case named {args.case_name!r} led by {SEED_LEAD_SUBJECT}; "
                    "seed it first with `python -m caselens_workers submit ...`",
                    file=sys.stderr,
                )
                return 2
            case_id: uuid.UUID = row[0]
            print(f"case {case_id} ({args.case_name})")
            for spec in DEV_USERS:
                user = users[spec.label]
                outcome = _ensure_member(conn, lead.id, case_id, user.id, spec.role)
                print(f"  {spec.label:8} {user.id} {spec.subject}: {outcome}")
    finally:
        db.close()
    print()
    for label, subject in (
        ("lead", SEED_LEAD_SUBJECT),
        *((u.label, u.subject) for u in DEV_USERS),
    ):
        print(f"{label.upper()}_TOKEN={verifier.mint(subject, hours=args.hours)}")
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    uvicorn.run("caselens_api.main:app", host=args.host, port=args.port, workers=1)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m caselens_api")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the API with uvicorn (one worker)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("dev-token", help="mint a local dev bearer token for an oidc subject")
    p.add_argument("subject")
    p.add_argument("--hours", type=float, default=12.0)
    p.add_argument("--email")
    p.add_argument("--name")
    p.set_defaults(func=cmd_dev_token)

    p = sub.add_parser("seed-dev-users", help="add the demo viewer and an analyst to the demo case")
    p.add_argument("--case-name", default=DEMO_CASE_NAME)
    p.add_argument("--hours", type=float, default=12.0)
    p.set_defaults(func=cmd_seed_dev_users)

    args = parser.parse_args(argv)
    code: int = args.func(args, get_settings())
    return code


if __name__ == "__main__":
    sys.exit(main())

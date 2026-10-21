"""Export the demo case's evidence originals from the local dev stack for the Space image.

Reads the demo case's evidence rows from the local dev database (CASELENS_DATABASE_URL, owner)
and copies each original object from MinIO (CASELENS_S3_*) to `infra/space/evidence/<key>`,
the layout `LocalDiskEvidenceStore` serves, with a `<file>.meta.json` content-type sidecar.
Every file's SHA-256 is checked against evidence.sha256; a mismatch aborts. Files already
present with the right hash are skipped, so re-running is cheap. SYNTHETIC data only.

    .venv/Scripts/python infra/space/export_evidence.py            # the one is_demo case
    .venv/Scripts/python infra/space/export_evidence.py --case-id <uuid>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import psycopg

from caselens_api.settings import Settings
from caselens_api.storage import HASH_CHUNK, META_SUFFIX, key_segments, split_uri
from caselens_workers.settings import libpq_url

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = Path(__file__).resolve().parent / "evidence"
MANIFEST = "manifest.json"


def demo_case_id(conn: psycopg.Connection[tuple[object, ...]], wanted: str | None) -> uuid.UUID:
    if wanted:
        row = conn.execute("SELECT id FROM public.cases WHERE id = %s", (wanted,)).fetchone()
        if row is None:
            raise SystemExit(f"no case {wanted} in the source database")
        return uuid.UUID(str(row[0]))
    rows = conn.execute("SELECT id FROM public.cases WHERE is_demo ORDER BY created_at").fetchall()
    if len(rows) != 1:
        raise SystemExit(f"expected exactly one is_demo case, found {len(rows)}; pass --case-id")
    return uuid.UUID(str(rows[0][0]))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--case-id", help="case to export (default: the single is_demo case)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output root")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="local .env")
    args = parser.parse_args(argv)

    env_file = args.env_file if args.env_file.is_file() else None
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    if not settings.database_url:
        raise SystemExit("CASELENS_DATABASE_URL (the local dev owner URL) is not set")
    from minio import Minio

    client = Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
        region=settings.s3_region,
    )
    out: Path = args.out.resolve()
    with psycopg.connect(libpq_url(settings.database_url)) as conn:
        case_id = demo_case_id(conn, args.case_id)
        rows = conn.execute(
            "SELECT id, gcs_uri, sha256, content_type, size_bytes, original_filename "
            "FROM public.evidence WHERE case_id = %s ORDER BY original_filename, id",
            (case_id,),
        ).fetchall()
    if not rows:
        raise SystemExit(f"case {case_id} has no evidence")

    manifest = []
    total = 0
    for evidence_id, uri, sha, content_type, size, filename in rows:
        _, bucket, key = split_uri(str(uri))
        target = out.joinpath(*key_segments(key))
        expected = str(sha).strip().lower()
        if target.is_file() and file_sha256(target) == expected:
            status = "kept"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".export-")
            tmp = Path(tmp_name)
            digest = hashlib.sha256()
            try:
                response = client.get_object(bucket, key)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        for chunk in response.stream(HASH_CHUNK):
                            digest.update(chunk)
                            handle.write(chunk)
                finally:
                    response.close()
                    response.release_conn()
                if digest.hexdigest() != expected:
                    raise SystemExit(f"SHA-256 mismatch for {filename} ({evidence_id}); aborting")
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
            status = "copied"
        meta = target.with_name(target.name + META_SUFFIX)
        meta.write_text(json.dumps({"content_type": str(content_type)}), encoding="utf-8")
        total += int(size)
        manifest.append(
            {"evidence_id": str(evidence_id), "key": key, "sha256": expected, "size": int(size)}
        )
        print(f"{status:7} {filename} ({int(size):,} bytes)")

    # Drop files of evidence that is no longer in the case (an older export), so the image
    # ships exactly the case's originals.
    wanted = {out.joinpath(*key_segments(str(item["key"]))) for item in manifest}
    wanted |= {p.with_name(p.name + META_SUFFIX) for p in wanted}
    for stale in sorted(p for p in out.rglob("*") if p.is_file() and p.parent != out):
        if stale not in wanted:
            stale.unlink()
            print(f"removed {stale.relative_to(out)}")

    (out / MANIFEST).write_text(
        json.dumps({"case_id": str(case_id), "files": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{len(rows)} files, {total:,} bytes, SHA-256 verified -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

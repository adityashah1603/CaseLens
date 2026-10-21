"""Serve the built web app (apps/web/dist) at `/` when CASELENS_WEB_DIST is set.

A GET / HEAD catch-all registered after every API route: an existing file under the dist
directory is served as-is (hashed `assets/*` cached for a year, everything else revalidated),
any other path gets index.html so client-side routes deep-link. Paths under the server's own
prefixes (/v1, /healthz, /storage, /docs, /openapi.json) are never answered with the SPA: an
unknown one is a problem+json 404, as without the SPA.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from caselens_api.problems import not_found

RESERVED_PREFIXES = ("v1", "healthz", "storage", "docs", "redoc", "openapi.json")
# Explicit types: the platform registry (notably on Windows) can map .js to text/plain, which
# browsers refuse for module scripts.
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
}
_IMMUTABLE = "public, max-age=31536000, immutable"
_REVALIDATE = "no-cache"


def _media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return _TYPES.get(suffix) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _is_reserved(path: str) -> bool:
    first = path.split("/", 1)[0]
    return first in RESERVED_PREFIXES


def mount_spa(app: FastAPI, dist: Path) -> None:
    """Register the SPA catch-all. Call after every other route has been included."""
    root = dist.resolve()
    index = root / "index.html"
    if not index.is_file():
        raise RuntimeError(f"CASELENS_WEB_DIST={dist} has no index.html; build apps/web first")

    def respond(file: Path, cache_control: str) -> FileResponse:
        return FileResponse(
            file,
            media_type=_media_type(file),
            headers={"Cache-Control": cache_control, "X-Content-Type-Options": "nosniff"},
        )

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if _is_reserved(path):
            raise not_found()
        if path:
            candidate = (root / path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                immutable = candidate.is_relative_to(root / "assets")
                return respond(candidate, _IMMUTABLE if immutable else _REVALIDATE)
            last = path.rsplit("/", 1)[-1]
            if "." in last:  # a missing file (asset, favicon), not a client route
                raise not_found()
        return respond(index, _REVALIDATE)

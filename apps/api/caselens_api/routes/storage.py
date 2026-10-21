"""GET /storage/{token}: the local-disk object store's signed URLs (CASELENS_STORAGE_BACKEND=local).

NOT part of the API contract (docs/contracts/openapi.yaml): it stands in for the object store's
own signed GET URL, which is where `GET /v1/.../evidence/{id}/content` redirects (307) after the
membership check, the SHA-256 re-check and the evidence.view audit row. Like an S3 / GCS signed
URL it carries no bearer token: the HMAC-signed, expiring token is the authorization. Bad,
tampered or expired tokens get 403. Range requests are served (206) so audio / video can seek.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from caselens_api.deps import ServicesDep
from caselens_api.problems import ProblemError, not_found
from caselens_api.storage import (
    MAX_TOKEN_LENGTH,
    STORAGE_ROUTE,
    InvalidStorageTokenError,
    LocalDiskEvidenceStore,
    StorageError,
)

router = APIRouter()


@router.api_route(STORAGE_ROUTE + "/{token}", methods=["GET", "HEAD"], include_in_schema=False)
def get_object(token: str, services: ServicesDep) -> FileResponse:
    store = services.store()
    if not isinstance(store, LocalDiskEvidenceStore):
        raise not_found()
    if len(token) > MAX_TOKEN_LENGTH:
        raise ProblemError(403, "Invalid or expired link.")
    try:
        path, content_type = store.resolve_token(token)
    except InvalidStorageTokenError as exc:
        raise ProblemError(403, "Invalid or expired link.") from exc
    except StorageError as exc:
        raise not_found("No such object.") from exc
    # FileResponse answers Range / If-Range (206, 416) and sends Accept-Ranges: bytes.
    return FileResponse(
        path,
        media_type=content_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )

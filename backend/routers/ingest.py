"""Ingest queue upload endpoint.

POST /ingest/upload — owner-only; returns a pre-signed S3 PUT URL for uploading
                      a file directly to the site's .user/ingest/ queue prefix.
                      The browser uploads directly to S3; the backend never handles
                      the file bytes.
"""

import logging
import mimetypes
import re

from fastapi import APIRouter, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials

from auth import decode_jwt, get_site_for_user, require_site_owner, _bearer
from config import S3_BUCKET, s3

log = logging.getLogger(__name__)

router = APIRouter()

_PRESIGN_EXPIRY = 900           # 15 minutes
_INGEST_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

# Filename must have an extension, no path separators, reasonable characters.
_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 ._-]*\.[a-zA-Z0-9]{1,10}$")


@router.post(
    "/ingest/upload",
    tags=["ingest"],
    summary="Request a pre-signed S3 PUT URL for ingest queue upload",
    description=(
        "Owner-only. Validates the filename, then returns a short-lived pre-signed "
        "S3 PUT URL targeting the site's `.user/ingest/` queue. "
        "The browser uploads directly to S3 — the backend never handles file bytes. "
        "Accepts any file type (PDF, DOCX, PPTX, XLSX, plain text, etc.)."
    ),
)
async def upload_ingest(
    site: str,
    filename: str,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> dict:
    if not _SAFE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    claims = decode_jwt(credentials)
    user_site = get_site_for_user(claims.user_id)
    require_site_owner(site, user_site)

    s3_key = f"{site}/.user/ingest/{filename}"
    content_type, _ = mimetypes.guess_type(filename)
    content_type = content_type or "application/octet-stream"

    presigned_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": S3_BUCKET,
            "Key": s3_key,
            "ContentType": content_type,
        },
        ExpiresIn=_PRESIGN_EXPIRY,
        HttpMethod="PUT",
    )

    log.info("Issued ingest upload URL for %s/%s", site, filename)

    return {
        "upload_url": presigned_url,
        "key": f".user/ingest/{filename}",
        "content_type": content_type,
        "max_bytes": _INGEST_MAX_BYTES,
    }

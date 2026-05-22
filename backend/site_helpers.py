"""Site-level S3 lifecycle helpers (bulk delete, vector cleanup)."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def delete_s3_prefix(site_name: str) -> None:
    from config import S3_BUCKET, s3

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{site_name}/"):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": objects, "Quiet": True})


def delete_site_vectors(site_name: str) -> None:
    """Delete all S3 Vectors entries for a site (best-effort, never raises)."""
    from config import S3_BUCKET, S3_VECTORS_BUCKET, S3_VECTORS_INDEX_NAME, s3, s3vectors

    if s3vectors is None:
        return
    paginator = s3.get_paginator("list_objects_v2")
    vector_ids: list[str] = []
    try:
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{site_name}/"):
            for obj in page.get("Contents", []):
                if "/.chunks/" in obj["Key"]:
                    vector_ids.append(obj["Key"].split("/")[-1])
    except Exception as exc:
        log.warning("Failed to list chunks for vector deletion (%s): %s", site_name, exc)
        return
    if not vector_ids:
        return
    try:
        for i in range(0, len(vector_ids), 100):
            s3vectors.delete_vectors(
                vectorBucketName=S3_VECTORS_BUCKET,
                indexName=S3_VECTORS_INDEX_NAME,
                keys=vector_ids[i : i + 100],
            )
        log.info("Deleted %d vectors for site %s", len(vector_ids), site_name)
    except Exception as exc:
        log.warning("Failed to delete vectors for site %s: %s", site_name, exc)

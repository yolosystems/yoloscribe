"""Core logic for archiving and emptying the page archive.

Archive layout: {site}/.user/archive/{page_path}/content.md
                {site}/.user/archive/{page_path}/settings.json

Archiving a page:
  - Copies content.md + settings.json to the archive prefix
  - Deletes all .chunks/ objects and their S3 Vectors embeddings
  - Removes the page from the SQLite FTS5 index
  - Deletes the original content.md and settings.json

All descendants are included automatically.
"""

from __future__ import annotations

import logging
from typing import Any

from fts_cache import fts_remove_pages

log = logging.getLogger(__name__)

_ARCHIVE_PREFIX = ".user/archive"


def _archive_key(site: str, page_path: str, filename: str) -> str:
    if page_path:
        return f"{site}/{_ARCHIVE_PREFIX}/{page_path}/{filename}"
    return f"{site}/{_ARCHIVE_PREFIX}/_root/{filename}"


def _content_key(site: str, page_path: str) -> str:
    return f"{site}/{page_path}/content.md" if page_path else f"{site}/content.md"


def _settings_key(site: str, page_path: str) -> str:
    return f"{site}/{page_path}/settings.json" if page_path else f"{site}/settings.json"


def _page_dir(site: str, page_path: str) -> str:
    return f"{site}/{page_path}" if page_path else site


def _is_internal(relative: str) -> bool:
    return any(seg.startswith(".") for seg in relative.split("/")[:-1])


def _list_pages_under(s3, bucket: str, site: str, page_path: str) -> list[str]:
    """Return page_paths for page_path and all descendants (non-internal pages only)."""
    results: list[str] = []

    if page_path:
        prefix = f"{site}/{page_path}/"
    else:
        prefix = f"{site}/"

    paginator = s3.get_paginator("list_objects_v2")
    for s3_page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in s3_page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/content.md"):
                continue
            relative = key[len(f"{site}/"):]
            if _is_internal(relative):
                continue
            pp = "" if relative == "content.md" else relative[: -len("/content.md")]
            # Only include pages at or under page_path
            if page_path and not (pp == page_path or pp.startswith(page_path + "/")):
                continue
            results.append(pp)

    # Also include the page itself for the root-of-archive case
    if page_path and page_path not in results:
        key = _content_key(site, page_path)
        try:
            s3.head_object(Bucket=bucket, Key=key)
            results.append(page_path)
        except Exception:
            pass

    return results


def archive_page(
    s3,
    bucket: str,
    site: str,
    page_path: str,
    s3vectors_client: Any = None,
    vectors_bucket: str = "",
    vectors_index: str = "",
) -> dict:
    """Archive page_path and all its descendants.

    Returns {"archived": int, "chunks_deleted": int}.
    """
    if not page_path:
        raise ValueError("Cannot archive the root page")
    if page_path.startswith("."):
        raise ValueError(f"Cannot archive internal path: '{page_path}'")

    pages = _list_pages_under(s3, bucket, site, page_path)
    if not pages:
        raise ValueError(f"Page not found: '{page_path}'")

    archived = 0
    total_chunks = 0

    for pp in pages:
        content_key = _content_key(site, pp)
        settings_key = _settings_key(site, pp)
        page_dir = _page_dir(site, pp)
        chunks_prefix = f"{page_dir}/.chunks/"

        # Copy content.md to archive
        try:
            s3.copy_object(
                CopySource={"Bucket": bucket, "Key": content_key},
                Bucket=bucket,
                Key=_archive_key(site, pp, "content.md"),
            )
        except Exception as exc:
            log.warning("Could not archive content for %s: %s", pp, exc)
            continue

        # Copy settings.json to archive (best-effort)
        try:
            s3.copy_object(
                CopySource={"Bucket": bucket, "Key": settings_key},
                Bucket=bucket,
                Key=_archive_key(site, pp, "settings.json"),
            )
        except Exception:
            pass

        # Copy .agents/ definitions to archive, then delete originals
        agents_prefix = f"{page_dir}/.agents/"
        agents_to_delete = []
        paginator = s3.get_paginator("list_objects_v2")
        for s3_page in paginator.paginate(Bucket=bucket, Prefix=agents_prefix):
            for obj in s3_page.get("Contents", []):
                src_key = obj["Key"]
                # Preserve path structure relative to page_dir
                relative = src_key[len(f"{site}/"):]  # e.g. "{pp}/.agents/name/agent.md"
                dst_key = f"{site}/{_ARCHIVE_PREFIX}/{relative}"
                try:
                    s3.copy_object(
                        CopySource={"Bucket": bucket, "Key": src_key},
                        Bucket=bucket,
                        Key=dst_key,
                    )
                    agents_to_delete.append({"Key": src_key})
                except Exception as exc:
                    log.warning("Could not archive agent %s: %s", src_key, exc)
        if agents_to_delete:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": agents_to_delete, "Quiet": True})
            log.info("Archived %d agent files for %s", len(agents_to_delete), pp)

        # Discard any pending proposed change
        proposed_key = f"{page_dir}/.proposed.content.md"
        try:
            s3.delete_object(Bucket=bucket, Key=proposed_key)
        except Exception:
            pass

        # Collect and delete chunks + vectors
        chunk_keys = []
        vector_ids = []
        paginator = s3.get_paginator("list_objects_v2")
        for s3_page in paginator.paginate(Bucket=bucket, Prefix=chunks_prefix):
            for obj in s3_page.get("Contents", []):
                chunk_keys.append({"Key": obj["Key"]})
                vector_ids.append(obj["Key"].split("/")[-1])

        if chunk_keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": chunk_keys, "Quiet": True})
            total_chunks += len(chunk_keys)
            log.info("Deleted %d chunks for %s", len(chunk_keys), pp)

        if vector_ids and s3vectors_client and vectors_bucket:
            try:
                for i in range(0, len(vector_ids), 100):
                    s3vectors_client.delete_vectors(
                        vectorBucketName=vectors_bucket,
                        indexName=vectors_index,
                        keys=vector_ids[i:i + 100],
                    )
                log.info("Deleted %d vectors for %s", len(vector_ids), pp)
            except Exception as exc:
                log.warning("Failed to delete vectors for %s: %s", pp, exc)

        # Delete originals
        keys_to_delete = [{"Key": content_key}]
        try:
            s3.head_object(Bucket=bucket, Key=settings_key)
            keys_to_delete.append({"Key": settings_key})
        except Exception:
            pass
        s3.delete_objects(Bucket=bucket, Delete={"Objects": keys_to_delete, "Quiet": True})

        archived += 1
        log.info("Archived page: %s", pp)

    # Remove archived pages from the FTS5 index
    if pages:
        try:
            fts_remove_pages(s3, bucket, site, pages)
        except Exception as exc:
            log.warning("Failed to update FTS index after archive: %s", exc)

    return {"archived": archived, "chunks_deleted": total_chunks}


def empty_archive(s3, bucket: str, site: str) -> dict:
    """Delete everything under {site}/.user/archive/.

    Returns {"deleted": int}.
    """
    prefix = f"{site}/{_ARCHIVE_PREFIX}/"
    paginator = s3.get_paginator("list_objects_v2")
    to_delete = []
    deleted = 0

    for s3_page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in s3_page.get("Contents", []):
            to_delete.append({"Key": obj["Key"]})

        # Flush in batches of 1000 (S3 delete_objects limit)
        while len(to_delete) >= 1000:
            batch = to_delete[:1000]
            to_delete = to_delete[1000:]
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            deleted += len(batch)

    if to_delete:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": to_delete, "Quiet": True})
        deleted += len(to_delete)

    log.info("Emptied archive for site=%s: %d objects deleted", site, deleted)
    return {"deleted": deleted}

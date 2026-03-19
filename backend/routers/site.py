import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from auth import get_user_context, require_site_owner
from aws.infra import deprovision_user_infrastructure, provision_user_infrastructure
from config import CLOUDFRONT_DOMAIN, S3_BUCKET, s3
from models import ProvisionRequest, ProvisionResponse
from s3_helpers import DEFAULT_WELCOME_MD, SITE_NAME_RE, VALID_THEMES, delete_s3_prefix, delete_site_vectors
from supabase_helpers import supabase_delete_auth_user, supabase_delete_user_site, supabase_insert_user_site

router = APIRouter()


@router.post(
    "/provision",
    tags=["site"],
    summary="Provision a new site",
    description=(
        "Create a new site in S3, insert the user→site mapping in Supabase, "
        "and provision IAM/K8s/Secrets Manager infrastructure. Each user may only "
        "provision one site. Returns the public URL of the new site."
    ),
)
async def provision(
    req: ProvisionRequest,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> ProvisionResponse:
    user_id, existing_site = ctx

    if existing_site is not None:
        raise HTTPException(status_code=409, detail="User already has a provisioned site")

    if not SITE_NAME_RE.match(req.site_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid site name: must be 3-50 lowercase alphanumeric characters or hyphens, not starting or ending with a hyphen",
        )

    if req.theme not in VALID_THEMES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid theme; must be one of: {', '.join(sorted(VALID_THEMES))}",
        )

    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=f"{req.site_name}/", MaxKeys=1)
    if resp.get("KeyCount", 0) > 0:
        raise HTTPException(status_code=409, detail="Site name already taken")

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{req.site_name}/content.md",
        Body=DEFAULT_WELCOME_MD.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{req.site_name}/config.json",
        Body=json.dumps({"theme": req.theme}).encode("utf-8"),
        ContentType="application/json",
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{req.site_name}/settings.json",
        Body=json.dumps({"visibility": "private", "shared_with": []}).encode("utf-8"),
        ContentType="application/json",
    )

    theme_prefix = f"_themes/{req.theme}/"
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=theme_prefix):
        for obj in page.get("Contents", []):
            src_key = obj["Key"]
            dst_key = f"{req.site_name}/" + src_key[len(theme_prefix):]
            s3.copy_object(
                Bucket=S3_BUCKET,
                CopySource={"Bucket": S3_BUCKET, "Key": src_key},
                Key=dst_key,
            )

    supabase_insert_user_site(user_id, req.site_name, req.theme)

    try:
        await provision_user_infrastructure(user_id, req.site_name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    site_url = (
        f"https://{CLOUDFRONT_DOMAIN}/{req.site_name}"
        if CLOUDFRONT_DOMAIN
        else f"/{req.site_name}"
    )
    return ProvisionResponse(site_url=site_url)


@router.get("/my-site", tags=["site"], summary="Get the current user's site name")
async def my_site(
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Return the authenticated user's site name."""
    _, site_name = ctx
    return {"site_name": site_name}


@router.delete("/account", tags=["site"], summary="Delete account and all associated resources")
async def delete_account(
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict[str, str]:
    """Permanently delete the authenticated user's account and all associated resources."""
    user_id, site_name = ctx

    if site_name is not None:
        delete_site_vectors(site_name)
        try:
            delete_s3_prefix(site_name)
        except Exception as exc:
            logging.warning("S3 prefix delete warning for %s: %s", site_name, exc)

    await deprovision_user_infrastructure(user_id, site_name)

    supabase_delete_user_site(user_id)

    supabase_delete_auth_user(user_id)

    return {"status": "deleted"}

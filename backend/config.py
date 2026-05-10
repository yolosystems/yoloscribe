"""Centralised configuration and AWS client singletons for YoloScribe."""

import logging
import os

import boto3

# ── Environment variables ──────────────────────────────────────────────────────

S3_BUCKET = os.environ.get("S3_BUCKET", "")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
SQS_INDEXING_QUEUE_URL = os.environ.get("SQS_INDEXING_QUEUE_URL", "")
S3_VECTORS_BUCKET = os.environ.get("S3_VECTORS_BUCKET", "")
S3_VECTORS_INDEX_NAME = os.environ.get("S3_VECTORS_INDEX_NAME", "yoloscribe")
CLOUDFRONT_DOMAIN = os.environ.get("CLOUDFRONT_DOMAIN", "")
# CloudFront key pair ID (e.g. "K2JCJMDEHXQW5F") registered in the distribution's
# trusted key group.  Required for signed-cookie media auth in production.
CLOUDFRONT_SIGNING_KEY_ID = os.environ.get("CLOUDFRONT_SIGNING_KEY_ID", "")
# Separate CloudFront domain for media assets, if different from the main domain.
# Falls back to CLOUDFRONT_DOMAIN when unset.
CLOUDFRONT_MEDIA_DOMAIN = os.environ.get("CLOUDFRONT_MEDIA_DOMAIN", "") or CLOUDFRONT_DOMAIN

# Cookie domain for CloudFront signed cookies. Must be a parent domain shared by
# both the API and media CloudFront origins (e.g. .yoloscribe.com) — browsers
# reject Set-Cookie for sibling subdomains (RFC 6265). Defaults to deriving the
# apex from CLOUDFRONT_MEDIA_DOMAIN (media-dev.yoloscribe.com → .yoloscribe.com).
# Override with CLOUDFRONT_COOKIE_DOMAIN if the default derivation is wrong.
def _derive_cookie_domain(media_domain: str) -> str:
    parts = media_domain.split(".")
    return "." + ".".join(parts[-2:]) if len(parts) >= 2 else media_domain

CLOUDFRONT_COOKIE_DOMAIN = (
    os.environ.get("CLOUDFRONT_COOKIE_DOMAIN", "")
    or (_derive_cookie_domain(CLOUDFRONT_MEDIA_DOMAIN) if CLOUDFRONT_MEDIA_DOMAIN else "")
)
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8000/oauth/callback")
MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "")
FRONTEND_URL = (
    f"https://{CLOUDFRONT_DOMAIN}"
    if CLOUDFRONT_DOMAIN
    else os.environ.get("FRONTEND_URL", "http://localhost:5173")
)
BEDROCK_EMBEDDING_MODEL = os.environ.get("BEDROCK_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")

# ── Local dev mode ─────────────────────────────────────────────────────────────
# Set LOCAL_MODE=true to bypass Supabase auth, IAM/K8s provisioning, and SM.
# Use with S3_ENDPOINT_URL (MinIO) and SQS_ENDPOINT_URL (ElasticMQ) for a
# fully offline dev environment via docker-compose.

LOCAL_MODE: bool = os.environ.get("LOCAL_MODE", "").lower() in ("1", "true", "yes")
LOCAL_SITE_NAME: str = os.environ.get("LOCAL_SITE_NAME", "local")
LOCAL_USER_ID: str = os.environ.get("LOCAL_USER_ID", "local-user-00000000")
# Static Bearer token accepted by the MCP server in LOCAL_MODE.
# Defaults to "local" so the server works out of the box with no config.
# Set to a non-default value if your local backend is reachable on a network.
LOCAL_MCP_API_KEY: str = os.environ.get("LOCAL_MCP_API_KEY", "local")

S3_ENDPOINT_URL: str = os.environ.get("S3_ENDPOINT_URL", "")
SQS_ENDPOINT_URL: str = os.environ.get("SQS_ENDPOINT_URL", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
EKS_OIDC_PROVIDER = os.environ.get("EKS_OIDC_PROVIDER", "")
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "yoloscribe")

# ── Rate limiting ─────────────────────────────────────────────────────────────
# When REDIS_URL is set, rate-limit state is shared across replicas.
# Leave unset to use the in-process memory backend (single-pod deployments).

REDIS_URL: str = os.environ.get("REDIS_URL", "")

# ── Content size limits (YOL-49) ──────────────────────────────────────────────
# All limits are tunable via env vars without a code change.

MAX_CHAT_MESSAGE_BYTES: int = int(os.environ.get("MAX_CHAT_MESSAGE_BYTES", 8_192))
MAX_CHAT_CONTENT_BYTES: int = int(os.environ.get("MAX_CHAT_CONTENT_BYTES", 65_536))
MAX_CHAT_HISTORY_TURNS: int = int(os.environ.get("MAX_CHAT_HISTORY_TURNS", 20))
MAX_CONTENT_BYTES: int = int(os.environ.get("MAX_CONTENT_BYTES", 512 * 1024))              # 512 KB
MAX_SHARED_WRITE_BYTES: int = int(os.environ.get("MAX_SHARED_WRITE_BYTES", 128 * 1024))  # 128 KB
MAX_REQUEST_BYTES: int = int(os.environ.get("MAX_REQUEST_BYTES", 1024 * 1024))            # 1 MB

# ── Auth provider singletons ───────────────────────────────────────────────────

from auth_providers import create_providers  # noqa: E402

auth_provider, user_site_repo, api_token_repo = create_providers()

# ── AWS clients ────────────────────────────────────────────────────────────────

_aws_profile = os.environ.get("AWS_PROFILE")
boto_session = boto3.Session(profile_name=_aws_profile) if _aws_profile else boto3.Session()

_s3_kwargs = {"endpoint_url": S3_ENDPOINT_URL} if S3_ENDPOINT_URL else {}
_sqs_kwargs = {"region_name": AWS_REGION, **({"endpoint_url": SQS_ENDPOINT_URL} if SQS_ENDPOINT_URL else {})}

# When S3_ENDPOINT_URL is set (MinIO), use dedicated MINIO_* credentials so
# that AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are free for Bedrock.
if S3_ENDPOINT_URL:
    _minio_key = os.environ.get("MINIO_ACCESS_KEY_ID")
    _minio_secret = os.environ.get("MINIO_SECRET_ACCESS_KEY")
    if _minio_key and _minio_secret:
        _s3_kwargs["aws_access_key_id"] = _minio_key
        _s3_kwargs["aws_secret_access_key"] = _minio_secret

s3 = boto_session.client("s3", **_s3_kwargs)
sqs = boto_session.client("sqs", **_sqs_kwargs) if SQS_QUEUE_URL else None
sqs_indexing = boto_session.client("sqs", **_sqs_kwargs) if SQS_INDEXING_QUEUE_URL else None
s3vectors = boto_session.client("s3vectors", region_name=AWS_REGION) if S3_VECTORS_BUCKET else None

_sm = None if LOCAL_MODE else boto_session.client("secretsmanager", region_name=AWS_REGION)

from yolo_secrets import make_secrets_store  # noqa: E402
secrets_store = make_secrets_store(local_mode=LOCAL_MODE, s3_client=s3, bucket=S3_BUCKET, sm_client=_sm)

# ── CloudFront signed-cookie signing key ───────────────────────────────────────
# Loaded eagerly at startup so /media-auth can sign cookies without a per-request
# Secrets Manager round-trip.  Skipped in LOCAL_MODE (no CloudFront locally).

if not LOCAL_MODE and CLOUDFRONT_SIGNING_KEY_ID:
    from cloudfront_signing import load_signing_key  # noqa: E402
    load_signing_key(secrets_store)


# ── Helpers ────────────────────────────────────────────────────────────────────

def mcp_api_base() -> str:
    """Return the public base URL of this server for MCP OAuth metadata."""
    if MCP_BASE_URL:
        return MCP_BASE_URL.rstrip("/")
    return OAUTH_REDIRECT_URI.removesuffix("/oauth/callback")

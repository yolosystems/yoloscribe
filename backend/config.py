"""Centralised configuration and AWS client singletons for AgentScribe."""

import logging
import os

import boto3
import jwt as pyjwt
from jwt import PyJWKClient

# ── Environment variables ──────────────────────────────────────────────────────

S3_BUCKET = os.environ.get("S3_BUCKET", "")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
SQS_INDEXING_QUEUE_URL = os.environ.get("SQS_INDEXING_QUEUE_URL", "")
S3_VECTORS_BUCKET = os.environ.get("S3_VECTORS_BUCKET", "")
S3_VECTORS_INDEX_NAME = os.environ.get("S3_VECTORS_INDEX_NAME", "agentscribe")
CLOUDFRONT_DOMAIN = os.environ.get("CLOUDFRONT_DOMAIN", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8000/oauth/callback")
MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "")
FRONTEND_URL = (
    f"https://{CLOUDFRONT_DOMAIN}"
    if CLOUDFRONT_DOMAIN
    else os.environ.get("FRONTEND_URL", "http://localhost:5173")
)
BEDROCK_EMBEDDING_MODEL = os.environ.get("BEDROCK_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
EKS_OIDC_PROVIDER = os.environ.get("EKS_OIDC_PROVIDER", "")
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "agentscribe")

# ── Supabase JWKS client ───────────────────────────────────────────────────────

jwks_client = (
    PyJWKClient(
        f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json",
        cache_keys=True,
        lifespan=600,
    )
    if SUPABASE_URL
    else None
)

# ── AWS clients ────────────────────────────────────────────────────────────────

_aws_profile = os.environ.get("AWS_PROFILE")
boto_session = boto3.Session(profile_name=_aws_profile) if _aws_profile else boto3.Session()

s3 = boto_session.client("s3")
sqs = boto_session.client("sqs", region_name=AWS_REGION) if SQS_QUEUE_URL else None
sqs_indexing = boto_session.client("sqs", region_name=AWS_REGION) if SQS_INDEXING_QUEUE_URL else None
sm = boto_session.client("secretsmanager", region_name=AWS_REGION)
s3vectors = boto_session.client("s3vectors", region_name=AWS_REGION) if S3_VECTORS_BUCKET else None


# ── Helpers ────────────────────────────────────────────────────────────────────

def mcp_api_base() -> str:
    """Return the public base URL of this server for MCP OAuth metadata."""
    if MCP_BASE_URL:
        return MCP_BASE_URL.rstrip("/")
    return OAUTH_REDIRECT_URI.removesuffix("/oauth/callback")

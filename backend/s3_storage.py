"""Singleton S3StorageBackend instance for the backend."""

from config import S3_BUCKET, s3
from yoloscribe_io import S3StorageBackend

storage = S3StorageBackend(S3_BUCKET, s3)

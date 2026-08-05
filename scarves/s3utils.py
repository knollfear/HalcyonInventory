"""
Thin helpers for talking to the Railway S3-compatible bucket directly (boto3),
used by the image-upload feature: presigned POST for browser direct-upload, and
pulling an object back so we can decode its barcode server-side.

These are only meaningful when settings.USE_S3 is true. Callers should guard on
that; in local dev (FileSystemStorage) the bucket isn't configured.
"""
import boto3
from botocore.client import Config
from django.conf import settings


def get_s3_client():
    """boto3 S3 client pointed at Railway's endpoint (path-style, SigV4)."""
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        region_name=settings.STORAGES["default"]["OPTIONS"].get("region_name", "auto"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def presigned_post(key, content_type="image/jpeg", max_mb=15, expires=300):
    """
    Presigned POST for a browser to upload one file straight to the bucket.
    Returns {"url": ..., "fields": {...}} to hand to the client.
    """
    client = get_s3_client()
    return client.generate_presigned_post(
        Bucket=settings.S3_BUCKET_NAME,
        Key=key,
        Fields={"Content-Type": content_type, "success_action_status": "201"},
        Conditions=[
            {"Content-Type": content_type},
            {"success_action_status": "201"},
            ["content-length-range", 1, max_mb * 1024 * 1024],
        ],
        ExpiresIn=expires,
    )


def download_object(key):
    """Fetch an object's bytes from the bucket (bucket egress = free)."""
    client = get_s3_client()
    resp = client.get_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
    return resp["Body"].read()


def upload_object(key, data, content_type="image/jpeg"):
    """Write bytes to a key, replacing whatever is there.

    Used to swap a freshly-uploaded phone photo for its downscaled version.
    This goes through boto3 rather than default_storage on purpose: the storage
    backend is configured with file_overwrite=False, so saving under an existing
    name would quietly write to a *different* key and leave the original behind.
    """
    client = get_s3_client()
    client.put_object(
        Bucket=settings.S3_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType=content_type,
    )


def delete_object(key):
    """Remove an object from the bucket. Missing keys are not an error."""
    client = get_s3_client()
    client.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)


def presigned_get(key, expires=3600):
    """A presigned GET URL for viewing an object in the browser (<img> src)."""
    client = get_s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET_NAME, "Key": key},
        ExpiresIn=expires,
    )

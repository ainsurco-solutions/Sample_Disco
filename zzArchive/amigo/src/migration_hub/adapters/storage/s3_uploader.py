from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from migration_hub.adapters.base import UploadTarget
from migration_hub.adapters.irp.errors import UploadCredentialsExpiredError

class ProgressCallback(Protocol):
    def __call__(self, *, bytes_transferred: int, total_bytes: int) -> None: ...

def decode_presign_params(raw: dict[str, str]) -> dict[str, str]:
    decoded: dict[str, str] = {}
    for key, value in raw.items():
        try:
            decoded[key] = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"presignParams[{key!r}] is not valid base64: {value!r}") from exc
    return decoded

def _parse_bucket_and_key(upload_url: str) -> tuple[str, str]:
    parsed = urlparse(upload_url)
    path = parsed.path.lstrip("/")
    host_labels = parsed.netloc.split(".")
    is_virtual_hosted = len(host_labels) > 1 and host_labels[1] == "s3"
    if is_virtual_hosted:
        bucket = host_labels[0]
        key = path
    else:
        segments = path.split("/", 1)
        bucket, key = segments if len(segments) == 2 else ("", "")
    if not bucket or not key:
        raise ValueError(f"Could not extract bucket/key from upload_url: {upload_url!r}")
    return bucket, key

def _endpoint_url(upload_url: str) -> str:
    parsed = urlparse(upload_url)
    return f"{parsed.scheme}://{parsed.netloc}"

def upload_file(
    *,
    source: Path,
    target: UploadTarget,
    max_concurrency: int = 4,
    multipart_chunksize_mb: int = 64,
    on_progress: ProgressCallback | None = None,
) -> None:
    bucket, key = _parse_bucket_and_key(target.upload_url)

    client = boto3.client(
        "s3",
        endpoint_url=_endpoint_url(target.upload_url),
        aws_access_key_id=target.access_key_id,
        aws_secret_access_key=target.secret_access_key,
        aws_session_token=target.session_token,
        region_name=target.region,
    )
    transfer_config = TransferConfig(
        multipart_chunksize=multipart_chunksize_mb * 1024 * 1024,
        max_concurrency=max_concurrency,
    )

    callback = None
    if on_progress is not None:
        total_bytes = source.stat().st_size
        state = {"transferred": 0}

        def _callback(bytes_amount: int) -> None:
            state["transferred"] += bytes_amount
            on_progress(bytes_transferred=state["transferred"], total_bytes=total_bytes)

        callback = _callback

    try:
        client.upload_file(str(source), bucket, key, Config=transfer_config, Callback=callback)
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 403:
            raise UploadCredentialsExpiredError(
                f"S3 PUT for {bucket}/{key} returned 403 -- the STS token most "
                "likely expired. Request a fresh import folder rather than retrying."
            ) from exc
        raise

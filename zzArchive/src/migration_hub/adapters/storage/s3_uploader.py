from __future__ import annotations

from math import ceil

_MB = 1024 * 1024
_GB = 1024 * _MB

S3_MAX_PARTS = 10_000
S3_MIN_PART = 5 * _MB
S3_MAX_PART = 5 * _GB
DEFAULT_MIN_CHUNK = 128 * _MB

def multipart_chunksize(file_size: int, *, min_chunk: int = DEFAULT_MIN_CHUNK) -> int:
    if file_size < 0:
        raise ValueError(f"file size cannot be negative: {file_size}")

    chunk = max(min_chunk, S3_MIN_PART, ceil(file_size / S3_MAX_PARTS))
    if chunk > S3_MAX_PART:
        raise ValueError(
            f"{file_size} bytes cannot be uploaded: it needs parts of {chunk} bytes, "
            f"above S3's {S3_MAX_PART}-byte maximum part size"
        )
    return chunk

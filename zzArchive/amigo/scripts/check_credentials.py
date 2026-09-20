from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parent

_SRC = _HERE.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from sqlalchemy import Engine

from migration_hub.adapters.base import UploadTarget
from migration_hub.adapters.irp.client import IrpAdapter
from migration_hub.config.settings import Settings
from migration_hub.core.engine import create_registry_engine
from migration_hub.core.models import Base

_OK = "  [ok]"
_NO = "  [!!]"

def _redacted(value: str | None) -> str:
    if value is None:
        return "absent"
    if not value:
        return "present but EMPTY"
    return f"present ({len(value)} chars)"

def _config_dir() -> Path:
    override = os.environ.get("MIGRATION_HUB_CONFIG_DIR")
    if override:
        return Path(override)

    candidates = [_HERE.parent.parent / "config", _HERE.parent / "config", Path("config")]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return Path("config")

def _step_settings() -> Settings:
    print("1. Loading settings")
    environment = os.environ.get("MIGRATION_HUB_ENV", "dev")
    os.environ.setdefault("MIGRATION_HUB_DATABASE_URL", "sqlite://")
    config_dir = _config_dir()
    print(f"{_OK} config dir:  {config_dir}")
    settings = Settings.load(environment=environment, config_dir=config_dir)
    print(f"{_OK} environment: {settings.environment}")
    print(f"{_OK} api_host:    {settings.api_host}")
    print(f"{_OK} entitlement: {settings.entitlement}")
    if "REPLACE_WITH" in settings.api_host:
        print(f"{_NO} api_host is still the shipped placeholder -- nothing will work")
        raise SystemExit(2)
    return settings

def _step_api_key(settings: Settings) -> str:
    print(f"\n2. Reading the API key from ${settings.api_key_env_var}")
    try:
        api_key = settings.api_key()
    except RuntimeError as exc:
        print(f"{_NO} {exc}")
        print(f"{_NO} set {settings.api_key_env_var} in .env or the environment")
        raise SystemExit(2) from exc
    print(f"{_OK} key {_redacted(api_key)}")
    return api_key

def _scratch_engine() -> Engine:
    engine = create_registry_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine

def _step_upload_target(settings: Settings, api_key: str) -> UploadTarget:
    print("\n3. Requesting an upload target (POST /platform/import/v1/folders)")
    adapter = IrpAdapter(
        host=settings.api_host,
        api_key=api_key,
        entitlement=settings.entitlement,
        engine=_scratch_engine(),
    )
    target = adapter.request_upload_target(file_extension="bak")
    print(f"{_OK} folder_id:  {target.folder_id}")
    print(f"{_OK} file_uri:   {target.file_uri}")
    return target

def _step_credentials(target: UploadTarget) -> None:
    print("\n4. Checking the decoded S3 credentials")
    fields = {
        "access_key_id": target.access_key_id,
        "secret_access_key": target.secret_access_key,
        "session_token": target.session_token,
        "region": target.region,
        "upload_url": target.upload_url,
    }
    missing = [name for name, value in fields.items() if not value]
    for name, value in fields.items():
        shown = value if name == "region" and value else _redacted(value)
        print(f"{_OK if value else _NO} {name}: {shown}")

    if missing:
        print(f"{_NO} missing: {', '.join(missing)}")
        print(f"{_NO} a blank value here usually means presignParams was not base64-decoded")
        raise SystemExit(1)

    _report_url_model(target.upload_url)

_PRESIGNED_MARKERS = ("X-Amz-Signature", "X-Amz-Credential", "Signature=", "AWSAccessKeyId")

def _report_url_model(upload_url: str) -> None:
    parsed = urlparse(upload_url)
    query = parse_qs(parsed.query)
    found = [
        marker
        for marker in _PRESIGNED_MARKERS
        if marker.rstrip("=") in query or marker in parsed.query
    ]

    print("\n   S3 auth model (open question 14):")
    if found:
        print(f"{_OK} the URL carries a signature ({', '.join(found)})")
        print("       -> PRESIGNED. A plain HTTP PUT can upload; boto3 is optional")
        print("       -> record this against open question 14 -- it changes the")
        print("          minimum client-side dependency set")
    else:
        print(f"{_OK} no signature in the URL; separate STS credentials were returned")
        print("       -> ENDPOINT + CREDENTIALS. Each request is signed at call time,")
        print("          so boto3 (or an equivalent SigV4 signer) is REQUIRED")
    if parsed.query:
        print(f"       query parameters present: {', '.join(sorted(query)) or '(unparsed)'}")

def _step_upload(settings: Settings, api_key: str, target: UploadTarget, source: Path) -> None:
    print(f"\n5. Uploading {source.name} ({source.stat().st_size} bytes)")
    if not source.is_file():
        print(f"{_NO} not a file: {source}")
        raise SystemExit(2)

    adapter = IrpAdapter(
        host=settings.api_host,
        api_key=api_key,
        entitlement=settings.entitlement,
        engine=_scratch_engine(),
    )
    adapter.upload(source=source, target=target)
    print(f"{_OK} upload returned without error")
    print(
        "  note: this proves the transfer, not the import. A synthetic .bak will "
        "upload fine and still fail to import -- see TASK-0020."
    )

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read the API key from the host, ask Moody's for AWS credentials."
    )
    parser.add_argument(
        "--upload",
        type=Path,
        metavar="PATH",
        help="also upload this file to the issued target (e.g. the zz.bak set of 1)",
    )
    args = parser.parse_args()

    try:
        settings = _step_settings()
        api_key = _step_api_key(settings)
        target = _step_upload_target(settings, api_key)
        _step_credentials(target)
        if args.upload is not None:
            _step_upload(settings, api_key, target, args.upload)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\n{_NO} {type(exc).__name__}: {exc}")
        print(f"{_NO} run with PYTHONFAULTHANDLER=1 or use the CLI for a full traceback")
        return 1

    print("\nAll checks passed.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

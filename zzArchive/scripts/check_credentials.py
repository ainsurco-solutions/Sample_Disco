from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

_SRC = _HERE.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from sqlalchemy import Engine

from migration_hub.adapters.databridge.client import DatabridgeAdapter
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
    print(f"{_OK} config dir:            {config_dir}")
    settings = Settings.load(environment=environment, config_dir=config_dir)
    print(f"{_OK} environment:           {settings.environment}")
    print(f"{_OK} api_host:              {settings.api_host}")
    print(f"{_OK} databridge_instance:   {settings.databridge_instance_name}")
    if "REPLACE_WITH" in settings.api_host:
        print(f"{_NO} api_host is still the shipped placeholder -- nothing will work")
        raise SystemExit(2)
    if not settings.databridge_instance_name:
        print(f"{_NO} databridge_instance_name is not set in config/{environment}.json")
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

def _step_sql_instances(adapter: DatabridgeAdapter, settings: Settings) -> None:
    print("\n3. Listing SQL instances (GET /databridge/v1/sql-instances)")
    instances = adapter.list_sql_instances()
    names = [i.name for i in instances]
    print(f"{_OK} {len(instances)} instance(s): {', '.join(names) or '(none)'}")
    if settings.databridge_instance_name not in names:
        print(
            f"{_NO} configured databridge_instance_name "
            f"{settings.databridge_instance_name!r} is not among them"
        )
        raise SystemExit(1)
    print(f"{_OK} {settings.databridge_instance_name!r} confirmed present")

def _step_resource_group(adapter: DatabridgeAdapter) -> None:
    print(
        "\n4. Resolving the RI-DATAVAULT resource group "
        "(GET /platform/tenantdata/v1/entitlements/RI-DATAVAULT/resourcegroups)"
    )
    session = adapter.open_session()
    print(f"{_OK} resourceGroupId: {session.resource_group_id}")
    print("       needed for the archive call's x-rms-resource-group-id header")

def _step_upload_and_import(
    adapter: DatabridgeAdapter, settings: Settings, source: Path, database_name: str
) -> None:
    print(f"\n5. Uploading and importing {source.name} as {database_name!r}")
    if not source.is_file():
        print(f"{_NO} not a file: {source}")
        raise SystemExit(2)

    assert settings.databridge_instance_name is not None
    job_id = adapter.upload_and_import(
        instance_name=settings.databridge_instance_name,
        database_name=database_name,
        file_extension=source.suffix.lstrip(".") or "bak",
        source=source,
    )
    print(f"{_OK} upload + import trigger returned job_id={job_id}")

    print("   polling GET /databridge/v1/Jobs/{job_id} ...")
    status = adapter.poll_job(job_id=job_id)
    while not status.is_terminal:
        status = adapter.poll_job(job_id=job_id)
    if not status.is_success:
        print(f"{_NO} job ended in {status.raw_status!r}")
        raise SystemExit(1)
    print(f"{_OK} job succeeded: {status.raw_status!r}")

    exists = adapter.database_exists(
        instance_name=settings.databridge_instance_name, database_name=database_name
    )
    print(f"{_OK if exists else _NO} database_exists: {exists}")
    if not exists:
        raise SystemExit(1)

_TLS_MARKERS = (
    "certificate_verify_failed",
    "certificate verify failed",
    "ssl: certificate",
    "sslcertverificationerror",
    "unable to get local issuer",
    "self signed certificate",
    "self-signed certificate",
)

def _explain_if_tls(exc: BaseException) -> None:
    text = f"{type(exc).__name__} {exc}".lower()
    chained = exc.__cause__ or exc.__context__
    if chained is not None:
        text += f" {type(chained).__name__} {chained}".lower()

    if not any(marker in text for marker in _TLS_MARKERS):
        return

    print()
    print(f"{_NO} This looks like TLS INSPECTION, not a Moody's problem.")
    print("       A proxy re-signs traffic with an internal CA that Python does")
    print("       not trust: httpx uses certifi's roots only.")
    print()

    try:
        from migration_hub import _tls

        if _tls.active:
            print("       truststore IS active, so the OS store is already in use.")
            print("       The proxy's root may genuinely be missing from the Windows")
            print("       certificate store -- that is one for whoever manages the")
            print("       build, not something this script can fix.")
        else:
            print(f"       truststore is NOT active ({_tls.unavailable_reason}).")
            print("       That is the likely fix:")
            print()
            print("           pip install truststore")
            print()
            print("       It routes verification through the Windows certificate")
            print("       store, where the proxy's root already is.")
    except ImportError:
        print("       Install truststore and try again:  pip install truststore")

    print()
    print("       Do NOT set verify=False or disable certificate checking. That")
    print("       sends the API key over a connection nobody is authenticating.")

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read the API key from the host, ask Moody's Data Bridge for real."
    )
    parser.add_argument(
        "--upload",
        type=Path,
        metavar="PATH",
        help="also upload and import this file (e.g. the zz.bak set of 1)",
    )
    parser.add_argument(
        "--database",
        metavar="NAME",
        help="target database_name for --upload (defaults to the file's stem, uppercased)",
    )
    args = parser.parse_args()

    adapter: DatabridgeAdapter | None = None
    try:
        settings = _step_settings()
        api_key = _step_api_key(settings)
        adapter = DatabridgeAdapter(host=settings.api_host, api_key=api_key, engine=_scratch_engine())
        _step_sql_instances(adapter, settings)
        _step_resource_group(adapter)
        if args.upload is not None:
            database_name = args.database or args.upload.stem.upper()
            _step_upload_and_import(adapter, settings, args.upload, database_name)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\n{_NO} {type(exc).__name__}: {exc}")
        _explain_if_tls(exc)
        print(f"{_NO} run with PYTHONFAULTHANDLER=1 or use the CLI for a full traceback")
        return 1
    finally:
        if adapter is not None:
            adapter.close()

    print("\nAll checks passed.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

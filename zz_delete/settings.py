from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

def _find_env_file() -> Path:
    override = os.environ.get("RECONCILE_ENV_FILE", "").strip()
    if override:
        return Path(override)

    beside = Path(__file__).with_name(".env")
    if beside.exists():
        return beside

    repo_root = Path(__file__).resolve().parents[2] / ".env"
    if repo_root.exists():
        return repo_root

    return beside

ENV_FILE = _find_env_file()

DEFAULT_EXPOSURES_PATH = "/platform/admindata/v1/databases"

DEFAULT_BRIDGE_NAME_FIELD = "databaseName"

DEFAULT_VAULT_PATH = "/platform/admindata/v1/archives"

DEFAULT_VAULT_NAME_FIELD = "archiveName"

def _load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values

@dataclass(frozen=True)
class ApiSettings:

    host: str = ""
    entitlement: str = ""
    exposures_path: str = DEFAULT_EXPOSURES_PATH
    vault_path: str = DEFAULT_VAULT_PATH
    vault_name_field: str = DEFAULT_VAULT_NAME_FIELD
    bridge_name_field: str = DEFAULT_BRIDGE_NAME_FIELD
    _api_key: str = field(default="", repr=False)

    def __str__(self) -> str:
        return (
            f"ApiSettings(host={self.host!r}, configured={self.configured}, "
            f"has_key={self.has_key})"
        )

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    @property
    def configured(self) -> bool:
        return bool(self.host and self._api_key and self.exposures_path)

    @property
    def vault_configured(self) -> bool:
        return bool(self.host and self._api_key and self.vault_path)

    def missing(self) -> list[str]:
        gaps = []
        if not self.host:
            gaps.append("RECONCILE_API_HOST")
        if not self._api_key:
            gaps.append("MOODYS_API_KEY")
        if not self.exposures_path:
            gaps.append("RECONCILE_EXPOSURES_PATH")
        return gaps

    def exposures_url(self) -> str:
        return f"{self.host.rstrip('/')}{self.exposures_path}"

    def vault_url(self) -> str:
        return f"{self.host.rstrip('/')}{self.vault_path}"

    @property
    def api_key(self) -> str:
        return self._api_key

INVENTORY_QUERY = (
    "SELECT name, create_date FROM sys.databases "
    "WHERE database_id > 4 "
    "ORDER BY name"
)

DEFAULT_COMPLETION_DATE = "2026-10-30"

def completion_date(path: Path = ENV_FILE) -> date:
    raw = (
        _load_env_file(path).get("RECONCILE_COMPLETION_DATE")
        or os.environ.get("RECONCILE_COMPLETION_DATE")
        or DEFAULT_COMPLETION_DATE
    ).strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return date.fromisoformat(DEFAULT_COMPLETION_DATE)

@dataclass(frozen=True)
class SqlServer:

    label: str
    host: str = ""
    driver: str = "ODBC Driver 18 for SQL Server"
    trust_certificate: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.host)

    def connection_string(self) -> str:
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={self.host}",
            "DATABASE=master",
            "Trusted_Connection=yes",
            "Encrypt=yes",
        ]
        if self.trust_certificate:
            parts.append("TrustServerCertificate=yes")
        return ";".join(parts) + ";"

@dataclass(frozen=True)
class SqlSettings:

    direct: SqlServer
    ri: SqlServer

    @property
    def any_configured(self) -> bool:
        return self.direct.configured or self.ri.configured

    def servers(self) -> list[SqlServer]:
        return [s for s in (self.direct, self.ri) if s.configured]

def load_sql_settings(path: Path = ENV_FILE) -> SqlSettings:
    from_file = _load_env_file(path)

    def value(name: str, default: str = "") -> str:
        return os.environ.get(name) or from_file.get(name) or default

    driver = value("RECONCILE_SQL_DRIVER", "ODBC Driver 18 for SQL Server")
    trust = value("RECONCILE_SQL_TRUST_CERT", "yes").strip().casefold() in (
        "y",
        "yes",
        "true",
        "1",
    )
    return SqlSettings(
        direct=SqlServer(
            label="Direct",
            host=value("RECONCILE_SQL_DIRECT_HOST"),
            driver=driver,
            trust_certificate=trust,
        ),
        ri=SqlServer(
            label="RI",
            host=value("RECONCILE_SQL_RI_HOST"),
            driver=driver,
            trust_certificate=trust,
        ),
    )

AMIGO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

def _amigo_config(environment: str = "") -> dict[str, str]:
    env = (
        environment
        or os.environ.get("MIGRATION_HUB_ENV", "")
        or _load_env_file().get("MIGRATION_HUB_ENV", "")
        or "dev"
    ).strip()
    for name in (f"{env}.json", f"{env}.example.json"):
        candidate = AMIGO_CONFIG_DIR / name
        if candidate.exists():
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            return {k: str(v) for k, v in loaded.items() if isinstance(v, str)}
    return {}

@dataclass(frozen=True)
class ConfigConflict:

    fact: str
    amigo_key: str
    amigo_value: str
    reconcile_key: str
    reconcile_value: str

    def __str__(self) -> str:
        return (
            f"{self.fact}: AMIGO's {self.amigo_key}={self.amigo_value!r} but "
            f"{self.reconcile_key}={self.reconcile_value!r}"
        )

def config_conflicts(path: Path | None = None) -> list[ConfigConflict]:
    amigo = _amigo_config()
    if not amigo:
        return []

    from_file = _load_env_file(path or ENV_FILE)

    def reconcile_value(name: str) -> str:
        return (os.environ.get(name) or from_file.get(name) or "").strip()

    pairs = (
        ("Moody's host", "api_host", "RECONCILE_API_HOST"),
        ("entitlement", "entitlement", "RECONCILE_ENTITLEMENT"),
    )

    conflicts = []
    for fact, amigo_key, reconcile_key in pairs:
        theirs = str(amigo.get(amigo_key, "")).strip()
        ours = reconcile_value(reconcile_key)
        if theirs and ours and theirs != ours:
            conflicts.append(
                ConfigConflict(fact, amigo_key, theirs, reconcile_key, ours)
            )
    return conflicts

def load_settings(path: Path = ENV_FILE) -> ApiSettings:
    from_file = _load_env_file(path)

    def value(name: str, default: str = "") -> str:
        return os.environ.get(name) or from_file.get(name) or default

    amigo = _amigo_config()

    def shared(reconcile_key: str, amigo_key: str, default: str = "") -> str:
        explicit = os.environ.get(reconcile_key) or from_file.get(reconcile_key)
        if explicit:
            return explicit
        return str(amigo.get(amigo_key, "")).strip() or default

    return ApiSettings(
        host=shared("RECONCILE_API_HOST", "api_host"),
        entitlement=shared("RECONCILE_ENTITLEMENT", "entitlement"),
        exposures_path=value("RECONCILE_EXPOSURES_PATH", DEFAULT_EXPOSURES_PATH),
        vault_path=value("RECONCILE_VAULT_PATH", DEFAULT_VAULT_PATH),
        vault_name_field=value(
            "RECONCILE_VAULT_NAME_FIELD", DEFAULT_VAULT_NAME_FIELD
        ),
        bridge_name_field=value(
            "RECONCILE_BRIDGE_NAME_FIELD", DEFAULT_BRIDGE_NAME_FIELD
        ),
        _api_key=value("MOODYS_API_KEY"),
    )

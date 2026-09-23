from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

_CREDENTIAL_LIKE_SUBSTRINGS = ("key", "secret", "password", "token", "credential")

_ENV_ONLY_FIELDS = frozenset({"database_url"})

RETIRED_FIELDS = frozenset(
    {"max_concurrent_imports", "backoff_base_seconds", "backoff_max_seconds"}
)

_LOG = logging.getLogger(__name__)

class ConfigCredentialError(RuntimeError):
    pass

class Settings(BaseModel):

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_root: Path
    file_pattern: str = "*.bak"

    database_url: str

    api_host: str
    api_key_env_var: str

    databridge_instance_name: str | None = None
    databridge_instances: dict[str, str] = Field(default_factory=dict)

    max_concurrent_uploads: int = 1
    poll_interval_seconds: int = 60
    max_poll_minutes: int = 360

    max_attempts: int = 3
    max_archive_attempts: int = Field(default=9, ge=1)
    claim_timeout_minutes: int = 420

    scheduler_poll_interval_seconds: float = 60.0
    scheduler_reap_interval_seconds: float = 300.0

    queue_snapshot_path: Path = Path("var/databridge-queue.json")
    queue_check_ttl_minutes: int = Field(default=5, ge=1)
    queue_hold_active_imports: int = Field(default=10, ge=1)
    queue_investigate_failures_24h: int = Field(default=5, ge=1)

    compute_checksums: bool = False
    dry_run: bool = True
    environment: str

    @classmethod
    def load(cls, *, environment: str, config_dir: Path) -> Settings:
        config_path = config_dir / f"{environment}.json"
        if not config_path.exists():
            raise FileNotFoundError(
                f"No config file for environment {environment!r}: {config_path}"
            )

        raw: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
        raw.pop("$comment", None)
        _reject_credential_like_keys(raw, source=config_path)

        retired = sorted(RETIRED_FIELDS & raw.keys())
        if retired:
            for field in retired:
                raw.pop(field)
            _LOG.warning(
                "%s sets %s, which no longer exist and were never read -- ignored. "
                "Remove them from the file.",
                config_path,
                ", ".join(retired),
            )

        for field in _ENV_ONLY_FIELDS:
            if field in raw:
                raise ConfigCredentialError(
                    f"{config_path} sets {field!r}. That field is supplied by the "
                    "environment, never by a committed config file."
                )

        raw["database_url"] = _require_env("MIGRATION_HUB_DATABASE_URL")
        raw.setdefault("environment", environment)

        settings = cls.model_validate(raw)
        if settings.environment != environment:
            raise ValueError(
                f"Requested environment {environment!r} but {config_path} declares "
                f"{settings.environment!r}"
            )
        return settings

    @model_validator(mode="after")
    def _claim_timeout_exceeds_poll_ceiling(self) -> Settings:
        if self.claim_timeout_minutes <= self.max_poll_minutes:
            raise ValueError(
                f"claim_timeout_minutes ({self.claim_timeout_minutes}) must exceed "
                f"max_poll_minutes ({self.max_poll_minutes}) -- otherwise reap/"
                "poll_running_imports can reclaim a claim a live worker still owns"
            )
        return self

    def api_key(self) -> str:
        return _require_env(self.api_key_env_var)

def _require_env(name: str) -> str:
    try:
        return os.environ[name]
    except KeyError as exc:
        raise RuntimeError(f"Required environment variable {name!r} is not set") from exc

def _reject_credential_like_keys(raw: dict[str, Any], *, source: Path) -> None:
    for key, value in raw.items():
        lowered = key.lower()
        if key != "api_key_env_var" and any(
            marker in lowered for marker in _CREDENTIAL_LIKE_SUBSTRINGS
        ):
            raise ConfigCredentialError(
                f"{source} contains key {key!r}, which looks like a credential. "
                "Secrets belong in the environment, never in a committed config file."
            )
        if isinstance(value, dict):
            _reject_credential_like_keys(value, source=source)

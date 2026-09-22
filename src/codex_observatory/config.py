from __future__ import annotations

import ipaddress
import os
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .paths import RuntimePaths, resolve_paths


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(StrictModel):
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8765

    @field_validator("host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("server.host must be a valid IP address") from exc
        if not address.is_loopback:
            raise ValueError("server.host must be loopback-only in v1")
        return value


class StorageConfig(StrictModel):
    sqlite_path: Path | None = None
    parquet_root: Path | None = None
    spool_root: Path | None = None
    sqlite_synchronous: Literal["OFF", "NORMAL", "FULL", "EXTRA"] = "NORMAL"
    write_batch_size: Annotated[int, Field(gt=0)] = 100
    write_batch_max_wait_ms: Annotated[int, Field(gt=0)] = 50


class OtlpConfig(StrictModel):
    enabled: bool = True
    max_decompressed_bytes: Annotated[int, Field(gt=0)] = 67108864
    accept_protobuf: bool = True
    accept_json: bool = True
    accept_gzip: bool = True


class HooksConfig(StrictModel):
    enabled: bool = True
    poll_interval_ms: Annotated[int, Field(gt=0)] = 250


class GitConfig(StrictModel):
    enabled: bool = True
    command_timeout_seconds: Annotated[int, Field(gt=0)] = 5


class AppServerConfig(StrictModel):
    enabled: bool = True
    mode: Literal["disabled", "reconcile", "controlled_live"] = "reconcile"
    reconcile_interval_seconds: Annotated[int, Field(gt=0)] = 30
    include_turns: bool = True


class OpenAIAdminConfig(StrictModel):
    enabled: bool = False
    sync_interval_minutes: Annotated[int, Field(gt=0)] = 60
    initial_lookback_hours: Annotated[int, Field(gt=0)] = 24
    overlap_hours: Annotated[int, Field(ge=0)] = 1
    bucket_width: Literal["1m", "1h", "1d"] = "1h"
    group_by: list[Literal["project_id", "user_id", "api_key_id", "model", "batch", "service_tier"]] = ["project_id", "model"]
    costs_enabled: bool = False
    costs_group_by: list[Literal["project_id", "line_item", "api_key_id"]] = ["project_id", "line_item"]


class CollectorsConfig(StrictModel):
    otlp: OtlpConfig = OtlpConfig()
    hooks: HooksConfig = HooksConfig()
    git: GitConfig = GitConfig()
    app_server: AppServerConfig = AppServerConfig()
    openai_admin: OpenAIAdminConfig = OpenAIAdminConfig()


class PrivacyConfig(StrictModel):
    mode: Literal["minimal", "forensic"] = "minimal"
    store_prompts: bool = False
    store_tool_arguments: Literal["off", "digest", "full"] = "digest"
    store_tool_output: Literal["off", "digest", "full"] = "digest"
    persist_raw_wire_payloads: bool = False
    processing_spool_max_hours: Annotated[int, Field(gt=0)] = 1

    @model_validator(mode="after")
    def enforce_forensic_controls(self) -> PrivacyConfig:
        if self.mode != "forensic" and (self.store_prompts or self.persist_raw_wire_payloads):
            raise ValueError("prompts and raw wire payloads require privacy.mode='forensic'")
        if self.mode == "minimal" and (self.store_tool_arguments == "full" or self.store_tool_output == "full"):
            raise ValueError("full tool arguments/output require privacy.mode='forensic'")
        return self


class RetentionConfig(StrictModel):
    enabled: bool = True
    hot_days: Annotated[int, Field(ge=0)] = 30
    raw_metadata_days: Annotated[int, Field(ge=0)] = 7
    forensic_raw_days: Annotated[int, Field(ge=0)] = 7
    historical_days: Annotated[int, Field(ge=0)] = 365
    aggregate_days: Annotated[int, Field(ge=0)] = 0
    run_interval_hours: Annotated[int, Field(gt=0)] = 24


class CompatibilityConfig(StrictModel):
    unknown_codex_version: Literal["warn", "fail"] = "warn"
    require_generated_app_server_schema: bool = True


class ObservatoryConfig(StrictModel):
    schema_version: Literal[1] = 1
    server: ServerConfig = ServerConfig()
    storage: StorageConfig = StorageConfig()
    collectors: CollectorsConfig = CollectorsConfig()
    privacy: PrivacyConfig = PrivacyConfig()
    retention: RetentionConfig = RetentionConfig()
    compatibility: CompatibilityConfig = CompatibilityConfig()

    def resolve_paths(self, paths: RuntimePaths | None = None) -> RuntimePaths:
        paths = paths or resolve_paths()
        for field in ("sqlite_path", "parquet_root", "spool_root"):
            value = getattr(self.storage, field)
            if value is not None:
                setattr(self.storage, field, value.expanduser().resolve())
        return paths


def load_config(path: Path | None = None) -> ObservatoryConfig:
    config_path = path or Path("config/observatory.example.toml")
    if not config_path.exists():
        return ObservatoryConfig()
    with config_path.open("rb") as stream:
        return ObservatoryConfig.model_validate(tomllib.load(stream))


def admin_key_present() -> bool:
    return bool(os.environ.get("OPENAI_ADMIN_KEY"))

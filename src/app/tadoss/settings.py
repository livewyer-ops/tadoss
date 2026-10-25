"""Validated process settings shared by the CLI, API and worker."""

import math
import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from tadoss import RELEASE


def parse_flag(value: str) -> bool:
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError("Boolean setting must be true or false")


def env_flag(name: str, default: bool | None) -> bool | None:
    value = os.getenv(name)
    return default if value is None else parse_flag(value)


@dataclass(frozen=True)
class Settings:
    database: str = ""
    database_ssl_root_cert: str = ""
    schema_target: str = RELEASE["schemaTarget"]
    api_token: str = ""
    basic_auth_username: str = "tadoss"
    basic_auth_password: str = ""
    dev: bool = False
    docs: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    pool_min_size: int = 1
    pool_max_size: int = 10
    pool_timeout: float = 30.0
    statement_timeout: str = "30s"
    migrate_timeout: float = 300.0
    oauth2_issuer: str = ""
    oauth2_audience: str = ""
    oauth2_jwks_uri: str = ""
    oauth2_algorithms: str = "RS256"
    oauth2_read_scope: str = "tads-api/read"
    oauth2_write_scope: str = "tads-api/write"
    oauth2_delete_scope: str = "tads-api/delete"
    oauth2_admin_scope: str = "tads-api/admin"
    worker_poll_interval_seconds: float = 5.0
    worker_lease_seconds: int = 60
    worker_max_requests: int = 50
    worker_health_port: int = 8001
    worker_queue_retention_seconds: int = 604800
    webhook_timeout_seconds: float = 10.0
    webhook_max_attempts: int = 5
    webhook_allowed_hosts: str = ""
    delete_batch_size: int = 1000
    delete_max_attempts: int = 5
    delete_sync_seconds: float = 1.0
    log_level: str = "info"
    log_format: str = "json"

    @classmethod
    def load(cls, **overrides: Any) -> Self:
        values = {}
        for field in fields(cls):
            name = "TADOSS_" + field.name.upper()
            raw = os.getenv(name)
            secret_file = os.getenv(name + "_FILE")
            if secret_file:
                if raw is not None:
                    raise ValueError(f"Set only one of {name} and {name}_FILE")
                try:
                    raw = Path(secret_file).read_text().rstrip("\r\n")
                except OSError:
                    raise ValueError(f"Cannot read {name}_FILE") from None
            if raw is None:
                continue
            try:
                values[field.name] = (
                    parse_flag(raw) if field.type is bool else type(field.default)(raw)
                )
            except (ValueError, TypeError):
                raise ValueError(f"Invalid {name}") from None
        values.update(
            {key: value for key, value in overrides.items() if value is not None}
        )
        settings = cls(**values)
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.schema_target != RELEASE["schemaTarget"]:
            raise ValueError("Image does not support TADOSS_SCHEMA_TARGET")
        for field in fields(self):
            value = getattr(self, field.name)
            if field.type in (int, float) and (
                not math.isfinite(value)
                or value < 0
                or (
                    value == 0
                    and field.name
                    not in {"pool_min_size", "worker_queue_retention_seconds"}
                )
            ):
                raise ValueError(f"TADOSS_{field.name.upper()} must be positive")
        if not re.fullmatch(r"[1-9][0-9]*(ms|s|min|h)?", self.statement_timeout):
            raise ValueError(
                "Invalid TADOSS_STATEMENT_TIMEOUT; use a positive duration"
            )
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("TADOSS_POOL_MIN_SIZE exceeds TADOSS_POOL_MAX_SIZE")
        if not 1 <= self.port <= 65535 or not 1 <= self.worker_health_port <= 65535:
            raise ValueError(
                "TADOSS_PORT and TADOSS_WORKER_HEALTH_PORT must be valid ports"
            )
        if self.log_format not in {"json", "text"} or self.log_level.lower() not in {
            "debug",
            "info",
            "warning",
            "error",
            "critical",
        }:
            raise ValueError("Invalid TADOSS_LOG_FORMAT or TADOSS_LOG_LEVEL")
        if self.oauth2_issuer:
            if not self.oauth2_audience or not self.oauth2_jwks_uri:
                raise ValueError(
                    "OAuth2 requires TADOSS_OAUTH2_AUDIENCE and TADOSS_OAUTH2_JWKS_URI"
                )
            for name in ("oauth2_issuer", "oauth2_jwks_uri"):
                parsed = urlsplit(getattr(self, name))
                if (
                    parsed.scheme not in ({"https", "http"} if self.dev else {"https"})
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    raise ValueError(f"Invalid TADOSS_{name.upper()}")
            if not set(self.oauth2_algorithms.split(",")) <= {
                "RS256",
                "RS384",
                "RS512",
                "ES256",
                "ES384",
                "ES512",
                "PS256",
                "PS384",
                "PS512",
                "EdDSA",
            }:
                raise ValueError(
                    "TADOSS_OAUTH2_ALGORITHMS must contain asymmetric signing "
                    "algorithms"
                )
        elif self.oauth2_audience or self.oauth2_jwks_uri:
            raise ValueError("Set TADOSS_OAUTH2_ISSUER")

    def database_url(self) -> str:
        if self.database:
            if self.database_ssl_root_cert:
                from psycopg.conninfo import make_conninfo

                return make_conninfo(
                    self.database,
                    sslmode="verify-full",
                    sslrootcert=self.database_ssl_root_cert,
                )
            return self.database
        if self.dev:
            return "postgresql://tadoss:tadoss@127.0.0.1:15432/tadoss"
        raise ValueError("Set TADOSS_DATABASE")

    def require_auth(self) -> None:
        if not (
            self.api_token or self.basic_auth_password or self.oauth2_issuer or self.dev
        ):
            raise ValueError(
                "Configure TADOSS_API_TOKEN, Basic credentials or OAuth2; "
                "TADOSS_DEV=true permits local anonymous access"
            )

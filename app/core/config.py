from typing import List, Union
from pydantic import AnyHttpUrl, PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import json
import re

class Settings(BaseSettings):
    PROJECT_NAME: str
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str
    SECRET_KEY_PREVIOUS: str | None = None
    ENV: str = "production"

    # Database
    POSTGRES_SERVER: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    POSTGRES_PORT: int = 5432
    DATABASE_URL: Union[str, PostgresDsn] = ""

    @field_validator("DATABASE_URL", mode="before")
    def assemble_db_connection(cls, v: str | None, info) -> str:
        if isinstance(v, str) and v:
            return v
        
        # Build URL if not provided directly
        return str(PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=info.data.get("POSTGRES_USER"),
            password=info.data.get("POSTGRES_PASSWORD"),
            host=info.data.get("POSTGRES_SERVER"),
            port=info.data.get("POSTGRES_PORT"),
            path=info.data.get("POSTGRES_DB", ""),
        ))

    # Redis
    REDIS_URL: str
    REDIS_SOCKET_TIMEOUT_SECONDS: float = 2.0

    # Meta
    META_APP_SECRET: str
    META_APP_SECRET_PREVIOUS: str | None = None
    META_VERIFY_TOKEN: str
    META_GRAPH_API_VERSION: str = "v26.0"
    INSTAGRAM_ACCESS_TOKEN: str
    INSTAGRAM_PAGE_ID: str

    # Telegram
    TELEGRAM_BOT_TOKEN: str
    ADMIN_IDS: List[int] = []

    # Operations API. Keep a previous value only during a bounded rotation window.
    OPS_API_TOKEN: str
    OPS_API_TOKEN_PREVIOUS: str | None = None

    # Public ingress controls.
    MAX_WEBHOOK_BODY_BYTES: int = 1_048_576
    WEBHOOK_INGRESS_RATE_LIMIT_PER_MINUTE: int = 300
    WEBHOOK_RATE_LIMIT_PER_MINUTE: int = 120
    PUBLIC_RATE_LIMIT_PER_MINUTE: int = 60

    # Durable Redis-backed webhook inbox.
    WEBHOOK_DEDUP_TTL_SECONDS: int = 604_800
    WEBHOOK_PROCESSING_LEASE_SECONDS: int = 300
    WEBHOOK_MAX_ATTEMPTS: int = 6
    WEBHOOK_RETRY_BASE_SECONDS: int = 5
    WEBHOOK_QUEUE_MAX_DEPTH: int = 250
    OUTBOUND_QUEUE_MAX_DEPTH: int = 1_000
    WORKER_POOL_SIZE: int = 4

    # Schema creation is convenient locally but must be migration-owned in production.
    DB_AUTO_CREATE_SCHEMA: bool = False

    @field_validator("ADMIN_IDS", mode="before")
    def parse_admin_ids(cls, v: Union[str, List[int]]) -> List[int]:
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return []
        return v

    @field_validator("META_GRAPH_API_VERSION")
    @classmethod
    def validate_meta_graph_api_version(cls, value: str) -> str:
        if not re.fullmatch(r"v[1-9]\d*\.0", value):
            raise ValueError("META_GRAPH_API_VERSION must look like v26.0")
        return value

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

    @property
    def meta_app_secrets(self) -> List[str]:
        return [value for value in (self.META_APP_SECRET, self.META_APP_SECRET_PREVIOUS) if value]

    @property
    def ops_api_tokens(self) -> List[str]:
        return [value for value in (self.OPS_API_TOKEN, self.OPS_API_TOKEN_PREVIOUS) if value]

    @property
    def encryption_keys(self) -> List[str]:
        return [value for value in (self.SECRET_KEY, self.SECRET_KEY_PREVIOUS) if value]

    @model_validator(mode="after")
    def reject_insecure_production_secrets(self):
        if self.ENV.lower() != "production":
            return self

        checks = {
            "SECRET_KEY": (self.SECRET_KEY, 32),
            "META_APP_SECRET": (self.META_APP_SECRET, 16),
            "META_VERIFY_TOKEN": (self.META_VERIFY_TOKEN, 16),
            "OPS_API_TOKEN": (self.OPS_API_TOKEN, 32),
            "TELEGRAM_BOT_TOKEN": (self.TELEGRAM_BOT_TOKEN, 16),
            "INSTAGRAM_ACCESS_TOKEN": (self.INSTAGRAM_ACCESS_TOKEN, 16),
        }
        optional_checks = {
            "SECRET_KEY_PREVIOUS": (self.SECRET_KEY_PREVIOUS, 32),
            "META_APP_SECRET_PREVIOUS": (self.META_APP_SECRET_PREVIOUS, 16),
            "OPS_API_TOKEN_PREVIOUS": (self.OPS_API_TOKEN_PREVIOUS, 32),
        }
        checks.update(
            {name: (value, minimum) for name, (value, minimum) in optional_checks.items() if value}
        )
        forbidden_fragments = (
            "changethis",
            "replace-with",
            "replace_with",
            "your_",
            "example",
            "placeholder",
        )
        invalid = [
            name
            for name, (value, minimum) in checks.items()
            if len(value.strip()) < minimum
            or any(fragment in value.lower() for fragment in forbidden_fragments)
        ]
        if invalid:
            raise ValueError(
                "Insecure production secret configuration: " + ", ".join(sorted(invalid))
            )
        return self

settings = Settings()

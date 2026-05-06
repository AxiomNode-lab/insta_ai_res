from typing import List, Union
from pydantic import AnyHttpUrl, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import json

class Settings(BaseSettings):
    PROJECT_NAME: str
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str
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

    # Meta
    META_APP_SECRET: str
    META_VERIFY_TOKEN: str
    INSTAGRAM_ACCESS_TOKEN: str
    INSTAGRAM_PAGE_ID: str

    # Telegram
    TELEGRAM_BOT_TOKEN: str
    ADMIN_IDS: List[int] = []

    @field_validator("ADMIN_IDS", mode="before")
    def parse_admin_ids(cls, v: Union[str, List[int]]) -> List[int]:
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return []
        return v

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

settings = Settings()

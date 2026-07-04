from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AI Interview Analysis Platform"
    environment: str = "development"

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "interview_growth_platform"

    jwt_secret_key: str = Field(default="change-this-secret")
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 1440
    jwt_password_reset_expire_minutes: int = 15

    admin_sync_token: str | None = None
    student_sheet_url: str | None = None

    cors_origins: List[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]
    cors_origin_regex: str | None = r"^http://(localhost|127\.0\.0\.1):\d+$"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

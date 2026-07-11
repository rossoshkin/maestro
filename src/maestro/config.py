"""Runtime configuration for the Maestro process."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_prefix="MAESTRO_", extra="ignore")

    database_url: str = "sqlite:///./data/maestro.db"
    artifact_root: Path = Path("./data/artifacts")
    workspace_root: Path = Path("./data/workspaces")
    log_level: str = Field(default="INFO", pattern=r"^[A-Za-z]+$")
    bind_address: str = "127.0.0.1"
    port: int = Field(default=7860, ge=1, le=65535)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached process settings."""

    return Settings()

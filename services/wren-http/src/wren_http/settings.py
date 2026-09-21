from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WREN_HTTP_", env_file=".env", extra="ignore")

    workspaces_file: Path
    duckdb_root: Path
    wren_home: Path | None = None
    http_host: str = "0.0.0.0"
    http_port: int = Field(default=8001, ge=1, le=65535)
    http_workers: int = Field(default=1, ge=1, le=8)
    admin_token: SecretStr | None = None
    model_api_key: SecretStr | None = None
    model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_name: str = "qwen3.7-plus"
    model_timeout_seconds: float = Field(default=60, gt=0, le=180)
    model_request_limit: int = Field(default=12, ge=2, le=30)
    memory_enabled: bool = True

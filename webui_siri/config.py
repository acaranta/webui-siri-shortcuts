from __future__ import annotations

from typing import Optional

from pydantic import AnyUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    open_webui_url: AnyUrl = Field(..., alias="OPEN_WEBUI_URL")
    open_webui_token: str = Field(..., alias="OPEN_WEBUI_TOKEN")
    open_webui_model: str = Field(..., alias="OPEN_WEBUI_MODEL")
    api_key: str = Field(..., alias="API_KEY")
    api_port: int = Field(8080, alias="API_PORT")
    open_webui_folder: Optional[str] = Field(None, alias="OPEN_WEBUI_FOLDER")

    def validate_values(self) -> None:
        if not self.api_key:
            raise ValueError("API_KEY must be set to a non-empty value")
        if not self.open_webui_model:
            raise ValueError("OPEN_WEBUI_MODEL must be set")


def load_config() -> AppConfig:
    config = AppConfig()
    config.validate_values()
    return config

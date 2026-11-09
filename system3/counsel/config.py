from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env", extra="ignore"
    )

    paypal_client_id: SecretStr = SecretStr("")
    paypal_client_secret: SecretStr = SecretStr("")
    paypal_base_url: Literal["https://api-m.sandbox.paypal.com"] = (
        "https://api-m.sandbox.paypal.com"
    )
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/gawah_system3"
    )
    system2_base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")
    system2_key: SecretStr = SecretStr("")
    system3_key: SecretStr = SecretStr("")

    @property
    def database_dsn(self):
        url = make_url(self.database_url.get_secret_value())
        if url.drivername in {"postgres", "postgresql"}:
            return url.set(drivername="postgresql+psycopg")
        return url

    def database_engine(self):
        return create_engine(self.database_dsn, pool_pre_ping=True, hide_parameters=True)

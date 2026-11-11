from pathlib import Path
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, StringConstraints, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

SYSTEM3_DIR = Path(__file__).resolve().parents[1]
OperatorId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class OperatorConfig(BaseModel):
    role: Literal["reviewer", "operator", "viewer"]
    token_sha256: SecretStr

    @field_validator("token_sha256")
    @classmethod
    def valid_digest(cls, value: SecretStr) -> SecretStr:
        digest = value.get_secret_value()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("Use a lowercase SHA-256 token digest.")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=SYSTEM3_DIR / ".env", extra="ignore"
    )

    paypal_client_id: SecretStr = SecretStr("")
    paypal_client_secret: SecretStr = SecretStr("")
    paypal_base_url: Literal["https://api-m.sandbox.paypal.com"] = (
        "https://api-m.sandbox.paypal.com"
    )
    database_url: SecretStr = SecretStr(f"sqlite:///{(SYSTEM3_DIR / 'counsel.db').as_posix()}")
    system2_base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")
    system2_key: SecretStr = SecretStr("")
    system3_key: SecretStr = SecretStr("")
    enable_sandbox_resolution: bool = False
    n5_operators: dict[OperatorId, OperatorConfig] = Field(default_factory=dict)
    groq_api_key: SecretStr = SecretStr("")
    groq_model: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ] = "openai/gpt-oss-20b"

    @property
    def database_dsn(self):
        url = make_url(self.database_url.get_secret_value())
        if url.drivername in {"postgres", "postgresql"}:
            return url.set(drivername="postgresql+psycopg")
        if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
            path = Path(url.database)
            if not path.is_absolute():
                return url.set(database=str(SYSTEM3_DIR / path))
        return url

    def database_engine(self):
        return create_engine(self.database_dsn, pool_pre_ping=True, hide_parameters=True)

    @property
    def local_sandbox_resolution(self) -> bool:
        """Whether simulated refund actions may run against a local System 2 sandbox."""
        return self.enable_sandbox_resolution and self.system2_base_url.host in {
            "127.0.0.1",
            "localhost",
            "::1",
        }

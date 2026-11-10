"""Runtime configuration for the System 1 shopper service."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """Settings loaded from process environment, falling back to the root .env file."""

    groq_api_key: str
    # "auto" discovers currently enabled Groq models and uses the approved priority list.
    groq_model: str = "auto"
    product_provider: Literal["demo", "channel3"] = "demo"
    channel3_api_key: str | None = None
    attack_simulation_enabled: bool = False
    merchant_signing_key_path: str = "keys/system1-merchant"
    model_config = SettingsConfigDict(
        env_file=ROOT_ENV_FILE,
        env_file_encoding="utf-8",
        # The shared template deliberately leaves the Guardian's GROQ_MODEL blank;
        # treat that as unset so System 1 retains its safe automatic selection default.
        env_ignore_empty=True,
        extra="ignore",
    )

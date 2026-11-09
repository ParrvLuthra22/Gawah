from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_env: str = "sandbox"
    buyer_email: str = ""
    buyer_password: str = ""
    groq_api_key: str = ""
    channel3_api_key: str = ""
    database_url: str = "sqlite:///./gawah.db"
    ledger_signing_key_path: str = ""
    # Service keys: one per calling service (the UI uses SYSTEM3_KEY).
    system1_key: str = ""
    system3_key: str = ""
    # When true (default), /v1 endpoints return fixtures instead of running real logic.
    gawah_mock: bool = True


settings = Settings()

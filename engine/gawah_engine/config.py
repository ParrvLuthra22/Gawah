from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_env: str = "sandbox"
    groq_api_key: str = ""
    channel3_api_key: str = ""
    database_url: str = "sqlite:///./gawah.db"
    ledger_signing_key_path: str = ""


settings = Settings()

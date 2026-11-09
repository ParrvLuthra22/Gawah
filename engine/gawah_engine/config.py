from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_env: str = "sandbox"
    # Sandbox test card for the card payment route. Config only: never hard-code, log or ledger.
    paypal_card_number: str = ""
    paypal_card_expiry: str = ""  # YYYY-MM
    paypal_card_cvv: str = ""
    paypal_card_name: str = "Gawah Demo"
    buyer_email: str = ""
    buyer_password: str = ""
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"
    # Ordered, comma-separated fallbacks tried after GROQ_MODEL when a model is not found (404)
    groq_models: str = "openai/gpt-oss-20b,openai/gpt-oss-120b"
    channel3_api_key: str = ""
    database_url: str = "sqlite:///./gawah.db"
    # Directories holding ES256 key files (generated on first run). Empty = ./keys
    ledger_signing_key_path: str = ""
    mandate_signing_key_path: str = ""
    # Service keys: one per calling service (the UI uses SYSTEM3_KEY).
    system1_key: str = ""
    system3_key: str = ""
    # Per-key request limit (sliding 60 s window, in memory, per process). 0 disables.
    rate_limit_per_minute: int = 60
    # GET /v1/ledger/stream: keepalive comment interval and database poll interval (seconds)
    sse_keepalive_s: float = 15.0
    sse_poll_s: float = 0.25
    # Test switch: use the SIMULATED PayPal. Refused unless GAWAH_ENV=test.
    gawah_env: str = ""
    gawah_fake_paypal: bool = False
    # When true (default), /v1 endpoints return fixtures instead of running real logic.
    gawah_mock: bool = True


settings = Settings()

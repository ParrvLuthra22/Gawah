import re

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_EXPIRY_FORMATS = (
    (re.compile(r"^(\d{4})-(\d{2})$"), lambda m: (m[1], m[2])),
    (re.compile(r"^(\d{2})/(\d{4})$"), lambda m: (m[2], m[1])),
    (re.compile(r"^(\d{2})/(\d{2})$"), lambda m: ("20" + m[2], m[1])),
)


def normalize_expiry(value: str) -> str | None:
    """`MM/YYYY`, `MM/YY` or `YYYY-MM` -> `YYYY-MM`; None if it is anything else (or month 13)."""
    text = (value or "").strip()
    for pattern, parts in _EXPIRY_FORMATS:
        m = pattern.match(text)
        if m:
            year, month = parts(m)
            return f"{year}-{month}" if 1 <= int(month) <= 12 else None
    return None


def card_problems(number: str, expiry: str, cvv: str) -> list[str]:
    """What is wrong with the sandbox card settings, by variable NAME only (never a value)."""
    problems = []
    digits = re.sub(r"[ -]", "", number or "")
    if not (number or "").strip():
        problems.append("PAYPAL_CARD_NUMBER is missing")
    elif not (digits.isascii() and digits.isdigit() and 12 <= len(digits) <= 19):
        problems.append("PAYPAL_CARD_NUMBER is malformed (expected 12 to 19 digits)")
    if not (expiry or "").strip():
        problems.append("PAYPAL_CARD_EXPIRY is missing")
    elif normalize_expiry(expiry) is None:
        problems.append("PAYPAL_CARD_EXPIRY is malformed (use MM/YYYY, MM/YY or YYYY-MM)")
    if not (cvv or "").strip():
        problems.append("PAYPAL_CARD_CVV is missing")
    elif not (cvv.isascii() and cvv.isdigit() and 3 <= len(cvv) <= 4):
        problems.append("PAYPAL_CARD_CVV is malformed (expected 3 or 4 digits)")
    return problems


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
    # Hosted demo. GAWAH_BOOTSTRAP=1: on start, if the database is empty, create the schema, the
    # signing keys and the demo merchant (nothing else is seeded). GAWAH_ENV=demo + ADMIN_KEY
    # enable POST /v1/admin/reset-demo (wipes the data and the keys, starts a new ledger).
    gawah_bootstrap: bool = False
    admin_key: str = ""
    # Comma-separated browser origins allowed by CORS (the web app calls Counsel through its own
    # server, so this only matters for direct browser calls to the engine).
    engine_cors_origins: str = "http://localhost:3000"

    @field_validator("paypal_card_expiry")
    @classmethod
    def _normalize_card_expiry(cls, v: str) -> str:
        # Normalise when it is a known format; keep anything else as-is so startup (not import)
        # reports it, in real mode only.
        return normalize_expiry(v) or v

    def card_problems(self) -> list[str]:
        return card_problems(self.paypal_card_number, self.paypal_card_expiry, self.paypal_card_cvv)


def cors_origins(value: str) -> list[str]:
    """`"https://a.example, https://b.example/"` -> the origins, trimmed, without trailing slash."""
    return [o.strip().rstrip("/") for o in value.split(",") if o.strip()]


settings = Settings()

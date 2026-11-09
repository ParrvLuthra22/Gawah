"""Builds the real (non-mock) services from settings: DB, signers, ledger, mandates, payments."""

from dataclasses import dataclass
from functools import lru_cache

from .config import settings
from .db import db_path, init_db
from .evidence import EvidenceService
from .guardian import GuardianService
from .ledger_service import LedgerService
from .mandate_service import MandateService
from .merchant_service import MerchantService
from .payments import PaymentConfig, PaymentService
from .semantic import SemanticChecker
from .signing import ES256Signer


@dataclass(frozen=True)
class Services:
    path: str
    ledger: LedgerService
    mandates: MandateService
    merchants: MerchantService
    guardian: GuardianService
    payments: PaymentService
    evidence: EvidenceService


def build_services(
    database_url: str,
    ledger_key_path: str = "",
    mandate_key_path: str = "",
    groq_api_key: str = "",
    groq_model: str = "llama-3.3-70b-versatile",
    payment_config: PaymentConfig | None = None,
) -> Services:
    path = db_path(database_url)
    init_db(path)
    ledger = LedgerService(path, ES256Signer(ledger_key_path or None, "gawah-ledger-key"))
    mandates = MandateService(path, ES256Signer(mandate_key_path or None, "gawah-mandate-key"), ledger)
    merchants = MerchantService(path)
    semantic = SemanticChecker(path, groq_api_key, groq_model)
    payments = PaymentService(path, mandates, ledger, payment_config or PaymentConfig())
    guardian = GuardianService(path, mandates, merchants, ledger, semantic, payments=payments)
    evidence = EvidenceService(path, mandates, merchants, ledger, payments)
    return Services(path, ledger, mandates, merchants, guardian, payments, evidence)


@lru_cache
def _cached(
    database_url: str,
    ledger_key_path: str,
    mandate_key_path: str,
    groq_api_key: str,
    groq_model: str,
    payment_config: PaymentConfig,
) -> Services:
    return build_services(
        database_url, ledger_key_path, mandate_key_path, groq_api_key, groq_model, payment_config
    )


# Tests set this to an httpx.MockTransport so no request ever leaves the machine.
paypal_transport_override = None


def payment_config_from_settings() -> PaymentConfig:
    return PaymentConfig(
        client_id=settings.paypal_client_id,
        client_secret=settings.paypal_client_secret,
        env=settings.paypal_env or "sandbox",
        card_number=settings.paypal_card_number,
        card_expiry=settings.paypal_card_expiry,
        card_cvv=settings.paypal_card_cvv,
        card_name=settings.paypal_card_name,
        buyer_email=settings.buyer_email,
        buyer_password=settings.buyer_password,
        transport=paypal_transport_override,
    )


def get_services() -> Services:
    """Services for the current settings. Cached per (database, key paths) combination."""
    return _cached(
        settings.database_url,
        settings.ledger_signing_key_path,
        settings.mandate_signing_key_path,
        settings.groq_api_key,
        settings.groq_model,
        payment_config_from_settings(),
    )

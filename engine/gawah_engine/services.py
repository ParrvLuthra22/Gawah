"""Builds the real (non-mock) services from settings: DB, signers, ledger, mandates."""

from dataclasses import dataclass
from functools import lru_cache

from .config import settings
from .db import db_path, init_db
from .guardian import GuardianService
from .ledger_service import LedgerService
from .mandate_service import MandateService
from .merchant_service import MerchantService
from .semantic import SemanticChecker
from .signing import ES256Signer


@dataclass(frozen=True)
class Services:
    path: str
    ledger: LedgerService
    mandates: MandateService
    merchants: MerchantService
    guardian: GuardianService


def build_services(
    database_url: str,
    ledger_key_path: str = "",
    mandate_key_path: str = "",
    groq_api_key: str = "",
    groq_model: str = "llama-3.3-70b-versatile",
) -> Services:
    path = db_path(database_url)
    init_db(path)
    ledger = LedgerService(path, ES256Signer(ledger_key_path or None, "gawah-ledger-key"))
    mandates = MandateService(path, ES256Signer(mandate_key_path or None, "gawah-mandate-key"), ledger)
    merchants = MerchantService(path)
    semantic = SemanticChecker(path, groq_api_key, groq_model)
    guardian = GuardianService(path, mandates, merchants, ledger, semantic)
    return Services(path, ledger, mandates, merchants, guardian)


@lru_cache
def _cached(
    database_url: str,
    ledger_key_path: str,
    mandate_key_path: str,
    groq_api_key: str,
    groq_model: str,
) -> Services:
    return build_services(
        database_url, ledger_key_path, mandate_key_path, groq_api_key, groq_model
    )


def get_services() -> Services:
    """Services for the current settings. Cached per (database, key paths) combination."""
    return _cached(
        settings.database_url,
        settings.ledger_signing_key_path,
        settings.mandate_signing_key_path,
        settings.groq_api_key,
        settings.groq_model,
    )

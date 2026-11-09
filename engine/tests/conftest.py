"""Shared fixtures for the real (non-mock) service tests. Everything lives under tmp_path."""

from datetime import UTC, datetime, timedelta

import pytest
from gawah_contract import MandateCreate
from gawah_engine.db import db_path, init_db
from gawah_engine.ledger_service import LedgerService
from gawah_engine.mandate_service import MandateService
from gawah_engine.services import Services
from gawah_engine.signing import ES256Signer


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def svc(tmp_path, clock) -> Services:
    path = db_path(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(path)
    keys = tmp_path / "keys"
    ledger = LedgerService(path, ES256Signer(keys, "gawah-ledger-key"), clock=clock)
    mandates = MandateService(path, ES256Signer(keys, "gawah-mandate-key"), ledger, clock=clock)
    return Services(path, ledger, mandates)


def make_body(clock: Clock | None = None, **over) -> MandateCreate:
    base = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC) if clock is None else clock()
    fields = {
        "user_id": "user_1",
        "category": "footwear",
        "item_description": "Black running shoes, size 9",
        "max_amount_minor": 8000,
        "currency": "USD",
        "allowed_merchants": ["demo-shoe-store"],
        "expires_at": base + timedelta(days=7),
    }
    fields.update(over)
    return MandateCreate(**fields)

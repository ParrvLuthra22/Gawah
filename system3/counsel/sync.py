import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from gawah_contract import DisputeCase, PaymentRecord
from gawah_contract.fixtures import load
from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, TypeAdapter
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .clients import IntegrationError, PayPalClient, System2Client
from .models import Dispute, DisputeStatusHistory, DisputeSyncLog, Source, now

logger = logging.getLogger(__name__)
PayPalID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,255}$")]
PAYPAL_ID = TypeAdapter(PayPalID)
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=255)]
Code = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class Money(BaseModel):
    currency_code: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    value: Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=2)]

    def minor_units(self) -> int:
        scale = 1 if self.currency_code in {"JPY", "HUF", "TWD"} else 100
        value = self.value * scale
        if value != value.to_integral_value():
            raise ValueError("Invalid currency precision")
        return int(value)


class Party(BaseModel):
    name: str | None = None
    email: str | None = None
    merchant_id: str | None = None

    def details(self) -> dict | None:
        return self.model_dump(exclude_none=True) or None


class Transaction(BaseModel):
    buyer_transaction_id: Identifier | None = None
    seller_transaction_id: Identifier | None = None
    custom: Identifier | None = None
    buyer: Party | None = None
    seller: Party | None = None

    def identifiers(self) -> tuple:
        return self.seller_transaction_id, self.buyer_transaction_id, self.custom


class PayPalDispute(BaseModel):
    dispute_id: PayPalID
    status: Code
    reason: Code | None = None
    dispute_amount: Money | None = None
    create_time: AwareDatetime | None = None
    update_time: AwareDatetime | None = None
    seller_response_due_date: AwareDatetime | None = None
    buyer_response_due_date: AwareDatetime | None = None
    disputed_transactions: list[Transaction] = Field(default_factory=list)

    def fields(self) -> dict:
        transactions = self.disputed_transactions
        primary = transactions[0] if len(transactions) == 1 else Transaction()
        due = self.seller_response_due_date or self.buyer_response_due_date
        return {
            "paypal_dispute_id": self.dispute_id,
            "transaction_id": primary.seller_transaction_id or primary.buyer_transaction_id,
            "status": self.status,
            "reason": self.reason,
            "amount_minor": self.dispute_amount.minor_units() if self.dispute_amount else None,
            "currency": self.dispute_amount.currency_code if self.dispute_amount else None,
            "buyer": primary.buyer.details() if primary.buyer else None,
            "merchant": primary.seller.details() if primary.seller else None,
            "created_at": as_utc(self.create_time),
            "updated_at": as_utc(self.update_time),
            "deadline": as_utc(due),
        }


def as_utc(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value is not None else None


def payment_reference(
    transactions: list[Transaction], payments: list[PaymentRecord]
) -> tuple[str | None, str]:
    ids = set()
    for transaction in transactions:
        transaction_ids = {transaction.seller_transaction_id, transaction.buyer_transaction_id} - {
            None
        }
        matches = {
            payment.id
            for payment in payments
            if transaction_ids.intersection((payment.paypal_capture_id, payment.paypal_order_id))
            or (transaction.custom and transaction.custom == payment.custom_id)
        }
        if len(matches) > 1:
            return None, "AMBIGUOUS"
        if not matches:
            return None, "UNMATCHED"
        ids.update(matches)
    if len(ids) == 1:
        return ids.pop(), "LINKED"
    return None, "AMBIGUOUS" if ids else "UNMATCHED"


def acquire_sync_lock(db: Session) -> None:
    dialect = db.get_bind().dialect.name
    locked = True
    if dialect == "postgresql":
        locked = db.scalar(text("SELECT pg_try_advisory_xact_lock(731003001)"))
    elif dialect == "sqlite":
        try:
            db.execute(text("BEGIN IMMEDIATE"))
        except OperationalError as exc:
            if getattr(exc.orig, "sqlite_errorname", "") != "SQLITE_BUSY":
                raise
            locked = False
    else:
        raise ValueError("System 3 requires PostgreSQL or SQLite.")
    if not locked:
        raise IntegrationError("SYNC_IN_PROGRESS", "A dispute sync is already running.", 409)


def existing_dispute(db: Session, source: Source, paypal_id: str) -> Dispute | None:
    return db.scalar(
        select(Dispute).where(Dispute.source == source, Dispute.paypal_dispute_id == paypal_id)
    )


def save_dispute(
    db: Session, row: Dispute | None, source: Source, fields: dict, raw: dict, synced: datetime
) -> tuple[int, int]:
    previous_status = row.status if row else None
    created = row is None
    changed = created
    if row is None:
        row = Dispute(**fields, source=source, raw_response=raw, last_synced_at=synced)
        db.add(row)
        db.flush()
    else:
        changed = any(getattr(row, key) != value for key, value in fields.items())
        for key, value in fields.items():
            setattr(row, key, value)
        row.raw_response = raw
        row.last_synced_at = synced
    if row.status != previous_status:
        db.add(
            DisputeStatusHistory(
                dispute_id=row.id,
                previous_status=previous_status,
                new_status=row.status,
                source=source,
                changed_at=synced,
            )
        )
    return int(created), int(changed and not created)


def sync_disputes(
    db: Session, paypal: PayPalClient, system2: System2Client
) -> tuple[DisputeSyncLog, IntegrationError | None]:
    acquire_sync_lock(db)
    log = DisputeSyncLog(
        started_at=now(),
        retrieved=0,
        created=0,
        updated=0,
        unmatched=0,
        result="SUCCESS",
        errors=[],
    )
    error = None
    try:
        with db.begin_nested():
            _sync_paypal(db, log, paypal, system2)
    except IntegrationError as exc:
        error = exc
    except Exception:
        logger.exception("Dispute synchronization failed")
        error = IntegrationError("SYNC_FAILED", "Disputes could not be synchronized.", 500)
    if error:
        log.created = log.updated = log.unmatched = 0
        log.result = "FAILED"
        log.errors = [str(error)]
    log.finished_at = now()
    db.add(log)
    db.commit()
    return log, error


def _sync_paypal(db: Session, log: DisputeSyncLog, paypal: PayPalClient, system2: System2Client):
    payments = None
    seen = set()
    source = Source.PAYPAL_API
    for summary in paypal.list_disputes():
        try:
            dispute_id = PAYPAL_ID.validate_python(summary.get("dispute_id"))
            if dispute_id in seen:
                continue
            seen.add(dispute_id)
            raw = {**summary, **paypal.get_dispute(dispute_id)}
            dispute = PayPalDispute.model_validate(raw)
            fields = dispute.fields()
        except ValueError:
            raise IntegrationError(
                "INVALID_RESPONSE", "PayPal returned invalid dispute fields."
            ) from None
        log.retrieved += 1
        if payments is None and not log.errors:
            try:
                payments = system2.list_payments()
            except IntegrationError as exc:
                log.result = "PARTIAL"
                log.errors = [str(exc)]
        row = existing_dispute(db, source, dispute_id)
        if (
            row
            and row.updated_at
            and (fields["updated_at"] is None or fields["updated_at"] < row.updated_at)
        ):
            # Re-link with the last accepted data when the upstream snapshot is stale.
            raw = row.raw_response
            dispute = PayPalDispute.model_validate(raw)
            fields = dispute.fields()
        if payments is None:
            previous = PayPalDispute.model_validate(row.raw_response) if row else None
            same_transactions = previous and (
                [tx.identifiers() for tx in previous.disputed_transactions]
                == [tx.identifiers() for tx in dispute.disputed_transactions]
            )
            payment_id = row.payment_id if row and same_transactions else None
            link_status = "LINKED" if payment_id else "LOOKUP_FAILED"
        else:
            payment_id, link_status = payment_reference(dispute.disputed_transactions, payments)
        fields.update(payment_id=payment_id, link_status=link_status)
        created, updated = save_dispute(db, row, source, fields, raw, now())
        log.created += created
        log.updated += updated
        log.unmatched += int(payment_id is None)


def seed_fixtures(db: Session, system2: System2Client) -> None:
    acquire_sync_lock(db)
    try:
        payment_ids = {payment.id for payment in system2.list_payments()}
    except IntegrationError:
        payment_ids = set()
    for name in ("dispute_defendable", "dispute_wrong_size"):
        raw = load(name)
        case = DisputeCase.model_validate(raw)
        paypal_id = case.paypal_dispute_id or case.id
        row = existing_dispute(db, Source.SIMULATED_FIXTURE, paypal_id)
        payment_id = case.payment_id if case.payment_id in payment_ids else None
        save_dispute(
            db,
            row,
            Source.SIMULATED_FIXTURE,
            {
                "paypal_dispute_id": paypal_id,
                "payment_id": payment_id,
                "link_status": "LINKED" if payment_id else "UNMATCHED",
                "reason": case.reason,
                "status": case.status,
                "amount_minor": case.amount_minor,
                "currency": case.currency,
                "created_at": as_utc(case.opened_at),
            },
            raw,
            now(),
        )
    db.commit()

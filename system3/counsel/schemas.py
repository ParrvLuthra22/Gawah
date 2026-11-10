from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .models import Source
from .sync import Party

LinkStatus = Literal["LINKED", "UNMATCHED", "AMBIGUOUS", "LOOKUP_FAILED"]


class PageParams(BaseModel):
    page: int = Field(default=1, ge=1, le=1_000_000)
    page_size: int = Field(default=25, ge=1, le=100)


class DisputeFilters(PageParams):
    search: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None, max_length=128)
    source: Source | None = None
    link_status: LinkStatus | None = None
    sort: Literal[
        "paypal_dispute_id",
        "payment_id",
        "reason",
        "amount_minor",
        "status",
        "created_at",
        "updated_at",
        "deadline",
        "source",
    ] = "created_at"
    order: Literal["asc", "desc"] = "desc"


class DisputeSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    paypal_dispute_id: str
    transaction_id: str | None
    payment_id: str | None
    link_status: LinkStatus
    reason: str | None
    status: str
    amount_minor: int | None
    currency: str | None
    created_at: datetime | None
    updated_at: datetime | None
    deadline: datetime | None
    source: Source
    last_synced_at: datetime


class StatusChange(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    previous_status: str | None
    new_status: str
    changed_at: datetime
    source: Source


class DisputeDetail(DisputeSummary):
    buyer: Party | None
    merchant: Party | None
    status_history: list[StatusChange]


class SyncResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    started_at: datetime
    finished_at: datetime
    result: Literal["SUCCESS", "PARTIAL", "FAILED"]
    retrieved: int
    created: int
    updated: int
    unmatched: int
    errors: list[str]


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class PayPalStatus(BaseModel):
    environment: Literal["sandbox"] = "sandbox"
    credentials_configured: bool
    connection: Literal["CONNECTED", "DISCONNECTED", "NOT_CONFIGURED"]
    last_sync: SyncResult | None
    total_disputes: int
    error: str | None = None


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["gawah-system3"] = "gawah-system3"

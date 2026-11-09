import asyncio
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from gawah_contract import (
    EvidenceBundle,
    LedgerEntry,
    LedgerEntryType,
    LedgerHead,
    LedgerVerifyResult,
)
from gawah_contract.fixtures import load
from gawah_contract.hashing import GENESIS_HASH, verify_chain

from ..config import settings
from ..errors import GawahError
from ..services import get_services

router = APIRouter(prefix="/v1", tags=["ledger"])

# GAWAH_MOCK=1 serves the fixture chain; GAWAH_MOCK=0 serves the real SQLite ledger.


@router.get("/ledger", response_model=list[LedgerEntry])
def list_ledger(
    mandate_id: str | None = None,
    subject_id: str | None = None,
    type: LedgerEntryType | None = None,
    since_seq: int | None = None,
) -> list[LedgerEntry]:
    if not settings.gawah_mock:
        return get_services().ledger.list(
            mandate_id=mandate_id, subject_id=subject_id, type=type, since_seq=since_seq
        )
    entries = [LedgerEntry.model_validate(e) for e in load("ledger_chain")]
    if mandate_id:
        entries = [e for e in entries if e.data.get("mandate_id") == mandate_id]
    if subject_id:
        entries = [e for e in entries if e.subject_id == subject_id]
    if type:
        entries = [e for e in entries if e.type == type]
    if since_seq is not None:
        entries = [e for e in entries if e.seq > since_seq]
    return entries


@router.get("/ledger/verify", response_model=LedgerVerifyResult)
def verify_ledger() -> LedgerVerifyResult:
    if not settings.gawah_mock:
        r = get_services().ledger.verify()
        return LedgerVerifyResult(**r)
    entries = [LedgerEntry.model_validate(e) for e in load("ledger_chain")]
    ok, first_broken = verify_chain(entries)
    reason = None if ok else f"hash or prev_hash mismatch at seq {first_broken}"
    return LedgerVerifyResult(
        ok=ok, checked=len(entries), first_broken_seq=first_broken, reason=reason
    )


@router.get("/evidence/{payment_id}", response_model=EvidenceBundle)
def get_evidence(payment_id: str) -> EvidenceBundle:
    if not settings.gawah_mock:
        return get_services().evidence.bundle(payment_id)
    names = {"pay_01": "evidence_bundle", "pay_04": "evidence_bundle_wrong_size"}
    if payment_id not in names:
        raise GawahError(404, "PAYMENT_NOT_FOUND", f"No evidence for payment {payment_id}")
    return EvidenceBundle.model_validate(load(names[payment_id]))


@router.get("/ledger/head", response_model=LedgerHead)
def ledger_head() -> LedgerHead:
    """Latest seq + hash, signed by the ledger key: an external anchor the UI can display."""
    if not settings.gawah_mock:
        return get_services().ledger.head()
    entries = [LedgerEntry.model_validate(e) for e in load("ledger_chain")]
    last = entries[-1]
    # SIMULATED: mock mode has no ledger key, so this signature is a placeholder and verifies nowhere
    return LedgerHead(
        seq=last.seq, hash=last.hash, message=f"gawah-ledger-head:{last.seq}:{last.hash}",
        signature="SIMULATED-mock-mode-no-signature", key_id="mock",
    ) if entries else LedgerHead(
        seq=0, hash=GENESIS_HASH, message=f"gawah-ledger-head:0:{GENESIS_HASH}",
        signature="SIMULATED-mock-mode-no-signature", key_id="mock",
    )


def _entries_after(seq: int) -> list[LedgerEntry]:
    if not settings.gawah_mock:
        return get_services().ledger.list(since_seq=seq)
    return [e for e in (LedgerEntry.model_validate(x) for x in load("ledger_chain")) if e.seq > seq]


def _head_seq() -> int:
    if not settings.gawah_mock:
        return get_services().ledger.head_seq()
    return max((e["seq"] for e in load("ledger_chain")), default=0)


def sse_event(entry: LedgerEntry) -> str:
    return f"id: {entry.seq}\nevent: ledger\ndata: {entry.model_dump_json()}\n\n"


@router.get("/ledger/stream")
async def ledger_stream(
    request: Request,
    last_event_id: str | None = Header(default=None),
    since_seq: int | None = Query(default=None, ge=0, description="Same as Last-Event-ID"),
    limit: int | None = Query(default=None, ge=1, le=10000, description="Close after N events"),
    max_seconds: float | None = Query(default=None, gt=0, le=3600, description="Close after N s"),
) -> StreamingResponse:
    """Server-Sent Events: every ledger entry appended from now on, one `ledger` event each.

    `id` is the entry's seq. Reconnect with `Last-Event-ID: <seq>` (or `?since_seq=`) to receive
    everything after it first, then live entries. A `: keepalive` comment is sent every 15 s.
    """
    cursor: int | None = since_seq
    if cursor is None and last_event_id is not None:
        if not last_event_id.strip().isdigit():
            raise GawahError(400, "BAD_LAST_EVENT_ID", "Last-Event-ID must be a ledger seq")
        cursor = int(last_event_id)
    if cursor is None:
        cursor = await run_in_threadpool(_head_seq)  # new entries only

    async def events() -> AsyncIterator[str]:
        nonlocal cursor
        sent, started, last_write = 0, time.monotonic(), time.monotonic()
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            for entry in await run_in_threadpool(_entries_after, cursor):
                cursor = entry.seq
                yield sse_event(entry)
                last_write = time.monotonic()
                sent += 1
                if limit is not None and sent >= limit:
                    return
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                return
            if time.monotonic() - last_write >= settings.sse_keepalive_s:
                yield ": keepalive\n\n"
                last_write = time.monotonic()
            await asyncio.sleep(settings.sse_poll_s)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

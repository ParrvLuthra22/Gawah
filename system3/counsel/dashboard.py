from typing import Annotated

import httpx
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from gawah_contract import BudgetStatus, LedgerEntry, LedgerHead, LedgerVerifyResult, Mandate
from pydantic import TypeAdapter, ValidationError
from starlette.background import BackgroundTask

from .clients import IntegrationError
from .evidence import PaymentReference

router = APIRouter(tags=["dashboard"])
MANDATES = TypeAdapter(list[Mandate])
LEDGER = TypeAdapter(list[LedgerEntry])
MANDATE = TypeAdapter(Mandate)
BUDGET = TypeAdapter(BudgetStatus)
HEAD = TypeAdapter(LedgerHead)
VERIFY = TypeAdapter(LedgerVerifyResult)


def system2_record(client, path: str, schema):
    try:
        payload = client.get_json(path, service="System 2 dashboard")
        return payload if schema is None else schema.validate_python(payload)
    except ValidationError:
        raise IntegrationError("INVALID_RESPONSE", "System 2 returned an invalid record.") from None
    except IntegrationError as exc:
        if exc.upstream_status == 404:
            raise IntegrationError("RECORD_NOT_FOUND", "System 2 record not found.", 404) from None
        raise


@router.get("/mandates", response_model=list[Mandate])
def mandates(request: Request):
    return system2_record(request.app.state.system2, "/v1/mandates", MANDATES)


@router.get("/mandates/{mandate_id}", response_model=Mandate)
def mandate(request: Request, mandate_id: PaymentReference):
    record = system2_record(request.app.state.system2, f"/v1/mandates/{mandate_id}", MANDATE)
    if record.id != mandate_id:
        raise IntegrationError("INVALID_RESPONSE", "System 2 returned a different mandate.")
    return record


@router.get("/mandates/{mandate_id}/budget", response_model=BudgetStatus)
def mandate_budget(request: Request, mandate_id: PaymentReference):
    record = system2_record(request.app.state.system2, f"/v1/mandates/{mandate_id}/budget", BUDGET)
    if record.mandate_id != mandate_id:
        raise IntegrationError("INVALID_RESPONSE", "System 2 returned a different mandate budget.")
    return record


@router.get("/ledger", response_model=list[LedgerEntry])
def ledger(request: Request):
    return system2_record(request.app.state.system2, "/v1/ledger", LEDGER)


@router.get("/ledger/head", response_model=LedgerHead)
def ledger_head(request: Request):
    return system2_record(request.app.state.system2, "/v1/ledger/head", HEAD)


@router.get("/ledger/verify", response_model=LedgerVerifyResult)
def verify_chain(request: Request):
    return system2_record(request.app.state.system2, "/v1/ledger/verify", VERIFY)


@router.get("/ledger/payments/{payment_id}/verification", response_model=LedgerVerifyResult)
def payment_verification(request: Request, payment_id: PaymentReference):
    payload = system2_record(request.app.state.system2, f"/v1/evidence/{payment_id}", None)
    try:
        if payload["payment"]["id"] != payment_id:
            raise ValueError
        return LedgerVerifyResult.model_validate(payload["verification"], strict=True)
    except (KeyError, TypeError, ValueError):
        raise IntegrationError(
            "INVALID_RESPONSE", "System 2 did not return usable verification for this payment."
        ) from None


@router.get("/ledger/stream")
async def ledger_stream(
    request: Request,
    since_seq: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
    last_event_id: Annotated[str | None, Header(pattern=r"^[0-9]{1,19}$")] = None,
):
    client = request.app.state.system2_stream
    headers = request.app.state.system2.headers()
    headers["Accept"] = "text/event-stream"
    params = {}
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    elif since_seq is not None:
        params["since_seq"] = since_seq
    try:
        upstream = await client.send(
            client.build_request("GET", "/v1/ledger/stream", headers=headers, params=params),
            stream=True,
        )
    except httpx.TimeoutException:
        raise IntegrationError(
            "UPSTREAM_TIMEOUT", "The System 2 ledger stream timed out.", 504
        ) from None
    except httpx.RequestError:
        raise IntegrationError(
            "UPSTREAM_UNAVAILABLE", "The System 2 ledger stream is unavailable."
        ) from None
    if not upstream.is_success or not upstream.headers.get("content-type", "").startswith(
        "text/event-stream"
    ):
        await upstream.aclose()
        raise IntegrationError("UPSTREAM_UNAVAILABLE", "The System 2 ledger stream is unavailable.")

    async def events():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        except httpx.RequestError:
            # Closing the stream lets EventSource reconnect using its last event ID.
            return
        finally:
            await upstream.aclose()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
        background=BackgroundTask(upstream.aclose),
    )

import hmac
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from gawah_contract import ErrorResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .clients import IntegrationError, PayPalClient, System2Client
from .config import Settings
from .evidence import PaymentReference, assemble_pack, get_pack
from .evidence_schemas import (
    EvidenceIssues,
    EvidencePackResponse,
    TimelineEvent,
    VerificationSummary,
)
from .models import Dispute, DisputeStatusHistory, DisputeSyncLog, Source
from .schemas import (
    DisputeDetail,
    DisputeFilters,
    DisputeSummary,
    Health,
    Page,
    PageParams,
    PayPalStatus,
    SyncResult,
)
from .sync import seed_fixtures, sync_disputes


def require_service_key(
    request: Request, x_gawah_key: Annotated[str | None, Header()] = None
) -> None:
    key = request.app.state.settings.system3_key.get_secret_value()
    if not key or not x_gawah_key or not hmac.compare_digest(x_gawah_key.encode(), key.encode()):
        raise IntegrationError("UNAUTHORIZED", "Missing or invalid X-Gawah-Key header.", 401)


def database(request: Request):
    with request.app.state.sessions() as db:
        yield db


def error_response(
    request: Request, exc: IntegrationError, details: dict | None = None
) -> JSONResponse:
    body = ErrorResponse(
        error={
            "code": exc.code,
            "message": str(exc),
            "details": details,
            "request_id": request.state.request_id,
        }
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=exc.status)


DB = Annotated[Session, Depends(database)]
router = APIRouter(prefix="/api", dependencies=[Depends(require_service_key)])


@router.get("/disputes", response_model=Page[DisputeSummary])
def list_disputes(db: DB, filters: Annotated[DisputeFilters, Query()]):
    conditions = [
        getattr(Dispute, field) == value
        for field in ("status", "source", "link_status")
        if (value := getattr(filters, field)) is not None
    ]
    if filters.search and (term := filters.search.strip()):
        conditions.append(
            or_(
                *[
                    field.icontains(term, autoescape=True)
                    for field in (
                        Dispute.paypal_dispute_id,
                        Dispute.transaction_id,
                        Dispute.payment_id,
                        Dispute.reason,
                    )
                ]
            )
        )
    sort = getattr(Dispute, filters.sort)
    order = sort.asc() if filters.order == "asc" else sort.desc()
    statement = (
        select(Dispute)
        .where(*conditions)
        .order_by(order.nulls_last(), Dispute.id.desc())
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    )
    return {
        "items": db.scalars(statement).all(),
        "total": db.scalar(select(func.count(Dispute.id)).where(*conditions)),
        "page": filters.page,
        "page_size": filters.page_size,
    }


@router.get("/disputes/sync/history", response_model=Page[SyncResult])
def sync_history(db: DB, pagination: Annotated[PageParams, Query()]):
    statement = (
        select(DisputeSyncLog)
        .order_by(DisputeSyncLog.id.desc())
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    return {
        "items": db.scalars(statement).all(),
        "total": db.scalar(select(func.count(DisputeSyncLog.id))),
        "page": pagination.page,
        "page_size": pagination.page_size,
    }


@router.post("/disputes/sync", response_model=SyncResult)
def sync(request: Request, db: DB):
    log, error = sync_disputes(db, request.app.state.paypal, request.app.state.system2)
    result = SyncResult.model_validate(log)
    if error:
        return error_response(request, error, result.model_dump(mode="json"))
    return result


@router.get("/disputes/{id}", response_model=DisputeDetail)
def dispute_detail(db: DB, id: Annotated[int, Path(ge=1, le=2_147_483_647)]):
    row = db.get(Dispute, id)
    if row is None:
        raise IntegrationError("DISPUTE_NOT_FOUND", "Dispute not found.", 404)
    history = db.scalars(
        select(DisputeStatusHistory)
        .where(DisputeStatusHistory.dispute_id == id)
        .order_by(DisputeStatusHistory.id.desc())
    ).all()
    return {
        **DisputeSummary.model_validate(row).model_dump(),
        "buyer": row.buyer,
        "merchant": row.merchant,
        "status_history": history,
    }


@router.get("/paypal/status", response_model=PayPalStatus)
def paypal_status(request: Request, db: DB):
    paypal = request.app.state.paypal
    connection = "NOT_CONFIGURED"
    error = None
    if paypal.configured:
        try:
            paypal.probe()
            connection = "CONNECTED"
        except IntegrationError as exc:
            connection = "DISCONNECTED"
            error = str(exc)
    return {
        "credentials_configured": paypal.configured,
        "connection": connection,
        "error": error,
        "last_sync": db.scalar(select(DisputeSyncLog).order_by(DisputeSyncLog.id.desc()).limit(1)),
        "total_disputes": db.scalar(
            select(func.count(Dispute.id)).where(Dispute.source == Source.PAYPAL_API)
        ),
    }


DisputeReference = Annotated[int | None, Query(ge=1, le=2_147_483_647)]
PackVersion = Annotated[int | None, Query(ge=1, le=2_147_483_647)]


@router.post("/evidence/{payment_id}/assemble", response_model=EvidencePackResponse)
def assemble_evidence(
    request: Request,
    db: DB,
    payment_id: PaymentReference,
    dispute_id: DisputeReference = None,
):
    return assemble_pack(db, request.app.state.system2, payment_id, dispute_id=dispute_id)


@router.get("/evidence/{payment_id}", response_model=EvidencePackResponse)
def evidence_pack(
    db: DB,
    payment_id: PaymentReference,
    dispute_id: DisputeReference = None,
    version: PackVersion = None,
):
    return get_pack(db, payment_id, dispute_id=dispute_id, version=version)


@router.get("/evidence/{payment_id}/timeline", response_model=list[TimelineEvent])
def evidence_timeline(
    db: DB,
    payment_id: PaymentReference,
    dispute_id: DisputeReference = None,
    version: PackVersion = None,
):
    return get_pack(db, payment_id, dispute_id=dispute_id, version=version).timeline


@router.get("/evidence/{payment_id}/verification", response_model=VerificationSummary)
def evidence_verification(
    db: DB,
    payment_id: PaymentReference,
    dispute_id: DisputeReference = None,
    version: PackVersion = None,
):
    return get_pack(db, payment_id, dispute_id=dispute_id, version=version).verification_summary


@router.get("/evidence/{payment_id}/issues", response_model=EvidenceIssues)
def evidence_issues(
    db: DB,
    payment_id: PaymentReference,
    dispute_id: DisputeReference = None,
    version: PackVersion = None,
):
    pack = get_pack(db, payment_id, dispute_id=dispute_id, version=version)
    return EvidenceIssues(
        missing_evidence=pack.missing_evidence,
        invalid_evidence=pack.invalid_evidence,
        contradictions=pack.contradictions,
    )


def create_app(settings: Settings | None = None, *, paypal_transport=None, system2_transport=None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings if settings is not None else Settings()
        engine = config.database_engine()
        app.state.settings = config
        app.state.sessions = sessionmaker(engine, expire_on_commit=False)
        try:
            with (
                httpx.Client(
                    base_url=config.paypal_base_url, transport=paypal_transport, timeout=15.0
                ) as paypal_http,
                httpx.Client(
                    base_url=str(config.system2_base_url), transport=system2_transport, timeout=10.0
                ) as system2_http,
            ):
                app.state.paypal = PayPalClient(config, paypal_http)
                app.state.system2 = System2Client(config, system2_http)
                yield
        finally:
            engine.dispose()

    app = FastAPI(title="Gawah Disputes (System 3)", lifespan=lifespan)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = f"req_{uuid4().hex[:12]}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(IntegrationError)
    async def integration_error(request: Request, exc: IntegrationError):
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error_response(
            request,
            IntegrationError("INVALID_REQUEST", "Invalid request parameters.", 422),
            {"fields": [list(error["loc"]) for error in exc.errors()]},
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, _: SQLAlchemyError):
        return error_response(
            request,
            IntegrationError("DATABASE_UNAVAILABLE", "Dispute storage is unavailable.", 503),
        )

    @app.get("/api/health", response_model=Health)
    def health(db: DB):
        db.scalar(select(Dispute.id).limit(1))
        return Health()

    app.include_router(router)
    return app


app = create_app()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manage Gawah System 3")
    parser.add_argument("command", choices=["seed-fixtures"])
    parser.parse_args()
    config = Settings()
    with (
        Session(config.database_engine()) as db,
        httpx.Client(base_url=str(config.system2_base_url), timeout=10.0) as system2_http,
    ):
        seed_fixtures(db, System2Client(config, system2_http))
    print("Simulated dispute fixtures seeded.")

import hmac
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from gawah_contract import ErrorResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from . import command_dashboard, demo, resolution_submission, resolution_workflow
from .clients import IntegrationError, PayPalClient, System2Client
from .command_schemas import (
    ActivityFeed,
    CaseFilters,
    CasePage,
    CounselStats,
    EvidenceStats,
    Overview,
    Period,
)
from .config import Settings
from .counsel_schemas import CounselAnalysisResponse
from .counsel_service import analyze, get_analysis, get_history
from .dashboard import router as dashboard_router
from .evidence import PaymentReference, assemble_pack, get_pack
from .evidence_schemas import (
    EvidenceIssues,
    EvidencePackResponse,
    TimelineEvent,
    VerificationSummary,
)
from .groq_counsel import GROQ_BASE_URL, GroqCounsel
from .models import Dispute, DisputeStatusHistory, DisputeSyncLog, ResolutionWorkflow, Source
from .resolution import Action, ResolutionOptions, get_options
from .resolution_auth import Actor, authenticate
from .resolution_schemas import (
    OpenWorkflow,
    QueueFilters,
    ResolutionStats,
    Review,
    Revision,
    SubmitWorkflow,
    WorkflowDetail,
    WorkflowRow,
)
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
ResolutionActor = Annotated[Actor, Depends(authenticate)]
WorkflowId = Annotated[int, Path(ge=1, le=2_147_483_647)]
router = APIRouter(prefix="/api", dependencies=[Depends(require_service_key)])
router.include_router(dashboard_router)


@router.get("/dashboard/overview", response_model=Overview)
def dashboard_overview(db: DB, filters: Annotated[Period, Query()]):
    return command_dashboard.overview(db, filters)


@router.get("/dashboard/cases", response_model=CasePage)
def dashboard_cases(db: DB, filters: Annotated[CaseFilters, Query()]):
    return command_dashboard.cases(db, filters)


@router.get("/dashboard/evidence", response_model=EvidenceStats)
def dashboard_evidence(db: DB, filters: Annotated[Period, Query()]):
    return command_dashboard.evidence_stats(db, filters)


@router.get("/dashboard/counsel", response_model=CounselStats)
def dashboard_counsel(db: DB, filters: Annotated[Period, Query()]):
    return command_dashboard.counsel_stats(db, filters)


@router.get("/dashboard/activity", response_model=ActivityFeed)
def dashboard_activity(db: DB, filters: Annotated[Period, Query()]):
    return command_dashboard.activity(db, filters)


@router.get("/dashboard/resolution", response_model=ResolutionStats)
def dashboard_resolution(db: DB, filters: Annotated[Period, Query()]):
    return resolution_workflow.statistics(
        db, *command_dashboard.period_conditions(filters, ResolutionWorkflow.created_at)
    )


class DemoScenarioResponse(BaseModel):
    scenario: str
    dispute_id: int
    payment_id: str
    analysis_status: str | None
    recommendation: str | None
    workflow_id: int | None
    status: str
    detail: str | None = None


@router.post("/dashboard/demo-scenarios", response_model=list[DemoScenarioResponse])
def demo_scenarios(request: Request, db: DB):
    """Create idempotent internal demo cases; no PayPal action is submitted."""
    return [
        DemoScenarioResponse(**row.__dict__)
        for row in demo.run(
            db,
            request.app.state.system2,
            request.app.state.groq_counsel,
            allow_simulated=request.app.state.settings.local_sandbox_resolution,
        )
    ]


@router.get("/resolution/actor", response_model=Actor)
def resolution_actor(actor: ResolutionActor):
    return actor


@router.get("/resolution/overview", response_model=ResolutionStats)
def resolution_overview(db: DB, actor: ResolutionActor):
    return resolution_workflow.statistics(db)


@router.get("/resolution/workflows", response_model=Page[WorkflowRow])
def resolution_queue(db: DB, actor: ResolutionActor, filters: Annotated[QueueFilters, Query()]):
    return resolution_workflow.list_workflows(db, filters)


@router.post("/resolution/workflows", response_model=WorkflowDetail)
def resolution_open(request: Request, db: DB, actor: ResolutionActor, body: OpenWorkflow):
    id = resolution_workflow.open_workflow(
        db,
        actor,
        body,
        allow_simulated=request.app.state.settings.local_sandbox_resolution,
    )
    return resolution_workflow.detail(db, actor, id)


@router.get("/resolution/workflows/{id}", response_model=WorkflowDetail)
def resolution_detail(db: DB, actor: ResolutionActor, id: WorkflowId):
    return resolution_workflow.detail(db, actor, id)


@router.get("/resolution/workflows/{id}/options", response_model=ResolutionOptions)
def resolution_workflow_options(request: Request, db: DB, actor: ResolutionActor, id: WorkflowId):
    row = resolution_workflow.get_workflow(db, id)
    if row.status not in resolution_workflow.REVIEWABLE:
        return ResolutionOptions(message="The workflow has no editable action.")
    return get_options(db, request.app.state.paypal, request.app.state.system2, row.dispute_id)


@router.post("/resolution/workflows/{id}/review", response_model=WorkflowDetail)
def resolution_review(request: Request, db: DB, actor: ResolutionActor, id: WorkflowId, body: Review):
    resolution_workflow.review(db, actor, request.app.state.paypal, request.app.state.system2, id, body)
    return resolution_workflow.detail(db, actor, id)


@router.post("/resolution/workflows/{id}/submit", response_model=WorkflowDetail)
def resolution_submit(
    request: Request, db: DB, actor: ResolutionActor, id: WorkflowId, body: SubmitWorkflow,
):
    resolution_submission.submit(db, actor, request.app.state.paypal, request.app.state.system2, id, body)
    return resolution_workflow.detail(db, actor, id)


@router.post("/resolution/workflows/{id}/reconcile", response_model=WorkflowDetail)
def resolution_reconcile(
    request: Request, db: DB, actor: ResolutionActor, id: WorkflowId, body: Revision,
):
    resolution_submission.reconcile(db, actor, request.app.state.paypal, request.app.state.system2, id, body)
    return resolution_workflow.detail(db, actor, id)


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


@router.get("/disputes/{id}/resolution", response_model=ResolutionOptions)
def resolution_options(request: Request, db: DB, id: Annotated[int, Path(ge=1, le=2_147_483_647)]):
    return get_options(db, request.app.state.paypal, request.app.state.system2, id)


@router.post("/disputes/{id}/resolution/{action}")
def resolve_dispute(id: WorkflowId, action: Action):
    raise IntegrationError("APPROVAL_REQUIRED", "Use the Resolution Center to approve and submit an action.", 409)


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


@router.post("/counsel/{payment_id}/analyze", response_model=CounselAnalysisResponse)
def analyze_case(
    request: Request,
    db: DB,
    payment_id: PaymentReference,
    dispute_id: DisputeReference = None,
    force: bool = False,
):
    result = analyze(
        db, request.app.state.groq_counsel, payment_id, dispute_id=dispute_id, force=force
    )
    if result.error:
        return error_response(
            request,
            IntegrationError(result.error.code, result.error.message, result.error.status),
            {"analysis_id": result.id},
        )
    return result


@router.get("/counsel/{payment_id}", response_model=CounselAnalysisResponse)
def latest_counsel(db: DB, payment_id: PaymentReference, dispute_id: DisputeReference = None):
    return get_analysis(db, payment_id, dispute_id)


class CounselHistoryParams(PageParams):
    dispute_id: int | None = Field(default=None, ge=1, le=2_147_483_647)


@router.get("/counsel/{payment_id}/history", response_model=Page[CounselAnalysisResponse])
def counsel_history(
    db: DB, payment_id: PaymentReference, params: Annotated[CounselHistoryParams, Query()]
):
    return get_history(db, payment_id, params.dispute_id, params.page, params.page_size)


def create_app(
    settings: Settings | None = None,
    *,
    paypal_transport=None,
    system2_transport=None,
    system2_stream_transport=None,
    groq_transport=None,
):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings if settings is not None else Settings()
        engine = config.database_engine()
        app.state.settings = config
        app.state.sessions = sessionmaker(engine, expire_on_commit=False)
        try:
            async with httpx.AsyncClient(
                base_url=str(config.system2_base_url),
                transport=system2_stream_transport,
                timeout=httpx.Timeout(25.0, connect=10.0),
            ) as system2_stream:
                app.state.system2_stream = system2_stream
                with (
                    httpx.Client(
                        base_url=config.paypal_base_url, transport=paypal_transport, timeout=15.0
                    ) as paypal_http,
                    httpx.Client(
                        base_url=str(config.system2_base_url), transport=system2_transport, timeout=10.0
                    ) as system2_http,
                    httpx.Client(
                        base_url=GROQ_BASE_URL, transport=groq_transport, timeout=25.0
                    ) as groq_http,
                ):
                    app.state.paypal = PayPalClient(config, paypal_http)
                    app.state.system2 = System2Client(config, system2_http)
                    app.state.groq_counsel = GroqCounsel(config, groq_http)
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

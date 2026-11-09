from langsmith import tracing_context
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .clients import IntegrationError
from .counsel_case import load_case
from .counsel_schemas import CounselAnalysisResponse
from .counsel_workflow import CounselRun, workflow
from .groq_counsel import GroqCounsel
from .models import CounselAnalysis, now
from .schemas import Page


def _response(row: CounselAnalysis) -> CounselAnalysisResponse:
    return CounselAnalysisResponse(
        **row.snapshot,
        id=row.id,
        payment_id=row.payment_id,
        dispute_record_id=row.dispute_id,
        evidence_pack_id=row.evidence_pack_id,
        evidence_pack_version=row.evidence_pack_version,
        analysis_status=row.analysis_status,
        review_status=row.review_status,
        model=row.model,
        created_at=row.created_at,
    )


def _lock_pack(db: Session, pack_id: int) -> None:
    # End the read transaction before taking a write lock on SQLite.
    db.rollback()
    if db.get_bind().dialect.name == "postgresql":
        locked = db.scalar(
            text("SELECT pg_try_advisory_xact_lock(731003003, :pack)"), {"pack": pack_id}
        )
    else:
        try:
            db.execute(text("BEGIN IMMEDIATE"))
            locked = True
        except OperationalError as exc:
            if getattr(exc.orig, "sqlite_errorname", "") != "SQLITE_BUSY":
                raise
            locked = False
    if not locked:
        raise IntegrationError(
            "COUNSEL_IN_PROGRESS", "An analysis is already running. Try again shortly.", 409
        )


def analyze(
    db: Session,
    groq: GroqCounsel,
    payment_id: str,
    *,
    dispute_id: int | None = None,
    force: bool = False,
) -> CounselAnalysisResponse:
    case = load_case(db, payment_id, dispute_id)
    pack = case.pack
    _lock_pack(db, pack.id)
    if not force:
        cached = db.scalar(
            select(CounselAnalysis)
            .where(
                CounselAnalysis.evidence_pack_id == pack.id,
                CounselAnalysis.analysis_status != "FAILED",
            )
            .order_by(CounselAnalysis.id.desc())
            .limit(1)
        )
        if cached is not None:
            result = _response(cached)
            db.rollback()
            return result

    # Keep the database session on the request thread and disable external graph tracing.
    with tracing_context(enabled=False):
        state = workflow.invoke({}, context=CounselRun(load=lambda: case, reason=groq.reason))
    snapshot = state["snapshot"]
    status = (
        "FAILED"
        if snapshot.error
        else "REVIEW_REQUIRED"
        if snapshot.outcome.conflict
        else "COMPLETED"
    )
    row = CounselAnalysis(
        payment_id=pack.payment_id,
        dispute_id=pack.dispute_record_id,
        evidence_pack_id=pack.id,
        evidence_pack_version=pack.version,
        analysis_status=status,
        review_status="PENDING",
        model=groq.model,
        created_at=now(),
        snapshot=snapshot.model_dump(mode="json"),
    )
    db.add(row)
    db.commit()
    return _response(row)


def _scoped(payment_id: str, dispute_id: int | None):
    conditions = [CounselAnalysis.payment_id == payment_id]
    if dispute_id is not None:
        conditions.append(CounselAnalysis.dispute_id == dispute_id)
    return conditions


def get_analysis(
    db: Session, payment_id: str, dispute_id: int | None = None
) -> CounselAnalysisResponse:
    row = db.scalar(
        select(CounselAnalysis)
        .where(*_scoped(payment_id, dispute_id))
        .order_by(CounselAnalysis.id.desc())
        .limit(1)
    )
    if row is None:
        raise IntegrationError(
            "COUNSEL_ANALYSIS_NOT_FOUND", "No Counsel analysis has been saved for this case.", 404
        )
    return _response(row)


def get_history(
    db: Session,
    payment_id: str,
    dispute_id: int | None,
    page: int,
    page_size: int,
) -> Page[CounselAnalysisResponse]:
    conditions = _scoped(payment_id, dispute_id)
    rows = db.scalars(
        select(CounselAnalysis)
        .where(*conditions)
        .order_by(CounselAnalysis.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return Page[CounselAnalysisResponse](
        items=[_response(row) for row in rows],
        total=db.scalar(select(func.count(CounselAnalysis.id)).where(*conditions)),
        page=page,
        page_size=page_size,
    )

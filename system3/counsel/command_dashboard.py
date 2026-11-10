import logging
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import and_, case, extract, func, literal, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .command_schemas import (
    ActivityEvent,
    ActivityFeed,
    CaseFilters,
    CasePage,
    CounselStats,
    EvidenceStats,
    Overview,
    Period,
)
from .models import (
    CounselAnalysis,
    Dispute,
    DisputeStatusHistory,
    DisputeSyncLog,
    EvidencePack,
    ResolutionAttempt,
    ResolutionEvent,
    ResolutionWorkflow,
    Source,
    now,
)

logger = logging.getLogger(__name__)
ACTIVE = ("OPEN", "WAITING_FOR_BUYER_RESPONSE", "WAITING_FOR_SELLER_RESPONSE", "UNDER_REVIEW")
OUTCOMES = (
    "RESOLVED_BUYER_FAVOUR",
    "RESOLVED_SELLER_FAVOUR",
    "RESOLVED_WITH_PAYOUT",
    "CANCELED_BY_BUYER",
    "ACCEPTED",
    "DENIED",
)
CHECKS = ("mandate_signature", "cart_signature", "ledger", "payment_reference_match")
CHECK_STATUSES = ("VERIFIED", "INVALID", "UNVERIFIED", "UNAVAILABLE", "PENDING")
RECENT_LIMIT = 8
FEED_LIMIT = 30


def period_conditions(filters: Period, timestamp):
    conditions = []
    if filters.source is not None:
        conditions.append(Dispute.source == filters.source)
    start, end = filters.start, filters.end
    if filters.days is not None:
        end = now().date()
        start = end - timedelta(days=filters.days - 1)
    if start is not None and end is not None:
        conditions.append(
            timestamp.between(
                datetime.combine(start, time.min, UTC),
                datetime.combine(end, time.max, UTC),
            )
        )
    return conditions


def groups(rows):
    return [{"label": label, "count": count} for label, count in rows]


def breakdown(db: Session, column, conditions):
    return groups(
        db.execute(
            select(column, func.count())
            .select_from(Dispute)
            .where(*conditions)
            .group_by(column)
            .order_by(func.count().desc(), column.asc().nulls_last())
        )
    )


def overview(db: Session, filters: Period) -> Overview:
    filters_by_opening = period_conditions(filters, Dispute.created_at)
    as_of = now()
    active = Dispute.status.in_(ACTIVE)
    resolved = Dispute.status == "RESOLVED"
    known_amount = and_(Dispute.currency.is_not(None), Dispute.amount_minor.is_not(None))
    counts = (
        db.execute(
            select(
                func.count().label("total"),
                func.count().filter(active).label("open"),
                func.count().filter(resolved).label("resolved"),
                func.count()
                .filter(Dispute.status == "WAITING_FOR_SELLER_RESPONSE")
                .label("awaiting_merchant"),
                func.count().filter(active, Dispute.deadline < as_of).label("overdue"),
                func.count()
                .filter(active, Dispute.deadline.between(as_of, as_of + timedelta(days=3)))
                .label("due_soon"),
                func.count().filter(Dispute.link_status != "LINKED").label("unlinked"),
                func.count().filter(~known_amount).label("unknown_amounts"),
                func.count().filter(Dispute.created_at.is_(None)).label("unknown_open_dates"),
            )
            .select_from(Dispute)
            .where(*filters_by_opening)
        )
        .mappings()
        .one()
    )

    amount_rows = db.execute(
        select(Dispute.currency, func.sum(Dispute.amount_minor))
        .where(*filters_by_opening, known_amount)
        .group_by(Dispute.currency)
        .order_by(Dispute.currency)
    )
    amounts = [{"currency": currency, "amount_minor": amount} for currency, amount in amount_rows]
    postgres = db.get_bind().dialect.name == "postgresql"
    opened = func.timezone("UTC", Dispute.created_at) if postgres else Dispute.created_at
    day = func.date(opened)
    currency = case((known_amount, Dispute.currency))
    daily_rows = db.execute(
        select(day, currency, func.count(), func.sum(Dispute.amount_minor))
        .where(*filters_by_opening, Dispute.created_at.is_not(None))
        .group_by(day, currency)
        .order_by(day, currency)
    )
    daily: dict[date, dict] = {}
    for date_value, currency_code, count, amount in daily_rows:
        bucket = daily.setdefault(date_value, {"day": date_value, "count": 0, "amounts": []})
        bucket["count"] += count
        if currency_code is not None:
            bucket["amounts"].append({"currency": currency_code, "amount_minor": amount})

    history = DisputeStatusHistory
    first_resolution = (
        select(history.dispute_id, func.min(history.changed_at).label("observed_at"))
        .where(history.new_status == "RESOLVED", history.previous_status.is_not(None))
        .group_by(history.dispute_id)
        .subquery()
    )
    observed_at = first_resolution.c.observed_at
    hours = (
        extract("epoch", observed_at - Dispute.created_at) / 3600
        if postgres
        else (func.julianday(observed_at) - func.julianday(Dispute.created_at)) * 24
    )
    average, samples = db.execute(
        select(func.avg(hours), func.count())
        .select_from(Dispute)
        .join(first_resolution, first_resolution.c.dispute_id == Dispute.id)
        .where(*filters_by_opening, resolved, observed_at >= Dispute.created_at)
    ).one()
    outcome = Dispute.raw_response["dispute_outcome"]["outcome_code"].as_string()
    outcome = case((outcome.in_(OUTCOMES), outcome))
    latest_sync = (
        db.execute(
            select(DisputeSyncLog.finished_at, DisputeSyncLog.result)
            .order_by(DisputeSyncLog.id.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    return Overview(
        **counts,
        amounts=amounts,
        daily=list(daily.values()),
        as_of=as_of,
        resolution_rate=counts["resolved"] / counts["total"] if counts["total"] else None,
        average_observed_resolution_hours=round(float(average), 2) if average is not None else None,
        resolution_samples=samples,
        by_status=breakdown(db, Dispute.status, filters_by_opening),
        by_reason=breakdown(db, Dispute.reason, filters_by_opening),
        by_outcome=breakdown(db, outcome, [*filters_by_opening, resolved]),
        latest_sync=latest_sync,
    )


def gap_counts(snapshot):
    return [
        func.coalesce(func.json_array_length(snapshot[key].as_json()), 0)
        for key in ("missing_evidence", "invalid_evidence", "contradictions")
    ]


def with_artifact(query, model, payment_id, order):
    latest = (
        select(model.id)
        .where(model.dispute_id == Dispute.id, model.payment_id == payment_id)
        .order_by(order.desc())
        .limit(1)
        .correlate(Dispute)
        .scalar_subquery()
    )
    return query.outerjoin(model, model.id == latest)


def case_rows(db: Session, filters: CaseFilters, conditions, payment_id, disabled):
    dispute_columns = [
        Dispute.id,
        Dispute.paypal_dispute_id,
        payment_id.label("payment_id"),
        Dispute.link_status,
        Dispute.source,
        Dispute.reason,
        Dispute.status,
        Dispute.amount_minor,
        Dispute.currency,
        Dispute.created_at,
        Dispute.updated_at,
        Dispute.deadline,
    ]
    query = select(*dispute_columns).where(*conditions)
    evidence_status = evidence_version = counsel_status = counsel_version = literal(None)
    if "evidence" not in disabled:
        query = with_artifact(query, EvidencePack, payment_id, EvidencePack.version)
        missing, invalid, contradictions = gap_counts(EvidencePack.snapshot)
        evidence_status = case(
            (EvidencePack.id.is_(None), "NOT_ASSEMBLED"),
            ((missing + invalid + contradictions) > 0, "SAVED_WITH_GAPS"),
            else_="SAVED",
        )
        evidence_version = EvidencePack.version
    if "counsel" not in disabled:
        query = with_artifact(query, CounselAnalysis, payment_id, CounselAnalysis.id)
        counsel_status = case(
            (CounselAnalysis.id.is_(None), "NOT_ANALYZED"),
            (CounselAnalysis.analysis_status == "FAILED", "FAILED"),
            (CounselAnalysis.evidence_pack_version < evidence_version, "STALE"),
            else_=CounselAnalysis.analysis_status,
        )
        counsel_version = CounselAnalysis.evidence_pack_version
    query = query.add_columns(
        evidence_status.label("evidence_status"),
        evidence_version.label("evidence_version"),
        counsel_status.label("counsel_status"),
        counsel_version.label("counsel_version"),
    )
    sorts = {column.key: column for column in dispute_columns}
    sorts.update(evidence_status=evidence_status, counsel_status=counsel_status)
    sort = sorts[filters.sort]
    ordered = sort.asc() if filters.order == "asc" else sort.desc()
    currency_order = [Dispute.currency.asc().nulls_last()] if filters.sort == "amount_minor" else []
    return (
        db.execute(
            query.order_by(*currency_order, ordered.nulls_last(), Dispute.id.desc())
            .offset((filters.page - 1) * filters.page_size)
            .limit(filters.page_size)
        )
        .mappings()
        .all()
    )


def cases(db: Session, filters: CaseFilters) -> CasePage:
    # N2 accepts the fixture's stored payment reference even when N1 could not match it.
    payment_id = func.coalesce(
        Dispute.payment_id,
        case(
            (
                Dispute.source == Source.SIMULATED_FIXTURE,
                Dispute.raw_response["payment_id"].as_string(),
            )
        ),
    )
    conditions = period_conditions(filters, Dispute.created_at)
    for field in ("status", "reason"):
        if (value := getattr(filters, field)) is not None:
            conditions.append(getattr(Dispute, field) == value)
    if filters.search and (term := filters.search.strip()):
        conditions.append(
            or_(
                Dispute.paypal_dispute_id.icontains(term, autoescape=True),
                payment_id.icontains(term, autoescape=True),
            )
        )
    total = db.scalar(select(func.count()).select_from(Dispute).where(*conditions))
    disabled: list[str] = []
    try:
        rows = case_rows(db, filters, conditions, payment_id, disabled)
    except SQLAlchemyError:
        db.rollback()
        # Keep N1 visible if an independently deployed module's table is unavailable.
        for name, model in (("evidence", EvidencePack), ("counsel", CounselAnalysis)):
            try:
                db.execute(select(model.id).limit(1)).first()
            except SQLAlchemyError:
                db.rollback()
                disabled.append(name)
                logger.warning("Dashboard case metadata unavailable for %s", name)
        if not disabled:
            raise
        rows = case_rows(db, filters, conditions, payment_id, disabled)
    return CasePage(
        items=rows,
        total=total,
        page=filters.page,
        page_size=filters.page_size,
        as_of=now(),
        unavailable=disabled,
    )


def artifact_query(model, filters, timestamp, *columns):
    return (
        select(*columns)
        .select_from(model)
        .outerjoin(Dispute, Dispute.id == model.dispute_id)
        .where(*period_conditions(filters, timestamp))
    )


def references(model):
    return (model.id, model.dispute_id, Dispute.paypal_dispute_id, Dispute.source, model.payment_id)


def per_workflow(model, order):
    return (
        func.row_number()
        .over(partition_by=(model.dispute_id, model.payment_id), order_by=order.desc())
        .label("workflow_rank")
    )


def evidence_stats(db: Session, filters: Period) -> EvidenceStats:
    pack = EvidencePack
    missing, invalid, contradictions = gap_counts(pack.snapshot)
    verification = [
        pack.snapshot["verification_summary"][name]["status"].as_string().label(name)
        for name in CHECKS
    ]
    scope = (
        artifact_query(
            pack,
            filters,
            pack.generated_at,
            *references(pack),
            pack.version,
            pack.generated_at,
            missing.label("missing"),
            invalid.label("invalid"),
            contradictions.label("contradictions"),
            *verification,
            per_workflow(pack, pack.version),
        )
        .subquery()
        .c
    )
    latest = scope.workflow_rank == 1
    status_counts = [
        sum(
            func.count().filter(latest, func.coalesce(scope[name], "UNAVAILABLE") == status)
            for name in CHECKS
        ).label(status)
        for status in CHECK_STATUSES
    ]
    counts = (
        db.execute(
            select(
                func.count().label("packs_generated"),
                func.count().filter(latest).label("latest_packs"),
                func.count()
                .filter(latest, scope.missing + scope.invalid + scope.contradictions > 0)
                .label("packs_with_gaps"),
                *[
                    func.coalesce(func.sum(col).filter(latest), 0).label(label)
                    for col, label in (
                        (scope.missing, "missing_records"),
                        (scope.invalid, "invalid_records"),
                        (scope.contradictions, "contradictions"),
                    )
                ],
                *status_counts,
            ).select_from(scope.id.table)
        )
        .mappings()
        .one()
    )
    counts = dict(counts)
    verification_counts = {status: counts.pop(status) for status in CHECK_STATUSES}
    decided = verification_counts["VERIFIED"] + verification_counts["INVALID"]
    recent = db.execute(
        select(
            scope.id,
            scope.dispute_id,
            scope.paypal_dispute_id,
            scope.source,
            scope.payment_id,
            scope.version,
            scope.generated_at,
            scope.missing,
            scope.invalid,
            scope.contradictions,
        )
        .where(latest)
        .order_by(scope.generated_at.desc(), scope.id.desc())
        .limit(RECENT_LIMIT)
    ).mappings()
    return EvidenceStats(
        **counts,
        verification_rate=verification_counts["VERIFIED"] / decided if decided else None,
        verification=groups(
            (status, count) for status, count in verification_counts.items() if count
        ),
        recent=list(recent),
    )


def counsel_fields():
    snapshot = CounselAnalysis.snapshot
    return (
        snapshot["outcome"]["recommendation"].as_string().label("recommendation"),
        (func.length(func.trim(snapshot["outcome"]["draft_response"].as_string())) > 0).label(
            "has_draft"
        ),
    )


def counsel_stats(db: Session, filters: Period) -> CounselStats:
    analysis = CounselAnalysis
    scope = (
        artifact_query(
            analysis,
            filters,
            analysis.created_at,
            *references(analysis),
            analysis.created_at,
            analysis.analysis_status.label("status"),
            analysis.review_status,
            analysis.evidence_pack_version.label("evidence_version"),
            *counsel_fields(),
        )
        .subquery()
        .c
    )
    recommended = and_(scope.recommendation.is_not(None), scope.status != "FAILED")
    standalone_payment = case((scope.dispute_id.is_(None), scope.payment_id))
    pending = (
        select(
            scope.review_status,
            func.row_number()
            .over(partition_by=(scope.dispute_id, standalone_payment), order_by=scope.id.desc())
            .label("review_rank"),
        )
        .where(recommended)
        .subquery()
    )
    pending_reviews = db.scalar(
        select(func.count())
        .select_from(pending)
        .where(pending.c.review_rank == 1, pending.c.review_status == "PENDING")
    )
    counts = (
        db.execute(
            select(
                func.count().label("analyses"),
                func.count().filter(recommended).label("recommendations"),
                func.count().filter(recommended, scope.has_draft).label("drafts"),
            ).select_from(scope.id.table)
        )
        .mappings()
        .one()
    )
    by_status = db.execute(
        select(scope.status, func.count()).group_by(scope.status).order_by(scope.status)
    )
    by_recommendation = db.execute(
        select(scope.recommendation, func.count())
        .where(recommended)
        .group_by(scope.recommendation)
        .order_by(scope.recommendation)
    )
    recent = db.execute(
        select(
            scope.id,
            scope.dispute_id,
            scope.paypal_dispute_id,
            scope.source,
            scope.payment_id,
            scope.created_at,
            scope.status,
            scope.review_status,
            scope.recommendation,
            scope.evidence_version,
        )
        .order_by(scope.created_at.desc(), scope.id.desc())
        .limit(RECENT_LIMIT)
    ).mappings()
    return CounselStats(
        **counts,
        pending_reviews=pending_reviews,
        by_status=groups(by_status),
        by_recommendation=groups(by_recommendation),
        recent=list(recent),
    )


def event(row, kind, status, timestamp, *, version=None):
    return ActivityEvent(
        id=f"{kind}-{row.id}",
        type=kind,
        status=status,
        timestamp=timestamp,
        dispute_id=row.dispute_id,
        paypal_dispute_id=row.paypal_dispute_id,
        source=row.source,
        payment_id=row.payment_id,
        evidence_version=version,
    )


def dispute_events(db, filters):
    history = DisputeStatusHistory
    query = artifact_query(
        history,
        filters,
        history.changed_at,
        history.id,
        history.dispute_id,
        Dispute.paypal_dispute_id,
        history.source,
        Dispute.payment_id,
        history.changed_at,
        history.previous_status,
        history.new_status,
    )
    for row in db.execute(
        query.order_by(history.changed_at.desc(), history.id.desc()).limit(FEED_LIMIT)
    ):
        kind = "DISPUTE_STATUS_CHANGED"
        if row.previous_status is None:
            kind = (
                "PAYPAL_DISPUTE_RECEIVED"
                if row.source == Source.PAYPAL_API
                else "SIMULATED_DISPUTE_ADDED"
            )
        yield event(row, kind, row.new_status, row.changed_at)


def evidence_events(db, filters):
    pack = EvidencePack
    missing, invalid, contradictions = gap_counts(pack.snapshot)
    statuses = [
        pack.snapshot["verification_summary"][name]["status"].as_string().label(name)
        for name in CHECKS
    ]
    query = artifact_query(
        pack,
        filters,
        pack.generated_at,
        *references(pack),
        pack.generated_at,
        pack.version,
        (missing + invalid + contradictions).label("gaps"),
        *statuses,
    )
    for row in db.execute(
        query.order_by(pack.generated_at.desc(), pack.id.desc()).limit(FEED_LIMIT)
    ):
        yield event(
            row,
            "EVIDENCE_PACK_GENERATED",
            f"Version {row.version}",
            row.generated_at,
            version=row.version,
        )
        results = {row._mapping[name] for name in CHECKS}
        if results & {"VERIFIED", "INVALID"}:
            yield event(
                row,
                "VERIFICATION_RESULTS_RECORDED",
                "INVALID_CHECK_PRESENT" if "INVALID" in results else "VERIFIED_CHECK_PRESENT",
                row.generated_at,
                version=row.version,
            )
        if row.gaps:
            yield event(
                row,
                "EVIDENCE_GAPS_RECORDED",
                f"{row.gaps} issues",
                row.generated_at,
                version=row.version,
            )


def counsel_events(db, filters):
    analysis = CounselAnalysis
    query = artifact_query(
        analysis,
        filters,
        analysis.created_at,
        *references(analysis),
        analysis.created_at,
        analysis.analysis_status,
        *counsel_fields(),
    )
    for row in db.execute(
        query.order_by(analysis.created_at.desc(), analysis.id.desc()).limit(FEED_LIMIT)
    ):
        kind = "AI_ANALYSIS_FAILED" if row.analysis_status == "FAILED" else "AI_ANALYSIS_COMPLETED"
        yield event(row, kind, row.analysis_status, row.created_at)
        if row.has_draft and row.recommendation and row.analysis_status != "FAILED":
            yield event(row, "MERCHANT_DRAFT_GENERATED", "PENDING_HUMAN_REVIEW", row.created_at)


def resolution_events(db, filters):
    attempt = ResolutionAttempt
    query = artifact_query(
        attempt,
        filters,
        attempt.created_at,
        attempt.id,
        attempt.dispute_id,
        Dispute.paypal_dispute_id,
        Dispute.source,
        Dispute.payment_id,
        attempt.created_at,
        attempt.action,
        attempt.status,
    )
    for row in db.execute(
        query.where(attempt.workflow_id.is_(None))
        .order_by(attempt.created_at.desc(), attempt.id.desc()).limit(FEED_LIMIT)
    ):
        yield event(
            row,
            f"RESOLUTION_{row.action.replace('-', '_').upper()}_ATTEMPTED",
            row.status,
            row.created_at,
        )


def workflow_events(db, filters):
    audit = ResolutionEvent
    workflow = ResolutionWorkflow
    query = select(
        audit.id, workflow.dispute_id, Dispute.paypal_dispute_id, Dispute.source,
        workflow.payment_id, audit.created_at, audit.event_type, audit.new_status,
    ).select_from(audit).join(workflow, audit.workflow_id == workflow.id).join(
        Dispute, workflow.dispute_id == Dispute.id
    ).where(*period_conditions(filters, audit.created_at))
    for row in db.execute(query.order_by(audit.created_at.desc(), audit.id.desc()).limit(FEED_LIMIT)):
        yield event(row, f"RESOLUTION_{row.event_type}", row.new_status, row.created_at)


def activity(db: Session, filters: Period) -> ActivityFeed:
    unavailable = []
    events = []
    for name, loader in (
        ("disputes", dispute_events),
        ("evidence", evidence_events),
        ("counsel", counsel_events),
        ("resolution", resolution_events),
        ("resolution_workflows", workflow_events),
    ):
        try:
            events.extend(list(loader(db, filters)))
        except SQLAlchemyError:
            db.rollback()
            unavailable.append(name)
            logger.warning("Dashboard activity unavailable for %s", name)
    events.sort(key=lambda item: (item.timestamp, item.id), reverse=True)
    return ActivityFeed(items=events[:FEED_LIMIT], unavailable=unavailable)

from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .clients import IntegrationError, PayPalClient, System2Client
from .counsel_schemas import AnalysisSnapshot
from .evidence_schemas import EvidenceSnapshot
from .models import (
    CounselAnalysis,
    Dispute,
    EvidencePack,
    ResolutionAttempt,
    ResolutionEvent,
    ResolutionWorkflow,
    Source,
    now,
)
from .resolution import (
    MONETARY,
    ActionPlan,
    action_payload,
    conflicting_attempt,
    current_case,
    disabled_reason,
    eligible_option,
    get_dispute,
    simulated_system2_payment,
)
from .resolution_auth import Actor
from .resolution_schemas import (
    CaseEvidence,
    InformationRequest,
    OpenWorkflow,
    QueueFilters,
    ResolutionStats,
    Review,
    WorkflowDetail,
    WorkflowRow,
)
from .schemas import Page
from .sync import acquire_sync_lock

STRATEGIES = {
    "DEFEND": "DEFEND",
    "CONCEDE_REMEDY": "REMEDY",
    "REQUEST_MORE_EVIDENCE": "INFORMATION",
}
TERMINAL = ("CONFIRMED", "REJECTED", "CANCELLED")
REVIEWABLE = (
    "PENDING_REVIEW",
    "ADDITIONAL_INFORMATION_REQUIRED",
    "READY_FOR_APPROVAL",
    "APPROVED",
    "FAILED",
)


def reject(code: str, message: str, status: int = 409):
    raise IntegrationError(code, message, status)


def lock(db: Session) -> None:
    db.rollback()
    acquire_sync_lock(db)


def get_workflow(db: Session, id: int, revision: int | None = None) -> ResolutionWorkflow:
    row = db.get(ResolutionWorkflow, id)
    if row is None:
        reject("WORKFLOW_NOT_FOUND", "Resolution workflow not found.", 404)
    if revision is not None and row.revision != revision:
        reject("WORKFLOW_CHANGED", "The workflow changed. Refresh and review the latest version.")
    return row


def record(
    db: Session, row: ResolutionWorkflow, actor: Actor, event: str, status: str, **details
) -> None:
    previous = row.status if row.revision else None
    row.revision += 1
    row.status = status
    row.updated_at = now()
    db.add(
        ResolutionEvent(
            workflow_id=row.id,
            revision=row.revision,
            actor_id=actor.id,
            event_type=event,
            previous_status=previous,
            new_status=status,
            details=details,
            created_at=row.updated_at,
        )
    )


def current_ids(db: Session, dispute: Dispute) -> tuple[int | None, int | None]:
    if not dispute.payment_id or dispute.link_status != "LINKED":
        return None, None
    evidence = db.scalar(
        select(EvidencePack.id)
        .where(EvidencePack.dispute_id == dispute.id, EvidencePack.payment_id == dispute.payment_id)
        .order_by(EvidencePack.version.desc())
        .limit(1)
    )
    analysis = db.scalar(
        select(CounselAnalysis.id)
        .where(
            CounselAnalysis.dispute_id == dispute.id,
            CounselAnalysis.payment_id == dispute.payment_id,
        )
        .order_by(CounselAnalysis.id.desc())
        .limit(1)
    )
    return analysis, evidence


def pinned_inputs(db: Session, analysis_id, pack_id):
    analysis = db.get(CounselAnalysis, analysis_id) if analysis_id else None
    pack = db.get(EvidencePack, pack_id) if pack_id else None
    if (
        analysis is None
        or pack is None
        or analysis.evidence_pack_id != pack.id
        or analysis.evidence_pack_version != pack.version
        or analysis.dispute_id != pack.dispute_id
        or analysis.payment_id != pack.payment_id
        or analysis.analysis_status == "FAILED"
    ):
        reject("COUNSEL_REQUIRED", "Run AI Counsel on the latest evidence pack for this dispute.")
    try:
        outcome = AnalysisSnapshot.model_validate(analysis.snapshot).outcome
        evidence = EvidenceSnapshot.model_validate(pack.snapshot)
    except ValidationError:
        reject("INPUTS_UNAVAILABLE", "The saved Counsel or evidence snapshot could not be read.")
    if evidence.payment_id != pack.payment_id or evidence.dispute_record_id != pack.dispute_id:
        reject("INPUTS_UNAVAILABLE", "The pinned evidence references a different workflow.")
    if outcome is None:
        reject("COUNSEL_REQUIRED", "A successful Counsel recommendation is required.")
    return outcome, evidence, pack.version


def stale(db: Session, row: ResolutionWorkflow, dispute: Dispute) -> bool:
    return row.payment_id != dispute.payment_id or current_ids(db, dispute) != (
        row.analysis_id,
        row.evidence_pack_id,
    )


def require_fresh(db: Session, row: ResolutionWorkflow, dispute: Dispute) -> None:
    if stale(db, row, dispute):
        reject(
            "REVIEW_STALE", "Refresh the workflow from the latest evidence and Counsel analysis."
        )


def review_state(row: ResolutionWorkflow) -> str:
    if row.strategy == "INFORMATION" or any(not item["fulfilled_at"] for item in row.information):
        return "ADDITIONAL_INFORMATION_REQUIRED"
    return "PENDING_REVIEW"


def open_workflow(db: Session, actor: Actor, body: OpenWorkflow, *, allow_simulated: bool = False) -> int:
    actor.require_review()
    lock(db)
    dispute = get_dispute(db, body.dispute_id)
    if dispute.source != Source.PAYPAL_API and not (
        allow_simulated and dispute.source == Source.SIMULATED_FIXTURE
    ):
        reject("SIMULATED_DISPUTE", "Simulated disputes are read-only.", 403)
    existing = db.scalar(
        select(ResolutionWorkflow)
        .where(
            ResolutionWorkflow.dispute_id == dispute.id,
        )
        .order_by(ResolutionWorkflow.id.desc())
        .limit(1)
    )
    if existing and existing.status not in TERMINAL:
        return existing.id
    analysis_id, pack_id = current_ids(db, dispute)
    if existing and existing.analysis_id == analysis_id:
        return existing.id
    outcome, _, _ = pinned_inputs(db, analysis_id, pack_id)
    started = now()
    row = ResolutionWorkflow(
        dispute_id=dispute.id,
        payment_id=dispute.payment_id,
        analysis_id=analysis_id,
        evidence_pack_id=pack_id,
        recommendation=outcome.recommendation,
        strategy=STRATEGIES[outcome.recommendation],
        status="PENDING_REVIEW",
        revision=0,
        assigned_reviewer=actor.id if body.assign_to_self else None,
        information=[],
        created_at=started,
        updated_at=started,
    )
    db.add(row)
    db.flush()
    record(
        db,
        row,
        actor,
        "WORKFLOW_CREATED",
        review_state(row),
        analysis_id=analysis_id,
        evidence_pack_id=pack_id,
        recommendation=outcome.recommendation,
    )
    db.commit()
    return row.id


def summary(row: ResolutionWorkflow, dispute: Dispute) -> WorkflowRow:
    return WorkflowRow(
        id=row.id,
        dispute_id=dispute.id,
        paypal_dispute_id=dispute.paypal_dispute_id,
        payment_id=row.payment_id,
        reason=dispute.reason,
        amount_minor=dispute.amount_minor,
        currency=dispute.currency,
        recommendation=row.recommendation,
        strategy=row.strategy,
        status=row.status,
        assigned_reviewer=row.assigned_reviewer,
        deadline=dispute.deadline,
        updated_at=row.updated_at,
    )


def detail(db: Session, actor: Actor, id: int) -> WorkflowDetail:
    row = get_workflow(db, id)
    dispute = get_dispute(db, row.dispute_id)
    outcome, evidence, version = pinned_inputs(db, row.analysis_id, row.evidence_pack_id)
    events = db.scalars(
        select(ResolutionEvent)
        .where(ResolutionEvent.workflow_id == id)
        .order_by(ResolutionEvent.revision.desc())
        .limit(100)
    ).all()
    approval = approval_event(db, row)
    attempts = db.scalars(
        select(ResolutionAttempt)
        .where(ResolutionAttempt.workflow_id == id)
        .order_by(ResolutionAttempt.id.desc())
        .limit(30)
    ).all()
    return WorkflowDetail(
        **summary(row, dispute).model_dump(),
        revision=row.revision,
        analysis_id=row.analysis_id,
        evidence_version=version,
        stale=stale(db, row, dispute),
        actor=actor,
        counsel=outcome,
        plan=row.plan,
        approval=approval,
        requests=row.information,
        attempts=attempts,
        activity=list(reversed(events)),
        dispute_status=dispute.status,
        paypal_status=row.paypal_status,
        paypal_checked_at=row.paypal_checked_at,
        submitted_at=row.submitted_at,
        completed_at=row.completed_at,
        verification=evidence.verification_summary,
        gaps=[*evidence.missing_evidence, *evidence.invalid_evidence, *evidence.contradictions],
        evidence=[
            CaseEvidence.model_validate(item, from_attributes=True) for item in evidence.evidence
        ],
    )


def approval_event(db: Session, row: ResolutionWorkflow) -> ResolutionEvent | None:
    if row.approval_revision is None:
        return None
    return db.scalar(
        select(ResolutionEvent).where(
            ResolutionEvent.workflow_id == row.id,
            ResolutionEvent.revision == row.approval_revision,
            ResolutionEvent.event_type == "APPROVED",
        )
    )


def list_workflows(db: Session, filters: QueueFilters) -> Page[WorkflowRow]:
    model = ResolutionWorkflow
    conditions = []
    if filters.status:
        conditions.append(model.status == filters.status)
    if filters.strategy:
        conditions.append(model.strategy == filters.strategy)
    if filters.search:
        term = filters.search.strip()
        conditions.append(
            or_(
                Dispute.paypal_dispute_id.icontains(term, autoescape=True),
                model.payment_id.icontains(term, autoescape=True),
            )
        )
    sort_table = (
        Dispute
        if filters.sort in ("paypal_dispute_id", "reason", "amount_minor", "deadline")
        else model
    )
    sort = getattr(sort_table, filters.sort)
    order = sort.asc() if filters.order == "asc" else sort.desc()
    amount_order = [Dispute.currency.asc().nulls_last()] if filters.sort == "amount_minor" else []
    rows = db.execute(
        select(model, Dispute)
        .join(Dispute, model.dispute_id == Dispute.id)
        .where(*conditions)
        .order_by(*amount_order, order.nulls_last(), model.id.desc())
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    )
    total = db.scalar(
        select(func.count())
        .select_from(model)
        .join(Dispute, model.dispute_id == Dispute.id)
        .where(*conditions)
    )
    return Page[WorkflowRow](
        items=[summary(row, dispute) for row, dispute in rows],
        total=total,
        page=filters.page,
        page_size=filters.page_size,
    )


def statistics(db: Session, *conditions) -> ResolutionStats:
    model = ResolutionWorkflow
    counts = dict(
        db.execute(
            select(model.status, func.count())
            .select_from(model)
            .join(Dispute, model.dispute_id == Dispute.id)
            .where(*conditions)
            .group_by(model.status)
        ).all()
    )
    return ResolutionStats(
        total=sum(counts.values()),
        awaiting_approval=counts.get("READY_FOR_APPROVAL", 0),
        pending_information=counts.get("ADDITIONAL_INFORMATION_REQUIRED", 0),
        submitted=counts.get("SUBMITTED", 0) + counts.get("CONFIRMED", 0),
        confirmed=counts.get("CONFIRMED", 0),
        failed=counts.get("FAILED", 0),
        unknown=counts.get("UNKNOWN", 0),
        by_status=counts,
    )


def require_editable(row: ResolutionWorkflow, actor: Actor) -> None:
    if row.status not in REVIEWABLE:
        reject("INVALID_TRANSITION", "This workflow can no longer be edited.")
    if row.assigned_reviewer and row.assigned_reviewer != actor.id and actor.role != "operator":
        reject(
            "ASSIGNED_REVIEWER_REQUIRED", "Only the assigned reviewer or an operator can edit.", 403
        )


def require_provider(dispute: Dispute, paypal: PayPalClient) -> None:
    if reason := disabled_reason(dispute, paypal):
        reject("RESOLUTION_DISABLED", reason, 403)


def evidence_note(body: Review, pack: EvidenceSnapshot) -> str:
    if len(set(body.evidence_ids)) != len(body.evidence_ids):
        reject("DUPLICATE_EVIDENCE", "Select each evidence record once.", 422)
    selected = {item.id: item for item in pack.evidence if item.id in body.evidence_ids}
    if len(selected) != len(body.evidence_ids) or any(
        item.payment_id != pack.payment_id
        or item.source_type != "REAL_RECORD"
        or item.verification_status == "INVALID"
        for item in selected.values()
    ):
        reject(
            "INVALID_EVIDENCE",
            "Choose real, non-invalid records from the pinned evidence pack.",
            422,
        )
    if body.action == "submit-evidence" and not selected:
        reject(
            "EVIDENCE_REQUIRED", "Select supporting evidence before preparing the submission.", 422
        )
    if body.action in ("submit-evidence", "reply-inquiry") and not body.text:
        reject("NOTE_REQUIRED", "Enter the merchant response.", 422)
    if selected and body.action not in MONETARY:
        lines = []
        for id in body.evidence_ids:
            item = selected[id]
            label = item.type.replace("_", " ").lower()
            lines.append(f"- {label} {item.source_record_id} ({item.verification_status.lower()})")
        return f"{body.text}\n\nSupporting records\n" + "\n".join(lines)
    return body.text


def prepare(
    db: Session, actor: Actor, paypal: PayPalClient, system2: System2Client, id: int, body: Review
) -> None:
    row = get_workflow(db, id, body.revision)
    require_editable(row, actor)
    dispute = get_dispute(db, row.dispute_id)
    require_provider(dispute, paypal)
    require_fresh(db, row, dispute)
    if not body.action:
        reject("ACTION_REQUIRED", "Choose a resolution action.", 422)
    if any(not item["fulfilled_at"] for item in row.information):
        reject("INFORMATION_OUTSTANDING", "Close outstanding information requests first.")
    strategy = "REMEDY" if body.action in MONETARY else "DEFEND"
    if strategy != STRATEGIES[row.recommendation] and not body.override_reason:
        reject(
            "OVERRIDE_REASON_REQUIRED",
            "Explain why the proposed strategy differs from Counsel.",
            422,
        )
    _, evidence, _ = pinned_inputs(db, row.analysis_id, row.evidence_pack_id)
    note = evidence_note(body, evidence)
    db.expunge(dispute)
    db.rollback()
    if dispute.source == Source.SIMULATED_FIXTURE:
        payment = simulated_system2_payment(system2, dispute)
        option = eligible_option(system2, dispute, None, body.action)
        if payment is None:
            reject("ACTION_UNAVAILABLE", "System 2 has no real capture for this simulated dispute.")
        provider_status, provider_stage, capture_id = (
            "SIMULATED_SANDBOX",
            "SYSTEM_2",
            payment.paypal_capture_id,
        )
    else:
        case = current_case(paypal, dispute)
        option = eligible_option(system2, dispute, case, body.action)
        provider_status, provider_stage, capture_id = (
            case.status,
            case.dispute_life_cycle_stage,
            case.capture_id,
        )
    plan = ActionPlan(
        option=option,
        paypal_status=provider_status,
        paypal_stage=provider_stage,
        capture_id=capture_id,
        payload=action_payload(option, note),
        evidence_ids=body.evidence_ids,
        override_reason=body.override_reason,
    ).model_dump(mode="json")
    lock(db)
    row = get_workflow(db, id, body.revision)
    require_fresh(db, row, get_dispute(db, row.dispute_id))
    if conflicting_attempt(db, row.dispute_id, body.action, capture_id):
        reject(
            "ALREADY_ATTEMPTED", "A prior submission blocks this action. Review the case in PayPal."
        )
    row.plan = plan
    row.strategy = strategy
    row.approval_revision = None
    row.paypal_status = provider_status
    row.paypal_checked_at = now()
    record(
        db,
        row,
        actor,
        "RESPONSE_PREPARED",
        "READY_FOR_APPROVAL",
        package=plan,
        merchant_response=body.text,
        evidence_reviewed=body.evidence_ids,
    )
    db.commit()


def review(
    db: Session,
    actor: Actor,
    paypal: PayPalClient,
    system2: System2Client,
    id: int,
    body: Review,
) -> None:
    actor.require_review()
    if body.command == "prepare":
        prepare(db, actor, paypal, system2, id, body)
        return
    lock(db)
    row = get_workflow(db, id, body.revision)
    require_editable(row, actor)
    status = row.status
    event, details = body.command.upper().replace("-", "_"), {}
    if body.command == "approve":
        if status != "READY_FOR_APPROVAL" or not row.plan:
            reject("INVALID_TRANSITION", "Prepare a response before approving it.")
        if row.assigned_reviewer and row.assigned_reviewer != actor.id:
            reject("ASSIGNED_REVIEWER_REQUIRED", "Only the assigned reviewer can approve.", 403)
        require_fresh(db, row, get_dispute(db, row.dispute_id))
        row.approval_revision = row.revision + 1
        status = "APPROVED"
        details = {
            "package": row.plan,
            "analysis_id": row.analysis_id,
            "evidence_pack_id": row.evidence_pack_id,
            "comments": body.text,
        }
        event = "APPROVED"
    elif body.command == "request-info":
        if not body.team or not body.text:
            reject("REQUEST_DETAILS_REQUIRED", "Enter a team and the information needed.", 422)
        if len(row.information) >= 50:
            reject("REQUEST_LIMIT", "This workflow has reached its information request limit.", 422)
        request = InformationRequest(
            id=uuid4().hex, team=body.team, description=body.text, requested_at=now()
        ).model_dump(mode="json")
        row.information = [*row.information, request]
        row.approval_revision = None
        row.plan = None
        status = "ADDITIONAL_INFORMATION_REQUIRED"
        details = {"request": request}
    elif body.command == "receive-info":
        if status != "ADDITIONAL_INFORMATION_REQUIRED" or not body.request_id or not body.text:
            reject(
                "INFORMATION_RESPONSE_REQUIRED",
                "Choose an outstanding request and enter the response.",
                422,
            )
        request = next(
            (
                item
                for item in row.information
                if item["id"] == body.request_id and not item["fulfilled_at"]
            ),
            None,
        )
        if request is None:
            reject("REQUEST_NOT_OPEN", "This information request is no longer open.")
        fulfilled = {**request, "response": body.text, "fulfilled_at": now().isoformat()}
        row.information = [
            fulfilled if item["id"] == body.request_id else item for item in row.information
        ]
        if all(item["fulfilled_at"] for item in row.information):
            status = "PENDING_REVIEW"
        details = {"request": fulfilled}
    elif body.command == "refresh":
        dispute = get_dispute(db, row.dispute_id)
        if not stale(db, row, dispute):
            reject("INPUTS_CURRENT", "This workflow already uses the latest analysis and evidence.")
        analysis_id, pack_id = current_ids(db, dispute)
        outcome, _, _ = pinned_inputs(db, analysis_id, pack_id)
        row.payment_id, row.analysis_id, row.evidence_pack_id = (
            dispute.payment_id,
            analysis_id,
            pack_id,
        )
        row.recommendation = outcome.recommendation
        row.strategy = STRATEGIES[row.recommendation]
        row.plan = None
        row.approval_revision = None
        status = review_state(row)
        details = {
            "analysis_id": analysis_id,
            "evidence_pack_id": pack_id,
            "recommendation": row.recommendation,
        }
    elif body.command in ("reject", "cancel"):
        if not body.text:
            reject("REASON_REQUIRED", "Enter a reason for this decision.", 422)
        status = "REJECTED" if body.command == "reject" else "CANCELLED"
        row.approval_revision = None
        row.completed_at = now()
        details = {"reason": body.text}
    elif body.command == "assign":
        actor.require_operator()
        reviewer = paypal.settings.n5_operators.get(body.reviewer) if body.reviewer else None
        if body.reviewer and (reviewer is None or reviewer.role == "viewer"):
            reject("REVIEWER_INVALID", "Choose a configured reviewer or operator.", 422)
        row.assigned_reviewer = body.reviewer
        details = {"reviewer": body.reviewer}
    elif body.command == "note":
        if not body.text:
            reject("NOTE_REQUIRED", "Enter an internal note.", 422)
        details = {"note": body.text}
    record(db, row, actor, event, status, **details)
    db.commit()

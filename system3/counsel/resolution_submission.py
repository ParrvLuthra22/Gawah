from datetime import timedelta
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .clients import IntegrationError, PayPalClient, System2Client
from .models import ResolutionAttempt, now
from .resolution import (
    ActionPlan,
    confirmed_by_upstream,
    conflicting_attempt,
    current_case,
    eligible_option,
    execute_action,
    get_dispute,
    matching_text_times,
)
from .resolution_auth import Actor
from .resolution_schemas import Revision, SubmitWorkflow
from .resolution_workflow import (
    approval_event,
    get_workflow,
    lock,
    record,
    reject,
    require_fresh,
    require_provider,
)


def approved_plan(db: Session, row) -> ActionPlan:
    approval = approval_event(db, row)
    if (
        not approval
        or approval.details.get("package") != row.plan
        or approval.details.get("analysis_id") != row.analysis_id
        or approval.details.get("evidence_pack_id") != row.evidence_pack_id
    ):
        reject("APPROVAL_REQUIRED", "Explicit approval of the current package is required.")
    return ActionPlan.model_validate(approval.details["package"])


def submit(
    db: Session,
    actor: Actor,
    paypal: PayPalClient,
    system2: System2Client,
    id: int,
    body: SubmitWorkflow,
) -> None:
    actor.require_operator()
    row = get_workflow(db, id, body.revision)
    if row.status != "APPROVED":
        reject("APPROVAL_REQUIRED", "The workflow needs approval before submission.")
    dispute = get_dispute(db, row.dispute_id)
    require_provider(dispute, paypal)
    require_fresh(db, row, dispute)
    if body.confirmation != dispute.paypal_dispute_id:
        reject("CONFIRMATION_REQUIRED", "Enter the exact PayPal dispute ID.", 422)
    plan = approved_plan(db, row)
    payment_id = row.payment_id
    db.expunge(dispute)
    db.rollback()
    case = current_case(paypal, dispute)
    option = eligible_option(system2, dispute, case, plan.option.action)
    if (
        case.status != plan.paypal_status
        or case.dispute_life_cycle_stage != plan.paypal_stage
        or case.capture_id != plan.capture_id
        or option != plan.option
    ):
        reject(
            "PAYPAL_CASE_CHANGED",
            "PayPal status, capture, or terms changed. Prepare and approve a new package.",
        )
    if matching_text_times(case, plan):
        reject(
            "RESPONSE_ALREADY_RECORDED",
            "PayPal already shows the same seller response. Review the case before continuing.",
        )

    lock(db)
    row = get_workflow(db, id, body.revision)
    require_fresh(db, row, get_dispute(db, row.dispute_id))
    if conflicting_attempt(db, row.dispute_id, option.action, case.capture_id):
        reject(
            "ALREADY_ATTEMPTED",
            "A prior submission blocks this action. Reconcile it before proceeding.",
        )
    attempt = ResolutionAttempt(
        workflow_id=id,
        dispute_id=row.dispute_id,
        capture_id=case.capture_id,
        action=option.action,
        request_id=uuid4().hex,
        status="RUNNING",
        created_at=now(),
    )
    db.add(attempt)
    db.flush()
    row.paypal_status, row.paypal_checked_at = case.status, now()
    record(
        db,
        row,
        actor,
        "SUBMISSION_STARTED",
        "SUBMITTING",
        attempt_id=attempt.id,
        request_id=attempt.request_id,
        approval_revision=row.approval_revision,
        action=option.action,
    )
    attempt_id, submitting_revision = attempt.id, row.revision
    db.commit()

    # The reservation is durable before the write. A network error cannot trigger a retry.
    response_reference = None
    details = {}
    try:
        response_reference = execute_action(paypal, system2, case.dispute_id, payment_id, plan)
        result = "SUCCEEDED"
        status = "CONFIRMED" if option.action == "refund" else "SUBMITTED"
    except (IntegrationError, ValidationError) as exc:
        rejected = isinstance(exc, IntegrationError) and exc.upstream_status in (
            400,
            401,
            403,
            404,
            422,
        )
        result = status = "FAILED" if rejected else "UNKNOWN"
        details = {
            "error_code": exc.code if isinstance(exc, IntegrationError) else "INVALID_RESPONSE",
            "upstream_status": exc.upstream_status if isinstance(exc, IntegrationError) else None,
        }
    lock(db)
    row = get_workflow(db, id, submitting_revision)
    attempt = db.get(ResolutionAttempt, attempt_id)
    attempt.status, attempt.finished_at, attempt.response_reference = (
        result,
        now(),
        response_reference,
    )
    if result == "SUCCEEDED":
        row.submitted_at = attempt.finished_at
        if status == "CONFIRMED":
            row.completed_at = attempt.finished_at
    elif result == "FAILED":
        row.approval_revision = None
    record(
        db,
        row,
        actor,
        "SUBMISSION_RESULT",
        status,
        attempt_id=attempt_id,
        response_reference=response_reference,
        **details,
    )
    db.commit()


def reconcile(
    db: Session,
    actor: Actor,
    paypal: PayPalClient,
    system2: System2Client,
    id: int,
    body: Revision,
) -> None:
    actor.require_review()
    row = get_workflow(db, id, body.revision)
    if row.status not in ("SUBMITTING", "SUBMITTED", "UNKNOWN"):
        reject("INVALID_TRANSITION", "Only submitted or uncertain attempts can be reconciled.")
    dispute = get_dispute(db, row.dispute_id)
    if not paypal.configured:
        reject(
            "PAYPAL_NOT_CONFIGURED", "PayPal Sandbox credentials are required to reconcile.", 503
        )
    plan = approved_plan(db, row)
    attempt = db.scalar(
        select(ResolutionAttempt)
        .where(ResolutionAttempt.workflow_id == id)
        .order_by(ResolutionAttempt.id.desc())
        .limit(1)
    )
    if attempt is None:
        reject("ATTEMPT_UNAVAILABLE", "No saved submission attempt was found.")
    if row.status == "SUBMITTING" and now() < attempt.created_at + timedelta(seconds=60):
        reject(
            "SUBMISSION_IN_PROGRESS",
            "Wait for the original submission to finish before reconciling.",
        )
    attempt_id = attempt.id
    db.expunge(dispute)
    db.expunge(attempt)
    db.rollback()
    case = current_case(paypal, dispute)
    confirmed = confirmed_by_upstream(system2, dispute, case, plan, attempt)

    lock(db)
    row = get_workflow(db, id, body.revision)
    attempt = db.get(ResolutionAttempt, attempt_id)
    checked = now()
    row.paypal_status, row.paypal_checked_at = case.status, checked
    status = row.status
    if confirmed:
        status = "CONFIRMED"
        attempt.status = "SUCCEEDED"
        attempt.response_reference = attempt.response_reference or case.dispute_id
        attempt.finished_at = attempt.finished_at or checked
        row.submitted_at = row.submitted_at or attempt.created_at
        row.completed_at = checked
    elif status == "SUBMITTING":
        status = "UNKNOWN"
        attempt.status, attempt.finished_at = "UNKNOWN", checked
    record(
        db,
        row,
        actor,
        "PAYPAL_RECONCILED",
        status,
        confirmed=confirmed,
        paypal_status=case.status,
        attempt_id=attempt.id,
    )
    db.commit()

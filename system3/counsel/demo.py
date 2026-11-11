"""Repeatable, non-financial demo scenarios built from real System 2 payment records."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import resolution_workflow
from .clients import IntegrationError, System2Client
from .counsel_service import analyze
from .evidence import assemble_pack
from .groq_counsel import GroqCounsel
from .models import Dispute, DisputeStatusHistory, Source, now
from .resolution_auth import Actor
from .resolution_schemas import OpenWorkflow

SCENARIOS = (("DEFEND", "UNAUTHORISED"), ("CONCEDE", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"))


@dataclass(frozen=True)
class DemoScenario:
    scenario: str
    dispute_id: int
    payment_id: str
    analysis_status: str | None
    recommendation: str | None
    workflow_id: int | None
    status: str
    detail: str | None = None


def _dispute(db: Session, payment, scenario: str, reason: str) -> Dispute:
    paypal_id = f"DEMO-{scenario}-{payment.id}"[:255]
    row = db.scalar(
        select(Dispute).where(
            Dispute.source == Source.SIMULATED_FIXTURE,
            Dispute.paypal_dispute_id == paypal_id,
        )
    )
    if row:
        return row
    created = now()
    row = Dispute(
        paypal_dispute_id=paypal_id,
        transaction_id=payment.paypal_capture_id or payment.paypal_order_id,
        payment_id=payment.id,
        link_status="LINKED",
        reason=reason,
        status="OPEN",
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        buyer=None,
        merchant=None,
        created_at=created,
        updated_at=created,
        deadline=None,
        source=Source.SIMULATED_FIXTURE,
        raw_response={
            "simulated": True,
            "demo_scenario": scenario,
            "payment_id": payment.id,
            "notice": "Demo record only. No PayPal dispute or financial action was created.",
        },
        last_synced_at=created,
    )
    db.add(row)
    db.flush()
    db.add(DisputeStatusHistory(
        dispute_id=row.id, previous_status=None, new_status="OPEN",
        source=Source.SIMULATED_FIXTURE, changed_at=created,
    ))
    db.commit()
    return row


def run(db: Session, system2: System2Client, groq: GroqCounsel) -> list[DemoScenario]:
    payments = [payment for payment in system2.list_payments() if payment.status == "PAID"]
    if len(payments) < 2:
        raise IntegrationError(
            "DEMO_PAYMENTS_REQUIRED",
            "Run two successful System 2 payments before creating demo scenarios.",
            409,
        )
    results = []
    # System 2 returns its payment ledger in creation order. Use the latest two so a just-run
    # engine demo is what appears in the System 3 Disputes view.
    for payment, (scenario, reason) in zip(payments[-2:], SCENARIOS, strict=True):
        dispute = _dispute(db, payment, scenario, reason)
        try:
            assemble_pack(db, system2, payment.id, dispute_id=dispute.id)
            analysis = analyze(db, groq, payment.id, dispute_id=dispute.id)
            workflow_id = resolution_workflow.open_workflow(
                db, Actor(id="demo-runner", role="reviewer"),
                OpenWorkflow(dispute_id=dispute.id, assign_to_self=False), allow_simulated=True,
            )
            results.append(DemoScenario(
                scenario, dispute.id, payment.id, analysis.analysis_status,
                analysis.outcome.recommendation if analysis.outcome else None,
                workflow_id, "COMPLETED",
            ))
        except IntegrationError as exc:
            results.append(DemoScenario(scenario, dispute.id, payment.id, None, None, None, "FAILED", str(exc)))
    return results

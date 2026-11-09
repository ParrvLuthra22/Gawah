from .clients import IntegrationError
from .counsel_case import CounselCase
from .counsel_schemas import (
    CaseAssessment,
    Classification,
    CounselDecision,
    CounselOutcome,
    ModelPlan,
)


def recommend(assessment: CaseAssessment, plan: ModelPlan) -> CounselDecision:
    if plan.recommendation == "REQUEST_MORE_EVIDENCE":
        return CounselDecision(recommendation=plan.recommendation, confidence=plan.confidence)
    if plan.recommendation != assessment.supported_outcome:
        conflict = (
            "The model proposed a recommendation the available verified evidence does not support."
        )
    elif any(
        "AMOUNT_COMPARISON" not in refs
        for refs in (plan.reasoning_finding_ids, plan.draft_finding_ids)
    ):
        conflict = (
            "The model did not cite the price comparison required for a decisive recommendation."
        )
    else:
        return CounselDecision(recommendation=plan.recommendation, confidence=plan.confidence)
    return CounselDecision(
        recommendation="REQUEST_MORE_EVIDENCE", confidence=None, conflict=conflict
    )


def draft(
    assessment: CaseAssessment,
    classification: Classification,
    plan: ModelPlan,
    decision: CounselDecision,
) -> CounselOutcome:
    findings = {finding.id: finding for finding in assessment.findings}
    trust = {record.id: record for record in classification.records}
    mandatory = [key for key in ("COMPLAINT", "ORDER_VARIANT_CONFLICT") if key in findings]
    reasoning = [
        findings[key]
        for key in dict.fromkeys([*mandatory, *plan.reasoning_finding_ids])
        if key in findings
    ]
    draft_findings = [
        findings[key]
        for key in dict.fromkeys([*mandatory, *plan.draft_finding_ids])
        if key in findings
    ]
    draft_ids = list(
        dict.fromkeys(ref for finding in draft_findings for ref in finding.evidence_ids)
    )
    summaries = {
        "DEFEND": "Recommend contesting the reported billing discrepancy after review.",
        "CONCEDE_REMEDY": "Recommend acknowledging the recorded price difference and reviewing a remedy.",
        "REQUEST_MORE_EVIDENCE": "Request the records needed to assess the complaint.",
    }
    replies = {
        "DEFEND": "Please review the billing claim using the signed price and capture records cited above.",
        "CONCEDE_REMEDY": "We acknowledge the recorded price difference and propose reviewing an appropriate remedy.",
        "REQUEST_MORE_EVIDENCE": assessment.documentation_needed,
    }
    draft_lines = []
    for finding in draft_findings:
        sources = {trust[ref].source_type for ref in finding.evidence_ids}
        label = ""
        if sources & {"MOCK_RESPONSE", "SIMULATED_FIXTURE"}:
            label = "Mock or simulated record. "
        elif "UNKNOWN" in sources:
            label = "Source unconfirmed. "
        draft_lines.append(f"{label}{finding.text} [{', '.join(finding.evidence_ids)}]")
    return CounselOutcome(
        **decision.model_dump(),
        executive_summary=summaries[decision.recommendation],
        reasoning=reasoning,
        supporting_evidence_ids=list(
            dict.fromkeys(
                ref for finding in reasoning for ref in finding.evidence_ids if trust[ref].reliable
            )
        ),
        draft_response="\n\n".join(
            [
                "Thank you for the opportunity to respond to this dispute.",
                *draft_lines,
                replies[decision.recommendation],
            ]
        ),
        draft_evidence_ids=draft_ids,
    )


def validate_output(
    case: CounselCase,
    assessment: CaseAssessment,
    plan: ModelPlan,
    outcome: CounselOutcome,
) -> None:
    available = {finding.id for finding in assessment.findings}
    selected = set(plan.reasoning_finding_ids) | set(plan.draft_finding_ids)
    existing = {item.id for item in case.pack.evidence}
    citations = {
        *outcome.supporting_evidence_ids,
        *outcome.draft_evidence_ids,
        *(evidence_id for finding in outcome.reasoning for evidence_id in finding.evidence_ids),
    }
    if not selected <= available or not citations <= existing:
        raise IntegrationError(
            "COUNSEL_CITATION_INVALID", "The analysis cited evidence outside the saved pack.", 502
        )

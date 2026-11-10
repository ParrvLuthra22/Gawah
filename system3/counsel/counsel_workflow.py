from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from .clients import IntegrationError
from .counsel_case import CounselCase, assess_case, classify_evidence
from .counsel_output import draft, recommend, validate_output
from .counsel_schemas import (
    AnalysisError,
    AnalysisSnapshot,
    CaseAssessment,
    Classification,
    CounselDecision,
    CounselOutcome,
    ModelPlan,
)


class CounselState(TypedDict, total=False):
    case: CounselCase
    classification: Classification
    assessment: CaseAssessment
    plan: ModelPlan
    decision: CounselDecision
    outcome: CounselOutcome | None
    error: AnalysisError
    snapshot: AnalysisSnapshot


@dataclass
class CounselRun:
    load: Callable[[], CounselCase]
    reason: Callable[[CaseAssessment, Classification], ModelPlan]


def _load(_state: CounselState, runtime: Runtime[CounselRun]):
    return {"case": runtime.context.load()}


def _classify(state: CounselState):
    return {"classification": classify_evidence(state["case"])}


def _assess(state: CounselState):
    return {"assessment": assess_case(state["case"], state["classification"])}


def _reason(state: CounselState, runtime: Runtime[CounselRun]):
    try:
        return {"plan": runtime.context.reason(state["assessment"], state["classification"])}
    except IntegrationError as exc:
        return {"error": AnalysisError(code=exc.code, message=str(exc), status=exc.status)}


def _recommend(state: CounselState):
    return {"decision": recommend(state["assessment"], state["plan"])}


def _draft(state: CounselState):
    return {
        "outcome": draft(
            state["assessment"], state["classification"], state["plan"], state["decision"]
        )
    }


def _validate_output(state: CounselState):
    try:
        validate_output(state["case"], state["assessment"], state["plan"], state["outcome"])
    except IntegrationError as exc:
        return {
            "error": AnalysisError(code=exc.code, message=str(exc), status=exc.status),
            "outcome": None,
        }
    return {}


def _prepare_persistence(state: CounselState):
    return {
        "snapshot": AnalysisSnapshot(
            dispute_id=state["case"].pack.dispute_id,
            dispute_reason=state["assessment"].dispute_reason,
            category=state["assessment"].category,
            classification=state["classification"],
            outcome=state.get("outcome"),
            error=state.get("error"),
        )
    }


workflow = (
    StateGraph(CounselState, context_schema=CounselRun)
    .add_node("load_evidence", _load)
    .add_node("validate_and_classify", _classify)
    .add_node("assess_dispute", _assess)
    .add_node("reason", _reason)
    .add_node("recommend", _recommend)
    .add_node("draft_response", _draft)
    .add_node("validate_citations", _validate_output)
    .add_node("prepare_persistence", _prepare_persistence)
    .add_edge(START, "load_evidence")
    .add_edge("load_evidence", "validate_and_classify")
    .add_edge("validate_and_classify", "assess_dispute")
    .add_edge("assess_dispute", "reason")
    .add_conditional_edges(
        "reason",
        lambda state: "prepare_persistence" if "error" in state else "recommend",
        ["prepare_persistence", "recommend"],
    )
    .add_edge("recommend", "draft_response")
    .add_edge("draft_response", "validate_citations")
    .add_edge("validate_citations", "prepare_persistence")
    .add_edge("prepare_persistence", END)
    .compile()
)

import json
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from counsel.clients import IntegrationError
from counsel.config import Settings
from counsel.counsel_case import assess_case, classify_evidence, load_case
from counsel.counsel_output import draft, recommend
from counsel.counsel_schemas import ModelPlan
from counsel.counsel_service import analyze, get_analysis, get_history
from counsel.counsel_workflow import CounselRun, workflow
from counsel.evidence_schemas import EvidenceSnapshot, Verification, VerificationSummary
from counsel.groq_counsel import GROQ_BASE_URL, GroqCounsel
from counsel.main import create_app
from counsel.models import Base, Dispute, EvidencePack, now
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

PAYMENT_ID = "pay_123"
NOT_CHECKED = Verification(status="UNVERIFIED", detail="No check reported.")


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def save_dispute(
    db,
    *,
    reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    case_id="PP-D-CASE",
    source="PAYPAL_API",
):
    row = Dispute(
        paypal_dispute_id=case_id,
        payment_id=PAYMENT_ID,
        link_status="LINKED",
        reason=reason,
        status="OPEN",
        transaction_id="CAPTURE1",
        source=source,
        raw_response={},
        last_synced_at=now(),
    )
    db.add(row)
    db.commit()
    return row


def save_pack(db, dispute=None, *, version=1, **fields):
    snapshot = EvidenceSnapshot(
        **{
            "dispute_id": dispute.paypal_dispute_id if dispute else None,
            "dispute_record_id": dispute.id if dispute else None,
            "payment_id": PAYMENT_ID,
            "evidence": [],
            "timeline": [],
            "verification_summary": VerificationSummary(
                mandate_signature=NOT_CHECKED,
                cart_signature=NOT_CHECKED,
                ledger=NOT_CHECKED,
                engine_ledger=NOT_CHECKED,
            ),
            "sources": [],
            "missing_evidence": [],
            "invalid_evidence": [],
            "contradictions": [],
            "completeness": {"available": 0, "missing": 0, "verified": 0, "unverified": 0},
            **fields,
        },
    )
    row = EvidencePack(
        payment_id=PAYMENT_ID,
        dispute_id=dispute.id if dispute else None,
        version=version,
        fingerprint=f"version-{version}",
        generated_at=now(),
        snapshot=snapshot.model_dump(mode="json"),
    )
    db.add(row)
    db.commit()
    return row


def save_billing_pack(db, dispute, records, *, verification_summary=None, **fields):
    return save_pack(
        db,
        dispute,
        evidence=records,
        verification_summary=verification_summary or VerificationSummary(
            mandate_signature=VERIFIED,
            cart_signature=VERIFIED,
            payment_reference_match=VERIFIED,
            ledger=VERIFIED,
            engine_ledger=VERIFIED,
        ),
        **fields,
    )


def test_load_case_uses_latest_persisted_pack_and_linked_dispute(db):
    dispute = save_dispute(db)
    save_pack(db, dispute)
    newest = save_pack(db, dispute, version=2)
    case = load_case(db, PAYMENT_ID)
    assert case.pack.id == newest.id and case.pack.version == 2
    assert case.dispute and case.dispute.paypal_dispute_id == dispute.paypal_dispute_id


def test_load_case_allows_payment_only_and_rejects_missing_pack(db):
    with pytest.raises(IntegrationError) as error:
        load_case(db, PAYMENT_ID)
    assert error.value.code == "EVIDENCE_PACK_NOT_FOUND"
    row = save_pack(db)
    case = load_case(db, PAYMENT_ID)
    assert case.pack.id == row.id and case.dispute is None


def test_load_case_requires_scope_for_multiple_disputes(db):
    first = save_dispute(db)
    second = save_dispute(db, case_id="PP-D-SECOND")
    first_pack = save_pack(db, first)
    save_pack(db, second, version=2)
    with pytest.raises(IntegrationError) as error:
        load_case(db, PAYMENT_ID)
    assert error.value.code == "DISPUTE_REQUIRED"
    assert load_case(db, PAYMENT_ID, first.id).pack.id == first_pack.id


VERIFIED = Verification(status="VERIFIED", detail="Check passed.", result={"ok": True})


def item(
    kind,
    record_id,
    *,
    dispute=None,
    status="VERIFIED",
    origin="REAL_RECORD",
    check=VERIFIED,
    **payload,
):
    return {
        "id": f"{kind}:{record_id}",
        "type": kind,
        "source_system": "SYSTEM_2",
        "source_type": origin,
        "source_record_id": str(record_id),
        "source_reference": f"/evidence/{record_id}",
        "dispute_id": dispute.paypal_dispute_id if dispute else None,
        "payment_id": PAYMENT_ID,
        "timestamp": None,
        "retrieved_at": now(),
        "verification_status": status,
        "verification": check,
        "payload": payload,
    }


def test_classification_preserves_provenance_and_distinguishes_gaps(db):
    save_pack(
        db,
        evidence=[
            item("mandate", 1),
            item("mandate", 2, origin="MOCK_RESPONSE"),
            item("mandate", 3, origin="SIMULATED_FIXTURE"),
            item("mandate", 4, origin="UNKNOWN"),
            item("payment", 5, status="VERIFIED", check=None),
            item("payment", 6, status="UNAVAILABLE", check=None),
            item("payment", 7, status="INVALID", check=None),
        ],
        sources=[
            {"source_system": "SYSTEM_2", "reference": "/missing", "status": "NOT_FOUND"},
            {"source_system": "SYSTEM_2", "reference": "/offline", "status": "UNAVAILABLE"},
        ],
        missing_evidence=[
            {
                "code": "MISSING_DELIVERY",
                "type": "MISSING",
                "severity": "WARNING",
                "explanation": "No delivery record.",
                "evidence_ids": [],
                "source_references": ["/missing"],
            }
        ],
        contradictions=[
            {
                "code": "RECORD_CONFLICT",
                "type": "CONTRADICTION",
                "severity": "HIGH",
                "explanation": "A source disagrees.",
                "evidence_ids": ["payment:7"],
                "source_references": [],
            }
        ],
    )
    classification = classify_evidence(load_case(db, PAYMENT_ID))
    assert [record.id for record in classification.records if record.reliable] == ["mandate:1"]
    assert classification.invalid_evidence_ids == ["payment:7"]
    assert classification.unverified_evidence_ids == ["payment:5"]
    assert classification.unavailable_evidence_ids == ["payment:6"]
    assert [source.reference for source in classification.unavailable_sources] == ["/offline"]
    assert classification.missing_evidence[0].code == "MISSING_DELIVERY"
    assert classification.contradictory_evidence[0].evidence_ids == ["payment:7"]
    assert "payment:5 has no matching verified result." in classification.verification_warnings


def test_classification_rejects_bad_evidence_references(db):
    save_pack(db, evidence=[item("payment", 1), item("payment", 1)])
    with pytest.raises(IntegrationError) as error:
        classify_evidence(load_case(db, PAYMENT_ID))
    assert error.value.code == "COUNSEL_EVIDENCE_INVALID"


def billing_evidence(dispute, charged=6500, *, cart_check=VERIFIED):
    return [
        item(
            "dispute",
            dispute.paypal_dispute_id,
            dispute=dispute,
            origin="SIMULATED_FIXTURE" if dispute.source == "SIMULATED_FIXTURE" else "REAL_RECORD",
            reason=dispute.reason,
            transaction_id="CAPTURE1",
            status="UNVERIFIED",
            check=None,
        ),
        item("mandate", "man_1", dispute=dispute),
        item(
            "proposal",
            "prop_1",
            dispute=dispute,
            id="prop_1",
            mandate_id="man_1",
            merchant_id="shop",
            quantity=1,
            unit_price_minor=6500,
            currency="USD",
            status="UNVERIFIED",
            check=None,
        ),
        item(
            "signed_cart",
            "cart_1",
            dispute=dispute,
            total_minor=6500,
            currency="USD",
            merchant_id="shop",
            status=cart_check.status,
            check=cart_check,
        ),
        item(
            "payment",
            PAYMENT_ID,
            dispute=dispute,
            status="VERIFIED",
            amount_minor=charged,
            paypal_capture_id="CAPTURE1",
            proposal_id="prop_1",
            decision_id="dec_1",
            currency="USD",
        ),
        item(
            "ledger_entry",
            "4",
            dispute=dispute,
            type="DECISION_MADE",
            subject_id="dec_1",
            data={
                "proposal_id": "prop_1",
                "mandate_id": "man_1",
                "verdict": "ALLOW",
                "amount_minor": 6500,
                "currency": "USD",
            },
        ),
        item(
            "decision",
            "dec_1",
            dispute=dispute,
            id="dec_1",
            verdict="ALLOW",
            mandate_id="man_1",
            proposal_id="prop_1",
            status="UNVERIFIED",
            check=None,
        ),
        item(
            "ledger_entry",
            "5",
            dispute=dispute,
            type="PAYMENT_COMPLETED",
            subject_id=PAYMENT_ID,
            data={
                "proposal_id": "prop_1",
                "paypal_capture_id": "CAPTURE1",
                "amount_minor": charged,
                "currency": "USD",
            },
        ),
    ]


@pytest.mark.parametrize(
    ("reason", "charged", "outcome", "document"),
    [
        ("INCORRECT_AMOUNT", 6500, "DEFEND", "capture records"),
        ("INCORRECT_AMOUNT", 7000, "CONCEDE_REMEDY", "capture records"),
        (
            "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
            6500,
            "REQUEST_MORE_EVIDENCE",
            "delivery confirmation",
        ),
        ("DUPLICATE_TRANSACTION", 6500, "REQUEST_MORE_EVIDENCE", "both alleged charges"),
        ("UNAUTHORISED", 6500, "DEFEND", "payer consent"),
    ],
)
def test_assessment_requires_evidence_relevant_to_the_complaint(
    db, reason, charged, outcome, document
):
    dispute = save_dispute(db, reason=reason)
    evidence = billing_evidence(dispute, charged)
    evidence[4]["payload"]["status"] = "PAID"
    save_billing_pack(db, dispute, evidence)
    case = load_case(db, PAYMENT_ID)
    result = assess_case(case, classify_evidence(case))
    assert result.supported_outcome == outcome
    assert document in result.documentation_needed
    assert any(finding.id == "AMOUNT_COMPARISON" for finding in result.findings) == (
        reason == "INCORRECT_AMOUNT"
    )
    if reason == "UNAUTHORISED":
        mandate_check = next(
            finding for finding in result.findings if finding.id == "MANDATE_CHECK"
        )
        assert mandate_check.evidence_ids == ["mandate:man_1"]
        assert "Payer consent still needs separate evidence" in mandate_check.text


def test_verified_unauthorised_case_defends_from_guardian_consent(db):
    dispute = save_dispute(db, reason="UNAUTHORISED")
    evidence = billing_evidence(dispute)
    evidence[4]["payload"]["status"] = "PAID"
    save_billing_pack(db, dispute, evidence)
    case = load_case(db, PAYMENT_ID)
    result = assess_case(case, classify_evidence(case))
    consent = next(finding for finding in result.findings if finding.id == "AUTHORIZATION_CONSENT")
    assert result.supported_outcome == "DEFEND"
    assert consent.evidence_ids == ["mandate:man_1", "decision:dec_1", f"payment:{PAYMENT_ID}"]


def test_unverified_primary_check_cannot_defend_unauthorised_case(db):
    dispute = save_dispute(db, reason="UNAUTHORISED")
    evidence = billing_evidence(dispute)
    evidence[4]["payload"]["status"] = "PAID"
    summary = VerificationSummary(
        mandate_signature=VERIFIED,
        cart_signature=Verification(status="UNVERIFIED", detail="Not checked."),
        payment_reference_match=VERIFIED,
        ledger=VERIFIED,
        engine_ledger=VERIFIED,
    )
    save_billing_pack(db, dispute, evidence, verification_summary=summary)
    case = load_case(db, PAYMENT_ID)
    assert assess_case(case, classify_evidence(case)).supported_outcome == "REQUEST_MORE_EVIDENCE"


def test_blocked_guardian_decision_cannot_defend_unauthorised_case(db):
    dispute = save_dispute(db, reason="UNAUTHORISED")
    evidence = billing_evidence(dispute)
    evidence[4]["payload"]["status"] = "PAID"
    evidence[5]["payload"]["data"]["verdict"] = "BLOCK"
    evidence[6]["payload"]["verdict"] = "BLOCK"
    save_billing_pack(db, dispute, evidence)
    case = load_case(db, PAYMENT_ID)
    assert assess_case(case, classify_evidence(case)).supported_outcome == "REQUEST_MORE_EVIDENCE"


def test_unpaid_payment_cannot_defend_unauthorised_case(db):
    dispute = save_dispute(db, reason="UNAUTHORISED")
    evidence = billing_evidence(dispute)
    evidence[4]["payload"]["status"] = "PENDING"
    save_billing_pack(db, dispute, evidence)
    case = load_case(db, PAYMENT_ID)
    assert assess_case(case, classify_evidence(case)).supported_outcome == "REQUEST_MORE_EVIDENCE"


def test_verified_size_advisory_concedes_remedy(db):
    dispute = save_dispute(db, reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED")
    evidence = billing_evidence(dispute)
    evidence[4]["payload"]["status"] = "PAID"
    save_billing_pack(
        db,
        dispute,
        evidence,
        contradictions=[{
            "code": "AUTHORIZED_SIZE_MISMATCH",
            "type": "CONTRADICTION",
            "severity": "HIGH",
            "explanation": "The signed cart differs from the authorized size.",
            "evidence_ids": ["mandate:man_1", "signed_cart:cart_1"],
            "source_references": ["/v1/evidence/pay_01"],
        }],
    )
    case = load_case(db, PAYMENT_ID)
    result = assess_case(case, classify_evidence(case))
    advisory = next(finding for finding in result.findings if finding.id == "AUTHORIZED_SIZE_MISMATCH")
    assert result.supported_outcome == "CONCEDE_REMEDY"
    assert advisory.evidence_ids == ["mandate:man_1", "signed_cart:cart_1"]


def test_missing_size_advisory_stays_request_more_evidence(db):
    dispute = save_dispute(db, reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED")
    evidence = billing_evidence(dispute)
    evidence[4]["payload"]["status"] = "PAID"
    save_billing_pack(db, dispute, evidence)
    case = load_case(db, PAYMENT_ID)
    assert assess_case(case, classify_evidence(case)).supported_outcome == "REQUEST_MORE_EVIDENCE"


def test_simulated_authorisation_defense_is_clearly_labelled(db):
    dispute = save_dispute(db, reason="UNAUTHORISED", source="SIMULATED_FIXTURE")
    evidence = billing_evidence(dispute)
    evidence[4]["payload"]["status"] = "PAID"
    save_billing_pack(db, dispute, evidence)
    case = load_case(db, PAYMENT_ID)
    classification = classify_evidence(case)
    assessment = assess_case(case, classification)
    plan = ModelPlan(
        recommendation="REQUEST_MORE_EVIDENCE",
        confidence=0.9,
        reasoning_finding_ids=[],
        draft_finding_ids=[],
    )
    outcome = draft(assessment, classification, plan, recommend(assessment, plan))
    assert outcome.recommendation == "DEFEND"
    assert outcome.executive_summary.startswith("SIMULATED CASE")
    assert "SIMULATED CASE" in outcome.draft_response


def test_invalid_cart_cannot_support_billing_outcome(db):
    dispute = save_dispute(db, reason="INCORRECT_AMOUNT")
    evidence = billing_evidence(
        dispute, cart_check=Verification(status="INVALID", detail="Failed.")
    )
    evidence[4]["payload"]["status"] = "PAID"
    save_billing_pack(db, dispute, evidence)
    case = load_case(db, PAYMENT_ID)
    assert assess_case(case, classify_evidence(case)).supported_outcome == "REQUEST_MORE_EVIDENCE"


def test_graph_loads_and_classifies_before_reasoning(db):
    dispute = save_dispute(db)
    saved = save_pack(db, dispute, evidence=[item("payment", PAYMENT_ID, dispute=dispute)])
    calls = []

    def reason(assessment, classification):
        calls.append(assessment.category)
        assert classification.records[0].reliable
        return ModelPlan(
            recommendation=assessment.supported_outcome,
            confidence=None,
            reasoning_finding_ids=[],
            draft_finding_ids=[],
        )

    result = workflow.invoke(
        {}, context=CounselRun(load=lambda: load_case(db, PAYMENT_ID), reason=reason)
    )
    assert calls == ["Non-receipt"]
    assert result["case"].pack.id == saved.id
    assert result["plan"].recommendation == "REQUEST_MORE_EVIDENCE"


def provider_response(plan):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(plan)}}]}


def test_groq_uses_structured_output_and_minimizes_case_data(db):
    dispute = save_dispute(db, reason="INCORRECT_AMOUNT")
    records = billing_evidence(dispute)
    records[4]["payload"]["status"] = "PAID"
    records[4]["payload"]["private"] = "do-not-send"
    save_billing_pack(db, dispute, records)
    case = load_case(db, PAYMENT_ID)
    classification = classify_evidence(case)
    assessment = assess_case(case, classification)

    def handle(request):
        assert request.url.path == "/openai/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        data = json.loads(request.content)
        assert data["model"] == "configured-model"
        assert data["response_format"]["json_schema"]["strict"]
        assert "do-not-send" not in data["messages"][1]["content"]
        assert "CAPTURE1" not in data["messages"][1]["content"]
        assert "Guardian ALLOW" in data["messages"][0]["content"]
        assert "AUTHORIZED_SIZE_MISMATCH" in data["messages"][0]["content"]
        return httpx.Response(
            200,
            json=provider_response(
                {
                    "recommendation": "DEFEND",
                    "confidence": 0.8,
                    "reasoning_finding_ids": ["AMOUNT_COMPARISON"],
                    "draft_finding_ids": ["AMOUNT_COMPARISON"],
                }
            ),
        )

    settings = Settings(_env_file=None, groq_api_key="test-key", groq_model="configured-model")
    with httpx.Client(base_url=GROQ_BASE_URL, transport=httpx.MockTransport(handle)) as http:
        assert (
            GroqCounsel(settings, http).reason(assessment, classification).recommendation
            == "DEFEND"
        )


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (429, "COUNSEL_RATE_LIMITED"),
        (401, "COUNSEL_AUTH_FAILED"),
        ("timeout", "UPSTREAM_TIMEOUT"),
        ("invalid", "COUNSEL_MODEL_INVALID"),
    ],
)
def test_groq_failure_returns_an_error_without_an_outcome(db, failure, code):
    save_pack(db)
    case = load_case(db, PAYMENT_ID)
    classification = classify_evidence(case)

    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private detail", request=request)
        if failure == "invalid":
            return httpx.Response(200, json=provider_response({"recommendation": "UNKNOWN"}))
        return httpx.Response(failure)

    settings = Settings(_env_file=None, groq_api_key="test-key")
    with (
        httpx.Client(base_url=GROQ_BASE_URL, transport=httpx.MockTransport(handle)) as http,
        pytest.raises(IntegrationError) as error,
    ):
        GroqCounsel(settings, http).reason(assess_case(case, classification), classification)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("charged", "action", "reply"),
    [
        (6500, "DEFEND", "Please review the billing claim"),
        (7000, "CONCEDE_REMEDY", "We acknowledge the recorded price difference"),
    ],
)
def test_billing_drafts_cite_the_supported_price_comparison(db, charged, action, reply):
    dispute = save_dispute(db, reason="INCORRECT_AMOUNT")
    evidence = billing_evidence(dispute, charged)
    evidence[4]["payload"]["status"] = "PAID"
    save_billing_pack(db, dispute, evidence)
    case = load_case(db, PAYMENT_ID)
    classification = classify_evidence(case)
    assessment = assess_case(case, classification)
    plan = ModelPlan(
        recommendation=action,
        confidence=0.8,
        reasoning_finding_ids=["AMOUNT_COMPARISON"],
        draft_finding_ids=["AMOUNT_COMPARISON"],
    )
    outcome = draft(assessment, classification, plan, recommend(assessment, plan))
    assert outcome.recommendation == action and outcome.human_review_required
    assert reply in outcome.draft_response
    assert (
        "ledger_entry:5" in outcome.draft_response
        and "signed_cart:cart_1" in outcome.draft_evidence_ids
    )


def test_unsupported_model_recommendation_requires_review_and_more_evidence(db):
    dispute = save_dispute(db)
    save_pack(
        db,
        dispute,
        evidence=[
            item(
                "dispute",
                dispute.paypal_dispute_id,
                dispute=dispute,
                reason=dispute.reason,
                origin="SIMULATED_FIXTURE",
                status="UNVERIFIED",
                check=None,
            ),
        ],
    )
    case = load_case(db, PAYMENT_ID)
    classification = classify_evidence(case)
    assessment = assess_case(case, classification)
    plan = ModelPlan(
        recommendation="DEFEND", confidence=1.0, reasoning_finding_ids=[], draft_finding_ids=[]
    )
    outcome = draft(assessment, classification, plan, recommend(assessment, plan))
    assert outcome.recommendation == "REQUEST_MORE_EVIDENCE"
    assert outcome.conflict and outcome.confidence is None and outcome.human_review_required
    assert "Mock or simulated record" in outcome.draft_response
    assert "delivery confirmation" in outcome.draft_response


def test_graph_rejects_unknown_model_citations(db):
    save_pack(db)
    plan = ModelPlan(
        recommendation="REQUEST_MORE_EVIDENCE",
        confidence=None,
        reasoning_finding_ids=["INVENTED"],
        draft_finding_ids=[],
    )
    state = workflow.invoke(
        {}, context=CounselRun(load=lambda: load_case(db, PAYMENT_ID), reason=lambda *_: plan)
    )
    assert state["snapshot"].error.code == "COUNSEL_CITATION_INVALID"
    assert state["snapshot"].outcome is None


def test_a_changed_dispute_requires_an_updated_pack(db):
    dispute = save_dispute(db)
    save_pack(
        db,
        dispute,
        evidence=[
            item("dispute", dispute.paypal_dispute_id, dispute=dispute, reason=dispute.reason),
        ],
    )
    dispute.reason = "INCORRECT_AMOUNT"
    db.commit()
    with pytest.raises(IntegrationError) as error:
        load_case(db, PAYMENT_ID)
    assert error.value.code == "COUNSEL_EVIDENCE_STALE"


class ReasonStub:
    model = "mock-model"
    calls = 0
    failure = None

    def reason(self, *_):
        self.calls += 1
        if self.failure:
            raise self.failure
        return ModelPlan(
            recommendation="REQUEST_MORE_EVIDENCE",
            confidence=None,
            reasoning_finding_ids=[],
            draft_finding_ids=[],
        )


def test_analysis_history_is_append_only_and_reuses_the_same_pack(db):
    first_pack = save_pack(db)
    model = ReasonStub()
    first = analyze(db, model, PAYMENT_ID)
    assert first.evidence_pack_id == first_pack.id and first.analysis_status == "COMPLETED"
    assert analyze(db, model, PAYMENT_ID) == first and model.calls == 1
    forced = analyze(db, model, PAYMENT_ID, force=True)
    assert forced.id != first.id and forced.evidence_pack_version == first.evidence_pack_version
    second_pack = save_pack(db, version=2)
    newest = analyze(db, model, PAYMENT_ID)
    assert newest.evidence_pack_id == second_pack.id and model.calls == 3
    assert get_analysis(db, PAYMENT_ID) == newest
    history = get_history(db, PAYMENT_ID, None, 1, 2)
    assert history.total == 3 and [row.id for row in history.items] == [newest.id, forced.id]
    assert get_history(db, PAYMENT_ID, None, 2, 2).items == [first]


def test_groq_failure_is_persisted_without_an_outcome_and_can_retry(db):
    save_pack(db)
    model = ReasonStub()
    model.failure = IntegrationError("UPSTREAM_TIMEOUT", "Groq timed out.", 504)
    failure = analyze(db, model, PAYMENT_ID)
    assert failure.analysis_status == "FAILED" and failure.outcome is None
    assert failure.error.code == "UPSTREAM_TIMEOUT"
    model.failure = None
    retry = analyze(db, model, PAYMENT_ID)
    assert retry.analysis_status == "COMPLETED" and model.calls == 2
    assert get_history(db, PAYMENT_ID, None, 1, 10).items == [retry, failure]


def test_counsel_migration_adds_analysis_references(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'counsel.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "0002_evidence_packs")
    command.upgrade(config, "head")
    engine = create_engine(url)
    assert {
        fk["referred_table"] for fk in inspect(engine).get_foreign_keys("counsel_analyses")
    } == {
        "disputes",
        "evidence_packs",
    }
    engine.dispose()


def test_counsel_api_authorization_history_and_retry(tmp_path):
    url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    settings = Settings(
        _env_file=None,
        database_url=url,
        system3_key="test-service-key",
        groq_api_key="test-groq",
    )
    engine = settings.database_engine()
    Base.metadata.create_all(engine)
    headers = {"X-Gawah-Key": "test-service-key"}
    path = f"/api/counsel/{PAYMENT_ID}"
    responses = iter(
        [
            httpx.Response(429),
            httpx.Response(
                200,
                json=provider_response(
                    {
                        "recommendation": "REQUEST_MORE_EVIDENCE",
                        "confidence": None,
                        "reasoning_finding_ids": ["COMPLAINT"],
                        "draft_finding_ids": [],
                    }
                ),
            ),
        ]
    )

    def model(request):
        assert request.headers["Authorization"] == "Bearer test-groq"
        return next(responses)

    app = create_app(settings, groq_transport=httpx.MockTransport(model))
    with TestClient(app) as client, Session(engine, expire_on_commit=False) as db:
        assert client.post(f"{path}/analyze").status_code == 401
        assert client.post(f"{path}/analyze", headers=headers).status_code == 404
        dispute = save_dispute(db)
        pack = save_pack(
            db,
            dispute,
            evidence=[
                item("dispute", dispute.paypal_dispute_id, dispute=dispute, reason=dispute.reason),
            ],
        )
        scope = f"?dispute_id={dispute.id}"
        assert client.get(path + scope, headers=headers).status_code == 404
        failed = client.post(f"{path}/analyze{scope}", headers=headers)
        assert (
            failed.status_code == 503 and failed.json()["error"]["code"] == "COUNSEL_RATE_LIMITED"
        )
        assert client.get(path + scope, headers=headers).json()["analysis_status"] == "FAILED"
        ok = client.post(f"{path}/analyze{scope}", headers=headers)
        assert ok.status_code == 200, ok.text
        result = ok.json()
        assert result["evidence_pack_id"] == pack.id
        assert result["outcome"]["reasoning"][0]["evidence_ids"] == ["dispute:PP-D-CASE"]
        assert result["human_review_required"]
        assert client.post(f"{path}/analyze{scope}", headers=headers).json() == result
        history = client.get(f"{path}/history{scope}&page_size=1", headers=headers).json()
        assert history["total"] == 2 and history["items"] == [result]
        assert client.get(f"{path}/history?page_size=0", headers=headers).status_code == 422
    engine.dispose()

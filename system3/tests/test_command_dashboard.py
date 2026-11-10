from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from counsel.config import Settings
from counsel.main import create_app
from counsel.models import (
    CounselAnalysis,
    Dispute,
    DisputeStatusHistory,
    DisputeSyncLog,
    EvidencePack,
    ResolutionAttempt,
    Source,
)
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

KEY = {"X-Gawah-Key": "dashboard-key"}
BASE = "/api/dashboard"
PERIOD = "?start=2026-11-03&end=2026-11-10"


def at(day, hour=0, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def dispute(
    ref, payment, opened, status, *, source=Source.PAYPAL_API, amount=6500, currency="USD", raw=None
):
    return Dispute(
        paypal_dispute_id=ref,
        payment_id=payment,
        source=source,
        link_status="LINKED" if payment else "UNMATCHED",
        status=status,
        reason="SIZE" if status == "RESOLVED" else "UNAUTHORISED",
        amount_minor=amount,
        currency=currency,
        created_at=opened,
        updated_at=opened,
        raw_response=raw or {},
        last_synced_at=at(10),
    )


def pack(payment, id, version, day, *, missing=False, status="UNAVAILABLE"):
    return EvidencePack(
        payment_id=payment,
        dispute_id=id,
        version=version,
        generated_at=at(day),
        fingerprint=str(version),
        snapshot={
            "missing_evidence": [{"code": "GAP"}] if missing else [],
            "invalid_evidence": [],
            "contradictions": [],
            "verification_summary": {"ledger": {"status": status}},
        },
    )


def analysis(evidence, day, status="COMPLETED", category="REQUEST_MORE_EVIDENCE"):
    outcome = (
        None if status == "FAILED" else {"recommendation": category, "draft_response": "Review."}
    )
    return CounselAnalysis(
        payment_id=evidence.payment_id,
        dispute_id=evidence.dispute_id,
        evidence_pack_id=evidence.id,
        evidence_pack_version=evidence.version,
        analysis_status=status,
        review_status="PENDING",
        model="test",
        created_at=at(day),
        snapshot={"outcome": outcome},
    )


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'command.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    settings = Settings(_env_file=None, database_url=url, system3_key=KEY["X-Gawah-Key"])
    engine = settings.database_engine()

    def blocked(_):
        pytest.fail("The dashboard must use saved records without calling upstream services.")

    upstream = httpx.MockTransport(blocked)
    with TestClient(
        create_app(
            settings,
            paypal_transport=upstream,
            system2_transport=upstream,
            groq_transport=upstream,
        )
    ) as client:
        yield client, engine
    engine.dispose()


def seed(engine):
    with Session(engine) as db:
        first = dispute("PP-D-A", "pay_01", at(3), "WAITING_FOR_SELLER_RESPONSE")
        second = dispute(
            "PP-D-B",
            "pay_01",
            at(7),
            "RESOLVED",
            currency="EUR",
            raw={"dispute_outcome": {"outcome_code": "RESOLVED_BUYER_FAVOUR"}},
        )
        simulated = dispute(
            "PP-D-SIM",
            None,
            at(10, 23, 59),
            "OPEN",
            source=Source.SIMULATED_FIXTURE,
            amount=None,
            currency=None,
            raw={"payment_id": "pay_04"},
        )
        older = dispute("PP-D-OLDER", None, at(2, 23, 59), "OPEN")
        undated = dispute("PP-D-UNDATED", None, None, "RESOLVED")
        db.add_all([first, second, simulated, older, undated])
        db.flush()
        db.add_all(
            [
                DisputeStatusHistory(
                    dispute_id=first.id,
                    new_status=first.status,
                    source=first.source,
                    changed_at=at(3),
                ),
                DisputeStatusHistory(
                    dispute_id=second.id,
                    previous_status="OPEN",
                    new_status="RESOLVED",
                    source=second.source,
                    changed_at=at(8),
                ),
                DisputeStatusHistory(
                    dispute_id=undated.id,
                    new_status="RESOLVED",
                    source=undated.source,
                    changed_at=at(8),
                ),
                DisputeSyncLog(
                    started_at=at(10),
                    finished_at=at(10),
                    result="PARTIAL",
                    retrieved=4,
                    created=3,
                    updated=1,
                    unmatched=2,
                    errors=["Private error detail"],
                ),
                ResolutionAttempt(
                    dispute_id=second.id,
                    action="accept-claim",
                    status="SUCCEEDED",
                    created_at=at(9),
                ),
            ]
        )
        old = pack("pay_01", first.id, 1, 3, status="VERIFIED")
        current = pack("pay_01", first.id, 2, 4, missing=True, status="INVALID")
        other = pack("pay_01", second.id, 3, 8, status="VERIFIED")
        fixture = pack("pay_04", simulated.id, 1, 10)
        standalone = pack("pay_99", None, 1, 10)
        db.add_all([old, current, other, fixture, standalone])
        db.flush()
        db.add_all(
            [
                analysis(old, 3),
                analysis(old, 4),
                analysis(other, 8, category="DEFEND"),
                analysis(other, 9, status="FAILED"),
                analysis(standalone, 10),
            ]
        )
        db.commit()


def get(client, name, query=""):
    response = client.get(f"{BASE}/{name}{query}", headers=KEY)
    assert response.status_code == 200, response.json()
    return response.json()


def test_empty_and_protected_endpoints(dashboard):
    client, _ = dashboard
    for name in ("overview", "cases", "evidence", "counsel", "activity"):
        assert client.get(f"{BASE}/{name}").status_code == 401
        assert client.get(f"{BASE}/{name}", headers={"X-Gawah-Key": "wrong"}).status_code == 401
        assert client.get(f"{BASE}/{name}?start=2026-11-10", headers=KEY).status_code == 422
    assert (
        client.get(f"{BASE}/overview?start=2026-11-10&end=2026-11-03", headers=KEY).status_code
        == 422
    )
    assert client.get(f"{BASE}/cases?page_size=101", headers=KEY).status_code == 422
    assert client.get(f"{BASE}/overview?days=91", headers=KEY).status_code == 422
    assert client.get(f"{BASE}/overview{PERIOD}&days=7", headers=KEY).status_code == 422
    overview = get(client, "overview")
    assert overview["total"] == 0 and overview["resolution_rate"] is None
    assert overview["amounts"] == [] and overview["average_observed_resolution_hours"] is None
    assert get(client, "cases")["items"] == []
    assert get(client, "evidence")["verification_rate"] is None
    counsel = get(client, "counsel")
    assert counsel["analyses"] == 0 and counsel["accepted"] is None and counsel["rejected"] is None
    assert get(client, "activity") == {"items": [], "unavailable": []}


def test_overview_counts_opening_cohort_observed_resolution_and_currency(dashboard, monkeypatch):
    client, engine = dashboard
    seed(engine)
    overview = get(client, "overview", PERIOD)
    assert (
        overview["total"],
        overview["open"],
        overview["resolved"],
        overview["awaiting_merchant"],
    ) == (3, 2, 1, 1)
    assert overview["amounts"] == [
        {"currency": code, "amount_minor": 6500} for code in ("EUR", "USD")
    ]
    assert overview["unknown_amounts"] == 1
    assert overview["resolution_rate"] == pytest.approx(1 / 3)
    assert overview["average_observed_resolution_hours"] == 24
    assert overview["resolution_samples"] == 1
    assert overview["by_outcome"] == [{"label": "RESOLVED_BUYER_FAVOUR", "count": 1}]
    assert overview["daily"][-1] == {"day": "2026-11-10", "count": 1, "amounts": []}
    assert overview["latest_sync"]["result"] == "PARTIAL"
    assert "Private error detail" not in str(overview)
    assert get(client, "overview")["unknown_open_dates"] == 1
    assert get(client, "overview", PERIOD + "&source=PAYPAL_API")["total"] == 2
    monkeypatch.setattr("counsel.command_dashboard.now", lambda: at(10))
    assert get(client, "overview", "?days=7")["total"] == 2
    assert get(client, "cases", "?days=1")["items"][0]["paypal_dispute_id"] == "PP-D-SIM"


def test_cases_filter_sort_paginate_and_keep_workflows_scoped(dashboard):
    client, engine = dashboard
    seed(engine)
    query = PERIOD + "&search=PAY_01&sort=paypal_dispute_id&order=asc&page_size=1"
    first = get(client, "cases", query)
    assert first["total"] == 2
    [row] = first["items"]
    assert (row["paypal_dispute_id"], row["evidence_version"], row["counsel_version"]) == (
        "PP-D-A",
        2,
        1,
    )
    assert row["evidence_status"] == "SAVED_WITH_GAPS" and row["counsel_status"] == "STALE"
    [row] = get(client, "cases", query + "&page=2")["items"]
    assert (row["paypal_dispute_id"], row["evidence_version"], row["counsel_status"]) == (
        "PP-D-B",
        3,
        "FAILED",
    )
    [row] = get(client, "cases", PERIOD + "&search=pay_04&status=OPEN&reason=UNAUTHORISED")["items"]
    assert row["source"] == "SIMULATED_FIXTURE" and row["link_status"] == "UNMATCHED"
    assert row["evidence_status"] == "SAVED" and row["counsel_status"] == "NOT_ANALYZED"
    [row] = get(client, "cases", "?search=PP-D-OLDER")["items"]
    assert row["payment_id"] is None and row["evidence_status"] == "NOT_ASSEMBLED"
    assert (
        get(client, "cases", PERIOD + "&search=pay_01&sort=evidence_status&order=asc")["items"][0][
            "paypal_dispute_id"
        ]
        == "PP-D-B"
    )


def test_evidence_counsel_and_activity_from_stored_snapshots(dashboard):
    client, engine = dashboard
    seed(engine)
    evidence = get(client, "evidence", PERIOD)
    assert (evidence["packs_generated"], evidence["latest_packs"], evidence["packs_with_gaps"]) == (
        5,
        4,
        1,
    )
    assert evidence["missing_records"] == 1 and evidence["verification_rate"] == 0.5
    assert {item["label"]: item["count"] for item in evidence["verification"]} == {
        "VERIFIED": 1,
        "INVALID": 1,
        "UNAVAILABLE": 14,
    }
    assert get(client, "evidence", PERIOD + "&source=PAYPAL_API")["packs_generated"] == 3
    counsel = get(client, "counsel", PERIOD)
    assert (
        counsel["analyses"],
        counsel["recommendations"],
        counsel["drafts"],
        counsel["pending_reviews"],
    ) == (5, 4, 4, 3)
    assert {item["label"]: item["count"] for item in counsel["by_status"]} == {
        "COMPLETED": 4,
        "FAILED": 1,
    }
    assert counsel["recent"][0]["dispute_id"] is None
    feed = get(client, "activity", PERIOD)
    assert not feed["unavailable"]
    types = {item["type"] for item in feed["items"]}
    assert {
        "PAYPAL_DISPUTE_RECEIVED",
        "EVIDENCE_PACK_GENERATED",
        "EVIDENCE_GAPS_RECORDED",
        "VERIFICATION_RESULTS_RECORDED",
        "AI_ANALYSIS_COMPLETED",
        "AI_ANALYSIS_FAILED",
        "MERCHANT_DRAFT_GENERATED",
        "RESOLUTION_ACCEPT_CLAIM_ATTEMPTED",
    } <= types
    assert "EVIDENCE_ASSEMBLY_INITIATED" not in types and "HUMAN_REVIEW_COMPLETED" not in types
    stamps = [item["timestamp"] for item in feed["items"]]
    assert stamps == sorted(stamps, reverse=True)


def test_review_queue_counts_a_case_once_after_payment_relink(dashboard):
    client, engine = dashboard
    seed(engine)
    with Session(engine) as db:
        relinked = db.scalar(select(Dispute).where(Dispute.paypal_dispute_id == "PP-D-A"))
        relinked.payment_id = "pay_corrected"
        evidence = pack(relinked.payment_id, relinked.id, 1, 10)
        db.add(evidence)
        db.flush()
        db.add(analysis(evidence, 10))
        db.commit()
    data = get(client, "counsel", PERIOD)
    assert data["recommendations"] == 5
    assert data["pending_reviews"] == 3


@pytest.mark.parametrize(
    "table,name", [("evidence_packs", "evidence"), ("counsel_analyses", "counsel")]
)
def test_independent_module_failure_preserves_other_sections(dashboard, table, name):
    client, engine = dashboard
    seed(engine)
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE {table}"))
    assert client.get(f"{BASE}/{name}", headers=KEY).status_code == 503
    assert get(client, "overview", PERIOD)["total"] == 3
    cases = get(client, "cases", PERIOD)
    assert cases["total"] == 3 and cases["unavailable"] == [name]
    assert all(row[f"{name}_status"] is None for row in cases["items"])
    feed = get(client, "activity", PERIOD)
    assert feed["unavailable"] == [name] and feed["items"]

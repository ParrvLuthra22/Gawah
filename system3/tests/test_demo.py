from types import SimpleNamespace

from counsel import demo
from counsel.models import Base, Dispute, Source
from gawah_contract import PaymentRecord
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


def payment(id: str) -> PaymentRecord:
    return PaymentRecord.model_validate({
        "id": id, "proposal_id": f"prop-{id}", "decision_id": f"dec-{id}",
        "paypal_order_id": f"order-{id}", "paypal_capture_id": f"capture-{id}",
        "status": "PAID", "amount_minor": 6500, "currency": "USD",
        "idempotency_key": f"idem-{id}", "custom_id": f"gawah:{id}",
        "created_at": "2026-11-10T10:00:00Z",
    })


class System2:
    def list_payments(self):
        return [payment("pay-old"), payment("pay-defend"), payment("pay-concede")]


def test_demo_uses_two_latest_real_payments_and_is_idempotent(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'demo.db').as_posix()}")
    Base.metadata.create_all(engine)
    assembled, analyzed, opened = [], [], []
    monkeypatch.setattr(demo, "assemble_pack", lambda _db, _client, pid, *, dispute_id: assembled.append((pid, dispute_id)))
    monkeypatch.setattr(
        demo, "analyze",
        lambda _db, _groq, pid, *, dispute_id: analyzed.append((pid, dispute_id)) or SimpleNamespace(
            analysis_status="COMPLETED", outcome=SimpleNamespace(recommendation="REQUEST_MORE_EVIDENCE")
        ),
    )
    monkeypatch.setattr(
        demo.resolution_workflow, "open_workflow",
        lambda _db, _actor, body, **kwargs: opened.append((body.dispute_id, kwargs)) or len(opened),
    )
    with Session(engine) as db:
        first = demo.run(db, System2(), object())
        second = demo.run(db, System2(), object())
        disputes = db.scalars(select(Dispute).where(Dispute.source == Source.SIMULATED_FIXTURE)).all()
    assert [row.payment_id for row in first] == ["pay-defend", "pay-concede"]
    assert [row.status for row in first] == ["COMPLETED", "COMPLETED"]
    assert len(disputes) == 2
    assert {row.payment_id for row in disputes} == {"pay-defend", "pay-concede"}
    assert len(assembled) == len(analyzed) == len(opened) == 4
    assert all(kwargs == {"allow_simulated": True} for _, kwargs in opened)
    assert [row.dispute_id for row in second] == [row.dispute_id for row in first]
    engine.dispose()

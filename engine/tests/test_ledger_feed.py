"""GET /v1/ledger/head and /v1/ledger/stream (SSE) with TestClient streaming."""

import json
import threading
import time

import pytest
from conftest import KEY_S1, KEY_S3, AutoKeyClient
from gawah_contract import LedgerHead
from gawah_engine.config import settings
from gawah_engine.main import app
from gawah_engine.services import get_services

H3 = {"X-Gawah-Key": KEY_S3}


@pytest.fixture
def feed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gawah_mock", False)
    monkeypatch.setattr(settings, "system1_key", KEY_S1)
    monkeypatch.setattr(settings, "system3_key", KEY_S3)
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'f.db'}")
    monkeypatch.setattr(settings, "ledger_signing_key_path", str(tmp_path / "keys"))
    monkeypatch.setattr(settings, "mandate_signing_key_path", str(tmp_path / "keys"))
    monkeypatch.setattr(settings, "sse_poll_s", 0.02)
    monkeypatch.setattr(settings, "sse_keepalive_s", 0.2)
    with AutoKeyClient(app) as c:
        yield c


def add_entry(i: int):
    from gawah_contract import LedgerEntryType

    return get_services().ledger.append(LedgerEntryType.MANDATE_REVOKED, f"man_{i}", {"mandate_id": f"man_{i}"})


def parse(lines):
    events, cur = [], {}
    for line in lines:
        if line == "":
            if cur:
                events.append(cur)
            cur = {}
        elif line.startswith(":"):
            events.append({"comment": line[1:].strip()})
        else:
            k, _, v = line.partition(": ")
            cur[k] = v
    return events


def test_head_of_an_empty_ledger_and_after_appends_is_signed(feed):
    h0 = LedgerHead.model_validate(feed.get("/v1/ledger/head", headers=H3).json())
    assert h0.seq == 0 and h0.hash == "0" * 64
    e = add_entry(1)
    h1 = LedgerHead.model_validate(feed.get("/v1/ledger/head", headers=H3).json())
    assert (h1.seq, h1.hash) == (e.seq, e.hash) and h1.message == f"gawah-ledger-head:{e.seq}:{e.hash}"
    signer = get_services().ledger.signer
    assert signer.verify(h1.message, h1.signature, h1.key_id)
    assert not signer.verify(h1.message + "x", h1.signature, h1.key_id)  # bound to the head


def test_stream_emits_new_entries_with_ids_and_a_keepalive(feed):
    add_entry(0)  # existing history is NOT replayed without Last-Event-ID
    with feed.stream("GET", "/v1/ledger/stream?limit=2&max_seconds=10", headers=H3) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        threading.Timer(0.5, add_entry, args=(1,)).start()
        threading.Timer(0.7, add_entry, args=(2,)).start()
        got = list(r.iter_lines())
    events = parse(got)
    ledger = [e for e in events if e.get("event") == "ledger"]
    assert [e["id"] for e in ledger] == ["2", "3"]
    assert [json.loads(e["data"])["seq"] for e in ledger] == [2, 3]
    assert json.loads(ledger[0]["data"])["subject_id"] == "man_1"
    assert any(e.get("comment") == "keepalive" for e in events)  # idle gap > 0.2 s before entry 1


def test_last_event_id_resumes_after_that_seq(feed):
    for i in range(4):
        add_entry(i)  # seq 1..4
    with feed.stream("GET", "/v1/ledger/stream?limit=2&max_seconds=10", headers={**H3, "Last-Event-ID": "2"}) as r:
        events = parse(list(r.iter_lines()))
    assert [e["id"] for e in events if e.get("event") == "ledger"] == ["3", "4"]
    with feed.stream("GET", "/v1/ledger/stream?limit=1&max_seconds=10&since_seq=3", headers=H3) as r:
        assert [e["id"] for e in parse(list(r.iter_lines())) if e.get("event") == "ledger"] == ["4"]


def test_resume_then_live_without_gaps_or_duplicates(feed):
    add_entry(0)
    with feed.stream(
        "GET", "/v1/ledger/stream?limit=2&max_seconds=10", headers={**H3, "Last-Event-ID": "0"}
    ) as r:
        threading.Timer(0.3, add_entry, args=(9,)).start()
        out = [e["id"] for e in parse(list(r.iter_lines())) if e.get("event") == "ledger"]
    assert out == ["1", "2"]  # history first (1), then the live entry (2): no gap, no duplicate


def test_bad_last_event_id_is_400_and_stream_needs_the_right_key(feed):
    r = feed.get("/v1/ledger/stream", headers={**H3, "Last-Event-ID": "abc"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "BAD_LAST_EVENT_ID"
    assert feed.get("/v1/ledger/stream", headers={"X-Gawah-Key": KEY_S1}).status_code == 403
    assert feed.get("/v1/ledger/head", headers={"X-Gawah-Key": KEY_S1}).status_code == 403
    assert feed.get("/v1/ledger/stream").status_code == 401


def test_mock_mode_stream_and_head_are_labelled_simulated(monkeypatch):
    monkeypatch.setattr(settings, "gawah_mock", True)
    monkeypatch.setattr(settings, "system3_key", KEY_S3)
    c = AutoKeyClient(app)
    head = c.get("/v1/ledger/head", headers=H3).json()
    assert head["key_id"] == "mock" and head["signature"].startswith("SIMULATED")
    t = time.time()
    with c.stream("GET", "/v1/ledger/stream?limit=1&max_seconds=10&since_seq=0", headers=H3) as r:
        events = parse(list(r.iter_lines()))
    assert [e["id"] for e in events if e.get("event") == "ledger"] == ["1"] and time.time() - t < 5

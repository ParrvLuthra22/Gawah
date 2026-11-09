"""Semantic check: ordered model fallback, one warning per dead model, visible in /health."""

import json
import logging

import httpx
import pytest
from conftest import KEY_S1, AutoKeyClient
from gawah_engine.config import settings
from gawah_engine.main import app
from gawah_engine.semantic import ModelNotFound, SemanticChecker, groq_llm, model_list

LINES = [("SHOE-BLK-RUN", "Black Runner", "size 9")]
OK = json.dumps({"category_match": "match", "variant_issues": [], "confidence": 0.95})


class FakeGroq:
    """A fake client: per-model behaviour, records which models were called."""

    def __init__(self, **by_model):
        self.by_model, self.calls = by_model, []

    def __call__(self, model, messages):
        self.calls.append(model)
        action = self.by_model.get(model, OK)
        if isinstance(action, Exception):
            raise action
        return action


def checker(tmp_path, fake, **kw):
    from gawah_engine.db import init_db

    path = str(tmp_path / "s.db")
    init_db(path)
    return SemanticChecker(path, client=fake, models=["m-missing", "m-good"], **kw)


def check(c):
    return c.check_inputs("footwear", "Black running shoes, size 9", LINES)


def test_model_list_order_and_defaults():
    assert model_list("openai/gpt-oss-20b", "openai/gpt-oss-20b,openai/gpt-oss-120b") == [
        "openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    assert model_list("custom", "a, b ,a") == ["custom", "a", "b"]
    assert settings.groq_model == "openai/gpt-oss-20b"
    assert settings.groq_models == "openai/gpt-oss-20b,openai/gpt-oss-120b"


def test_404_logs_one_warning_and_tries_the_next_model(tmp_path, caplog):
    fake = FakeGroq(**{"m-missing": ModelNotFound("m-missing")})
    c = checker(tmp_path, fake)
    with caplog.at_level(logging.WARNING, logger="gawah.semantic"):
        first = check(c)
        c.check_inputs("footwear", "other description", LINES)  # second call: no new 404 / warning
    assert first.source == "model" and first.category_match == "match"
    assert fake.calls == ["m-missing", "m-good", "m-good"]  # dead model is not retried
    warnings = [r for r in caplog.records if r.name == "gawah.semantic"]
    assert len(warnings) == 1 and "m-missing" in warnings[0].getMessage()
    assert "m-good" in warnings[0].getMessage()
    assert c.status() == {"semantic": "groq", "model": "m-good", "checked": True}


def test_every_model_missing_uses_the_keyword_fallback_and_says_so(tmp_path, caplog):
    fake = FakeGroq(**{"m-missing": ModelNotFound("a"), "m-good": ModelNotFound("b")})
    c = checker(tmp_path, fake)
    with caplog.at_level(logging.WARNING, logger="gawah.semantic"):
        res = check(c)
    assert res.source == "fallback"
    assert c.status()["semantic"] == "fallback" and c.status()["model"] is None
    assert len([r for r in caplog.records if r.name == "gawah.semantic"]) == 2
    assert "keyword fallback" in caplog.records[-1].getMessage()
    check(c)  # no further network once all models are known dead
    assert len(fake.calls) == 2


def test_timeout_is_not_a_404_so_it_falls_back_without_trying_the_next_model(tmp_path):
    fake = FakeGroq(**{"m-missing": httpx.ReadTimeout("slow")})
    c = checker(tmp_path, fake)
    res = check(c)
    assert res.source == "fallback" and fake.calls == ["m-missing", "m-missing"]  # one retry
    assert c.status() == {"semantic": "fallback", "model": "m-missing", "checked": True}


def test_garbage_answer_falls_back(tmp_path):
    c = checker(tmp_path, FakeGroq(**{"m-missing": "not json"}))
    assert check(c).source == "fallback"


def test_cached_answers_do_not_change_the_reported_status(tmp_path):
    fake = FakeGroq(**{"m-missing": ModelNotFound("x")})
    c = checker(tmp_path, fake)
    check(c)
    n = len(fake.calls)
    assert check(c).source == "cache" and len(fake.calls) == n


def test_no_key_is_reported_as_fallback_before_any_call(tmp_path):
    c = SemanticChecker(str(tmp_path / "x.db"))
    assert c.status() == {"semantic": "fallback", "model": None, "checked": False}


def test_groq_client_raises_model_not_found_on_404(monkeypatch):
    class R:
        status_code = 404
        text = "no such model"

        def raise_for_status(self):
            raise AssertionError("must not be reached")

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    with pytest.raises(ModelNotFound):
        groq_llm("key")("some-model", [])


def test_health_reports_the_last_real_outcome(tmp_path, monkeypatch):
    from conftest import KEY_S3
    from gawah_engine.services import get_services

    monkeypatch.setattr(settings, "gawah_mock", False)
    monkeypatch.setattr(settings, "system1_key", KEY_S1)
    monkeypatch.setattr(settings, "system3_key", KEY_S3)
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'h.db'}")
    monkeypatch.setattr(settings, "ledger_signing_key_path", str(tmp_path / "keys"))
    monkeypatch.setattr(settings, "mandate_signing_key_path", str(tmp_path / "keys"))
    with AutoKeyClient(app) as client:
        assert client.get("/health").json()["semantic"] == "fallback"  # no key configured
        sem = get_services().guardian.semantic
        fake = FakeGroq(**{"m-missing": ModelNotFound("x")})
        live = SemanticChecker(sem.path, client=fake, models=["m-missing", "m-good"])
        get_services().guardian.semantic = live
        assert client.get("/health").json() == {
            "status": "ok", "service": "gawah", "semantic": "groq", "model": "m-missing"}
        check(live)
        assert client.get("/health").json()["model"] == "m-good"
        fake.by_model["m-good"] = httpx.ReadTimeout("down")
        live.check_inputs("footwear", "a different description entirely", LINES)
        h = client.get("/health").json()
        assert h["semantic"] == "fallback" and h["model"] == "m-good"

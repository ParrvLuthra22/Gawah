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


def test_garbage_answers_from_every_model_fall_back(tmp_path):
    c = checker(tmp_path, FakeGroq(**{"m-missing": "not json", "m-good": "{}"}))
    res = check(c)
    assert res.source == "fallback" and "unusable answer" in res.reason


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
            "status": "ok", "service": "gawah", "env": "dev", "mock": False, "paypal": "sandbox", "card_configured": False,
            "semantic": "groq", "model": "m-missing"}
        check(live)
        assert client.get("/health").json()["model"] == "m-good"
        fake.by_model["m-good"] = httpx.ReadTimeout("down")
        live.check_inputs("footwear", "a different description entirely", LINES)
        h = client.get("/health").json()
        assert h["semantic"] == "fallback" and h["model"] is None


# --- rate limits, 5xx, timeouts, cooldowns --------------------------------------------------------


def http_error(status, retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    req = httpx.Request("POST", "https://api.groq.com/x")
    return httpx.HTTPStatusError("err", request=req, response=httpx.Response(status, headers=headers, request=req))


class Scripted:
    """Per model, a list of outcomes consumed in order; the last one repeats."""

    def __init__(self, **plan):
        self.plan, self.calls = {k: list(v) for k, v in plan.items()}, []

    def __call__(self, model, messages):
        self.calls.append(model)
        steps = self.plan.get(model, [OK])
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if isinstance(step, Exception):
            raise step
        return step


def timed(tmp_path, clock, fake, **kw):
    from gawah_engine.db import init_db

    path = str(tmp_path / "t.db")
    init_db(path)
    return SemanticChecker(path, client=fake, models=["m1", "m2"], clock=clock, **kw)


def ask(c, desc="Black running shoes"):
    return c.check_inputs("footwear", desc, LINES)


def test_429_tries_the_next_model_and_records_why(tmp_path, clock):
    fake = Scripted(m1=[http_error(429, retry_after=30)])
    c = timed(tmp_path, clock, fake)
    res = ask(c)
    assert fake.calls == ["m1", "m2"]
    assert res.source == "model" and res.model == "m2"
    assert "m1 rate limited (429), cooling 30s" in res.reason
    assert "[m2]" in res.describe() and "rate limited" in res.describe()
    assert c.status() == {"semantic": "groq", "model": "m2", "checked": True}


@pytest.mark.parametrize("failure", [http_error(503), http_error(500), httpx.ReadTimeout("slow"),
                                    httpx.ConnectError("down")])
def test_5xx_timeouts_and_connection_errors_try_the_next_model(tmp_path, clock, failure):
    fake = Scripted(m1=[failure])
    res = ask(timed(tmp_path, clock, fake))
    assert fake.calls == ["m1", "m2"] and res.model == "m2" and res.source == "model"
    assert "m1" in res.reason and "cooling 60s" in res.reason  # default cooldown without Retry-After


def test_404_then_next_model_is_recorded(tmp_path, clock):
    res = ask(timed(tmp_path, clock, Scripted(m1=[ModelNotFound("m1")])))
    assert res.model == "m2" and "m1 not found" in res.reason


def test_cooled_down_models_are_skipped_without_a_call(tmp_path, clock):
    fake = Scripted(m1=[http_error(429, retry_after=120)])
    c = timed(tmp_path, clock, fake)
    ask(c, "first description")
    fake.calls.clear()
    clock.advance(seconds=10)
    res = ask(c, "second description")
    assert fake.calls == ["m2"]  # m1 is cooling, never called
    assert "m1 cooling down (110s left)" in res.reason


def test_all_models_cooled_down_uses_the_keyword_fallback_without_any_call(tmp_path, clock):
    fake = Scripted(m1=[http_error(429, retry_after=60)], m2=[http_error(429)])
    c = timed(tmp_path, clock, fake)
    res = ask(c, "first")
    assert res.source == "fallback" and res.model is None
    assert "m1 rate limited" in res.reason and "m2 rate limited" in res.reason
    assert c.status() == {"semantic": "fallback", "model": None, "checked": True}
    fake.calls.clear()
    res2 = ask(c, "second")
    assert fake.calls == [] and res2.source == "fallback"
    assert "m1 cooling down" in res2.reason and "m2 cooling down" in res2.reason


def test_cooldown_expiry_brings_a_model_back(tmp_path, clock):
    fake = Scripted(m1=[http_error(429, retry_after=30), OK], m2=[http_error(429, retry_after=90), OK])
    c = timed(tmp_path, clock, fake)
    assert ask(c, "a").source == "fallback"
    fake.calls.clear()
    clock.advance(seconds=31)  # m1's 30 s are over, m2 still has 59 s
    res = ask(c, "b")
    assert fake.calls == ["m1"] and res.model == "m1" and res.source == "model"
    assert "m2 cooling down" not in res.reason  # m1 answered first, m2 never considered
    clock.advance(seconds=100)
    assert ask(c, "c").model == "m1"


def test_retry_after_defaults_and_is_clamped(tmp_path, clock):
    from gawah_engine.semantic import retry_after_seconds

    assert retry_after_seconds(None, 60) == 60
    assert retry_after_seconds("12", 60) == 12
    assert retry_after_seconds("garbage", 60) == 60
    assert retry_after_seconds("0", 60) == 1 and retry_after_seconds("999999", 60) == 3600


def test_decision_explanation_and_ledger_record_the_model_or_fallback(tmp_path, clock, psvc):
    from world import World

    w = World(psvc, clock)
    fake = Scripted(m1=[http_error(429, retry_after=45)])
    psvc.guardian.semantic = SemanticChecker(psvc.path, client=fake, models=["m1", "m2"], clock=clock)
    d = psvc.guardian.check(w.proposal())
    assert "semantic check: model [m2]; m1 rate limited (429), cooling 45s" in d.explanation
    p = w.proposal()
    psvc.guardian.submit(p)
    made = w.entries(type="DECISION_MADE")[-1].data
    # the dry run above cached m2's answer, so the real submit is served from the cache but still
    # names the model that produced it
    assert made["semantic_source"] == "cache" and made["semantic_model"] == "m2"

    psvc.guardian.semantic = SemanticChecker(
        psvc.path, client=Scripted(m1=[http_error(429)], m2=[http_error(503)]), models=["m1", "m2"], clock=clock)
    p2 = w.proposal(w.cart(title="Black Runner Mk2", unit=1000))  # different prompt: not cached
    psvc.guardian.submit(p2)
    made = w.entries(type="DECISION_MADE")[-1].data
    assert made["semantic_source"] == "fallback" and made["semantic_model"] == "fallback"
    assert "m1 rate limited" in made["semantic_why"] and "m2 HTTP 503" in made["semantic_why"]
    assert "semantic check: fallback; m1 rate limited" in made["explanation"]

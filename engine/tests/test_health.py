from fastapi.testclient import TestClient
from gawah_engine.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["service"] == "gawah"
    assert r.json()["semantic"] in ("groq", "fallback")

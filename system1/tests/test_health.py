from fastapi.testclient import TestClient
from shopper.main import app


def test_health_endpoint_still_works() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "system": "system1"}

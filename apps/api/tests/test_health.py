from fastapi.testclient import TestClient

from caselens_api.main import app


def test_healthz_returns_ok() -> None:
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

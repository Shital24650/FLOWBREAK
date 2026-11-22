import os
os.environ["DATA_DIR"] = "/tmp/flowbreak-test-data"

from fastapi.testclient import TestClient
from app.main import app, safe_public_url

client = TestClient(app)

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_rejects_localhost():
    try:
        safe_public_url("http://localhost:8000")
        assert False, "localhost must be rejected"
    except ValueError:
        assert True

def test_rejects_non_http_scheme():
    try:
        safe_public_url("file:///etc/passwd")
        assert False, "file scheme must be rejected"
    except ValueError:
        assert True

def test_demo_is_explicitly_labelled():
    response = client.post("/api/demo")
    assert response.status_code == 200
    data = response.json()
    assert data["is_demo"] is True
    assert data["metrics"]["failures"] == 2

def test_demo_run_is_persisted():
    created = client.post("/api/demo").json()
    fetched = client.get(f"/api/runs/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["is_demo"] is True

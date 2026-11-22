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



def test_main_document_failure_is_not_recorded_twice():
    from app.main import record_resource_http_failures

    failures = []
    def record(category, severity, title, detail, link=None):
        failures.append({
            "category": category,
            "severity": severity,
            "title": title,
            "detail": detail,
            "url": link,
        })

    failed_responses = [
        {"status": 503, "url": "https://example.com/", "resource_type": "document"},
    ]
    record("http", "critical", "Main page returned HTTP 503", "https://example.com/")
    record_resource_http_failures(record, failed_responses)

    assert len(failures) == 1
    assert failures[0]["title"] == "Main page returned HTTP 503"


def test_failed_subresource_is_still_recorded():
    from app.main import record_resource_http_failures

    failures = []
    def record(category, severity, title, detail, link=None):
        failures.append((category, severity, title, detail, link))

    record_resource_http_failures(record, [
        {"status": 404, "url": "https://example.com/missing.js", "resource_type": "script"},
    ])

    assert len(failures) == 1
    assert failures[0][0] == "http"
    assert failures[0][2] == "HTTP 404 resource error"


def test_rejects_url_credentials():
    try:
        safe_public_url("https://user:password@example.com/")
        assert False, "URLs with embedded credentials must be rejected"
    except ValueError:
        assert True


def test_rejects_hostname_with_any_private_dns_answer(monkeypatch):
    import socket

    def fake_getaddrinfo(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    try:
        safe_public_url("https://mixed-dns.example/")
        assert False, "A hostname resolving to any non-public address must be rejected"
    except ValueError:
        assert True

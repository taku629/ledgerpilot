"""HTTP-layer regression tests: every endpoint must return valid JSON
(no NaN/inf leaks from pandas) and 200."""
import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _no_nan(obj):
    if isinstance(obj, float):
        assert obj == obj and abs(obj) != float("inf"), f"non-finite float {obj}"
    elif isinstance(obj, dict):
        for v in obj.values():
            _no_nan(v)
    elif isinstance(obj, list):
        for v in obj:
            _no_nan(v)


def test_state_ok_and_json_clean():
    r = client.get("/api/state")
    assert r.status_code == 200
    _no_nan(r.json())


def test_transactions_ok_and_json_clean():
    r = client.get("/api/transactions", params={"months": 3})
    assert r.status_code == 200
    d = r.json()
    assert d["rows"]
    _no_nan(d)


def test_invoices_ok_and_json_clean():
    r = client.get("/api/invoices")
    assert r.status_code == 200
    d = r.json()
    assert len(d["rows"]) >= 10
    _no_nan(d)


def test_forecast_and_financing_ok():
    assert client.get("/api/forecast").status_code == 200
    assert client.get("/api/financing").status_code == 200


def test_whatif_ok():
    r = client.post("/api/whatif", json={"sales_pct": -20})
    assert r.status_code == 200
    _no_nan(r.json())


def test_chat_reply_and_tool_calls():
    r = client.post("/api/chat", json={"message": "how is my cashflow?"})
    assert r.status_code == 200
    d = r.json()
    assert d["reply"] and d["tool_calls"]


def test_security_headers_present():
    r = client.get("/")
    h = r.headers
    assert "default-src 'self'" in h["content-security-policy"]
    assert "script-src 'self'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"


def test_unknown_endpoint_404_json():
    r = client.get("/api/nope")
    assert r.status_code == 404
    json.loads(r.text)  # still valid JSON


def test_error_handler_returns_json_not_traceback():
    r = client.post("/api/chat", json={})  # missing required field -> 422
    assert r.status_code == 422
    assert "Traceback" not in r.text

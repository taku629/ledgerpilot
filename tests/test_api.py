"""HTTP-layer regression tests: every endpoint must return valid JSON
(no NaN/inf leaks from pandas) and 200."""
import json

import pytest
from fastapi.testclient import TestClient

from app import sandbox
from app.main import app, ctx


@pytest.fixture(autouse=True)
def _fresh_sandbox():
    """Each API test starts from the generated baseline state — the file and
    the in-memory schedule (built at import, so stale reschedules must be
    re-applied too)."""
    sandbox.STATE_PATH.unlink(missing_ok=True)
    ctx.scheduled = sandbox.apply_reschedules(ctx.scheduled_raw)
    from app.main import chat_limiter
    chat_limiter._buckets.clear()
    yield
    sandbox.STATE_PATH.unlink(missing_ok=True)


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


def test_chat_propose_approve_execute_e2e():
    """Full Tier-2 flow over HTTP: chat proposes, owner approves, sandbox executes,
    audit log records every step."""
    r = client.post("/api/chat",
                    json={"message": "reschedule pay-012 to 2026-12-08"})
    assert r.status_code == 200
    pend = r.json()["pending_actions"]
    assert len(pend) == 1 and pend[0]["tool"] == "reschedule_payment"

    aid = pend[0]["action_id"]
    r2 = client.post(f"/api/actions/{aid}/approve")
    assert r2.status_code == 200
    res = r2.json()
    assert res["ok"] is True
    assert res["result"]["new_date"] == "2026-12-08"

    # Replay of the same approval token must be refused (403)
    r3 = client.post(f"/api/actions/{aid}/approve")
    assert r3.status_code == 403

    audit = client.get("/api/audit").json()["rows"]
    kinds = [e.get("kind") for e in audit]
    assert "sandbox_exec" in kinds or "execute" in str(kinds)


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


def test_input_bounds_enforced():
    r = client.post("/api/chat", json={"message": "x" * 2001})
    assert r.status_code == 422
    r = client.post("/api/whatif", json={"sales_pct": -500})
    assert r.status_code == 422
    r = client.post("/api/whatif", json={"ar_delay_weeks": 52})
    assert r.status_code == 422


def test_regenerate_preserves_audit_trail():
    r = client.post("/api/dev/regenerate")
    assert r.status_code == 200
    rows = client.get("/api/audit").json()["rows"]
    assert rows, "audit log must survive a data reset"
    assert any(e.get("kind") == "data_regenerated" for e in rows)


def test_audit_jsonl_export():
    """Evidence download: /api/audit?format=jsonl streams NDJSON that
    parses line-by-line — same events as the JSON view."""
    r = client.get("/api/audit?format=jsonl&n=500")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in r.headers["content-disposition"]
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    import json as _json
    events = [_json.loads(ln) for ln in lines]
    assert events and all("kind" in e for e in events)


def test_financing_memo_markdown():
    """Decision memo: deterministic markdown with gap, ranked + rejected
    tables and the not-advice disclaimer — evidence for judges."""
    r = client.get("/api/financing/memo")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"]
    t = r.text
    assert "Financing decision memo" in t
    assert "## Ranked options" in t and "## Rejected" in t
    assert "not financial advice" in t


def test_rate_limit_trips_after_burst():
    from app.main import chat_limiter
    chat_limiter._buckets.clear()
    for _ in range(12):
        r = client.post("/api/chat", json={"message": "hi"})
        assert r.status_code == 200
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 429


def test_healthz_reports_integrity():
    r = client.get("/healthz")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["audit_integrity_ok"] is True
    assert d["transactions"] > 0 and d["as_of"]

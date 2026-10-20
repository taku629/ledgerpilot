"""Round 9: bank-CSV import (mapping/validation/PII/refit), mark-invoice-paid
AR removal, and named what-if scenario comparison."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import sandbox
from app.config import DATA_DIR
from app.data_gen import load_all
from app.main import app, ctx

client = TestClient(app)
TXN = DATA_DIR / "transactions.csv"


@pytest.fixture(autouse=True)
def _restore():
    """Snapshot ledger + in-memory context; restore after each test."""
    from app.main import chat_limiter
    chat_limiter._buckets.clear()
    sandbox.STATE_PATH.unlink(missing_ok=True)
    orig = TXN.read_bytes()
    profile = dict(ctx.profile)
    ctx.invoices = sandbox.apply_paid_invoices(load_all()["invoices"])
    yield
    TXN.write_bytes(orig)
    (DATA_DIR / "transactions_backup.csv").unlink(missing_ok=True)
    sandbox.STATE_PATH.unlink(missing_ok=True)
    ctx.profile.clear()
    ctx.profile.update(profile)
    ctx.txn = pd.read_csv(TXN, parse_dates=["date"])
    ctx.invoices = sandbox.apply_paid_invoices(load_all()["invoices"])
    ctx._forecast_cache.clear()


def _post_csv(path, body, **params):
    return client.post(path, params=params, content=body,
                       headers={"content-type": "text/csv"})


# ------------------------------------------------------------- CSV import
NONSTANDARD = ("Fecha,Importe,Concepto,Ref\n"
               "2026-10-10,-2400.00,Shop rent October,a1\n"
               "2026-10-11,3100.50,Wholesale order customer payout,a2\n"
               "2026-10-12,-120.00,Refund to jane.doe@example.com tel 090-1234-5678,a3\n"
               "2026-10-13,-55.10,AWS cloud subscription card 4111 1111 1111 1111,a4\n")


def test_preview_explicit_mapping_categorizes_and_masks():
    # aliases can't find these Spanish headers → preview reports the gap
    r = _post_csv("/api/data/preview", NONSTANDARD)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False and d["mapping"]["date"] is None
    assert d["columns"] == ["Fecha", "Importe", "Concepto", "Ref"]
    # with an explicit column mapping it parses
    r = _post_csv("/api/data/preview", NONSTANDARD, date_col="Fecha",
                  amount_col="Importe", description_col="Concepto")
    d = r.json()
    assert d["ok"] and d["valid_rows"] == 4
    cats = {s["counterparty"][:9]: s["category"] for s in d["sample"]}
    assert cats["Shop rent"] == "Rent"
    assert cats["Wholesale"] == "Coffee & Supplies"   # first matching rule wins
    memos = " ".join(s["memo"] for s in d["sample"])
    assert "jane.doe@example.com" not in memos and "[EMAIL]" in memos
    assert "4111" not in memos and "[CARD]" in memos
    assert "1234-5678" not in memos
    assert d["inflow_total"] == 3100.5 and d["outflow_total"] == 2575.1
    # preview never writes
    assert len(ctx.txn) == len(pd.read_csv(TXN))


def test_upload_with_mapping_refits_forecast_and_balance():
    bal0 = ctx.profile["current_balance"]
    min0 = ctx.forecast()["min_balance"]
    body = ("Fecha,Importe,Concepto\n"
            "2026-10-16,-8000.00,Emergency roaster repair\n"
            "2026-10-17,-4000.00,Payroll bonus run\n")
    r = _post_csv("/api/data/upload", body, date_col="Fecha",
                  amount_col="Importe", description_col="Concepto")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["added"] == 2
    assert d["forecast_before"]["current_balance"] == pytest.approx(bal0)
    assert d["forecast"]["current_balance"] == pytest.approx(bal0 - 12000, abs=0.01)
    assert d["forecast"]["min_balance"] < min0      # forecast actually refit
    assert ctx.forecast()["min_balance"] == d["forecast"]["min_balance"]
    assert set(d["categories"]) == {"Equipment", "Payroll"}
    st = client.get("/api/state").json()
    assert st["profile"]["current_balance"] == pytest.approx(bal0 - 12000, abs=0.01)
    rows = client.get("/api/audit", params={"n": 10}).json()["rows"]
    up = [e for e in rows if e.get("kind") == "data_uploaded"
          and e.get("mapping", {}).get("date") == "Fecha"]
    assert up and up[0]["min_balance_after"] < up[0]["min_balance_before"]


def test_upload_credit_debit_columns_and_parenthesised_negatives():
    body = ("Posted,Description,Debit,Credit\n"
            "2026-10-15,Electric utility bill,180.25,\n"
            "2026-10-15,Card sales payout,,950.00\n")
    d = _post_csv("/api/data/preview", body).json()
    assert d["ok"] and d["mapping"]["debit"] == "Debit"
    flows = {s["category"]: s["flow"] for s in d["sample"]}
    assert flows["Utilities"] == "outflow" and flows["Revenue"] == "inflow"
    d2 = _post_csv("/api/data/preview",
                   "date,amount,memo\n2026-10-15,(75.00),Insurance premium\n").json()
    assert d2["sample"][0]["flow"] == "outflow" and d2["sample"][0]["amount"] == 75.0


def test_upload_validation_size_format_rowcount_and_mapping():
    from app import ingest
    big = b"date,amount,description\n" + b"2026-10-01,1.00,x\n" * (ingest.MAX_BYTES // 17 + 10)
    r = _post_csv("/api/data/upload", big)
    assert r.status_code == 413
    many = "date,amount,description\n" + "".join(
        f"2026-10-01,{i + 1}.00,row\n" for i in range(ingest.MAX_ROWS + 1))
    r = _post_csv("/api/data/upload", many)
    assert r.status_code == 422 and "limit" in r.json()["detail"]
    assert _post_csv("/api/data/upload", b"\x00\x01\x02binary").status_code == 422
    r = _post_csv("/api/data/upload", b"PK\x03\x04xlsx-ish")
    assert r.status_code == 422 and "csv" in r.json()["detail"].lower()
    assert _post_csv("/api/data/upload", b"").status_code == 422
    assert _post_csv("/api/data/upload", b"onlyonecolumn\n1\n").status_code == 422
    r = _post_csv("/api/data/upload", "date,amount\n2026-10-01,5\n", date_col="Nope")
    assert r.status_code == 422 and "no such column" in r.json()["detail"]
    r = _post_csv("/api/data/upload", "date,amount\nnot-a-date,5\nfoo,6\n")
    assert r.status_code == 422
    # nothing above touched the ledger
    assert len(pd.read_csv(TXN)) == len(ctx.txn)


def test_upload_replace_backs_up_and_regenerate_restores():
    n0 = len(ctx.txn)
    body = ("date,amount,description\n"
            "2026-09-01,20000,Opening sales\n2026-09-15,-5000,Rent\n")
    d = _post_csv("/api/data/upload", body, mode="replace", opening_balance=1000).json()
    assert d["total"] == 2 and d["backup"] == "transactions_backup.csv"
    assert (DATA_DIR / "transactions_backup.csv").exists()
    assert ctx.profile["current_balance"] == pytest.approx(16000)
    assert client.post("/api/dev/regenerate").status_code == 200
    assert len(ctx.txn) == n0                       # demo ledger back in memory


# --------------------------------------------------- mark invoice paid (AR)
def test_mark_paid_removes_invoice_from_open_receivables():
    def open_ids():
        return {r["id"] for r in client.get("/api/invoices").json()["rows"]
                if r["status"] != "paid"}
    st0 = client.get("/api/state").json()
    assert "INV-0014" in open_ids()
    assert any(a["type"] == "AR_OVERDUE" for a in st0["alerts"])
    d = client.post("/api/chat", json={"message": "mark INV-0014 paid"}).json()
    pa = d["pending_actions"][0]
    assert pa["tool"] == "mark_invoice_paid" and pa["params"]["invoice_id"] == "INV-0014"
    assert "INV-0014" in open_ids()                 # nothing changes before approval
    r = client.post(f"/api/actions/{pa['action_id']}/approve").json()
    assert r["ok"] and r["result"]["recorded"]["invoice_id"] == "INV-0014"
    assert "INV-0014" not in open_ids()
    st1 = client.get("/api/state").json()
    wc0, wc1 = st0["kpis"]["working_capital"], st1["kpis"]["working_capital"]
    assert wc1["overdue_count"] == wc0["overdue_count"] - 1
    assert not any(a["type"] == "AR_OVERDUE" and "INV-0014" in str(a)
                   for a in st1["alerts"])
    # replay of the same approval token is refused
    assert client.post(f"/api/actions/{pa['action_id']}/approve").status_code == 403
    # persisted: a fresh context replays the paid status
    fresh = sandbox.apply_paid_invoices(load_all()["invoices"])
    assert fresh[fresh.id == "INV-0014"].iloc[0].status == "paid"


def test_mark_paid_unknown_invoice_refused():
    d = client.post("/api/chat", json={"message": "mark INV-9999 paid"}).json()
    assert not any(p.get("params", {}).get("invoice_id") == "INV-9999"
                   for p in d["pending_actions"])


# ------------------------------------------------ named scenario comparison
def test_scenarios_baseline_a_b_saved_and_compared():
    d = client.get("/api/scenarios").json()
    assert [s["name"] for s in d["scenarios"]] == ["baseline"]
    base = d["scenarios"][0]
    assert base["delta_vs_baseline"] == 0 and len(base["series"]) == 13
    r = client.post("/api/scenarios", json={"name": "A", "params": {"sales_pct": -20}})
    assert r.status_code == 200
    r = client.post("/api/scenarios", json={"name": "B", "params": {"ar_delay_weeks": 4}})
    rows = {s["name"]: s for s in r.json()["scenarios"]}
    assert list(rows) == ["baseline", "A", "B"]
    assert rows["A"]["min_balance"] < base["min_balance"]
    assert rows["A"]["delta_vs_baseline"] == pytest.approx(
        rows["A"]["min_balance"] - base["min_balance"], abs=0.01)
    assert rows["A"]["params"] == {"sales_pct": -20}
    # matches the stand-alone what-if engine exactly
    wi = client.post("/api/whatif", json={"sales_pct": -20}).json()
    assert rows["A"]["min_balance"] == wi["forecast"]["min_balance"]
    # overwrite keeps the name, changes the numbers
    r = client.post("/api/scenarios", json={"name": "A", "params": {"sales_pct": 10}})
    a2 = {s["name"]: s for s in r.json()["scenarios"]}["A"]
    assert a2["min_balance"] > rows["A"]["min_balance"]
    # chat compares the saved set
    c = client.post("/api/chat", json={"message": "compare my scenarios"}).json()
    assert "| baseline |" in c["reply"] and "| A |" in c["reply"] and "| B |" in c["reply"]
    # delete
    r = client.delete("/api/scenarios/B")
    assert [s["name"] for s in r.json()["scenarios"]] == ["baseline", "A"]
    assert client.delete("/api/scenarios/B").status_code == 404


def test_scenarios_validation_and_cap():
    assert client.post("/api/scenarios", json={"name": "baseline"}).status_code == 422
    assert client.post("/api/scenarios", json={"name": "<script>"}).status_code == 422
    assert client.post("/api/scenarios",
                       json={"name": "X", "params": {"sales_pct": 999}}).status_code == 422
    for i in range(sandbox.MAX_SCENARIOS):
        assert client.post("/api/scenarios",
                           json={"name": f"S{i}", "params": {"sales_pct": -i}}).status_code == 200
    assert client.post("/api/scenarios", json={"name": "extra"}).status_code == 409


def test_scenarios_reprice_after_ledger_change():
    client.post("/api/scenarios", json={"name": "A", "params": {"supplies_pct": 15}})
    a0 = client.get("/api/scenarios").json()["scenarios"][1]["min_balance"]
    _post_csv("/api/data/upload", "date,amount,description\n2026-10-17,-6000,Equipment deposit\n")
    a1 = client.get("/api/scenarios").json()["scenarios"][1]["min_balance"]
    assert a1 < a0


def test_compare_scenarios_empty_hint():
    c = client.post("/api/chat", json={"message": "compare scenarios"}).json()
    assert "No saved scenarios" in c["reply"]

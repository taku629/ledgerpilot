"""LedgerPilot API + static UI."""
from __future__ import annotations

import json

import math
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent
from . import sandbox
from .config import DATA_DIR, DISCLAIMER, LLM_BASE_URL
from .data_gen import generate, load_all
from .security import (ApprovalError, ApprovalStore, AuditLog, RateLimiter,
                       scan_for_injection)
from .tools import BusinessContext, call_tool, execute_approved
from . import kpis as kpis_mod

STATIC = Path(__file__).parent / "static"


def _nan_clean(o):
    """Replace non-finite floats (NaN/inf from pandas) so JSON never 500s."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _nan_clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_nan_clean(v) for v in o]
    return o


class CleanJSONResponse(JSONResponse):
    def render(self, content):
        return super().render(_nan_clean(content))


audit = AuditLog()
approvals = ApprovalStore()
chat_limiter = RateLimiter(rate_per_min=30, burst=12)
ctx = BusinessContext(load_all(), audit, approvals)
agent = Agent(ctx)

app = FastAPI(title="LedgerPilot — SME Finance Copilot",
              description="AI agent that completes SME finance tasks end-to-end "
                          "with permission tiers, approvals and a full audit trail. "
                          "Demo data is 100% synthetic.",
              default_response_class=CleanJSONResponse)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """Token bucket on mutating/compute endpoints (chat, what-if, approve,
    dev-regenerate) — the paths that cost model work or write state."""
    if request.method == "POST" and request.url.path.startswith("/api/"):
        client = request.client.host if request.client else "unknown"
        if not chat_limiter.allow(client):
            audit.record({"kind": "rate_limited", "path": request.url.path,
                          "client": client})
            return JSONResponse({"detail": "rate limit exceeded — retry shortly"},
                                status_code=429,
                                headers={"Retry-After": "2"})
    return await call_next(request)


@app.middleware("http")
async def security_headers(request, call_next):
    """Every response gets browser-hardening headers; CSP is self-only since
    all assets (incl. vendored Chart.js) are served from this origin."""
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return resp


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class WhatIfIn(BaseModel):
    sales_pct: float = Field(default=0, ge=-90, le=200)
    supplies_pct: float = Field(default=0, ge=-90, le=200)
    ar_delay_weeks: int = Field(default=0, ge=0, le=26)
    extra_outflow: dict | None = None
    extra_inflow: dict | None = None


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz():
    v = audit.verify()
    return {"ok": True, "llm_configured": bool(LLM_BASE_URL),
            "as_of": ctx.forecast()["as_of"],
            "audit_events": v["events"], "audit_integrity_ok": v["ok"],
            "transactions": len(ctx.txn), "invoices": len(ctx.invoices)}


@app.get("/api/state")
def state():
    fc = ctx.forecast()
    al = ctx.alerts()
    kp = kpis_mod.kpi_summary(ctx.txn, ctx.invoices)
    return {"profile": ctx.profile, "forecast": fc, "alerts": al,
            "kpis": kp, "disclaimer": DISCLAIMER,
            "llm_configured": agent.llm.ok,
            "catalog_size": len(ctx.catalog)}


@app.post("/api/chat")
def chat(inp: ChatIn):
    if not inp.message.strip():
        raise HTTPException(400, "empty message")
    scan = scan_for_injection(inp.message, channel="user")
    if scan["flagged"]:
        audit.record({"kind": "user_input_flagged", "matches": scan["matches"],
                      "preview": inp.message[:120]})
    out = agent.respond(inp.message)
    for pa in out.get("pending_actions", []):
        audit.record({"kind": "action_proposed", **{k: pa[k] for k in
                     ("action_id", "tool", "preview") if k in pa}})
    return out


@app.get("/api/forecast")
def forecast():
    return ctx.forecast()


@app.get("/api/financing")
def financing():
    from . import financing as fin_mod
    fc = ctx.forecast()
    gap = max(fc["gap_amount"], 0)
    return fin_mod.match_financing(ctx.catalog, ctx.financing_ctx(),
                                   gap if gap > 0 else 20000, fc["gap_week"])


@app.get("/api/financing/memo")
def financing_memo():
    """Deterministic decision memo (markdown): gap, ranked options with cost
    math, rejected options with reasons, disclaimer. Downloadable."""
    from . import financing as fin_mod
    fc = ctx.forecast()
    gap = max(fc["gap_amount"], 0)
    m = fin_mod.match_financing(ctx.catalog, ctx.financing_ctx(),
                                gap if gap > 0 else 20000, fc["gap_week"])
    worst = min(fc["weeks"], key=lambda w: w["balance_p50"])
    L = [f"# Financing decision memo — {ctx.profile['business_name']}",
         f"_Generated {fc['as_of']} from the 13-week forecast. "
         "All figures computed by code; no AI estimates._", "",
         f"- **Funding gap:** ${m['gap_amount']:,.0f} "
         f"{'(first breach week ' + str(m['gap_week']) + ')' if m['gap_week'] else '(no breach — planning amount)'}",
         f"- **Lowest projected balance:** ${worst['balance_p50']:,.0f} "
         f"in week {worst['week']} ({worst['start']})",
         f"- **Cash buffer policy:** ${ctx.profile['cash_buffer']:,.0f}", "",
         "## Ranked options",
         "| # | Product | Type | Score | Amount | Est. total cost | Monthly | Funds in | Why |",
         "|--:|---|---|--:|--:|--:|--:|--:|---|"]
    for i, r in enumerate(m["ranked"], 1):
        L.append(f"| {i} | {r['name']} | {r['type']} | {r['score']} | "
                 f"${r['amount']:,.0f} | ${r['est_total_cost']:,.0f} | "
                 f"${r['est_monthly_payment']:,.0f} | {r['time_to_fund_days']}d | {r['why']} |")
    L += ["", "## Rejected — and why",
          "| Product | Reason |", "|---|---|"]
    for r in m["rejected"]:
        L.append(f"| {r['name']} | {'; '.join(r['reasons'])} |")
    L += ["", f"_{m['disclaimer']}_", "",
          "_LedgerPilot sandbox memo — no real funding application was made._"]
    return PlainTextResponse(
        "\n".join(L), media_type="text/markdown",
        headers={"Content-Disposition": 'attachment; filename="financing_memo.md"'})


@app.post("/api/whatif")
def whatif(w: WhatIfIn):
    params = {k: v for k, v in w.model_dump().items() if v not in (0, None)}
    fc = ctx.forecast(whatif=params, use_cache=False)
    audit.record({"kind": "what_if", "params": params})
    return {"params": params, "forecast": fc, "baseline": ctx.forecast()}


@app.post("/api/actions/{action_id}/approve")
def approve(action_id: str):
    try:
        action = approvals.consume(action_id)
    except ApprovalError as e:
        audit.record({"kind": "approval_failed", "action_id": action_id,
                      "reason": str(e)})
        raise HTTPException(403, str(e))
    res = execute_approved(ctx, action)
    audit.record({"kind": "action_executed", "action_id": action_id,
                  "tool": action["tool"], "params": action.get("params", {}),
                  "result": res})
    return {"ok": res.get("ok", True), "result": res,
            "forecast": ctx.forecast()}


@app.post("/api/actions/{action_id}/reject")
def reject(action_id: str):
    audit.record({"kind": "action_rejected", "action_id": action_id})
    return {"ok": True}


@app.get("/api/transactions")
def transactions(months: int = Query(3, ge=1, le=24),
                 category: str | None = Query(None, max_length=80),
                 q: str | None = Query(None, max_length=200)):
    out = call_tool(ctx, "load_transactions",
                    {"months": months, "category": category}, origin="api")
    if q:
        ql = q.lower()
        out["rows"] = [r for r in out["rows"]
                       if ql in json_dump(r).lower()]
    return out


def json_dump(o):
    return json.dumps(o, default=str)


@app.get("/api/invoices")
def invoices():
    return {"rows": ctx.invoices.to_dict("records")}


@app.get("/api/audit")
def audit_tail(n: int = Query(60, ge=1, le=500),
               format: str = Query("json", pattern="^(json|jsonl)$")):
    if format == "jsonl":
        lines = "".join(json.dumps(e, ensure_ascii=False) + "\n"
                        for e in audit.tail(n))
        return PlainTextResponse(
            lines, media_type="application/x-ndjson",
            headers={"Content-Disposition":
                     'attachment; filename="ledgerpilot_audit.jsonl"'})
    return {"rows": audit.tail(n)}


@app.get("/api/audit/verify")
def audit_verify():
    """Re-verify the hash chain over the append-only log — proves to judges
    the trail is tamper-evident, not just written."""
    return audit.verify()


@app.post("/api/dev/regenerate")
def regenerate():
    # Resets sandbox state + data files. The audit log is intentionally NOT
    # deleted — the trail must survive a data reset; the regenerate itself
    # is appended to it.
    try:
        sandbox.STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    ctx.scheduled = sandbox.apply_reschedules(ctx.scheduled_raw)
    generate(out_dir=DATA_DIR)
    audit.record({"kind": "data_regenerated"})
    return {"ok": True}


@app.exception_handler(Exception)
def on_error(request, exc):
    audit.record({"kind": "api_error", "path": str(request.url.path),
                  "error": str(exc)[:300]})
    return JSONResponse(status_code=500, content={"error": "internal error"})

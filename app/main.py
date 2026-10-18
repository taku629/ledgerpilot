"""LedgerPilot API + static UI."""
from __future__ import annotations

import math
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import Agent
from . import sandbox
from .config import DATA_DIR, DISCLAIMER, LLM_BASE_URL, AUDIT_LOG
from .data_gen import generate, load_all
from .security import (ApprovalError, ApprovalStore, AuditLog,
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
ctx = BusinessContext(load_all(), audit, approvals)
agent = Agent(ctx)

app = FastAPI(title="LedgerPilot — SME Finance Copilot",
              description="AI agent that completes SME finance tasks end-to-end "
                          "with permission tiers, approvals and a full audit trail. "
                          "Demo data is 100% synthetic.",
              default_response_class=CleanJSONResponse)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


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
    message: str


class WhatIfIn(BaseModel):
    sales_pct: float = 0
    supplies_pct: float = 0
    ar_delay_weeks: int = 0
    extra_outflow: dict | None = None
    extra_inflow: dict | None = None


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True, "llm_configured": bool(LLM_BASE_URL)}


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
def transactions(months: int = 3, category: str | None = None, q: str | None = None):
    out = call_tool(ctx, "load_transactions",
                    {"months": months, "category": category}, origin="api")
    if q:
        ql = q.lower()
        out["rows"] = [r for r in out["rows"]
                       if ql in json_dump(r).lower()]
    return out


def json_dump(o):
    import json
    return json.dumps(o, default=str)


@app.get("/api/invoices")
def invoices():
    return {"rows": ctx.invoices.to_dict("records")}


@app.get("/api/audit")
def audit_tail(n: int = 60):
    return {"rows": audit.tail(n)}


@app.post("/api/dev/regenerate")
def regenerate():
    for p in (sandbox.STATE_PATH, AUDIT_LOG):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    generate(out_dir=DATA_DIR)
    return {"ok": True}


@app.exception_handler(Exception)
def on_error(request, exc):
    audit.record({"kind": "api_error", "path": str(request.url.path),
                  "error": str(exc)[:300]})
    return JSONResponse(status_code=500, content={"error": "internal error"})

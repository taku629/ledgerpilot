"""Agent tool surface. Every function declares a permission tier; dispatch
enforces it server-side before running anything. Tool arguments may only come
from a user message or an approved action — never from data-field content.
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from . import alerts as alerts_mod
from . import financing as fin_mod
from . import forecasting as fc_mod
from . import kpis as kpis_mod
from . import sandbox
from .config import DISCLAIMER
from .security import (TIER_DRAFT, TIER_READ, TIER_SANDBOX, TIER_REAL,
                       ApprovalError, TierError,
                       check_tier, fingerprint, mask_pii, sanitize_record_fields)

MAX_ROWS = 200


class BusinessContext:
    """Loaded data + runtime services shared by tools, agent and API."""

    def __init__(self, data: dict, audit, approvals):
        self.txn = data["transactions"]
        self.invoices = data["invoices"]
        self.scheduled_raw = data["scheduled"]
        self.scheduled = sandbox.apply_reschedules(data["scheduled"])
        self.catalog = data["catalog"]
        self.profile = dict(data["profile"])
        override = sandbox.get_state().get("cash_buffer_override")
        if override:
            self.profile["cash_buffer"] = override
        self.audit = audit
        self.approvals = approvals
        self._forecast_cache = {}

    def forecast(self, whatif: dict | None = None, use_cache: bool = True) -> dict:
        key = fingerprint(whatif or {})
        if use_cache and key in self._forecast_cache:
            return self._forecast_cache[key]
        fc = fc_mod.forecast_cashflow(self.txn, self.scheduled, self.profile,
                                      whatif=whatif)
        self._forecast_cache[key] = fc
        return fc

    def alerts(self) -> list[dict]:
        return alerts_mod.detect_alerts(self.forecast(), self.invoices,
                                        self.txn, self.profile)

    def financing_ctx(self) -> dict:
        fc = self.forecast()
        over = self.invoices[self.invoices.status != "paid"]
        last_wk = [w["net_p50"] for w in fc["weeks"][:4]] or [0]
        return {"months_operating": self.profile["months_operating"],
                "avg_monthly_revenue": self.profile["avg_monthly_revenue"],
                "has_b2b_invoices": bool(len(over)),
                "open_receivables": float(over.amount.sum()),
                "avg_weekly_free_cash": float(sum(last_wk) / len(last_wk)),
                "equipment_quote": True, "business_plan": True,
                "digital_project": True, "supplier_agreement": True}


# ------------------------------------------------------------------ tools
TOOL_DEFS = [
    {"name": "load_transactions", "tier": TIER_READ,
     "description": "Load recent bank transactions (sanitized). Args: months (int, default 3), category (optional).",
     "params": {"months": "int?", "category": "str?"}},
    {"name": "get_cashflow_forecast", "tier": TIER_READ,
     "description": "13-week cash-flow forecast: weekly inflow/outflow/balance with P10-P90 band, gap week, backtest.",
     "params": {}},
    {"name": "get_kpis", "tier": TIER_READ,
     "description": "Monthly P/L, gross margin, fixed-cost ratio, DSO/DPO/inventory days, MoM change drivers.",
     "params": {"months": "int?"}},
    {"name": "get_alerts", "tier": TIER_READ,
     "description": "Detected alerts: low-balance weeks, payment clusters, overdue receivables, suspicious text.",
     "params": {}},
    {"name": "match_financing", "tier": TIER_READ,
     "description": "Score the financing catalog against the forecast gap. Returns ranked options + rejection reasons.",
     "params": {"months": "int?"}},
    {"name": "get_evidence", "tier": TIER_READ,
     "description": "Underlying transaction/invoice rows behind a figure or alert. Args: kind+id.",
     "params": {"kind": "str", "ref": "str"}},
    {"name": "what_if", "tier": TIER_DRAFT,
     "description": "Recompute the forecast under scenarios: sales_pct, supplies_pct, ar_delay_weeks, extra_outflow{amount,week,label}, extra_inflow{amount,week,label}.",
     "params": {"sales_pct": "float?", "supplies_pct": "float?",
                "ar_delay_weeks": "int?", "extra_outflow": "dict?", "extra_inflow": "dict?"}},
    {"name": "draft_dunning_email", "tier": TIER_DRAFT,
     "description": "Draft a payment-reminder email for an overdue invoice.",
     "params": {"invoice_id": "str"}},
    {"name": "draft_loan_application", "tier": TIER_DRAFT,
     "description": "Draft a loan application for a financing product id.",
     "params": {"product_id": "str"}},
    {"name": "reschedule_payment", "tier": TIER_SANDBOX,
     "description": "Move a scheduled payment to a new date (sandbox ledger). Creates a pending action requiring user approval.",
     "params": {"payment_id": "str", "new_date": "YYYY-MM-DD"}},
    {"name": "record_sandbox_payment", "tier": TIER_SANDBOX,
     "description": "Record a simulated outbound payment in the sandbox ledger. Requires user approval + amount cap.",
     "params": {"amount": "float", "counterparty": "str", "memo": "str?"}},
    {"name": "set_cash_buffer", "tier": TIER_SANDBOX,
     "description": "Change the low-balance alert threshold. Requires user approval.",
     "params": {"amount": "float"}},
]


def openai_tool_specs() -> list[dict]:
    specs = []
    for t in TOOL_DEFS:
        props, req = {}, []
        for k, v in t["params"].items():
            typ, opt = v.rstrip("?"), not v.endswith("?")
            props[k] = {"type": {"str": "string", "int": "integer",
                                 "float": "number", "dict": "object"}.get(typ, "string")}
            if not opt:
                req.append(k)
        specs.append({"type": "function",
                      "function": {"name": t["name"], "description": t["description"],
                                   "parameters": {"type": "object",
                                                  "properties": props,
                                                  "required": req}}})
    return specs


def _recent_txn(ctx: BusinessContext, months: int = 3, category: str | None = None):
    end = ctx.txn.date.max()
    t = ctx.txn[ctx.txn.date > end - pd.Timedelta(days=30 * months)]
    if category:
        t = t[t.category.str.contains(category, case=False, na=False)]
    return t


def call_tool(ctx: BusinessContext, name: str, args: dict,
              user_tier: int = TIER_SANDBOX, origin: str = "user") -> dict:
    """Dispatch a tool call. user_tier is the *session context tier*: TIER_READ
    contexts can't even propose actions; TIER_SANDBOX (the signed-in owner) may
    propose Tier-2 actions — which still can't run without an approval token.
    Tier-3 tools are unconditionally refused."""
    spec = next((t for t in TOOL_DEFS if t["name"] == name), None)
    if not spec:
        return {"error": f"unknown tool {name}"}
    try:
        check_tier(spec["tier"], user_tier)
    except TierError as e:
        ctx.audit.record({"kind": "tool_denied", "tool": name, "args": mask_pii(args),
                          "reason": str(e), "origin": origin})
        return {"error": str(e), "denied": True}

    ctx.audit.record({"kind": "tool_call", "tool": name, "args": mask_pii(args),
                      "tier": spec["tier"], "origin": origin})
    a = args or {}

    if name == "load_transactions":
        t = _recent_txn(ctx, int(a.get("months", 3)), a.get("category"))
        rows = [sanitize_record_fields(r) for r in
                t.tail(MAX_ROWS).assign(date=lambda d: d.date.dt.strftime("%Y-%m-%d"))
                .to_dict("records")]
        return {"rows": mask_pii(rows), "count": int(len(t)),
                "span": [str(t.date.min().date()), str(t.date.max().date())]}

    if name == "get_cashflow_forecast":
        return ctx.forecast()

    if name == "get_kpis":
        return kpis_mod.kpi_summary(ctx.txn, ctx.invoices,
                                    months=int(a.get("months", 6)))

    if name == "get_alerts":
        return {"alerts": ctx.alerts(), "count": len(ctx.alerts())}

    if name == "match_financing":
        fc = ctx.forecast()
        gap = max(fc["gap_amount"], 0)
        res = fin_mod.match_financing(ctx.catalog, ctx.financing_ctx(),
                                      gap if gap > 0 else 20000, fc["gap_week"])
        res["disclaimer"] = DISCLAIMER
        return res

    if name == "get_evidence":
        return _evidence(ctx, a.get("kind", ""), a.get("ref", ""))

    if name == "what_if":
        return ctx.forecast(whatif=a, use_cache=False)

    if name == "draft_dunning_email":
        inv = ctx.invoices[ctx.invoices.id == a.get("invoice_id", "")]
        if inv.empty:
            return {"error": f"unknown invoice_id {a.get('invoice_id')}"}
        return sandbox.draft_dunning_email(inv.iloc[0].to_dict(),
                                           ctx.profile["business_name"])

    if name == "draft_loan_application":
        p = next((x for x in ctx.catalog if x["id"] == a.get("product_id")), None)
        if not p:
            return {"error": f"unknown product_id {a.get('product_id')}"}
        fc = ctx.forecast()
        gap = {"gap_amount": max(fc["gap_amount"], 1), "gap_week": fc["gap_week"]}
        return sandbox.draft_loan_application(p, ctx.profile, gap)

    # ---- Tier-2: never executes here — creates a pending approval ----
    try:
        if name == "reschedule_payment":
            return ctx.approvals.create(
                {"tool": "reschedule_payment",
                 "params": {"payment_id": a.get("payment_id"),
                            "new_date": a.get("new_date")},
                 "preview": _reschedule_preview(ctx, a)})

        if name == "record_sandbox_payment":
            return ctx.approvals.create(
                {"tool": "record_sandbox_payment",
                 "params": {"amount": float(a.get("amount", 0)),
                            "counterparty": a.get("counterparty", ""),
                            "memo": a.get("memo", "")},
                 "preview": f"Record simulated payment of "
                            f"${float(a.get('amount', 0)):,.2f} to "
                            f"{a.get('counterparty', '?')}"})

        if name == "set_cash_buffer":
            return ctx.approvals.create(
                {"tool": "set_cash_buffer",
                 "params": {"amount": float(a.get("amount", 0))},
                 "preview": f"Change low-balance threshold to "
                            f"${float(a.get('amount', 0)):,.0f}"})
    except ApprovalError as e:
        ctx.audit.record({"kind": "tool_denied", "tool": name,
                          "args": mask_pii(args), "reason": str(e),
                          "origin": origin})
        return {"error": str(e), "denied": True}

    return {"error": f"unhandled tool {name}"}


def _reschedule_preview(ctx: BusinessContext, a: dict) -> str:
    row = ctx.scheduled[ctx.scheduled.id == a.get("payment_id")]
    if row.empty:
        return f"Reschedule {a.get('payment_id')} → {a.get('new_date')}"
    r = row.iloc[0]
    return (f"Move {r.category} payment of ${r.amount:,.0f} "
            f"({r.counterparty}) from {r.next_date} to {a.get('new_date')}")


def execute_approved(ctx: BusinessContext, action: dict) -> dict:
    """Runs a consumed approval. Called ONLY by the API layer after the store
    validates the token — never reachable by the LLM directly."""
    name, p = action["tool"], action.get("params", {})
    if name == "reschedule_payment":
        res = sandbox.exec_reschedule_payment(ctx.scheduled, p["payment_id"],
                                              p["new_date"])
        if res.get("ok"):
            ctx.scheduled = sandbox.apply_reschedules(ctx.scheduled_raw)
            ctx._forecast_cache.clear()
        return res
    if name == "record_sandbox_payment":
        return sandbox.exec_record_sandbox_wire(p)
    if name == "set_cash_buffer":
        res = sandbox.exec_set_cash_buffer(p["amount"])
        if res.get("ok"):
            ctx.profile["cash_buffer"] = float(p["amount"])
            ctx._forecast_cache.clear()
        return res
    return {"ok": False, "error": f"unknown action tool {name}"}


def _evidence(ctx: BusinessContext, kind: str, ref: str) -> dict:
    if kind == "transaction":
        t = ctx.txn[ctx.txn.id == ref]
        rows = t.assign(date=lambda d: d.date.dt.strftime("%Y-%m-%d")).to_dict("records")
    elif kind == "invoice":
        rows = ctx.invoices[ctx.invoices.id == ref].to_dict("records")
    elif kind == "week":
        fc = ctx.forecast()
        w = next((x for x in fc["weeks"] if x["week"] == int(ref)), None)
        rows = {"forecast_week": w}
        if w:
            s, e = pd.Timestamp(w["start"]), pd.Timestamp(w["end"]) + pd.Timedelta(days=1)
            hist = ctx.txn[(ctx.txn.date >= s - pd.Timedelta(weeks=52))].tail(0)
            rows["note"] = "Committed rows shown in forecast_week.committed; modeled values are per-category forecasts."
    elif kind == "category":
        t = _recent_txn(ctx, 3, ref)
        rows = t.tail(60).assign(date=lambda d: d.date.dt.strftime("%Y-%m-%d")).to_dict("records")
    else:
        return {"error": f"unknown evidence kind {kind}"}
    return {"kind": kind, "ref": ref, "rows": mask_pii(rows)}

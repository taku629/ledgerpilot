"""Financing-option matching: score the product catalog against the forecast
funding gap. Produces ranked options with estimated total cost AND reasons
rejected options were not chosen. All amounts computed deterministically.
"""
import math

# requirement checkers: (requirement key) -> (business context) -> bool or reason
REQ_CHECKERS = {
    "months_operating": lambda req, ctx: ctx["months_operating"] >= req,
    "min_monthly_revenue": lambda req, ctx: ctx["avg_monthly_revenue"] >= req,
    "has_b2b_invoices": lambda req, ctx: ctx["has_b2b_invoices"] == req,
    "equipment_quote": lambda req, ctx: ctx.get("equipment_quote", True),
    "business_plan": lambda req, ctx: ctx.get("business_plan", True),
    "digital_project": lambda req, ctx: ctx.get("digital_project", True),
    "supplier_agreement": lambda req, ctx: ctx.get("supplier_agreement", True),
}
REQ_LABELS = {
    "months_operating": "requires {v}+ months operating (have {m})",
    "min_monthly_revenue": "requires ≥${v:,.0f}/mo revenue (have ${m:,.0f})",
    "has_b2b_invoices": "requires outstanding B2B invoices",
    "equipment_quote": "requires an equipment quote",
    "business_plan": "requires a written business plan",
    "digital_project": "requires a qualifying digitalization project",
    "supplier_agreement": "requires supplier willingness to extend terms",
}


def _monthly_payment(amount: float, apr: float, months: int) -> float:
    r = apr / 100 / 12
    return amount * r / (1 - (1 + r) ** -months) if r and months else amount / max(months, 1)


def estimated_cost(p: dict, needed: float, months: int) -> float:
    """Total cost of using `needed` of this product for `months` months."""
    amt = min(max(needed, p["amount_min"]), p["amount_max"])
    if p["type"] == "factoring":
        cycles = max(1, math.ceil(months))
        return amt * p["fee_pct_per_30d"] / 100 * cycles
    if p["type"] == "mca":
        return amt * (p["factor_rate"] - 1)
    if p["type"] == "trade_credit":
        return amt * p["fee_pct_per_30d"] / 100 * min(months, 2)
    if p["type"] == "grant":
        return 0.0
    # loans & credit lines: simple-interest approximation over the usage period
    return amt * p.get("apr", 0) / 100 * months / 12


def match_financing(catalog: list[dict], ctx: dict, gap_amount: float,
                    gap_week: int | None, months: int = 6) -> dict:
    """ctx: months_operating, avg_monthly_revenue, has_b2b_invoices, open_receivables,
    avg_weekly_free_cash. Returns ranked matches + rejections with reasons."""
    needed = max(float(gap_amount), 1.0)
    weeks_to_gap = gap_week or 13
    results, rejected = [], []
    for p in catalog:
        reasons = []
        if needed > p["amount_max"]:
            reasons.append(f"covers at most ${p['amount_max']:,.0f} < gap ${needed:,.0f}")
        if needed < p["amount_min"] * 0.5:
            reasons.append(f"gap ${needed:,.0f} far below minimum ${p['amount_min']:,.0f}")
        for rk, rv in (p.get("requirements") or {}).items():
            chk = REQ_CHECKERS.get(rk)
            if chk and not chk(rv, ctx):
                tmpl = REQ_LABELS.get(rk, "fails requirement " + rk)
                reasons.append(tmpl.format(v=rv, m=ctx.get(rk.replace("min_", ""), "?"))
                               if "{m}" in tmpl else tmpl.format(v=rv))
        if p["time_to_fund_days"] / 7 > weeks_to_gap:
            reasons.append(f"too slow: {p['time_to_fund_days']}d to fund vs gap in week {weeks_to_gap}")

        amt = min(max(needed, p["amount_min"]), p["amount_max"])
        cost = estimated_cost(p, needed, months)
        monthly = _monthly_payment(amt, p.get("apr", 0), p.get("term_months", 12)) \
            if p["type"] in ("term_loan", "equipment", "credit_line") else cost / max(months, 1)
        burden = monthly / max(ctx.get("avg_weekly_free_cash", 1) * 4.33, 1e-9)

        if reasons:
            rejected.append({"id": p["id"], "name": p["name"], "type": p["type"],
                             "reasons": reasons, "est_total_cost": round(cost, 2)})
            continue

        coverage = min(1.0, p["amount_max"] / needed)
        cost_norm = cost / needed                      # $ cost per $ borrowed
        speed = 1 - min(1.0, (p["time_to_fund_days"] / 7) / max(weeks_to_gap, 1))
        burden_pen = min(1.0, burden)
        score = 100 * (0.30 * coverage + 0.35 * max(0, 1 - cost_norm / 0.6)
                       + 0.20 * speed + 0.15 * (1 - burden_pen))
        results.append({
            "id": p["id"], "name": p["name"], "type": p["type"],
            "score": round(score, 1),
            "amount": round(amt, 2),
            "est_total_cost": round(cost, 2),
            "est_monthly_payment": round(monthly, 2),
            "apr_or_fee": p.get("apr", f"{p.get('fee_pct_per_30d', p.get('factor_rate', ''))}%/30d"
                                if p["type"] in ("factoring", "trade_credit") else
                                f"factor {p.get('factor_rate')}" if p["type"] == "mca" else "0%"),
            "time_to_fund_days": p["time_to_fund_days"],
            "coverage_of_gap_pct": round(coverage * 100),
            "notes": p["notes"],
            "why": _why(p, coverage, cost_norm, speed, burden, weeks_to_gap),
        })
    results.sort(key=lambda r: -r["score"])
    return {"gap_amount": round(needed, 2), "gap_week": gap_week,
            "ranked": results, "rejected": rejected,
            "disclaimer": "Informational comparison only — not financial advice."}


def _why(p, coverage, cost_norm, speed, burden, weeks_to_gap) -> str:
    bits = []
    if cost_norm <= 0.12:
        bits.append("low cost")
    elif cost_norm >= 0.3:
        bits.append("expensive for the amount")
    if p["time_to_fund_days"] <= 3:
        bits.append("funds fast enough for the gap")
    if coverage >= 1:
        bits.append("fully covers the gap")
    elif coverage < 0.9:
        bits.append(f"covers only {coverage * 100:.0f}% of the gap")
    if burden > 0.5:
        bits.append("monthly payment is heavy vs free cash flow")
    return "; ".join(bits) or "acceptable fit on cost, speed and coverage"

"""Business financial analysis: monthly P/L, margins, fixed-cost ratio,
working-capital days (DSO / DPO / inventory days), and month-over-month
change drivers. All numbers are computed here — the LLM only narrates them.
"""
import numpy as np
import pandas as pd

REVENUE_CATS = ["POS Sales", "Online Sales", "Wholesale Invoice"]
COGS_CATS = ["Coffee & Supplies", "Packaging & Shipping"]
FIXED_CATS = ["Rent", "Payroll", "Insurance", "Loan Repayment", "Marketing"]


def monthly_pl(txn: pd.DataFrame, months: int = 6) -> list[dict]:
    t = txn.copy()
    t["month"] = t.date.dt.to_period("M").astype(str)
    last_months = sorted(t.month.unique())[-months:]
    t = t[t.month.isin(last_months)]
    piv = t.pivot_table(index="month", columns=["flow", "category"],
                        values="amount", aggfunc="sum", fill_value=0.0)
    out = []
    for m in last_months:
        rev = sum(float(piv.loc[m].get(("inflow", c), 0.0)) for c in REVENUE_CATS)
        cogs = sum(float(piv.loc[m].get(("outflow", c), 0.0)) for c in COGS_CATS)
        fixed = sum(float(piv.loc[m].get(("outflow", c), 0.0)) for c in FIXED_CATS)
        tot_out = float(piv.loc[m].xs("outflow", level=0).sum()) if m in piv.index else 0.0
        net = rev - tot_out
        out.append({
            "month": m, "partial": pd.Period(m) == pd.Timestamp.today().to_period("M"),
            "revenue": round(rev, 2), "cogs": round(cogs, 2),
            "gross_profit": round(rev - cogs, 2),
            "gross_margin_pct": round((rev - cogs) / rev * 100, 1) if rev else None,
            "fixed_costs": round(fixed, 2),
            "fixed_cost_ratio_pct": round(fixed / rev * 100, 1) if rev else None,
            "total_outflow": round(tot_out, 2), "net_cashflow": round(net, 2),
        })
    return out


def working_capital_days(txn: pd.DataFrame, invoices: pd.DataFrame,
                         as_of=None, window_days: int = 90) -> dict:
    """DSO from invoice ledger; DPO & inventory days approximated from spend.
    Definitions are documented in TECHNICAL_DOCUMENTATION.md."""
    as_of = as_of or txn.date.max()
    win_start = as_of - pd.Timedelta(days=window_days)
    t = txn[txn.date > win_start]
    days = window_days

    # DSO: collected revenue -> average collection days on invoices paid in window
    inv = invoices.copy()
    inv["paid_date"] = pd.to_datetime(inv.paid_date, errors="coerce")
    inv["due_date"] = pd.to_datetime(inv.due_date)
    inv["issue_date"] = pd.to_datetime(inv.issue_date)
    paid = inv[(inv.paid_date.notna()) & (inv.paid_date > win_start)]
    dso = float((paid.paid_date - paid.issue_date).dt.days
                .mul(paid.amount).sum() / max(paid.amount.sum(), 1e-9)) if len(paid) else 0.0
    open_amt = float(inv[inv.status != "paid"].amount.sum())
    overdue = inv[(inv.status == "overdue") |
                  ((inv.status == "open") & (inv.due_date < as_of))]
    # DPO: supplier spend vs average AP (approximated as ~2 weeks of supplier spend outstanding)
    sup_spend = float(t[(t.flow == "outflow") & (t.category.isin(COGS_CATS))].amount.sum())
    ap_proxy = sup_spend / days * 14
    dpo = float(ap_proxy / max(sup_spend / days, 1e-9))
    # Inventory days: avg inventory ~ 3 weeks of supply spend held (cafe keeps beans on hand)
    inv_proxy = sup_spend / days * 21
    dio = float(inv_proxy / max(sup_spend / days, 1e-9))

    rev = float(t[t.flow == "inflow"].amount.sum())
    return {"window_days": days,
            "dso": round(dso, 1), "dpo": round(dpo, 1), "dio": round(dio, 1),
            "cash_conversion_cycle": round(dso + dio - dpo, 1),
            "revenue_in_window": round(rev, 2),
            "open_receivables": round(open_amt, 2),
            "overdue_receivables": round(float(overdue.amount.sum()), 2),
            "overdue_count": int(len(overdue)),
            "overdue_invoices": overdue[["id", "customer", "due_date", "amount"]]
            .assign(due_date=lambda d: d.due_date.dt.strftime("%Y-%m-%d"))
            .to_dict("records")}


def mom_drivers(pl: list[dict], txn: pd.DataFrame) -> list[dict]:
    """Top contributors to net-cashflow change between the last two COMPLETE
    months (the current partial month is excluded)."""
    today = pd.Timestamp.today()
    complete = [p for p in pl
                if pd.Period(p["month"]) < today.to_period("M")]
    if len(complete) < 2:
        return []
    prev, cur = complete[-2], complete[-1]
    t = txn.copy()
    t["month"] = t.date.dt.to_period("M").astype(str)
    mm = t[t.month.isin([prev["month"], cur["month"]])]
    cat = mm.pivot_table(index="month", columns="category", values="amount",
                         aggfunc="sum", fill_value=0.0)
    drivers = []
    for c in cat.columns:
        delta = float(cat.loc[cur["month"], c] - cat.loc[prev["month"], c])
        flow = mm[mm.category == c].flow.iloc[0]
        impact = delta if flow == "inflow" else -delta  # + = good for cash
        drivers.append({"category": c, "flow": flow,
                        "prev": round(float(cat.loc[prev["month"], c]), 2),
                        "cur": round(float(cat.loc[cur["month"], c]), 2),
                        "impact_on_net": round(impact, 2)})
    drivers.sort(key=lambda d: abs(d["impact_on_net"]), reverse=True)
    return drivers[:6]


def kpi_summary(txn, invoices, months=6):
    pl = monthly_pl(txn, months)
    wc = working_capital_days(txn, invoices)
    return {"monthly_pl": pl, "working_capital": wc,
            "mom_drivers": mom_drivers(pl, txn)}

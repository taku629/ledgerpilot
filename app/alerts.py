"""Alert detection rules over forecast output, AR ledger and transactions."""
import pandas as pd

from .security import scan_for_injection


def detect_alerts(forecast: dict, invoices: pd.DataFrame, txn: pd.DataFrame,
                  profile: dict) -> list[dict]:
    alerts = []
    buffer = float(profile.get("cash_buffer", 0))

    # 1. projected balance below buffer — one grouped alert, all breach weeks
    breach = [w for w in forecast["weeks"] if w["balance_p50"] < buffer]
    if breach:
        worst = min(breach, key=lambda w: w["balance_p50"])
        weeks_l = ", ".join(str(w["week"]) for w in breach)
        alerts.append({
            "id": "lowbal", "type": "LOW_BALANCE",
            "severity": "critical" if worst["balance_p50"] < 0 else "high",
            "title": f"Projected balance falls below the ${buffer:,.0f} buffer "
                     f"in {len(breach)} weeks — first breach week {breach[0]['week']}",
            "detail": f"Worst point ${worst['balance_p50']:,.0f} in week {worst['week']} "
                      f"({worst['start']}). Breach weeks: {weeks_l}.",
            "evidence": {"weeks": [
                {"week": w["week"], "start": w["start"], "end": w["end"],
                 "balance_p50": w["balance_p50"], "inflow_p50": w["inflow_p50"],
                 "outflow": w["outflow_modeled"] + w["outflow_committed"],
                 "committed": w["committed"]}
                for w in breach]},
        })

    # 2. clustered large committed payments in one week (>25% of typical monthly outflow)
    mo_out = txn[txn.flow == "outflow"].amount.sum()
    typical_month = mo_out / max(len(pd.period_range(txn.date.min().to_period("M"),
                                                     txn.date.max().to_period("M"))), 1)
    for w in forecast["weeks"]:
        if w["outflow_committed"] > 0.35 * typical_month and len(w["committed"]) >= 2:
            alerts.append({
                "id": f"cluster-w{w['week']}", "type": "PAYMENT_CLUSTER", "severity": "medium",
                "title": f"${w['outflow_committed']:,.0f} of committed payments land "
                         f"in week {w['week']}",
                "detail": "; ".join(f"{c['category']} ${c['amount']:,.0f} ({c['date']})"
                                    for c in w["committed"]),
                "evidence": {"week": w["week"], "committed": w["committed"]},
            })

    # 3. overdue / late receivables
    inv = invoices.copy()
    inv["due_date"] = pd.to_datetime(inv.due_date)
    today = pd.Timestamp(forecast["as_of"])
    over = inv[(inv.status == "overdue") | ((inv.status == "open") & (inv.due_date < today))]
    for _, r in over.iterrows():
        days = int((today - r.due_date).days)
        alerts.append({
            "id": f"ar-{r.id}", "type": "AR_OVERDUE", "severity": "high" if days > 14 else "medium",
            "title": f"{r.customer} invoice {r.id} is {days}d overdue (${r.amount:,.0f})",
            "detail": f"Due {r.due_date:%Y-%m-%d}; still unpaid. Consider a payment reminder.",
            "evidence": {"invoice_id": r.id, "customer": r.customer,
                         "due_date": r.due_date.strftime("%Y-%m-%d"),
                         "amount": r.amount, "days_overdue": days},
        })

    # 4. instruction-like text inside financial records (injection watch)
    for _, r in txn.iterrows():
        scan = scan_for_injection(str(r.memo) + " " + str(r.counterparty))
        if scan["flagged"]:
            alerts.append({
                "id": f"inj-{r.id}", "type": "INJECTION_ATTEMPT", "severity": "medium",
                "title": f"Suspicious instruction-like text in memo of {r.id}",
                "detail": "Matched: " + "; ".join(scan["matches"])
                          + ". Treated as inert data — no action taken.",
                "evidence": {"transaction_id": r.id, "memo": r.memo},
            })

    # 5. unusual single outflows vs category history (z > 2.5)
    out = txn[txn.flow == "outflow"]
    for cat, g in out.groupby("category"):
        if len(g) < 12:
            continue
        mu, sd = g.amount.mean(), g.amount.std()
        last = g.sort_values("date").iloc[-1]
        if sd and (last.amount - mu) / sd > 2.5:
            alerts.append({
                "id": f"anom-{last.id}", "type": "UNUSUAL_OUTFLOW", "severity": "low",
                "title": f"Unusually large {cat} payment: ${last.amount:,.0f}",
                "detail": f"{(last.amount - mu) / sd:.1f}σ above the {cat} average "
                          f"(${mu:,.0f}).",
                "evidence": {"transaction_id": last.id, "category": cat,
                             "amount": last.amount},
            })

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    alerts.sort(key=lambda a: (order.get(a["severity"], 4), a["id"]))
    return alerts

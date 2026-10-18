"""13-week cash-flow forecast.

Design: committed outflows come from the payment calendar (scheduled_payments);
discretionary categories (sales, supplies, packaging, wholesale collections) are
forecast per-category on weekly buckets with a recursive LightGBM model
(lag + calendar features). Prediction intervals come from backtest residual
quantiles. If LightGBM is unavailable or too little history exists, a seasonal
moving-average fallback is used instead. A holdout backtest reports WAPE vs a
seasonal-naive baseline so the model's edge is measured, not asserted.

Forecasting lineage: adapted from the StockSense demand-forecasting approach
(quantile gradient boosting with leakage-safe features + naive-baseline backtest).
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

FORECAST_WEEKS = 13
BACKTEST_WEEKS = 8
# categories forecast by the statistical model; everything else comes from the payment calendar
MODELED_INFLOW = ["POS Sales", "Online Sales", "Wholesale Invoice"]
MODELED_OUTFLOW = ["Coffee & Supplies", "Packaging & Shipping"]

try:
    import lightgbm as lgb
    _HAS_LGBM = True
except Exception:  # pragma: no cover - depends on env
    _HAS_LGBM = False


def _week_index(dates: pd.Series, end: date) -> pd.Series:
    """0 = most recent complete 7-day bucket ending at `end`, increasing backwards."""
    return ((pd.Timestamp(end) - dates).dt.days // 7).astype(int)


def weekly_series(txn: pd.DataFrame, end: date) -> pd.DataFrame:
    """week_idx x category matrix of signed amounts (inflow +, outflow -)."""
    t = txn.copy()
    t["w"] = _week_index(t.date, end)
    t["signed"] = t.amount * t.flow.map({"inflow": 1.0, "outflow": -1.0})
    m = t.pivot_table(index="w", columns="category", values="signed",
                      aggfunc="sum", fill_value=0.0)
    m.index.name = "w"
    return m.sort_index(ascending=False)  # w=0 last row -> chronological


def _calendar_feats(week_start: pd.Timestamp) -> dict:
    return {"month": week_start.month, "wom": (week_start.day - 1) // 7 + 1,
            "woy": int(week_start.isocalendar().week)}


def _build_frame(series: np.ndarray, week0_start: pd.Timestamp) -> pd.DataFrame:
    """Feature frame for one category's chronological weekly series."""
    feats, n = [], len(series)
    for i in range(4, n):
        row = {"t": i}
        for lag in (1, 2, 3, 4, 8, 13):
            row[f"lag{lag}"] = series[i - lag] if i - lag >= 0 else np.nan
        row["rmean4"] = np.mean(series[max(0, i - 4):i])
        row["rmean8"] = np.mean(series[max(0, i - 8):i])
        row["rstd8"] = np.std(series[max(0, i - 8):i])
        row.update(_calendar_feats(week0_start + pd.Timedelta(weeks=i)))
        feats.append(row)
    return pd.DataFrame(feats)


def _fit_lgbm(feats: pd.DataFrame, y: np.ndarray):
    m = lgb.LGBMRegressor(n_estimators=300, learning_rate=0.06, num_leaves=15,
                          min_child_samples=8, subsample=0.9, colsample_bytree=0.9,
                          verbose=-1)
    m.fit(feats, y)
    return m


FEATS = ["t", "lag1", "lag2", "lag3", "lag4", "lag8", "lag13",
         "rmean4", "rmean8", "rstd8", "month", "wom", "woy"]


def _forecast_category(series: np.ndarray, week0_start: pd.Timestamp,
                       horizon: int) -> dict:
    """Recursive 13-week forecast for one weekly series.
    Returns p50 path, residual-quantile band, and WAPE backtest vs seasonal naive."""
    series = series.astype(float)
    n = len(series)
    if n < 18 or np.allclose(series, 0):
        mu = float(np.mean(series)) if n else 0.0
        return {"p50": np.repeat(max(mu, 0.0), horizon),
                "band": np.repeat(max(np.std(series), mu * 0.15, 1.0), horizon),
                "wape_model": None, "wape_naive": None, "method": "level"}

    # --- backtest: last BACKTEST_WEEKS as holdout ---
    tr, te = series[:-BACKTEST_WEEKS], series[-BACKTEST_WEEKS:]
    naive_pred = np.array([np.mean(tr[i % 4::4]) for i in range(len(te))]) \
        if len(tr) >= 4 else np.repeat(np.mean(tr), len(te))
    if _HAS_LGBM:
        ftr = _build_frame(tr, week0_start)
        mb = _fit_lgbm(ftr[FEATS].fillna(0), tr[len(tr) - len(ftr):])
        # recursive prediction over the holdout
        hist = list(tr)
        bt_pred = []
        start0 = week0_start
        for i in range(len(te)):
            t_i = len(hist)
            row = {"t": t_i}
            for lag in (1, 2, 3, 4, 8, 13):
                row[f"lag{lag}"] = hist[t_i - lag] if t_i - lag >= 0 else 0.0
            row["rmean4"] = np.mean(hist[-4:]); row["rmean8"] = np.mean(hist[-8:])
            row["rstd8"] = np.std(hist[-8:])
            row.update(_calendar_feats(start0 + pd.Timedelta(weeks=t_i)))
            p = float(mb.predict(pd.DataFrame([row])[FEATS])[0])
            p = max(p, 0.0)
            bt_pred.append(p)
            hist.append(te[i])  # backtest uses actuals to continue (walk-forward)
        bt_pred = np.array(bt_pred)
        method = "lightgbm"
    else:
        bt_pred = naive_pred
        method = "seasonal_ma"
    if abs(te.sum()) < 500:  # too little signal in the holdout to score meaningfully
        wape_model = wape_naive = None
    else:
        wape_model = float(np.abs(te - bt_pred).sum() / np.abs(te).sum() * 100)
        wape_naive = float(np.abs(te - naive_pred).sum() / np.abs(te).sum() * 100)

    # --- residual quantiles -> prediction band ---
    resid = te - bt_pred
    q_lo, q_hi = float(np.quantile(resid, 0.1)), float(np.quantile(resid, 0.9))
    band = float(max(q_hi - q_lo, np.std(series) * 0.4, 1.0)) / 2

    # --- final model on full history, recursive 13-week path ---
    hist = list(series)
    path = []
    if _HAS_LGBM:
        ff = _build_frame(series, week0_start)
        mf = _fit_lgbm(ff[FEATS].fillna(0), series[len(series) - len(ff):])
    for i in range(horizon):
        t_i = len(hist)
        row = {"t": t_i}
        for lag in (1, 2, 3, 4, 8, 13):
            row[f"lag{lag}"] = hist[t_i - lag] if t_i - lag >= 0 else 0.0
        row["rmean4"] = np.mean(hist[-4:]); row["rmean8"] = np.mean(hist[-8:])
        row["rstd8"] = np.std(hist[-8:])
        row.update(_calendar_feats(week0_start + pd.Timedelta(weeks=t_i)))
        if _HAS_LGBM:
            p = max(float(mf.predict(pd.DataFrame([row])[FEATS])[0]), 0.0)
        else:
            p = float(np.mean(series[(t_i % 4)::4][-8:])) if len(series) >= 8 else float(np.mean(series))
            p = max(p, 0.0)
        path.append(p)
        hist.append(p)
    return {"p50": np.array(path), "band": np.repeat(band, horizon),
            "wape_model": round(wape_model, 1) if wape_model is not None else None,
            "wape_naive": round(wape_naive, 1) if wape_naive is not None else None,
            "method": method}


def _project_scheduled(sched: pd.DataFrame, end: date, horizon: int) -> pd.DataFrame:
    """Expand committed outflows onto the future week grid (week 1..horizon)."""
    rows = []
    horizon_end = end + timedelta(weeks=horizon)
    for _, r in sched.iterrows():
        d = pd.Timestamp(r.next_date).date()
        step = {"monthly": 28, "biweekly": 14, "quarterly": 90,
                "one_off": None, "weekly": 7}.get(r.frequency, None)
        while d <= horizon_end:
            if d > end:
                wk = math.ceil((d - end).days / 7)
                rows.append({"week": wk, "date": d.isoformat(), "category": r.category,
                             "counterparty": r.counterparty, "amount": float(r.amount),
                             "payment_id": r.id, "memo": r.memo})
            if step is None:
                break
            # monthly = same day next month, not +28d
            if r.frequency == "monthly":
                nxt = (d.replace(day=28) + timedelta(days=8)).replace(day=min(d.day, 28))
            elif r.frequency == "quarterly":
                nxt = d + timedelta(days=90)
            else:
                nxt = d + timedelta(days=step)
            d = nxt
    return pd.DataFrame(rows)


def forecast_cashflow(txn: pd.DataFrame, sched: pd.DataFrame, profile: dict,
                      end: date | None = None, weeks: int = FORECAST_WEEKS,
                      whatif: dict | None = None) -> dict:
    """Returns weekly forecast rows + balance path + backtest summary.

    whatif keys (all optional): sales_pct, supplies_pct, ar_delay_weeks,
    extra_outflow {amount, week, label}, extra_inflow {amount, week, label}.
    """
    whatif = whatif or {}
    end = end or txn.date.max().date()
    wk = weekly_series(txn, end)
    week0_start = pd.Timestamp(end) - pd.Timedelta(days=6)
    hist_start_weeks = min(26, wk.shape[0])

    sales_cats = {"POS Sales", "Online Sales", "Wholesale Invoice"}
    sales_mult = 1.0 + float(whatif.get("sales_pct", 0)) / 100
    sup_mult = 1.0 + float(whatif.get("supplies_pct", 0)) / 100
    ar_delay = int(whatif.get("ar_delay_weeks", 0))

    modeled, backtests = [], {}
    for flow, cats in (("inflow", MODELED_INFLOW), ("outflow", MODELED_OUTFLOW)):
        for cat in cats:
            s = (wk[cat].abs() if cat in wk.columns else pd.Series(dtype=float))
            s = s.tail(hist_start_weeks).to_numpy()
            r = _forecast_category(s, week0_start, weeks)
            band = r["band"]
            p50 = r["p50"].copy()
            if flow == "inflow" and cat in sales_cats:
                p50 *= sales_mult
            if flow == "outflow":
                p50 *= sup_mult
            if cat == "Wholesale Invoice" and ar_delay:
                p50 = np.concatenate([np.zeros(ar_delay), p50[:-ar_delay]])
            modeled.append({"flow": flow, "category": cat, "p50": p50, "band": band})
            if r["wape_model"] is not None:
                backtests[cat] = {"model": r["method"], "wape_model": r["wape_model"],
                                  "wape_seasonal_naive": r["wape_naive"]}

    sched_rows = _project_scheduled(sched, end, weeks)
    sched_by_week = sched_rows.groupby("week").amount.sum().to_dict() \
        if len(sched_rows) else {}

    extra_out = whatif.get("extra_outflow") or {}
    extra_in = whatif.get("extra_inflow") or {}

    out_weeks, balance = [], float(profile["current_balance"])
    for w in range(1, weeks + 1):
        start_d = end + timedelta(days=7 * (w - 1) + 1)
        inflow = sum(m["p50"][w - 1] for m in modeled if m["flow"] == "inflow")
        outflow_m = sum(m["p50"][w - 1] for m in modeled if m["flow"] == "outflow")
        outflow_c = float(sched_by_week.get(w, 0.0))
        if extra_out.get("week") == w:
            outflow_c += float(extra_out.get("amount", 0))
        if extra_in.get("week") == w:
            inflow += float(extra_in.get("amount", 0))
        band2 = sum(m["band"][w - 1] ** 2 for m in modeled) ** 0.5
        net = inflow - outflow_m - outflow_c
        balance += net
        out_weeks.append({
            "week": w, "start": start_d.isoformat(),
            "end": (start_d + timedelta(days=6)).isoformat(),
            "inflow_p50": round(inflow, 2),
            "outflow_modeled": round(outflow_m, 2),
            "outflow_committed": round(outflow_c, 2),
            "net_p50": round(net, 2),
            "balance_p50": round(balance, 2),
            "balance_p10": round(balance - band2, 2),
            "balance_p90": round(balance + band2, 2),
            "committed": sched_rows[sched_rows.week == w][
                ["date", "category", "counterparty", "amount", "payment_id", "memo"]
            ].to_dict("records") if len(sched_rows) else [],
        })

    gap_week = next((w for w in out_weeks
                     if w["balance_p50"] < float(profile.get("cash_buffer", 0))), None)
    return {"as_of": end.isoformat(), "weeks": out_weeks,
            "min_balance": round(min(w["balance_p50"] for w in out_weeks), 2),
            "gap_week": gap_week["week"] if gap_week else None,
            "gap_amount": round(float(profile.get("cash_buffer", 0))
                                - min(w["balance_p50"] for w in out_weeks), 2),
            "backtest": backtests,
            "balance_start": float(profile["current_balance"]),
            "cash_buffer": float(profile.get("cash_buffer", 0))}

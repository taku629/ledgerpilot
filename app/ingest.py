"""Bank-statement CSV ingestion: parse → validate → categorize → merge.

Security notes
--------------
* Hard caps (1 MB / 5,000 rows) — a malicious or malformed file can't DoS the
  forecast engine.
* Everything is data, never code: memos are scanned with the same injection
  scanner used on transaction imports, and stored PII-masked.
* `mode="replace"` first snapshots the current ledger to
  transactions_backup.csv so /api/dev/regenerate (or a restore) can't lose it.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pandas as pd

from .security import mask_pii, scan_for_injection

MAX_BYTES = 1_000_000
MAX_ROWS = 5000

_DATE_COLS = ("date", "posted", "posting_date", "transaction_date", "value_date")
_AMT_COLS = ("amount", "value", "amt", "total")
_CREDIT_COLS = ("credit", "deposit", "inflow", "money_in")
_DEBIT_COLS = ("debit", "withdrawal", "outflow", "money_out")
_CP_COLS = ("counterparty", "description", "memo", "payee", "name",
            "merchant", "reference", "details")
_TYPE_COLS = ("type", "flow", "dr_cr", "direction")

CATEGORY_RULES = [
    ("Rent", r"rent|lease|landlord"),
    ("Payroll", r"payroll|salary|wages|gusto|adp\b|employee"),
    ("Utilities", r"utilit|electric|water|gas bill|internet|phone bill|telecom"),
    ("Insurance", r"insur"),
    ("Tax", r"\btax\b|irs|vat|withholding"),
    ("Loan Repayment", r"loan|debt service|repayment|kabbage"),
    ("Marketing", r"ads?\b|marketing|facebook|google ad|meta\b|promotion"),
    ("Delivery Platform Fees", r"uber ?eats|doordash|deliveroo|platform fee|commission"),
    ("Software & POS Fees", r"software|saas|subscription|stripe fee|square|pos\b|shopify|aws|cloud"),
    ("Coffee & Supplies", r"supplies|beans|coffee|milk|wholesale|inventory|costco|supplier"),
    ("Equipment", r"equipment|machine|roaster|hardware|fixture"),
    ("Repairs & Maintenance", r"repair|maintenance|plumb|fix"),
    ("Professional Services", r"accountant|legal|consult|lawyer|professional|notary"),
    ("Revenue", r"sale|revenue|payment from|payout|settlement|customer|order"),
]


def _find_col(cols_lower: dict, cands) -> str | None:
    for c in cands:
        if c in cols_lower:
            return cols_lower[c]
    return None


def categorize(text: str) -> str:
    t = (text or "").lower()
    for cat, pat in CATEGORY_RULES:
        if re.search(pat, t):
            return cat
    return "Other"


class IngestError(ValueError):
    pass


def parse_bank_csv(raw: bytes) -> pd.DataFrame:
    """Raw bytes → normalized txn frame (id,date,flow,category,counterparty,
    amount,memo). Raises IngestError on anything unusable."""
    if len(raw) > MAX_BYTES:
        raise IngestError(f"file too large ({len(raw):,} bytes, max {MAX_BYTES:,})")
    if not raw.strip():
        raise IngestError("empty file")
    try:
        df = pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(io.BytesIO(raw), encoding="cp932")
    except Exception as e:
        raise IngestError(f"not a readable CSV: {e}")
    if len(df) > MAX_ROWS:
        raise IngestError(f"{len(df)} rows exceeds the {MAX_ROWS} limit")
    if df.empty:
        raise IngestError("CSV has a header but no rows")

    cols_lower = {str(c).strip().lower(): c for c in df.columns}
    date_col = _find_col(cols_lower, _DATE_COLS)
    amt_col = _find_col(cols_lower, _AMT_COLS)
    credit_col = _find_col(cols_lower, _CREDIT_COLS)
    debit_col = _find_col(cols_lower, _DEBIT_COLS)
    cp_col = _find_col(cols_lower, _CP_COLS)
    type_col = _find_col(cols_lower, _TYPE_COLS)
    cat_col = _find_col(cols_lower, ("category", "cat"))

    if not date_col:
        raise IngestError(
            f"no date column found (tried {_DATE_COLS}); got {list(df.columns)}")
    if not amt_col and not (credit_col or debit_col):
        raise IngestError(
            "no amount column — provide 'amount' (signed) or credit/debit columns")

    out = pd.DataFrame()
    d = pd.to_datetime(df[date_col], errors="coerce", format="mixed")
    bad = int(d.isna().sum())
    if bad > len(df) * 0.1 or bad == len(df):
        raise IngestError(f"{bad}/{len(df)} dates unparseable — expected YYYY-MM-DD style")
    out["date"] = d.dt.strftime("%Y-%m-%d")

    def _num(col):
        return pd.to_numeric(
            df[col].astype(str).str.replace(r"[$,¥¥￥ ]", "", regex=True),
            errors="coerce").fillna(0)

    if amt_col:
        amt = _num(amt_col)
        if type_col:
            t = df[type_col].astype(str).str.lower()
            sign = t.str.contains("credit|inflow|deposit|cr\b", regex=True).map(
                {True: 1, False: -1})
            amt = amt.abs() * sign
        flow = amt.map(lambda a: "inflow" if a >= 0 else "outflow")
        out["amount"] = amt.abs()
    else:
        cr = _num(credit_col) if credit_col else 0
        dr = _num(debit_col) if debit_col else 0
        amt = cr - dr
        flow = amt.map(lambda a: "inflow" if a >= 0 else "outflow")
        out["amount"] = amt.abs()

    if (out["amount"] > 1e9).any():
        raise IngestError("amounts > $1B look like a parse error — check the file")
    out = out[out["amount"] > 0]
    out["flow"] = flow.loc[out.index]
    cp = df[cp_col].astype(str) if cp_col else "imported"
    out["counterparty"] = cp.loc[out.index].str.slice(0, 80).map(mask_pii)
    out["memo"] = out["counterparty"]
    out["category"] = (df[cat_col].loc[out.index].astype(str)
                       if cat_col else out["counterparty"]).map(categorize)
    out["id"] = [f"csv-{i + 1}" for i in range(len(out))]
    out["invoice_id"] = ""
    out["payment_id"] = ""
    return out[["id", "date", "flow", "category", "counterparty",
                "amount", "memo", "invoice_id", "payment_id"]]


def merge_transactions(existing: pd.DataFrame, new: pd.DataFrame,
                       mode: str, data_dir: Path) -> dict:
    """Append or replace. Returns the merge stats."""
    flagged = []
    for memo in new["memo"].dropna().unique()[:500]:
        s = scan_for_injection(str(memo), channel="data")
        if s["flagged"]:
            flagged.append({"memo": str(memo)[:60], "matches": s["matches"]})

    if mode == "replace":
        existing.to_csv(data_dir / "transactions_backup.csv", index=False)
        merged = new.copy()
        dupes = 0
    else:
        key = ["date", "amount", "counterparty"]
        e_keys = set(map(tuple, existing[key].astype(str).values))
        is_dup = new[key].astype(str).apply(tuple, axis=1).isin(e_keys)
        dupes = int(is_dup.sum())
        merged = pd.concat([existing, new[~is_dup]], ignore_index=True)

    merged["date"] = pd.to_datetime(merged["date"])
    merged = merged.sort_values("date").reset_index(drop=True)
    merged.to_csv(data_dir / "transactions.csv", index=False)
    return {"added": int(len(merged) - (len(existing) if mode == "append" else 0)),
            "skipped_duplicates": dupes,
            "flagged_memos": flagged,
            "total": int(len(merged)),
            "span": [str(merged.date.min().date()), str(merged.date.max().date())]}

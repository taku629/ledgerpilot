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

_DATE_COLS = ("date", "posted", "posting_date", "transaction_date", "value_date",
              "日付", "取引日")
_AMT_COLS = ("amount", "value", "amt", "total", "金額")
_CREDIT_COLS = ("credit", "deposit", "inflow", "money_in", "入金", "お預り金額")
_DEBIT_COLS = ("debit", "withdrawal", "outflow", "money_out", "出金", "お引出金額")
_CP_COLS = ("counterparty", "description", "memo", "payee", "name", "摘要", "内容",
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


def _norm_header(h) -> str:
    h = re.sub(r"[^0-9a-z]+", "_", str(h).strip().lower()).strip("_")
    return h[:-1] if h.endswith("s") and len(h) > 3 else h


def _find_col(cols_lower: dict, cands) -> str | None:
    for c in cands:
        if c in cols_lower:
            return cols_lower[c]
    normed = {_norm_header(k): v for k, v in cols_lower.items()}
    for c in cands:
        if _norm_header(c) in normed:
            return normed[_norm_header(c)]
    for c in cands:            # "Posted Date" → posted, "Txn Amount" → amount
        for k, v in normed.items():
            if c in k.split("_"):
                return v
    return None


def categorize(text: str) -> str:
    t = (text or "").lower()
    for cat, pat in CATEGORY_RULES:
        if re.search(pat, t):
            return cat
    return "Other"


class IngestError(ValueError):
    pass


MAPPING_FIELDS = ("date", "amount", "credit", "debit", "description",
                  "type", "category")
_ALIASES = {"date": _DATE_COLS, "amount": _AMT_COLS, "credit": _CREDIT_COLS,
            "debit": _DEBIT_COLS, "description": _CP_COLS, "type": _TYPE_COLS,
            "category": ("category", "cat")}


def read_csv_bytes(raw: bytes) -> pd.DataFrame:
    """Format/size/count validation → raw DataFrame (all columns as str)."""
    if len(raw) > MAX_BYTES:
        raise IngestError(f"file too large ({len(raw):,} bytes, max {MAX_BYTES:,})")
    if not raw.strip():
        raise IngestError("empty file")
    if b"\x00" in raw[:8192]:
        raise IngestError("binary file — upload a plain-text .csv export")
    if raw[:4] in (b"PK\x03\x04", b"%PDF") or raw[:2] == b"\xd0\xcf":
        raise IngestError("looks like a spreadsheet/PDF — export it as .csv first")
    try:
        df = pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig", dtype=str,
                         keep_default_na=False)
    except UnicodeDecodeError:
        try:
            df = pd.read_csv(io.BytesIO(raw), encoding="cp932", dtype=str,
                             keep_default_na=False)
        except Exception as e:
            raise IngestError(f"not a readable CSV: {e}")
    except Exception as e:
        raise IngestError(f"not a readable CSV: {e}")
    if len(df.columns) < 2:
        raise IngestError("CSV needs at least 2 columns (date + amount)")
    if len(df) > MAX_ROWS:
        raise IngestError(f"{len(df)} rows exceeds the {MAX_ROWS} limit")
    if df.empty:
        raise IngestError("CSV has a header but no rows")
    return df


def detect_mapping(df: pd.DataFrame, overrides: dict | None = None) -> dict:
    """Field → source-column mapping: alias auto-detect, then explicit user
    overrides (validated against the real header)."""
    cols_lower = {str(c).strip().lower(): c for c in df.columns}
    mapping = {f: _find_col(cols_lower, _ALIASES[f]) for f in MAPPING_FIELDS}
    taken = {mapping["date"]}
    for f in MAPPING_FIELDS[1:]:          # one source column feeds one field
        if mapping[f] in taken:
            mapping[f] = None
        elif mapping[f]:
            taken.add(mapping[f])
    for f, col in (overrides or {}).items():
        if f not in MAPPING_FIELDS:
            raise IngestError(f"unknown mapping field '{f}'")
        if col in (None, ""):
            continue
        if col == "-":
            mapping[f] = None
            continue
        if col not in df.columns:
            raise IngestError(f"mapping {f}→'{col}': no such column; "
                              f"got {list(df.columns)}")
        mapping[f] = col
    return mapping


def parse_bank_csv(raw: bytes, mapping: dict | None = None) -> pd.DataFrame:
    """Raw bytes → normalized txn frame (id,date,flow,category,counterparty,
    amount,memo). Raises IngestError on anything unusable."""
    df = read_csv_bytes(raw)
    return normalize(df, detect_mapping(df, mapping))


def normalize(df: pd.DataFrame, m: dict) -> pd.DataFrame:
    date_col, amt_col = m["date"], m["amount"]
    credit_col, debit_col = m["credit"], m["debit"]
    cp_col, type_col, cat_col = m["description"], m["type"], m["category"]

    if not date_col:
        raise IngestError(
            f"no date column found (tried {_DATE_COLS}); got {list(df.columns)}"
            " — map it explicitly")
    if not amt_col and not (credit_col or debit_col):
        raise IngestError(
            "no amount column — provide 'amount' (signed) or credit/debit columns,"
            " or map one explicitly")

    out = pd.DataFrame(index=df.index)
    d = pd.to_datetime(df[date_col], errors="coerce", format="mixed")
    bad = int(d.isna().sum())
    if bad > len(df) * 0.1 or bad == len(df):
        raise IngestError(f"{bad}/{len(df)} dates unparseable — expected YYYY-MM-DD style")
    out["date"] = d.dt.strftime("%Y-%m-%d")

    def _num(col):
        s = df[col].astype(str).str.strip()
        neg = s.str.match(r"^\(.*\)$")
        s = s.str.replace(r"[$,¥￥€£\s()]", "", regex=True)
        v = pd.to_numeric(s, errors="coerce").fillna(0)
        return v.where(~neg, -v.abs())

    if amt_col:
        amt = _num(amt_col)
        if type_col:
            t = df[type_col].astype(str).str.lower()
            sign = t.str.contains(r"credit|inflow|deposit|\bcr\b|入金", regex=True).map(
                {True: 1, False: -1})
            amt = amt.abs() * sign
    else:
        cr = _num(credit_col).abs() if credit_col else 0
        dr = _num(debit_col).abs() if debit_col else 0
        amt = cr - dr
    flow = amt.map(lambda a: "inflow" if a >= 0 else "outflow")
    out["amount"] = amt.abs().round(2)

    if (out["amount"] > 1e9).any():
        raise IngestError("amounts > $1B look like a parse error — check the file")
    out = out[out["date"].notna() & (out["amount"] > 0)].copy()
    if out.empty:
        raise IngestError("no rows with a valid date and non-zero amount")
    out["flow"] = flow.loc[out.index]
    cp = (df[cp_col].astype(str) if cp_col
          else pd.Series("imported", index=df.index))
    out["counterparty"] = cp.loc[out.index].str.slice(0, 80).map(mask_pii)
    out["memo"] = out["counterparty"]
    src = df[cat_col].astype(str) if cat_col else None
    cats = []
    for i in out.index:
        given = src.loc[i].strip() if src is not None else ""
        c = categorize(given) if given else "Other"
        if c == "Other":
            c = categorize(out.at[i, "counterparty"])
        if c == "Other" and given:
            c = given[:40]
        if c == "Other" and out.at[i, "flow"] == "inflow":
            c = "Revenue"
        cats.append(c)
    out["category"] = cats
    out["id"] = [f"csv-{i + 1}" for i in range(len(out))]
    out["invoice_id"] = ""
    out["payment_id"] = ""
    return out[["id", "date", "flow", "category", "counterparty",
                "amount", "memo", "invoice_id", "payment_id"]].reset_index(drop=True)


def preview_bank_csv(raw: bytes, mapping: dict | None = None,
                     sample: int = 8) -> dict:
    """Dry run for the mapping step: header, detected mapping, masked +
    categorized sample, category totals. Writes nothing."""
    df = read_csv_bytes(raw)
    m = detect_mapping(df, mapping)
    res = {"columns": [str(c) for c in df.columns], "rows": int(len(df)),
           "mapping": m, "fields": list(MAPPING_FIELDS),
           "limits": {"max_bytes": MAX_BYTES, "max_rows": MAX_ROWS}}
    try:
        norm = normalize(df, m)
    except IngestError as e:
        return {**res, "ok": False, "error": str(e), "sample": []}
    by_cat = (norm.groupby(["category", "flow"]).amount.agg(["count", "sum"])
              .reset_index().sort_values("sum", ascending=False))
    flagged = [str(x)[:60] for x in norm["memo"].unique()[:500]
               if scan_for_injection(str(x), channel="data")["flagged"]]
    return {**res, "ok": True, "valid_rows": int(len(norm)),
            "dropped_rows": int(len(df) - len(norm)),
            "inflow_total": round(float(norm[norm.flow == "inflow"].amount.sum()), 2),
            "outflow_total": round(float(norm[norm.flow == "outflow"].amount.sum()), 2),
            "span": [norm.date.min(), norm.date.max()],
            "categories": [{"category": r.category, "flow": r.flow,
                            "count": int(r["count"]), "total": round(float(r["sum"]), 2)}
                           for _, r in by_cat.iterrows()],
            "flagged_memos": flagged,
            "sample": norm.head(sample).to_dict("records")}


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

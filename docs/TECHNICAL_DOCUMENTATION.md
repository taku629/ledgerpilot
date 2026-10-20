# LedgerPilot — Technical Documentation

**2026 Shenzhen FinTechathon · International Track · Topic E: SME Finance Copilot**
*Theme: "AI as a Financial Participant"*

---

## 1. System overview

LedgerPilot is an AI copilot that completes SME finance tasks end-to-end:
forecast cash, explain the numbers, match financing, and execute sandbox
actions — under a permission model where **the agent proposes, the server
enforces, and the owner approves**.

```
┌─────────────────────────────── Browser (public demo) ───────────────────────────┐
│  Chat panel                      │  Dashboard                                    │
│  · agent replies + tool chips    │  · 13-week forecast chart (P10–P90 band)       │
│  · approval cards (Approve/Deny) │  · alerts / KPI analysis / financing / audit   │
│                                  │  · what-if sliders · evidence modals           │
└──────────────┬──────────────────────────────────────────────────────────────────┘
               │ HTTPS (JSON)
┌──────────────▼──────────────────────────────────────────────────────────────────┐
│ FastAPI app.main                                                                 │
│   /api/state /api/chat /api/forecast /api/financing /api/whatif                    │
│   /api/actions/{id}/approve · /api/actions/{id}/reject                             │
│   /api/transactions /api/invoices /api/audit /api/dev/regenerate                   │
│                                                                                  │
│  Agent (app/agent.py)                                                            │
│   ├─ LLMBackend        OpenAI-compatible chat.completions + function tools       │
│   ├─ DeterministicBackend  intent parser → same tools → templated replies        │
│   └─ number_guard      every ≥$10 figure in a reply must appear in tool output   │
│                                                                                  │
│  Tool surface (app/tools.py) — 13 tools, tier-checked server-side                │
│   T0 read:   load_transactions · get_cashflow_forecast · get_kpis                │
│              get_alerts · match_financing · get_evidence                          │
│   T1 draft:  what_if · draft_dunning_email · draft_loan_application               │
│   T2 sandbox:reschedule_payment · record_sandbox_payment · set_cash_buffer        │
│              · mark_invoice_paid                                                  │
│              → returns a PENDING ACTION; never executes here                      │
│   T3 real:   (registered, permanently disabled — check_tier refuses)             │
│                                                                                  │
│  Execution gate: ApprovalStore — single-use, TTL=300s, amount cap $50,000        │
│   consume() → execute_approved() → sandbox fns → AuditLog (JSONL)                │
│                                                                                  │
│  Engines                                                                        │
│   forecasting.py  weekly LightGBM per-category P50 + quantile band +             │
│                   committed-payment calendar + backtest vs seasonal naive        │
│   kpis.py         monthly P/L, margins, fixed-cost ratio, DSO/DPO/DIO,           │
│                   MoM drivers (complete months only)                              │
│   alerts.py       low balance / cluster / overdue AR / injection / outlier        │
│   financing.py    eligibility + cost + coverage/speed/burden scoring              │
│   sandbox.py      simulated ledger writes + draft generators                     │
│   ingest.py       bank-CSV parser (aliases, encodings, dup check, PII mask)      │
│   security.py     tiers, injection scanner, PII masking, approvals, audit        │
│   data_gen.py     seeded synthetic dataset (deterministic)                        │
└──────────────────────────────────────────────────────────────────────────────────┘
```

## 2. Core algorithms

### 2.1 Thirteen-week cash-flow forecast (`app/forecasting.py`)

1. **Weekly series** — transactions are signed (+inflow/−outflow) and pivoted
   into a week × category matrix aligned to ISO weeks.
2. **Per-category model** — the categories that behave like a time series get
   their own forecaster:
   `POS Sales`, `Online Sales`, `Wholesale Invoice`,
   `Coffee & Supplies`, `Packaging & Shipping`.
   Features per week *t*: `t, lag1, lag2, lag3, lag4, lag8, lag13,
   rmean4, rmean8, rstd8, month, wom, woy`. The model is a recursive
   LightGBM regressor (predict week t+1, feed it back as lag features).
   Fallbacks: seasonal moving average → level, chosen by history depth.
3. **Uncertainty band** — holdout residual quantiles per category give an
   *asymmetric* interval `pred + [q10, q90]` (captures holdout bias and skew);
   the lo/hi offsets propagate to the balance P10/P90. The fold reports the
   band's measured **coverage** — the share of held-out weeks whose actuals
   landed inside it (demo: 75–87.5% vs an 80% ideal; 8-week granularity).
4. **Committed payments** — the scheduled-payment calendar (rent, payroll,
   loan, tax, equipment …) is expanded onto the same week grid and added as
   *certain* outflow — these are obligations, not predictions.
5. **Balance projection** — starting from the ledger balance, each week:
   `balance_t = balance_{t-1} + inflow_p50 − outflow_modeled − outflow_committed`.
   The first week under the configured buffer ($30,000 default) becomes the
   **gap week**; `gap_amount` sizes the shortfall.
6. **Backtest** — last 8 weeks per category: WAPE of the model vs a
   seasonal-naive baseline (same week last cycle). Reported per category;
   categories with a negligible holdout denominator are marked unscored.
   Demo result: LightGBM beats naive on all 4 scorable categories
   (POS 2.3 vs 2.8, Online 7.5 vs 8.5, Supplies 13.3 vs 15.0,
   Packaging 9.9 vs 13.1).
7. **What-if** — scenario knobs scale modeled inflows (`sales_pct`), supplier
   costs (`supplies_pct`), shift receivables (`ar_delay_weeks`), or inject
   one-off `extra_inflow`/`extra_outflow` events at a given week, then the
   whole projection is recomputed — sub-second, all in code.

### 2.2 Business analysis (`app/kpis.py`)

- Monthly P/L: revenue (POS + Online + Wholesale), COGS (supplies +
  packaging), fixed costs, net cash flow, gross-margin %, fixed-cost ratio %.
  The current partial month is flagged so it is never compared like a full
  month.
- Working-capital days over a 90-day window: **DSO** = amount-weighted mean
  issue→paid lag on invoices; **DPO/DIO** are proxies (14d / 21d of supplier
  spend — a café has no AP ledger); **cash conversion cycle** = DSO + DIO − DPO.
- **MoM drivers**: category-level deltas between the last two *complete*
  months, signed by flow direction, ranked by |impact on net|.
- **Narrative** (`kpi_narrative`): a deterministic plain-English "what
  changed" summary assembled from the same figures — current month, net
  delta vs the prior complete month, top drivers, working-capital days.
  Rendered above the drivers table; computed in code, never by the LLM.

### 2.3 Financing matching (`app/financing.py`)

Each of the 10 catalog products is checked for eligibility (amount range vs
the gap, per-requirement checkers — months operating, revenue floor,
receivables, speed vs the gap week). Survivors are scored:

```
score = 100 × (0.30 × coverage_of_gap
             + 0.35 × cost_term            # 1 − cost/gap, normalised at 60%
             + 0.20 × speed                # 1 − funding_days/7 ÷ weeks_to_gap
             + 0.15 × (1 − burden))        # est. monthly payment ÷ free cash
```

Every row reports estimated total cost and monthly payment; **rejected
products stay visible with their reasons** ("too slow: 14d vs gap in week 2")
— transparency is part of the rubric, not an afterthought.

### 2.4a Bank-CSV ingest (`app/ingest.py`)

`POST /api/data/upload` accepts a raw bank statement CSV and merges it into
the transaction ledger:

- **Robust parse** — utf-8-sig → cp932 fallback, column aliases
  (date/posted/value_date…; amount/balance/金額…; separate credit/debit
  columns supported), `$`/`¥` stripping, ISO-ish and `YYYY/M/D` dates,
  >10% unparseable dates or >1 MB / >5,000 rows → HTTP 422.
- **Safety** — counterparty free-text is PII-masked before storage; up to
  500 memo fields per file are run through the injection scanner and
  flagged hits are returned in the response.
- **Merge modes** — `append` dedupes on (date, amount, counterparty);
  `replace` snapshots the old ledger to `transactions_backup.csv` first.
- After a merge the in-memory ledger is reloaded so forecasts/KPIs recompute
  on the new data.

### 2.4 Agent (`app/agent.py`)

- **LLM mode** — OpenAI-compatible `chat.completions` with the 12 tool specs;
  up to 6 tool rounds; system prompt forbids inventing figures.
- **Deterministic mode** (default for the public demo) — intent patterns map
  to the same tool calls; replies are templated from real tool output.
- **number_guard** — every numeric token ≥ 10 in the agent's reply must match
  a figure that appeared in a tool result (±2%). Unverified figures are
  flagged inline and written to the audit log.

## 3. Security design

See [`SECURITY_SELF_ASSESSMENT.md`](SECURITY_SELF_ASSESSMENT.md) for the full
report. The five layers:

1. **Permission tiers on every dispatch** — `call_tool` checks the tool's
   declared tier against the session tier before running anything.
2. **Execution gate** — Tier-2 calls only mint a `pending_action`
   (action_id + preview). Execution requires `POST /api/actions/{id}/approve`,
   which consumes a **single-use, TTL-bound** approval. Nothing else can
   mint one — not tool output, not a memo, not LLM text.
3. **Injection defense** — a dual-channel scanner: jailbreak-shaped text is
   suspicious anywhere; agent-directed imperatives ("wire $9,999…") are
   suspicious only inside *data* fields (a user may type "wire $5,000").
   Flagged data is wrapped `[UNTRUSTED DATA]` before it can reach the model
   and raises an `INJECTION_ATTEMPT` alert. The dataset ships a live hostile
   memo so judges can watch it fail to execute.
4. **Audit** — every tool call, denial, proposal, execution and what-if is
   appended to `data/audit_log.jsonl` with PII masking applied first.
   Each line carries `chain = sha256(prev_chain | event)`, so editing or
   deleting any earlier line provably breaks every later hash —
   `GET /api/audit/verify` re-checks the chain and the UI shows a
   "integrity verified" badge.
5. **Transport & input hardening** — every response carries CSP
   `default-src 'self'` (all assets incl. vendored Chart.js are
   same-origin), `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`,
   `Permissions-Policy`. Every `POST /api/*` passes a per-client-IP
   token bucket (12-burst, 30/min → 429 + audit event). Request bodies
   are bounded: chat ≤ 2,000 chars, what-if params clamped
   (`±90–200%`, AR delay ≤ 26 wk) — over-range input is a 422 before it
   reaches the engine.

## 4. Data model

| File | Contents |
|---|---|
| `transactions.csv` | 1,235 rows: date, category, counterparty, amount, flow, memo |
| `invoices.csv` | 15 B2B invoices: customer, issue/due/paid dates, amount, status |
| `scheduled_payments.csv` | 13 recurring/one-off obligations |
| `financing_catalog.json` | 10 products: type, amount range, APR/fee/factor, term, days-to-fund, requirements |
| `business_profile.json` | name, sector, founded, buffers, balances |
| `sandbox_state.json` | reschedules, executed wires, paid invoices, buffer override (runtime only) |
| `audit_log.jsonl` | append-only event log (runtime only) |

All synthetic, generated deterministically by `app/data_gen.py`
(`python -m app.data_gen` or `scripts/gen_data.py`).

## 5. API reference

| Method & path | Purpose | Tier |
|---|---|---|
| `GET /api/state` | profile + forecast + alerts + KPIs + disclaimer | T0 |
| `POST /api/chat` | agent turn (user text scanned for injection) | — |
| `GET /api/forecast` | 13-week projection | T0 |
| `GET /api/financing` | ranked + rejected financing vs current gap | T0 |
| `GET /api/financing/memo` | deterministic markdown decision memo (download) | T0 |
| `POST /api/whatif` | scenario recompute (UI can pin named scenarios to compare) | T1 |
| `POST /api/data/upload?mode=append|replace` | bank-CSV ingest: column aliases, cp932/utf-8-sig, credit/debit forms, dup check, memo injection scan, forecast refit | T1 |
| `POST /api/actions/{id}/approve` | consume approval → execute | T2 gate |
| `POST /api/actions/{id}/reject` | reject proposal | T2 gate |
| `GET /api/transactions?months=&category=&q=` | sanitized rows | T0 |
| `GET /api/invoices` | invoice ledger | T0 |
| `GET /api/audit?n=&format=` | audit log tail (json or jsonl download) | T0 |
| `POST /api/dev/regenerate` | regenerate synthetic data (audit log preserved) | dev |
| `GET /healthz` | liveness | — |

## 6. Limitations & honest notes

- Forecast quality is only as good as category history; sparse categories
  fall back to moving-average/level models and are marked as such.
- DPO/DIO are spend-proxies, not true AP/inventory ledgers.
- Single-file sandbox state — fine for a demo, not multi-tenant.
- No authentication — the demo is a single-owner session; in production the
  same tier/approval model would sit behind real auth.

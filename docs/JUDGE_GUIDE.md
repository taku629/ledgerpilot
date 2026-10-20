# Judge guide — where each rubric point lives

LedgerPilot — SME Finance Copilot (Harbor & Bean Roasters, fictional café + EC store).
Live: https://ledgerpilot-7k6f.onrender.com · Repo: https://github.com/taku629/ledgerpilot

Fastest tour (≈4 min): ask **"What should I do first?"** in the chat — it computes a
ranked action plan from live alerts + forecast, and each line names the next command.

## Task completion (40%)

| Rubric item | Where to see it | Try typing |
|---|---|---|
| CSV ingest + categorization | Transactions tab (1,235-row ledger, last-3-months view; categories, PII masked, flagged memos marked). **Transactions tab → Import bank CSV**: choose file → **column-mapping step** (auto-detected, every field overridable via dropdowns, incl. `Posted Date`/`Withdrawals`/`Deposits` and 日付/金額/摘要 headers) → **classification preview** (masked sample rows + per-category totals, writes nothing) → **Import** → forecast refits and the result shows *min balance before → after*. Rejected with a reason: >1 MB (413), >5,000 rows, binary/xlsx/PDF, unknown mapped column, unparseable dates (422). `Reset demo data` restores the synthetic ledger | `what is my biggest expense`, `can I upload my bank csv` |
| 13-week forecast (real model) | Main chart + "Forecast model accuracy" fold — LightGBM vs seasonal-naive WAPE, **measured band coverage** | `give me the 13-week forecast`, `how accurate is the forecast` |
| Alerts (low balance / clusters / overdue AR) | Alerts tab — grouped LOW_BALANCE with per-week evidence table | `when will I run out of cash`, `why is week 8 so low` |
| P/L, margins, working capital | Analysis tab — monthly P/L, DSO/DPO/DIO, MoM drivers, code-written narrative | `what changed last month`, `compare this month to last` |
| Financing matching + rejections | Financing tab — 10-product catalog, scores, **rejected with reasons**, `Download decision memo` | `what financing options cover the gap` |
| Sandbox execution, approval-gated | Ask to reschedule → approval card with live TTL countdown → Approve → forecast re-projects. Four executable actions: reschedule, sandbox wire, buffer change, **mark invoice paid** — `mark INV-0014 paid` (or *Mark paid…* in the **Open receivables** table) proposes a Tier-2 card; only after Approve does INV-0014 leave open AR, the AR_OVERDUE alert and the overdue-receivables KPI. Re-marking or an unknown invoice is refused at proposal time | `reschedule pay-012 to 2026-12-08`, `mark INV-0014 paid`, `wire $500 to Northwind` |
| Operation log | Audit tab — color-coded, **"integrity verified — N events hash-chained"**, JSONL download | `show me the audit log` |
| What-if + named scenario compare | Sliders → Apply → type a name (default A, B…) → **Save as scenario**. Scenarios are saved server-side (`/api/scenarios`) and drawn as dashed lines next to the baseline; the compare table lists **baseline / A / B** with min balance, delta vs baseline, first breach, week-13 balance. They are re-priced live, so they move after a CSV import or approved action. Chat: `compare my scenarios` returns the same table | `increase prices 10 percent`, `compare my scenarios` |
| Category cost shock | Per-payment delta × occurrences in the 13-wk window (frequency-aware) | `what happens if rent goes up 15%`, `what if payroll goes up 10%` |
| Working-capital answer | DSO/DPO/DIO + open & overdue AR | `how fast do customers pay me` |
| Seasonality answer | Monthly revenue profile from the ledger | `is my business seasonal` |
| Break-even | Fixed costs ÷ gross margin vs actual revenue | `break even point` |
| Safe discretionary spend | min projected balance − buffer | `how much can I safely spend` |

## Security & compliance (30%)

| Rubric item | Evidence |
|---|---|
| Permission tiers T0–T3 enforced server-side | `app/security.py` tier check per tool; T3 registered but disabled; `tests/test_security.py` |
| Server-side execution gate | No approve tool exists for the model; single-use tokens, 300 s TTL (visible countdown), $50k cap, replay refused — all in `evidence/sandbox_op_log.jsonl`. Typing `yes, approve it` in chat is refused too — approvals are clicks, not words (step in the demo scenario shows it) |
| Prompt-injection defense | Live poisoned memo `txn-01150` flagged → `INJECTION_ATTEMPT` alert, never executed; user-channel jailbreak/extraction patterns flagged too |
| PII masking | Transactions/Audit tabs; `mask_pii` applied before storage |
| Auditability | `data/audit_log.jsonl` — append-only, **hash-chained**; `GET /api/audit/verify` re-checks the chain (tamper test: `test_audit_chain_tamper_evidence`) |
| Input hardening | CSP `default-src 'self'`, zero CDN, per-IP rate limit (429 + Retry-After), bounded bodies (422) |
| Known-risk list + mitigations | `docs/SECURITY_SELF_ASSESSMENT.md` |
| Not-advice disclaimer | Footer + every financing reply + decision memo |

## Innovation & interaction (30%)

- **Evidence → action**: every alert opens source rows; AR_OVERDUE drafts the reminder, LOW_BALANCE jumps to each breach week, PAYMENT_CLUSTER offers Move +1w.
- **Action plan intent**: `what should I do first` fuses alerts + forecast + financing into an ordered plan with the exact next commands.
- **Calibrated uncertainty**: asymmetric P10–P90 band from residual quantiles; coverage measured on held-out weeks (75–87.5%).
- **Deterministic-fallback agent**: identical answers with or without an LLM key — demo can't fail on a flaky model; LLM mode adds `number_guard` that flags invented figures.
- **Artifacts**: one-click financing decision memo (markdown) + audit JSONL export.

## Numbers a judge may quote (2026-10-18 dataset)

- Start balance $21,353 → min **$16,678** (week 8) — below the $30,000 buffer; gap **$13,322**, first breach **week 2**
- Backtest WAPE: model beats seasonal-naive in **4/4** categories; band coverage 75–87.5%
- Overdue AR: INV-0014 Northwind Catering, $2,929, 25d
- Week-7 committed cluster: $22,450 (rent + equipment + loan)
- 111 pytest cases, all green (`tests/test_round9.py` covers CSV mapping/validation/PII/refit, mark-paid AR removal, named scenarios)

# LedgerPilot — SME Finance Copilot

[![CI](https://github.com/taku629/ledgerpilot/actions/workflows/ci.yml/badge.svg)](https://github.com/taku629/ledgerpilot/actions/workflows/ci.yml)

**2026 Shenzhen FinTechathon · International Track · Topic E**
*Theme: "AI as a Financial Participant"*

LedgerPilot is an AI copilot for small-business owners that completes real
finance tasks end-to-end: it ingests bank transactions, forecasts the next 13
weeks of cash, explains what changed in the numbers, matches financing
products to the projected gap, and executes sandbox actions — always behind an
explicit owner approval.

**All data is 100% synthetic.** The bundled demo business is "Harbor & Bean
Roasters", a fictional café + online store. No real company data, no licensed
datasets, no credentials in the repo.

---

## What it does

| Capability | How |
|---|---|
| **Cash-flow forecast** | Bank CSV → categories → weekly LightGBM per-category forecast (P10/P50/P90) + committed-payment calendar → 13-week balance projection. Forecasting is pure code — **the LLM never predicts**. |
| **Alerts** | Below-buffer weeks, payment clusters, overdue receivables, unusual outflows, and suspicious instruction-like text in data fields. |
| **Business analysis** | Monthly P/L, gross margin, fixed-cost ratio, DSO/DPO/inventory days, month-over-month change drivers. Every number is computed in Python; the agent only narrates. |
| **Financing matching** | 10-product synthetic catalog (loans, factoring, credit line, MCA, trade credit, grant…) scored against the forecast gap: coverage, cost, speed vs. gap week, payment burden. Rejected options list their reasons. |
| **Sandbox execution** | Reschedule payments, record simulated payments, change the alert threshold, draft dunning emails and loan applications. **Tier-2 actions only run after the owner clicks Approve** — and are written to the JSONL audit log. |
| **What-if** | "Sales −20%?", "supplies +15%?", "receivables 2 weeks late?" — instant recompute on the dashboard sliders or in chat. |
| **Evidence drilldown** | Every alert and every forecast point jumps to the underlying rows (invoices, committed payments, flagged records) — and the rows act back: *Move +1w* proposes a reschedule, *Draft reminder* writes the collection email. |
| **Model accountability** | A walk-forward backtest (WAPE per category, model vs seasonal-naive, plus measured P10–P90 band coverage) is folded under the chart — accuracy claims are inspectable, not asserted. |
| **Deterministic narrative** | The "what changed" explanation in the Analysis tab is assembled in code from the same table rows — it can never disagree with the numbers. |

## Security model (summary)

- **Permission tiers enforced server-side on every tool call:**
  Tier 0 read · Tier 1 draft/analysis · Tier 2 sandbox execution
  (requires a server-issued, single-use, 5-minute approval + $50k amount cap)
  · Tier 3 real-money — **registered but permanently disabled by design.**
- **Prompt-injection defense in depth:** data hygiene scanner (every memo is
  flagged if it looks like an instruction), channel separation (tool args may
  only come from the user's message, never from data), the execution gate
  (no tool output or LLM text can mint an approval token), and a full audit
  trail. A poisoned transaction memo is seeded into the demo data; the app
  surfaces it as an `INJECTION_ATTEMPT` alert and never acts on it.
- **PII masking** on tool outputs and the audit log · **API keys via env vars
  only** · number-guard that flags LLM figures not traceable to tool output.
- **Edge hardening:** strict self-only CSP (Chart.js vendored — zero external
  requests), `nosniff`/`DENY`/`no-referrer` headers, per-IP rate limit on
  POST endpoints (429 + audit event), and bounded inputs on every endpoint.
- **Exportable evidence:** the append-only audit log (survives data resets)
  is downloadable as JSONL from the Audit tab or `GET /api/audit?format=jsonl`.
- Full write-up: [`docs/SECURITY_SELF_ASSESSMENT.md`](docs/SECURITY_SELF_ASSESSMENT.md)

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
# open http://localhost:8000
```

First run auto-generates the synthetic dataset into `data/` (deterministic,
seeded). By default the dataset ends at today's date, so figures drift a
little each day; set `LEDGERPILOT_DATA_DATE=2026-10-18` to pin it (the hosted
demo does). To regenerate: `python -m app.data_gen` or
`POST /api/dev/regenerate`.

### With an LLM (optional)

The app is fully functional **without** any API key — a deterministic intent
engine answers the same intents. Point it at any OpenAI-compatible endpoint to
enable the LLM agent:

```bash
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_API_KEY=sk-...
export LLM_MODEL=gpt-4o-mini
```

See `.env.example` for all variables. Even in LLM mode the model can only
*request* tools — tier checks and the approval gate still apply.

## Deploy (free tier)

**Docker / Render / HF Spaces:**
```bash
docker build -t ledgerpilot . && docker run -p 8000:8000 ledgerpilot
```
On Render: *New → Web Service → public repo URL → Docker → deploy*, or
import `render.yaml` as a Blueprint. Hugging Face Spaces: choose the *Docker*
SDK and push this repo. No secrets are required for the public demo
(deterministic engine + synthetic data). Git-connected Render services created
from a public repo URL do not receive push webhooks — redeploy with
*Manual Deploy → Deploy latest commit* or a Render deploy hook.

## Repo layout

```
app/
  main.py         FastAPI: /api/state /chat /whatif /actions/{id}/approve ...
  agent.py        tool-calling agent (LLM backend + deterministic fallback,
                  number-guard on all figures)
  tools.py        12 tools, each declared with a permission tier; dispatch
                  enforces tiers server-side; Tier-2 → approval tokens only
  forecasting.py  weekly LightGBM forecast + scheduled-payment projection +
                  backtest vs seasonal naive
  kpis.py         P/L, margins, fixed-cost ratio, DSO/DPO/DIO, MoM drivers
  alerts.py       low-balance / cluster / overdue-AR / injection / outlier
  financing.py    eligibility + cost + coverage scoring vs the forecast gap
  sandbox.py      simulated ledger actions + draft generators
  security.py     tiers, injection scanner, PII masking, approval store, audit
  data_gen.py     seeded synthetic business (1,235 txns / 15 invoices / 13
                  scheduled payments / 10-product financing catalog)
  static/         vanilla-JS dashboard + chat (Chart.js CDN)
tests/            pytest — tiers, approvals, injection, PII, engine, agent
scripts/          gen_data.py, run_demo_scenario.py (reproduces evidence/)
docs/             technical doc, security self-assessment, demo script, deck
evidence/         sandbox op log (JSONL) produced by the demo scenario
```

## Submission deliverables (FinTechathon Topic E)

| # | Deliverable | Path |
|---|---|---|
| 01 | Technical documentation | [docs/TECHNICAL_DOCUMENTATION.md](docs/TECHNICAL_DOCUMENTATION.md) |
| 02 | Presentation deck (11 slides) | [docs/PRESENTATION_DECK.md](docs/PRESENTATION_DECK.md) · [HTML slideshow](docs/PRESENTATION_DECK.html) |
| 03 | Demo video script (≤5 min) | [docs/DEMO_VIDEO_SCRIPT.md](docs/DEMO_VIDEO_SCRIPT.md) |
| 04 | Source + | this repo · `tests/` |
| 05 | Security self-assessment | [docs/SECURITY_SELF_ASSESSMENT.md](docs/SECURITY_SELF_ASSESSMENT.md) |
| 06 | Execution evidence | [evidence/sandbox_op_log.jsonl](evidence/sandbox_op_log.jsonl) + [evidence/demo_scenario.md](evidence/demo_scenario.md) |
| — | **Judge guide (rubric → where)** | [docs/JUDGE_GUIDE.md](docs/JUDGE_GUIDE.md) |

**Live demo:** https://ledgerpilot-vx59.onrender.com (free tier — cold start up to ~60 s)

## Tests

```bash
python -m pytest        # 90 tests — engine + security + injection defense + API
```

## Assumptions

- **Currency: USD** — the International Track judges are global; the synthetic
  business is therefore a US-style café/EC shop rather than a Tokyo one.
- **"Bank CSV" is synthetic** — `app/data_gen.py` produces a realistic
  transaction feed; `load_transactions` is the same tool a real adapter would
  implement.
- **DPO / inventory days are proxies** — a small café has no AP ledger, so they
  are estimated as 14d / 21d of supplier spend; definitions in
  `docs/TECHNICAL_DOCUMENTATION.md`.
- **LLM is optional** — the public demo runs the deterministic engine so the
  URL works with zero secrets; judges can set `LLM_*` env vars to enable the
  LLM backend.
- **"Real account" (Tier 3) is intentionally absent** — there is no real-money
  integration; the tier exists in the model and is refused unconditionally.
- **No auth/login** — a demo deployment assumes a single owner session; the
  approval gate and audit trail are the security boundary being demonstrated.

## Known risks

Hallucinated numbers (mitigated by number-guard + tools-only figures), forecast
error on sparse categories (backtest WAPE shown per category), data quality of
real bank feeds (dedup/category heuristics needed), single-session state file
(sandbox_state.json — fine for demo, not for multi-user). Full list:
[`docs/SECURITY_SELF_ASSESSMENT.md`](docs/SECURITY_SELF_ASSESSMENT.md).

---

*LedgerPilot provides informational analysis only. It is not financial,
investment, tax, or legal advice. Final financing and payment decisions remain
with the owner.*

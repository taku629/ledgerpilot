/* LedgerPilot UI — chat + dashboard. All figures come from API responses. */
const $ = (s) => document.querySelector(s);
const fmt = (v, d = 0) => v == null ? "—" :
  (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, {maximumFractionDigits: d});
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const md = (s) => {
  const safe = esc(s);
  const lines = safe.split("\n");
  let out = [], tbl = [];
  const flush = () => {
    if (tbl.length < 2) { out.push(...tbl); tbl = []; return; }
    const rows = tbl.filter(l => !/^\|[\s\-|:]+\|$/.test(l.trim()));
    const cells = l => l.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
    const [head, ...body] = rows;
    out.push(`<table class="mdTbl"><tr>${cells(head).map(c => `<th>${c}</th>`).join("")}</tr>` +
      body.map(r => `<tr>${cells(r).map(c => `<td>${c}</td>`).join("")}</tr>`).join("") + "</table>");
    tbl = [];
  };
  for (const l of lines) {
    if (l.trim().startsWith("|")) { tbl.push(l); continue; }
    flush();
    out.push(l);
  }
  flush();
  return out.join("<br>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/```([\s\S]*?)```/g, '<pre style="background:#0d1420;padding:10px;border-radius:8px;overflow:auto;font-size:11px">$1</pre>')
    .replace(/`([^`]+)`/g, "<code>$1</code>");
};

let STATE = null, chart = null, scenarioSeries = null;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || e.error || r.status); }
  return r.json();
}

/* ---------------- KPI strip ---------------- */
function renderKpis() {
  const f = STATE.forecast, p = STATE.profile, wc = STATE.kpis.working_capital;
  const kpis = [
    {k: "Current balance", v: fmt(f.balance_start), s: p.business_name},
    {k: "Min projected (13wk)", v: fmt(f.min_balance),
     s: f.gap_week ? `below buffer in week ${f.gap_week}` : "stays above buffer",
     cls: f.gap_week ? "crit" : ""},
    {k: "Gap to buffer", v: f.gap_amount > 0 ? fmt(f.gap_amount) : "none",
     s: `buffer ${fmt(f.cash_buffer)}`, cls: f.gap_amount > 0 ? "warn" : ""},
    {k: "Overdue receivables", v: fmt(wc.overdue_receivables),
     s: `${wc.overdue_count} invoice(s)`, cls: wc.overdue_count ? "warn" : ""},
    {k: "Active alerts", v: STATE.alerts.length,
     s: STATE.alerts[0] ? STATE.alerts[0].type : "", cls: "warn"},
    {k: "Cash cycle", v: wc.cash_conversion_cycle + "d",
     s: `DSO ${wc.dso} · DPO ${wc.dpo} · inv ${wc.dio}`},
  ];
  $("#kpiStrip").innerHTML = kpis.map(k =>
    `<div class="kpi ${k.cls || ""}"><div class="k">${k.k}</div>
     <div class="v">${k.v}</div><div class="s">${k.s}</div></div>`).join("");
}

/* ---------------- Chart ---------------- */
function renderChart() {
  const f = STATE.forecast;
  const labels = f.weeks.map(w => "W" + w.week);
  const bal = f.weeks.map(w => w.balance_p50);
  const lo = f.weeks.map(w => w.balance_p10), hi = f.weeks.map(w => w.balance_p90);
  const committed = f.weeks.map(w => -w.outflow_committed);
  const bufferLine = f.weeks.map(() => f.cash_buffer);
  const sets = [
    {label: "Balance P50", data: bal, borderColor: "#35d0a5", backgroundColor: "#35d0a5",
     borderWidth: 2.5, pointRadius: 3, type: "line", tension: .3},
    {label: "P10–P90 band", data: hi, borderColor: "rgba(74,168,255,.35)",
     backgroundColor: "rgba(74,168,255,.10)", fill: "-1", pointRadius: 0, type: "line", tension: .3},
    {label: "", data: lo, borderColor: "rgba(74,168,255,.35)", pointRadius: 0, type: "line",
     fill: false, tension: .3, showInLegend: false},
    {label: "Buffer floor", data: bufferLine, borderColor: "#ffb020",
     borderDash: [6, 5], borderWidth: 1.5, pointRadius: 0, type: "line"},
    {label: "Committed outflow", data: committed, type: "bar",
     backgroundColor: "rgba(255,90,101,.45)", borderRadius: 4},
  ];
  if (scenarioSeries) sets.push({label: "Scenario", data: scenarioSeries,
    borderColor: "#c792ff", borderDash: [4, 4], borderWidth: 2, pointRadius: 0,
    type: "line", tension: .3});
  if (chart) chart.destroy();
  chart = new Chart($("#cfChart"), {
    data: {labels, datasets: sets},
    options: {responsive: true, maintainAspectRatio: false,
      plugins: {legend: {labels: {color: "#8b96a7", boxWidth: 14, filter: i => i.text}},
        tooltip: {callbacks: {label: c => `${c.dataset.label}: ${fmt(c.parsed.y)}`}}},
      scales: {x: {ticks: {color: "#8b96a7"}, grid: {color: "#1a2331"}},
               y: {ticks: {color: "#8b96a7", callback: v => "$" + v / 1000 + "k"},
                   grid: {color: "#1a2331"}}},
      onClick: (e, els) => { if (els.length) showWeekEvidence(els[0].index + 1); }}});
  $("#gapNote").innerHTML = f.gap_week
    ? `⚠️ Projected to breach the ${fmt(f.cash_buffer)} buffer in <b>week ${f.gap_week}</b> —
       minimum ${fmt(f.min_balance)}. Click a point to see that week's committed payments.`
    : `Balance stays above the ${fmt(f.cash_buffer)} buffer all 13 weeks. Min ${fmt(f.min_balance)}.`;
  renderModelAccuracy(f.backtest);
}

function renderModelAccuracy(bt) {
  const rows = Object.entries(bt || {}).filter(([, v]) => v.wape_model != null);
  $("#modelAcc").innerHTML = !rows.length ? "" : `<details>
    <summary>Forecast model accuracy — walk-forward backtest (WAPE %, lower is better)</summary>
    <table><tr><th>Category</th><th>Model</th><th>Model WAPE</th><th>Seasonal-naive WAPE</th><th>Edge</th><th>Band coverage</th></tr>` +
    rows.map(([cat, v]) => {
      const edge = v.wape_seasonal_naive - v.wape_model;
      return `<tr><td>${esc(cat)}</td><td>${esc(v.model)}</td><td>${v.wape_model}%</td>
        <td>${v.wape_seasonal_naive}%</td>
        <td class="${edge >= 0 ? "pos" : "neg"}">${edge >= 0 ? "+" : ""}${edge.toFixed(1)}pt</td>
        <td>${v.coverage_pct != null ? v.coverage_pct + "%" : "—"}</td></tr>`;
    }).join("") +
    `</table><div class="fineprint">Forecasts are produced by code only — the LLM
     never predicts. Walk-forward validation re-fits each model on past weeks and
     scores the held-out week. Band coverage = share of held-out weeks whose
     actuals landed inside the P10–P90 band (ideal ≈ 80%).</div></details>`;
}

/* ---------------- Alerts ---------------- */
function renderAlerts() {
  $("#alertCount").textContent = STATE.alerts.length;
  $("#alertsList").innerHTML = STATE.alerts.map(a => `
    <div class="alert" data-id="${a.id}">
      <span class="sev ${a.severity}">${a.severity}</span>
      <div><div class="t">${esc(a.title)}</div><div class="d">${esc(a.detail)}</div></div>
      <span class="evlink">evidence →</span>
    </div>`).join("");
  document.querySelectorAll(".alert").forEach(el =>
    el.onclick = () => showAlertEvidence(el.dataset.id));
}

function showAlertEvidence(id) {
  const a = STATE.alerts.find(x => x.id === id);
  if (!a) return;
  let html = `<h2>${esc(a.title)}</h2><p style="color:var(--dim);font-size:12px;margin:8px 0">${esc(a.detail)}</p>`;
  const ev = a.evidence || {};
  if (ev.weeks?.length) {
    html += `<h2>Weeks below buffer</h2><table>
      <tr><th>Week</th><th>Dates</th><th>Projected balance</th><th>Inflow</th><th>Outflow</th><th></th></tr>` +
      ev.weeks.map(w => `<tr><td>W${w.week}</td><td>${w.start} → ${w.end}</td>
        <td class="${w.balance_p50 < 0 ? "neg" : ""}">${fmt(w.balance_p50)}</td>
        <td>${fmt(w.inflow_p50)}</td><td>${fmt(w.outflow)}</td>
        <td><button class="chip evWeek" data-w="${w.week}">evidence →</button></td></tr>`).join("") +
      `</table>`;
  } else if (ev.committed?.length) {
    html += `<h2>Committed payments — week ${ev.week}</h2><table>
      <tr><th>Date</th><th>Category</th><th>Counterparty</th><th>Amount</th><th>ID</th><th></th></tr>` +
      ev.committed.map(c => `<tr><td>${c.date}</td><td>${esc(c.category)}</td>
        <td>${esc(c.counterparty)}</td><td>${fmt(c.amount)}</td>
        <td>${c.payment_id}</td>
        <td><button class="chip evMove" data-id="${c.payment_id}" data-d="${c.date}">Move +1w</button></td></tr>`).join("") +
      `</table><div class="fineprint">Moving a payment is a Tier-2 sandbox action — it proposes a change that still needs your approval in chat.</div>`;
  } else if (ev.invoice_id) {
    html += `<h2>Invoice</h2><table><tr><th>ID</th><th>Customer</th><th>Due</th><th>Amount</th></tr>
      <tr><td>${ev.invoice_id}</td><td>${esc(ev.customer)}</td>
      <td>${ev.due_date}</td><td>${fmt(ev.amount)}</td></tr></table>
      <div style="margin-top:12px"><button id="evAct" class="chip">Draft a payment reminder for ${esc(ev.invoice_id)}</button></div>`;
  } else if (ev.transaction_id) {
    html += `<h2>Flagged record</h2><table><tr><th>Txn</th><th>Memo</th></tr>
      <tr><td>${ev.transaction_id}</td><td>${esc(ev.memo || "")}</td></tr></table>
      <p class="fineprint">This text was flagged by the injection scanner and is treated
      as inert data — it can never trigger an action.</p>`;
  } else {
    html += `<pre>${esc(JSON.stringify(ev, null, 2))}</pre>`;
  }
  openModal(html);
  const act = document.getElementById("evAct");
  if (act) act.onclick = () => {
    $("#modalBg").classList.remove("open");
    send(`Draft a payment reminder for ${ev.invoice_id}`);
  };
  document.querySelectorAll(".evMove").forEach(b => b.onclick = () => {
    const d = new Date(b.dataset.d + "T00:00:00");
    d.setDate(d.getDate() + 7);
    $("#modalBg").classList.remove("open");
    send(`reschedule ${b.dataset.id} to ${d.toISOString().slice(0, 10)}`);
  });
  document.querySelectorAll(".evWeek").forEach(b => b.onclick = () =>
    showWeekEvidence(+b.dataset.w));
}

function showWeekEvidence(wk) {
  const w = STATE.forecast.weeks.find(x => x.week === wk);
  if (!w) return;
  openModal(`<h2>Week ${w.week} — ${w.start} → ${w.end}</h2>
    <table><tr><th>Inflow P50</th><th>Modeled outflow</th><th>Committed</th><th>Net</th><th>Balance</th></tr>
    <tr><td>${fmt(w.inflow_p50)}</td><td>${fmt(w.outflow_modeled)}</td>
    <td>${fmt(w.outflow_committed)}</td><td class="${w.net_p50 < 0 ? "neg" : "pos"}">${fmt(w.net_p50)}</td>
    <td>${fmt(w.balance_p50)}</td></tr></table>
    <h2 style="margin-top:14px">Committed payments</h2>
    ${w.committed.length ? `<table><tr><th>Date</th><th>Category</th><th>Counterparty</th><th>Amount</th><th>ID</th></tr>` +
      w.committed.map(c => `<tr><td>${c.date}</td><td>${esc(c.category)}</td>
      <td>${esc(c.counterparty)}</td><td>${fmt(c.amount)}</td><td>${c.payment_id}</td></tr>`).join("") + "</table>"
      : '<p style="color:var(--dim)">No committed payments this week.</p>'}`);
}

/* ---------------- Analysis ---------------- */
function renderAnalysis() {
  const pl = STATE.kpis.monthly_pl;
  $("#plTable").innerHTML = `<table><tr><th>Month</th><th>Revenue</th><th>COGS</th>
    <th>Gross margin</th><th>Fixed</th><th>Fixed %</th><th>Net</th></tr>` +
    pl.map(p => `<tr class="${p.partial ? "" : ""}">
      <td>${p.month}${p.partial ? " <span class='flag'>partial</span>" : ""}</td>
      <td>${fmt(p.revenue)}</td><td>${fmt(p.cogs)}</td>
      <td>${p.gross_margin_pct}%</td><td>${fmt(p.fixed_costs)}</td>
      <td>${p.fixed_cost_ratio_pct}%</td>
      <td class="${p.net_cashflow < 0 ? "neg" : "pos"}">${fmt(p.net_cashflow)}</td></tr>`).join("") + "</table>";
  const wc = STATE.kpis.working_capital;
  $("#wcDays").innerHTML = `<table>
    <tr><th>DSO</th><th>DPO</th><th>Inventory</th><th>Cycle</th></tr>
    <tr><td>${wc.dso}d</td><td>${wc.dpo}d</td><td>${wc.dio}d</td>
    <td><b>${wc.cash_conversion_cycle}d</b></td></tr></table>
    <div class="fineprint">Overdue AR ${fmt(wc.overdue_receivables)} · open AR ${fmt(wc.open_receivables)}
    · 90-day window</div>`;
  $("#drivers").innerHTML =
    (STATE.kpis.narrative ? `<p class="narr">${esc(STATE.kpis.narrative)}</p>` : "") +
    `<table><tr><th>Category</th><th>Flow</th><th>Prev</th><th>Cur</th><th>Net impact</th></tr>` +
    STATE.kpis.mom_drivers.map(d => `<tr><td>${esc(d.category)}</td><td>${d.flow}</td>
      <td>${fmt(d.prev)}</td><td>${fmt(d.cur)}</td>
      <td class="${d.impact_on_net < 0 ? "neg" : "pos"}">${fmt(d.impact_on_net)}</td></tr>`).join("") + "</table>";
}

/* ---------------- Financing ---------------- */
async function renderFinancing() {
  const data = await api("/api/financing");
  renderFinTable(data);
}

function renderFinTable(m) {
  $("#finDisc").textContent = m.disclaimer || "";
  $("#finTable").innerHTML = `
    <div class="fineprint" style="padding:0 0 10px">Funding gap
    <b style="color:var(--warn)">${fmt(m.gap_amount)}</b>${m.gap_week ? ` first hit in week ${m.gap_week}` : ""}
    — ranked by coverage, cost, speed vs the gap, and payment burden.
    <a href="/api/financing/memo" download="financing_memo.md">Download decision memo (markdown)</a></div>
    <table><tr><th>Score</th><th>Product</th><th>Type</th><th>Est. cost</th>
    <th>Funds in</th><th>Coverage</th><th>Why</th></tr>` +
    m.ranked.map(r => `<tr><td><span class="finScore">${r.score}</span></td>
      <td>${esc(r.name)}</td><td>${r.type}</td><td>${fmt(r.est_total_cost)}</td>
      <td>${r.time_to_fund_days}d</td><td>${r.coverage_of_gap_pct}%</td>
      <td style="text-align:left">${esc(r.why)}</td></tr>`).join("") +
    (m.rejected.length ? `<tr><td colspan="7" style="text-align:left;color:var(--dim);padding-top:12px"><b>Not chosen — with reasons</b></td></tr>` +
     m.rejected.map(r => `<tr class="rejRow"><td>—</td><td>${esc(r.name)}</td>
       <td>${r.type}</td><td>${fmt(r.est_total_cost)}</td><td colspan="3" style="text-align:left">${esc(r.reasons.join("; "))}</td></tr>`).join("")
     : "") + "</table>";
}

/* ---------------- Audit ---------------- */
function auditKindClass(k) {
  if (/denied|flagged|limited|error|fail/i.test(k)) return "kBad";
  if (/exec|approv|proposed/i.test(k)) return "kGood";
  return "";
}

async function renderAudit() {
  const a = await api("/api/audit?n=60");
  let badge = "";
  try {
    const v = await api("/api/audit/verify");
    badge = v.ok
      ? `<b style="color:var(--accent)">integrity verified</b> — ${v.events} events hash-chained`
      : `<b class="neg">chain broken at event ${v.first_broken_index}</b>`;
  } catch (e) { badge = ""; }
  $("#auditList").innerHTML =
    `<div class="fineprint" style="padding:0 0 8px">${a.rows.length} events —
     append-only; survives data reset. ${badge}.
     <a href="/api/audit?format=jsonl&n=500" download="ledgerpilot_audit.jsonl">Download evidence (JSONL)</a></div>` +
    (a.rows.map(e =>
    `<div class="auditRow"><span class="ts">${e.ts || ""}</span>
     <span class="k ${auditKindClass(e.kind)}">${esc(e.kind)}</span>
     <span>${esc(e.tool || "")} ${esc(JSON.stringify(e.args || e.params || e.matches || e.result || e.reason || "")).slice(0, 130)}</span></div>`).join("")
    || "<div class='auditRow'>No events yet.</div>");
}

/* ---------------- Transactions ---------------- */
async function renderTxns() {
  const months = +$("#txnMonths").value, q = $("#txnFilter").value;
  const t = await api(`/api/transactions?months=${months}${q ? "&q=" + encodeURIComponent(q) : ""}`);
  $("#txnTable").innerHTML = `<div class="fineprint" style="padding:0 0 8px">${t.count} transactions
    (${t.span.join(" → ")}). PII is masked; flagged memos marked.</div>
    <table><tr><th>Date</th><th>Category</th><th>Counterparty</th><th>Amount</th><th>Memo</th></tr>` +
    t.rows.map(r => `<tr>
      <td>${r.date}</td><td>${esc(r.category)}</td><td>${esc(r.counterparty)}</td>
      <td class="${r.flow === "outflow" ? "neg" : "pos"}">${r.flow === "outflow" ? "-" : "+"}${fmt(r.amount)}</td>
      <td style="text-align:left;max-width:280px">${r._flags ? '<span class="flag">flagged</span> ' : ""}${esc(String(r.memo || "").slice(0, 90))}</td></tr>`).join("") + "</table>";
}

/* ---------------- Chat ---------------- */
function addMsg(who, html) {
  const d = document.createElement("div");
  d.className = "msg " + who;
  d.innerHTML = html;
  $("#chatLog").appendChild(d);
  $("#chatLog").scrollTop = 1e9;
  return d;
}

function addApprovalCard(pa) {
  const d = document.createElement("div");
  d.className = "approval";
  const expiryLine = pa.expires_in_s
    ? `<div class="ttl2">single use · expires in <b class="exp"></b></div>` : "";
  d.innerHTML = `<div class="ttl">Approval required — Tier 2 sandbox action</div>${expiryLine}
    <div class="prev">${esc(pa.preview || pa.tool)}</div>
    <div class="btns"><button class="approve">Approve &amp; execute</button>
    <button class="deny">Reject</button></div>`;
  if (pa.expires_in_s) {
    const end = Date.now() + pa.expires_in_s * 1000;
    const el = d.querySelector(".exp");
    const tick = () => {
      const s = Math.max(0, Math.round((end - Date.now()) / 1000));
      el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      if (s === 0) {
        clearInterval(iv);
        if (!d.classList.contains("done")) {
          d.classList.add("done");
          d.querySelectorAll("button").forEach(b => b.disabled = true);
          d.querySelector(".prev").innerHTML += `<br><b class="neg">Approval expired — ask again to re-issue.</b>`;
        }
      }
    };
    const iv = setInterval(tick, 1000);
    tick();
  }
  d.querySelector(".approve").onclick = async () => {
    d.querySelectorAll("button").forEach(b => b.disabled = true);
    try {
      const r = await api(`/api/actions/${pa.action_id}/approve`, {method: "POST"});
      d.classList.add("done");
      d.querySelector(".prev").innerHTML +=
        `<br><b style="color:var(--accent)">Executed:</b> ${esc(JSON.stringify(r.result).slice(0, 220))}`;
      addMsg("bot", `✅ Action executed and written to the audit log.`);
      await refresh();
    } catch (e) { d.querySelector(".prev").innerHTML += `<br><b class="neg">${esc(e.message)}</b>`; }
  };
  d.querySelector(".deny").onclick = async () => {
    d.classList.add("done");
    await api(`/api/actions/${pa.action_id}/reject`, {method: "POST"});
    d.querySelector(".prev").innerHTML += `<br><b>Rejected by user.</b>`;
  };
  $("#chatLog").appendChild(d);
  $("#chatLog").scrollTop = 1e9;
}

async function send(text) {
  if (!text.trim()) return;
  addMsg("user", esc(text));
  $("#chatInput").value = "";
  const thinking = addMsg("bot", "<i style='color:var(--dim)'>thinking…</i>");
  try {
    const r = await api("/api/chat", {method: "POST",
      headers: {"content-type": "application/json"}, body: JSON.stringify({message: text})});
    thinking.innerHTML = md(r.reply) +
      (r.tool_calls?.length ? `<div class="tools">` + r.tool_calls.map(c =>
        `<span class="chip">⚙ ${c.tool}</span>`).join("") + "</div>" : "");
    for (const pa of r.pending_actions || []) addApprovalCard(pa);
    if (r.data && Object.keys(r.data).length) refresh();
  } catch (e) { thinking.innerHTML = "Error: " + esc(e.message); }
}

/* ---------------- misc ---------------- */
function openModal(html) { $("#modalBody").innerHTML = html; $("#modalBg").classList.add("open"); }
$("#modalX").onclick = () => $("#modalBg").classList.remove("open");
$("#modalBg").onclick = (e) => { if (e.target.id === "modalBg") e.target.classList.remove("open"); };

document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".tabPane").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  $("#tab-" + t.dataset.tab).classList.add("active");
  if (t.dataset.tab === "audit") renderAudit();
  if (t.dataset.tab === "data") renderTxns();
  if (t.dataset.tab === "financing") renderFinancing();
});

async function applyWhatIf() {
  const params = {sales_pct: +$("#wiSales").value,
                  supplies_pct: +$("#wiSup").value,
                  ar_delay_weeks: +$("#wiAR").value};
  const r = await api("/api/whatif", {method: "POST",
    headers: {"content-type": "application/json"}, body: JSON.stringify(params)});
  scenarioSeries = r.forecast.weeks.map(w => w.balance_p50);
  renderChart();
  $("#gapNote").innerHTML = `Scenario ${JSON.stringify(r.params)} → min
    <b class="${r.forecast.min_balance < 0 ? "neg" : ""}">${fmt(r.forecast.min_balance)}</b>
    (baseline ${fmt(r.baseline.min_balance)}), first buffer breach
    ${r.forecast.gap_week ? "week " + r.forecast.gap_week : "none"}.`;
}
["wiSales", "wiSup", "wiAR"].forEach(id =>
  $("#" + id).oninput = () => {
    $("#" + id + "V").textContent =
      id === "wiAR" ? $("#" + id).value + "w" : $("#" + id).value + "%";
  });
$("#wiApply").onclick = applyWhatIf;
$("#wiReset").onclick = () => { scenarioSeries = null;
  ["wiSales", "wiSup"].forEach(id => { $("#" + id).value = 0; $("#" + id + "V").textContent = "0%"; });
  $("#wiAR").value = 0; $("#wiARV").textContent = "0w"; renderChart(); };

$("#chatForm").onsubmit = (e) => { e.preventDefault(); send($("#chatInput").value); };
document.querySelectorAll(".suggestions button").forEach(b =>
  b.onclick = () => send(b.dataset.q));
$("#txnMonths").onchange = renderTxns;
$("#txnFilter").oninput = () => renderTxns();

async function refresh() {
  STATE = await api("/api/state");
  renderKpis(); renderChart(); renderAlerts(); renderAnalysis();
}

(async function init() {
  try {
    await refresh();
    $("#disclaimer").textContent = STATE.disclaimer;
    $("#bizName").textContent = STATE.profile.business_name + " — " +
      STATE.profile.data_window.start + " → " + STATE.profile.data_window.end;
    $("#llmBadge").textContent = STATE.llm_configured ? "LLM connected" : "deterministic engine (no LLM key)";
    $("#llmBadge").classList.toggle("on", STATE.llm_configured);
    addMsg("bot", `Hi — I'm <b>LedgerPilot</b>. I can forecast your cash 13 weeks out,
      explain what changed in your numbers, match financing to the gap, draft reminders
      and applications, and run sandbox actions — always behind your approval.
      ${STATE.alerts.length ? `<br><br>⚠️ You have <b>${STATE.alerts.length} active alerts</b> — top: ${esc(STATE.alerts[0].title)}` : ""}`);
  } catch (e) {
    addMsg("bot", "Failed to load state: " + esc(e.message));
  }
})();

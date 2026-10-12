(() => {
  const $ = (id) => document.getElementById(id);
  const fmt = (v, d = 2) => v === null || v === undefined || !Number.isFinite(Number(v)) ? "N/A" : Number(v).toFixed(d);
  const pct = (v) => v === null || v === undefined || !Number.isFinite(Number(v)) ? "N/A" : (Number(v) * 100).toFixed(1) + "%";
  const priceFmt = (v) => {
    if (v === null || v === undefined || !Number.isFinite(Number(v))) return "N/A";
    const n = Number(v);
    return n !== 0 && Math.abs(n) < 0.0001 ? n.toExponential(4) : n.toPrecision(7).replace(/0+$/, "").replace(/\.$/, "");
  };
  let lastPositions = [];
  let refreshInProgress = false;
  let observedBars = { mint: null, bars: [], coverage: null, fetchedAt: 0 };
  let observedFetchMint = null;

  function setStatus(el, value) {
    if (!el) return;
    el.textContent = value ?? "UNKNOWN";
    el.className = "";
    if (["HEALTHY", "PASS", "APPROVED", "CONNECTED"].includes(value)) el.classList.add("status-ok");
    else if (["DEGRADED", "WAITING", "NEEDS_REFRESH", "ADVISORY", "UNKNOWN", "NOT_CONFIGURED", "NOT_CHECKED", "NOT_INSTRUMENTED", "READY", "MOCK", "DISABLED"].includes(value)) el.classList.add("status-warn");
    else if (["UNHEALTHY", "REJECT", "REJECTED", "ERROR", "DISCONNECTED"].includes(value)) el.classList.add("status-bad");
    else el.classList.add("status-idle");
  }

  async function getJson(url) {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`${url} (${response.status})`);
    return response.json();
  }

  function addCell(row, value, className = "") {
    const td = document.createElement("td");
    td.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
    if (className) td.className = className;
    row.appendChild(td);
    return td;
  }

  function ageLabel(ts) {
    if (ts === null || ts === undefined || ts === "" || !Number.isFinite(Number(ts))) return "N/A";
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
    return seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${Math.floor(seconds / 3600)}h`;
  }

  async function refreshHealth() {
    try {
      const h = await getJson("/health");
      setStatus($("health"), h.status);
      setStatus($("scanner"), h.components?.intelligence?.state);
      setStatus($("market"), h.components?.market_data?.state);
      const launch = h.components?.launch_stream || {};
      const launchLabel = launch.connected ? (launch.last_message_age_seconds == null ? "CONNECTED · waiting for first event" : `CONNECTED · ${launch.events_buffered || 0} events`) : (launch.enabled ? `DISCONNECTED · ${launch.last_error || "reconnecting"}` : "DISABLED · disabled in config");
      if ($("launch-stream")) { setStatus($("launch-stream"), launch.connected ? (launch.last_message_age_seconds == null ? "WAITING" : "HEALTHY") : "WAITING"); $("launch-stream").textContent = launchLabel; }
      if ($("pipeline-launch")) $("pipeline-launch").textContent = `PUMP.FUN STREAM: ${launch.connected ? (launch.last_message_age_seconds == null ? "CONNECTED / NO MESSAGES YET" : "CONNECTED") : launch.enabled ? "RECONNECTING" : "DISABLED"}`;
      $("uptime").textContent = `${Math.floor(h.uptime_seconds || 0)}s`;
      const scanTs = h.components?.intelligence?.last_scan_ts;
      $("last-scan").textContent = scanTs ? `${ageLabel(scanTs)} ago` : "Waiting for first scan";
      const providerState = h.components?.market_data?.state || "UNKNOWN";
      $("pipeline-provider").textContent = `MARKET DATA: ${providerState}`;
      $("pipeline-scan").textContent = `SCANNER: ${h.components?.intelligence?.state || "UNKNOWN"}`;
      $("pipeline-last-scan").textContent = scanTs ? `Last scan ${ageLabel(scanTs)} ago · candidates in last scan: ${h.components?.intelligence?.last_scan_candidates ?? "N/A"}` : "Waiting for first completed scan…";
    } catch (error) {
      setStatus($("health"), "UNHEALTHY");
      $("uptime").textContent = "N/A";
      $("last-scan").textContent = "Unavailable";
    }
  }

  async function refreshPortfolio() {
    try {
      const p = await getJson("/api/portfolio");
      $("equity").textContent = `$${fmt(p.equity)}`;
      $("realized").textContent = `$${fmt(p.realized_pnl)}`;
      $("unrealized").textContent = `$${fmt(p.unrealized_pnl)}`;
      $("today_pnl").textContent = `$${fmt(p.today_pnl)}`;
      $("win_rate").textContent = pct(p.win_rate);
      $("pf").textContent = p.profit_factor === null ? "N/A" : (p.profit_factor === Infinity ? "∞" : fmt(p.profit_factor));
      $("dd").textContent = `$${fmt(p.max_drawdown)}`;
      $("opens").textContent = `${p.open_positions ?? "N/A"} / ${p.max_total_open_positions ?? "N/A"}`;
      $("exposure").textContent = `$${fmt(p.open_exposure)}`;
      $("dlr").textContent = `$${fmt(p.daily_loss_remaining)}`;
      setStatus($("daily-lock"), p.daily_locked ? "LOCKED" : "NOT LOCKED");
      const real = Number(p.realized_pnl);
      const unreal = Number(p.unrealized_pnl);
      $("realized").className = Number.isFinite(real) ? (real >= 0 ? "positive" : "negative") : "";
      $("unrealized").className = Number.isFinite(unreal) ? (unreal >= 0 ? "positive" : "negative") : "";
    } catch (error) {
      ["equity", "realized", "unrealized", "today_pnl", "win_rate", "pf", "dd", "opens", "exposure", "dlr"].forEach(id => { $(id).textContent = "N/A"; });
      setStatus($("daily-lock"), "UNKNOWN");
    }
  }

  async function refreshCandidates() {
    try {
      const rows = await getJson("/api/candidates");
      const tbody = $("candidates").querySelector("tbody");
      tbody.replaceChildren();
      $("candidate-count").textContent = `${rows.length} candidates`;
      if (!rows.length) {
        const tr = document.createElement("tr");
        const td = document.createElement("td"); td.colSpan = 12; td.textContent = "No candidates recorded yet. Check provider status and discovery logs.";
        tr.appendChild(td); tbody.appendChild(tr); return;
      }
      for (const r of rows) {
        const tr = document.createElement("tr");
        const market = r.market || {};
        const launchTs = Number(r.launch_ts);
        const pairAge = Number(market.pair_age_seconds);
        const age = Number.isFinite(launchTs) && launchTs > 0 ? ageLabel(launchTs) : Number.isFinite(pairAge) && market.pair_age_seconds !== null ? (pairAge < 60 ? `${Math.floor(pairAge)}s` : pairAge < 3600 ? `${Math.floor(pairAge/60)}m` : `${Math.floor(pairAge/3600)}h`) : "N/A";
        const momentum = market.price_change_5m_pct ?? null;
        addCell(tr, r.symbol || "Unnamed token");
        addCell(tr, r.mint ? `${r.mint.slice(0, 8)}…${r.mint.slice(-5)}` : "Missing mint", "mono");
        addCell(tr, r.source || "UNKNOWN");
        addCell(tr, age);
        addCell(tr, market.liquidity_usd == null ? "N/A" : `$${fmt(market.liquidity_usd, 0)}`);
        addCell(tr, momentum == null ? "N/A" : `${fmt(momentum, 1)}%`);
        addCell(tr, r.setup || "—");
        addCell(tr, r.score === null || r.score === undefined ? "N/A" : fmt(r.score, 2));
        addCell(tr, r.decision || "UNKNOWN", r.decision === "APPROVED" ? "status-ok" : r.decision === "REJECTED" || r.decision === "ENTRY_WINDOW_MISSED" ? "status-bad" : "status-warn");
        const safety = r.safety_passed === true ? "PASS" : r.safety_passed === false ? "REJECT / WAIT" : "UNKNOWN";
        addCell(tr, safety, safety === "PASS" ? "status-ok" : safety === "REJECT / WAIT" ? "status-bad" : "status-idle");
        addCell(tr, r.block_reason || (r.reasons || []).join(", "));
        addCell(tr, ageLabel(r.last_seen_at) + " ago");
        tbody.appendChild(tr);
      }
    } catch (error) {
      $("candidate-count").textContent = "Data unavailable";
    }
  }

  async function refreshObservedBars(mint) {
    if (!mint || observedFetchMint === mint) return;
    if (observedBars.mint === mint && Date.now() - observedBars.fetchedAt < 5000) return;
    observedFetchMint = mint;
    try {
      const payload = await getJson(`/api/observed-candles/${encodeURIComponent(mint)}?limit=120`);
      observedBars = { mint, bars: payload.bars || [], coverage: payload.coverage || null, fetchedAt: Date.now() };
      const coverage = payload.coverage || {};
      $("observed-candle-note").textContent = `${payload.label || "observed candles (GROK6-collected) — not exchange OHLCV"} · ${coverage.status || "UNKNOWN"} · ${coverage.bars ?? 0} bars`;
      if ($("position-select").value) drawSelectedPosition(false);
    } catch (error) {
      observedBars = { mint, bars: [], coverage: { status: "UNAVAILABLE" }, fetchedAt: Date.now() };
      $("observed-candle-note").textContent = "Observed candles unavailable; no history is synthesized.";
    } finally { observedFetchMint = null; }
  }

  function renderPositionSelector(positions) {
    const select = $("position-select");
    const current = select.value;
    select.replaceChildren();
    const prompt = document.createElement("option"); prompt.value = ""; prompt.textContent = "Select an open position"; select.appendChild(prompt);
    for (const p of positions.filter(p => ["OPEN", "TP1_HIT"].includes(p.status))) {
      const option = document.createElement("option"); option.value = p.id; option.textContent = `${p.symbol || p.mint.slice(0, 8)} · ${p.status}`; select.appendChild(option);
    }
    if (positions.some(p => p.id === current && ["OPEN", "TP1_HIT"].includes(p.status))) select.value = current;
    else if (select.options.length > 1) select.selectedIndex = 1;
    drawSelectedPosition();
  }

  function drawSelectedPosition(loadBars = true) {
    const canvas = $("trade-chart");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const width = Math.max(320, canvas.clientWidth || 600);
    const height = 320;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#0b111a"; ctx.fillRect(0, 0, width, height);
    const id = $("position-select").value;
    const pos = lastPositions.find(p => p.id === id && ["OPEN", "TP1_HIT"].includes(p.status));
    const levelsBox = $("trade-levels"); levelsBox.replaceChildren();
    if (!pos) {
      ctx.fillStyle = "#94a3b8"; ctx.font = "13px sans-serif"; ctx.fillText("No open position selected", 20, 35);
      $("chart-note").textContent = "Open paper positions will appear here with actual recorded entry and management levels.";
      $("observed-candle-note").textContent = "Observed candles: no position selected; no market bars are fabricated.";
      const div = document.createElement("div"); div.className = "empty-state"; div.textContent = "No open position selected."; levelsBox.appendChild(div); return;
    }
    if (loadBars) refreshObservedBars(pos.mint);
    const bars = observedBars.mint === pos.mint ? observedBars.bars : [];
    const priceRows = [
      ["TP2 target", Number(pos.tp2_price), "#28d17c"],
      ["TP1 target", Number(pos.tp1_price), "#76e1a8"],
      ["Market mark", Number(pos.last_mark_price), "#f6b94a"],
      ["Entry price", Number(pos.entry_price), "#61a5ff"],
      [pos.trailing_active ? "Protective stop" : "Stop loss", Number(pos.trailing_stop || pos.stop_price), "#ff5964"],
    ].filter(x => Number.isFinite(x[1]) && x[1] > 0);
    const vals = priceRows.map(x => x[1]);
    for (const b of bars) {
      if (Number.isFinite(Number(b.low))) vals.push(Number(b.low));
      if (Number.isFinite(Number(b.high))) vals.push(Number(b.high));
    }
    if (!vals.length) return;
    let min = Math.min(...vals), max = Math.max(...vals);
    const span = Math.max(max - min, Math.abs(max) * 0.01, 1e-12);
    min -= span * 0.12; max += span * 0.12;
    const top = 25, bottom = height - 26, plotH = bottom - top;
    const yOf = price => bottom - (price - min) / (max - min) * plotH;
    ctx.font = "11px ui-monospace, monospace";
    for (let i = 0; i <= 4; i++) {
      const y = top + (plotH * i / 4);
      ctx.strokeStyle = "#1e2938"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(58, y); ctx.lineTo(width - 12, y); ctx.stroke();
      const price = max - (max - min) * i / 4;
      ctx.fillStyle = "#7f90a6"; ctx.fillText(priceFmt(price), 7, y - 4);
    }
    if (bars.length) {
      const plotLeft = 62, plotRight = width - 18, usableWidth = Math.max(1, plotRight - plotLeft);
      const slot = usableWidth / bars.length;
      const bodyWidth = Math.max(2, Math.min(9, slot * 0.58));
      bars.forEach((bar, i) => {
        const open = Number(bar.open), high = Number(bar.high), low = Number(bar.low), close = Number(bar.close);
        if (![open, high, low, close].every(Number.isFinite)) return;
        const x = plotLeft + (i + 0.5) * slot;
        const yHigh = yOf(high), yLow = yOf(low), yOpen = yOf(open), yClose = yOf(close);
        const rising = close >= open, candleColor = rising ? "#28d17c" : "#ff5964";
        ctx.strokeStyle = candleColor; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, yHigh); ctx.lineTo(x, yLow); ctx.stroke();
        ctx.fillStyle = candleColor;
        const bodyTop = Math.min(yOpen, yClose), bodyHeight = Math.max(1.5, Math.abs(yClose - yOpen));
        ctx.fillRect(x - bodyWidth / 2, bodyTop, bodyWidth, bodyHeight);
      });
    }
    for (const [label, price, color] of priceRows) {
      const y = yOf(price);
      ctx.strokeStyle = color; ctx.lineWidth = label === "Market mark" ? 2.2 : 1.4;
      ctx.setLineDash(label === "Entry price" ? [5, 4] : []);
      ctx.beginPath(); ctx.moveTo(58, y); ctx.lineTo(width - 12, y); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = color; ctx.font = "bold 11px sans-serif";
      const ly = Math.max(13, Math.min(height - 7, y - 5));
      ctx.fillText(`${label}: ${priceFmt(price)}`, 68, ly);
    }
    const markAge = ageLabel(pos.last_mark_ts);
    $("chart-note").textContent = `Levels are based on persisted position state. Last mark: ${markAge} ago. ${Number(pos.last_mark_ts) && (Date.now() / 1000 - Number(pos.last_mark_ts) > 120) ? "WARNING: mark may be stale." : ""}`;
    const unrealized = pos.last_mark_price ? (Number(pos.last_mark_price) - Number(pos.entry_price)) * Number(pos.remaining_qty) : null;
    const rows = [
      ["Token", pos.symbol || pos.mint], ["Lifecycle", pos.status], ["Entry reason", pos.entry_reason || "N/A"],
      ["Entry", priceFmt(pos.entry_price)], ["Stop", priceFmt(pos.stop_price)], ["TP1", priceFmt(pos.tp1_price)],
      ["TP1 status", pos.tp1_done ? "DONE · half closed" : "WAITING"], ["TP2", priceFmt(pos.tp2_price)],
      ["Trailing stop", pos.trailing_active ? priceFmt(pos.trailing_stop) : "Not active"],
      ["Realized PnL", `$${fmt(pos.realized_pnl, 4)}`], ["Unrealized PnL (gross)", unrealized === null ? "N/A" : `$${fmt(unrealized, 4)}`],
      ["Current mark age", markAge],
    ];
    for (const [label, value] of rows) {
      const div = document.createElement("div"); div.className = "level-row";
      const spanEl = document.createElement("span"); spanEl.textContent = label;
      const b = document.createElement("b"); b.textContent = String(value ?? "N/A");
      div.append(spanEl, b); levelsBox.appendChild(div);
    }
  }

  async function refreshPositions() {
    try {
      const rows = await getJson("/api/positions");
      lastPositions = rows;
      renderPositionSelector(rows);
      const tbody = $("positions").querySelector("tbody"); tbody.replaceChildren();
      const opens = rows.filter(p => ["OPEN", "TP1_HIT"].includes(p.status));
      if (!opens.length) {
        const tr = document.createElement("tr"), td = document.createElement("td"); td.colSpan = 10; td.textContent = "No open paper positions."; tr.appendChild(td); tbody.appendChild(tr);
      }
      for (const p of opens) {
        const tr = document.createElement("tr");
        const unrealized = p.last_mark_price ? (Number(p.last_mark_price) - Number(p.entry_price)) * Number(p.remaining_qty) : null;
        const total = unrealized === null ? null : unrealized + Number(p.realized_pnl || 0);
        addCell(tr, p.symbol || p.mint);
        addCell(tr, p.status, p.status === "TP1_HIT" ? "status-ok" : "status-warn");
        addCell(tr, priceFmt(p.entry_price), "mono"); addCell(tr, priceFmt(p.last_mark_price), "mono");
        addCell(tr, priceFmt(p.stop_price), "mono"); addCell(tr, priceFmt(p.tp1_price), "mono"); addCell(tr, priceFmt(p.tp2_price), "mono");
        addCell(tr, fmt(p.remaining_qty, 6), "mono"); addCell(tr, total === null ? "N/A" : `$${fmt(total, 4)}`, total === null ? "" : total >= 0 ? "positive" : "negative");
        addCell(tr, ageLabel(p.last_mark_ts)); tbody.appendChild(tr);
      }
      const history = $("history").querySelector("tbody"); history.replaceChildren();
      const closed = rows.filter(p => p.status === "CLOSED").sort((a,b) => Number(b.closed_at||0)-Number(a.closed_at||0)).slice(0,100);
      if (!closed.length) { const tr = document.createElement("tr"), td = document.createElement("td"); td.colSpan=8; td.textContent="No completed trades yet."; tr.appendChild(td); history.appendChild(tr); }
      for (const p of closed) {
        const tr=document.createElement("tr"); addCell(tr,p.symbol||p.mint); addCell(tr,p.setup); addCell(tr,p.opened_at?new Date(p.opened_at*1000).toLocaleString():"N/A");
        addCell(tr,p.closed_at?new Date(p.closed_at*1000).toLocaleString():"N/A");
        const pnl=Number(p.realized_pnl||0); addCell(tr,`$${fmt(pnl,4)}`,pnl>=0?"positive":"negative");
        addCell(tr,`$${fmt(p.fees_paid,4)}`); addCell(tr,`$${fmt(p.slippage_paid,4)}`); addCell(tr,p.exit_reason||"N/A"); history.appendChild(tr);
      }
    } catch (error) {
      $("chart-note").textContent = "Position data unavailable.";
    }
  }

  async function refreshDecisions() {
    try {
      const rows = await getJson("/api/agents");
      const roster = new Map();
      for (const r of rows) for (const a of (Array.isArray(r.agents) ? r.agents : [])) {
        const prior = roster.get(a.agent);
        if (!prior || Number(r.ts || 0) > Number(prior.ts || 0)) roster.set(a.agent, { ...a, ts: r.ts, symbol: r.symbol || r.mint, decision: r.decision });
      }
      document.querySelectorAll(".agent-card[data-agent]").forEach(card => {
        const key = card.dataset.agent, a = roster.get(key);
        const state = card.querySelector(".agent-state"), evidence = card.querySelector(".agent-evidence");
        if (!a) return;
        state.textContent = a.status || "UNKNOWN"; state.className = "agent-state";
        state.classList.add(["PASS","APPROVED"].includes(a.status) ? "status-ok" : ["REJECT","REJECTED","ERROR"].includes(a.status) ? "status-bad" : "status-warn");
        const reasons = Array.isArray(a.reason_codes) ? a.reason_codes : [];
        const ev = a.evidence && typeof a.evidence === "object" ? Object.entries(a.evidence).slice(0,3).map(([k,v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ") : "";
        evidence.textContent = `${a.symbol || "Latest scan"} · ${reasons.join(", ") || "No reason code"}${ev ? " · " + ev : ""}`;
      });
      const host = $("agent-decisions"); host.replaceChildren();
      if (!rows.length) { const div=document.createElement("div"); div.className="empty-state"; div.textContent="No scan decisions recorded yet."; host.appendChild(div); return; }
      for (const r of rows.slice(0,12)) {
        const card=document.createElement("div"); card.className="decision-item";
        const head=document.createElement("div"); head.className="decision-title";
        const name=document.createElement("span"); name.textContent=`${r.symbol||r.mint||"Unknown token"} · ${r.setup||"NO_SETUP"}`;
        const status=document.createElement("span"); status.textContent=r.decision||"UNKNOWN"; status.className=r.decision==="APPROVED"?"status-ok":r.decision==="REJECTED"||r.decision==="ENTRY_WINDOW_MISSED"?"status-bad":"status-warn";
        head.append(name,status); const details=document.createElement("div"); details.className="decision-reasons"; details.textContent=(r.reasons||[]).join(", ")||"No reason recorded";
        const score=document.createElement("div"); score.className="decision-reasons"; score.textContent=`Score: ${fmt(r.score,2)} · ${ageLabel(r.ts)} ago`;
        card.append(head,details,score);
        const agents=Array.isArray(r.agents)?r.agents:[];
        if(agents.length){ const ag=document.createElement("div"); ag.className="decision-reasons"; ag.textContent=agents.map(a=>`${a.agent}:${a.status}`).join(" · "); card.appendChild(ag); }
        host.appendChild(card);
      }
    } catch (error) {
      const host=$("agent-decisions"); host.textContent="Decision audit is unavailable.";
    }
  }

  async function refreshLogs() {
    try {
      const qs = new URLSearchParams({ limit: 200 });
      if ($("log-level").value) qs.set("level", $("log-level").value);
      if ($("log-component").value) qs.set("component", $("log-component").value);
      const rows = await getJson("/api/logs?" + qs.toString());
      const tbody = $("logs").querySelector("tbody"); tbody.replaceChildren();
      for (const log of rows) {
        const tr=document.createElement("tr"); addCell(tr,new Date(log.ts*1000).toISOString().replace("T"," ").slice(0,19),"mono");
        addCell(tr,log.level,log.level==="ERROR"?"status-bad":log.level==="WARN"?"status-warn":""); addCell(tr,log.component); addCell(tr,log.event); addCell(tr,log.message); tbody.appendChild(tr);
      }
      if (!rows.length) { const tr=document.createElement("tr"), td=document.createElement("td"); td.colSpan=5; td.textContent="No log entries."; tr.appendChild(td); tbody.appendChild(tr); }
    } catch (error) {}
  }

  async function refreshSettings() {
    try {
      const c = await getJson("/api/settings");
      $("setting-notional").value = c.max_position_notional;
      $("setting-exposure").value = c.max_total_open_exposure;
      $("setting-risk").value = (Number(c.risk_per_trade_pct) * 100).toString();
      $("setting-daily-loss").value = c.daily_realized_loss_lock;
      $("setting-rr").value = c.min_net_reward_risk;
      $("setting-extension").value = (Number(c.max_entry_extension_pct) * 100).toString();
      $("setting-fast").checked = Boolean(c.fast_launch_enabled);
      $("settings-message").textContent = "Loaded from active runtime configuration.";
    } catch (error) { $("settings-message").textContent = "Settings unavailable."; }
  }

  async function saveSettings(event) {
    event.preventDefault();
    const message = $("settings-message"); message.textContent = "Validating…";
    const body = {
      max_position_notional: Number($("setting-notional").value),
      max_total_open_exposure: Number($("setting-exposure").value),
      risk_per_trade_pct: Number($("setting-risk").value) / 100,
      daily_realized_loss_lock: Number($("setting-daily-loss").value),
      min_net_reward_risk: Number($("setting-rr").value),
      max_entry_extension_pct: Number($("setting-extension").value) / 100,
      fast_launch_enabled: $("setting-fast").checked,
    };
    try {
      const response = await fetch("/api/settings", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`);
      message.textContent = "Settings applied to the active paper runtime.";
      await refreshPortfolio();
    } catch (error) { message.textContent = `Not applied: ${error.message}`; }
  }

  async function refreshProviderHealth() {
    try {
      const payload = await getJson("/api/providers/health");
      const tbody = $("provider-health-table").querySelector("tbody"); tbody.replaceChildren();
      const providers = payload.providers || [];
      if (!providers.length) { const tr=document.createElement("tr"), td=document.createElement("td");td.colSpan=4;td.textContent="No providers registered.";tr.appendChild(td);tbody.appendChild(tr); }
      for (const provider of providers) {
        const tr = document.createElement("tr");
        addCell(tr, provider.name || "unknown");
        const stateCell = addCell(tr, provider.state || "UNKNOWN"); setStatus(stateCell, provider.state || "UNKNOWN");
        addCell(tr, provider.requests_made ?? provider.messages_received ?? "—");
        const note = provider.last_error || provider.note || provider.endpoint_version_note || (provider.api_key_configured === false && provider.api_key_required !== false ? "API key not configured" : "—");
        addCell(tr, String(note).slice(0, 180));
        tbody.appendChild(tr);
      }
      $("provider-updated").textContent = `Updated ${new Date(Number(payload.server_time) * 1000).toLocaleTimeString()}`;
    } catch (error) { $("provider-updated").textContent = "Provider health unavailable"; }
  }

  async function refreshRejections() {
    try {
      const payload = await getJson("/api/rejections/by-reason?minutes=60");
      $("rejection-total").textContent = `${payload.total ?? 0} decisions`;
      const cards = $("decision-counts"); cards.replaceChildren();
      const decisionCounts = payload.decision_counts || {};
      for (const [label, count] of Object.entries(decisionCounts).slice(0, 4)) {
        const box = document.createElement("div"); box.className="metric";
        const title = document.createElement("span"); title.textContent=label;
        const value = document.createElement("b"); value.textContent=String(count);
        box.append(title, value); cards.appendChild(box);
      }
      const tbody = $("rejection-table").querySelector("tbody"); tbody.replaceChildren();
      const reasons = Object.entries(payload.reason_counts || {}).slice(0, 10);
      if (!reasons.length) { const tr=document.createElement("tr"), td=document.createElement("td");td.colSpan=2;td.textContent="No persisted block reasons in this period.";tr.appendChild(td);tbody.appendChild(tr); }
      for (const [reason, count] of reasons) { const tr=document.createElement("tr"); addCell(tr, reason); addCell(tr, count); tbody.appendChild(tr); }
    } catch (error) { $("rejection-total").textContent = "Unavailable"; }
  }

  function tickClock() { $("clock").textContent = new Date().toISOString().replace("T", " ").slice(0, 19) + "Z"; }
  async function refreshAll() {
    if (refreshInProgress) return;
    refreshInProgress = true;
    try { await Promise.allSettled([refreshHealth(), refreshProviderHealth(), refreshRejections(), refreshPortfolio(), refreshCandidates(), refreshPositions(), refreshDecisions(), refreshLogs()]); }
    finally { refreshInProgress = false; }
  }

  $("position-select").addEventListener("change", drawSelectedPosition);
  $("settings-form").addEventListener("submit", saveSettings);
  $("log-refresh").addEventListener("click", refreshLogs);
  $("log-level").addEventListener("change", refreshLogs);
  window.addEventListener("resize", drawSelectedPosition);
  tickClock(); setInterval(tickClock, 1000); setInterval(refreshAll, 5000); refreshSettings(); refreshAll();

  async function refreshWalletWatch() {
    try {
      const data = await getJson("/api/wallet-watch");
      const health = data.health || {};
      $("wallet-provider-state").textContent = health.state || "UNKNOWN";
      $("wallet-watch-note").textContent = health.note || "Provider state unavailable.";
      const tbody = $("wallet-watch-table").querySelector("tbody"); tbody.replaceChildren();
      const wallets = data.wallets || [];
      if (!wallets.length) { const tr=document.createElement("tr"); const td=document.createElement("td"); td.colSpan=8; td.textContent="No wallets tracked yet."; tr.appendChild(td); tbody.appendChild(tr); }
      for (const w of wallets) {
        const tr=document.createElement("tr"); addCell(tr, `${w.address.slice(0,6)}…${w.address.slice(-5)}`, "mono"); addCell(tr,w.label||"—"); addCell(tr,w.status||"UNKNOWN"); addCell(tr,w.buy_events??0); addCell(tr,w.sell_events??0); addCell(tr,w.last_event_at?ageLabel(w.last_event_at)+" ago":"N/A"); addCell(tr,w.evidence_note||"N/A");
        const td=document.createElement("td"), b=document.createElement("button"); b.type="button"; b.textContent="Remove"; b.addEventListener("click",async()=>{await fetch(`/api/wallet-watch/${encodeURIComponent(w.address)}`,{method:"DELETE"});refreshWalletWatch();}); td.appendChild(b);tr.appendChild(td);tbody.appendChild(tr);
      }
      const eb=$("wallet-events-table").querySelector("tbody"); eb.replaceChildren(); const events=data.events||[];
      if(!events.length){const tr=document.createElement("tr"),td=document.createElement("td");td.colSpan=7;td.textContent="No verified wallet events yet. Configure an RPC/Geyser adapter to populate this feed.";tr.appendChild(td);eb.appendChild(tr);}
      for(const e of events){const tr=document.createElement("tr");addCell(tr,new Date(Number(e.ts)*1000).toLocaleTimeString());addCell(tr,`${e.wallet.slice(0,6)}…${e.wallet.slice(-4)}`,"mono");addCell(tr,e.side);addCell(tr,`${e.mint.slice(0,6)}…${e.mint.slice(-4)}`,"mono");addCell(tr,e.amount_sol==null?"N/A":fmt(e.amount_sol,4));addCell(tr,e.source||"verified-adapter");addCell(tr,e.signature?`${e.signature.slice(0,8)}…`:"N/A","mono");eb.appendChild(tr);}
    } catch(e) { $("wallet-provider-state").textContent="ERROR"; $("wallet-watch-note").textContent="Wallet watch API unavailable."; }
  }
  const walletForm = $("wallet-watch-form");
  if(walletForm) walletForm.addEventListener("submit",async(event)=>{event.preventDefault();const address=$("wallet-address").value.trim(),label=$("wallet-label").value.trim();const response=await fetch("/api/wallet-watch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({address,label})});const data=await response.json();if(!response.ok){$("wallet-watch-note").textContent=data.error||"Could not add wallet.";return;}$("wallet-address").value="";$("wallet-label").value="";refreshWalletWatch();});

  refreshWalletWatch();
  setInterval(refreshWalletWatch, 5000);
})();

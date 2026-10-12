(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const num = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
  const fmt = (v, d = 2) => num(v) ? Number(v).toLocaleString(undefined, {minimumFractionDigits:d,maximumFractionDigits:d}) : 'N/A';
  const usd = (v, d = 2) => num(v) ? '$' + fmt(v, d) : 'N/A';
  const pct = (v, multiplier = 1) => num(v) ? (Number(v) * multiplier).toFixed(1) + '%' : 'N/A';
  const price = (v) => {
    if (!num(v)) return 'N/A'; const n = Number(v);
    return n !== 0 && Math.abs(n) < 0.0001 ? n.toExponential(4) : n.toPrecision(7).replace(/0+$/, '').replace(/\.$/, '');
  };
  const when = (ts) => num(ts) && Number(ts) > 0 ? new Date(Number(ts) * 1000).toLocaleString() : 'N/A';
  const timeOnly = (ts) => num(ts) && Number(ts) > 0 ? new Date(Number(ts) * 1000).toLocaleTimeString() : '—';
  const age = (ts) => {
    if (!num(ts) || Number(ts) <= 0) return 'UNKNOWN';
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
    return seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds/60)}m` : `${Math.floor(seconds/3600)}h`;
  };
  const statusClass = (s) => {
    const x = String(s ?? '').toUpperCase();
    if (['HEALTHY','CONNECTED','PASS','APPROVED','ACTIVE','OK','TRUE','PAPER_OPEN','RUNNING','READY'].includes(x)) return 'status-ok';
    if (['DEGRADED','WARN','ADVISORY','WAITING','NEEDS_REFRESH','NOT_CONFIGURED','NOT_CHECKED','NOT_INSTRUMENTED','UNKNOWN','IDLE','MOCK','DISABLED','INSUFFICIENT_HISTORY','NEEDS_MARKET_ENRICHMENT','RETRY_EXHAUSTED','INSUFFICIENT_DATA','ENTRY_WINDOW_MISSED','NOT_READY'].includes(x)) return 'status-warn';
    if (['UNHEALTHY','ERROR','REJECT','REJECTED','DISCONNECTED','FALSE','EXPIRED','ENTRY_WINDOW_MISSED'].includes(x)) return 'status-bad';
    return 'status-idle';
  };
  const pill = (s) => `<span class="status-pill ${statusClass(s)}">${esc(s || 'UNKNOWN')}</span>`;
  const inflight = new Map();
  async function get(url) {
    if (inflight.has(url)) return inflight.get(url);
    const p = fetch(url, {cache:'no-store', headers:{'Accept':'application/json'}})
      .then(async r => { const data = await r.json().catch(() => ({})); if (!r.ok) throw new Error(data.error || `${r.status} ${url}`); return data; })
      .finally(() => inflight.delete(url));
    inflight.set(url, p); return p;
  }
  async function send(url, method, body) {
    const response = await fetch(url, {method, cache:'no-store', headers:{'Content-Type':'application/json','Accept':'application/json'}, body:JSON.stringify(body)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `${response.status} ${url}`);
    return data;
  }

  const model = {health:null, providers:[], candidates:[], positions:[], agents:null, logs:[], fills:[], wallet:null, selectedMint:'', selectedPositionId:''};
  function navigate(name) {
    $$('.tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    $$('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${name}`));
    if (name === 'anatomy') drawChart();
  }
  $$('.tabs button').forEach(b => b.addEventListener('click', () => navigate(b.dataset.tab)));
  $$('[data-goto]').forEach(b => b.addEventListener('click', () => navigate(b.dataset.goto)));
  function setText(selector, value) { const e = $(selector); if (e) e.textContent = value === null || value === undefined || value === '' ? 'N/A' : String(value); }
  function emptyRow(colspan, text) { return `<tr><td class="empty-state" colspan="${colspan}">${esc(text)}</td></tr>`; }

  async function refreshHealth() {
    const h = await get('/health'); model.health = h;
    const [providerData, status] = await Promise.allSettled([get('/api/providers/health'), get('/api/status')]);
    model.providers = providerData.status === 'fulfilled' ? (providerData.value.providers || []) : [];
    const statusData = status.status === 'fulfilled' ? status.value : {};
    const scan = statusData.scanner || h.components?.intelligence || {};
    setText('#connection-label', h.status || 'UNKNOWN');
    const c = $('#connection-label'); c.className = statusClass(h.status);
    const modeResolution = h.mode_resolution || {};
    const resolvedMode = String(modeResolution.mode || h.mode || 'UNKNOWN').toUpperCase();
    const executor = h.live_executor || {};
    const liveAuthorized = Boolean(h.live_authorized || modeResolution.live_authorized);
    const modeLabels = {PAPER:'PAPER SIMULATION', WATCH_ONLY:'WATCH ONLY', LIVE_BLOCKED:'LIVE REQUEST BLOCKED', LIVE:'LIVE MODE AUTHORIZED', SAFE_STOP:'SAFE STOP'};
    setText('#runtime-mode', modeLabels[resolvedMode] || resolvedMode);
    const modeReasons = Array.isArray(modeResolution.reasons) ? modeResolution.reasons : [];
    const modeNote = resolvedMode === 'PAPER' ? 'NEW ENTRIES: SIMULATED ONLY'
      : resolvedMode === 'LIVE' && liveAuthorized
        ? (executor.last_execution_ts ? `Signer/Jupiter configured · last successful swap ${age(executor.last_execution_ts)} ago` : 'Signer/Jupiter configured · no successful live swap verified in this process')
        : (modeReasons.join(' · ') || executor.reason || 'New entries are not authorized');
    const modeBadge = $('#mode-badge');
    if (modeBadge) {
      modeBadge.textContent = resolvedMode === 'LIVE' && liveAuthorized ? 'LIVE MODE' : resolvedMode === 'PAPER' ? 'PAPER MODE' : resolvedMode;
      modeBadge.className = `badge ${resolvedMode === 'LIVE' && liveAuthorized ? 'info' : ['LIVE_BLOCKED','SAFE_STOP'].includes(resolvedMode) ? 'danger' : 'warn'}`;
    }
    const executionBadge = $('#execution-badge');
    const executionFooter = $('#execution-footer');
    const settingsBadge = $('#settings-mode-badge');
    const contractTitle = $('#execution-contract-title');
    const contractDescription = $('#execution-contract-description');
    const liveExecuted = Boolean(executor.last_execution_ts && executor.last_signature);
    const executionText = resolvedMode === 'LIVE' && liveAuthorized
      ? (liveExecuted ? 'LIVE SWAP SUCCESS REPORTED' : 'LIVE CONFIG READY · SWAP NOT YET VERIFIED')
      : resolvedMode === 'PAPER' ? 'PAPER EXECUTION · NO LIVE ORDERS'
      : `LIVE EXECUTION BLOCKED · ${executor.reason || modeReasons.join(', ') || resolvedMode}`;
    if (executionBadge) { executionBadge.textContent = executionText; executionBadge.className = `badge ${resolvedMode === 'LIVE' && liveAuthorized ? (liveExecuted ? 'info' : 'warn') : resolvedMode === 'PAPER' ? 'danger' : 'danger'}`; }
    if (executionFooter) executionFooter.textContent = executionText;
    if (settingsBadge) { settingsBadge.textContent = resolvedMode === 'LIVE' && liveAuthorized ? 'LIVE EXECUTOR CONFIGURED' : resolvedMode === 'PAPER' ? 'PAPER MODE' : 'LIVE EXECUTION BLOCKED'; settingsBadge.className = `badge ${resolvedMode === 'LIVE' && liveAuthorized ? 'info' : resolvedMode === 'PAPER' ? 'warn' : 'danger'}`; }
    if (contractTitle) contractTitle.textContent = resolvedMode === 'LIVE' && liveAuthorized ? 'Live execution adapter configured' : resolvedMode === 'PAPER' ? 'Paper-only execution' : 'Live execution is blocked';
    if (contractDescription) contractDescription.textContent = resolvedMode === 'LIVE' && liveAuthorized
      ? 'The local signer and Jupiter configuration passed startup checks. This does not prove RPC/balance readiness or a successful swap; stop-loss and take-profit decisions are managed by the running process, not resting on-chain orders.'
      : resolvedMode === 'PAPER' ? 'New positions are simulated. No live orders are submitted in PAPER mode.'
      : `New live entries are blocked: ${executor.reason || modeReasons.join(', ') || resolvedMode}. Do not remove safety gates to bypass this state.`;
    const heroSubtitle = $('#hero-subtitle');
    if (heroSubtitle) heroSubtitle.textContent = resolvedMode === 'LIVE' && liveAuthorized
      ? 'Live discovery → evidence verification → six-agent review → safety-gated Jupiter execution.'
      : 'Live discovery → evidence verification → six-agent review → guarded execution.';
    setText('#kpi-position-label', resolvedMode === 'LIVE' && liveAuthorized ? 'OPEN LIVE POSITIONS' : 'OPEN PAPER POSITIONS');
    setText('#kpi-equity-label', resolvedMode === 'LIVE' && liveAuthorized ? 'MODEL EQUITY · NOT WALLET' : 'VIRTUAL EQUITY');
    setText('#kpi-equity-note', resolvedMode === 'LIVE' && liveAuthorized ? 'Strategy reference equity; actual wallet balance is separate' : 'Simulated balance only');
    setText('#portfolio-title', resolvedMode === 'LIVE' && liveAuthorized ? 'Live trade ledger' : 'Paper portfolio');
    setText('#portfolio-eyebrow', resolvedMode === 'LIVE' && liveAuthorized ? 'LIVE TRADE LEDGER' : 'SIMULATED CAPITAL');
    setText('#portfolio-mode-tag', resolvedMode === 'LIVE' && liveAuthorized ? 'LIVE MODE' : 'PAPER ONLY');
    setText('#positions-eyebrow', resolvedMode === 'LIVE' && liveAuthorized ? 'LIVE EXECUTION LEDGER' : 'EXECUTION SIMULATION');
    setText('#positions-mode-note', resolvedMode === 'LIVE' && liveAuthorized ? 'Only confirmed swaps are recorded; active exits still depend on this process and market data.' : 'All positions are simulated; live orders are disabled.');
    setText('#anatomy-eyebrow', resolvedMode === 'LIVE' && liveAuthorized ? 'LIVE POSITION FORENSICS' : 'PAPER POSITION FORENSICS');
    setText('#anatomy-mode-tag', resolvedMode === 'LIVE' && liveAuthorized ? 'Live tracked state' : 'Paper state');
    setText('#runtime-mode-note', modeNote);
    const modeStamp = $('#runtime-mode-stamp');
    if (modeStamp) {
      modeStamp.classList.toggle('is-blocked', ['LIVE_BLOCKED','SAFE_STOP'].includes(resolvedMode));
      modeStamp.classList.toggle('is-watch', resolvedMode === 'WATCH_ONLY');
    }
    setText('#uptime', num(h.uptime_seconds) ? `${Math.floor(h.uptime_seconds/3600)}h ${Math.floor((h.uptime_seconds%3600)/60)}m` : 'N/A');
    setText('#kpi-lastscan', scan.last_scan_ts ? `${age(scan.last_scan_ts)} ago` : 'WAITING');
    setText('#kpi-lastscan-note', `${scan.last_scan_candidates ?? 0} candidate(s) last scan`);
    setText('#scan-interval', statusData.scanner ? 'Configured' : 'N/A');
    const healthComp = h.components || {};
    const providerRows = [
      ['Scanner', healthComp.intelligence?.state || scan.state],
      ['DEX / Market', healthComp.market_data?.state || statusData.market?.state],
      ['PumpPortal', healthComp.launch_stream?.state || statusData.launch_stream?.state],
      ['Solana RPC', findProvider('solana_rpc')?.state],
      ['RugCheck', findProvider('rugcheck')?.state],
      ['Jupiter', findProvider('jupiter')?.state],
      ['Helius', findProvider('helius')?.state || statusData.wallet_watch?.state],
    ];
    const counted = providerRows.map(([n,s]) => ({name:n, state:s || 'NOT_CONFIGURED', optional:n==='Helius'}));
    const core = counted.filter(p => !p.optional); // Helius wallet tracking is optional.
    const good = core.filter(x => ['HEALTHY','CONNECTED','ACTIVE','OK'].includes(String(x.state).toUpperCase())).length;
    const bad = core.filter(x => ['UNHEALTHY','ERROR','DISCONNECTED'].includes(String(x.state).toUpperCase())).length;
    const warn = core.length - good - bad;
    const percent = core.length ? Math.round(good/core.length*100) : 0;
    $('#health-ring').style.setProperty('--health-pct', `${percent}%`);
    setText('#health-pct', `${percent}%`); setText('#health-good', good); setText('#health-warn', warn); setText('#health-bad', bad);
    const overall = $('#overall-health'); overall.textContent = h.status || 'UNKNOWN'; overall.className = `status-pill ${statusClass(h.status)}`;
    $('#provider-mini').innerHTML = counted.map(p => `<div class="provider-chip"><span>${esc(p.name)}${p.optional?' · optional':''}</span>${pill(p.state)}</div>`).join('');
    const ready = h.paper_readiness || {};
    const alert = $('#readiness-alert');
    if (alert) {
      const blocked = String(ready.state || '').toUpperCase() === 'BLOCKED';
      alert.hidden = !blocked && !ready.state;
      alert.classList.toggle('is-blocked', blocked);
      alert.classList.toggle('is-configured', !blocked && Boolean(ready.state));
      setText('#readiness-title', blocked ? 'PAPER ENTRY BLOCKED' : 'PAPER ENTRY PREREQUISITES');
      const msg = blocked
        ? `Reason: ${(ready.blockers || ['REQUIRED_PROVIDER_UNAVAILABLE']).join(', ')}. Configure JUPITER_API_KEY in the local .env and restart. The bot will keep all entries blocked until each token has a verified route and complete critical safety evidence. Helius is optional.`
        : `${ready.note || 'Provider is configured; token-level safety checks are still mandatory. Helius wallet tracking is optional.'}`;
      setText('#readiness-message', msg);
    }
    setText('#provider-updated', 'Server-reported · Helius excluded from core health %');
  }
  function findProvider(needle) {
    return model.providers.find(p => `${p.name || ''} ${p.source || ''}`.toLowerCase().includes(needle.toLowerCase())) || null;
  }

  async function refreshCandidates() {
    const [candidates, pipeline, rejected] = await Promise.allSettled([get('/api/candidates'), get('/api/candidates/pipeline'), get('/api/rejections/by-reason?minutes=60')]);
    if (candidates.status === 'fulfilled') model.candidates = Array.isArray(candidates.value) ? candidates.value : [];
    setText('#kpi-candidates', model.candidates.length); setText('#radar-count', `${model.candidates.length} candidates`);
    renderCandidates();
    if (pipeline.status === 'fulfilled') {
      const p = pipeline.value.counts || pipeline.value;
      ['DISCOVERED','NEEDS_MARKET_ENRICHMENT','NEEDS_RISK_CHECK','READY_FOR_DECISION','ELIGIBLE_PAPER','PAPER_OPEN'].forEach(k => {
        const el = $(`#cnt-${k}`); if (el) el.textContent = p[k] ?? 0;
        const stage = $(`.stage[data-stage="${k}"]`); if (stage) stage.classList.toggle('active', Number(p[k] || 0) > 0);
      });
      setText('#pipeline-summary', `${pipeline.value.total_candidates ?? model.candidates.length} tracked candidates`);
    }
    if (rejected.status === 'fulfilled') renderRejections(rejected.value);
  }
  function candidateSafety(c) {
    if (c.safety_passed === true) return 'PASS';
    if (c.safety_passed === false) return 'BLOCKED / WAIT';
    return 'UNKNOWN';
  }
  function candidateAge(c) {
    const m = c.market || {};
    if (num(c.launch_ts) && Number(c.launch_ts)>0) return age(c.launch_ts);
    if (num(m.pair_age_seconds)) { const a=Number(m.pair_age_seconds); return a<60?`${Math.floor(a)}s`:a<3600?`${Math.floor(a/60)}m`:`${Math.floor(a/3600)}h`; }
    return 'UNKNOWN';
  }
  function renderCandidates() {
    const q = ($('#radar-search')?.value || '').trim().toLowerCase();
    const filtered = model.candidates.filter(c => !q || [c.symbol,c.mint,c.decision,c.block_reason,(c.reasons||[]).join(' ')].some(v => String(v||'').toLowerCase().includes(q)));
    const overview = $('#overview-candidates');
    if (overview) overview.innerHTML = model.candidates.slice(0,7).map(c => {
      const m=c.market||{}; const safety=candidateSafety(c); const mom=m.price_change_5m_pct;
      return `<tr><td class="token-cell"><b>${esc(c.symbol||'Unnamed token')}</b><small>${esc(c.mint ? c.mint.slice(0,12)+'…' : 'MINT UNKNOWN')}</small></td><td>${esc(candidateAge(c))}</td><td>${usd(m.liquidity_usd,0)}</td><td>${num(mom)?fmt(mom,1)+'%':'N/A'}</td><td>${pill(safety)}</td><td>${pill(c.decision||'UNKNOWN')}</td></tr>`;
    }).join('') || emptyRow(6,'No candidates persisted yet. Check provider health and scanner logs.');
    const tbody = $('#radar-table tbody'); if (!tbody) return;
    tbody.innerHTML = filtered.slice(0,250).map(c => {
      const m=c.market||{}; const safety=candidateSafety(c); const decision=c.decision||'UNKNOWN';
      const reason=c.block_reason || (Array.isArray(c.reasons)?c.reasons.join(', '):'—');
      return `<tr><td class="token-cell"><b>${esc(c.symbol||'Unnamed token')}</b><small class="mono">${esc(c.mint||'UNKNOWN')}</small></td><td>${esc(c.source||'UNKNOWN')}</td><td>${esc(candidateAge(c))}</td><td>${usd(m.liquidity_usd,0)}</td><td>${num(m.price_change_5m_pct)?fmt(m.price_change_5m_pct,1)+'%':'N/A'}</td><td>${pill(safety)}</td><td>${num(c.score)?fmt(c.score,2):'N/A'}</td><td>${pill(decision)}</td><td>${esc(reason || '—')}</td><td>${esc(age(c.last_seen_at))} ago</td></tr>`;
    }).join('') || emptyRow(10, q ? 'No candidates match the search.' : 'No candidates recorded yet.');
  }
  $('#radar-search')?.addEventListener('input', renderCandidates);

  function renderRejections(data) {
    const reasons = data.reason_counts || {};
    const pairs = Object.entries(reasons).sort((a,b)=>b[1]-a[1]).slice(0,8);
    setText('#rejection-window', `${data.total ?? 0} decisions · last hour`);
    if (!pairs.length) { $('#rejection-bars').innerHTML = '<div class="empty-state">No rejection history recorded in the selected window.</div>'; return; }
    const max = Math.max(...pairs.map(([,n])=>Number(n)||0),1);
    $('#rejection-bars').innerHTML = pairs.map(([name,n]) => `<div class="reject-row"><span class="reject-label" title="${esc(name)}">${esc(name)}</span><div class="reject-track"><div class="reject-fill" style="width:${Math.max(0,Math.min(100,Number(n)/max*100))}%"></div></div><b class="reject-value">${Number(n)||0}</b></div>`).join('');
  }

  async function refreshPortfolio() {
    const p = await get('/api/portfolio');
    setText('#kpi-equity', usd(p.equity)); setText('#kpi-positions', `${p.open_positions ?? 'N/A'} / ${p.max_total_open_positions ?? 'N/A'}`);
    setText('#kpi-position-cap', `Hard cap: ${p.max_total_open_positions ?? 'N/A'} total`);
    const totalPnl = p.valuation_complete === false || !num(p.realized_pnl) || !num(p.unrealized_pnl)
      ? null : Number(p.realized_pnl)+Number(p.unrealized_pnl);
    setText('#kpi-pnl', usd(totalPnl)); $('#kpi-pnl').className = totalPnl===null?'':totalPnl>=0?'positive':'negative';
    const markState = String(p.valuation_state || 'UNKNOWN').toUpperCase();
    const liveMode = String(model.health?.mode_resolution?.mode || model.health?.mode || '').toUpperCase() === 'LIVE' && Boolean(model.health?.live_authorized);
    const pnlPrefix = liveMode ? 'Tracked live PnL' : 'Paper PnL';
    setText('#kpi-pnl-note', markState==='FRESH' ? `${pnlPrefix} · marks fresh`
      : markState==='STALE' ? `${pnlPrefix} · ${Number(p.stale_marks||0)} stale mark(s)`
      : `${pnlPrefix} · valuation unavailable`);
    setText('#pf-today', usd(p.today_pnl)); setText('#pf-dlr', usd(p.daily_loss_remaining)); setText('#pf-win', p.win_rate===null?'N/A':pct(p.win_rate,100)); setText('#pf-factor', p.profit_factor===null?'N/A':(p.profit_factor===Infinity||p.profit_factor==='INF'?'∞':fmt(p.profit_factor,2)));
    const count=Number(p.open_positions||0), max=Number(p.max_total_open_positions||0); const cap=max>0?Math.min(100,count/max*100):0;
    $('#capacity-fill').style.width=`${cap}%`; setText('#capacity-label', `${count} / ${max} positions`);
    setText('#kpi-risk', p.daily_locked?'LOCKED':'NOT LOCKED'); setText('#kpi-risk-note', `Daily realized PnL: ${usd(p.today_pnl)}`);
  }

  async function refreshAgents() {
    const d = await get('/api/agents/status'); model.agents=d;
    const agents = d.agents || [];
    const cards = agents.map((a,i) => agentCard(a,i)).join('') || '<div class="empty-state">No agent evidence yet.</div>';
    $('#agent-grid').innerHTML=cards; $('#agents-full').innerHTML=cards;
    const cons=d.consensus||{}; setText('#consensus-label',cons.label||'N/A'); setText('#consensus-score',num(cons.score)?`Score ${fmt(cons.score,2)}`:'No score available');
    const table = (d.recent_decisions||[]).slice(0,120).map(x=>`<tr><td class="mono">${esc(x.mint?x.mint.slice(0,10)+'…':'—')}</td><td>${esc(x.agent||'UNKNOWN')}</td><td>${pill(x.status)}</td><td>${num(x.score)?fmt(x.score,2):'N/A'}</td><td>${esc((x.reason_codes||[]).join(', ')||'—')}</td><td>${esc(timeOnly(x.ts))}</td></tr>`).join('');
    $('#agent-decisions').innerHTML=table||emptyRow(6,'No reviewer decisions have been persisted yet.');
  }
  function agentCard(a,i) {
    const colors={KIBO:'var(--cyan)',YUZU:'var(--blue)',MOMO:'var(--violet)',RIN:'var(--amber)',SORA:'var(--green)',AKI:'var(--red)'};
    const name=a.name||a.agent||`AGENT ${i+1}`; const state=a.state||'IDLE'; const count=a.metrics_count ?? a.evaluations ?? 'N/A';
    const score=num(a.score)?fmt(a.score,2):'N/A'; const task=a.task||a.latest_reason||'No recent event';
    const waiting = Number(a.waiting_count||0);
    const activity = waiting ? `${esc(count)} eval · ${esc(waiting)} waiting` : `${esc(count)} eval`;
    return `<article class="agent-card" style="--agent-color:${colors[name]||'var(--muted)'}"><div class="agent-top"><span class="agent-id">NODE ${String(a.id||i+1).padStart(2,'0')}</span><i class="agent-led"></i></div><div class="agent-name">${esc(name)}</div><div class="agent-task">${esc(task)}</div><div class="agent-footer"><span class="agent-state">${esc(state)}</span><span>${activity} · score ${esc(score)}</span></div></article>`;
  }

  async function refreshPositions() {
    const [positions, fills] = await Promise.allSettled([get('/api/positions'),get('/api/fills')]);
    if (positions.status==='fulfilled') model.positions=Array.isArray(positions.value)?positions.value:[];
    if (fills.status==='fulfilled') model.fills=Array.isArray(fills.value)?fills.value:[];
    const open = model.positions.filter(p=>['OPEN','TP1_HIT'].includes(p.status));
    setText('#position-count', `${model.positions.length} positions · ${open.length} open`);
    $('#positions-table').innerHTML=model.positions.map(p=>{
      const pnl=num(p.realized_pnl)?Number(p.realized_pnl):null;
      const upnl=num(p.unrealized_pnl)?Number(p.unrealized_pnl):null;
      const markState=p.mark_state||'UNKNOWN';
      const markAge=num(p.mark_age_seconds)?`${Math.floor(Number(p.mark_age_seconds))}s`:'N/A';
      return `<tr><td class="token-cell"><b>${esc(p.symbol||'Unnamed token')}</b><small>${esc((p.mint||'').slice(0,12))}… · ${esc((p.id||'').slice(0,8))}</small></td><td>${pill(p.status)}</td><td class="mono">${esc(price(p.entry_price))}</td><td class="mono">${esc(price(p.last_mark_price))}<small>${esc(markAge)}</small></td><td class="mono">${esc(price(p.stop_price))}</td><td class="mono">${esc(price(p.tp1_price))}</td><td class="mono">${esc(price(p.tp2_price))}</td><td>${fmt(p.remaining_qty,4)}</td><td class="${pnl===null?'':pnl>=0?'positive':'negative'}">${usd(pnl,4)}</td><td class="${upnl===null?'':upnl>=0?'positive':'negative'}">${usd(upnl,4)}</td><td>${pill(markState)}</td><td>${p.trailing_active?'ACTIVE':'—'}${num(p.trailing_stop)?'<small class="mono"> '+esc(price(p.trailing_stop))+'</small>':''}</td><td>${esc(p.exit_reason||'—')}</td></tr>`;
    }).join('')||emptyRow(13, String(model.health?.mode_resolution?.mode || model.health?.mode || '').toUpperCase() === 'LIVE' ? 'No live positions recorded.' : 'No paper positions recorded.');
    $('#fills-table').innerHTML=model.fills.slice(0,100).map(f=>`<tr><td>${esc(timeOnly(f.ts))}</td><td class="mono">${esc((f.position_id||'').slice(0,12))}</td><td>${esc(f.kind||'—')}</td><td class="mono">${esc(price(f.price))}</td><td>${fmt(f.qty,5)}</td><td class="${Number(f.realized_pnl||0)>=0?'positive':'negative'}">${usd(f.realized_pnl,4)}</td><td>${esc(f.note||'—')}</td></tr>`).join('')||emptyRow(7,'No persisted fills yet.');
    const select=$('#position-select'); const previous=select.value || model.selectedPositionId;
    select.innerHTML='<option value="">Select a paper position</option>'+model.positions.map(p=>`<option value="${esc(p.id)}" ${p.id===previous?'selected':''}>${esc(p.symbol||p.mint.slice(0,8))} · ${esc(p.status)} · ${esc((p.id||'').slice(0,6))}</option>`).join('');
    if (!select.value && model.positions.length) select.value=(open[0]||model.positions[0]).id;
    if (!model.selectedPositionId && select.value) model.selectedPositionId=select.value;
    if (select.value!==model.selectedPositionId) model.selectedPositionId=select.value;
    renderTradeLevels();
  }
  function renderTradeLevels() {
    const p=model.positions.find(x=>x.id===($('#position-select')?.value || model.selectedPositionId));
    if (!p) { $('#trade-levels').innerHTML='<div class="empty-state">No position selected.</div>'; setText('#trade-reason-text','N/A'); setText('#chart-title','Observed price samples'); return; }
    model.selectedMint=p.mint; model.selectedPositionId=p.id; setText('#chart-title',`${p.symbol||p.mint.slice(0,8)} · ${String(p.id||'').slice(0,8)} · observed samples`);
    const unrealizedColor=Number(p.unrealized_pnl||0)>=0?'target':'stop';
    $('#trade-levels').innerHTML=[['STATUS',p.status,''],['ENTRY',price(p.entry_price),''],['MARK',price(p.last_mark_price),''],['MARK QUALITY',p.mark_state||'UNKNOWN',''],['MARK AGE',num(p.mark_age_seconds)?`${Math.floor(p.mark_age_seconds)} seconds`:'N/A',''],['STOP LOSS',price(p.stop_price),'stop'],['TP1',price(p.tp1_price),'target'],[p.meta?.fast_launch_runner?'TP2 / RUNNER ARM':'TP2',price(p.tp2_price),'target'],['TRAILING STOP',p.trailing_active?price(p.trailing_stop):'INACTIVE',''],['REMAINING QTY',fmt(p.remaining_qty,5),''],['REALIZED PnL',usd(p.realized_pnl,4),Number(p.realized_pnl||0)>=0?'target':'stop'],['UNREALIZED PnL',p.unrealized_pnl===null?'N/A':usd(p.unrealized_pnl,4),unrealizedColor],['ENTRY FEE ALLOCATED TO REMAINING',usd(p.remaining_entry_fee,5),'']].map(([n,v,c])=>`<div class="level-row ${c}"><span>${esc(n)}</span><b>${esc(v)}</b></div>`).join('');
    setText('#trade-reason-text',p.exit_reason||p.entry_reason||'No entry/exit reason recorded.');
    loadChart(p.mint);
  }
  $('#position-select')?.addEventListener('change', e=>{model.selectedPositionId=e.target.value;renderTradeLevels();});
  let chartRequestMint='';
  async function loadChart(mint) {
    if (!mint) { clearChart('Select a token with collected samples.'); return; }
    if (chartRequestMint===mint && model.chartLoadedAt && Date.now()-model.chartLoadedAt<4000) return;
    chartRequestMint=mint;
    try {
      const data=await get('/api/charts/'+encodeURIComponent(mint));
      model.chartLoadedAt=Date.now(); model.chartData=data;
      setText('#chart-label',data.label||'OBSERVED SAMPLES · NOT EXCHANGE OHLCV');
      const cov=data.coverage||{};
      setText('#observed-candle-note',`${cov.source||'Observed samples'} · ${cov.bars??(data.bars||[]).length} bars · ${cov.status||data.status||'UNKNOWN'} · not exchange OHLCV`);
      const bars=data.bars||[];
      if (bars.length<3) { clearChart(`INSUFFICIENT_HISTORY — ${bars.length} observed sample(s).`); return; }
      $('#chart-note').hidden=true; drawBars(bars);
    } catch (e) { clearChart('Chart unavailable: '+e.message); }
  }
  function clearChart(note) { const cv=$('#price-chart'); if (!cv) return; const ctx=cv.getContext('2d'); ctx.clearRect(0,0,cv.width,cv.height); $('#chart-note').hidden=false; setText('#chart-note',note); }
  function drawChart() { if (model.chartData?.bars?.length>=3) drawBars(model.chartData.bars); }
  function drawBars(bars) {
    const canvas=$('#price-chart'); if (!canvas) return;
    const rect=canvas.getBoundingClientRect(); if (rect.width<5) return;
    const dpr=window.devicePixelRatio||1; canvas.width=Math.floor(rect.width*dpr); canvas.height=Math.floor(rect.height*dpr);
    const ctx=canvas.getContext('2d'); ctx.scale(dpr,dpr); const w=rect.width,h=rect.height;
    ctx.clearRect(0,0,w,h); const pad={l:14,r:56,t:18,b:22}; const highs=bars.map(b=>Number(b.high)).filter(Number.isFinite), lows=bars.map(b=>Number(b.low)).filter(Number.isFinite);
    if(!highs.length||!lows.length){clearChart('No valid observed OHLC samples.');return;}
    let top=Math.max(...highs),bottom=Math.min(...lows); if(top===bottom){top*=1.001;bottom*=.999;} const y=v=>pad.t+(top-v)/(top-bottom)*(h-pad.t-pad.b); const innerW=w-pad.l-pad.r; const step=innerW/bars.length; const body=Math.max(2,Math.min(8,step*.5));
    ctx.font='9px ui-monospace,monospace'; ctx.strokeStyle='#19283b'; ctx.fillStyle='#8194ae'; ctx.lineWidth=1;
    for(let i=0;i<=4;i++){const value=top-(top-bottom)*i/4, yy=y(value);ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(w-pad.r,yy);ctx.stroke();ctx.fillText(price(value),w-pad.r+5,yy+3);}
    bars.forEach((b,i)=>{const x=pad.l+step*(i+.5);const o=Number(b.open),c=Number(b.close),hi=Number(b.high),lo=Number(b.low);const up=c>=o;ctx.strokeStyle=up?'#2bdd9a':'#ff637c';ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.moveTo(x,y(hi));ctx.lineTo(x,y(lo));ctx.stroke();const yy=Math.min(y(o),y(c)),hh=Math.max(1,Math.abs(y(o)-y(c)));ctx.fillRect(x-body/2,yy,body,hh);});
    ctx.fillStyle='#6f839e';ctx.fillText('OBSERVED',pad.l,h-6);
  }

  async function refreshWallets() {
    const w=await get('/api/wallet-watch'); model.wallet=w;
    const health=w.health||{}; setText('#wallet-provider-state',health.state||'UNKNOWN'); $('#wallet-provider-state').className=`status-pill ${statusClass(health.state)}`;
    const wallets=w.wallets||[]; setText('#wallet-count',`${wallets.length} tracked`);
    $('#wallet-table').innerHTML=wallets.map(x=>`<tr><td class="mono">${esc(x.address||'')}</td><td>${esc(x.label||'—')}</td><td>${pill(x.status||health.state||'UNKNOWN')}</td><td>${esc(timeOnly(x.added_at||x.created_at))}</td><td><button class="delete-btn" data-remove-wallet="${esc(x.address||'')}">Remove</button></td></tr>`).join('')||emptyRow(5,'No wallets added.');
    const events=w.events||[]; $('#wallet-events').innerHTML=events.slice(0,50).map(e=>`<li><span class="event-time">${esc(timeOnly(e.ts||e.timestamp))}</span><span class="event-component">${esc(e.wallet_label||e.label||'WALLET')}</span><span class="event-message">${esc(e.summary||e.description||e.type||e.signature||'Observed wallet event')}</span></li>`).join('')||'<li class="empty-state">No wallet events recorded. This does not mean monitored wallets are inactive.</li>';
  }
  $('#wallet-watch-form')?.addEventListener('submit',async e=>{
    e.preventDefault(); const msg=$('#wallet-message'); msg.textContent='Adding wallet…';
    try { await send('/api/wallet-watch','POST',{address:$('#wallet-address').value.trim(),label:$('#wallet-label').value.trim()}); $('#wallet-address').value='';$('#wallet-label').value='';msg.textContent='Wallet added to the local watchlist.';await refreshWallets(); }
    catch(err){msg.textContent='Could not add wallet: '+err.message;}
  });
  $('#wallet-table')?.addEventListener('click',async e=>{const b=e.target.closest('[data-remove-wallet]');if(!b)return;try{await fetch('/api/wallet-watch/'+encodeURIComponent(b.dataset.removeWallet),{method:'DELETE'});await refreshWallets();}catch(err){$('#wallet-message').textContent='Remove failed: '+err.message;}});

  async function refreshLogs() {
    const qs=new URLSearchParams({limit:'200'}); const level=$('#log-level')?.value||'';const comp=$('#log-component')?.value.trim()||'';if(level)qs.set('level',level);if(comp)qs.set('component',comp);
    const logs=await get('/api/logs?'+qs.toString());model.logs=Array.isArray(logs)?logs:[];setText('#log-count',`${model.logs.length} events`);
    $('#log-table').innerHTML=model.logs.map(r=>`<tr><td class="mono">${esc(timeOnly(r.ts))}</td><td><span class="log-level ${statusClass(r.level==='ERROR'?'ERROR':r.level==='WARN'?'WARN':'INFO')}">${esc(r.level||'INFO')}</span></td><td>${esc(r.component||'—')}</td><td class="mono">${esc(r.event||'—')}</td><td>${esc(r.message||'—')}</td></tr>`).join('')||emptyRow(5,'No log events match the current filters.');
    $('#event-stream').innerHTML=model.logs.slice(0,8).map(r=>`<li><span class="event-time">${esc(timeOnly(r.ts))}</span><span class="event-component">${esc(r.component||'APP')}</span><span class="event-message">${esc(r.message||r.event||'—')}</span></li>`).join('')||'<li class="empty-state">No recent runtime events.</li>';
  }
  $('#log-refresh')?.addEventListener('click',refreshLogs);
  $('#log-level')?.addEventListener('change',refreshLogs);
  let compTimer=null;$('#log-component')?.addEventListener('input',()=>{clearTimeout(compTimer);compTimer=setTimeout(refreshLogs,300);});

  async function refreshSettings() {
    const s=await get('/api/settings');const allowed=Object.entries(s||{}).filter(([k,v])=>!/(key|secret|token|password|url)/i.test(k)&&['string','number','boolean'].includes(typeof v));
    $('#settings-grid').innerHTML=allowed.map(([k,v])=>`<div class="setting-cell"><span>${esc(k.replaceAll('_',' ').toUpperCase())}</span><b>${esc(typeof v==='boolean'?String(v).toUpperCase():String(v))}</b></div>`).join('')||'<div class="empty-state">No public settings were returned.</div>';
    if(s.scan_interval_seconds!=null)setText('#scan-interval',`${s.scan_interval_seconds}s`);
  }

  async function refreshFastPath() {
    const result = await Promise.allSettled([get('/api/fast-path/status'), get('/api/system/status'), get('/api/continuation')]);
    const fast = result[0].status === 'fulfilled' ? result[0].value : {};
    const system = result[1].status === 'fulfilled' ? result[1].value : {};
    const contRows = result[2].status === 'fulfilled' && Array.isArray(result[2].value) ? result[2].value : [];
    const bus = fast.bus || {}, sniper = fast.sniper || {}, retries = fast.retries || {}, cont = fast.continuation || {};
    const monitor = fast.position_monitor || system.position_monitor || {};
    const core = system.core_readiness || {};
    setText('#fastpath-state', core.state || bus.state || 'UNKNOWN');
    const stateEl = $('#fastpath-state'); if (stateEl) stateEl.className = `status-pill ${statusClass(core.state || bus.state)}`;
    setText('#glance-events', num(sniper.events_handled) ? sniper.events_handled : 0);
    setText('#glance-queue', num(bus.queue_depth) ? bus.queue_depth : 0);
    setText('#glance-latency', num(sniper.decision_latency_p95_ms) ? `${fmt(sniper.decision_latency_p95_ms,1)} ms` : 'N/A');
    setText('#glance-retries', num(retries.scheduled) ? retries.scheduled : 0);
    const contTotal = num(cont.classified) ? cont.classified : 0;
    setText('#glance-continuation', contTotal);
    setText('#fast-events', num(sniper.events_handled) ? sniper.events_handled : 0);
    setText('#fast-event-types', `${sniper.fresh_events || 0} launches · ${sniper.migrations_handled || 0} migrations · ${sniper.retries_handled || 0} retries`);
    setText('#fast-queue', num(bus.queue_depth) ? bus.queue_depth : 0);
    setText('#fast-workers', `${bus.workers || 0} workers · ${bus.state || 'STOPPED'}`);
    setText('#fast-opened', num(sniper.opened_paper) ? sniper.opened_paper : 0);
    setText('#fast-p95', num(sniper.decision_latency_p95_ms) ? `${fmt(sniper.decision_latency_p95_ms,1)} ms` : 'N/A');
    setText('#fast-p50', `P50: ${num(sniper.decision_latency_p50_ms) ? `${fmt(sniper.decision_latency_p50_ms,1)} ms` : 'N/A'} · P99: ${num(sniper.decision_latency_p99_ms) ? `${fmt(sniper.decision_latency_p99_ms,1)} ms` : 'N/A'}`);
    setText('#fast-retries', num(retries.scheduled) ? retries.scheduled : 0);
    setText('#fast-retry-detail', `${retries.dispatching || 0} dispatching · ${retries.exhausted || 0} exhausted · max ${retries.max_attempts || 0} attempts`);
    setText('#fast-misses', num(sniper.entry_window_misses) ? sniper.entry_window_misses : 0);
    setText('#fast-monitor', monitor.alive ? `${fmt(monitor.interval_seconds,1)}s` : 'STOPPED');
    setText('#fast-monitor-detail', `${monitor.positions_marked || 0} marks · ${monitor.positions_closed || 0} exits · ${monitor.price_missing || 0} missing prices`);
    setText('#bus-status', bus.state || 'UNKNOWN');
    const busDetails = [
      ['Queue Depth', bus.queue_depth], ['Enqueued', bus.enqueued], ['Processed', bus.processed],
      ['Dispatch P95', num(bus.dispatch_p95_ms) ? `${fmt(bus.dispatch_p95_ms,1)} ms` : null],
      ['Total Latency P95', num(bus.total_p95_ms) ? `${fmt(bus.total_p95_ms,1)} ms` : null],
      ['Duplicates Dropped', bus.duplicates_dropped], ['Queue Full Events', bus.dropped_on_full], ['Worker Errors', bus.errors],
      ['Core Readiness', core.state], ['Data Completeness', num(system.data_completeness?.pct_complete) ? `${fmt(system.data_completeness.pct_complete,1)}%` : null], ['Position Monitor', monitor.alive ? 'RUNNING' : 'STOPPED'], ['Monitor Cycle', num(monitor.last_cycle_ms) ? `${fmt(monitor.last_cycle_ms,1)} ms` : null]
    ];
    const bd = $('#fast-bus-details');
    if (bd) bd.innerHTML = busDetails.map(([label,value])=>`<div class="setting-cell"><span>${esc(label)}</span><b>${esc(value === null || value === undefined ? 'N/A' : String(value))}</b></div>`).join('');

    const counts = cont.counts || {};
    setText('#continuation-count', `${cont.classified || 0} classified`);
    const countEntries = Object.entries(counts).sort((a,b)=>b[1]-a[1]);
    const maxCount = Math.max(1,...countEntries.map(x=>Number(x[1]||0)));
    const stateList = $('#continuation-state-list');
    if (stateList) stateList.innerHTML = countEntries.map(([name,count])=>`<div class="bar"><div class="label mono" title="${esc(name)}">${esc(name)}</div><div class="track"><div class="fill" style="width:${Math.max(0,Math.min(100,Number(count||0)/maxCount*100)).toFixed(1)}%"></div></div><div class="n">${esc(count)}</div></div>`).join('') || '<div class="empty-state">No continuation states recorded yet.</div>';

    // Build this table only from persisted candidates that have real fast-path evidence.
    const rows = (model.candidates || []).filter(c => c.sniper && Object.keys(c.sniper).length || c.continuation && Object.keys(c.continuation).length).slice(0,40);
    const body = $('#fastpath-candidates');
    if (body) body.innerHTML = rows.map(c=>{
      const sn = c.sniper || {}, co = c.continuation || {}, market = c.market || {};
      const latency = num(c.sniper_latency_ms) ? `${fmt(c.sniper_latency_ms,1)} ms` : (num(sn.entry_age_seconds) ? `age ${fmt(sn.entry_age_seconds,1)}s` : 'N/A');
      return `<tr><td><b>${esc(c.symbol||'—')}</b><div class="mono">${esc(String(c.mint||'').slice(0,12))}…</div></td><td>${pill(c.decision||'UNKNOWN')}</td><td>${pill(sn.state||'NOT_QUEUED')}</td><td>${pill(co.state||'NOT_OBSERVED')}</td><td>${esc(latency)}</td><td class="mono">${esc(c.block_reason||'—')}</td><td>${esc(timeOnly(c.last_seen_at))}</td></tr>`;
    }).join('') || emptyRow(7,'No persisted fast-path activity yet. New events will appear here after runtime dispatch.');
  }

  let refreshing=false;
  async function refreshAll() {
    if(refreshing)return;refreshing=true;
    try {
      const tasks=await Promise.allSettled([refreshHealth(),refreshCandidates(),refreshPortfolio(),refreshAgents(),refreshPositions(),refreshWallets(),refreshLogs(),refreshSettings(),refreshFastPath()]);
      const failed=tasks.filter(x=>x.status==='rejected').length;
      setText('#last-refresh',`${new Date().toLocaleTimeString()} · ${failed?`${failed} section(s) unavailable`:'all requested sections refreshed'}`);
    } finally {refreshing=false;}
  }
  function tick(){setText('#clock',new Date().toISOString().replace('T',' ').slice(0,19)+' UTC');}
  tick();refreshAll();setInterval(tick,1000);setInterval(refreshAll,5000);
  window.addEventListener('resize',()=>{if($('#tab-anatomy')?.classList.contains('active'))drawChart();});

  async function refreshSocialRadar() {
    try {
      const [health, data] = await Promise.all([get('/api/social-radar/health'), get('/api/social-radar?limit=100')]);
      setText('#social-state', health.state || 'UNKNOWN');
      const stateEl = $('#social-state'); if (stateEl) stateEl.className = `status-pill ${statusClass(health.state)}`;
      setText('#social-x-state', health.x?.configured ? (health.x?.error || (health.x?.last_poll ? 'CONNECTED' : 'WAITING')) : 'NOT_CONFIGURED');
      setText('#social-x-note', health.x?.error || (health.x?.last_poll ? `Last poll ${age(health.x.last_poll)} ago` : 'Bearer token required'));
      setText('#social-tg-state', health.telegram?.configured ? (health.telegram?.error || (health.telegram?.last_poll ? 'CONNECTED' : 'WAITING')) : 'NOT_CONFIGURED');
      setText('#social-tg-note', health.telegram?.error || (health.telegram?.last_poll ? `Last poll ${age(health.telegram.last_poll)} ago` : 'Bot token and accessible channels required'));
      setText('#social-count', health.items_cached ?? 0);
      const trends = $('#social-trends');
      if (trends) trends.innerHTML = (data.trends || []).length ? data.trends.map(t => `<div class="social-trend-chip"><b>${esc(t.key)}</b><span>${fmt(t.mentions_weighted,1)} recency-weighted mentions</span></div>`).join('') : '<div class="empty-state">No ranked mentions yet. Configure sources and ensure the bot can read selected channels.</div>';
      const body = $('#social-items');
      if (body) body.innerHTML = (data.items || []).length ? data.items.map(item => {
        const labels = [...(item.mint ? [item.mint] : []), ...(item.tickers || []).map(x => '$'+x)].join(' · ') || 'No contract/ticker extracted';
        const timestamp = item.created_at ? when(item.created_at) : 'Unknown';
        const link = item.url ? `<a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">Open source</a>` : '—';
        return `<tr><td>${esc(item.source)}</td><td class="social-source-text">${esc(item.text)}</td><td>${esc(labels)}</td><td>${esc(timestamp)}</td><td>${link}</td></tr>`;
      }).join('') : '<tr><td colspan="5" class="empty-state">No social messages collected yet.</td></tr>';
    } catch (e) {
      setText('#social-state', 'ERROR');
    }
  }
  const socialTab = $('#tab-social');
  if (socialTab) {
    refreshSocialRadar();
    setInterval(refreshSocialRadar, 15000);
  }
})();

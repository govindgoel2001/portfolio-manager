/* Portfolio console.
   One page, four views, no build step. State lives in `S`, every render is a
   full redraw of the content area, and the broker is polled on a timer so the
   numbers on screen are the numbers in the paper account. */

const S = {
  view: 'overview',
  cls: 'all',
  universe: null,
  health: null,
  portfolio: null,
  run: null,
  quotes: {},
  prevQuotes: {},
  curve: null,
  autopilot: null,
  concentration: null,
  orders: [],
  busy: new Set(),
};

const POLL_MS = 15000;
const $ = (sel) => document.querySelector(sel);
const el = (id) => document.getElementById(id);

const fmtUSD = (n, d = 2) =>
  (n < 0 ? '-$' : '$') + Math.abs(Number(n) || 0).toLocaleString('en-US',
    { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtPct = (n, d = 1) => `${(Number(n) * 100).toFixed(d)}%`;
const fmtSigned = (n, d = 1) => `${n >= 0 ? '+' : ''}${(Number(n) * 100).toFixed(d)}%`;
const cls = (n) => (n > 0 ? 'pos' : n < 0 ? 'neg' : '');
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'content-type': 'application/json' },
    ...opts,
  });
  if (r.status === 401) { showLogin(); throw new Error('not authenticated'); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `${r.status} ${r.statusText}`);
  return body;
}

function toast(msg, kind = '') {
  const t = document.createElement('div');
  t.className = `toast ${kind}`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 5200);
}

/* ---------------- auth ---------------- */

function showLogin() {
  el('login').hidden = false;
  el('app').hidden = true;
}

el('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  el('login-err').textContent = '';
  try {
    await api('/api/login', { method: 'POST', body: JSON.stringify({ password: el('pw').value }) });
    el('login').hidden = true;
    el('app').hidden = false;
    boot();
  } catch (err) {
    el('login-err').textContent = err.message;
  }
});

el('btn-logout').addEventListener('click', async () => {
  await fetch('/api/logout', { method: 'POST' });
  location.reload();
});

/* ---------------- boot ---------------- */

async function start() {
  const s = await fetch('/api/session').then((r) => r.json());
  if (!s.authenticated) return showLogin();
  el('login').hidden = true;
  el('app').hidden = false;
  boot();
}

async function boot() {
  try {
    const [universe, health] = await Promise.all([api('/api/universe'), api('/api/health')]);
    S.universe = universe;
    S.health = health;
    renderSidebar();
    renderHealth();
    await Promise.all([refreshPortfolio(), loadRun(), loadCurve(),
                       loadAutopilot(), loadConcentration()]);
    render();
    setInterval(tick, POLL_MS);

    // The filings rail refreshes on its own slower clock. These are SEC and
    // House Clerk documents on a lag of days, so polling them at the price
    // tick rate would be pure noise and pure load.
    loadSmartMoney();
    setInterval(loadSmartMoney, 10 * 60 * 1000);

    // The heatmap, the leaderboard and the edge all move on the scale of a
    // day, not a tick. Loaded once and left alone.
    loadObjective();
    loadBasket(null).then(() => { if (S.view === 'basket') render(); });
    loadConnections().then(() => { if (S.view === 'connect') render(); });
    loadSectors().then(() => { if (S.view === 'sectors') render(); });
    loadTrackers().then(() => { if (S.view === 'trackers') render(); });
  } catch (err) {
    el('content').innerHTML = `<div class="empty">could not load: ${esc(err.message)}</div>`;
  }
}

async function tick() {
  try {
    await refreshPortfolio();
    if (S.view === 'overview') render();
    el('live-label').textContent = 'live';
  } catch {
    el('live-label').textContent = 'stalled';
  }
}

async function refreshPortfolio() {
  const [p, q] = await Promise.all([api('/api/portfolio'), api('/api/quotes')]);
  S.prevQuotes = S.quotes;
  S.portfolio = p;
  S.quotes = q;
  renderTopBar();
  renderTape();
}

async function loadRun() {
  try { S.run = await api('/api/run/latest'); }
  catch { S.run = null; }
  renderPendingPill();
}

async function loadCurve() {
  try { S.curve = await api('/api/equity-curve'); }
  catch { S.curve = { points: [] }; }
}

async function loadAutopilot() {
  try { S.autopilot = await api('/api/autopilot'); }
  catch { S.autopilot = null; }
  renderAutopilot();
}

async function loadConcentration() {
  try { S.concentration = await api('/api/concentration'); }
  catch { S.concentration = null; }
}

function renderAutopilot() {
  const btn = el('btn-autopilot');
  const label = el('autopilot-label');
  if (!btn || !S.autopilot) return;
  const armed = S.autopilot.armed && S.autopilot.enabled_in_config;
  btn.classList.toggle('armed', armed);
  btn.querySelector('.dot').className = `dot ${armed ? 'ok' : 'bad'}`;
  label.textContent = armed
    ? `AUTOPILOT ON  ${S.autopilot.trades_today}/${S.autopilot.max_trades_per_day}`
    : 'AUTOPILOT OFF';
  btn.title = S.autopilot.enabled_in_config
    ? 'Event-driven automatic execution. Click to toggle.'
    : 'Disabled in risk.yaml. Set autopilot.enabled to true to allow arming.';
}

el('btn-autopilot').addEventListener('click', async () => {
  if (!S.autopilot) return;
  if (!S.autopilot.enabled_in_config) {
    toast('Autopilot is switched off in risk.yaml. Set autopilot.enabled: true first.', 'bad');
    return;
  }
  const next = !S.autopilot.armed;
  const cap = (S.autopilot.max_notional_per_trade_pct * 100).toFixed(0);
  const msg = 'Arm autopilot? Material news will place paper orders without '
    + 'asking, sized by quarter Kelly and capped at ' + cap + '% of equity '
    + 'per trade, ' + S.autopilot.max_trades_per_day + ' trades a day.';
  if (next && !confirm(msg)) return;
  try {
    await api('/api/autopilot', { method: 'POST', body: JSON.stringify({ armed: next }) });
    await loadAutopilot();
    toast(next ? 'Autopilot armed.' : 'Autopilot disarmed.', next ? 'good' : '');
  } catch (err) { toast(err.message, 'bad'); }
});

el('btn-news').addEventListener('click', async () => {
  const btn = el('btn-news');
  btn.disabled = true; btn.textContent = 'Scanning';
  try {
    const r = await api('/api/scan-news', { method: 'POST' });
    toast(r.ok ? 'News scan complete.' : 'News scan failed, see order log.',
          r.ok ? 'good' : 'bad');
    await Promise.all([loadRun(), loadAutopilot(), refreshPortfolio()]);
    render();
  } catch (err) { toast(err.message, 'bad'); }
  finally { btn.disabled = false; btn.textContent = 'Scan news'; }
});

/* ---------------- sidebar ---------------- */

function classSymbols(key) {
  if (key === 'all') return null;
  const c = S.universe.classes.find((x) => x.key === key);
  return c ? new Set(c.symbols) : new Set();
}

function inClass(symbol, key) {
  if (key === 'all') return true;
  const set = classSymbols(key);
  return set.has(String(symbol).toUpperCase());
}

function renderSidebar() {
  const exposure = S.portfolio?.exposure_by_class || {};
  const positions = S.portfolio?.positions || [];
  const rows = [{ key: 'all', label: 'All', symbols: [], proxy_for: null, broker: '' }]
    .concat(S.universe.classes);

  el('classes').innerHTML = rows.map((c) => {
    const held = c.key === 'all'
      ? positions.length
      : positions.filter((p) => inClass(p.symbol, c.key)).length;
    const w = c.key === 'all'
      ? Object.values(exposure).reduce((a, b) => a + b, 0)
      : (exposure[c.key] || 0);
    const universeCount = c.key === 'all'
      ? S.universe.classes.reduce((a, x) => a + x.symbols.length, 0)
      : c.symbols.length;
    return `
      <button class="class-btn ${S.cls === c.key ? 'active' : ''}" data-cls="${c.key}">
        <div class="row">
          <span class="name">${esc(c.label)}</span>
          <span class="count">${held}/${universeCount}</span>
        </div>
        ${c.proxy_for ? `<span class="proxy">proxy ${esc(c.proxy_for)}</span>` : ''}
        <div class="bar"><i style="width:${Math.min(100, w * 250).toFixed(1)}%"></i></div>
      </button>`;
  }).join('');

  el('classes').querySelectorAll('.class-btn').forEach((b) =>
    b.addEventListener('click', () => { S.cls = b.dataset.cls; renderSidebar(); render(); renderTape(); }));
}

function renderHealth() {
  const h = S.health;
  const lines = h.brokers.map((b) =>
    `<div><span class="dot ${b.ok ? 'ok' : 'bad'}"></span>${esc(b.name)} <span class="dim">${esc(b.mode)}</span></div>`
  );
  for (const [name, why] of Object.entries(h.failures || {})) {
    lines.push(`<div title="${esc(why)}"><span class="dot warn"></span>${esc(name)} <span class="dim">unavailable</span></div>`);
  }
  const backend = esc(h.llm_backend || (h.llm ? 'connected' : 'not configured'));
  lines.push(`<div style="margin-top:8px" title="${backend}">
    <span class="dot ${h.llm ? 'ok' : 'warn'}"></span>${backend.split(' (')[0]}</div>`);
  el('health').innerHTML = lines.join('');

  const live = h.brokers.find((b) => b.ok);
  el('mode-line').textContent = live
    ? `${live.mode} · ${live.name.replace(/_/g, ' ')}`
    : 'no broker connected';
}

function renderPendingPill() {
  const props = S.run?.proposals || [];
  const n = props.filter((p) => p.status === 'pending').length;
  const pill = el('pending-pill');
  pill.textContent = n;
  pill.className = `pill ${n ? '' : 'zero'}`;

  const ev = props.filter((p) => p.event).length;
  const epill = el('events-pill');
  if (epill) { epill.textContent = ev; epill.className = `pill ${ev ? '' : 'zero'}`; }
}

/* ---------------- top bar and tape ---------------- */

function renderTopBar() {
  const p = S.portfolio;
  if (!p) return;
  const shown = p.positions.filter((x) => inClass(x.symbol, S.cls));
  const pl = shown.reduce((a, x) => a + x.unrealized_pl, 0);
  const cost = shown.reduce((a, x) => a + (x.market_value - x.unrealized_pl), 0);
  el('kpi-equity').textContent = fmtUSD(p.equity);
  el('kpi-cash').innerHTML = `${fmtUSD(p.cash, 0)} <span class="dim" style="font-size:12px">${fmtPct(p.cash_weight)}</span>`;
  el('kpi-pl').innerHTML = `<span class="${cls(pl)}">${fmtUSD(pl)}</span>` +
    (cost > 0 ? ` <span class="dim" style="font-size:12px">${fmtSigned(pl / cost)}</span>` : '');
  el('kpi-count').textContent = `${shown.length}`;
}

function renderTape() {
  if (!S.universe) return;
  const syms = S.cls === 'all'
    ? S.universe.classes.flatMap((c) => c.symbols).slice(0, 14)
    : (classSymbols(S.cls) ? [...classSymbols(S.cls)] : []);
  el('tape').innerHTML = syms.map((s) => {
    const q = S.quotes[s];
    if (!q) return `<div class="t"><span class="s">${esc(s)}</span><span class="p dim">no data</span></div>`;
    const prev = S.prevQuotes[s]?.price;
    const dir = prev == null || prev === q.price ? '' : (q.price > prev ? 'up' : 'down');
    const px = q.price >= 1000 ? q.price.toFixed(0) : q.price.toFixed(2);
    return `<div class="t ${dir}"><span class="s">${esc(s)}</span><span class="p">${px}</span>${q.stale ? '<span class="dim">stale</span>' : ''}</div>`;
  }).join('');
}

/* ---------------- views ---------------- */

document.querySelectorAll('.nav-btn').forEach((b) =>
  b.addEventListener('click', () => {
    S.view = b.dataset.view;
    document.querySelectorAll('.nav-btn').forEach((x) => x.classList.toggle('active', x === b));
    render();
  }));

el('btn-run').addEventListener('click', async () => {
  const btn = el('btn-run');
  btn.disabled = true; btn.textContent = 'Scanning';
  try {
    const r = await api('/api/run', { method: 'POST' });
    toast(r.ok ? 'Scan complete. Memo updated.' : 'Scan failed, see order log.', r.ok ? 'good' : 'bad');
    await Promise.all([loadRun(), loadCurve(), refreshPortfolio(),
                       loadConcentration(), loadAutopilot()]);
    render();
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    btn.disabled = false; btn.textContent = 'Run scan';
  }
});

function render() {
  renderSidebar();
  renderTopBar();
  const c = el('content');
  if (S.view === 'overview') c.innerHTML = viewOverview();
  else if (S.view === 'proposals') c.innerHTML = viewProposals();
  else if (S.view === 'events') c.innerHTML = viewEvents();
  else if (S.view === 'memo') c.innerHTML = viewMemo();
  else if (S.view === 'committee') c.innerHTML = viewCommittee();
  else if (S.view === 'smartmoney') c.innerHTML = viewSmartMoney();
  else if (S.view === 'manual') c.innerHTML = viewManual();
  else if (S.view === 'basket') c.innerHTML = viewBasket();
  else if (S.view === 'connect') c.innerHTML = viewConnect();
  else if (S.view === 'sectors') c.innerHTML = viewSectors();
  else if (S.view === 'trackers') c.innerHTML = viewTrackers();
  else c.innerHTML = viewOrders();
  wireActions();
  wireCommittee();
  wireManual();
  wireSectors();
  wireBasket();
  wireConnect();
  if (S.view === 'overview') drawCurve();
}

/* --- overview --- */

function viewOverview() {
  const p = S.portfolio;
  const positions = (p?.positions || []).filter((x) => inClass(x.symbol, S.cls));
  const label = S.cls === 'all' ? 'all classes'
    : S.universe.classes.find((c) => c.key === S.cls)?.label.toLowerCase();

  let out = `
    <div class="panel curve-panel">
      <h2>Equity vs benchmark<span class="dim" id="curve-note"></span></h2>
      <div class="body">
        <svg class="spark" id="curve" preserveAspectRatio="none"></svg>
        <div class="legend">
          <span><i style="background:var(--gold)"></i>portfolio</span>
          <span><i style="background:var(--muted)"></i>${esc(S.universe.benchmark || 'benchmark')} bought day one</span>
          <span class="dim" id="curve-range"></span>
        </div>
      </div>
    </div>`;

  out += `<div class="panel">
    <h2>Positions <span class="dim">${esc(label)}</span></h2>`;
  if (!positions.length) {
    out += `<div class="empty">no open positions in ${esc(label)}<br>run a scan, then approve a proposal to open one</div></div>`;
  } else {
    out += `<div class="body flush"><table>
      <thead><tr>
        <th>Symbol</th><th>Class</th><th class="r">Weight</th><th class="r">Qty</th>
        <th class="r">Avg cost</th><th class="r">Last</th><th class="r">Value</th><th class="r">Open P&amp;L</th>
      </tr></thead><tbody>` +
      positions.map((x) => {
        const q = S.quotes[x.symbol];
        return `<tr>
          <td class="sym">${esc(x.symbol)}</td>
          <td class="dim">${esc(x.universe_class)}</td>
          <td class="r mono">${fmtPct(x.weight)}</td>
          <td class="r mono">${Number(x.qty).toFixed(4)}</td>
          <td class="r mono">${fmtUSD(x.avg_cost)}</td>
          <td class="r mono">${q ? fmtUSD(q.price) : '<span class="dim">-</span>'}</td>
          <td class="r mono">${fmtUSD(x.market_value)}</td>
          <td class="r mono ${cls(x.unrealized_pl)}">${fmtUSD(x.unrealized_pl)} <span class="dim">${fmtSigned(x.unrealized_plpc)}</span></td>
        </tr>`;
      }).join('') + `</tbody></table></div></div>`;
  }

  const c = S.concentration;
  if (c && c.nominal_n > 1) {
    const thin = c.effective_n < c.nominal_n * 0.6;
    out += `<div class="panel"><h2>Concentration<span class="dim">${thin ? 'clustered' : 'ok'}</span></h2>
      <div class="body">
        <div class="stat-row">
          <div class="s"><div class="k">Positions</div><div class="v">${c.nominal_n}</div></div>
          <div class="s"><div class="k">Independent bets</div>
            <div class="v ${thin ? 'neg' : ''}">${(c.effective_n || 0).toFixed(1)}</div></div>
          <div class="s"><div class="k">Portfolio vol</div>
            <div class="v">${fmtPct(c.portfolio_vol || 0)}</div></div>
          <div class="s"><div class="k">Diversification</div>
            <div class="v">${(c.diversification_ratio || 1).toFixed(2)}x</div></div>
        </div>
        <p class="dim" style="font-size:12.5px;margin:12px 0 0;line-height:1.6">${esc(c.note || '')}</p>
      </div></div>`;
  }

  const pending = (S.run?.proposals || [])
    .filter((x) => x.status === 'pending' && inClass(x.symbol, S.cls));
  if (pending.length) {
    out += `<div class="panel"><h2>Awaiting approval<span class="dim">${pending.length}</span></h2>
      <div class="body">${pending.map(propCard).join('')}</div></div>`;
  }

  if (S.run) {
    const scores = Object.entries(S.run.scores || {})
      .filter(([s]) => inClass(s, S.cls))
      .sort((a, b) => b[1].total - a[1].total).slice(0, 12);
    out += `<div class="panel"><h2>Ranked opportunities<span class="dim">run ${esc(S.run.run_id)}</span></h2>
      <div class="body flush"><table>
      <thead><tr><th>Symbol</th><th class="r">Score</th><th class="r">Valuation</th>
      <th class="r">Quality</th><th class="r">Catalyst</th><th class="r">Momentum</th>
      <th class="r">Vol</th><th class="r">Measured</th></tr></thead><tbody>` +
      scores.map(([sym, v]) => {
        const cov = v.coverage ?? 1;
        const thin = cov < 0.6;
        const cell = (k) => (v.unscored || []).includes(k)
          ? '<span class="dim">-</span>'
          : (v.components[k] ?? 0).toFixed(0);
        return `<tr>
        <td class="sym">${esc(sym)}</td>
        <td class="r mono" style="color:${v.eligible ? 'var(--gold)' : 'var(--muted)'}">${v.total.toFixed(1)}</td>
        <td class="r mono">${cell('valuation')}</td>
        <td class="r mono">${cell('quality')}</td>
        <td class="r mono">${cell('catalyst')}</td>
        <td class="r mono dim">${cell('momentum')}</td>
        <td class="r mono dim">${cell('volatility')}</td>
        <td class="r mono ${thin ? 'dim' : ''}" title="share of the rubric backed by real data">${(cov * 100).toFixed(0)}%</td>
      </tr>`; }).join('') + `</tbody></table>
      <div style="padding:12px 18px;font-size:11.5px;line-height:1.6" class="dim">
        A dash means no data source for that component, so it was excluded from the
        weighting rather than filled with a neutral. Measured is how much of the
        rubric was actually backed by data.
      </div></div></div>`;
  } else {
    out += `<div class="empty">no scan has run yet. press Run scan.</div>`;
  }
  return out;
}

/* --- proposals --- */

function propCard(x) {
  const canAct = x.status === 'pending';
  return `<div class="prop ${esc(x.status)}">
    <div class="head">
      <span class="sym">${esc(x.symbol)}</span>
      <span class="tag ${esc(x.action.toLowerCase())}">${esc(x.action)}</span>
      <span class="tag">${esc(x.asset_class)}</span>
      <span class="tag">score ${x.score.toFixed(1)}</span>
      <span class="tag">${esc(x.confidence || 'unrated')} confidence</span>
      <span class="move">${fmtPct(x.current_weight)} &rarr; ${fmtPct(x.target_weight)}
        <span class="${cls(x.delta_usd)}">${fmtUSD(x.delta_usd)}</span></span>
    </div>
    <div class="lines">
      ${x.thesis ? `<p><b>Thesis</b>${esc(x.thesis)}</p>` : ''}
      ${x.counter ? `<p><b>Against</b>${esc(x.counter)}</p>` : ''}
      ${x.exit_rule ? `<p><b>Exit rule</b>${esc(x.exit_rule)}</p>` : ''}
      ${x.invalidation ? `<p><b>Invalidated if</b>${esc(x.invalidation)}</p>` : ''}
    </div>
    ${x.event ? `<div class="headline">
      <a href="${esc(x.event.url)}" target="_blank" rel="noopener">${esc(x.event.headline)}</a>
      <span class="meta">${esc(x.event.direction)} &middot; ${esc(x.event.thesis_impact)}
        &middot; materiality ${(x.event.materiality * 100).toFixed(0)}%
        &middot; ${esc(x.event.classifier)}</span>
    </div>` : ''}
    ${x.kelly && x.kelly.full_kelly ? `<div class="note dim">Kelly: ${esc(x.kelly.reason)}</div>` : ''}
    ${x.decision_note ? `<div class="note">${esc(x.decision_note)}</div>` : ''}
    ${canAct ? `<div class="actions">
      <button class="btn gold" data-approve="${esc(x.id)}">Approve</button>
      <button class="btn danger" data-reject="${esc(x.id)}">Reject</button>
      <span class="dim mono" style="font-size:11px">sends a ${x.delta_usd > 0 ? 'buy' : 'sell'} to the paper account</span>
    </div>` : `<div class="actions"><span class="dim mono" style="font-size:11px">${esc(x.status)}${x.order ? ` &middot; ${esc(x.order.status)} ${Number(x.order.filled_qty).toFixed(4)} @ ${fmtUSD(x.order.filled_avg_price || 0)}` : ''}</span></div>`}
  </div>`;
}

function viewProposals() {
  if (!S.run) return `<div class="empty">no scan has run yet</div>`;
  const all = S.run.proposals.filter((x) => x.action !== 'KEEP' && inClass(x.symbol, S.cls));
  if (!all.length) return `<div class="empty">no proposed changes in this class for run ${esc(S.run.run_id)}<br>every holding is inside the rebalance band</div>`;
  const order = { pending: 0, blocked: 1, executed: 2, rejected: 3, failed: 4 };
  all.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
  return `<div class="panel"><h2>Proposals<span class="dim">run ${esc(S.run.run_id)}</span></h2>
    <div class="body">${all.map(propCard).join('')}</div></div>`;
}

function viewEvents() {
  const props = (S.run?.proposals || []).filter((p) => p.event && inClass(p.symbol, S.cls));
  const ap = S.autopilot;

  let out = '';
  if (ap) {
    const armed = ap.armed && ap.enabled_in_config;
    out += `<div class="banner ${armed ? '' : 'warn'}">
      ${armed
        ? `Autopilot is ARMED. Material news can place paper orders without asking:
           quarter Kelly sizing, max ${(ap.max_notional_per_trade_pct * 100).toFixed(0)}% of
           equity per order, ${ap.trades_today} of ${ap.max_trades_per_day} automatic trades used today.
           Needs materiality above ${(ap.min_materiality * 100).toFixed(0)}% and confidence above
           ${(ap.min_confidence * 100).toFixed(0)}%.`
        : `Autopilot is OFF. News is still read and assessed every cycle, and anything material
           is queued here for you to approve. Nothing executes on its own.`}
    </div>`;
  }

  if (!props.length) {
    out += `<div class="empty">no news events assessed yet in this class<br>
      press Scan news, or wait for the next scheduled check</div>`;
    return out;
  }
  const order = { pending: 0, blocked: 1, executed: 2, rejected: 3, failed: 4 };
  props.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
  out += `<div class="panel"><h2>News events<span class="dim">${esc(S.run.run_id)}</span></h2>
    <div class="body">${props.map(propCard).join('')}</div></div>`;
  return out;
}

function wireActions() {
  document.querySelectorAll('[data-approve]').forEach((b) =>
    b.addEventListener('click', () => decide(b.dataset.approve, 'approve', b)));
  document.querySelectorAll('[data-reject]').forEach((b) =>
    b.addEventListener('click', () => decide(b.dataset.reject, 'reject', b)));
}

async function decide(id, action, btn) {
  if (S.busy.has(id)) return;
  S.busy.add(id);
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = action === 'approve' ? 'Sending' : 'Rejecting';
  try {
    const r = await api(`/api/proposals/${encodeURIComponent(id)}/${action}`, {
      method: 'POST', body: JSON.stringify({ note: '' }),
    });
    if (action === 'approve') {
      toast(r.skipped ? r.reason : `Order sent: ${r.order.side} ${r.order.symbol}`, 'good');
    } else {
      toast(`Rejected ${id.split(':')[1]}`);
    }
    await Promise.all([loadRun(), refreshPortfolio()]);
    render();
  } catch (err) {
    toast(err.message, 'bad');
    btn.disabled = false;
    btn.textContent = original;
  } finally {
    S.busy.delete(id);
  }
}

/* --- memo --- */

function viewMemo() {
  if (!S.run) return `<div class="empty">no scan has run yet</div>`;
  const box = `<div class="panel memo"><div class="body" id="memo-body">loading memo</div></div>`;
  fetch(`/api/report/${S.run.run_id}`)
    .then((r) => r.json())
    .then((d) => { const n = el('memo-body'); if (n) n.innerHTML = md(d.markdown || ''); })
    .catch(() => { const n = el('memo-body'); if (n) n.textContent = 'no memo file for this run'; });
  return box;
}

/* Small markdown renderer. Handles what report.py emits: headings, tables,
   bullets, bold, inline code, paragraphs. Nothing else, on purpose. */
function md(src) {
  const lines = src.replace(/\r/g, '').split('\n');
  const out = [];
  let i = 0;
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  while (i < lines.length) {
    const line = lines[i];

    if (/^\|/.test(line) && /^\|[\s:|-]+\|$/.test(lines[i + 1] || '')) {
      const cells = (r) => r.split('|').slice(1, -1).map((c) => c.trim());
      const head = cells(line);
      const align = cells(lines[i + 1]).map((c) => (c.endsWith(':') ? ' class="r"' : ''));
      i += 2;
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) { rows.push(cells(lines[i])); i++; }
      out.push('<table><thead><tr>' +
        head.map((h, k) => `<th${align[k] || ''}>${inline(h)}</th>`).join('') +
        '</tr></thead><tbody>' +
        rows.map((r) => '<tr>' + r.map((c, k) => `<td${align[k] || ''}>${inline(c)}</td>`).join('') + '</tr>').join('') +
        '</tbody></table>');
      continue;
    }

    if (/^-\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^-\s+/.test(lines[i])) { items.push(lines[i].replace(/^-\s+/, '')); i++; }
      out.push('<ul>' + items.map((t) => `<li>${inline(t)}</li>`).join('') + '</ul>');
      continue;
    }

    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }

    if (!line.trim()) { i++; continue; }

    const para = [];
    while (i < lines.length && lines[i].trim() && !/^[#|-]/.test(lines[i])) { para.push(lines[i]); i++; }
    out.push(`<p>${inline(para.join(' '))}</p>`);
  }
  return out.join('');
}

/* --- orders --- */

function viewOrders() {
  api('/api/executions').then((d) => {
    const n = el('exec-body');
    if (!n) return;
    const rows = (d.executions || []).filter((x) => !S.cls || S.cls === 'all' || inClass(x.symbol || '', S.cls));
    if (!rows.length) { n.innerHTML = `<div class="empty">no orders yet</div>`; return; }
    n.innerHTML = `<table><thead><tr>
      <th>When</th><th>Event</th><th>Symbol</th><th>Side</th>
      <th class="r">Notional</th><th class="r">Filled</th><th>Status</th></tr></thead><tbody>` +
      rows.map((x) => `<tr>
        <td class="mono dim" style="font-size:11.5px">${esc((x.at || '').replace('T', ' ').slice(0, 16))}</td>
        <td><span class="tag ${x.event === 'executed' ? 'open' : x.event === 'failed' ? 'exit' : ''}">${esc(x.event)}</span></td>
        <td class="sym">${esc(x.symbol || '')}</td>
        <td class="mono">${esc(x.side || '')}</td>
        <td class="r mono">${x.notional ? fmtUSD(x.notional) : '<span class="dim">-</span>'}</td>
        <td class="r mono">${x.order ? Number(x.order.filled_qty).toFixed(4) : '<span class="dim">-</span>'}</td>
        <td class="dim mono" style="font-size:11.5px">${esc(x.order?.status || x.error || x.note || '')}</td>
      </tr>`).join('') + '</tbody></table>';
  });
  return `<div class="panel"><h2>Order log<span class="dim">approved and sent from this dashboard</span></h2>
    <div class="body flush" id="exec-body"><div class="empty">loading</div></div></div>`;
}

/* --- equity curve --- */

function drawCurve() {
  const svg = el('curve');
  const note = el('curve-note');
  const range = el('curve-range');
  const panel = document.querySelector('.curve-panel');
  if (!svg) return;

  const pts = S.curve?.points || [];
  const bench = S.curve?.benchmark || [];

  if (pts.length < 2) {
    // One point is not a line. Collapse to a single readable row rather than
    // leaving a tall empty box at the top of the page.
    if (panel) panel.classList.add('collapsed');
    svg.innerHTML = '';
    const only = pts[0];
    if (note) {
      note.textContent = only
        ? `${fmtUSD(only.equity)} on ${only.t}, one day of history so far`
        : 'no history yet';
    }
    if (range) range.textContent = '';
    return;
  }
  if (panel) panel.classList.remove('collapsed');

  const W = svg.clientWidth || 800;
  const H = svg.clientHeight || 132;
  const padX = 4, padTop = 10, padBot = 16;

  const benchBy = Object.fromEntries(bench.map((b) => [b.t, b.equity]));
  const series = pts.map((p) => p.equity);
  const benchSeries = pts.map((p) => (p.t in benchBy ? benchBy[p.t] : null));
  const all = series.concat(benchSeries.filter((v) => v != null));
  let lo = Math.min(...all), hi = Math.max(...all);
  if (hi === lo) { hi = lo * 1.001 + 1; lo = lo * 0.999 - 1; }
  const padded = (hi - lo) * 0.12;
  lo -= padded; hi += padded;

  const x = (k) => padX + (k / (pts.length - 1)) * (W - padX * 2);
  const y = (v) => H - padBot - ((v - lo) / (hi - lo)) * (H - padTop - padBot);
  const path = (arr) => arr
    .map((v, k) => (v == null ? null : `${k === 0 ? 'M' : 'L'}${x(k).toFixed(1)},${y(v).toFixed(1)}`))
    .filter(Boolean).join(' ');

  const first = series[0];
  const area = `${path(series)} L${x(pts.length - 1).toFixed(1)},${(H - padBot).toFixed(1)} L${x(0).toFixed(1)},${(H - padBot).toFixed(1)} Z`;

  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = `
    <defs>
      <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="var(--gold)" stop-opacity=".16"/>
        <stop offset="100%" stop-color="var(--gold)" stop-opacity="0"/>
      </linearGradient>
    </defs>
    ${first >= lo && first <= hi
      ? `<line x1="${padX}" y1="${y(first).toFixed(1)}" x2="${W - padX}" y2="${y(first).toFixed(1)}"
               stroke="var(--line)" stroke-dasharray="2 4"/>` : ''}
    <path d="${area}" fill="url(#fill)"/>
    ${benchSeries.some((v) => v != null)
      ? `<path d="${path(benchSeries)}" fill="none" stroke="var(--muted)" stroke-width="1.4"/>` : ''}
    <path d="${path(series)}" fill="none" stroke="var(--gold)" stroke-width="1.9"
          stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${x(pts.length - 1).toFixed(1)}" cy="${y(series[series.length - 1]).toFixed(1)}"
            r="2.6" fill="var(--gold)"/>`;

  const last = series[series.length - 1];
  const portRet = first ? last / first - 1 : 0;
  const lastBench = benchSeries.filter((v) => v != null).pop();
  const benchRet = lastBench && first ? lastBench / first - 1 : null;

  if (note) {
    note.innerHTML = `<span class="${cls(portRet)}">${fmtSigned(portRet, 2)}</span>` +
      (benchRet == null ? '' :
        ` vs ${esc(S.curve.benchmark_symbol || 'benchmark')} <span class="${cls(benchRet)}">${fmtSigned(benchRet, 2)}</span>`);
  }
  if (range) {
    const span = `${pts[0].t} to ${pts[pts.length - 1].t}`;
    range.textContent = S.curve.source === 'allocation_backtest'
      ? `${span} - current weights held, not realised returns`
      : S.curve.source === 'runs' ? `${span}, one point per saved run` : span;
  }

  // drawCurve runs on every poll and on resize, so clear anything a previous
  // pass appended or the stats row stacks up.
  panel?.querySelectorAll('.banner, .body > .stat-row')
    .forEach((n) => n.remove());
  if (S.curve.source === 'allocation_backtest' && panel) {
    // Never let a look back be read as a track record.
    const b = document.createElement('div');
    b.className = 'banner warn';
    b.style.margin = '12px 0 0';
    const st = S.curve.stats || {};
    b.textContent = S.curve.note
      || `Your account has no price history yet, so this holds today's weights across `
         + `the trailing window instead. Not returns anyone earned.`;
    panel.querySelector('.body')?.appendChild(b);
    if (st.max_drawdown != null) {
      const s2 = document.createElement('div');
      s2.className = 'stat-row';
      s2.style.marginTop = '14px';
      s2.innerHTML = `
        <div class="s"><div class="k">Look back</div>
          <div class="v ${cls(st.total_return)}">${fmtSigned(st.total_return, 1)}</div></div>
        <div class="s"><div class="k">${esc(S.curve.benchmark_symbol || 'Benchmark')}</div>
          <div class="v ${cls(st.benchmark_return)}">${fmtSigned(st.benchmark_return, 1)}</div></div>
        <div class="s"><div class="k">Excess</div>
          <div class="v ${cls(st.excess)}">${fmtSigned(st.excess, 1)}</div></div>
        <div class="s"><div class="k">Max drawdown</div>
          <div class="v neg">${fmtPct(st.max_drawdown, 1)}</div></div>
        <div class="s"><div class="k">Sharpe</div>
          <div class="v">${(st.sharpe || 0).toFixed(2)}</div></div>`;
      panel.querySelector('.body')?.appendChild(s2);
    }
  }
}

/* ==================================================================
   Smart money, committee, manual entry, and the orb.
   ================================================================== */

const SRC_LABEL = { congress: 'CONGRESS', insider: 'INSIDER', fund: 'FUND' };

const compactUSD = (n) => {
  const v = Math.abs(Number(n) || 0);
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `$${(v / 1e3).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
};

async function loadSmartMoney() {
  try { S.smart = await api('/api/smart-money'); }
  catch { S.smart = null; }
  renderFlow();
}

/* The left rail tape. Newest disclosure first, colour by direction. */
function renderFlow() {
  const list = el('tape-list');
  const dot = el('tape-dot');
  if (!list) return;

  const feed = S.smart?.feed || [];
  if (dot) dot.classList.toggle('stale', feed.length === 0);
  if (!feed.length) {
    list.innerHTML = '<div class="tape-empty">no disclosures in the window</div>';
    return;
  }
  list.innerHTML = feed.map((t) => `
    <div class="tape-row ${t.direction === 'BUY' ? 'buy' : 'sell'}">
      <div class="tape-top">
        <span class="tape-sym">${esc(t.symbol)}</span>
        <span class="tape-val ${t.direction === 'BUY' ? 'pos' : 'neg'}">
          ${t.direction === 'BUY' ? '+' : '-'}${compactUSD(t.value_usd)}</span>
      </div>
      <div class="tape-who">
        <span class="tape-src ${esc(t.source)}">${SRC_LABEL[t.source] || esc(t.source)}</span>
        ${esc(t.actor)}
      </div>
      <div class="tape-who">filed ${esc(t.filed_on)} &middot; ${t.lag_days}d after the trade</div>
    </div>`).join('');
}

/* --- smart money view --- */

function viewSmartMoney() {
  if (!S.smart) return '<div class="empty">smart money sources unavailable</div>';
  const all = Object.values(S.smart.signals || {});
  const sigs = all.filter((x) => x.score !== null)
    .sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
  const unscored = all.filter((x) => x.score === null);

  const rows = sigs.map((x) => `
    <div class="row">
      <div class="sym">${esc(x.symbol)}</div>
      <div>
        <div class="note">${esc(x.note)}</div>
        <div class="bar" style="margin-top:6px"><i style="width:${x.score}%"></i></div>
      </div>
      <div style="text-align:right">
        <div class="headline ${x.net_usd >= 0 ? 'pos' : 'neg'}">${x.score.toFixed(0)}</div>
        <div class="meta">net ${x.net_usd >= 0 ? '+' : '-'}${compactUSD(x.net_usd)}</div>
      </div>
    </div>`).join('');

  return `
    <div class="panel">
      <h2>Disclosed positioning<span class="dim">${sigs.length} scored</span></h2>
      <div class="body flush">${rows || '<div class="empty">nothing disclosed</div>'}</div>
    </div>
    <div class="panel">
      <h2>What this is</h2>
      <div class="body note">
        Congressional periodic transaction reports from the House Clerk, insider
        Form 4 filings, and quarterly 13F holdings, all read from the primary
        filings rather than a third party scrape. Congress discloses a range
        rather than an amount, so those dollar figures are range midpoints.
        Everything here is lagged: a Form 4 by two days, a congressional report
        by up to 45, a 13F by up to 45 days after a quarter that had already
        ended. It carries 6% of the rubric.
        ${unscored.length ? `<br><br><span class="muted">${unscored.length} symbols
        had no disclosures and are left unscored rather than counted neutral:
        ${esc(unscored.map((u) => u.symbol).join(', '))}.</span>` : ''}
      </div>
    </div>`;
}

/* --- committee --- */

function viewCommittee() {
  const symbols = (S.universe?.classes || []).flatMap((c) => c.symbols)
    .filter((s) => !s.includes('/'));
  const v = S.committee;

  const picker = `
    <div class="panel">
      <h2>Convene the committee<span class="dim">four models, independent, then cross examined</span></h2>
      <div class="body">
        <div class="form-grid">
          <div class="field">
            <label>Symbol</label>
            <select id="cm-symbol">
              ${symbols.map((s) => `<option ${s === S.committeeSymbol ? 'selected' : ''}>${esc(s)}</option>`).join('')}
            </select>
          </div>
          <div class="field">
            <label>&nbsp;</label>
            <button class="btn primary" id="cm-run">Convene</button>
            <div class="hint">several minutes: four models answer, then audit each other</div>
          </div>
        </div>
      </div>
    </div>`;

  if (!v) return picker + '<div class="empty">no verdict yet</div>';

  const pct = (v.score / 10) * 100;
  const circ = 2 * Math.PI * 46;
  const stanceColour = { BUY: 'var(--pos)', AVOID: 'var(--neg)' }[v.consensus] || 'var(--violet)';

  const seats = (v.seats || []).map((s) => `
    <div class="seat ${s.answered ? '' : 'abstain'}" style="--seat:${esc(s.colour || '#a06bff')}">
      <div class="seat-top">
        <div>
          <div class="seat-name">${esc(s.name)}</div>
          <div class="seat-model">${esc(s.model)}</div>
        </div>
        <span class="stance ${esc(s.stance)}">${esc(s.stance)}</span>
      </div>
      ${s.answered ? `
        <div class="meta">conviction ${s.conviction}/10 &middot; ${s.latency_s}s</div>
        <ul>${(s.bull || []).slice(0, 3).map((b) => `<li>${esc(b)}</li>`).join('')}</ul>
        <ul>${(s.bear || []).slice(0, 3).map((b) => `<li class="bear">${esc(b)}</li>`).join('')}</ul>
        ${s.key_risk ? `<div class="meta" style="margin-top:8px">Key risk: ${esc(s.key_risk)}</div>` : ''}`
      : `<div class="meta">did not answer: ${esc(s.error || 'unknown')}</div>`}
    </div>`).join('');

  const claims = (v.claims || []).map((c) => `
    <div class="claim ${c.disputed ? 'disputed' : ''}">
      <div>${esc(c.text)} <span class="muted">&mdash; ${esc(c.by)}</span></div>
      <div class="ticks">
        ${Object.entries(c.verdicts || {}).map(([who, verdict]) => `
          <span class="tick ${esc(verdict)}" title="${esc(who)}: ${esc(verdict)}">
            ${verdict === 'supported' ? '&check;' : verdict === 'contradicted' ? '&times;' : '?'}
          </span>`).join('')}
      </div>
    </div>`).join('');

  return picker + `
    <div class="panel">
      <h2>${esc(v.symbol)} verdict<span class="dim">${v.answered} of ${v.total} seats answered</span></h2>
      <div class="body">
        <div class="verdict">
          <div class="gauge">
            <svg width="108" height="108">
              <circle cx="54" cy="54" r="46" fill="none" stroke="var(--line)" stroke-width="7"/>
              <circle cx="54" cy="54" r="46" fill="none" stroke="${stanceColour}" stroke-width="7"
                      stroke-linecap="round" stroke-dasharray="${circ}"
                      stroke-dashoffset="${circ - (circ * pct) / 100}"/>
            </svg>
            <div class="gauge-mid">${v.score}<small>/10</small></div>
          </div>
          <div class="verdict-main">
            <div class="verdict-call" style="color:${stanceColour}">${esc(v.consensus)}</div>
            <div class="note">${esc(v.note)}</div>
            <div class="disagree">
              <div class="legend"><span>disagreement</span><span>${v.disagreement}/100</span></div>
              <div class="bar"><i style="width:${v.disagreement}%"></i></div>
              <div class="meta" style="margin-top:5px">
                0 is unanimous, 100 is an even split. Allocator multiplier ${v.multiplier}&times;.
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="panel">
      <h2>The seats</h2>
      <div class="body"><div class="seat-grid">${seats}</div></div>
    </div>

    ${claims ? `
    <div class="panel">
      <h2>Cross examination<span class="dim">each seat audits the others against the same evidence</span></h2>
      <div class="body">${claims}</div>
    </div>` : ''}`;
}

function wireCommittee() {
  const btn = el('cm-run');
  if (!btn) return;
  btn.onclick = async () => {
    const symbol = el('cm-symbol').value;
    S.committeeSymbol = symbol;
    btn.disabled = true;
    btn.textContent = 'sitting';
    setOrb('thinking');
    try {
      S.committee = await api('/api/committee', {
        method: 'POST', body: JSON.stringify({ symbol }),
      });
      render();
    } catch (err) {
      toast(err.message, 'bad');
    } finally {
      setOrb('idle');
      const b = el('cm-run');
      if (b) { b.disabled = false; b.textContent = 'Convene'; }
    }
  };
}

/* --- manual entry --- */

function viewManual() {
  const symbols = (S.universe?.classes || []).flatMap((c) => c.symbols);
  const r = S.manualResult;

  return `
    <div class="panel">
      <h2>Manual trade<span class="dim">same risk gate, same approval queue</span></h2>
      <div class="body">
        <div class="form-grid">
          <div class="field">
            <label>Symbol</label>
            <select id="mo-symbol">${symbols.map((s) => `<option>${esc(s)}</option>`).join('')}</select>
          </div>
          <div class="field">
            <label>Side</label>
            <select id="mo-side"><option>BUY</option><option>SELL</option></select>
          </div>
          <div class="field">
            <label>Size as</label>
            <select id="mo-mode">
              <option value="notional">Dollars</option>
              <option value="weight">Target weight %</option>
            </select>
          </div>
          <div class="field">
            <label>Amount</label>
            <input id="mo-amount" type="number" step="any" placeholder="1000">
          </div>
          <div class="field form-wide">
            <label>Exit rule</label>
            <input id="mo-exit" placeholder="what would make you sell this">
          </div>
          <div class="field form-wide">
            <label>Invalidation</label>
            <input id="mo-inval" placeholder="what would prove the idea wrong">
          </div>
          <div class="field form-wide">
            <label>Reason</label>
            <textarea id="mo-reason" placeholder="why, in your own words"></textarea>
          </div>
          <div class="field form-wide">
            <label style="display:flex;gap:8px;align-items:center;text-transform:none;letter-spacing:0">
              <input type="checkbox" id="mo-override" style="width:auto">
              Override the minimum holding period
            </label>
            <div class="hint">
              Only you can set this, and only when a thesis is genuinely dead
              rather than merely having had a bad month. It is recorded on the order.
            </div>
          </div>
          <div class="field form-wide">
            <button class="btn primary" id="mo-submit">Send to the risk gate</button>
          </div>
        </div>
        ${r ? renderManualResult(r) : ''}
      </div>
    </div>

    <div class="panel">
      <h2>Why this is not a shortcut</h2>
      <div class="body note">
        A manual order runs the same checks a generated proposal does and lands
        in the same queue, still needing approval. It cannot exceed the position
        limit, breach the cash floor, or buy something outside
        <span class="mono">config/universe.yaml</span>. The exit rule and the
        invalidation are required because the rubric cannot supply them for you,
        and without them a future run has nothing to hold the decision to.
      </div>
    </div>`;
}

function renderManualResult(r) {
  if (r.accepted) {
    return `<div class="callout good" style="margin-top:14px">
      Queued for approval: ${esc(r.proposal.action)} ${esc(r.proposal.symbol)}
      to ${(r.proposal.target_weight * 100).toFixed(2)}% of equity.
      Open Proposals to send it.
      ${(r.warnings || []).length ? `<ul>${r.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
    </div>`;
  }
  return `<div class="callout bad" style="margin-top:14px">
    Rejected.<ul>${(r.reasons || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
  </div>`;
}

function wireManual() {
  const btn = el('mo-submit');
  if (!btn) return;
  btn.onclick = async () => {
    const mode = el('mo-mode').value;
    const amount = parseFloat(el('mo-amount').value);
    if (!isFinite(amount) || amount <= 0) return toast('enter an amount', 'bad');

    const body = {
      symbol: el('mo-symbol').value,
      side: el('mo-side').value,
      reason: el('mo-reason').value,
      exit_rule: el('mo-exit').value,
      invalidation: el('mo-inval').value,
      override_holding_period: el('mo-override').checked,
    };
    if (mode === 'weight') body.target_weight = amount / 100;
    else body.notional = amount;

    btn.disabled = true;
    try {
      S.manualResult = await api('/api/manual-order', {
        method: 'POST', body: JSON.stringify(body),
      });
      await loadRun();
      render();
    } catch (err) {
      toast(err.message, 'bad');
    } finally {
      const b = el('mo-submit');
      if (b) b.disabled = false;
    }
  };
}

/* ==================================================================
   Sector heatmap, the drill down, the tracker leaderboard, and the
   edge against the benchmark.
   ================================================================== */

const HEAT_WINDOWS = [
  { key: 'm1', label: '1M' }, { key: 'm3', label: '3M' },
  { key: 'm6', label: '6M' }, { key: 'ytd', label: 'YTD' },
  { key: 'y1', label: '1Y' },
];

async function loadSectors() {
  try { S.sectors = await api('/api/sectors'); }
  catch { S.sectors = null; }
}

async function loadTrackers() {
  try { S.trackers = await api('/api/trackers'); }
  catch { S.trackers = null; }
}

async function loadObjective() {
  try { S.objective = await api('/api/objective'); }
  catch { S.objective = null; }
  renderObjective();
}

/* Colour a tile by return. Fixed stops rather than a scale relative to the
   best and worst on screen: a relative scale repaints the whole map when one
   sector moves, so nothing on it means the same thing two days running. */
function heatColour(v) {
  if (v === null || v === undefined) return 'var(--panel-2)';
  const x = Math.max(-12, Math.min(12, v)) / 12;
  const a = 0.10 + Math.abs(x) * 0.42;
  return x >= 0 ? `rgba(79, 201, 163, ${a.toFixed(3)})`
                : `rgba(226, 105, 92, ${a.toFixed(3)})`;
}

/* --- the edge readout --- */

function renderObjective() {
  const host = el('edge-strip');
  if (!host) return;
  const o = S.objective;
  if (!o || !o.available) {
    host.innerHTML = '<span class="dim">edge not measurable yet</span>';
    return;
  }
  const good = o.beating_benchmark;
  const shown = o.annualised ? o.excess_annual_pct : o.excess_pct;
  host.innerHTML = `
    <span class="edge-label">vs ${esc(o.benchmark)}</span>
    <span class="edge-value ${good ? 'pos' : 'neg'}">${shown >= 0 ? '+' : ''}${shown.toFixed(1)}${o.annualised ? '%/yr' : '%'}</span>
    <span class="edge-target">target +${o.target_pct.toFixed(0)}</span>
    <span class="edge-verdict ${good ? '' : 'neg'}">${esc(o.verdict)}</span>`;
  host.title = o.headline + ' ' + o.detail;
}

/* --- sectors --- */

function viewSectors() {
  const d = S.sectors;
  if (!d) return '<div class="empty">sector data unavailable</div>';
  const w = S.heatWindow || 'm3';
  const bench = d.benchmark || {};

  const tiles = d.sectors.map((s) => {
    const v = s[w];
    const flow = s.net_flow;
    return `
      <button class="tile" data-sector="${esc(s.etf)}" style="background:${heatColour(v)}">
        <div class="tile-top">
          <span class="tile-etf">${esc(s.etf)}</span>
          <span class="tile-ret ${cls(v)}">${v === null ? '--' : (v >= 0 ? '+' : '') + v.toFixed(1) + '%'}</span>
        </div>
        <div class="tile-name">${esc(s.name)}</div>
        <div class="tile-foot">
          <span class="dim">vs200 ${s.vs200 === null ? '--' : (s.vs200 >= 0 ? '+' : '') + s.vs200.toFixed(0) + '%'}</span>
          ${s.flow_names ? `<span class="${flow >= 0 ? 'pos' : 'neg'}">${flow >= 0 ? '+' : '-'}${compactUSD(flow)}</span>`
                         : '<span class="dim">no filings</span>'}
        </div>
      </button>`;
  }).join('');

  return `
    <div class="panel">
      <h2>US sector map<span class="dim">${esc(d.generated.slice(0, 10))} &middot; ${esc(bench.symbol || 'SPY')} ${bench[w] === null || bench[w] === undefined ? '' : (bench[w] >= 0 ? '+' : '') + bench[w].toFixed(1) + '%'}</span></h2>
      <div class="body">
        <div class="win-row">
          ${HEAT_WINDOWS.map((x) => `<button class="win-btn ${x.key === w ? 'active' : ''}" data-win="${x.key}">${x.label}</button>`).join('')}
        </div>
        <div class="heatgrid">${tiles}</div>
        <div class="meta" style="margin-top:12px">
          Colour is the ${esc(HEAT_WINDOWS.find((x) => x.key === w).label)} return, on a
          fixed scale so a tile means the same thing every day. The figure at the
          bottom right is net disclosed dollars from filings, which is a different
          question from price and often disagrees with it.
          ${esc(d.flow_coverage.note)}
        </div>
      </div>
    </div>
    ${S.sectorDetail ? sectorDetail(S.sectorDetail) : `
      <div class="panel"><div class="body meta">Pick a sector to see which
      holdings drove it, which tracked managers own it, and who has been buying
      inside it.</div></div>`}`;
}

function sectorDetail(d) {
  const drivers = (d.drivers || []).map((x) => {
    // Share of the move is only meaningful when there was a move. When a
    // sector nets out near flat, one contributor can be 300% of it and the
    // number reads as nonsense, so below a point of movement it is dropped
    // and the contribution in points stands on its own.
    const moved = Math.abs(d.sector_return || 0) >= 1.0;
    const share = moved && x.contribution !== null
      ? (x.contribution / d.sector_return) * 100 : null;
    return `
      <div class="row">
        <div class="sym">${esc(x.symbol)}</div>
        <div>
          <div>${esc(x.name)}</div>
          <div class="meta">${(x.weight * 100).toFixed(1)}% of the fund
            ${x.ret === null ? '' : `&middot; ${x.ret >= 0 ? '+' : ''}${x.ret.toFixed(1)}% over the window`}</div>
        </div>
        <div style="text-align:right">
          <div class="${cls(x.contribution)}">${x.contribution === null ? '--' : (x.contribution >= 0 ? '+' : '') + x.contribution.toFixed(2) + ' pts'}</div>
          ${share === null ? '' : `<div class="meta">${share.toFixed(0)}% of the move</div>`}
        </div>
      </div>`;
  }).join('');

  const investors = (d.investors || []).map((i) => `
    <div class="row">
      <div class="sym" style="font-size:12px">${esc(i.name)}</div>
      <div>
        <div class="meta">${(i.book_weight * 100).toFixed(1)}% of their disclosed book &middot; ${esc(i.positions.join(', '))}</div>
      </div>
      <div style="text-align:right">
        <div class="${cls(i.excess)}">${i.excess === null ? '--' : (i.excess >= 0 ? '+' : '') + i.excess.toFixed(2)}</div>
        <div class="meta">mean excess</div>
      </div>
    </div>`).join('');

  const actors = (d.flow?.actors || []).map((a) => `
    <div class="row">
      <div class="sym" style="font-size:11.5px">${esc(a.actor)}</div>
      <div><span class="tape-src ${esc(a.source)}">${esc((a.source || '').toUpperCase())}</span>
        <span class="meta">${esc(a.symbols.join(', '))}</span></div>
      <div class="${a.net_usd >= 0 ? 'pos' : 'neg'}" style="text-align:right">
        ${a.net_usd >= 0 ? '+' : '-'}${compactUSD(a.net_usd)}</div>
    </div>`).join('');

  return `
    <div class="panel">
      <h2>${esc(d.name)} &middot; what moved it<span class="dim">${esc(d.etf)} ${d.sector_return === null ? '' : (d.sector_return >= 0 ? '+' : '') + d.sector_return.toFixed(1) + '%'}</span></h2>
      <div class="body flush">${drivers || '<div class="empty">no holdings published</div>'}</div>
      <div class="body meta" style="border-top:1px solid var(--line-soft)">${esc(d.coverage.note)}
        Contribution is weight times return, so it is the share of the move each
        name is responsible for rather than how much the name itself rose.</div>
    </div>

    <div class="panel">
      <h2>Who owns it<span class="dim">tracked managers, with their measured record</span></h2>
      <div class="body flush">${investors || '<div class="empty">no tracked manager holds this sector</div>'}</div>
    </div>

    ${actors ? `
    <div class="panel">
      <h2>Who has been trading it<span class="dim">net disclosed dollars per actor</span></h2>
      <div class="body flush">${actors}</div>
    </div>` : ''}`;
}

function wireSectors() {
  document.querySelectorAll('.win-btn').forEach((b) => {
    b.onclick = () => { S.heatWindow = b.dataset.win; S.sectorDetail = null; render(); };
  });
  document.querySelectorAll('.tile').forEach((b) => {
    b.onclick = async () => {
      b.classList.add('loading');
      try {
        S.sectorDetail = await api(`/api/sectors/${b.dataset.sector}?window=${S.heatWindow || 'm3'}`);
        render();
      } catch (err) { toast(err.message, 'bad'); }
      finally { b.classList.remove('loading'); }
    };
  });
}

/* --- trackers --- */

function viewTrackers() {
  const d = S.trackers;
  if (!d) return '<div class="empty">leaderboard unavailable, it may still be building</div>';

  const rows = d.trackers.map((t) => {
    if (t.excess === null) {
      return `<div class="row">
        <div class="sym" style="font-size:12px">${esc(t.name)}</div>
        <div class="meta">${esc(t.error || 'not measured')}</div>
        <div class="dim" style="text-align:right">--</div>
      </div>`;
    }
    const win = t.windows.map((w) => `<i class="${w.excess >= 0 ? 'pos' : 'neg'}" title="${esc(w.label)}: ${w.excess >= 0 ? '+' : ''}${w.excess}"></i>`).join('');
    return `
      <div class="row">
        <div class="sym" style="font-size:12px">${esc(t.name)}</div>
        <div>
          <div class="meta">${esc(t.kind)} &middot; ${t.windows.length} windows &middot;
            beat ${(t.beat_rate * 100).toFixed(0)}% &middot; filed ${t.stale_days}d ago</div>
          <div class="winbar">${win}</div>
        </div>
        <div style="text-align:right">
          <div class="headline ${cls(t.excess)}">${t.excess >= 0 ? '+' : ''}${t.excess.toFixed(2)}</div>
          <div class="meta">mean excess</div>
        </div>
      </div>`;
  }).join('');

  const consensus = (d.consensus || []).map((c) => `
    <div class="row">
      <div class="sym">${esc(c.symbol)}</div>
      <div class="meta">${esc(c.backers.join(', '))}</div>
      <div style="text-align:right">${c.n_backers} of them</div>
    </div>`).join('');

  const s = d.summary;
  return `
    <div class="panel">
      <h2>Measured against ${esc(d.benchmark)}<span class="dim">${esc(d.generated.slice(0, 10))}</span></h2>
      <div class="body">
        <div class="callout ${s.beating_benchmark > s.measured / 2 ? 'good' : 'bad'}">
          ${esc(s.note)} Median excess is ${s.median_excess === null ? 'unknown' : s.median_excess.toFixed(2)} points.
        </div>
        <div class="meta" style="margin-top:10px">
          Every tracker measured stays on this board, including the negative ones.
          A leaderboard that shows only the winners is a survivorship machine.
          Returns are the disclosed long book, entered on the date it became
          public, so this is what a follower could have captured rather than what
          the manager made.
        </div>
      </div>
    </div>

    <div class="panel">
      <h2>Leaderboard<span class="dim">mean excess over ${esc(d.benchmark)}, per window</span></h2>
      <div class="body flush">${rows}</div>
    </div>

    ${consensus ? `
    <div class="panel">
      <h2>What the ones that beat it are holding<span class="dim">books under 150 days old only</span></h2>
      <div class="body flush">${consensus}</div>
    </div>` : ''}`;
}

/* ==================================================================
   The basket. The beginner facing view: what to hold, why, and what
   to expect, with the option to copy somebody else's book instead.
   ================================================================== */

async function loadBasket(follow) {
  const q = follow ? `?follow=${encodeURIComponent(follow)}` : '';
  try { S.basket = await api(`/api/basket${q}`); }
  catch (err) { S.basket = null; S.basketError = err.message; }
}

function viewBasket() {
  const b = S.basket;
  if (!b) return `<div class="empty">${esc(S.basketError || 'building the basket')}</div>`;

  const follows = (b.followable || []).map((f) => `
    <button class="follow-btn ${b.following === f.name ? 'active' : ''}" data-follow="${esc(f.name)}">
      <span class="follow-name">${esc(f.name)}</span>
      <span class="follow-edge ${cls(f.excess)}">${f.excess >= 0 ? '+' : ''}${f.excess.toFixed(2)}</span>
    </button>`).join('');

  const sleeves = b.sleeves.map((s) => `
    <div class="panel">
      <h2>${esc(s.label)}<span class="dim">${(s.actual * 100).toFixed(0)}% of the portfolio</span></h2>
      <div class="body">
        <div class="note">${esc(s.plain)}</div>
      </div>
      ${s.picks.length ? `<div class="body flush">${s.picks.map((p) => `
        <div class="row">
          <div class="sym">${esc(p.symbol)}</div>
          <div><div class="note">${esc(p.why)}</div></div>
          <div style="text-align:right">
            <div class="headline">${(p.weight * 100).toFixed(1)}%</div>
            ${p.score === null ? '' : `<div class="meta">score ${p.score}</div>`}
          </div>
        </div>`).join('')}</div>`
      : `<div class="body meta">${esc(s.note)}</div>`}
    </div>`).join('');

  return `
    <div class="panel">
      <h2>${esc(b.headline)}<span class="dim">${esc(b.generated.slice(0, 10))}</span></h2>
      <div class="body">
        <div class="alloc-bar">
          ${b.sleeves.filter((s) => s.actual > 0).map((s, i) => `
            <span class="alloc-seg s${i}" style="width:${(s.actual * 100).toFixed(1)}%"
                  title="${esc(s.label)} ${(s.actual * 100).toFixed(0)}%"></span>`).join('')}
          <span class="alloc-seg cash" style="width:${(b.cash * 100).toFixed(1)}%" title="Cash"></span>
        </div>
        <div class="alloc-key">
          ${b.sleeves.filter((s) => s.actual > 0).map((s, i) => `
            <span><i class="s${i}"></i>${esc(s.label)} ${(s.actual * 100).toFixed(0)}%</span>`).join('')}
          <span><i class="cash"></i>Cash ${(b.cash * 100).toFixed(0)}%</span>
        </div>

        ${b.warnings.length ? `<div class="callout bad" style="margin-top:14px">
          <ul>${b.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></div>` : ''}

        <div class="actions" style="margin-top:16px">
          <button class="btn gold" id="bk-queue">Send this basket to the approval queue</button>
          ${b.following ? '<button class="btn" id="bk-clear">Back to the balanced basket</button>' : ''}
        </div>
        <div class="meta" style="margin-top:8px">
          Nothing is bought by that button. Each line still needs an approval
          under Proposals, and the risk gate can refuse any of them.
        </div>
        <div id="bk-result"></div>
      </div>
    </div>

    <div class="panel">
      <h2>What to expect<span class="dim">read this before the holdings</span></h2>
      <div class="body">
        <div class="lines">
          ${b.expectations.map((e) => `<p class="note">${esc(e)}</p>`).join('')}
        </div>
      </div>
    </div>

    ${sleeves}

    <div class="panel">
      <h2>Or copy somebody else<span class="dim">the number is how they did against the index</span></h2>
      <div class="body">
        <div class="follow-grid">${follows}</div>
        <div class="meta" style="margin-top:12px">
          These are measured, not advertised: each figure is that portfolio's
          disclosed holdings entered on the day they became public, against the
          S&amp;P over the same dates. A negative number means following them
          would have done worse than doing nothing. Their picks still have to
          clear the same screen, so copying somebody is a place to look rather
          than a decision.
        </div>
      </div>
    </div>`;
}

function wireBasket() {
  document.querySelectorAll('.follow-btn').forEach((b) => {
    b.onclick = async () => {
      setOrb('thinking');
      await loadBasket(b.dataset.follow);
      setOrb('idle');
      render();
    };
  });
  const clear = el('bk-clear');
  if (clear) clear.onclick = async () => { await loadBasket(null); render(); };

  const queue = el('bk-queue');
  if (queue) queue.onclick = async () => {
    queue.disabled = true;
    queue.textContent = 'queueing';
    try {
      const r = await api('/api/basket/queue', {
        method: 'POST',
        body: JSON.stringify({ follow: S.basket?.following || null }),
      });
      el('bk-result').innerHTML = `
        <div class="callout ${r.rejected.length ? 'bad' : 'good'}" style="margin-top:12px">
          ${esc(r.note)}
          ${r.rejected.length ? `<ul>${r.rejected.map((x) =>
            `<li>${esc(x.symbol)}: ${esc((x.reasons || []).join('; '))}</li>`).join('')}</ul>` : ''}
        </div>`;
      await loadRun();
      renderPendingPill();
    } catch (err) {
      toast(err.message, 'bad');
    } finally {
      queue.disabled = false;
      queue.textContent = 'Send this basket to the approval queue';
    }
  };
}

/* ==================================================================
   Connections and import. Pick a broker, paste the fields, see your
   holdings against the basket.
   ================================================================== */

async function loadConnections() {
  try { S.connections = await api('/api/connections'); }
  catch { S.connections = null; }
}

const VERDICT_LABEL = {
  keep: 'Keep', trim: 'Oversized', add: 'Missing',
  rejected: 'Not picked', uncovered: 'Not covered',
};
const VERDICT_TAG = {
  keep: 'pos', trim: 'warn', add: 'violet',
  rejected: 'neg', uncovered: '',
};

function viewConnect() {
  const d = S.connections;
  if (!d) return '<div class="empty">could not load connections</div>';

  const chosen = S.connectBroker || 'alpaca';
  const spec = d.catalog.find((c) => c.key === chosen) || d.catalog[0];

  const picker = d.catalog.map((c) => `
    <button class="follow-btn ${c.key === chosen ? 'active' : ''}" data-broker="${esc(c.key)}">
      <span class="follow-name">${esc(c.label)}</span>
      <span class="meta">${c.can_trade ? 'trade' : 'read only'}</span>
    </button>`).join('');

  const fields = spec.fields.map((f) => `
    <div class="field form-wide">
      <label>${esc(f.label)}</label>
      <input id="cx-${esc(f.key)}" type="${f.secret ? 'password' : 'text'}"
             placeholder="${esc(f.placeholder || '')}" autocomplete="off">
      ${f.help ? `<div class="hint">${esc(f.help)}</div>` : ''}
    </div>`).join('');

  const modes = spec.modes.map((m) => `
    <button class="win-btn ${m === (S.connectMode || spec.modes[0]) ? 'active' : ''}"
            data-mode="${esc(m)}">${m === 'paper' ? 'Paper' : 'Live money'}</button>`).join('');

  const live = (S.connectMode || spec.modes[0]) === 'live';

  const existing = (d.connections || []).map((c) => `
    <div class="row">
      <div class="sym" style="font-size:12px">${esc(c.label)}</div>
      <div>
        <div class="meta">
          ${esc(c.broker)} &middot;
          <span class="${c.mode === 'live' ? 'neg' : ''}">${esc(c.mode)}</span> &middot;
          ${c.enabled ? 'on' : 'off'}
          ${Object.keys(c.hints || {}).length
            ? ' &middot; ' + Object.entries(c.hints).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(', ')
            : ''}
        </div>
        ${c.last_error ? `<div class="meta neg">${esc(c.last_error)}</div>`
                       : c.last_ok ? `<div class="meta pos">last checked ${esc(c.last_ok.slice(0, 16))}</div>` : ''}
      </div>
      <div class="actions">
        <button class="btn" data-test="${esc(c.id)}">Check</button>
        <button class="btn" data-import="${esc(c.id)}">Import</button>
        <button class="btn danger" data-forget="${esc(c.id)}">Forget</button>
      </div>
    </div>`).join('');

  return `
    <div class="panel">
      <h2>Connect a broker<span class="dim">keys stay on the server and are never shown back</span></h2>
      <div class="body">
        <div class="follow-grid">${picker}</div>

        <div class="note" style="margin-top:14px">${esc(spec.note)}</div>

        <div class="meta" style="margin-top:10px">
          ${spec.can_read ? 'Reads holdings.' : 'Cannot read holdings.'}
          ${spec.can_trade ? 'Can place orders.' : 'Cannot place orders from here.'}
          ${spec.has_paper ? 'Has a paper mode.' : 'Has no paper mode.'}
          ${spec.signup ? ` <a href="${esc(spec.signup)}" target="_blank" rel="noopener">Where to get the keys</a>.` : ''}
        </div>

        ${spec.fields.length ? `
        <div class="form-grid" style="margin-top:16px">
          <div class="field form-wide">
            <label>Mode</label>
            <div class="win-row">${modes}</div>
            ${live ? `<div class="callout bad" style="margin-top:8px">
              Live selects your real account, and it is read only: this reads
              the positions in it. Orders are not routed here. Everything this
              app places still goes to the broker in
              <span class="mono">config/brokers.yaml</span>, which is the paper
              account, and still needs an approval per line.
            </div>` : ''}
          </div>
          <div class="field form-wide">
            <label>Name it</label>
            <input id="cx-label" placeholder="${esc(spec.label)}" autocomplete="off">
          </div>
          ${fields}
          <div class="field form-wide">
            <button class="btn gold" id="cx-save">Connect and check</button>
          </div>
        </div>
        <div id="cx-result"></div>`
        : `<div class="meta" style="margin-top:14px">
             Nothing to connect. Use the paste box below.
           </div>`}
      </div>
    </div>

    ${existing ? `
    <div class="panel">
      <h2>Connected<span class="dim">${d.connections.length}</span></h2>
      <div class="body flush">${existing}</div>
    </div>` : ''}

    <div class="panel">
      <h2>Or paste what you own<span class="dim">one holding per line</span></h2>
      <div class="body">
        <div class="field">
          <textarea id="cx-paste" style="min-height:130px" placeholder="AAPL 10 2200
MSFT 5 2100
GLD 8"></textarea>
          <div class="hint">
            Symbol then quantity, and a value if you have it. Commas, tabs or
            spaces all work, and a broker's CSV usually pastes straight in.
            Anything unreadable is shown back rather than guessed at.
          </div>
        </div>
        <div class="actions" style="margin-top:12px">
          <button class="btn gold" id="cx-compare">Compare with the basket</button>
        </div>
      </div>
    </div>

    ${S.importReport ? importReport(S.importReport) : ''}`;
}

function importReport(r) {
  const rows = r.lines.map((l) => `
    <div class="row">
      <div class="sym">${esc(l.symbol)}</div>
      <div>
        <div><span class="tag ${VERDICT_TAG[l.verdict] || ''}">${esc(VERDICT_LABEL[l.verdict] || l.verdict)}</span></div>
        <div class="note" style="margin-top:5px">${esc(l.why)}</div>
      </div>
      <div style="text-align:right">
        <div class="headline">${l.held_weight ? (l.held_weight * 100).toFixed(1) + '%' : '&mdash;'}</div>
        <div class="meta">${l.target_weight ? 'basket ' + (l.target_weight * 100).toFixed(1) + '%' : 'not in basket'}</div>
      </div>
    </div>`).join('');

  return `
    <div class="panel">
      <h2>Your portfolio against the basket<span class="dim">${esc(r.source || 'pasted')}</span></h2>
      <div class="body">
        <div class="callout">${esc(r.headline)}</div>
        ${r.notes.map((n) => `<div class="meta" style="margin-top:10px">${esc(n)}</div>`).join('')}
        ${r.unparsed && r.unparsed.length ? `
          <div class="callout bad" style="margin-top:12px">
            Could not read these lines, so they were left out rather than guessed at:
            <ul>${r.unparsed.map((u) => `<li class="mono">${esc(u)}</li>`).join('')}</ul>
          </div>` : ''}
      </div>
      <div class="body flush">${rows}</div>
      <div class="body meta">
        Nothing here is an order. A holding outside the universe is listed as
        not covered because this system follows a few dozen instruments and has
        no view on the rest; that is missing information, not a sell signal.
      </div>
    </div>`;
}

function wireConnect() {
  document.querySelectorAll('[data-broker]').forEach((b) => {
    b.onclick = () => {
      S.connectBroker = b.dataset.broker;
      S.connectMode = null;
      render();
    };
  });
  document.querySelectorAll('[data-mode]').forEach((b) => {
    b.onclick = () => { S.connectMode = b.dataset.mode; render(); };
  });

  const save = el('cx-save');
  if (save) save.onclick = async () => {
    const d = S.connections;
    const spec = d.catalog.find((c) => c.key === (S.connectBroker || 'alpaca'));
    const creds = {};
    let missing = false;
    spec.fields.forEach((f) => {
      const v = (el(`cx-${f.key}`)?.value || '').trim();
      if (!v) missing = true;
      creds[f.key] = v;
    });
    if (missing) return toast('fill every field', 'bad');

    save.disabled = true;
    save.textContent = 'checking';
    try {
      const r = await api('/api/connections', {
        method: 'POST',
        body: JSON.stringify({
          broker: spec.key,
          label: el('cx-label')?.value || '',
          mode: S.connectMode || spec.modes[0],
          credentials: creds,
        }),
      });
      el('cx-result').innerHTML = r.test.ok
        ? `<div class="callout good" style="margin-top:12px">Connected. ${esc(r.test.summary || '')}</div>`
        : `<div class="callout bad" style="margin-top:12px">${esc(r.test.error || 'could not connect')}</div>`;
      await loadConnections();
      render();
    } catch (err) {
      toast(err.message, 'bad');
    } finally {
      const b = el('cx-save');
      if (b) { b.disabled = false; b.textContent = 'Connect and check'; }
    }
  };

  document.querySelectorAll('[data-test]').forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        const r = await api(`/api/connections/${b.dataset.test}/test`, { method: 'POST' });
        toast(r.ok ? (r.summary || 'connected') : (r.error || 'failed'), r.ok ? 'good' : 'bad');
        await loadConnections();
        render();
      } finally { b.disabled = false; }
    };
  });

  document.querySelectorAll('[data-forget]').forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        await api(`/api/connections/${b.dataset.forget}`, { method: 'DELETE' });
        await loadConnections();
        render();
      } catch (err) { toast(err.message, 'bad'); b.disabled = false; }
    };
  });

  document.querySelectorAll('[data-import]').forEach((b) => {
    b.onclick = () => runImport({ connection_id: b.dataset.import }, b);
  });

  const compare = el('cx-compare');
  if (compare) compare.onclick = () => {
    const text = el('cx-paste')?.value || '';
    if (!text.trim()) return toast('paste some holdings first', 'bad');
    runImport({ text }, compare);
  };
}

async function runImport(body, button) {
  button.disabled = true;
  const label = button.textContent;
  button.textContent = 'reading';
  setOrb('thinking');
  try {
    S.importReport = await api('/api/import', {
      method: 'POST', body: JSON.stringify(body),
    });
    render();
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    setOrb('idle');
    button.disabled = false;
    button.textContent = label;
  }
}

/* --- the orb --- */

let bootOrb = null;
let railOrb = null;

function setOrb(state) {
  if (railOrb) railOrb.setState(state);
  if (bootOrb) bootOrb.setState(state);
}

function initOrbs() {
  const bc = el('boot-orb');
  if (bc && !bootOrb && window.Orb) { bootOrb = new Orb(bc, { lat: 30, lon: 40 }); bootOrb.start(); }
}

window.addEventListener('resize', () => { if (S.view === 'overview') drawCurve(); });
initOrbs();
start();

/* ═══════════════════════════════════════════════════════════════
   CDR Direct v2 — Customer × State page JS (self-contained)
   ═══════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const authMeta = document.querySelector('meta[name="cdr-auth-mode"]');
  const PROXY_AUTH = !!authMeta && authMeta.content === 'proxy';

  const state = {
    token: PROXY_AUTH ? '' : (localStorage.getItem('cdr_direct_token') || ''),
    entities: new Set(['MyCallConnect', 'SalamTalk', 'Dialphone', 'Vestacall']),
    side: 'orig',
    rawRows: [],
    filteredRows: [],
    sortKey: 'minutes',
    sortDir: 'desc',
  };

  const $ = (id) => document.getElementById(id);

  // ─── Browser localStorage cache (instant repeat render) ─────
  const LOCAL_CACHE_KEY = 'cdr_customer_state_cache_v1';
  const LOCAL_CACHE_MAX = 12;
  const bodyKey = (b) => JSON.stringify({
    d: b.start_date + '~' + b.end_date,
    e: (b.entities || []).slice().sort().join(','),
    s: b.state_side || 'orig',
    c: b.customer || '',
  });
  function lcRead() {
    try { return JSON.parse(localStorage.getItem(LOCAL_CACHE_KEY) || '{}'); }
    catch (e) { return {}; }
  }
  function lcGet(b) { return lcRead()[bodyKey(b)] || null; }
  function lcPut(b, resp) {
    const all = lcRead();
    all[bodyKey(b)] = { savedAt: Date.now(), response: resp };
    const keys = Object.keys(all);
    if (keys.length > LOCAL_CACHE_MAX) {
      keys.sort((x, y) => (all[x].savedAt || 0) - (all[y].savedAt || 0));
      while (keys.length > LOCAL_CACHE_MAX) delete all[keys.shift()];
    }
    try { localStorage.setItem(LOCAL_CACHE_KEY, JSON.stringify(all)); } catch (e) {}
  }

  // ─── Formatters ─────────────────────────────────────────────
  const fmtInt = (n) => (n == null) ? '—'
    : Number(n).toLocaleString('en-US', { maximumFractionDigits: 0 });
  const fmtNum = (n, dec) => (n == null) ? '—'
    : Number(n).toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec });
  const fmtMoney = (n) => {
    if (n == null) return '—';
    const v = Number(n);
    return (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString('en-US', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  };
  const fmtPct = (n) => (n == null) ? '—' : Number(n).toFixed(2) + '%';

  // ─── Date helpers ───────────────────────────────────────────
  const isoDate = (d) => d.toISOString().slice(0, 10);
  const todayUTC = () => isoDate(new Date());
  const daysAgoUTC = (n) => {
    const d = new Date();
    d.setUTCDate(d.getUTCDate() - n);
    return isoDate(d);
  };

  function applyDatePreset(preset) {
    const s = $('start-date'), e = $('end-date');
    if (preset === 'today') { s.value = todayUTC(); e.value = todayUTC(); }
    else if (preset === 'yesterday') { s.value = daysAgoUTC(1); e.value = daysAgoUTC(1); }
    else if (preset === 'last3') { s.value = daysAgoUTC(2); e.value = todayUTC(); }
    else if (preset === 'last7') { s.value = daysAgoUTC(6); e.value = todayUTC(); }
    else if (preset === 'last30') { s.value = daysAgoUTC(29); e.value = todayUTC(); }
  }

  // ─── Status ─────────────────────────────────────────────────
  function setStatus(msg, kind) {
    const el = $('status');
    el.textContent = msg || '';
    el.className = 'status' + (kind ? ' ' + kind : '');
  }

  // ─── Token modal (direct mode only) ─────────────────────────
  function ensureToken() {
    if (PROXY_AUTH) return true;
    if (state.token) return true;
    $('token-modal').classList.remove('hidden');
    return false;
  }
  $('btn-token-save').addEventListener('click', () => {
    const v = $('token-input').value.trim();
    if (!v) return;
    state.token = v;
    localStorage.setItem('cdr_direct_token', v);
    $('token-modal').classList.add('hidden');
    runReport();
  });
  $('btn-logout').addEventListener('click', () => {
    state.token = '';
    localStorage.removeItem('cdr_direct_token');
    if (!PROXY_AUTH) location.reload();
  });

  // ─── API ────────────────────────────────────────────────────
  function apiHeaders() {
    const headers = { 'Content-Type': 'application/json' };
    if (!PROXY_AUTH) headers['X-Auth-Token'] = state.token;
    return headers;
  }

  async function apiCall(path, body, timeoutMs) {
    const ctrl = new AbortController();
    const timer = timeoutMs ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
    let res;
    try {
      res = await fetch(path, {
        method: 'POST',
        headers: apiHeaders(),
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
    } catch (e) {
      if (e.name === 'AbortError') throw new Error('TIMEOUT');
      throw e;
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (res.status === 401) {
      if (!PROXY_AUTH) {
        state.token = '';
        localStorage.removeItem('cdr_direct_token');
        ensureToken();
      }
      throw new Error('401 — token rejected');
    }
    const text = await res.text();
    let payload;
    try { payload = JSON.parse(text); }
    catch (e) { throw new Error('HTTP ' + res.status + ' — non-JSON: ' + text.slice(0, 120)); }
    if (!res.ok) throw new Error(payload.error || ('HTTP ' + res.status));
    return payload;
  }

  // ─── Filters ────────────────────────────────────────────────
  document.querySelectorAll('#date-presets .preset').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#date-presets .preset').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      applyDatePreset(btn.dataset.preset);
    });
  });

  document.querySelectorAll('#side-chips .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('#side-chips .chip').forEach((c) => c.classList.remove('active'));
      chip.classList.add('active');
      state.side = chip.dataset.side;
    });
  });

  document.querySelectorAll('#entity-chips .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const ent = chip.dataset.entity;
      if (state.entities.has(ent)) { state.entities.delete(ent); chip.classList.remove('active'); }
      else { state.entities.add(ent); chip.classList.add('active'); }
    });
  });

  // ─── Run report ─────────────────────────────────────────────
  function paint(json) {
    state.rawRows = json.rows || [];
    renderTotals(json.totals || {});
    applyFilter();
  }

  async function runReport(force) {
    if (!ensureToken()) return;
    if (state.entities.size === 0) { setStatus('Select at least one entity', 'error'); return; }

    const body = {
      start_date: $('start-date').value,
      end_date: $('end-date').value,
      entities: Array.from(state.entities),
      state_side: state.side,
      customer: $('customer-search').value.trim() || null,
      sort_by: state.sortKey,
      sort_dir: state.sortDir,
    };
    if (force) body.force_refresh = true;

    // Instant repaint from browser cache, then revalidate in background.
    const cached = !force && lcGet(body);
    if (cached) {
      paint(cached.response);
      const age = Math.round((Date.now() - cached.savedAt) / 1000);
      setStatus('✓ ' + state.rawRows.length.toLocaleString() + ' rows (cached ' + age + 's ago) · refreshing…', 'success');
    } else {
      setStatus('Running… first run for this window computes on the server (can take a minute); repeats are instant.', 'loading');
    }

    $('btn-run').disabled = true;
    try {
      const t0 = performance.now();
      const json = await apiCall('/api/usa-customer-state', body, 95000);
      const ms = Math.round(performance.now() - t0);
      lcPut(body, json);
      paint(json);
      const hit = json._cache && json._cache.hit ? ' · server cache' : '';
      setStatus('✓ ' + state.rawRows.length.toLocaleString() + ' rows in ' + ms + ' ms' + hit, 'success');
    } catch (e) {
      if (/^401/.test(e.message)) { /* modal shown */ }
      else if (e.message === 'TIMEOUT') {
        setStatus('Still computing on the server (large window). It keeps running in the background — click Run again in ~30s and it will be cached & instant.' + (cached ? ' Showing last cached result meanwhile.' : ''), cached ? 'success' : 'error');
      } else if (!cached) {
        setStatus('✗ ' + e.message, 'error');
      }
    } finally {
      $('btn-run').disabled = false;
    }
  }

  function renderTotals(t) {
    $('stat-customers').textContent = fmtInt(t.customer_count);
    $('stat-states').textContent = fmtInt(t.state_count);
    $('stat-attempts').textContent = fmtInt(t.attempts);
    $('stat-minutes').textContent = fmtNum(t.minutes, 1);
    $('stat-revenue').textContent = fmtMoney(t.revenue);
    const m = $('stat-margin');
    m.textContent = fmtMoney(t.margin);
    m.style.color = (t.margin < 0) ? 'var(--danger, #e5484d)' : '';
  }

  // ─── Client-side filter + sort + render ─────────────────────
  function applyFilter() {
    const q = $('filter-input').value.trim().toLowerCase();
    let rows = state.rawRows;
    if (q) {
      rows = rows.filter((r) =>
        String(r.state || '').toLowerCase().includes(q) ||
        String(r.customer || '').toLowerCase().includes(q));
    }
    rows = rows.slice().sort((a, b) => {
      const av = a[state.sortKey], bv = b[state.sortKey];
      let cmp;
      if (typeof av === 'number' || typeof bv === 'number') cmp = (av || 0) - (bv || 0);
      else cmp = String(av || '').localeCompare(String(bv || ''));
      return state.sortDir === 'asc' ? cmp : -cmp;
    });
    state.filteredRows = rows;
    renderRows(rows);
  }

  function renderRows(rows) {
    const body = $('results-body');
    $('row-count').textContent = rows.length.toLocaleString();
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="9" class="muted center">No rows match</td></tr>';
      return;
    }
    const html = rows.map((r) => {
      const marginCls = (r.margin < 0) ? ' style="color:var(--danger,#e5484d)"' : '';
      return '<tr>' +
        '<td>' + esc(r.state) + '</td>' +
        '<td>' + esc(r.customer) + '</td>' +
        '<td class="num">' + fmtInt(r.attempts) + '</td>' +
        '<td class="num">' + fmtInt(r.completions) + '</td>' +
        '<td class="num">' + fmtPct(r.asr_pct) + '</td>' +
        '<td class="num">' + fmtNum(r.minutes, 1) + '</td>' +
        '<td class="num">' + fmtMoney(r.revenue) + '</td>' +
        '<td class="num">' + fmtMoney(r.cost) + '</td>' +
        '<td class="num"' + marginCls + '>' + fmtMoney(r.margin) + '</td>' +
        '</tr>';
    }).join('');
    body.innerHTML = html;
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ─── Sorting via header click ───────────────────────────────
  document.querySelectorAll('#head-row th[data-sort]').forEach((th) => {
    th.style.cursor = 'pointer';
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (state.sortKey === key) state.sortDir = (state.sortDir === 'asc') ? 'desc' : 'asc';
      else { state.sortKey = key; state.sortDir = 'desc'; }
      applyFilter();
    });
  });

  $('filter-input').addEventListener('input', applyFilter);

  // ─── CSV export (client-side, of loaded rows) ───────────────
  $('btn-csv').addEventListener('click', () => {
    const rows = state.filteredRows;
    if (!rows.length) { setStatus('Nothing to export', 'error'); return; }
    const cols = ['state', 'customer', 'attempts', 'completions', 'asr_pct', 'minutes', 'revenue', 'cost', 'margin'];
    const head = cols.join(',');
    const lines = rows.map((r) => cols.map((c) => {
      const v = r[c];
      const s = (v == null) ? '' : String(v);
      return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }).join(','));
    const csv = [head].concat(lines).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const side = state.side === 'orig' ? 'origination' : 'destination';
    a.href = url;
    a.download = 'customer-state-' + side + '-' + $('start-date').value + '_' + $('end-date').value + '.csv';
    a.click();
    URL.revokeObjectURL(url);
    setStatus('Downloaded ' + rows.length.toLocaleString() + ' rows as CSV', 'success');
  });

  $('btn-run').addEventListener('click', () => runReport());
  $('window-label').textContent = 'yesterday (UTC)';

  // ─── Boot ───────────────────────────────────────────────────
  applyDatePreset('yesterday');
  ensureToken();
})();

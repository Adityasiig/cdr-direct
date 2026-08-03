/* ═══════════════════════════════════════════════════════════════
   CDR Direct v2 — dashboard JS
   ═══════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // Coolify proxy mode injects the private backend token server-side.
  // Direct/raw-server mode retains the browser token prompt.
  const authMeta = document.querySelector('meta[name="cdr-auth-mode"]');
  const PROXY_AUTH = !!authMeta && authMeta.content === 'proxy';

  // ─── State ──────────────────────────────────────────────────
  const state = {
    token: PROXY_AUTH ? '' : (localStorage.getItem('cdr_direct_token') || ''),
    selectedEntities: new Set(['MyCallConnect', 'SalamTalk', 'Dialphone', 'Vestacall']),
    selectedSips: new Set(['all']),
    selectedReasons: new Set(),
    view: 'customer-code',
    rawRows: [],
    filteredRows: [],
    customerNames: [],
    sortKey: 'revenue',
    sortDir: 'desc',
    autoRun: localStorage.getItem('cdr_auto_run') === '1',
    debounceTimer: null,
    quickFilter: null,    // 'profitable' | 'fas-suspect' | null  (post-query row filters)
    lastQueryAt: 0,
    initializing: true,   // suppresses chip-handler side-effects during boot
    lastSuccessStatus: null,  // remember last good status to restore after transient errors
    lastCacheMeta: null,  // last response's _cache block — drives the pill
    cachePillTimer: null, // ticks the "X min ago" label every 30 sec
    localCacheUsed: false, // true if page rendered from localStorage instead of fetch
  };

  // ─── Local browser cache (localStorage) ─────────────────────
  // Page-load instant render: paint from this BEFORE any network fetch.
  // Keeps last N successful queries keyed by canonical body.
  const LOCAL_CACHE_KEY = 'cdr_direct_local_cache_v2';
  const LOCAL_CACHE_MAX_ENTRIES = 12;

  function bodyToCacheKey(body) {
    return JSON.stringify({
      e: body.start_date + '~' + (body.end_date || body.start_date),
      n: (body.entities || []).slice().sort().join(','),
      s: (body.sip_codes || []).slice().sort().join(','),
      r: (body.reasons || []).slice().sort().join(','),
      c: body.customer || '',
      h: (body.start_hour ?? '') + '~' + (body.end_hour ?? ''),
      o: (body.sort_by || 'revenue') + '~' + (body.sort_dir || 'desc'),
      q: body.quick_filter || '',
      v: body._endpoint || '',
    });
  }
  function localCacheRead() {
    try { return JSON.parse(localStorage.getItem(LOCAL_CACHE_KEY) || '{}'); }
    catch (e) { return {}; }
  }
  function localCacheWrite(obj) {
    try { localStorage.setItem(LOCAL_CACHE_KEY, JSON.stringify(obj)); }
    catch (e) { /* quota exceeded — drop silently */ }
  }
  function localCacheGet(body) {
    const all = localCacheRead();
    const key = bodyToCacheKey(body);
    return all[key] || null;
  }
  function localCachePut(body, response) {
    const all = localCacheRead();
    const key = bodyToCacheKey(body);
    all[key] = { savedAt: Date.now(), response: response, body: body };
    // Evict oldest if over limit
    const keys = Object.keys(all);
    if (keys.length > LOCAL_CACHE_MAX_ENTRIES) {
      keys.sort((a, b) => (all[a].savedAt || 0) - (all[b].savedAt || 0));
      while (keys.length > LOCAL_CACHE_MAX_ENTRIES) {
        delete all[keys.shift()];
      }
    }
    localCacheWrite(all);
  }

  // ─── DOM cache ──────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const $$ = (sel) => document.querySelectorAll(sel);

  const el = {
    startDate: $('start-date'),
    endDate: $('end-date'),
    startHour: $('start-hour'),
    endHour: $('end-hour'),
    hourPresets: $$('#hour-presets .preset'),
    windowLabel: $('window-label'),
    viewChips: $$('#view-chips .chip'),
    entityChips: $$('#entity-chips .chip'),
    sipChips: $$('#sip-chips .chip'),
    reasonChips: $$('#reason-chips .chip'),
    reasonField: $('reason-field'),
    presets: $$('#date-presets .preset'),
    customerSearch: $('customer-search'),
    customerList: $('customer-list'),
    btnRun: $('btn-run'),
    btnAuto: $('btn-auto'),
    btnCsv: $('btn-csv'),
    btnLogout: $('btn-logout'),
    btnForceRefresh: $('btn-force-refresh'),
    cachePill: $('cache-pill'),
    status: $('status'),
    rowCount: $('row-count'),
    filterInput: $('filter-input'),
    tableCard: $('table-card'),
    tableScroll: $('table-scroll'),
    tableBody: $('results-body'),
    tableHead: $$('#results-table th'),
    tokenModal: $('token-modal'),
    tokenInput: $('token-input'),
    btnTokenSave: $('btn-token-save'),
    quickActions: $$('#quick-actions .qa-btn'),
    stat: {
      attempts:     $('stat-attempts'),
      completions:  $('stat-completions'),
      asr:          $('stat-asr'),
      minutes:      $('stat-minutes'),
      revenue:      $('stat-revenue'),
      cost:         $('stat-cost'),
      margin:       $('stat-margin'),
      marginSub:    $('stat-margin-sub'),
      codes:        $('stat-codes'),
      codesSub:     $('stat-codes-sub'),
    },
  };

  // ─── Formatters ─────────────────────────────────────────────
  const fmtInt = (n) =>
    (n === null || n === undefined) ? '—'
      : Number(n).toLocaleString('en-US', { maximumFractionDigits: 0 });

  const fmtNum = (n, dec) =>
    (n === null || n === undefined) ? '—'
      : Number(n).toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec });

  const fmtMoney = (n) => {
    if (n === null || n === undefined) return '—';
    const v = Number(n);
    return (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString('en-US', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  };

  const fmtPct = (n) =>
    (n === null || n === undefined) ? '—' : Number(n).toFixed(2) + '%';

  // ─── Date helpers ───────────────────────────────────────────
  const isoDate = (d) => d.toISOString().slice(0, 10);
  const todayUTC = () => isoDate(new Date());
  const daysAgoUTC = (n) => {
    const d = new Date();
    d.setUTCDate(d.getUTCDate() - n);
    return isoDate(d);
  };

  function applyPreset(preset) {
    el.presets.forEach((p) => p.classList.toggle('active', p.dataset.preset === preset));
    switch (preset) {
      case 'today':     el.startDate.value = todayUTC();      el.endDate.value = todayUTC();     break;
      case 'yesterday': el.startDate.value = daysAgoUTC(1);   el.endDate.value = daysAgoUTC(1);  break;
      case 'last3':     el.startDate.value = daysAgoUTC(2);   el.endDate.value = todayUTC();     break;
      case 'last7':     el.startDate.value = daysAgoUTC(6);   el.endDate.value = todayUTC();     break;
      case 'last30':    el.startDate.value = daysAgoUTC(29);  el.endDate.value = todayUTC();     break;
    }
    updateWindowLabel();
    if (state.initializing) return;
    scheduleAutoRun();
  }
  el.presets.forEach((p) => p.addEventListener('click', () => applyPreset(p.dataset.preset)));

  function clearPreset() {
    el.presets.forEach((p) => p.classList.remove('active'));
  }

  function updateWindowLabel() {
    const s = el.startDate.value, e = el.endDate.value;
    if (!s) { el.windowLabel.textContent = '—'; return; }
    const date = (s === e) ? s : (s + ' → ' + e);
    const sh = el.startHour.value, eh = el.endHour.value;
    const hr = (sh !== '' && eh !== '') ? ('  ·  ' + sh.padStart(2, '0') + '-' + eh.padStart(2, '0') + 'h UTC') : '';
    el.windowLabel.textContent = date + hr;
  }

  // ─── Hour selectors ─────────────────────────────────────────
  // Populate 00..23 options on both selectors
  (function initHourSelectors() {
    for (let h = 0; h < 24; h++) {
      const label = String(h).padStart(2, '0') + ':00';
      const o1 = document.createElement('option');
      o1.value = h; o1.textContent = label;
      el.startHour.appendChild(o1);
      const o2 = document.createElement('option');
      o2.value = h; o2.textContent = label;
      el.endHour.appendChild(o2);
    }
  })();

  function setHourPreset(preset) {
    el.hourPresets.forEach((p) => p.classList.toggle('active', p.dataset.hourPreset === preset));
    switch (preset) {
      case 'all':       el.startHour.value = ''; el.endHour.value = ''; break;
      case 'peak-us':   el.startHour.value = 14; el.endHour.value = 22; break;
      case 'business':  el.startHour.value = 9;  el.endHour.value = 17; break;
      case 'overnight': el.startHour.value = 22; el.endHour.value = 6;  break;  // wraps
    }
    updateWindowLabel();
    if (state.initializing) return;
    scheduleAutoRun();
  }
  el.hourPresets.forEach((p) => p.addEventListener('click', () => setHourPreset(p.dataset.hourPreset)));

  function clearHourPreset() {
    el.hourPresets.forEach((p) => p.classList.remove('active'));
  }
  [el.startHour, el.endHour].forEach((s) => {
    s.addEventListener('change', () => {
      clearHourPreset();
      updateWindowLabel();
      scheduleAutoRun();
    });
  });

  [el.startDate, el.endDate].forEach((inp) => {
    inp.addEventListener('change', () => {
      clearPreset();
      updateWindowLabel();
      scheduleAutoRun();
    });
  });

  // ─── Status ─────────────────────────────────────────────────
  function setStatus(msg, kind) {
    el.status.className = 'status' + (kind ? ' ' + kind : '');
    el.status.textContent = msg || '';
    if (kind === 'success' && msg) state.lastSuccessStatus = msg;
  }

  // ─── Cache pill ─────────────────────────────────────────────
  function fmtAge(secs) {
    secs = Math.max(0, Math.round(secs));
    if (secs < 60) return secs + 's';
    if (secs < 3600) return Math.floor(secs / 60) + 'm';
    if (secs < 86400) return Math.floor(secs / 3600) + 'h';
    return Math.floor(secs / 86400) + 'd';
  }
  function renderCachePill() {
    const meta = state.lastCacheMeta;
    if (!meta) {
      el.cachePill.textContent = 'no cache yet';
      el.cachePill.className = 'pill cache-pill';
      return;
    }
    const ageSec = (Date.now() / 1000) - meta.refreshed_at;
    const fresh = ageSec < meta.ttl;
    const label = meta.hit
      ? (fresh ? '✓ cached ' + fmtAge(ageSec) + ' ago'
               : '⚠ stale ' + fmtAge(ageSec) + ' ago — bg refresh queued')
      : '⚡ live · ' + fmtAge(meta.compute_ms / 1000) + ' compute';
    el.cachePill.textContent = label;
    el.cachePill.className = 'pill cache-pill ' +
      (!meta.hit ? 'miss' : (fresh ? 'fresh' : 'stale'));
  }
  function startCachePillTicker() {
    if (state.cachePillTimer) clearInterval(state.cachePillTimer);
    state.cachePillTimer = setInterval(renderCachePill, 30000);  // tick every 30s
  }
  el.cachePill.addEventListener('click', () => {
    runQuery({ force: true });
  });
  el.btnForceRefresh.addEventListener('click', () => {
    runQuery({ force: true });
  });

  // Auto-clear a transient error after delay, restoring last success status
  function transientError(msg, delayMs) {
    setStatus(msg, 'error');
    setTimeout(() => {
      // Only restore if the error is still showing (user hasn't moved on)
      if (el.status.classList.contains('error') && el.status.textContent === msg) {
        if (state.lastSuccessStatus) {
          setStatus(state.lastSuccessStatus, 'success');
        } else {
          setStatus('', '');
        }
      }
    }, delayMs || 5000);
  }

  // ─── Auth modal ─────────────────────────────────────────────
  function ensureToken() {
    if (PROXY_AUTH) {
      el.tokenModal.classList.add('hidden');
      return true;
    }
    if (!state.token) {
      el.tokenModal.classList.remove('hidden');
      el.tokenInput.focus();
      return false;
    }
    el.tokenModal.classList.add('hidden');
    return true;
  }
  el.btnTokenSave.addEventListener('click', () => {
    const v = el.tokenInput.value.trim();
    if (!v) return;
    state.token = v;
    localStorage.setItem('cdr_direct_token', v);
    el.tokenModal.classList.add('hidden');
    setStatus('Token saved. Running first query…', 'success');
    runQuery();
  });
  el.tokenInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') el.btnTokenSave.click();
  });
  el.btnLogout.addEventListener('click', () => {
    if (PROXY_AUTH) return;
    localStorage.removeItem('cdr_direct_token');
    state.token = '';
    state.rawRows = [];
    renderTable();
    renderStats({});
    setStatus('Logged out.', '');
    ensureToken();
  });

  // ─── View toggle ────────────────────────────────────────────
  function setView(v) {
    state.view = 'customer-code';   // only one view now
    el.tableCard.classList.add('view-customer-code');
    renderThead();
    renderTable();
    // Function declarations are hoisted — safe to call here even though defined below
    rebuildCustomerDropdownFromRows();
    if (state.initializing) return;
    scheduleAutoRun();
  }
  el.viewChips.forEach((c) => c.addEventListener('click', () => setView(c.dataset.view)));

  // ─── Entity / SIP chips ─────────────────────────────────────
  el.entityChips.forEach((c) => {
    c.addEventListener('click', () => {
      const k = c.dataset.entity;
      if (state.selectedEntities.has(k)) {
        state.selectedEntities.delete(k);
        c.classList.remove('active');
      } else {
        state.selectedEntities.add(k);
        c.classList.add('active');
      }
      // Origin-trunk dropdown rebuilds from the complete aggregate metadata.
      scheduleAutoRun();
    });
  });

  el.sipChips.forEach((c) => {
    c.addEventListener('click', () => {
      const k = c.dataset.sip;
      if (k === 'all') {
        state.selectedSips.clear();
        el.sipChips.forEach((cc) => cc.classList.remove('active'));
        c.classList.add('active');
        state.selectedSips.add('all');
      } else {
        if (state.selectedSips.has('all')) {
          state.selectedSips.delete('all');
          document.querySelector('#sip-chips .chip[data-sip="all"]').classList.remove('active');
        }
        if (state.selectedSips.has(k)) {
          state.selectedSips.delete(k);
          c.classList.remove('active');
        } else {
          state.selectedSips.add(k);
          c.classList.add('active');
        }
        if (state.selectedSips.size === 0) selectAllSipCodes();
      }
      syncReasonVisibility();
      scheduleAutoRun();
    });
  });

  function selectAllSipCodes() {
    state.selectedSips.clear();
    state.selectedSips.add('all');
    el.sipChips.forEach((chip) => {
      chip.classList.toggle('active', chip.dataset.sip === 'all');
    });
    syncReasonVisibility();
  }

  // The 503 reason sub-filter only makes sense when 503 (or All) is in scope.
  // Hide it otherwise and clear any selection so it can't silently filter.
  function syncReasonVisibility() {
    const show = state.selectedSips.has('503') || state.selectedSips.has('all');
    el.reasonField.style.display = show ? '' : 'none';
    if (!show && state.selectedReasons.size) {
      state.selectedReasons.clear();
      el.reasonChips.forEach((cc) => cc.classList.remove('active'));
    }
  }

  el.reasonChips.forEach((c) => {
    c.addEventListener('click', () => {
      const k = c.dataset.reason;
      if (state.selectedReasons.has(k)) {
        state.selectedReasons.delete(k);
        c.classList.remove('active');
      } else {
        state.selectedReasons.add(k);
        c.classList.add('active');
      }
      scheduleAutoRun();
    });
  });

  // Set initial visibility to match default SIP selection.
  syncReasonVisibility();

  // ─── Origin-trunk typeahead ─────────────────────────────────
  el.customerSearch.addEventListener('change', scheduleAutoRun);
  el.customerSearch.addEventListener('input', (e) => {
    // Datalist handles the dropdown filter natively
    if (e.target.value === '') scheduleAutoRun();
  });

  // ─── Auto-refresh toggle ────────────────────────────────────
  function updateAutoButton() {
    el.btnAuto.textContent = state.autoRun ? '⚡ Auto: On' : '⚡ Auto: Off';
    el.btnAuto.style.color = state.autoRun ? 'var(--accent)' : '';
  }
  el.btnAuto.addEventListener('click', () => {
    state.autoRun = !state.autoRun;
    localStorage.setItem('cdr_auto_run', state.autoRun ? '1' : '0');
    updateAutoButton();
    if (state.autoRun) {
      setStatus('Auto-refresh enabled. Filter changes will trigger query after 1.5s pause.', 'success');
    } else {
      setStatus('Auto-refresh disabled. Click Run query manually.', '');
    }
  });
  updateAutoButton();

  function scheduleAutoRun() {
    if (!state.autoRun) return;
    if (!PROXY_AUTH && !state.token) return;
    if (!el.startDate.value) return;
    if (state.selectedEntities.size === 0) return;
    clearTimeout(state.debounceTimer);
    state.debounceTimer = setTimeout(() => { runQuery(); }, 1500);
  }

  // ─── Quick actions ──────────────────────────────────────────
  el.quickActions.forEach((b) => {
    b.addEventListener('click', () => {
      const action = b.dataset.qa;
      switch (action) {
        case 'worst-margin':
          state.sortKey = 'margin'; state.sortDir = 'asc';
          state.quickFilter = null;
          el.filterInput.value = '';
          runQuery();
          break;
        case 'top-revenue':
          state.sortKey = 'revenue'; state.sortDir = 'desc';
          state.quickFilter = null;
          el.filterInput.value = '';
          runQuery();
          break;
        case 'top-traffic':
          state.sortKey = 'attempts'; state.sortDir = 'desc';
          state.quickFilter = null;
          el.filterInput.value = '';
          runQuery();
          break;
        case 'fas-suspect':
          // ASR needs successes and failures in the denominator. Keeping a
          // SIP 200 outcome filter would mathematically force ASR to 100%.
          selectAllSipCodes();
          state.quickFilter = 'fas-suspect';
          state.sortKey = 'attempts'; state.sortDir = 'desc';
          el.filterInput.value = '';
          runQuery();
          break;
        case 'profitable':
          state.quickFilter = 'profitable';
          state.sortKey = 'margin'; state.sortDir = 'desc';
          el.filterInput.value = '';
          runQuery();
          break;
        case 'clear':
          state.quickFilter = null;
          state.sortKey = 'revenue'; state.sortDir = 'desc';
          el.filterInput.value = '';
          runQuery();
          break;
      }
    });
  });

  // ─── API ────────────────────────────────────────────────────
  // Resilient fetch — auto-retries once on network errors (SSH tunnel hiccup).
  function apiHeaders() {
    const headers = { 'Content-Type': 'application/json' };
    if (!PROXY_AUTH) headers['X-Auth-Token'] = state.token;
    return headers;
  }

  async function apiCall(path, body, opts) {
    opts = opts || {};
    const maxAttempts = opts.noRetry ? 1 : 2;
    let lastErr = null;
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      const t0 = performance.now();
      try {
        const res = await fetch(path, {
          method: 'POST',
          headers: apiHeaders(),
          body: JSON.stringify(body),
        });
        const ms = Math.round(performance.now() - t0);
        if (res.status === 401) {
          if (!PROXY_AUTH) {
            state.token = '';
            localStorage.removeItem('cdr_direct_token');
            ensureToken();
          }
          throw new Error('401 — token rejected');
        }
        // Try to parse JSON, fall back to text if backend returned HTML (gateway error)
        let payload;
        const text = await res.text();
        try { payload = JSON.parse(text); }
        catch (e) {
          throw new Error('HTTP ' + res.status + ' — non-JSON response: ' + text.slice(0, 120));
        }
        if (!res.ok) throw new Error(payload.error || ('HTTP ' + res.status));
        return { json: payload, ms };
      } catch (e) {
        lastErr = e;
        const isNetError = (e instanceof TypeError) || /Failed to fetch|NetworkError|Load failed/i.test(e.message);
        const is401 = /^401/.test(e.message);
        // 401 is fatal — modal already showing
        if (is401) throw e;
        // On last attempt, surface the error
        if (attempt === maxAttempts) throw e;
        // Only retry network errors (not 4xx/5xx — those are deterministic)
        if (!isNetError) throw e;
        // Wait briefly before retry (gives tunnel a chance to reconnect)
        await new Promise(r => setTimeout(r, 800));
      }
    }
    throw lastErr || new Error('unknown error');
  }

  async function loadDailySnapshot() {
    const started = performance.now();
    const res = await fetch('/api/daily-snapshot', {
      method: 'GET',
      headers: apiHeaders(),
    });
    if (res.status === 404) return false;
    if (res.status === 401) {
      if (!PROXY_AUTH) {
        state.token = '';
        localStorage.removeItem('cdr_direct_token');
        ensureToken();
      }
      throw new Error('401 — token rejected');
    }
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || ('HTTP ' + res.status));

    const snapshot = payload._snapshot || {};
    if (snapshot.date) {
      el.startDate.value = snapshot.date;
      el.endDate.value = snapshot.date;
      el.datePill.textContent = snapshot.date;
    }
    state.rawRows = payload.rows || [];
    state.customerNames = payload.customers ||
      (payload.totals && payload.totals.customers) || [];
    state.totalRowCount = payload.total_row_count || state.rawRows.length;
    renderStats(payload.totals || {});
    applyFilter();
    rebuildCustomerDropdownFromRows();
    state.lastCacheMeta = payload._cache || null;
    renderCachePill();

    const body = getQueryBody();
    body._endpoint = '/api/usa-customer-codes';
    localCachePut(body, payload);
    const elapsed = Math.round(performance.now() - started);
    setStatus(
      'Ready — full day ' + (snapshot.date || '') +
      ' loaded from the prepared snapshot in ' + elapsed + 'ms · ' +
      state.rawRows.length.toLocaleString() + ' rows shown',
      'success'
    );
    return true;
  }

  function getQueryBody() {
    const sipList = state.selectedSips.has('all')
      ? []
      : Array.from(state.selectedSips).map((s) => parseInt(s, 10));

    // Send whatever user typed — backend exact-matches orig_trunk_group_name.
    const customer = el.customerSearch.value.trim() || null;

    const body = {
      start_date: el.startDate.value,
      end_date: el.endDate.value,
      entities: Array.from(state.selectedEntities),
      sip_codes: sipList,
      customer: customer,
      limit: 5000,
      sort_by: state.sortKey,
      sort_dir: state.sortDir,
      quick_filter: state.quickFilter,
    };
    // Only attach reason filter when 503/All is in scope and chips are picked.
    if (state.selectedReasons.size &&
        (state.selectedSips.has('503') || state.selectedSips.has('all'))) {
      body.reasons = Array.from(state.selectedReasons);
    }
    const sh = el.startHour.value, eh = el.endHour.value;
    if (sh !== '' && eh !== '') {
      body.start_hour = parseInt(sh, 10);
      body.end_hour = parseInt(eh, 10);
    }
    return body;
  }

  // ─── Render stats ───────────────────────────────────────────
  function renderStats(t) {
    el.stat.attempts.textContent     = fmtInt(t.attempts);
    el.stat.completions.textContent  = fmtInt(t.completions);
    el.stat.asr.textContent          = fmtPct(t.asr_pct);
    el.stat.minutes.textContent      = fmtNum(t.minutes, 2);
    el.stat.revenue.textContent      = fmtMoney(t.revenue);
    el.stat.cost.textContent         = fmtMoney(t.cost);
    el.stat.margin.textContent       = fmtMoney(t.margin);
    el.stat.margin.className = 'stat-value ' + (
      (t.margin === undefined || t.margin === null) ? '' : (t.margin >= 0 ? 'positive' : 'negative')
    );
    if (t.margin !== undefined && t.revenue) {
      const pct = (t.margin / t.revenue * 100);
      el.stat.marginSub.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '% of revenue';
    } else {
      el.stat.marginSub.textContent = 'profit / loss';
    }
    if (t.pair_count !== undefined) {
      el.stat.codes.textContent = fmtInt(t.code_count);
      el.stat.codesSub.textContent =
        fmtInt(t.customer_count) + ' trunks · ' + fmtInt(t.pair_count) + ' pairs';
    } else {
      el.stat.codes.textContent = fmtInt(t.code_count);
      el.stat.codesSub.textContent = 'destinations';
    }
  }

  // ─── Filter + sort + render ────────────────────────────────
  function applyFilter() {
    const q = (el.filterInput.value || '').trim().toLowerCase();
    let rows = state.rawRows.slice();

    if (q) {
      rows = rows.filter((r) =>
        String(r.code || '').toLowerCase().includes(q) ||
        String(r.term_code || '').toLowerCase().includes(q) ||
        String(r.state || '').toLowerCase().includes(q) ||
        String(r.ratecenter || '').toLowerCase().includes(q) ||
        String(r.customer || '').toLowerCase().includes(q)
      );
    }

    state.filteredRows = rows;
    applySort();
  }

  function applySort() {
    const k = state.sortKey;
    const dir = state.sortDir === 'asc' ? 1 : -1;
    state.filteredRows.sort((a, b) => {
      let av = a[k], bv = b[k];
      if (av === null || av === undefined) av = (typeof bv === 'number' ? -Infinity : '');
      if (bv === null || bv === undefined) bv = (typeof av === 'number' ? -Infinity : '');
      if (typeof av === 'string') return dir * av.localeCompare(bv);
      return dir * (av - bv);
    });
    renderTable();
  }


  function renderThead() {
    const head = document.getElementById('head-row');
    if (!head) return;
    head.innerHTML =
      '<th data-sort="customer" class="col-customer">Origin trunk</th>' +
      '<th data-sort="code">Origin billed prefix</th>' +
      '<th data-sort="term_code">Termination billed prefix</th>' +
      '<th data-sort="state">State</th>' +
      '<th data-sort="ratecenter">Ratecenter</th>' +
      '<th data-sort="x5u_url">STIR X5U</th>' +
      '<th data-sort="attest">Attest</th>' +
      '<th data-sort="attempts" class="num">Attempts</th>' +
      '<th data-sort="completions" class="num">Completions</th>' +
      '<th data-sort="asr_pct" class="num">ASR %</th>' +
      '<th data-sort="minutes" class="num">Minutes</th>' +
      '<th data-sort="revenue" class="num">Revenue</th>' +
      '<th data-sort="cost" class="num">Cost</th>' +
      '<th data-sort="margin" class="num">Margin</th>';
    // Re-cache + rebind sort handlers (nodes just replaced)
    el.tableHead = document.querySelectorAll('#results-table th');
    el.tableHead.forEach((th) => {
      th.addEventListener('click', () => {
        const k = th.dataset.sort;
        if (!k) return;
        if (state.sortKey === k) {
          state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
        } else {
          state.sortKey = k;
          state.sortDir = (k === 'code' || k === 'term_code' || k === 'state' || k === 'ratecenter' || k === 'customer' || k === 'x5u_url' || k === 'attest') ? 'asc' : 'desc';
        }
        runQuery();
      });
    });
  }

  function renderTable() {
    el.tableHead.forEach((th) => {
      th.classList.remove('sort-asc', 'sort-desc');
      if (th.dataset.sort === state.sortKey) th.classList.add('sort-' + state.sortDir);
    });
    el.rowCount.textContent = (state.totalRowCount && state.totalRowCount > state.rawRows.length)
      ? (state.filteredRows.length.toLocaleString() + ' loaded · ' + state.totalRowCount.toLocaleString() + ' matching')
      : state.filteredRows.length.toLocaleString();

    const showCust = state.view === 'customer-code';
    const colspan = 14;

    if (state.filteredRows.length === 0) {
      el.tableBody.innerHTML =
        '<tr><td colspan="' + colspan + '" class="muted center">' +
        (state.rawRows.length === 0
          ? 'No data yet — pick a date and click <strong>Run query</strong>'
          : 'No rows match the current filter') +
        '</td></tr>';
      return;
    }

    const parts = [];
    for (const r of state.filteredRows) {
      const m = Number(r.margin || 0);
      const rowCls = m < 0 ? 'row-loss' : (m > 0 ? 'row-gain' : '');
      const marginCls = m >= 0 ? 'positive' : 'negative';
      const asr = Number(r.asr_pct || 0);
      const asrCls = asr < 15 ? 'warn' : (asr >= 40 ? 'positive' : '');

      const custCell = showCust
        ? '<td class="customer" title="' + escapeAttr(r.customer || '') + '">' + escapeHtml(r.customer || '') + '</td>'
        : '<td class="customer col-customer"></td>';

      // Truncate long X5U URL for display, keep full in title
      const fullUrl = String(r.x5u_url || '');
      const truncated = fullUrl.length > 60
        ? fullUrl.slice(0, 32) + '…' + fullUrl.slice(-20)
        : fullUrl;

      parts.push(
        '<tr class="' + rowCls + '">' +
        custCell +
        '<td class="code">' + (r.code || '') + '</td>' +
        '<td class="code">' + (r.term_code || '') + '</td>' +
        '<td>' + escapeHtml(r.state || '') + '</td>' +
        '<td>' + escapeHtml(r.ratecenter || '') + '</td>' +
        '<td class="x5u" title="' + escapeAttr(fullUrl) + '">' + escapeHtml(truncated) + '</td>' +
        '<td class="code">' + escapeHtml(r.attest || '') + '</td>' +
        '<td class="num">' + fmtInt(r.attempts) + '</td>' +
        '<td class="num">' + fmtInt(r.completions) + '</td>' +
        '<td class="num ' + asrCls + '">' + fmtPct(r.asr_pct) + '</td>' +
        '<td class="num">' + fmtNum(r.minutes, 2) + '</td>' +
        '<td class="num">' + fmtMoney(r.revenue) + '</td>' +
        '<td class="num">' + fmtMoney(r.cost) + '</td>' +
        '<td class="num ' + marginCls + '">' + fmtMoney(r.margin) + '</td>' +
        '</tr>'
      );
    }
    el.tableBody.innerHTML = parts.join('');
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
    );
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // ─── Sort handlers ──────────────────────────────────────────
  el.tableHead.forEach((th) => {
    th.addEventListener('click', () => {
      const k = th.dataset.sort;
      if (!k) return;
      if (state.sortKey === k) {
        state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        state.sortKey = k;
        state.sortDir = (k === 'code' || k === 'term_code' || k === 'state' || k === 'ratecenter' || k === 'customer') ? 'asc' : 'desc';
      }
      runQuery();
    });
  });

  el.filterInput.addEventListener('input', applyFilter);

  // ─── Origin-trunk dropdown — complete list from aggregate totals ───
  function rebuildCustomerDropdownFromRows() {
    // Only works on customer-code view (rows have .customer field)
    el.customerList.innerHTML = '';
    if (state.view !== 'customer-code' || state.view === 'stir-x5u') {
      updateCustomerLabel(0);
      return;
    }
    const seen = new Set(state.customerNames || []);
    if (seen.size === 0) {
      for (const r of state.rawRows) if (r.customer) seen.add(r.customer);
    }
    const customers = Array.from(seen).sort((a, b) => a.localeCompare(b));
    for (const c of customers) {
      const opt = document.createElement('option');
      opt.value = c;
      el.customerList.appendChild(opt);
    }
    updateCustomerLabel(customers.length);
  }

  function entityShort(e) {
    return ({ MyCallConnect: 'MCC', SalamTalk: 'ST', Dialphone: 'DP', Vestacall: 'VC' })[e] || e;
  }

  function updateCustomerLabel(n) {
    const badge = document.getElementById('customer-badge');
    if (!badge) return;
    if (state.selectedEntities.size === 0) {
      badge.textContent = '0 — pick an entity';
      badge.classList.add('empty');
    } else if (state.view !== 'customer-code') {
      badge.textContent = 'switch to Per Origin Trunk × Code view';
      badge.classList.remove('empty');
    } else if (n === 0) {
      badge.textContent = 'run a query';
      badge.classList.remove('empty');
    } else {
      const ents = Array.from(state.selectedEntities).map(entityShort).join('+');
      badge.textContent = n + ' in ' + ents;
      badge.classList.remove('empty');
    }
  }

  // ─── Run query ──────────────────────────────────────────────
  async function runQuery(opts) {
    opts = opts || {};
    if (!ensureToken()) return;
    if (state.selectedEntities.size === 0) {
      setStatus('Pick at least one entity', 'error');
      return;
    }
    if (!el.startDate.value) {
      setStatus('Pick a start date', 'error');
      return;
    }

    const endpoint = '/api/usa-customer-codes';
    const label = '(origin trunk × code) rows';
    const queryId = ++state.lastQueryAt;

    const body = getQueryBody();
    body._endpoint = endpoint;  // for local cache key
    if (opts.force) body.force_refresh = true;

    state.localCacheUsed = false;
    // ─── Instant render from an exact localStorage match ───
    if (!opts.force) {
      const local = localCacheGet(body);
      if (local) {
        const ageMin = Math.round((Date.now() - local.savedAt) / 60000);
        const r = local.response;
        state.rawRows = r.rows || [];
        state.customerNames = r.customers || (r.totals && r.totals.customers) || [];
        state.totalRowCount = r.total_row_count || state.rawRows.length;
        renderStats(r.totals || {});
        applyFilter();
        rebuildCustomerDropdownFromRows();
        state.lastCacheMeta = r._cache || {
          hit: true, refreshed_at: local.savedAt / 1000, age_seconds: ageMin * 60,
          tier: 'local', ttl: 60, stale: ageMin > 1, compute_ms: 0,
        };
        renderCachePill();
        const noteBits = local.body
          ? '(' + (local.body.start_date || '?') +
            (local.body.end_date && local.body.end_date !== local.body.start_date ? ' → ' + local.body.end_date : '') +
            ')'
          : '';
        setStatus(
          '📂 Loaded ' + state.rawRows.length.toLocaleString() + ' ' + label +
          ' from browser cache ' + noteBits + ' · saved ' + ageMin + ' min ago' +
          ' · refreshing in background…',
          'success'
        );
        state.localCacheUsed = true;
        el.cachePill.classList.add('refreshing');
      }
    }

    if (opts.force) {
      setStatus('Force refresh — bypassing cache, running DuckDB (~40-60s)…', 'loading');
      el.cachePill.classList.add('refreshing');
    } else if (!state.localCacheUsed) {
      setStatus('Loading — scans raw CSV to include STIR X5U, ~40-90s cold…', 'loading');
    }
    el.btnRun.disabled = true;
    if (!state.localCacheUsed) el.tableScroll.classList.add('is-loading');

    try {
      const r = await apiCall(endpoint, body);
      // Ignore stale responses if user already kicked another query
      if (queryId !== state.lastQueryAt) return;

      state.rawRows = r.json.rows || [];
      state.customerNames = r.json.customers || (r.json.totals && r.json.totals.customers) || [];
      state.totalRowCount = r.json.total_row_count || state.rawRows.length;
      renderStats(r.json.totals || {});
      applyFilter();
      rebuildCustomerDropdownFromRows();    // free — no extra API call

      // Save to local browser cache for next page load
      localCachePut(body, r.json);
      state.localCacheUsed = false;

      // Cache metadata + pill update
      state.lastCacheMeta = r.json._cache || null;
      el.cachePill.classList.remove('refreshing');
      renderCachePill();

      const t = r.json.totals || {};
      const meta = r.json._cache || {};
      const cacheTag = meta.hit
        ? (meta.stale ? '⚠ stale cache (' + fmtAge(meta.age_seconds) + ' old)' : '✓ cache hit')
        : '⚡ fresh compute (' + (meta.compute_ms / 1000).toFixed(1) + 's)';
      const extra = (t.customer_count !== undefined)
        ? '  ·  ' + fmtInt(t.customer_count) + ' trunks · ' + fmtInt(t.code_count) + ' codes · ' + fmtInt(t.pair_count) + ' observed pairs'
        : '  ·  ' + fmtInt(t.code_count) + ' distinct codes';
      setStatus(
        '✓ ' + state.rawRows.length.toLocaleString() + ' ' + label +
        ' in ' + (r.ms / 1000).toFixed(1) + 's · ' + cacheTag + extra,
        'success'
      );

      // If we served a stale cache, hint at the pill — the background systemd
      // refresher will repopulate within 5 min. User can click the pill to
      // force immediate refresh if they don't want to wait.
      if (meta.hit && meta.stale) {
        el.cachePill.classList.add('refreshing');
        setTimeout(() => el.cachePill.classList.remove('refreshing'), 300000);
      }
    } catch (e) {
      // If error happened on a stale query, don't show it (user moved on)
      if (queryId !== state.lastQueryAt) return;
      const isNet = /Failed to fetch|NetworkError|Load failed/i.test(e.message);
      if (state.localCacheUsed) {
        // We have data on screen from localStorage. Network failure is acceptable.
        transientError('Background refresh failed (' + e.message + ') — showing local cached data.', 8000);
      } else if (isNet && state.rawRows.length > 0) {
        transientError('Network blip during refresh — tunnel might have hiccuped. Showing previous result.', 6000);
      } else {
        setStatus('Query failed: ' + e.message, 'error');
      }
      el.cachePill.classList.remove('refreshing');
    } finally {
      el.btnRun.disabled = false;
      el.tableScroll.classList.remove('is-loading');
    }
  }
  el.btnRun.addEventListener('click', runQuery);

  // ─── CSV export (server streams DuckDB output directly to disk) ───
  el.btnCsv.addEventListener('click', async () => {
    if (!PROXY_AUTH && !state.token) { ensureToken(); return; }
    const endpoint = '/api/usa-customer-codes/csv-ticket';
    const body = getQueryBody();
    delete body.limit;
    const totalHint = state.totalRowCount ? state.totalRowCount.toLocaleString() : '';
    setStatus('Preparing full CSV' + (totalHint ? ' (' + totalHint + ' rows)' : '') + '…', 'loading');
    try {
      const resp = await fetch(endpoint, {
        method: 'POST',
        headers: apiHeaders(),
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        let detail = 'HTTP ' + resp.status;
        try { detail = (await resp.json()).error || detail; } catch (ignore) {}
        throw new Error(detail);
      }
      const ticket = await resp.json();
      if (!ticket.download_url) throw new Error('server did not return a download link');
      const a = document.createElement('a');
      a.href = ticket.download_url;
      a.download = '';
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setStatus(
        'CSV export started' + (totalHint ? ' for ' + totalHint + ' rows' : '') +
        '. Keep this page open until the browser download appears.',
        'success'
      );
    } catch (e) {
      setStatus('CSV export failed: ' + e.message, 'error');
    }
  });
  // ─── (legacy client-side CSV builder — kept dead but replaced above) ─
  // eslint-disable-next-line no-unused-vars
  function _legacy_csv_export() {
    const baseCols = ['code', 'term_code', 'state', 'ratecenter', 'attempts', 'completions', 'asr_pct', 'minutes', 'revenue', 'cost', 'margin'];
    const cols = state.view === 'customer-code' ? ['customer'].concat(baseCols) : baseCols;
    const lines = [cols.join(',')];
    for (const r of state.filteredRows) {
      lines.push(cols.map((c) => {
        const v = r[c];
        if (v === null || v === undefined) return '';
        if (typeof v === 'string' && (v.includes(',') || v.includes('"') || v.includes('\n'))) {
          return '"' + v.replace(/"/g, '""') + '"';
        }
        return String(v);
      }).join(','));
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const viewSuffix = 'customer_codes';
    a.download = 'cdr_usa_' + viewSuffix + '_' + el.startDate.value + '_to_' + el.endDate.value + '.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    setStatus('Downloaded ' + state.filteredRows.length.toLocaleString() + ' rows as CSV', 'success');
  }

  // ─── Keyboard shortcuts ─────────────────────────────────────
  document.addEventListener('keydown', (e) => {
    // Cmd/Ctrl + Enter = Run query
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      runQuery();
    }
    // / = focus filter
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
      e.preventDefault();
      el.filterInput.focus();
    }
  });

  // ─── Init ───────────────────────────────────────────────────
  applyPreset('yesterday');
  setHourPreset('all');
  setView('customer-code');
  state.initializing = false;
  if (PROXY_AUTH) {
    el.btnLogout.classList.add('hidden');
    localStorage.removeItem('cdr_direct_token');
  }
  ensureToken();
  setStatus('', '');
  updateCustomerLabel(0);
  renderCachePill();
  startCachePillTicker();
  if (PROXY_AUTH || state.token) {
    setStatus('Opening the latest prepared full-day snapshot…', 'loading');
    loadDailySnapshot()
      .then((loaded) => {
        if (!loaded) {
          setStatus(
            'Preparing the first daily snapshot — this one-time scan can take 40–90s…',
            'loading'
          );
          runQuery();
        }
      })
      .catch((e) => {
        setStatus(
          'Snapshot unavailable (' + e.message + ') — loading through the normal cache…',
          'loading'
        );
        runQuery();
      });
  }
})();

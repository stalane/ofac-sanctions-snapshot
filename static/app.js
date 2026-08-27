(function () {
  'use strict';

  var FONT = { family: 'DejaVu Sans' };
  var COLORS = {
    blue: '#4f9cf7',
    green: '#2ecc8f',
    red: '#ff5a5f',
    gold: '#f7b32b',
    violet: '#a78bfa',
    muted: '#8aa0bd'
  };
  var PALETTE = [COLORS.blue, COLORS.green, COLORS.gold, COLORS.violet, COLORS.muted, COLORS.red];
  var TYPE_COLORS = {
    Individual: COLORS.blue,
    Entity: COLORS.green,
    Vessel: COLORS.gold,
    Aircraft: COLORS.violet
  };

  var charts = {};
  var asOf = null;
  var pollTimer = null;
  var progressTimer = null;
  var geoSettled = false;
  var userPicked = false;

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fmt(n) {
    return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(n);
  }

  function fmtCompact(n) {
    return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(n);
  }

  function truncate(s, len) {
    s = String(s == null ? '' : s);
    return s.length > (len || 60) ? s.slice(0, (len || 60) - 1) + '…' : s;
  }

  function asOfText(iso) {
    var clean = String(iso || '').replace(/\.\d+/, '');
    var d = new Date(clean);
    return isNaN(d.getTime()) ? (iso || '—') : d.toLocaleString();
  }

  function setStatus(cls, text) {
    var el = $('status');
    el.className = 'status' + (cls ? ' ' + cls : '');
    el.textContent = text;
  }

  function setState(panelEl, state) {
    panelEl.dataset.state = state;
  }

  function fetchJSON(url) {
    return fetch(url).then(function (res) {
      if (!res.ok) throw new Error(url + ' -> ' + res.status);
      return res.json();
    });
  }

  function deepMerge(base, over) {
    var out = Array.isArray(base) ? base.slice() : Object.assign({}, base);
    if (over && typeof over === 'object') {
      Object.keys(over).forEach(function (k) {
        if (over[k] && typeof over[k] === 'object' && !Array.isArray(over[k]) &&
            out[k] && typeof out[k] === 'object' && !Array.isArray(out[k])) {
          out[k] = deepMerge(out[k], over[k]);
        } else {
          out[k] = over[k];
        }
      });
    }
    return out;
  }

  var themeOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        labels: { color: COLORS.muted, usePointStyle: true, boxWidth: 10, font: FONT }
      }
    },
    scales: {
      x: { grid: { color: '#1e2a3a' }, ticks: { color: COLORS.muted, font: FONT } },
      y: { grid: { color: '#1e2a3a' }, ticks: { color: COLORS.muted, font: FONT } }
    }
  };

  function makeChart(id, type, data, overrides) {
    var el = $(id);
    if (!el) return;
    if (charts[id]) { charts[id].destroy(); delete charts[id]; }
    var options = overrides ? deepMerge(themeOptions, overrides) : themeOptions;
    charts[id] = new Chart(el, { type: type, data: data, options: options });
  }

  /* ---------------- rendering ---------------- */

  function renderMeta(m) {
    $('asof').textContent = asOfText(m.data_as_of);
    $('kpi-entities').textContent = fmt(m.entities);
    $('kpi-countries').textContent = fmt(m.countries);
    $('kpi-programs').textContent = fmt(m.programs);
    $('kpi-lists').textContent = fmt(m.lists);
    var delta = $('kpi-entities-delta');
    if (m.entities > 0) {
      delta.textContent = '▲ ' + fmtCompact(m.entities) + ' total';
      delta.className = 'kpi-delta up';
    }
    var tops = document.querySelectorAll('.panel-asof');
    for (var i = 0; i < tops.length; i++) tops[i].textContent = asOfText(m.data_as_of);
  }

  function renderCountries(data) {
    var rows = data.slice(0, 15);
    makeChart('chart-topCountries', 'bar', {
      labels: rows.map(function (r) { return r.country; }),
      datasets: [{
        label: 'Sanctioned entities',
        data: rows.map(function (r) { return r.total; }),
        backgroundColor: 'rgba(79,156,247,0.7)',
        borderColor: COLORS.blue,
        borderWidth: 1,
        borderRadius: 4
      }]
    }, { indexAxis: 'y' });

    var select = $('country-select');
    var prev = select.value;
    select.innerHTML = data.map(function (r) {
      return '<option value="' + esc(r.country) + '">' + esc(r.country) + ' (' + fmt(r.total) + ')</option>';
    }).join('');
    if (prev && data.some(function (r) { return r.country === prev; })) {
      select.value = prev;
    } else if (!userPicked) {
      select.value = '';
    }
  }

  function renderListsOverview(data) {
    makeChart('chart-lists', 'doughnut', {
      labels: data.map(function (x) { return x.list_name; }),
      datasets: [{
        data: data.map(function (x) { return x.count; }),
        backgroundColor: data.map(function (_, i) { return PALETTE[i % PALETTE.length]; }),
        borderColor: '#121a28',
        borderWidth: 2
      }]
    }, { cutout: '62%' });
  }

  function fillCountry(d) {
    var tMap = {};
    var total = 0;
    d.types.forEach(function (t) {
      tMap[t.entity_type] = t.count;
      total += t.count;
    });
    $('c-entities').textContent = fmt(total);
    $('c-individuals').textContent = fmt(tMap.Individual || 0);
    $('c-organizations').textContent = fmt(tMap.Entity || 0);
    $('c-vessels').textContent = fmt(tMap.Vessel || 0);

    makeChart('chart-types', 'doughnut', {
      labels: d.types.map(function (t) { return t.entity_type; }),
      datasets: [{
        data: d.types.map(function (t) { return t.count; }),
        backgroundColor: d.types.map(function (t) { return TYPE_COLORS[t.entity_type] || COLORS.muted; }),
        borderColor: '#121a28',
        borderWidth: 2
      }]
    }, { cutout: '62%' });

    makeChart('chart-programs', 'bar', {
      labels: d.programs.map(function (p) { return p.program; }),
      datasets: [{
        label: 'Entities',
        data: d.programs.map(function (p) { return p.count; }),
        backgroundColor: 'rgba(79,156,247,0.7)',
        borderColor: COLORS.blue,
        borderWidth: 1,
        borderRadius: 4
      }]
    }, { indexAxis: 'y' });

    makeChart('chart-countryLists', 'bar', {
      labels: d.lists.map(function (l) { return l.list_name; }),
      datasets: [{
        label: 'Entities',
        data: d.lists.map(function (l) { return l.count; }),
        backgroundColor: 'rgba(247,179,43,0.7)',
        borderColor: COLORS.gold,
        borderWidth: 1,
        borderRadius: 4
      }]
    }, { indexAxis: 'y' });

    var tbody = $('country-table');
    if (!d.entities.length) {
      tbody.innerHTML = '<tr><td colspan="4" style="color:var(--muted)">No entities listed for this country.</td></tr>';
      return;
    }
    tbody.innerHTML = d.entities.map(function (e) {
      return '<tr>' +
        '<td>' + esc(e.name) + '</td>' +
        '<td>' + esc(e.entity_type) + '</td>' +
        '<td>' + esc(truncate(e.programs, 40)) + '</td>' +
        '<td>' + esc(truncate(e.lists, 40)) + '</td>' +
        '</tr>';
    }).join('');
  }

  /* ---------------- loaders ---------------- */

  function loadTopCountries() {
    var panel = $('panel-topCountries');
    setState(panel, 'loading');
    return fetchJSON('/api/countries').then(function (res) {
      renderCountries(res.data);
      setState(panel, 'ready');
      return res.data;
    }).catch(function () {
      setState(panel, 'error');
      throw new Error('topCountries');
    });
  }

  function loadListsOverview() {
    var panel = $('panel-lists');
    setState(panel, 'loading');
    return fetchJSON('/api/lists').then(function (res) {
      renderListsOverview(res.data);
      setState(panel, 'ready');
    }).catch(function () {
      setState(panel, 'error');
    });
  }

  function loadCountry(name) {
    var panel = $('panel-country');
    setState(panel, 'loading');
    $('country-content').classList.add('hidden');
    return fetchJSON('/api/country/' + encodeURIComponent(name)).then(function (d) {
      fillCountry(d);
      setState(panel, 'ready');
      $('country-content').classList.remove('hidden');
    }).catch(function () {
      setState(panel, 'error');
    });
  }

  var GEO_CACHE_KEY = 'ofac_geo_country';
  var GEO_CACHE_TTL = 24 * 60 * 60 * 1000;
  var GEO_PROVIDERS = [
    {
      url: 'http://ip-api.com/json/?fields=status,country',
      httpOnly: true,
      parse: function (d) { return d && d.status === 'success' ? (d.country || null) : null; }
    },
    {
      url: 'https://ipwho.is/',
      httpOnly: false,
      parse: function (d) { return d && d.success ? (d.country || null) : null; }
    },
    {
      url: 'https://ipapi.co/json/',
      httpOnly: false,
      parse: function (d) { return d && d.country_name ? d.country_name : null; }
    }
  ];

  function cachedGeoCountry() {
    try {
      var raw = localStorage.getItem(GEO_CACHE_KEY);
      if (!raw) return null;
      var obj = JSON.parse(raw);
      if (obj && obj.country && Date.now() - obj.ts < GEO_CACHE_TTL) return obj.country;
    } catch (e) {}
    return null;
  }

  function cacheGeoCountry(country) {
    try {
      localStorage.setItem(GEO_CACHE_KEY, JSON.stringify({ country: country, ts: Date.now() }));
    } catch (e) {}
  }

  function getGeoCountry() {
    var cached = cachedGeoCountry();
    if (cached) return Promise.resolve(cached);
    var chain = [];
    GEO_PROVIDERS.forEach(function (p) {
      if (p.httpOnly && location.protocol === 'https:') return;
      chain.push(p);
    });
    function attempt(i) {
      if (i >= chain.length) return Promise.resolve(null);
      var p = chain[i];
      var controller = new AbortController();
      var timer = setTimeout(function () { controller.abort(); }, 8000);
      return fetch(p.url, { signal: controller.signal })
        .then(function (res) {
          if (!res.ok) throw new Error('geo ' + res.status);
          return res.json();
        })
        .then(function (d) {
          var c = p.parse(d);
          if (!c) throw new Error('geo empty');
          cacheGeoCountry(c);
          return c;
        })
        .catch(function () { return attempt(i + 1); })
        .finally(function () { clearTimeout(timer); });
    }
    return attempt(0);
  }

  function setDefaultCountry(data) {
    var select = $('country-select');
    var countries = data.map(function (r) { return r.country; });
    if (userPicked && select.value && countries.indexOf(select.value) !== -1) {
      return Promise.resolve(select.value);
    }
    return getGeoCountry().then(function (geo) {
      if (!geo) return getGeoCountry();
      return geo;
    }).then(function (geo) {
      var matched = null;
      if (geo && window.CountryMatch) matched = window.CountryMatch.match(geo, countries);
      var country = matched || (countries[0] || '');
      select.value = country;
      return country;
    });
  }

  function settleGeo() {
    getGeoCountry().then(function (geo) {
      var select = $('country-select');
      if (!geo || !window.CountryMatch) return;
      var options = Array.prototype.map.call(select.options, function (o) { return o.value; });
      var matched = window.CountryMatch.match(geo, options);
      if (matched && matched !== select.value) {
        select.value = matched;
        loadCountry(matched);
      }
    });
  }

  function renderDashboard() {
    return loadTopCountries()
      .then(function (data) { return setDefaultCountry(data); })
      .then(function (country) { return loadCountry(country); })
      .then(loadListsOverview)
      .catch(function () {});
  }

  /* ---------------- readiness / refresh ---------------- */

  function ensureReady() {
    fetchJSON('/api/meta').then(function (m) {
      if (!m.ready) {
        showProgress();
        startProgressPoll();
        setStatus('', 'fetching data…');
        pollTimer = setTimeout(ensureReady, 5000);
        return;
      }
      renderMeta(m);
      asOf = m.data_as_of;
      if (m.refreshing) {
        showProgress();
        startProgressPoll();
        setStatus('refreshing', 'refreshing data…');
        pollTimer = setTimeout(waitForRefresh, 4000);
        return;
      }
      stopProgressPoll();
      hideProgress();
      setStatus('live', 'live');
      renderDashboard();
      if (!geoSettled) {
        geoSettled = true;
        settleGeo();
      }
    }).catch(function () {
      setStatus('offline', 'offline');
      pollTimer = setTimeout(ensureReady, 5000);
    });
  }

  function showProgress() {
    $('progress-modal').classList.remove('hidden');
  }

  function hideProgress() {
    $('progress-modal').classList.add('hidden');
  }

  function renderProgress(p) {
    var fill = $('progress-fill');
    var status = $('modal-status');
    if (!fill || !status) return;
    if (p.phase === 'downloading') {
      var mb = p.bytes / 1048576;
      if (p.total_bytes) {
        var pct = Math.min(100, (p.bytes / p.total_bytes) * 100);
        fill.classList.remove('indeterminate');
        fill.style.width = pct.toFixed(1) + '%';
        status.textContent = 'Downloading… ' + mb.toFixed(1) + ' of ' +
          (p.total_bytes / 1048576).toFixed(1) + ' MB';
      } else {
        fill.classList.add('indeterminate');
        status.textContent = 'Downloading… ' + mb.toFixed(1) + ' MB';
      }
    } else if (p.phase === 'parsing') {
      fill.classList.remove('indeterminate');
      fill.style.width = '100%';
      status.textContent = 'Parsing… ' + fmt(p.entities || 0) + ' entities';
    }
  }

  function pollProgress() {
    fetchJSON('/api/progress').then(renderProgress).catch(function () {});
  }

  function startProgressPoll() {
    stopProgressPoll();
    pollProgress();
    progressTimer = setInterval(pollProgress, 1000);
  }

  function stopProgressPoll() {
    if (progressTimer) {
      clearInterval(progressTimer);
      progressTimer = null;
    }
  }

  function waitForRefresh() {
    fetchJSON('/api/meta').then(function (m) {
      if (!m.ready || m.refreshing) {
        setStatus('refreshing', 'refreshing data…');
        pollTimer = setTimeout(waitForRefresh, 4000);
        return;
      }
      stopProgressPoll();
      hideProgress();
      renderMeta(m);
      asOf = m.data_as_of;
      setStatus('live', 'live');
      $('refresh').disabled = false;
      renderDashboard();
    }).catch(function () {
      pollTimer = setTimeout(waitForRefresh, 4000);
    });
  }

  function refresh() {
    if (pollTimer) clearTimeout(pollTimer);
    $('refresh').disabled = true;
    setStatus('refreshing', 'refreshing data…');
    showProgress();
    startProgressPoll();
    fetch('/api/refresh', { method: 'POST' }).then(function () {
      waitForRefresh();
    }).catch(function () {
      stopProgressPoll();
      hideProgress();
      setStatus('offline', 'refresh failed');
      $('refresh').disabled = false;
    });
  }

  /* ---------------- wiring ---------------- */

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.retry');
    if (!btn) return;
    var target = btn.dataset.target;
    if (target === 'topCountries') loadTopCountries().then(function (data) {
      return loadCountry($('country-select').value || (data[0] && data[0].country) || '');
    });
    else if (target === 'lists') loadListsOverview();
    else if (target === 'country') loadCountry($('country-select').value);
  });

  $('country-select').addEventListener('change', function () {
    userPicked = true;
    loadCountry(this.value);
  });

  $('refresh').addEventListener('click', refresh);

  ensureReady();
})();
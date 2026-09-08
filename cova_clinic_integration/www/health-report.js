// Clinic Analytics — the /health-report portal dashboard.
//
// One page, five views selected from the floating rail. Every view is a
// descriptor (endpoint, filters, KPI/chart/table renderers) fed through the
// same load-render cycle, so adding a dashboard means adding a descriptor.
//
// The page shell is creamy white / white / black; colour is reserved for the
// charts, where it does encoding work:
//   * categorical  — one fixed hue order, assigned by slot and never cycled;
//     multi-series lines carry a distinct marker shape as a secondary channel
//     so identity never rests on hue alone;
//   * sequential   — one blue hue, light -> dark, for magnitude (heat cells);
//   * status       — the reserved good/warning/serious/critical steps, used
//     only for clinical outcomes, always beside a visible label.

(function (boot) {
  // The CSRF token script sits at the very end of <body>, after this file, so
  // defer everything until the document is ready or fetch() will be rejected.
  if (window.frappe && frappe.ready) { frappe.ready(boot); }
  else { document.addEventListener('DOMContentLoaded', boot); }
})(function () {
  'use strict';

  var app = document.getElementById('cova-clinic-app');
  if (!app) { return; }

  var INK = '#111111';
  var SURFACE = '#ffffff';
  var GRID = '#e1e0d9';
  var AXIS_TEXT = '#898781';

  // Categorical slots, in fixed order — the ordering is the colour-blind-safety
  // mechanism, so slots are assigned by index and never cycled or reshuffled.
  var SERIES = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'];

  // Single blue hue, light -> dark, for magnitude in table cells.
  var SEQ = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#104281'];

  // Reserved status steps — never used as a series colour.
  var STATUS = {
    good: '#0ca30c',
    warning: '#fab219',
    serious: '#ec835a',
    critical: '#d03b3b'
  };

  // COVA's clinical outcomes are states, not categories, so they wear the
  // status palette. Every bar is labelled on the axis, so hue never carries
  // the meaning on its own.
  var OUTCOME_COLORS = {
    FitForWork: STATUS.good,
    FitWithRestrictions: STATUS.warning,
    InconclusiveRetestRequired: STATUS.serious,
    UnfitForWork: STATUS.critical
  };

  // Every line is solid; marker shape is the only secondary channel. Filled
  // shapes only: Chart.js draws 'star'/'cross' as thin strokes, which all but
  // vanish in the legend at a light series colour.
  var POINT_STYLES = ['circle', 'rect', 'triangle', 'rectRot', 'rectRounded'];

  var CHECKIN_URL = '/api/method/cova_clinic_integration.api.clinic_checkin_report';
  var REQUEST_URL = '/api/method/cova_clinic_integration.api.clinic_test_request_report';
  var RESULT_URL = '/api/method/cova_clinic_integration.api.clinic_test_result_report';
  var VISIT_URL = '/api/method/cova_clinic_integration.api.clinic_visit_cost_report';

  var charts = [];
  var current = 'health';

  // Global filters that persist across all views
  var GLOBAL_FILTERS = [
    { key: 'year', label: 'Year', type: 'select', src: 'years', all: 'All Years' },
    { key: 'month', label: 'Month', type: 'select', src: 'months', all: 'All' },
    { key: 'from_date', label: 'From', type: 'date', notAfter: 'to_date' },
    { key: 'to_date', label: 'To', type: 'date', notBefore: 'from_date' }
  ];
  var globalValues = {};
  var globalLabels = {};
  var globalFilterOptions = {};

  // ── small helpers ─────────────────────────────────────────────────

  function el(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function csrf() {
    try { return (window.frappe && frappe.csrf_token) ? frappe.csrf_token : ''; }
    catch (e) { return ''; }
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Frappe-CSRF-Token': csrf() },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (payload) {
        if (!r.ok) {
          var exc = payload && (payload._server_messages || payload.exception || payload.message);
          throw new Error(typeof exc === 'string' ? exc : ('HTTP ' + r.status));
        }
        return payload && payload.message ? payload.message : {};
      });
    });
  }

  function slot(index) { return SERIES[index % SERIES.length]; }

  function ensureChartJs(cb) {
    if (window.Chart) { cb(); return; }
    var s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js';
    s.onload = cb;
    s.onerror = function () { cb(); };
    document.head.appendChild(s);
  }

  function destroyCharts() {
    charts.forEach(function (c) { if (c) { c.destroy(); } });
    charts = [];
  }

  function num(v) { return (v === null || v === undefined) ? 0 : v; }

  function money(v) {
    return Number(v || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  // The currency the costs are in, as the server reports it for the configured
  // company. Amounts that stand on their own carry it; the month grid and the
  // line-item lists name it once in their heading instead, because repeating it
  // down thirteen columns costs width and buys nothing.
  var CURRENCY = '';

  // Tile-sized money: K from ten thousand, M from a million, exact below that.
  // The exact figure rides along as the tile's tooltip.
  function cashShort(v) {
    var n = Number(v || 0), abs = Math.abs(n), sign = n < 0 ? '-' : '';
    if (abs >= 1e6) { return CURRENCY + ' ' + sign + trimNum(abs / 1e6, 2) + 'M'; }
    if (abs >= 1e4) { return CURRENCY + ' ' + sign + trimNum(abs / 1e3, 1) + 'K'; }
    return cash(v);
  }
  function trimNum(n, d) { return String(Number(n.toFixed(d))); }

  function cash(v) {
    return CURRENCY ? CURRENCY + ' ' + money(v) : money(v);
  }

  // For a heading that labels a whole block of bare amounts.
  function inCurrency(label) {
    return CURRENCY ? label + ' (' + CURRENCY + ')' : label;
  }

  // Sequential ramp for table heat cells: one hue, binned light -> dark, so
  // every cell lands on a documented step rather than an arbitrary blend.
  function heatStep(v, max) {
    if (!v || !max) { return -1; }
    return Math.min(SEQ.length - 1, Math.floor((v / max) * SEQ.length - 1e-9));
  }
  function heatBg(v, max) {
    var i = heatStep(v, max);
    return i < 0 ? 'transparent' : SEQ[i];
  }
  function heatFg(v, max) {
    // The top two steps are dark enough to need light text.
    return heatStep(v, max) >= SEQ.length - 2 ? '#ffffff' : INK;
  }

  // ── date picker ───────────────────────────────────────────────────
  // A small month calendar, because the portal bundle ships no date-picker
  // library and the native <input type="date"> control cannot be themed.
  // Values are held on the input as plain ISO yyyy-mm-dd, so every other part
  // of the page keeps reading input.value and nothing else has to change.

  var DAY_NAMES = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'];
  var MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
                     'July', 'August', 'September', 'October', 'November', 'December'];
  // Mirrors MONTH_LABELS in api.py — the labels every grid column is keyed by.
  var MONTH_LABELS = MONTH_NAMES.map(function (m) { return m.slice(0, 3).toUpperCase(); });
  var openCal = null;

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function toIso(d) {
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }

  // Parsed as local time on purpose: `new Date('2026-03-05')` is UTC midnight,
  // which lands on the previous day for anyone west of Greenwich.
  function fromIso(str) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(str || '');
    return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
  }

  function closeCalendar() {
    if (openCal) { openCal.remove(); openCal = null; }
  }

  function attachCalendar(input, view, field) {
    input.addEventListener('click', function (e) {
      e.stopPropagation();
      if (openCal && openCal.dataset.owner === input.id) { closeCalendar(); return; }
      openCalendar(input, view, field);
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
      if (e.key === 'Escape') { closeCalendar(); }
    });
  }

  function openCalendar(input, view, field) {
    closeCalendar();

    var selected = fromIso(input.value);
    var cursor = selected ? new Date(selected.getFullYear(), selected.getMonth(), 1)
                          : new Date(new Date().getFullYear(), new Date().getMonth(), 1);

    // A From date cannot sit after the To date, and vice versa — the bound is
    // read live so it tracks whatever the other field currently holds.
    function bounds() {
      var min = null, max = null;
      if (field.notBefore) { min = fromIso((el('f-' + field.notBefore) || {}).value); }
      if (field.notAfter) { max = fromIso((el('f-' + field.notAfter) || {}).value); }
      return { min: min, max: max };
    }

    var cal = document.createElement('div');
    cal.className = 'cdd-cal';
    cal.dataset.owner = input.id;
    cal.addEventListener('click', function (e) { e.stopPropagation(); });

    function render() {
      var b = bounds();
      var year = cursor.getFullYear();
      var month = cursor.getMonth();
      var today = toIso(new Date());

      var h = '<div class="cdd-cal-head">' +
              '<button type="button" class="cdd-cal-nav" data-step="-1" aria-label="Previous month">&#8249;</button>' +
              '<span class="cdd-cal-label">' + MONTH_NAMES[month] + ' ' + year + '</span>' +
              '<button type="button" class="cdd-cal-nav" data-step="1" aria-label="Next month">&#8250;</button>' +
              '</div><div class="cdd-cal-grid">';

      DAY_NAMES.forEach(function (d) { h += '<span class="cdd-cal-dow">' + d + '</span>'; });

      // Monday-first grid: JS getDay() is Sunday-first, so shift by one.
      var first = new Date(year, month, 1);
      var lead = (first.getDay() + 6) % 7;
      var days = new Date(year, month + 1, 0).getDate();

      for (var i = 0; i < lead; i++) { h += '<span class="cdd-cal-pad"></span>'; }
      for (var day = 1; day <= days; day++) {
        var date = new Date(year, month, day);
        var iso = toIso(date);
        var disabled = (b.min && date < b.min) || (b.max && date > b.max);
        var cls = 'cdd-cal-day';
        if (iso === input.value) { cls += ' is-selected'; }
        if (iso === today) { cls += ' is-today'; }
        if (disabled) { cls += ' is-disabled'; }
        h += '<button type="button" class="' + cls + '"' +
             (disabled ? ' disabled' : ' data-iso="' + iso + '"') + '>' + day + '</button>';
      }

      h += '</div><div class="cdd-cal-foot">' +
           '<button type="button" class="cdd-cal-action" data-action="today">Today</button>' +
           '<button type="button" class="cdd-cal-action" data-action="clear">Clear</button>' +
           '</div>';

      cal.innerHTML = h;

      Array.prototype.forEach.call(cal.querySelectorAll('.cdd-cal-nav'), function (btn) {
        btn.addEventListener('click', function () {
          cursor = new Date(cursor.getFullYear(), cursor.getMonth() + Number(btn.dataset.step), 1);
          render();
        });
      });
      Array.prototype.forEach.call(cal.querySelectorAll('.cdd-cal-day[data-iso]'), function (btn) {
        btn.addEventListener('click', function () { commit(btn.dataset.iso); });
      });
      cal.querySelector('[data-action="today"]').addEventListener('click', function () {
        var b2 = bounds();
        var now = new Date();
        if ((b2.min && now < b2.min) || (b2.max && now > b2.max)) { return; }
        commit(toIso(now));
      });
      cal.querySelector('[data-action="clear"]').addEventListener('click', function () { commit(''); });
    }

    function commit(iso) {
      input.value = iso;
      closeCalendar();
      input.dispatchEvent(new Event('change'));
    }

    render();
    input.parentNode.appendChild(cal);
    openCal = cal;
  }

  document.addEventListener('click', closeCalendar);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { closeCalendar(); }
  });

  // ── link field (typeahead) ────────────────────────────────────────
  // Options arrive as [{value, label}] with the filter payload, so matching is
  // done in the page rather than round-tripping a search to the server.

  var openLink = null;

  function closeLink() {
    if (openLink) { openLink.remove(); openLink = null; }
  }

  function attachLink(input, view, field, options) {
    var v = VIEWS[view];

    function commit(opt) {
      v.values = v.values || {};
      v.labels = v.labels || {};
      input.dataset.value = opt ? opt.value : '';
      input.value = opt ? opt.label : '';
      v.values[field.key] = input.dataset.value;
      v.labels[field.key] = input.value;
      closeLink();
      input.dispatchEvent(new Event('change'));
    }

    function show() {
      closeLink();
      var typed = input.value.trim().toLowerCase();
      // While a selection stands, typing nothing lists everything again.
      var isSelection = input.dataset.value && input.value === (v.labels || {})[field.key];
      var matches = (isSelection || !typed)
        ? options
        : options.filter(function (o) { return o.label.toLowerCase().indexOf(typed) !== -1; });

      var box = document.createElement('div');
      box.className = 'cdd-linklist';
      box.addEventListener('mousedown', function (e) { e.preventDefault(); });

      if (!matches.length) {
        box.innerHTML = '<div class="cdd-linkempty">No matching employee</div>';
      } else {
        var shown = matches.slice(0, 200);
        var h = '<button type="button" class="cdd-linkopt is-any" data-any="1">All employees</button>';
        shown.forEach(function (o, i) {
          h += '<button type="button" class="cdd-linkopt' +
               (o.value === input.dataset.value ? ' is-selected' : '') +
               '" data-i="' + i + '">' + esc(o.label) + '</button>';
        });
        if (matches.length > shown.length) {
          h += '<div class="cdd-linkempty">' + (matches.length - shown.length) +
               ' more — keep typing to narrow</div>';
        }
        box.innerHTML = h;
        Array.prototype.forEach.call(box.querySelectorAll('.cdd-linkopt'), function (btn) {
          btn.addEventListener('click', function () {
            commit(btn.dataset.any ? null : shown[Number(btn.dataset.i)]);
          });
        });
      }

      input.parentNode.appendChild(box);
      openLink = box;
    }

    input.addEventListener('focus', show);
    input.addEventListener('click', function (e) { e.stopPropagation(); show(); });
    input.addEventListener('input', show);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { closeLink(); input.blur(); }
    });
    // Leaving a half-typed name would otherwise show a label that no longer
    // matches the id being filtered on.
    input.addEventListener('blur', function () {
      setTimeout(function () {
        var label = (v.labels || {})[field.key] || '';
        if (input.value !== label) { input.value = label; }
        closeLink();
      }, 120);
    });

    input.parentNode.querySelector('.cdd-linkclear')
      .addEventListener('click', function (e) { e.stopPropagation(); commit(null); });
  }

  document.addEventListener('click', closeLink);

  // ── block builders ────────────────────────────────────────────────

  // A tile carrying `drill` becomes a button that opens the panel listing the
  // records behind its number. A tile showing zero is left inert — there is
  // nothing to look at, and a click that opens an empty panel reads as a fault.
  // Which month a tile's trend is judged at: the month filter if one is set,
  // otherwise the latest month in the series with anything in it.
  function trendIndex(series) {
    if (globalValues.month) {
      var mi = MONTH_LABELS.indexOf(globalValues.month);
      if (mi >= 0) { return mi; }
    }
    for (var i = series.length - 1; i >= 0; i--) { if (series[i]) { return i; } }
    return -1;
  }

  // A small area chart of the year by month plus an up/down badge against
  // the previous month. Colour follows direction, the same on every tile:
  // an increase is red, a decrease green, no change grey.
  function kpiTrend(series) {
    if (!series || !series.length) { return ''; }
    var idx = trendIndex(series);
    if (idx < 0) { return ''; }
    var cur = Number(series[idx]) || 0;
    var prev = idx > 0 ? Number(series[idx - 1]) || 0 : null;

    var dir = prev === null ? 'flat' : (cur > prev ? 'up' : (cur < prev ? 'down' : 'flat'));
    var color = dir === 'up' ? '#e34948' : dir === 'down' ? '#1baf7a' : '#898781';

    var W = 84, H = 30, PAD = 2;
    var max = Math.max.apply(null, series.map(Number)) || 1;
    var step = (W - 2 * PAD) / (series.length - 1);
    var pts = series.map(function (v, i) {
      var x = PAD + i * step;
      var y = H - PAD - ((Number(v) || 0) / max) * (H - 2 * PAD - 4);
      return [x.toFixed(1), y.toFixed(1)];
    });
    var line = pts.map(function (p) { return p.join(','); }).join(' ');
    var area = 'M' + pts[0][0] + ',' + (H - PAD) + ' L' + pts.map(function (p) { return p.join(','); }).join(' L') +
               ' L' + pts[pts.length - 1][0] + ',' + (H - PAD) + ' Z';
    var spark = '<svg class="cdd-spark" viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" aria-hidden="true">' +
                '<path d="' + area + '" fill="' + color + '" fill-opacity="0.14"/>' +
                '<polyline fill="none" stroke="' + color + '" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" points="' + line + '"/>' +
                '<line x1="' + PAD + '" y1="' + (H - PAD) + '" x2="' + (W - PAD) + '" y2="' + (H - PAD) + '" stroke="' + color + '" stroke-opacity="0.35" stroke-width="1"/>' +
                '<circle cx="' + pts[idx][0] + '" cy="' + pts[idx][1] + '" r="2.6" fill="' + color + '"/></svg>';

    var badge = '';
    if (prev !== null && (cur || prev)) {
      var text = dir === 'flat' ? '0%'
        : (prev ? Math.round(Math.abs(cur - prev) / prev * 100) + '%' : 'new');
      badge = '<span class="cdd-kpi-delta" style="color:' + color + '" title="vs ' + MONTH_LABELS[idx - 1] + '">' +
              (dir === 'up' ? '\u25b2' : dir === 'down' ? '\u25bc' : '\u25ac') + ' ' + text + '</span>';
    }
    return '<div class="cdd-kpi-trend">' + spark + badge + '</div>';
  }

  function kpiBlock(tiles) {
    var h = '<div class="cdd-kpis">';
    tiles.forEach(function (t) {
      // `drill` may legitimately be '' (meaning "everything"), so test for the
      // key's presence rather than its truthiness.
      var live = t.drill !== undefined && Number(t.v) > 0;
      h += '<div class="cdd-kpi' + (live ? ' cdd-kpi-drill' : '') + '"' +
           (live ? ' data-drill="' + esc(t.drill) + '" data-drill-label="' + esc(t.l) + '"' +
                   ' role="button" tabindex="0" title="Show the people behind this"' : '') +
           // Title first, then the figure with its trend beside it.
           '><div class="l">' + esc(t.l) + '</div>' +
           '<div class="cdd-kpi-row"><div class="v"' + (t.full ? ' title="' + esc(t.full) + '"' : '') + '>' + esc(t.v) + '</div>' +
           kpiTrend(t.series) + '</div>' +
           '</div>';
    });
    return h + '</div>';
  }

  // Each entry is a canvas id, or {id, action: {label, action}} for a chart
  // with a button in its corner (wired by wireChartActions after render).
  function chartRow(ids, single) {
    var h = '<div class="cdd-charts-row' + (single ? ' is-single' : '') + '">';
    ids.forEach(function (c) {
      var id = typeof c === 'string' ? c : c.id;
      var action = typeof c === 'string' ? null : c.action;
      h += '<div class="cdd-chart-wrap">' +
           (action
             ? '<button type="button" class="cdd-chart-action" data-action="' + esc(action.action) + '">' +
               esc(action.label) + '</button>'
             : '') +
           '<canvas id="' + id + '"></canvas></div>';
    });
    return h + '</div>';
  }

  function tableBlock(title, head, bodyRows, opts) {
    opts = opts || {};
    var h = title ? '<div class="cdd-section-title">' + esc(title) + '</div>' : '';
    h += '<div class="cdd-table-wrap"><table><thead><tr>';
    head.forEach(function (c) {
      // A plain string is a static column. An object marks the column sortable:
      // the caller reads data-sort back on click and re-renders.
      if (c && typeof c === 'object') {
        h += '<th class="cdd-sort' + (c.active ? ' is-active' : '') + '"' +
             ' data-sort="' + esc(c.key) + '" role="button" tabindex="0"' +
             ' aria-sort="' + (c.active ? (c.dir > 0 ? 'ascending' : 'descending') : 'none') + '">' +
             esc(c.label) +
             '<span class="cdd-caret" aria-hidden="true">' +
             (c.active ? (c.dir > 0 ? '▲' : '▼') : '⇅') +
             '</span></th>';
      } else {
        h += '<th>' + esc(c) + '</th>';
      }
    });
    h += '</tr></thead><tbody>';
    if (!bodyRows.length) {
      h += '<tr><td colspan="' + head.length + '"><div class="cdd-empty">' +
           esc(opts.empty || 'Nothing to show for the selected filters') + '</div></td></tr>';
    } else {
      h += bodyRows.join('');
    }
    h += '</tbody></table></div>';
    return h;
  }

  // A plain row: first cell is the sticky label, the rest are values.
  function row(label, cells, cls) {
    var h = '<tr' + (cls ? ' class="' + cls + '"' : '') + '><td class="cond">' + esc(label) + '</td>';
    cells.forEach(function (c) { h += '<td>' + (c === '' ? '' : esc(c)) + '</td>'; });
    return h + '</tr>';
  }

  // A row that opens the breakdown panel for its employee. `noCheckin` marks
  // the rows whose panel should show only the visits missing a checkin, so the
  // detail matches the figure that was clicked.
  function empRow(r, cells, noCheckin) {
    var name = r.employee_name || r.employee;
    var h = '<tr class="cdd-emp-row" data-emp-id="' + esc(r.employee) + '"' +
            ' data-emp-name="' + esc(name) + '"' +
            (noCheckin ? ' data-no-checkin="1"' : '') +
            ' title="View visit breakdown">' +
            '<td class="cond">' + esc(name) + '</td>';
    cells.forEach(function (c) { h += '<td>' + esc(c) + '</td>'; });
    return h + '</tr>';
  }

  // The spend grid — employee x month, heat-shaded, every filled cell a click
  // target for the breakdown panel. Built here rather than inline in the visits
  // renderer because the year-only refetch swaps this one block back in, and it
  // must produce byte-identical markup when it does.
  var EMPCOST_GRID_ID = 'cdd-empcost-grid';

  // Sort state for the grid, kept outside the render so it survives the
  // year-only refetch swapping the block out. key is 'name', 'total', or a
  // month's column index; dir is 1 ascending / -1 descending. A null key means
  // the server's own order (heaviest spender first), which is the default.
  var gridSort = { key: null, dir: -1 };
  var gridData = null;

  function sortGridRows(rows, months) {
    if (gridSort.key === null) { return rows; }
    var key = gridSort.key;
    var dir = gridSort.dir;

    return rows.slice().sort(function (a, b) {
      if (key === 'name') {
        var an = (a.employee_name || a.employee || '').toLowerCase();
        var bn = (b.employee_name || b.employee || '').toLowerCase();
        return (an < bn ? -1 : an > bn ? 1 : 0) * dir;
      }
      var av = key === 'total' ? a.total : (a.cells || [])[Number(key)];
      var bv = key === 'total' ? b.total : (b.cells || [])[Number(key)];
      // A blank month is an absence of spend, so it sorts as zero either way.
      return ((av || 0) - (bv || 0)) * dir;
    });
  }

  function empCostGrid(grid) {
    grid = grid || { months: [], rows: [], col_totals: [] };
    gridData = grid;

    var maxCell = 0;
    (grid.rows || []).forEach(function (r) {
      r.cells.forEach(function (c) { if (c > maxCell) { maxCell = c; } });
    });

    var body = sortGridRows(grid.rows || [], grid.months).map(function (r) {
      var h = '<tr><td class="cond">' + esc(r.employee_name || r.employee) + '</td>';
      r.cells.forEach(function (c, colIdx) {
        h += '<td class="heat cdd-cost-cell"' +
             ' data-emp-id="' + esc(r.employee) + '"' +
             ' data-emp-name="' + esc(r.employee_name || r.employee) + '"' +
             ' data-month="' + esc(grid.months[colIdx] || '') + '"' +
             ' style="background:' + heatBg(c, maxCell) + ';color:' + heatFg(c, maxCell) +
             ';cursor:' + (c ? 'pointer' : 'default') + '">' + (c ? money(c) : '') + '</td>';
      });
      return h + '<td><b>' + money(r.total) + '</b></td></tr>';
    });

    if (body.length) {
      body.push(row('TOTAL',
        (grid.col_totals || []).map(money).concat([money(grid.grand_total)]), 'total-row'));
    }

    // Every column sorts; the active one carries the arrow.
    var head = [{ key: 'name', label: 'Employee' }]
      .concat((grid.months || []).map(function (m, i) {
        return { key: String(i), label: m };
      }))
      .concat([{ key: 'total', label: 'Total' }]);

    head.forEach(function (c) {
      c.active = gridSort.key === c.key;
      c.dir = gridSort.dir;
    });

    return tableBlock(inCurrency('Total Cost per Employee over Time'), head, body,
      { empty: 'No visits for the selected period' });
  }

  // Re-renders the grid in place on a header click. Horizontal scroll is
  // restored afterwards — with thirteen columns, snapping back to January on
  // every sort would be worse than not sorting at all.
  function wireGridSort() {
    var host = el(EMPCOST_GRID_ID);
    if (!host) { return; }

    Array.prototype.forEach.call(host.querySelectorAll('th.cdd-sort'), function (th) {
      function apply() {
        var key = th.getAttribute('data-sort');
        if (gridSort.key === key) {
          gridSort.dir = -gridSort.dir;
        } else {
          gridSort.key = key;
          // Names read best A-Z; money reads best heaviest-first.
          gridSort.dir = key === 'name' ? 1 : -1;
        }

        var wrap = host.querySelector('.cdd-table-wrap');
        var left = wrap ? wrap.scrollLeft : 0;
        var top = wrap ? wrap.scrollTop : 0;

        host.innerHTML = empCostGrid(gridData);

        var moved = host.querySelector('.cdd-table-wrap');
        if (moved) { moved.scrollLeft = left; moved.scrollTop = top; }

        wireCostCells();
        wireGridSort();
      }

      th.addEventListener('click', apply);
      th.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); apply(); }
      });
    });
  }

  function barChart(canvasId, cfg) {
    var ctx = el(canvasId);
    if (!ctx || !window.Chart) { return; }
    var horizontal = cfg.horizontal !== false;

    // cfg.series draws grouped bars (e.g. checked in vs checked out); a bare
    // cfg.values is the single-series case.
    var multi = cfg.series && cfg.series.length > 1;
    // The value axis never lets a tiny maximum fill the whole chart: a count
    // of 1 gets a 0–5 axis with unit ticks instead of one bar spanning it all.
    var peak = 0;
    (cfg.series ? cfg.series : [{ values: cfg.values || [] }]).forEach(function (sx) {
      (sx.values || []).forEach(function (v) { if (Number(v) > peak) { peak = Number(v); } });
    });
    var valueAxis = {
      beginAtZero: true, grid: { color: GRID },
      ticks: { precision: 0, color: AXIS_TEXT }
    };
    if (peak <= 5) {
      valueAxis.suggestedMax = 5;
      valueAxis.ticks.stepSize = 1;
    } else if (peak <= 10) {
      valueAxis.suggestedMax = 10;
      valueAxis.ticks.stepSize = 1;
    }

    var datasets = cfg.series
      ? cfg.series.map(function (sx, i) {
          return {
            label: sx.label,
            data: sx.values,
            backgroundColor: sx.color || slot(i),
            borderRadius: 4,
            borderSkipped: horizontal ? 'start' : 'bottom',
            maxBarThickness: 18
          };
        })
      : [{
          label: cfg.seriesLabel || 'Count',
          data: cfg.values,
          // cfg.colors paints each bar individually (states); otherwise the
          // whole single series takes one categorical slot.
          backgroundColor: cfg.colors || cfg.color || slot(0),
          // Round the data end only; the baseline end stays square.
          borderRadius: 4,
          borderSkipped: horizontal ? 'start' : 'bottom',
          maxBarThickness: 22
        }];

    charts.push(new Chart(ctx, {
      type: 'bar',
      data: { labels: cfg.labels, datasets: datasets },
      options: {
        indexAxis: horizontal ? 'y' : 'x',
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          // A legend only earns its place once there is more than one series;
          // for a single series the title already names it.
          legend: {
            display: !!multi,
            position: 'bottom',
            labels: { usePointStyle: true, padding: 16, color: INK, font: { size: 12 } }
          },
          title: {
            display: true, text: cfg.title, color: INK,
            font: { size: 14, weight: '700' }, padding: { bottom: 14 }
          },
          tooltip: {
            backgroundColor: INK, titleColor: '#ffffff', bodyColor: '#ffffff',
            padding: 10, cornerRadius: 8, displayColors: !!multi
          }
        },
        scales: {
          x: horizontal
            ? valueAxis
            : { grid: { display: false }, ticks: { color: AXIS_TEXT, maxRotation: 0, autoSkip: true } },
          y: horizontal
            ? { grid: { display: false }, ticks: { color: AXIS_TEXT } }
            : valueAxis
        }
      }
    }));
  }

  function lineChart(canvasId, cfg) {
    var ctx = el(canvasId);
    if (!ctx || !window.Chart) { return; }
    var series = cfg.series || [];
    if (!series.length) { return; }

    var datasets = series.map(function (s, i) {
      var stroke = s.color || slot(i);
      return {
        label: s.label,
        data: s.values,
        borderColor: stroke,
        backgroundColor: stroke,
        pointStyle: POINT_STYLES[i % POINT_STYLES.length],
        fill: false,
        tension: 0.35,
        pointRadius: 4,
        pointHoverRadius: 6,
        // Solid markers: no ring around the point.
        pointBackgroundColor: stroke,
        pointBorderColor: stroke,
        pointBorderWidth: 0,
        pointHoverBorderWidth: 0,
        borderWidth: 2
      };
    });

    charts.push(new Chart(ctx, {
      type: 'line',
      data: { labels: cfg.labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: {
            display: true, text: cfg.title, color: INK,
            font: { size: 14, weight: '700' }, padding: { bottom: 14 }
          },
          // A legend is only meaningful once there is more than one series.
          legend: {
            display: series.length > 1,
            position: 'bottom',
            labels: { usePointStyle: true, padding: 16, color: INK, font: { size: 12 } }
          },
          tooltip: {
            mode: 'index', intersect: false,
            backgroundColor: INK, titleColor: '#ffffff', bodyColor: '#ffffff',
            padding: 10, cornerRadius: 8
          }
        },
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: { grid: { display: false }, ticks: Object.assign({ color: AXIS_TEXT }, cfg.xTicks || {}) },
          y: { beginAtZero: true, grid: { color: GRID }, ticks: { precision: 0, color: AXIS_TEXT } }
        }
      }
    }));
    return charts[charts.length - 1];
  }

  // ── view descriptors ──────────────────────────────────────────────

  var VIEWS = {

    health: {
      title: 'Disease & Health Monthly Report',
      url: '/api/method/clinic_disease_report',
      drill: function (kind, label) { openHealthModal(kind, label); },
      filters: [],
      render: function (d) {
        var months = d.months || [];
        var rows = d.rows || [];
        var maxCell = 0, maxPct = 0;
        rows.forEach(function (r) {
          r.cells.forEach(function (c) { if (c > maxCell) { maxCell = c; } });
          if (r.percent > maxPct) { maxPct = r.percent; }
        });

        var body = rows.map(function (r) {
          var h = '<tr class="cdd-cond-row" data-condition="' + esc(r.condition) + '" tabindex="0" role="button">' +
                  '<td class="cond">' + esc(r.condition) + '</td>';
          r.cells.forEach(function (c) {
            h += '<td class="heat" style="background:' + heatBg(c, maxCell) +
                 ';color:' + heatFg(c, maxCell) + '">' + (c || '') + '</td>';
          });
          var w = maxPct ? Math.max(4, (r.percent / maxPct) * 48) : 0;
          h += '<td><b>' + r.total + '</b></td>';
          h += '<td><span class="pct-bar" style="width:' + w + 'px;"></span>' + r.percent + '%</td>';
          return h + '<td>' + r.average + '</td></tr>';
        });
        if (rows.length) {
          body.push(row('TOTAL',
            (d.col_totals || []).concat([d.grand_total || 0, '100%', d.monthly_average || 0]),
            'total-row'));
        }

        // drill values are the `kind` disease_report_detail lists.
        var S = d.kpi_series || {};
        return kpiBlock([
          { v: num(d.grand_total), l: 'Total Encounters', drill: 'total', series: S.total },
          { v: num(d.condition_count), l: 'Conditions Tracked', drill: 'conditions', series: S.conditions },
          { v: num(d.monthly_average), l: 'Monthly Average', drill: 'average', series: S.total },
          { v: months.length, l: 'Months Reported', drill: 'months', series: S.months_reported }
        ]) +
          chartRow([{ id: 'c-top', action: { label: 'Trend', action: 'trend' } }, 'c-trend']) +
          tableBlock('Conditions by Month',
            ['Condition'].concat(months).concat(['Total', 'Share', 'Avg']), body,
            { empty: 'No data for the selected filters' });
      },
      draw: function (d) {
        var top = (d.rows || []).slice(0, 8);
        barChart('c-top', {
          title: 'Top Conditions by Encounters',
          labels: top.map(function (r) { return r.condition; }),
          values: top.map(function (r) { return r.total; }),
          seriesLabel: 'Encounters'
        });
        var trend = d.trend || { months: [], years: [] };
        lineChart('c-trend', {
          title: 'Monthly Encounters by Year',
          labels: trend.months || [],
          series: (trend.years || []).map(function (y) { return { label: y.year, values: y.values }; })
        });
      },
      csv: function (d) {
        var out = [['Condition'].concat(d.months || []).concat(['Total', '%', 'Avg'])];
        (d.rows || []).forEach(function (r) {
          out.push([r.condition].concat(r.cells).concat([r.total, r.percent, r.average]));
        });
        out.push(['TOTAL'].concat(d.col_totals || []).concat([d.grand_total, 100, d.monthly_average]));
        return out;
      }
    },

    biometric: {
      title: 'Clinic Visits — Biometric',
      subtitle: 'Footfall from the biometric punch log (log type / time)',
      url: CHECKIN_URL,
      pick: function (res) { return res.biometric || {}; },
      filters: [],
      // Footfall is a daily question: the From/To range opens on today.
      dateDefault: 'today',
      drill: function (kind, label) { openPunchModal(kind, label); },
      render: function (d) {
        var k = d.kpis || {};
        var body = (d.top_employees || []).map(function (r) {
          return row(r.employee_name || r.employee,
            [r.payroll_number || '', r.in_punches, r.out_punches, r.visits,
             r.first_in || '', r.last_out || '']);
        });
        // drill values are the `kind` clinic_punch_people lists.
        var S = d.kpi_series || {};
        return kpiBlock([
          { v: num(k.punches), l: 'Total Punches', drill: '', series: S.punches },
          { v: num(k.employees), l: 'Unique Employees', drill: 'employees', series: S.employees },
          { v: num(k.in_punches), l: 'Checked In', drill: 'IN', series: S.in_punches },
          { v: num(k.out_punches), l: 'Checked Out', drill: 'OUT', series: S.out_punches },
          { v: num(k.days), l: 'Days with Visits', drill: 'days', series: S.days },
          { v: num(k.avg_per_day), l: 'Avg Punches / Day', series: S.avg_per_day }
        ]) +
          chartRow(['c-bio-month', 'c-bio-hour']) +
          chartRow(['c-bio-emp'], true) +
          tableBlock('Most Frequent Visitors',
            ['Employee', 'Payroll No.', 'In', 'Out', 'Total', 'First In', 'Last Out'], body,
            { empty: 'No biometric punches for the selected period' });
      },
      draw: function (d) {
        var m = d.by_month || { months: [], values: [] };
        lineChart('c-bio-month', {
          title: 'Clinic Punches by Month',
          labels: m.months, series: [{ label: 'Punches', values: m.values }]
        });

        // Arrivals and departures side by side answers "when is the clinic busy".
        var h = d.by_hour || { labels: [], in: [], out: [] };
        barChart('c-bio-hour', {
          title: 'Arrivals & Departures by Hour',
          labels: h.labels,
          horizontal: false,
          series: [
            { label: 'Checked In', values: h.in },
            { label: 'Checked Out', values: h.out }
          ]
        });

        // Same top employees as the table, as a chart, split by log type.
        var top = (d.top_employees || []).slice(0, 8);
        barChart('c-bio-emp', {
          title: 'Most Frequent Visitors — In vs Out',
          labels: top.map(function (r) { return r.employee_name || r.employee; }),
          series: [
            { label: 'Checked In', values: top.map(function (r) { return r.in_punches; }) },
            { label: 'Checked Out', values: top.map(function (r) { return r.out_punches; }) }
          ]
        });
      },
      csv: function (d) {
        var out = [['Employee', 'Payroll No.', 'In', 'Out', 'Total', 'First In', 'Last Out']];
        (d.top_employees || []).forEach(function (r) {
          out.push([r.employee_name || r.employee, r.payroll_number || '',
            r.in_punches, r.out_punches, r.visits, r.first_in || '', r.last_out || '']);
        });
        return out;
      }
    },

    sickoff: {
      title: 'Sick-Off & Leave',
      url: CHECKIN_URL,
      pick: function (res) { return res.sick_off || {}; },
      filters: [],
      drill: function (kind, label) { openSickOffModal(kind, label); },
      render: function (d) {
        var k = d.kpis || {};
        var body = (d.top_employees || []).map(function (r) {
          return row(r.employee_name || r.employee,
            [r.payroll_number || '', r.episodes, r.days]);
        });
        // drill values are the `kind` sick_off_people lists.
        var S = d.kpi_series || {};
        return kpiBlock([
          { v: num(k.records), l: 'Sick-Off Records', drill: '', series: S.records },
          { v: num(k.employees), l: 'Employees', drill: 'employees', series: S.employees },
          { v: num(k.days), l: 'Sick Days', drill: 'days', series: S.days },
          { v: num(k.with_leave), l: 'Leave Applications', drill: 'with_leave', series: S.with_leave },
          { v: num(k.without_leave), l: 'Missing Leave', drill: 'without_leave', series: S.without_leave },
          { v: num(k.avg_days), l: 'Avg Days / Episode', series: S.avg_days }
        ]) +
          chartRow(['c-so-month', 'c-so-dur']) +
          tableBlock('Most Sick Days',
            ['Employee', 'Payroll No.', 'Episodes', 'Sick Days'], body,
            { empty: 'No sick-off records for the selected period' });
      },
      draw: function (d) {
        var m = d.by_month || { months: [], values: [] };
        lineChart('c-so-month', {
          title: 'Sick-Off Records by Month',
          labels: m.months, series: [{ label: 'Records', values: m.values }]
        });
        var du = d.durations || { labels: [], values: [] };
        barChart('c-so-dur', {
          title: 'Sick-Off Length', labels: du.labels, values: du.values, seriesLabel: 'Records'
        });
      },
      csv: function (d) {
        var out = [['Employee', 'Payroll No.', 'Episodes', 'Sick Days']];
        (d.top_employees || []).forEach(function (r) {
          out.push([r.employee_name || r.employee, r.payroll_number || '', r.episodes, r.days]);
        });
        return out;
      }
    },

    visits: {
      title: 'Employee Visits & Cost',
      subtitle: 'What visits cost and what benefit cover is left',
      url: VISIT_URL,
      filters: [],
      render: function (d) {
        var k = d.kpis || {};
        var purposes = d.by_purpose || [];
        var maxCost = 0;
        purposes.forEach(function (p) { if (p.cost > maxCost) { maxCost = p.cost; } });

        // Each purpose row opens who that spend went to.
        var purposeBody = purposes.map(function (p) {
          var w = maxCost ? Math.max(4, (p.cost / maxCost) * 48) : 0;
          return '<tr class="cdd-purpose-row" data-purpose="' + esc(p.purpose) + '" tabindex="0" role="button">' +
                 '<td class="cond">' + esc(p.purpose) + '</td>' +
                 '<td>' + p.items + '</td>' +
                 '<td><b>' + cash(p.cost) + '</b></td>' +
                 '<td><span class="pct-bar" style="width:' + w + 'px;"></span>' + p.percent + '%</td></tr>';
        });

        // Same click target as a grid cell, minus the month: the panel then
        // covers whatever period the filters currently describe.
        var empCells = function (r) {
          return [r.payroll_number || '', r.visits, cash(r.cost), r.last_visit || ''];
        };
        var empBody = (d.top_employees || []).map(function (r) {
          return empRow(r, empCells(r));
        });
        var noCheckinBody = (d.no_checkin_costs || []).map(function (r) {
          return empRow(r, empCells(r), true);
        });

        // Cost per employee per month. This block is year-scoped on purpose —
        // the month and date filters narrow every other block but not this one,
        // so load() refetches it with the year alone and swaps it in below.
        var grid = d.cost_by_employee_full_year || d.cost_by_employee;

        var balanceTiles = (d.balances_latest || []).map(function (b) {
          return { v: cashShort(b.value), full: cash(b.value), l: b.label };
        });

        var S = d.kpi_series || {};
        return kpiBlock([
          { v: num(k.visits), l: 'Visits', series: S.visits },
          { v: num(k.employees), l: 'Employees Seen', series: S.employees },
          { v: cashShort(k.cost), full: cash(k.cost), l: 'Total Cost', series: S.cost },
          { v: cashShort(k.avg_cost), full: cash(k.avg_cost), l: 'Avg Cost / Visit', series: S.avg_cost },
          { v: cashShort(k.cost_per_employee), full: cash(k.cost_per_employee), l: 'Cost / Employee', series: S.cost_per_employee },
          { v: cashShort(k.balance_total), full: cash(k.balance_total), l: 'Benefit Left — All' }
        ]) +
          (balanceTiles.length
            ? '<div class="cdd-section-title">Benefit Balances</div>' + kpiBlock(balanceTiles)
            : '') +
          chartRow(['c-vs-month', 'c-vs-purpose']) +
          chartRow(['c-vs-balance'], true) +
          chartRow(['c-vs-empcost'], true) +
          '<div id="' + EMPCOST_GRID_ID + '">' + empCostGrid(grid) + '</div>' +
          tableBlock('Spend by Purpose', ['Purpose', 'Line Items', 'Cost', 'Share'], purposeBody,
            { empty: 'No visit line items for the selected period' }) +
          tableBlock('Costliest Employees',
            ['Employee', 'Payroll No.', 'Visits', 'Cost', 'Last Visit'], empBody,
            { empty: 'No visits for the selected period' }) +
          tableBlock('Employees With Cost but No Checkin on That Date',
            ['Employee', 'Payroll No.', 'Visits', 'Cost', 'Last Visit'], noCheckinBody,
            { empty: 'Every cost has a clinic checkin on its visit date' });
      },
      draw: function (d) {
        var m = d.by_month || { months: [], visits: [], cost: [] };
        // Visits and cost are different measures, so they get their own charts
        // rather than a second y-axis.
        lineChart('c-vs-month', {
          title: 'Visits by Month',
          labels: m.months, series: [{ label: 'Visits', values: m.visits }]
        });
        barChart('c-vs-purpose', {
          title: inCurrency('Spend by Purpose'),
          labels: (d.by_purpose || []).map(function (p) { return p.purpose; }),
          values: (d.by_purpose || []).map(function (p) { return p.cost; }),
          color: slot(1),
          seriesLabel: 'Cost'
        });
        var b = d.balances || { months: [], series: [] };
        lineChart('c-vs-balance', {
          title: inCurrency('Benefit Balance Remaining by Month'),
          labels: b.months, series: b.series
        });

        // Spend over time for the heaviest employees. Capped at the palette's
        // safe run; the grid below carries everyone.
        var grid = d.cost_by_employee || { months: [], rows: [] };
        var top = (grid.rows || []).slice(0, 5);
        if (top.length) {
          lineChart('c-vs-empcost', {
            title: inCurrency('Cost per Employee over Time') +
                   ((grid.rows || []).length > top.length
                     ? ' — top ' + top.length + ' of ' + grid.rows.length : ''),
            labels: grid.months,
            series: top.map(function (r) {
              return { label: r.employee_name || r.employee, values: r.cells };
            })
          });
        }
      },
      csv: function (d) {
        var grid = d.cost_by_employee || { months: [], rows: [], col_totals: [] };
        var out = [['Employee'].concat(grid.months || []).concat(['Total'])];
        (grid.rows || []).forEach(function (r) {
          out.push([r.employee_name || r.employee].concat(r.cells).concat([r.total]));
        });
        out.push(['TOTAL'].concat(grid.col_totals || []).concat([grid.grand_total]));
        out.push([]);
        out.push(['Benefit balances (latest)']);
        (d.balances_latest || []).forEach(function (b) { out.push([b.label, b.value]); });
        out.push([]);
        out.push(['Purpose', 'Line Items', 'Cost', 'Share %']);
        (d.by_purpose || []).forEach(function (p) {
          out.push([p.purpose, p.items, p.cost, p.percent]);
        });
        return out;
      }
    },

    requests: {
      title: 'Test Requests',
      url: REQUEST_URL,
      drill: function (kind, label) { openRequestsModal(kind, label); },
      filters: [
        { key: 'status', label: 'Status', src: 'statuses', all: 'All' },
        { key: 'test_package', label: 'Test Package', src: 'test_packages', all: 'All' },
        { key: 'member_type', label: 'Member Type', src: 'member_types', all: 'All' }
      ],
      render: function (d) {
        var k = d.kpis || {};
        var pkgs = d.by_package || [];
        var maxTotal = 0;
        pkgs.forEach(function (p) { if (p.total > maxTotal) { maxTotal = p.total; } });

        // Every filled count opens the requests behind it: a cell is one
        // package at one status, the row total is the package at any status.
        function reqCell(count, attrs, cls, style) {
          if (!count) { return '<td class="' + cls + '"' + (style || '') + '></td>'; }
          var a = '';
          Object.keys(attrs).forEach(function (k) {
            if (attrs[k]) { a += ' data-' + k + '="' + esc(String(attrs[k])) + '"'; }
          });
          return '<td class="' + cls + ' cdd-request-cell"' + a + (style || '') +
                 ' tabindex="0" role="button">' + count + '</td>';
        }
        var statuses = ['Pending', 'Completed', 'Cancelled'];
        var pkgBody = pkgs.map(function (p) {
          var pkg = p.test_package || 'Unspecified';
          var h = '<tr><td class="cond">' + esc(pkg) + '</td>';
          [p.pending, p.completed, p.cancelled].forEach(function (c, i) {
            h += reqCell(c, { pkg: pkg, status: statuses[i], label: statuses[i] + ' — ' + pkg }, 'heat',
              ' style="background:' + heatBg(c, maxTotal) + ';color:' + heatFg(c, maxTotal) + '"');
          });
          h += reqCell(p.total, { pkg: pkg, label: pkg }, 'cdd-result-total')
                 .replace('>' + p.total + '</td>', '><b>' + p.total + '</b></td>');
          return h + '<td>' + p.completion_rate + '%</td></tr>';
        });
        if (pkgBody.length) {
          var sum = function (k) { return pkgs.reduce(function (a, p) { return a + (p[k] || 0); }, 0); };
          var t = '<tr class="total-row"><td class="cond">TOTAL</td>';
          ['pending', 'completed', 'cancelled'].forEach(function (k, i) {
            t += reqCell(sum(k), { status: statuses[i], label: statuses[i] + ' — all packages' }, '');
          });
          var all = sum('total');
          t += reqCell(all, { label: 'All requests' }, '') +
               '<td>' + (all ? Math.round((sum('completed') * 1000) / all) / 10 : 0) + '%</td></tr>';
          pkgBody.push(t);
        }

        var overdue = (d.overdue_rows || []).map(function (r) {
          return row(r.who || r.name, [r.name, r.test_package || '', r.member_type || '',
            r.scheduled_to || '', r.days_late]);
        });

        // drill values are what test_request_people filters on: a status,
        // "overdue", or "" for every request.
        var S = d.kpi_series || {};
        return kpiBlock([
          { v: num(k.total), l: 'Total Requests', drill: '', series: S.total },
          { v: num(k.pending), l: 'Pending', drill: 'Pending', series: S.pending },
          { v: num(k.completed), l: 'Completed', drill: 'Completed', series: S.completed },
          { v: num(k.cancelled), l: 'Cancelled', drill: 'Cancelled', series: S.cancelled },
          { v: num(k.overdue), l: 'Overdue', drill: 'overdue', series: S.overdue }
        ]) +
          chartRow(['c-rq-month', 'c-rq-pkg']) +
          tableBlock('Packages by Status',
            ['Test Package', 'Pending', 'Completed', 'Cancelled', 'Total', 'Completion'], pkgBody,
            { empty: 'No requests for the selected filters' }) +
          tableBlock('Overdue Requests',
            ['Candidate', 'Request', 'Package', 'Member Type', 'Scheduled To', 'Days Late'], overdue,
            { empty: 'Nothing overdue — every pending request is still inside its window' });
      },
      draw: function (d) {
        var m = d.by_month || { months: [], values: [] };
        lineChart('c-rq-month', {
          title: 'Requests by Scheduled Month',
          labels: m.months, series: [{ label: 'Requests', values: m.values }]
        });
        var pkgs = d.by_package || [];
        barChart('c-rq-pkg', {
          title: 'Requests by Package',
          labels: pkgs.map(function (p) { return p.test_package || 'Unspecified'; }),
          values: pkgs.map(function (p) { return p.total; }),
          seriesLabel: 'Requests'
        });
      },
      csv: function (d) {
        var out = [['Test Package', 'Pending', 'Completed', 'Cancelled', 'Total', 'Completion %']];
        (d.by_package || []).forEach(function (p) {
          out.push([p.test_package, p.pending, p.completed, p.cancelled, p.total, p.completion_rate]);
        });
        return out;
      }
    },

    results: {
      title: 'Test Results',
      url: RESULT_URL,
      filters: [
        { key: 'test_package', label: 'Test Package', src: 'test_packages', all: 'All' },
        { key: 'clinical_outcome', label: 'Outcome', src: 'clinical_outcomes', all: 'All' },
        { key: 'member_type', label: 'Member Type', src: 'member_types', all: 'All' }
      ],
      render: function (d) {
        var k = d.kpis || {};
        var risk = d.risk || { levels: [], rows: [], totals: [] };
        var maxCell = 0;
        (risk.rows || []).forEach(function (r) {
          r.cells.forEach(function (c) { if (c > maxCell) { maxCell = c; } });
        });

        // Every filled count opens the list behind it: a cell is one
        // condition at one grade, a row total is the condition at any grade,
        // a TOTAL-row cell is the grade across conditions.
        function drillCell(count, attrs, cls, style) {
          if (!count) { return '<td class="' + cls + '"' + (style || '') + '></td>'; }
          var a = '';
          Object.keys(attrs).forEach(function (k) {
            if (attrs[k] !== undefined && attrs[k] !== null) {
              a += ' data-' + k + '="' + esc(String(attrs[k])) + '"';
            }
          });
          return '<td class="' + cls + ' cdd-result-cell"' + a + (style || '') +
                 ' tabindex="0" role="button">' + count + '</td>';
        }

        var levels = risk.levels || [];
        var riskBody = (risk.rows || []).map(function (r) {
          var h = '<tr><td class="cond">' + esc(r.medical_case) + '</td>';
          r.cells.forEach(function (c, i) {
            h += drillCell(c, { 'case': r.medical_case, risk: levels[i],
              label: levels[i] + ' — ' + r.medical_case }, 'heat',
              ' style="background:' + heatBg(c, maxCell) + ';color:' + heatFg(c, maxCell) + '"');
          });
          return h + drillCell(r.total, { 'case': r.medical_case, label: r.medical_case },
            'cdd-result-total').replace('>' + r.total + '</td>', '><b>' + r.total + '</b></td>') + '</tr>';
        });
        if (riskBody.length) {
          var grand = (risk.totals || []).reduce(function (a, b) { return a + b; }, 0);
          var t = '<tr class="total-row"><td class="cond">TOTAL</td>';
          (risk.totals || []).forEach(function (c, i) {
            t += drillCell(c, { risk: levels[i], label: levels[i] + ' — all conditions' }, '');
          });
          t += drillCell(grand, { label: 'All graded results' }, '') + '</tr>';
          riskBody.push(t);
        }

        var pkgBody = (d.by_package || []).map(function (p) {
          return '<tr class="cdd-pkg-row" data-package="' + esc(p.test_package) + '"' +
                 ' data-label="' + esc(p.test_package) + '" tabindex="0" role="button">' +
                 '<td class="cond">' + esc(p.test_package) + '</td><td>' + esc(p.cnt) + '</td></tr>';
        });

        // drill values are what the endpoint filters on: a clinical outcome,
        // "employees" for the distinct-employee tile, or "" for every result.
        var S = d.kpi_series || {};
        return kpiBlock([
          { v: num(k.total), l: 'Results Received', drill: '', series: S.total },
          { v: num(k.employees), l: 'Employees', drill: 'employees', series: S.employees },
          { v: num(k.fit), l: 'Fit for Work', drill: 'FitForWork', series: S.fit },
          { v: num(k.restricted), l: 'Fit with Restrictions', drill: 'FitWithRestrictions', series: S.restricted },
          { v: num(k.unfit), l: 'Unfit for Work', drill: 'UnfitForWork', series: S.unfit },
          { v: num(k.retest), l: 'Retest Required', drill: 'InconclusiveRetestRequired', series: S.retest }
        ]) +
          chartRow(['c-rs-outcome', 'c-rs-month']) +
          tableBlock('Condition by Risk Grade',
            ['Medical Case'].concat(risk.levels || []).concat(['Total']), riskBody,
            { empty: 'No graded results for the selected filters' }) +
          tableBlock('Results by Package', ['Test Package', 'Results'], pkgBody,
            { empty: 'No results for the selected filters' });
      },
      draw: function (d) {
        var out = d.by_outcome || [];
        barChart('c-rs-outcome', {
          title: 'Clinical Outcomes',
          labels: out.map(function (o) { return o.clinical_outcome; }),
          values: out.map(function (o) { return o.cnt; }),
          // States, not series: fit / restricted / retest / unfit take the
          // reserved status steps, each beside its axis label.
          colors: out.map(function (o) { return OUTCOME_COLORS[o.clinical_outcome] || slot(0); }),
          seriesLabel: 'Results'
        });
        var m = d.by_month || { months: [], values: [] };
        lineChart('c-rs-month', {
          title: 'Results by Month',
          labels: m.months, series: [{ label: 'Results', values: m.values }]
        });
      },
      csv: function (d) {
        var risk = d.risk || { levels: [], rows: [] };
        var out = [['Medical Case'].concat(risk.levels).concat(['Total'])];
        (risk.rows || []).forEach(function (r) {
          out.push([r.medical_case].concat(r.cells).concat([r.total]));
        });
        return out;
      }
    }
  };

  // ── load / render cycle ───────────────────────────────────────────

  function filterValues(view) {
    var v = VIEWS[view];
    var body = {};

    // Add global filter values
    GLOBAL_FILTERS.forEach(function (f) {
      if (globalValues[f.key]) { body[f.key] = globalValues[f.key]; }
    });

    // Add view-specific filter values
    v.filters.forEach(function (f) {
      var sel = el('f-' + f.key);
      if (!sel) { return; }
      // A link field's control shows a label; the value the API wants is the id.
      var value = (f.type === 'link') ? (sel.dataset.value || '') : sel.value;
      if (value) { body[f.key] = value; }
    });
    return body;
  }

  function buildFilters(view, options) {
    var v = VIEWS[view];
    var wrap = el('cdd-filters');
    var h = '';

    // Build global filters only once, but merge options on each load
    if (!Object.keys(globalFilterOptions).length) {
      globalFilterOptions = options || {};
      // Seed global filter defaults
      GLOBAL_FILTERS.forEach(function (f) {
        if (f.key === 'year') { globalValues[f.key] = String(new Date().getFullYear()); }
      });
    } else {
      // Merge view-specific options (like employees for visits view)
      globalFilterOptions = Object.assign({}, globalFilterOptions, options || {});
    }

    // Build view-specific filter defaults and store options
    if (!v.values) {
      v.values = {};
      v.filters.forEach(function (f) {
        if (f.default === 'today') { v.values[f.key] = toIso(new Date()); }
      });
      v.seededDefaults = Object.keys(v.values).length > 0;
    }
    // Store view-specific filter options
    v.filterOptions = options || {};
    // Render global filters
    var globalFilterHtml = '';
    GLOBAL_FILTERS.forEach(function (f) {
      globalFilterHtml += '<div class="cdd-field"><label>' + esc(f.label) + '</label>';
      if (f.type === 'date') {
        globalFilterHtml += '<div class="cdd-datefield">' +
             '<input type="text" id="f-' + f.key + '" class="cdd-dateinput cdd-global-filter" readonly ' +
             'autocomplete="off" placeholder="Select date">' +
             '<span class="cdd-datefield-icon" aria-hidden="true">' +
             '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
             'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' +
             '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>' +
             '</svg></span></div>';
      } else {
        globalFilterHtml += '<select id="f-' + f.key + '" class="cdd-global-filter"><option value="">' + esc(f.all) + '</option>';
        ((globalFilterOptions || {})[f.src] || []).forEach(function (o) {
          globalFilterHtml += '<option value="' + esc(o) + '">' + esc(o) + '</option>';
        });
        globalFilterHtml += '</select>';
      }
      globalFilterHtml += '</div>';
    });

    // Render view-specific filters
    var viewFilterHtml = '';
    v.filters.forEach(function (f) {
      viewFilterHtml += '<div class="cdd-field"><label>' + esc(f.label) + '</label>';
      if (f.type === 'date') {
        viewFilterHtml += '<div class="cdd-datefield">' +
             '<input type="text" id="f-' + f.key + '" class="cdd-dateinput" readonly ' +
             'autocomplete="off" placeholder="Select date">' +
             '<span class="cdd-datefield-icon" aria-hidden="true">' +
             '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
             'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' +
             '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>' +
             '</svg></span></div>';
      } else if (f.type === 'link') {
        viewFilterHtml += '<div class="cdd-linkfield">' +
             '<input type="text" id="f-' + f.key + '" class="cdd-linkinput" ' +
             'autocomplete="off" placeholder="' + esc(f.all) + '">' +
             '<button type="button" class="cdd-linkclear" aria-label="Clear">&times;</button>' +
             '</div>';
      } else {
        viewFilterHtml += '<select id="f-' + f.key + '"><option value="">' + esc(f.all) + '</option>';
        ((globalFilterOptions || {})[f.src] || []).forEach(function (o) {
          viewFilterHtml += '<option value="' + esc(o) + '">' + esc(o) + '</option>';
        });
        viewFilterHtml += '</select>';
      }
      viewFilterHtml += '</div>';
    });

    // An icon-only reset; the label lives in the tooltip and for screen readers.
    h = globalFilterHtml + viewFilterHtml +
        '<button class="cdd-clear" id="cdd-clear" type="button" title="Clear filters" aria-label="Clear filters">' +
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M6 6l12 12M18 6L6 18"/>' +
        '</svg></button>';
    wrap.innerHTML = h;

    // Smart date-month sync: if month is set, auto-fill from/to dates; if dates
    // are manually set, clear the month filter.
    function syncDateMonth() {
      var yearSel = el('f-year');
      var monthSel = el('f-month');
      var fromSel = el('f-from_date');
      var toSel = el('f-to_date');

      if (!yearSel || !monthSel || !fromSel || !toSel) { return; }

      var year = yearSel.value;
      var month = monthSel.value;
      var fromVal = fromSel.value;
      var toVal = toSel.value;

      // If month is selected, auto-fill from/to dates for that month
      if (month && year) {
        var monthNum = new Date(month + ' 1').getMonth();
        var yearNum = parseInt(year);
        var firstDay = new Date(yearNum, monthNum, 1);
        var lastDay = new Date(yearNum, monthNum + 1, 0);
        fromSel.value = toIso(firstDay);
        toSel.value = toIso(lastDay);
        globalValues['from_date'] = toIso(firstDay);
        globalValues['to_date'] = toIso(lastDay);
      }
      // If from/to dates are manually set, clear the month filter
      else if (fromVal || toVal) {
        monthSel.value = '';
        globalValues['month'] = '';
      }
    }

    // Bind global filter listeners
    GLOBAL_FILTERS.forEach(function (f) {
      var sel = el('f-' + f.key);
      if (!sel) { return; }
      // Restore previous global filter values
      if (globalValues[f.key]) {
        sel.value = globalValues[f.key];
      }
      if (f.type === 'date') { attachCalendar(sel, view, f); }
      sel.addEventListener('change', function () {
        globalValues[f.key] = sel.value;
        syncDateMonth();
        load(view);
      });
    });

    // Bind view-specific filter listeners
    v.filters.forEach(function (f) {
      var sel = el('f-' + f.key);
      if (!sel) { return; }
      // Restore previous view-specific filter values
      if (v.values && v.values[f.key]) {
        if (f.type === 'link') {
          sel.dataset.value = v.values[f.key];
          sel.value = (v.labels || {})[f.key] || v.values[f.key];
        } else {
          sel.value = v.values[f.key];
        }
      }
      if (f.type === 'date') { attachCalendar(sel, view, f); }
      if (f.type === 'link') {
        var linkOptions = (v.filterOptions || {})[f.src] || (globalFilterOptions || {})[f.src] || [];
        attachLink(sel, view, f, linkOptions);
      }
      sel.addEventListener('change', function () {
        v.values = v.values || {};
        v.values[f.key] = sel.value;
        load(view);
      });
    });
    el('cdd-clear').addEventListener('click', function () {
      // Clear global filters (except year)
      globalValues = { year: String(new Date().getFullYear()) };
      globalLabels = {};
      GLOBAL_FILTERS.forEach(function (f) {
        var sel = el('f-' + f.key);
        if (!sel) { return; }
        if (f.key === 'year') {
          sel.value = String(new Date().getFullYear());
        } else {
          sel.value = '';
        }
      });

      // Clear view-specific filters
      v.values = {};
      v.labels = {};
      v.filters.forEach(function (f) {
        var sel = el('f-' + f.key);
        if (!sel) { return; }
        sel.value = '';
        if (f.type === 'link') { sel.dataset.value = ''; }
      });
      load(view);
    });
  }

  function describe(view) {
    var v = VIEWS[view];
    var parts = [];
    v.filters.forEach(function (f) {
      var sel = el('f-' + f.key);
      if (sel && sel.value) { parts.push(sel.value); }
    });
    el('cdd-title').textContent = v.title;
  }

  function load(view) {
    var v = VIEWS[view];
    el('cdd-loading').style.display = 'block';

    post(v.url, filterValues(view)).then(function (res) {
      if (current !== view) { return; }   // the user switched away mid-flight
      v.raw = res;
      v.data = v.pick ? v.pick(res) : res;
      if (res.currency) { CURRENCY = res.currency; }

      // First response also carries the filter option lists. Rebuild the bar,
      // preselect the most recent year, and re-fetch once with it.
      // The first response also carries the filter option lists, so the bar is
      // built from it. If that build seeded defaults (the From/To range starts
      // at today), fetch once more so the view matches what the bar now shows.
      if (!v.optionsLoaded) {
        v.optionsLoaded = true;
        buildFilters(view, res.filter_options || {});
        if (v.seededDefaults) {
          v.seededDefaults = false;
          load(view);
          return;
        }
      }

      destroyCharts();
      el('cdd-view').innerHTML = v.render(v.data);
      describe(view);

      // The spend grid ignores the month and date filters by design, so it is
      // fetched again with the year alone and swapped into its own container —
      // only that block, or the KPIs and charts around it go with it.
      if (current === 'visits' && v.data.cost_by_employee) {
        post(VISIT_URL, { year: globalValues.year }).then(function (full) {
          if (current !== view || !full || !full.cost_by_employee) { return; }
          v.data.cost_by_employee_full_year = full.cost_by_employee;
          var host = el(EMPCOST_GRID_ID);
          if (host) {
            host.innerHTML = empCostGrid(full.cost_by_employee);
            wireCostCells();
            wireGridSort();
          }
        }).catch(function (err) {
          console.error('Could not load full-year grid:', err);
        });
      }

      wireCostCells();
      wireGridSort();
      wireKpiDrill();
      wireResultDrill();
      wireChartActions();

      el('cdd-loading').style.display = 'none';
      ensureChartJs(function () {
        if (current === view) { v.draw(v.data); }
      });
    }).catch(function (err) {
      if (current !== view) { return; }
      el('cdd-loading').style.display = 'none';
      el('cdd-view').innerHTML = '<div class="cdd-empty">Could not load this dashboard: ' +
        esc(err && err.message ? err.message : err) + '</div>';
    });
  }

  function activate(view) {
    if (!VIEWS[view]) { return; }
    current = view;
    Array.prototype.forEach.call(app.querySelectorAll('.cdd-nav-item'), function (b) {
      b.classList.toggle('is-active', b.getAttribute('data-view') === view);
    });
    destroyCharts();
    resetScroll();
    el('cdd-view').innerHTML = '';
    var v = VIEWS[view];
    // A From/To range belongs to the tab it was picked on: every tab opens
    // with the range cleared. Year and month carry across.
    globalValues.from_date = '';
    globalValues.to_date = '';
    // A view can open on today's date, unless a month narrows it already.
    // Clear Filters still widens back to the whole year.
    if (v.dateDefault === 'today' && !globalValues.month) {
      var today = toIso(new Date());
      globalValues.from_date = today;
      globalValues.to_date = today;
    }
    if (v.optionsLoaded) {
      buildFilters(view, (v.raw || {}).filter_options || {});
    } else {
      el('cdd-filters').innerHTML = '';
    }
    load(view);
  }

  // The rows are built here (already filtered, exactly what is on screen) and
  // POSTed to the server, which returns a real .xlsx. A form submit is used
  // rather than fetch() so the browser handles it as a native download.
  function exportXlsx() {
    var v = VIEWS[current];
    if (!v || !v.data) { return; }
    var rows = v.csv(v.data) || [];
    if (!rows.length) { return; }

    var form = document.createElement('form');
    form.method = 'POST';
    form.action = '/api/method/cova_clinic_integration.api.clinic_report_xlsx';
    form.style.display = 'none';

    function field(name, value) {
      var input = document.createElement('input');
      input.type = 'hidden';
      input.name = name;
      input.value = value;
      form.appendChild(input);
    }

    field('rows', JSON.stringify(rows));
    field('title', v.title);
    field('cmd', 'cova_clinic_integration.api.clinic_report_xlsx');
    var token = csrf();
    if (token) { field('csrf_token', token); }

    document.body.appendChild(form);
    form.submit();
    document.body.removeChild(form);
  }

  // ── employee modal ────────────────────────────────────────────────
  var empModal = el('cdd-emp-modal');
  var empModalBody = el('cdd-emp-modal-body');
  var empModalTitle = el('cdd-emp-modal-title');
  var empModalClose = el('cdd-emp-modal-close');

  function closeEmpModal() {
    empModal.classList.remove('is-open');
  }

  // Both breakdown entry points: a filled cell in the spend grid (scoped to
  // that one month) and a row of Costliest Employees (scoped to whatever the
  // filters currently describe). Called again after either table is re-rendered,
  // since that drops the listeners along with the old nodes.
  function wireCostCells() {
    var host = el('cdd-view');
    if (!host) { return; }

    function openFrom(node, month, opts) {
      var empId = node.getAttribute('data-emp-id');
      var empName = node.getAttribute('data-emp-name');
      if (empId && empName) { openEmpModal(empId, empName, month, opts); }
    }

    Array.prototype.forEach.call(host.querySelectorAll('.cdd-cost-cell'), function (cell) {
      if (!cell.textContent.trim()) { return; }
      cell.addEventListener('click', function (e) {
        // Otherwise this same click reaches the close-on-outside handler.
        e.stopPropagation();
        openFrom(cell, cell.getAttribute('data-month'));
      });
    });

    Array.prototype.forEach.call(host.querySelectorAll('.cdd-emp-row'), function (tr) {
      tr.addEventListener('click', function (e) {
        e.stopPropagation();
        // These tables honour the month filter, so the panel must too.
        openFrom(tr, globalValues.month || null,
          { noCheckin: tr.getAttribute('data-no-checkin') === '1' });
      });
    });
  }

  // The people behind one Test Results tile, in the same right-hand panel.
  // `extra` narrows further for the two tables: {medical_case, risk} for a
  // cell of the risk grid, {test_package} for a Results by Package row.
  function openResultsModal(outcome, label, extra) {
    empModalTitle.textContent = label;
    empModalBody.innerHTML = '<div class="cdd-modal-empty">Loading&hellip;</div>';
    empModal.classList.add('is-open');

    // The same filter set the tile was computed from, or the list would not
    // match the number that was clicked.
    var filters = filterValues('results');
    filters.outcome = outcome || '';
    Object.keys(extra || {}).forEach(function (k) {
      if (extra[k]) { filters[k] = extra[k]; }
    });

    post('/api/method/cova_clinic_integration.api.test_result_people', filters)
      .then(function (res) {
        var rows = (res && res.rows) || [];
        if (!rows.length) {
          empModalBody.innerHTML = '<div class="cdd-modal-empty">Nothing to show.</div>';
          return;
        }

        var html = '<div class="cdd-visit-total">' +
                   '<span>' + rows.length + (rows.length === 1 ? ' record' : ' records') + '</span>' +
                   '</div>';

        rows.forEach(function (r) {
          var name = r.employee_name || r.employee || '—';
          // Grouped rows are one per person and have no single record to open.
          var head = r.route
            ? '<a class="cdd-visit-date" href="' + esc(r.route) + '" target="_blank" rel="noopener"' +
              ' title="Open ' + esc(r.name) + '">' + esc(name) +
              '<span class="cdd-visit-open" aria-hidden="true">&#8599;</span></a>'
            : '<span class="cdd-visit-date">' + esc(name) + '</span>';

          var meta = [];
          if (r.payroll_number) {
            // A pre-employment candidate has no Employee record yet, so the
            // number shown is their National ID — say which it is.
            meta.push(esc(r.payroll_number) +
              (r.member_type === 'Pre Employment' ? ' (ID)' : ''));
          }
          if (r.test_package) { meta.push(esc(r.test_package)); }
          if (res.grouped && r.results) {
            meta.push(r.results + (r.results === 1 ? ' result' : ' results'));
          } else if (r.clinical_outcome) {
            meta.push(esc(r.clinical_outcome));
          }
          if (r.risk_grade) { meta.push(esc(r.risk_grade)); }

          // The date wears the outcome's status colour, the same one the
          // Clinical Outcomes chart uses, so a scan down the panel reads the
          // same way as the chart. Never colour alone: the outcome is spelled
          // out in the meta line directly beneath it.
          var tone = OUTCOME_COLORS[r.clinical_outcome];
          var dateStyle = tone ? ' style="color:' + tone + '"' : '';

          html += '<div class="cdd-visit-item">' +
                  '<div class="cdd-visit-head">' + head +
                    '<span class="cdd-visit-cost"' + dateStyle + '>' +
                      esc(r.received || '') + '</span>' +
                  '</div>' +
                  (meta.length ? '<div class="cdd-line-note">' + meta.join(' · ') + '</div>' : '') +
                  '</div>';
        });

        empModalBody.innerHTML = html;
      })
      .catch(function (err) {
        console.error('Failed to load result list:', err);
        empModalBody.innerHTML = '<div class="cdd-modal-empty">Could not load the list.</div>';
      });
  }

  // The two Test Results tables: a count in the risk grid, or a package row.
  function wireResultDrill() {
    var host = el('cdd-view');
    if (!host) { return; }

    function bind(node, open) {
      node.addEventListener('click', function (e) { e.stopPropagation(); open(); });
      node.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); open(); }
      });
    }

    Array.prototype.forEach.call(host.querySelectorAll('.cdd-result-cell'), function (cell) {
      bind(cell, function () {
        openResultsModal('', cell.getAttribute('data-label'), {
          medical_case: cell.getAttribute('data-case'),
          risk: cell.getAttribute('data-risk')
        });
      });
    });

    Array.prototype.forEach.call(host.querySelectorAll('.cdd-pkg-row'), function (tr) {
      bind(tr, function () {
        openResultsModal('', tr.getAttribute('data-label'), {
          test_package: tr.getAttribute('data-package')
        });
      });
    });

    Array.prototype.forEach.call(host.querySelectorAll('.cdd-cond-row'), function (tr) {
      bind(tr, function () { openConditionTrend(tr.getAttribute('data-condition')); });
    });

    Array.prototype.forEach.call(host.querySelectorAll('.cdd-purpose-row'), function (tr) {
      bind(tr, function () { openPurposeModal(tr.getAttribute('data-purpose')); });
    });

    Array.prototype.forEach.call(host.querySelectorAll('.cdd-request-cell'), function (cell) {
      bind(cell, function () {
        openRequestsModal(cell.getAttribute('data-status') || '', cell.getAttribute('data-label'), {
          test_package: cell.getAttribute('data-pkg')
        });
      });
    });
  }

  // Who the spend on one purpose went to, with each person's total.
  function openPurposeModal(purpose) {
    empModalTitle.textContent = purpose + ' \u2014 spend by employee';
    empModalBody.innerHTML = '<div class="cdd-modal-empty">Loading&hellip;</div>';
    empModal.classList.add('is-open');

    var filters = filterValues('visits');
    filters.purpose = purpose;

    post('/api/method/cova_clinic_integration.api.purpose_spend_people', filters)
      .then(function (res) {
        var rows = (res && res.rows) || [];
        if (!rows.length) {
          empModalBody.innerHTML = '<div class="cdd-modal-empty">Nothing to show.</div>';
          return;
        }
        var html = '<div class="cdd-visit-total">' +
                   '<span>' + rows.length + (rows.length === 1 ? ' employee' : ' employees') + '</span>' +
                   '<span>' + esc(cash(res.total)) + '</span></div>';

        rows.forEach(function (r) {
          var name = r.employee_name || r.employee || '\u2014';
          var head = r.route
            ? '<a class="cdd-visit-date" href="' + esc(r.route) + '" target="_blank" rel="noopener"' +
              ' title="Open ' + esc(r.employee) + '">' + esc(name) +
              '<span class="cdd-visit-open" aria-hidden="true">&#8599;</span></a>'
            : '<span class="cdd-visit-date">' + esc(name) + '</span>';
          var meta = [];
          if (r.payroll_number) { meta.push(esc(r.payroll_number)); }
          meta.push(r.items + (r.items === 1 ? ' item' : ' items') + ' \u00b7 ' +
                    r.visits + (r.visits === 1 ? ' visit' : ' visits'));
          if (r.last_visit) { meta.push('Last ' + esc(r.last_visit)); }

          html += '<div class="cdd-visit-item">' +
                  '<div class="cdd-visit-head">' + head +
                    '<span class="cdd-visit-cost">' + esc(cash(r.cost)) + '</span>' +
                  '</div>' +
                  '<div class="cdd-line-note">' + meta.join(' \u00b7 ') + '</div>' +
                  '</div>';
        });
        empModalBody.innerHTML = html;
      })
      .catch(function (err) {
        console.error('Failed to load purpose spend:', err);
        empModalBody.innerHTML = '<div class="cdd-modal-empty">Could not load the list.</div>';
      });
  }

  // The sick-off records behind one Sick-Off & Leave tile: who was off, for
  // which days, how many, and the leave that booked it.
  function openSickOffModal(kind, label) {
    empModalTitle.textContent = label;
    empModalBody.innerHTML = '<div class="cdd-modal-empty">Loading&hellip;</div>';
    empModal.classList.add('is-open');

    var filters = filterValues('sickoff');
    // Sick Days is the sum over the same records as Sick-Off Records.
    filters.kind = kind === 'days' ? '' : (kind || '');

    post('/api/method/cova_clinic_integration.api.sick_off_people', filters)
      .then(function (res) {
        var rows = (res && res.rows) || [];
        if (!rows.length) {
          empModalBody.innerHTML = '<div class="cdd-modal-empty">Nothing to show.</div>';
          return;
        }
        var totalDays = rows.reduce(function (a, r) { return a + (r.days || 0); }, 0);
        var summary = res.grouped
          ? rows.length + (rows.length === 1 ? ' employee' : ' employees') + ' \u00b7 ' + totalDays + ' sick days'
          : rows.length + (rows.length === 1 ? ' record' : ' records') + ' \u00b7 ' + totalDays + ' sick days';
        var html = '<div class="cdd-visit-total"><span>' + esc(summary) + '</span></div>';

        function dayLabel(n) { return n + (n === 1 ? ' day' : ' days'); }

        rows.forEach(function (r) {
          var name = r.employee_name || r.employee || '\u2014';
          var head = r.route
            ? '<a class="cdd-visit-date" href="' + esc(r.route) + '" target="_blank" rel="noopener"' +
              ' title="Open ' + esc(r.name) + '">' + esc(name) +
              '<span class="cdd-visit-open" aria-hidden="true">&#8599;</span></a>'
            : '<span class="cdd-visit-date">' + esc(name) + '</span>';

          var meta = [];
          if (r.payroll_number) { meta.push(esc(r.payroll_number)); }
          if (res.grouped) {
            meta.push(r.episodes + (r.episodes === 1 ? ' episode' : ' episodes'));
            if (r.first_off) {
              meta.push(esc(r.first_off) + (r.last_off && r.last_off !== r.first_off ? ' \u2192 ' + esc(r.last_off) : ''));
            }
            meta.push(r.with_leave + ' with leave');
          } else {
            meta.push(esc(r.start_date) + (r.end_date && r.end_date !== r.start_date ? ' \u2192 ' + esc(r.end_date) : ''));
            if (r.reason) { meta.push(esc(r.reason)); }
            meta.push(r.leave_route
              ? '<a href="' + esc(r.leave_route) + '" target="_blank" rel="noopener">' + esc(r.leave_application) + '</a>'
              : '<span style="color:' + slot(7) + '">no leave application</span>');
          }

          html += '<div class="cdd-visit-item">' +
                  '<div class="cdd-visit-head">' + head +
                    '<span class="cdd-visit-cost">' + dayLabel(r.days || 0) + '</span>' +
                  '</div>' +
                  '<div class="cdd-line-note">' + meta.join(' \u00b7 ') + '</div>' +
                  '</div>';
        });
        empModalBody.innerHTML = html;
      })
      .catch(function (err) {
        console.error('Failed to load sick-off list:', err);
        empModalBody.innerHTML = '<div class="cdd-modal-empty">Could not load the list.</div>';
      });
  }

  // The requests behind one Test Requests tile or Packages by Status cell.
  var STATUS_COLORS = { Pending: slot(3), Completed: slot(2), Cancelled: slot(7) };

  function openRequestsModal(kind, label, extra) {
    empModalTitle.textContent = label;
    empModalBody.innerHTML = '<div class="cdd-modal-empty">Loading&hellip;</div>';
    empModal.classList.add('is-open');

    var filters = filterValues('requests');
    filters.kind = kind || '';
    // A grid cell names its package and status outright; those must win over
    // the bar's own Status / Package selects, or the list would not match.
    Object.keys(extra || {}).forEach(function (k) {
      if (extra[k]) { filters[k] = extra[k]; }
    });
    if (kind && kind !== 'overdue') { filters.status = kind; }

    post('/api/method/cova_clinic_integration.api.test_request_people', filters)
      .then(function (res) {
        var rows = (res && res.rows) || [];
        if (!rows.length) {
          empModalBody.innerHTML = '<div class="cdd-modal-empty">Nothing to show.</div>';
          return;
        }
        var html = '<div class="cdd-visit-total"><span>' + rows.length +
                   (rows.length === 1 ? ' request' : ' requests') + '</span></div>';

        rows.forEach(function (r) {
          var name = r.who || r.name;
          var head = '<a class="cdd-visit-date" href="' + esc(r.route) + '" target="_blank" rel="noopener"' +
                     ' title="Open ' + esc(r.name) + '">' + esc(name) +
                     '<span class="cdd-visit-open" aria-hidden="true">&#8599;</span></a>';

          var meta = [esc(r.name)];
          if (r.payroll_number) {
            meta.push(esc(r.payroll_number) + (r.member_type === 'Pre Employment' ? ' (ID)' : ''));
          }
          if (r.test_package) { meta.push(esc(r.test_package)); }
          if (r.scheduled_from) {
            meta.push(esc(r.scheduled_from) +
              (r.scheduled_to && r.scheduled_to !== r.scheduled_from ? ' \u2192 ' + esc(r.scheduled_to) : ''));
          }
          if (r.days_late) { meta.push(r.days_late + (r.days_late === 1 ? ' day late' : ' days late')); }
          if (r.result_route) {
            meta.push('<a href="' + esc(r.result_route) + '" target="_blank" rel="noopener">' +
                      esc(r.linked_test_result) + '</a>');
          }

          var tone = STATUS_COLORS[r.status];
          html += '<div class="cdd-visit-item">' +
                  '<div class="cdd-visit-head">' + head +
                    '<span class="cdd-visit-cost"' + (tone ? ' style="color:' + tone + '"' : '') + '>' +
                      esc(r.status || '') + '</span>' +
                  '</div>' +
                  '<div class="cdd-line-note">' + meta.join(' \u00b7 ') + '</div>' +
                  '</div>';
        });
        empModalBody.innerHTML = html;
      })
      .catch(function (err) {
        console.error('Failed to load request list:', err);
        empModalBody.innerHTML = '<div class="cdd-modal-empty">Could not load the list.</div>';
      });
  }

  // The records behind one Disease & Health tile.
  function openHealthModal(kind, label) {
    empModalTitle.textContent = label;
    empModalBody.innerHTML = '<div class="cdd-modal-empty">Loading&hellip;</div>';
    empModal.classList.add('is-open');

    var filters = filterValues('health');
    filters.kind = kind;

    post('/api/method/cova_clinic_integration.api.disease_report_detail', filters)
      .then(function (res) {
        var rows = (res && res.rows) || [];
        if (!rows.length) {
          empModalBody.innerHTML = '<div class="cdd-modal-empty">Nothing to show.</div>';
          return;
        }

        // The average is per month of one year, so the title says which year.
        // With no year filter the rows may span several; name them all.
        if (kind === 'average') {
          var years = filters.year ? [String(filters.year)] : [];
          if (!years.length) {
            rows.forEach(function (r) {
              var y = (r.posting_date || '').slice(0, 4);
              if (y && years.indexOf(y) === -1) { years.push(y); }
            });
            years.sort();
          }
          if (years.length) { empModalTitle.textContent = label + ' ' + years.join(', '); }
        }

        var summary;
        if (kind === 'conditions') {
          summary = rows.length + (rows.length === 1 ? ' condition' : ' conditions') +
                    ' · ' + num(res.total) + ' encounters';
        } else if (kind === 'average') {
          summary = num(Math.round(res.total / rows.length)) + ' per month across ' +
                    rows.length + (rows.length === 1 ? ' report' : ' reports');
        } else if (kind === 'months') {
          summary = rows.length + (rows.length === 1 ? ' monthly report' : ' monthly reports') +
                    ' · ' + num(res.total) + ' encounters';
        } else {
          summary = rows.length + (rows.length === 1 ? ' line' : ' lines') +
                    ' · ' + num(res.total) + ' encounters';
        }
        var html = '<div class="cdd-visit-total"><span>' + esc(summary) + '</span></div>';

        rows.forEach(function (r) {
          var title, meta = [];
          if (kind === 'conditions') {
            title = r.medical_case;
            meta.push(r.reports + (r.reports === 1 ? ' report' : ' reports'));
          } else if (kind === 'average' || kind === 'months') {
            title = r.month + (r.posting_date ? ' ' + r.posting_date.slice(0, 4) : '');
            if (r.posting_date) { meta.push('Posted ' + esc(r.posting_date)); }
            meta.push(r.conditions + (r.conditions === 1 ? ' condition' : ' conditions'));
          } else {
            title = r.medical_case;
            meta.push(esc(r.month) + (r.posting_date ? ' · posted ' + esc(r.posting_date) : ''));
            if (r.months_seen) {
              meta.push(r.months_seen === 1 ? 'seen in 1 month' : 'recurred in ' + r.months_seen + ' months');
            }
          }

          var head = r.route
            ? '<a class="cdd-visit-date" href="' + esc(r.route) + '" target="_blank" rel="noopener"' +
              ' title="Open ' + esc(r.name || title) + '">' + esc(title) +
              '<span class="cdd-visit-open" aria-hidden="true">&#8599;</span></a>'
            : '<span class="cdd-visit-date">' + esc(title) + '</span>';

          html += '<div class="cdd-visit-item">' +
                  '<div class="cdd-visit-head">' + head +
                    '<span class="cdd-visit-cost">' + num(r.total) + '</span>' +
                  '</div>' +
                  (meta.length ? '<div class="cdd-line-note">' + meta.join(' · ') + '</div>' : '') +
                  '</div>';
        });

        empModalBody.innerHTML = html;
      })
      .catch(function (err) {
        console.error('Failed to load health list:', err);
        empModalBody.innerHTML = '<div class="cdd-modal-empty">Could not load the list.</div>';
      });
  }

  // The punches behind one Clinic Visits (biometric) tile.
  function openPunchModal(kind, label) {
    empModalTitle.textContent = label;
    empModalBody.innerHTML = '<div class="cdd-modal-empty">Loading&hellip;</div>';
    empModal.classList.add('is-open');

    var filters = filterValues('biometric');
    filters.kind = kind || '';

    // Same two colours as the Arrivals & Departures chart series.
    var tone = { IN: slot(0), OUT: slot(1) };

    post('/api/method/cova_clinic_integration.api.clinic_punch_people', filters)
      .then(function (res) {
        var rows = (res && res.rows) || [];
        if (!rows.length) {
          empModalBody.innerHTML = '<div class="cdd-modal-empty">Nothing to show.</div>';
          return;
        }

        var summary, html;
        if (kind === 'employees' || kind === 'days') {
          // One grid row per person (or day): the count, its in/out split and
          // the span of activity, each in its own column.
          var isEmp = kind === 'employees';
          summary = isEmp
            ? rows.length + (rows.length === 1 ? ' employee' : ' employees')
            : rows.length + (rows.length === 1 ? ' day' : ' days');
          var totalPunches = rows.reduce(function (a, r) { return a + (r.punches || 0); }, 0);
          html = '<div class="cdd-visit-total"><span>' + esc(summary) + '</span>' +
                 '<span>' + totalPunches + ' punches</span></div>' +
                 '<div class="cdd-punch-cols cdd-punch-cols-emp"><span>' + (isEmp ? 'Employee' : 'Day') + '</span>' +
                 '<span>Visits</span><span>In</span><span>Out</span>' +
                 '<span>' + (isEmp ? 'Seen' : 'People') + '</span></div>';

          function stamp(ts) {
            // Time only when the range is a single day; date otherwise.
            if (!ts) { return '\u2014'; }
            return ts.slice(0, 10) === today ? ts.slice(11, 16) : ts.slice(0, 10);
          }
          var today = filters.from_date && filters.from_date === filters.to_date ? filters.from_date : '';

          rows.forEach(function (r) {
            var who, seen;
            if (isEmp) {
              var name = r.employee_name || r.employee || '\u2014';
              who = '<span class="cdd-punch-who">' + esc(name) +
                    (r.payroll_number ? '<small>' + esc(r.payroll_number) + '</small>' : '') + '</span>';
              seen = r.first_seen
                ? stamp(r.first_seen) + (r.last_seen && r.last_seen !== r.first_seen ? ' \u2013 ' + stamp(r.last_seen) : '')
                : '\u2014';
            } else {
              who = '<span class="cdd-punch-who">' + esc(r.day) + '</span>';
              seen = r.employees + (r.employees === 1 ? ' person' : ' people');
            }
            html += '<div class="cdd-punch-row cdd-punch-row-emp">' + who +
                    '<span class="cdd-punch-n">' + r.punches + '</span>' +
                    '<span class="cdd-punch-t" style="color:' + tone.IN + '">' + r.in_punches + '</span>' +
                    '<span class="cdd-punch-t" style="color:' + tone.OUT + '">' + r.out_punches + '</span>' +
                    '<span class="cdd-punch-dur">' + esc(seen) + '</span>' +
                    '</div>';
          });
          empModalBody.innerHTML = html;
          return;
        }

        // Visits: one row per employee stay, grouped under the day, with the
        // in and out times side by side and how long they spent.
        var ins = 0, outs = 0;
        rows.forEach(function (r) { if (r.time_in) { ins++; } if (r.time_out) { outs++; } });
        summary = (ins + outs) + ' punches \u00b7 ' + rows.length + (rows.length === 1 ? ' visit' : ' visits');
        html = '<div class="cdd-visit-total"><span>' + esc(summary) + '</span></div>';

        function hm(ts) { return ts ? ts.slice(11, 16) : '\u2014'; }
        function dur(mins) {
          if (mins === null || mins === undefined) { return ''; }
          var h = Math.floor(mins / 60), m = mins % 60;
          return (h ? h + 'h ' : '') + (m < 10 && h ? '0' : '') + m + 'm';
        }

        var lastDay = null;
        rows.forEach(function (r) {
          if (r.day !== lastDay) {
            lastDay = r.day;
            var dayCount = rows.filter(function (x) { return x.day === r.day; }).length;
            html += '<div class="cdd-punch-day"><span>' + esc(r.day) + '</span>' +
                    '<span>' + dayCount + (dayCount === 1 ? ' visit' : ' visits') + '</span></div>' +
                    '<div class="cdd-punch-cols"><span>Employee</span><span>In</span><span>Out</span><span>Stay</span></div>';
          }
          var name = r.employee_name || r.employee || '\u2014';
          var who = r.route
            ? '<a href="' + esc(r.route) + '" target="_blank" rel="noopener" title="Open ' + esc(r.name) + '">' + esc(name) + '</a>'
            : esc(name);
          html += '<div class="cdd-punch-row">' +
                  '<span class="cdd-punch-who">' + who +
                    (r.payroll_number ? '<small>' + esc(r.payroll_number) + '</small>' : '') + '</span>' +
                  '<span class="cdd-punch-t" style="color:' + tone.IN + '">' + hm(r.time_in) + '</span>' +
                  '<span class="cdd-punch-t" style="color:' + tone.OUT + '">' + hm(r.time_out) + '</span>' +
                  '<span class="cdd-punch-dur">' + esc(dur(r.minutes)) + '</span>' +
                  '</div>';
        });

        empModalBody.innerHTML = html;
      })
      .catch(function (err) {
        console.error('Failed to load punch list:', err);
        empModalBody.innerHTML = '<div class="cdd-modal-empty">Could not load the list.</div>';
      });
  }

  function wireChartActions() {
    var host = el('cdd-view');
    if (!host) { return; }
    Array.prototype.forEach.call(host.querySelectorAll('.cdd-chart-action'), function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        if (btn.getAttribute('data-action') === 'trend') { openTrendModal(); }
      });
    });
  }

  // ── conditions trend modal ─────────────────────────────────────────
  // Encounters per condition as a line, highest on the left, with its own
  // year / month / from / to filters so the period can be changed in place.
  var trendModal = el('cdd-trend-modal');
  var trendChart = null;
  var trendBuilt = false;
  var trendMode = '';          // 'conditions' (all cases) or 'condition' (one case by month)
  var trendCondition = '';

  function trendValues() {
    var months = [];
    Array.prototype.forEach.call(document.querySelectorAll('#cdd-trend-months .cdd-toggle.is-on'), function (b) {
      months.push(b.getAttribute('data-month'));
    });
    return { year: (el('f-trend_year') || {}).value || '', months: months };
  }

  function buildTrendFilters() {
    var wrap = el('cdd-trend-filters');
    if (!wrap) { return; }
    var years = (globalFilterOptions || {}).years || [];
    var h = '<div class="cdd-field cdd-trend-year"><label>Year</label><select id="f-trend_year"><option value="">All Years</option>';
    years.forEach(function (y) { h += '<option value="' + esc(y) + '">' + esc(y) + '</option>'; });
    h += '</select></div>';
    // Months are toggles: none on means the whole year as one line, each one
    // switched on becomes its own line.
    h += '<div class="cdd-field cdd-trend-monthfield"><label>Months</label><div class="cdd-toggles" id="cdd-trend-months">';
    MONTH_LABELS.forEach(function (m) {
      h += '<button type="button" class="cdd-toggle" data-month="' + m + '" aria-pressed="false">' + m + '</button>';
    });
    h += '</div></div>';
    wrap.innerHTML = h;

    el('f-trend_year').addEventListener('change', loadTrend);
    Array.prototype.forEach.call(wrap.querySelectorAll('.cdd-toggle'), function (b) {
      b.addEventListener('click', function () {
        var on = !b.classList.contains('is-on');
        b.classList.toggle('is-on', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        loadTrend();
      });
    });
    trendBuilt = true;
  }

  function setTrendMonths(months) {
    Array.prototype.forEach.call(document.querySelectorAll('#cdd-trend-months .cdd-toggle'), function (b) {
      var on = months.indexOf(b.getAttribute('data-month')) !== -1;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  function loadTrend() {
    var body = el('cdd-trend-body');
    var status = el('cdd-trend-status');
    var filters = trendValues();
    filters.kind = 'conditions_by_month';
    if (status) { status.textContent = 'Loading\u2026'; }

    post('/api/method/cova_clinic_integration.api.disease_report_detail', filters)
      .then(function (res) {
        var rows = (res && res.rows) || [];
        if (trendChart) { trendChart.destroy(); charts = charts.filter(function (c) { return c !== trendChart; }); trendChart = null; }
        if (!rows.length) {
          if (status) { status.textContent = 'No encounters for the selected filters.'; }
          body.hidden = true;
          return;
        }
        body.hidden = false;

        // Pivot condition x month. Cases run highest total on the left to
        // lowest on the right, whatever months are switched on.
        var totals = {}, byCase = {};
        rows.forEach(function (r) {
          totals[r.medical_case] = (totals[r.medical_case] || 0) + r.total;
          byCase[r.medical_case] = byCase[r.medical_case] || {};
          byCase[r.medical_case][r.month] = (byCase[r.medical_case][r.month] || 0) + r.total;
        });
        var cases = Object.keys(totals).sort(function (a, b) { return totals[b] - totals[a]; });
        var grand = cases.reduce(function (a, c) { return a + totals[c]; }, 0);

        var months = filters.months.length
          ? MONTH_LABELS.filter(function (m) { return filters.months.indexOf(m) !== -1; })
          : [];
        var series = months.length
          ? months.map(function (m) {
              return { label: m, values: cases.map(function (c) { return byCase[c][m] || 0; }) };
            })
          : [{ label: 'Encounters', values: cases.map(function (c) { return totals[c]; }) }];

        if (status) {
          status.textContent = cases.length + (cases.length === 1 ? ' condition' : ' conditions') +
                               ' \u00b7 ' + num(grand) + ' encounters' +
                               (months.length ? ' \u00b7 ' + months.join(', ') : '');
        }
        ensureChartJs(function () {
          if (!trendModal.classList.contains('is-open')) { return; }
          trendChart = lineChart('c-trend-modal', {
            title: 'Encounters by Condition',
            labels: cases,
            series: series,
            xTicks: { autoSkip: false, maxRotation: 45, minRotation: 30 }
          });
        });
      })
      .catch(function (err) {
        console.error('Failed to load trend:', err);
        if (status) { status.textContent = 'Could not load the trend.'; }
      });
  }

  // One condition across the months of one year, with a single-select year
  // toggle. Opened from a row of Conditions by Month.
  function buildConditionFilters() {
    var wrap = el('cdd-trend-filters');
    if (!wrap) { return; }
    var years = (globalFilterOptions || {}).years || [];
    var h = '<div class="cdd-field cdd-trend-monthfield"><label>Year</label><div class="cdd-toggles cdd-toggles-years" id="cdd-trend-years">';
    years.forEach(function (y) {
      h += '<button type="button" class="cdd-toggle" data-year="' + esc(y) + '" aria-pressed="false">' + esc(y) + '</button>';
    });
    h += '</div></div>';
    wrap.innerHTML = h;
    Array.prototype.forEach.call(wrap.querySelectorAll('.cdd-toggle'), function (b) {
      b.addEventListener('click', function () {
        // Radio behaviour: exactly one year on at a time.
        setTrendYear(b.getAttribute('data-year'));
        loadConditionTrend();
      });
    });
    trendBuilt = true;
  }

  function setTrendYear(year) {
    Array.prototype.forEach.call(document.querySelectorAll('#cdd-trend-years .cdd-toggle'), function (b) {
      var on = b.getAttribute('data-year') === String(year);
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  function trendYear() {
    var on = document.querySelector('#cdd-trend-years .cdd-toggle.is-on');
    return on ? on.getAttribute('data-year') : '';
  }

  function loadConditionTrend() {
    var body = el('cdd-trend-body');
    var status = el('cdd-trend-status');
    var year = trendYear();
    if (status) { status.textContent = 'Loading\u2026'; }

    post('/api/method/cova_clinic_integration.api.disease_report_detail',
         { kind: 'conditions_by_month', year: year, medical_case: trendCondition })
      .then(function (res) {
        var rows = (res && res.rows) || [];
        if (trendChart) { trendChart.destroy(); charts = charts.filter(function (c) { return c !== trendChart; }); trendChart = null; }
        var values = MONTH_LABELS.map(function () { return 0; });
        var total = 0;
        rows.forEach(function (r) {
          var i = MONTH_LABELS.indexOf(r.month);
          if (i >= 0) { values[i] += r.total; total += r.total; }
        });
        if (!total) {
          if (status) { status.textContent = 'No encounters for ' + trendCondition + ' in ' + (year || 'any year') + '.'; }
          body.hidden = true;
          return;
        }
        body.hidden = false;
        var seen = values.filter(function (v) { return v; }).length;
        if (status) {
          status.textContent = num(total) + ' encounters \u00b7 ' + seen + (seen === 1 ? ' month' : ' months') +
                               ' \u00b7 ' + num(Math.round(total / seen)) + ' per month';
        }
        ensureChartJs(function () {
          if (!trendModal.classList.contains('is-open')) { return; }
          trendChart = lineChart('c-trend-modal', {
            title: trendCondition + ' \u2014 ' + (year || 'all years'),
            labels: MONTH_LABELS,
            series: [{ label: 'Encounters', values: values }]
          });
        });
      })
      .catch(function (err) {
        console.error('Failed to load condition trend:', err);
        if (status) { status.textContent = 'Could not load the trend.'; }
      });
  }

  function openConditionTrend(condition) {
    if (!trendModal) { return; }
    trendCondition = condition;
    if (trendMode !== 'condition') { trendMode = 'condition'; trendBuilt = false; }
    if (!trendBuilt) { buildConditionFilters(); }
    el('cdd-trend-title').textContent = condition;
    var years = (globalFilterOptions || {}).years || [];
    var year = globalValues.year && years.indexOf(String(globalValues.year)) !== -1
      ? String(globalValues.year) : (years[0] || '');
    setTrendYear(year);
    trendModal.classList.add('is-open');
    loadConditionTrend();
  }

  function openTrendModal() {
    if (!trendModal) { return; }
    if (trendMode !== 'conditions') { trendMode = 'conditions'; trendBuilt = false; }
    if (!trendBuilt) { buildTrendFilters(); }
    el('cdd-trend-title').textContent = 'Conditions Trend';
    // Open on the page's year; a month picked on the page starts switched on.
    var y = el('f-trend_year');
    if (y) { y.value = globalValues.year || ''; }
    setTrendMonths(globalValues.month ? [globalValues.month] : []);
    trendModal.classList.add('is-open');
    loadTrend();
  }

  function closeTrendModal() {
    if (!trendModal) { return; }
    trendModal.classList.remove('is-open');
    if (trendChart) { trendChart.destroy(); charts = charts.filter(function (c) { return c !== trendChart; }); trendChart = null; }
  }

  if (trendModal) {
    el('cdd-trend-close').addEventListener('click', closeTrendModal);
    trendModal.addEventListener('click', function (e) {
      // Only a click on the backdrop itself closes; the dialog swallows its own.
      if (e.target === trendModal) { closeTrendModal(); }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && trendModal.classList.contains('is-open')) { closeTrendModal(); }
    });
  }

  function wireKpiDrill() {
    var host = el('cdd-view');
    if (!host) { return; }
    Array.prototype.forEach.call(host.querySelectorAll('.cdd-kpi-drill'), function (tile) {
      function open(e) {
        e.stopPropagation();
        // A view may bring its own list; the Test Results people list is the default.
        var opener = (VIEWS[current] && VIEWS[current].drill) || openResultsModal;
        opener(tile.getAttribute('data-drill'), tile.getAttribute('data-drill-label'));
      }
      tile.addEventListener('click', open);
      tile.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(e); }
      });
    });
  }

  function openEmpModal(empId, empName, month, opts) {
    opts = opts || {};

    // Grid columns are keyed by the short label; the heading spells it out.
    var monthIdx = month ? MONTH_LABELS.indexOf(String(month).toUpperCase()) : -1;
    var monthFull = monthIdx >= 0 ? MONTH_NAMES[monthIdx] : '';

    // The payroll number only arrives with the rows, so the heading is set
    // once without it and again as soon as the first visit names it.
    function setTitle(payroll) {
      var parts = [empName];
      if (payroll) { parts.push(payroll); }
      if (monthFull) { parts.push(monthFull); }
      if (opts.noCheckin) { parts.push('no checkin'); }
      empModalTitle.textContent = parts.join(' — ');
    }

    setTitle('');
    empModalBody.innerHTML = '<div class="cdd-modal-empty">Loading visits&hellip;</div>';
    empModal.classList.add('is-open');

    // Fetch visit details for the employee from the breakdown endpoint
    var filters = { employee: empId };

    // Add date range filters if provided
    if (globalValues.from_date) { filters.from_date = globalValues.from_date; }
    if (globalValues.to_date) { filters.to_date = globalValues.to_date; }
    if (globalValues.year) { filters.year = globalValues.year; }
    if (opts.noCheckin) { filters.no_checkin = 1; }

    post('/api/method/cova_clinic_integration.api.employee_visit_breakdown', filters)
      .then(function (res) {
        if (res && res.currency) { CURRENCY = res.currency; }
        var allVisits = res && res.visits ? res.visits : [];

        // The cell that was clicked names one month, so narrow to it here —
        // matched off the label's position, not by parsing "JUL" as a date.
        var visits = allVisits;
        if (monthIdx >= 0) {
          var mm = (monthIdx < 9 ? '0' : '') + (monthIdx + 1);
          visits = allVisits.filter(function (v) {
            return v.visit_date && v.visit_date.substring(5, 7) === mm;
          });
        }

        // Payroll number is carried on the rows, not on the grid cell.
        var payroll = (allVisits[0] || {}).payroll_number;
        if (payroll) { setTitle(payroll); }

        if (!visits.length) {
          empModalBody.innerHTML = '<div class="cdd-modal-empty">No Clinic Visit Cost records for this employee' +
            (monthFull ? ' in ' + esc(monthFull) : '') + '.</div>';
          return;
        }

        var totalCost = 0;
        var html = '';
        visits.forEach(function (v) {
          totalCost += (v.total_cost || 0);

          // Line items are what the money actually went on — the notes carry
          // the drug or service name, which is the whole point of the panel.
          var itemsHtml = '';
          if (v.items && v.items.length) {
            itemsHtml = '<ul class="cdd-visit-lines">';
            v.items.forEach(function (item) {
              itemsHtml += '<li>' +
                '<span class="cdd-line-purpose">' + esc(item.purpose || 'Unspecified') + '</span>' +
                '<span class="cdd-line-cost">' + money(item.cost) + '</span>' +
                (item.notes ? '<span class="cdd-line-note">' + esc(item.notes) + '</span>' : '') +
                '</li>';
            });
            itemsHtml += '</ul>';
          }

          // The date opens the Clinic Visit Cost record itself, so a figure
          // that looks wrong can be traced back to the row it came from. The
          // route is built server-side — the desk prefix moved in v17.
          var dateCell = v.route
            ? '<a class="cdd-visit-date" href="' + esc(v.route) + '" target="_blank" rel="noopener"' +
              ' title="Open ' + esc(v.name) + '">' + esc(v.visit_date || '') +
              '<span class="cdd-visit-open" aria-hidden="true">&#8599;</span></a>'
            : '<span class="cdd-visit-date">' + esc(v.visit_date || '') + '</span>';

          html += '<div class="cdd-visit-item">' +
                  '<div class="cdd-visit-head">' +
                    dateCell +
                    '<span class="cdd-visit-cost">' + cash(v.total_cost) + '</span>' +
                  '</div>' +
                  itemsHtml +
                  '</div>';
        });

        empModalBody.innerHTML =
          '<div class="cdd-visit-total">' +
            '<span>' + visits.length + (visits.length === 1 ? ' visit' : ' visits') + '</span>' +
            '<span>' + cash(totalCost) + '</span>' +
          '</div>' + html;
      })
      .catch(function (err) {
        console.error('Failed to load employee visits:', err);
        empModalBody.innerHTML = '<div class="cdd-modal-empty">Could not load visit details.</div>';
      });
  }

  if (empModalClose) {
    empModalClose.addEventListener('click', closeEmpModal);
  }

  // The panel is the whole overlay — there is no backdrop element to click, so
  // "outside" means anywhere in the document that is not inside the panel. The
  // cell that opens it stops its own click bubbling, or opening would close it
  // again in the same event.
  document.addEventListener('click', function (e) {
    if (!empModal || !empModal.classList.contains('is-open')) { return; }
    if (!empModal.contains(e.target)) { closeEmpModal(); }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && empModal && empModal.classList.contains('is-open')) {
      closeEmpModal();
    }
  });

  // ── wiring ────────────────────────────────────────────────────────

  Array.prototype.forEach.call(app.querySelectorAll('.cdd-nav-item'), function (b) {
    b.addEventListener('click', function () { activate(b.getAttribute('data-view')); });
  });

  el('cdd-rail-toggle').addEventListener('click', function () {
    el('cdd-rail').classList.toggle('is-collapsed');
    // Chart.js needs a nudge once the column finishes resizing.
    setTimeout(function () {
      charts.forEach(function (c) { if (c) { c.resize(); } });
    }, 220);
  });

  el('cdd-refresh').addEventListener('click', function () { load(current); });
  el('cdd-export').addEventListener('click', exportXlsx);

  // The band is position:fixed, so it is out of flow — reserve exactly its
  // height at the top of the main column. Measured rather than hardcoded
  // because the band wraps to two lines at narrow widths. Below 860px the
  // band returns to the flow and the stylesheet owns the padding again.
  // The band is a flex row of the main column now, so nothing has to be
  // measured. Switching dashboards returns the scroll box to the top; changing
  // a filter deliberately does not, so you keep your place in a long table.
  function resetScroll() {
    var box = el('cdd-scroll');
    if (box) { box.scrollTop = 0; }
  }

  activate('health');
});

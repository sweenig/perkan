// Sprint burn-up page: cumulative minutes spent on the sprint's cards over the sprint's dates.
// Data: GET /api/sprint/<id|active>/burnup?step=hour|day. The page URL keeps the choices:
// ?sprint=<id> (default: the active sprint), ?step=day (default: hour) and ?hide=1|0 (default: the saved
// "hide non-working time" setting; Settings > Working time).

const $ = (id) => document.getElementById(id);
const SVG_NS = 'http://www.w3.org/2000/svg';
const W = 900, H = 360, M = { l: 62, r: 96, t: 20, b: 46 };
const IW = W - M.l - M.r, IH = H - M.t - M.b;

let sprints = [];
let showTable = false;
let current = null;          // the data on screen
let cursor = 0;              // index of the point the crosshair is on

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function svg(tag, attrs, text) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}

// 45 -> "45 min", 1988 -> "33.1 h": from an hour up, hours read better than a long minute count
const fmtMinutes = (n) => (n < 60 ? `${Number(n).toLocaleString()} min` : `${fmtHours(n)} h`);
const fmtHours = (n) => (n / 60).toLocaleString(undefined, { maximumFractionDigits: 1 });
// the same, with the exact minutes alongside once it is shown in hours: "33.1 h (1,988 min)"
const fmtMinutesBoth = (n) => (n < 60 ? fmtMinutes(n) : `${fmtMinutes(n)} (${Number(n).toLocaleString()} min)`);

function fmtHM(n) {
  const h = Math.floor(n / 60), m = n % 60;
  if (!h) return `${m} m`;
  return m ? `${h} h ${m} m` : `${h} h`;
}

function parseDay(iso) {            // yyyy-mm-dd -> local Date (no time zone shifting)
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d);
}

function fmtDate(iso, opts) {
  return parseDay(iso).toLocaleDateString(undefined, opts || { weekday: 'short', month: 'short', day: 'numeric' });
}

// "2026-11-04T15:00" is already wall-clock time in the server's zone: build a Date from its parts, never shift it
function fmtLocal(local) {
  const [d, t] = local.split('T');
  const [y, mo, da] = d.split('-').map(Number);
  const [h, mi] = t.split(':').map(Number);
  return new Date(y, mo - 1, da, h, mi).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

// a UTC instant ("2026-11-04T15:00:00Z") shown in the sprint's time zone
function fmtLocalUtc(iso, tz) {
  try {
    return new Date(iso).toLocaleString(undefined, { timeZone: tz, weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  } catch (e) {
    return new Date(iso).toLocaleString();
  }
}

function pointLabel(p) {
  switch (p.kind) {
    case 'start': return `Start of ${fmtDate(p.day)}`;
    case 'day_end': return `End of ${fmtDate(p.day)}`;
    case 'end': return 'End of the sprint';
    case 'now': return `${fmtLocal(p.local)} (now)`;
    case 'closed': return `${fmtLocal(p.local)} (sprint closed)`;
    default: return fmtLocal(p.local);
  }
}

// the smallest "round minutes" step that fits the data into about five gridlines
function niceStep(max) {
  const steps = [5, 10, 15, 30, 60, 120, 180, 240, 300, 600, 1200, 1800, 3000, 6000, 12000, 30000, 60000];
  for (const s of steps) if (max / s <= 5) return s;
  return steps[steps.length - 1];
}

async function api(url) {
  const res = await fetch(url);
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
  return data;
}

const params = () => new URLSearchParams(location.search);
const currentStep = () => (params().get('step') === 'day' ? 'day' : 'hour');
const hideQuery = () => { const h = params().get('hide'); return h === '1' ? '&hide_non_working=true' : (h === '0' ? '&hide_non_working=false' : ''); };

function sprintLabel(s) {
  const dates = s.start_date ? `${fmtDate(s.start_date, { month: 'short', day: 'numeric' })} to ${fmtDate(s.end_date, { month: 'short', day: 'numeric' })}`
    : `ends ${fmtDate(s.end_date, { month: 'short', day: 'numeric' })}`;
  return `${s.name} · ${s.state} · ${dates}`;
}

function buildSelect() {
  const select = $('buTitle');
  select.replaceChildren();
  if (!sprints.length) {
    select.appendChild(new Option('No sprints yet', ''));
    select.disabled = true;
    return;
  }
  select.appendChild(Object.assign(new Option('Select a sprint…', ''), { disabled: true, hidden: true }));
  sprints.forEach((s) => select.appendChild(new Option(sprintLabel(s), s.id)));
  select.addEventListener('change', () => {
    const p = params();
    p.set('sprint', select.value);
    history.pushState(null, '', `?${p}`);
    show();
  });
}

function reset() {
  ['buNotes', 'buDayTable', 'buCards', 'buUnknown', 'buChart'].forEach((id) => $(id).replaceChildren());
  ['buChartWrap', 'buTotal', 'buSplit', 'buLegend', 'buDesc', 'buTableBtn', 'buHideBtn', 'buSteps', 'buCardsSection', 'buTip'].forEach((id) => { $(id).hidden = true; });
  $('buSub').textContent = '';
  $('buDesc').textContent = '';
  current = null;
}

async function show() {
  reset();
  const ref = params().get('sprint') || 'active';
  let data;
  try {
    data = await api(`/api/sprint/${encodeURIComponent(ref)}/burnup?step=${currentStep()}${hideQuery()}`);
  } catch (err) {
    $('buTitle').value = '';
    $('buNotes').appendChild(el('p', 'bu-note', err.message + (ref === 'active' ? '. Pick a sprint above, or start one on the Sprints page.' : '.')));
    return;
  }
  $('buTitle').value = data.sprint.id;
  render(data);
}

function render(data) {
  current = data;
  const sp = data.sprint;
  $('buSub').textContent = sp.start_date
    ? `${fmtDate(sp.start_date)} to ${fmtDate(sp.end_date)} · times in ${data.tz}`
    : `ends ${fmtDate(sp.end_date)}`;
  data.notes.forEach((n) => {
    const p = el('p', 'bu-note', n);
    if (/Settings > Statuses/.test(n)) {
      p.textContent = n.replace(' in Settings > Statuses.', ' in ');
      const a = el('a', '', 'Settings > Statuses');
      a.href = '/settings?tab=statuses';
      p.append(a, '.');
    }
    $('buNotes').appendChild(p);
  });
  if (!data.days.length) return;

  $('buTotal').hidden = false;
  $('buTotalValue').textContent = fmtMinutesBoth(data.total_minutes);
  const hasAdded = (data.additions || []).length > 0;
  $('buSplit').hidden = !hasAdded;
  if (hasAdded) {
    const pct = data.total_minutes ? Math.round((data.added_minutes / data.total_minutes) * 100) : 0;
    $('buSplit').textContent = `${fmtMinutesBoth(data.planned_minutes)} planned · ${fmtMinutesBoth(data.added_minutes)} added mid-sprint (${pct}%)`;
  }
  $('buLegend').hidden = !hasAdded;
  renderLegend(data);
  $('buChartWrap').hidden = false;
  $('buSteps').hidden = false;
  $('buHideBtn').hidden = false;
  $('buHideBtn').setAttribute('aria-pressed', data.hide_non_working ? 'true' : 'false');
  $('buHideBtn').textContent = data.hide_non_working ? 'Show non-working time' : 'Hide non-working time';
  document.querySelectorAll('#buSteps [data-step]').forEach((b) => b.setAttribute('aria-pressed', b.dataset.step === data.step ? 'true' : 'false'));
  drawChart(data);
  const cols = data.work_columns.map((c) => c.title).join(', ');
  if (cols) {
    $('buDesc').hidden = false;
    $('buDesc').textContent = `Cumulative wall-clock minutes the sprint's cards spent in ${cols}, while they were in this sprint. ` +
      'Cards in progress at the same time add up. ' +
      (hasAdded ? 'The solid area is time on cards that were in the sprint when it started; the hatched area on top is time on cards added after it started, and each ▲ marks a card being added. ' : '') +
      (data.step === 'hour' ? 'One point per hour.' : 'One point per finished day.') +
      (!data.hide_non_working && (data.non_working || []).length ? ' Gray areas are non-working time.' : '') +
      (data.hide_non_working ? ' Non-working time is hidden; time tracked in it is included at the next working point, so the total does not change.' : '');
  }
  renderTable(data);
  renderCards(data);
}

// x position (0..1) of an instant: each shown day is an equal band, placed by that band's exact start and end
// (DST safe). An instant in a hidden stretch (a weekend, the night) sits at the edge between the bands around it.
function xFraction(iso, bands) {
  const t = Date.parse(iso);
  const n = bands.length;
  for (let i = 0; i < n; i++) {
    const a = Date.parse(bands[i].start), b = Date.parse(bands[i].end);
    if (t < a) return i / n;
    if (t <= b) return (i + (t - a) / (b - a)) / n;
  }
  return 1;
}

// legend: only when cards were added mid-sprint (otherwise there is one series and nothing to tell apart)
function renderLegend(data) {
  const box = $('buLegend');
  box.replaceChildren();
  if (!(data.additions || []).length) return;
  const item = (swatch, text) => { const i = el('span', 'bu-legend-item'); i.append(swatch, text); box.appendChild(i); };
  item(el('span', 'bu-swatch bu-swatch-planned'), 'Planned into the sprint');
  item(el('span', 'bu-swatch bu-swatch-added'), 'Added mid-sprint');
  item(el('span', 'bu-swatch-mark', '▲'), 'A card was added');
}

function drawChart(data) {
  const chart = $('buChart');
  chart.replaceChildren();
  const n = data.days.length;
  const points = data.points;
  const maxV = Math.max(...points.map((p) => p.minutes), 0);
  const step = niceStep(Math.max(maxV, 30));
  const top = Math.max(step, Math.ceil(maxV / step) * step);
  const hours = step >= 60;                       // gridline steps of an hour or more are labelled in hours
  const x = (frac) => M.l + frac * IW;
  const y = (v) => M.t + IH - (v / top) * IH;
  const pts = points.map((p) => [x(xFraction(p.at, data.bands)), y(p.minutes)]);
  const base = points.map((p, i) => [pts[i][0], y(p.planned_minutes)]);      // top of the planned series
  const additions = data.additions || [];
  const hasAdded = additions.length > 0;

  for (let v = 0; v <= top; v += step) {
    chart.appendChild(svg('line', { class: v === 0 ? 'bu-axis' : 'bu-grid', x1: M.l, x2: M.l + IW, y1: y(v), y2: y(v) }));
    chart.appendChild(svg('text', { class: 'bu-axis-text', x: M.l - 8, y: y(v) + 4, 'text-anchor': 'end' }, hours ? (v / 60).toLocaleString() : v.toLocaleString()));
  }
  chart.appendChild(svg('text', { class: 'bu-axis-title', transform: `translate(14 ${M.t + IH / 2}) rotate(-90)`, 'text-anchor': 'middle' }, hours ? 'Hours spent (cumulative)' : 'Minutes spent (cumulative)'));

  // non-working time (only sent while it is shown) is shaded gray behind everything else
  (data.non_working || []).forEach((seg) => {
    const x0 = x(xFraction(seg.start, data.bands)), x1 = x(xFraction(seg.end, data.bands));
    if (x1 > x0) chart.appendChild(svg('rect', { class: 'bu-nonwork', x: x0, y: M.t, width: x1 - x0, height: IH }));
  });

  // day separators, and one label per day band (every k-th when there are many; the last only if it has room)
  for (let i = 1; i < n; i++) chart.appendChild(svg('line', { class: 'bu-grid', x1: x(i / n), x2: x(i / n), y1: M.t, y2: M.t + IH }));
  const k = Math.max(1, Math.ceil(n / 12));
  data.days.forEach((d, i) => {
    if (!(i % k === 0 || (i === n - 1 && (n - 1) % k >= Math.ceil(k / 2)))) return;
    const cx = x((i + 0.5) / n);
    const t = svg('text', { class: 'bu-axis-text', x: cx, y: M.t + IH + 16, 'text-anchor': 'middle' });
    t.appendChild(svg('tspan', { x: cx }, fmtDate(d, { weekday: 'short' })));
    t.appendChild(svg('tspan', { x: cx, dy: 13 }, fmtDate(d, { month: 'numeric', day: 'numeric' })));
    chart.appendChild(t);
  });

  const line = (a) => a.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  const d = line(pts);
  const last = pts[pts.length - 1];
  if (hasAdded) {
    // the added series is the band between the planned line and the total line; hatched so colour is not the only cue
    const defs = svg('defs');
    const pat = svg('pattern', { id: 'buHatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
    pat.appendChild(svg('rect', { class: 'bu-hatch-bg', width: 6, height: 6 }));
    pat.appendChild(svg('line', { class: 'bu-hatch-line', x1: 0, y1: 0, x2: 0, y2: 6 }));
    defs.appendChild(pat);
    chart.appendChild(defs);
  }
  const lastBase = base[base.length - 1];
  chart.appendChild(svg('path', { class: 'bu-area', d: `${line(base)} L${lastBase[0].toFixed(1)} ${y(0)} L${base[0][0].toFixed(1)} ${y(0)} Z` }));
  if (hasAdded) {
    chart.appendChild(svg('path', { class: 'bu-area-added', d: `${d} L${[...base].reverse().map((p) => `${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' L')} Z` }));
    chart.appendChild(svg('path', { class: 'bu-line-added', d }));
    chart.appendChild(svg('path', { class: 'bu-line', d: line(base) }));
  } else {
    chart.appendChild(svg('path', { class: 'bu-line', d }));
  }
  // one marker per card added mid-sprint, on the time axis
  additions.forEach((a) => {
    const mx = x(xFraction(a.at, data.bands));
    const m = svg('path', { class: 'bu-add-mark', d: `M${(mx - 5).toFixed(1)} ${y(0)} L${(mx + 5).toFixed(1)} ${y(0)} L${mx.toFixed(1)} ${(y(0) - 9).toFixed(1)} Z` });
    m.appendChild(svg('title', {}, `${a.title} added ${fmtLocalUtc(a.at, data.tz)}`));
    chart.appendChild(m);
  });
  const cross = svg('line', { class: 'bu-cross', y1: M.t, y2: M.t + IH, visibility: 'hidden' });
  const marker = svg('circle', { class: 'bu-dot', r: 5, visibility: 'hidden' });
  chart.appendChild(cross);
  chart.appendChild(svg('circle', { class: 'bu-dot', cx: last[0], cy: last[1], r: 5 }));
  chart.appendChild(svg('text', { class: 'bu-endlabel', x: last[0] + 12, y: last[1] + 4 }, fmtMinutes(points[points.length - 1].minutes)));
  if (hasAdded && lastBase[1] - last[1] > 14) chart.appendChild(svg('text', { class: 'bu-endlabel bu-endlabel-sub', x: lastBase[0] + 12, y: lastBase[1] + 4 }, fmtMinutes(points[points.length - 1].planned_minutes) + ' planned'));
  chart.appendChild(marker);

  // hover and keyboard: the crosshair snaps to the nearest point; left/right arrows step through the points
  const tip = $('buTip');
  const show = (i) => {
    cursor = i;
    const p = points[i];
    cross.setAttribute('x1', pts[i][0]);
    cross.setAttribute('x2', pts[i][0]);
    cross.setAttribute('visibility', 'visible');
    marker.setAttribute('cx', pts[i][0]);
    marker.setAttribute('cy', pts[i][1]);
    marker.setAttribute('visibility', 'visible');
    tip.replaceChildren();
    tip.appendChild(el('div', 'bu-tip-day', pointLabel(p)));
    const row = el('div', 'bu-tip-row');
    row.append(el('span', 'bu-tip-key'), el('span', 'bu-tip-value', fmtMinutes(p.minutes)), ...(p.minutes >= 60 ? [el('span', 'bu-tip-day', `(${p.minutes.toLocaleString()} min)`)] : []));
    tip.appendChild(row);
    if (hasAdded) {
      const r1 = el('div', 'bu-tip-row'), r2 = el('div', 'bu-tip-row');
      r1.append(el('span', 'bu-tip-key'), el('span', '', `${fmtMinutes(p.planned_minutes)} planned`));
      r2.append(el('span', 'bu-tip-key bu-tip-key-added'), el('span', '', `${fmtMinutes(p.added_minutes)} added mid-sprint`));
      tip.append(r1, r2);
      const prevAt = i > 0 ? Date.parse(points[i - 1].at) : -Infinity;
      additions.filter((a) => Date.parse(a.at) > prevAt && Date.parse(a.at) <= Date.parse(p.at))
        .forEach((a) => tip.appendChild(el('div', 'bu-tip-day', `▲ ${a.title} added`)));
    }
    if (i > 0) tip.appendChild(el('div', 'bu-tip-day', `+${fmtMinutes(p.minutes - points[i - 1].minutes)} since the previous point`));
    tip.hidden = false;
    const box = chart.getBoundingClientRect();
    const scale = box.width / W;
    const wrapBox = $('buChartWrap').getBoundingClientRect();
    let left = box.left - wrapBox.left + pts[i][0] * scale + 14;
    const topPx = box.top - wrapBox.top + pts[i][1] * scale - 14;
    if (left + tip.offsetWidth > wrapBox.width) left -= tip.offsetWidth + 28;
    tip.style.left = `${Math.max(0, left)}px`;
    tip.style.top = `${Math.max(0, topPx)}px`;
  };
  const hide = () => {
    cross.setAttribute('visibility', 'hidden');
    marker.setAttribute('visibility', 'hidden');
    tip.hidden = true;
  };
  chart.onpointermove = (e) => {
    const box = chart.getBoundingClientRect();
    const px = ((e.clientX - box.left) / box.width) * W;
    let best = 0;
    pts.forEach((p, i) => { if (Math.abs(p[0] - px) < Math.abs(pts[best][0] - px)) best = i; });
    show(best);
  };
  chart.onpointerleave = () => { if (document.activeElement !== chart) hide(); };
  chart.onfocus = () => show(Math.min(cursor, points.length - 1));
  chart.onblur = hide;
  chart.onkeydown = (e) => {
    const keys = { ArrowLeft: Math.max(0, cursor - 1), ArrowRight: Math.min(points.length - 1, cursor + 1), Home: 0, End: points.length - 1 };
    if (!(e.key in keys)) return;
    e.preventDefault();
    show(keys[e.key]);
  };
  cursor = points.length - 1;
}

function table(headers, rows, numericFrom) {
  const t = el('table', 'bu-table');
  const head = el('tr');
  headers.forEach((h, i) => head.appendChild(el('th', i >= numericFrom ? 'num' : '', h)));
  t.appendChild(el('thead')).appendChild(head);
  const body = el('tbody');
  rows.forEach((r) => {
    const tr = el('tr');
    r.forEach((cell, i) => tr.appendChild(el('td', i >= numericFrom ? 'num' : '', cell)));
    body.appendChild(tr);
  });
  t.appendChild(body);
  return t;
}

function renderTable(data) {
  const btn = $('buTableBtn');
  const box = $('buDayTable');
  btn.hidden = false;
  const split = (data.additions || []).length > 0;       // the planned / added columns only matter once cards were added
  const rows = data.points.map((p, i) => [pointLabel(p), p.minutes.toLocaleString(),
    ...(split ? [p.planned_minutes.toLocaleString(), p.added_minutes.toLocaleString()] : []),
    i ? (p.minutes - data.points[i - 1].minutes).toLocaleString() : '']);
  box.replaceChildren(table(['Point', 'Cumulative minutes', ...(split ? ['Planned', 'Added mid-sprint'] : []), 'Change'], rows, 1));
  applyTable();
}

function applyTable() {
  $('buDayTable').hidden = !showTable;
  const btn = $('buTableBtn');
  btn.setAttribute('aria-pressed', showTable ? 'true' : 'false');
  btn.textContent = showTable ? 'Hide table' : 'Show as table';
}

function renderCards(data) {
  if (!data.cards.length && !data.unknown.length) return;
  $('buCardsSection').hidden = false;
  const split = (data.additions || []).length > 0;
  const addedAt = Object.fromEntries((data.additions || []).map((a) => [a.id, a.at]));
  const came = (c) => (c.added ? `Added ${fmtLocalUtc(addedAt[c.id], data.tz)}` : 'Planned');
  const rows = data.cards.map((c) => [c.title + (c.note ? `  (${c.note})` : ''), ...(split ? [came(c)] : []), c.minutes.toLocaleString(), fmtHM(c.minutes)]);
  if (rows.length) $('buCards').appendChild(table(['Card', ...(split ? ['Came in'] : []), 'Minutes', 'As h m'], rows, split ? 2 : 1));
  if (data.unknown.length) {
    $('buUnknown').appendChild(el('p', 'bu-sub-h', 'Time unknown (not counted, not zero):'));
    $('buUnknown').appendChild(table(['Card', 'Reason'], data.unknown.map((c) => [c.title, c.reason]), 99));
  }
}

$('buHideBtn').addEventListener('click', () => {
  const p = params();
  p.set('hide', current && current.hide_non_working ? '0' : '1');
  history.replaceState(null, '', `?${p}`);
  show();
});
$('buTableBtn').addEventListener('click', () => { showTable = !showTable; applyTable(); });
document.querySelectorAll('#buSteps [data-step]').forEach((b) => b.addEventListener('click', () => {
  if (b.dataset.step === currentStep()) return;
  const p = params();
  p.set('step', b.dataset.step);
  history.replaceState(null, '', `?${p}`);
  show();
}));
window.addEventListener('popstate', show);

(async function init() {
  try { sprints = (await api('/api/sprints?include_closed=true')).sprints; } catch (e) { sprints = []; }
  buildSelect();
  show();
})();

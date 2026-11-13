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

const fmtMinutes = (n) => `${Number(n).toLocaleString()} min`;

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
  ['buChartWrap', 'buTotal', 'buDesc', 'buTableBtn', 'buHideBtn', 'buSteps', 'buCardsSection', 'buTip'].forEach((id) => { $(id).hidden = true; });
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
  $('buTotalValue').textContent = fmtMinutes(data.total_minutes);
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
      'Cards in progress at the same time add up. ' + (data.step === 'hour' ? 'One point per hour.' : 'One point per finished day.') +
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

function drawChart(data) {
  const chart = $('buChart');
  chart.replaceChildren();
  const n = data.days.length;
  const points = data.points;
  const maxV = Math.max(...points.map((p) => p.minutes), 0);
  const step = niceStep(Math.max(maxV, 30));
  const top = Math.max(step, Math.ceil(maxV / step) * step);
  const x = (frac) => M.l + frac * IW;
  const y = (v) => M.t + IH - (v / top) * IH;
  const pts = points.map((p) => [x(xFraction(p.at, data.bands)), y(p.minutes)]);

  for (let v = 0; v <= top; v += step) {
    chart.appendChild(svg('line', { class: v === 0 ? 'bu-axis' : 'bu-grid', x1: M.l, x2: M.l + IW, y1: y(v), y2: y(v) }));
    chart.appendChild(svg('text', { class: 'bu-axis-text', x: M.l - 8, y: y(v) + 4, 'text-anchor': 'end' }, v.toLocaleString()));
  }
  chart.appendChild(svg('text', { class: 'bu-axis-title', transform: `translate(14 ${M.t + IH / 2}) rotate(-90)`, 'text-anchor': 'middle' }, 'Minutes spent (cumulative)'));

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

  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  const last = pts[pts.length - 1];
  chart.appendChild(svg('path', { class: 'bu-area', d: `${d} L${last[0].toFixed(1)} ${y(0)} L${pts[0][0].toFixed(1)} ${y(0)} Z` }));
  chart.appendChild(svg('path', { class: 'bu-line', d }));
  const cross = svg('line', { class: 'bu-cross', y1: M.t, y2: M.t + IH, visibility: 'hidden' });
  const marker = svg('circle', { class: 'bu-dot', r: 5, visibility: 'hidden' });
  chart.appendChild(cross);
  chart.appendChild(svg('circle', { class: 'bu-dot', cx: last[0], cy: last[1], r: 5 }));
  chart.appendChild(svg('text', { class: 'bu-endlabel', x: last[0] + 12, y: last[1] + 4 }, fmtMinutes(points[points.length - 1].minutes)));
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
    row.append(el('span', 'bu-tip-key'), el('span', 'bu-tip-value', fmtMinutes(p.minutes)), el('span', 'bu-tip-day', `(${fmtHM(p.minutes)})`));
    tip.appendChild(row);
    if (i > 0) tip.appendChild(el('div', 'bu-tip-day', `+${(p.minutes - points[i - 1].minutes).toLocaleString()} min since the previous point`));
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
  const rows = data.points.map((p, i) => [pointLabel(p), p.minutes.toLocaleString(), i ? (p.minutes - data.points[i - 1].minutes).toLocaleString() : '']);
  box.replaceChildren(table(['Point', 'Cumulative minutes', 'Added'], rows, 1));
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
  const rows = data.cards.map((c) => [c.title + (c.note ? `  (${c.note})` : ''), c.minutes.toLocaleString(), fmtHM(c.minutes)]);
  if (rows.length) $('buCards').appendChild(table(['Card', 'Minutes', 'As h m'], rows, 1));
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

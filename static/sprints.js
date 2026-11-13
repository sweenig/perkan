// Sprint planning page: active sprint on top, planned sprints below it, then the backlog
// (and closed sprints on request). Cards can be dragged between sections and reordered.

const state = { sprints: [], cards: [], columns: {}, projects: [], today: '', showClosed: false, dragId: null, project: null };
const $ = (id) => document.getElementById(id);

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Perkan-Source': 'ui' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
  return data;
}

async function load() {
  const [sp, cols, cards, projects] = await Promise.all([
    api('GET', '/api/sprints?include_closed=true'),
    api('GET', '/api/columns?include_hidden=true'),
    api('GET', '/api/cards?include_hidden=true&include_closed=true'),
    api('GET', '/api/projects'),
  ]);
  state.projects = projects.projects;
  state.sprints = sp.sprints;
  state.today = sp.today;
  state.columns = Object.fromEntries(cols.columns.map((c) => [c.id, c]));
  state.cards = cards.cards;
  render();
}

// ---------------------------------------------------------------- helpers

function dayNumber(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return Math.round(Date.UTC(y, m - 1, d) / 86400000);
}

function fmtDate(iso) {
  if (!iso) return '';
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}

function addDays(iso, n) {
  const [y, m, d] = iso.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d + n));
  return dt.toISOString().slice(0, 10);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function button(label, cls, onClick, title) {
  const b = el('button', 'sp-btn ' + (cls || ''), label);
  b.type = 'button';
  if (title) b.title = title;
  b.addEventListener('click', onClick);
  return b;
}

function cardsFor(sprintId, closed) {
  // a card in a hidden column is left out, unless that column is a done column or the sprint is closed
  return state.cards.filter((c) => {
    if ((c.sprint || null) !== sprintId) return false;
    const col = state.columns[c.column];
    if (!closed && col && col.hidden && !col.is_done) return false;
    return true;
  });
}

const matchesProject = (card) => !state.project || (card.project || '').trim() === state.project;
const isDone = (card) => !!(state.columns[card.column] || {}).is_done;

// ---------------------------------------------------------------- rendering

function render() {
  renderProjectFilters();
  const main = $('sprintsMain');
  main.replaceChildren();
  const active = state.sprints.find((s) => s.state === 'active');
  const planned = state.sprints.filter((s) => s.state === 'planned');
  const closed = state.sprints.filter((s) => s.state === 'closed');

  if (active) main.appendChild(sectionFor(active, !!active));
  else main.appendChild(emptyActive());
  planned.forEach((s) => main.appendChild(sectionFor(s, !!active)));
  main.appendChild(backlogSection());
  if (state.showClosed) closed.forEach((s) => main.appendChild(sectionFor(s, !!active)));
}

function countLabel(items) {
  const shown = items.filter(matchesProject).length;
  const n = state.project ? `${shown}/${items.length}` : `${items.length}`;
  return `${n} card${items.length === 1 ? '' : 's'}`;
}

function safeColor(c) {
  return /^#[0-9a-fA-F]{3,8}$/.test(c || '') ? c : '#5b2e8a';
}

// Same pills as the board and list pages: one per project with its card count; click toggles the filter.
function renderProjectFilters() {
  const box = $('projectFilters');
  const counts = new Map();
  const closedIds = new Set(state.sprints.filter((s) => s.state === 'closed').map((s) => s.id));
  state.cards.forEach((c) => {
    const col = state.columns[c.column];
    const inClosed = c.sprint && closedIds.has(c.sprint);
    if (inClosed ? !state.showClosed : (col && col.hidden && !col.is_done)) return;   // not shown on this page
    const name = (c.project || '').trim();
    if (name) counts.set(name, (counts.get(name) || 0) + 1);
  });
  if (state.project && !counts.has(state.project)) state.project = null;
  box.replaceChildren();
  if (!counts.size) {
    box.appendChild(el('div', 'project-filters-empty', 'Tag tasks with a project to enable filtering.'));
    return;
  }
  const names = state.projects.map((p) => p.name).filter((n) => counts.has(n));
  counts.forEach((_, n) => { if (!names.includes(n)) names.push(n); });
  const colors = Object.fromEntries(state.projects.map((p) => [p.name, p.color]));
  names.forEach((name) => {
    const b = el('button', 'project-filter-pill' + (state.project === name ? ' active' : ''));
    b.type = 'button';
    b.dataset.project = name;
    b.setAttribute('aria-pressed', state.project === name ? 'true' : 'false');
    const dot = el('span', 'project-filter-pill-dot');
    dot.style.backgroundColor = readableOnDark(safeColor(colors[name]), 3);
    b.append(dot, el('span', 'project-filter-pill-label', `${name} (${counts.get(name)})`));
    b.addEventListener('click', () => { state.project = state.project === name ? null : name; render(); });
    box.appendChild(b);
  });
}

function emptyActive() {
  const box = el('section', 'sp-section sp-active');
  const head = el('div', 'sp-head');
  head.append(el('span', 'sp-kind', 'Active'), el('span', 'sp-title', 'No active sprint'),
    el('span', 'sp-meta', 'Start a planned sprint below, or create a new one.'));
  box.appendChild(head);
  return box;
}

function sectionFor(sprint, hasActive) {
  const closed = sprint.state === 'closed';
  const box = el('section', `sp-section sp-${sprint.state}`);
  box.dataset.sprint = sprint.id;
  const head = el('div', 'sp-head');
  head.append(el('span', 'sp-kind', sprint.state), el('span', 'sp-title', sprint.name));

  const dates = sprint.start_date ? `${fmtDate(sprint.start_date)} – ${fmtDate(sprint.end_date)}` : `ends ${fmtDate(sprint.end_date)}`;
  head.appendChild(el('span', 'sp-meta', dates));

  const items = cardsFor(sprint.id, closed);
  const doneN = items.filter(isDone).length;
  if (closed) {
    head.appendChild(el('span', 'sp-meta', `committed ${sprint.committed ?? '?'} · completed ${sprint.completed ?? '?'}` +
      (sprint.added ? ` · added mid-sprint ${sprint.added}` : '') +
      (sprint.carried ? ` · carried ${sprint.carried}` : '')));
  } else {
    head.appendChild(el('span', 'sp-meta', `${countLabel(items)} · ${doneN} done`));
    if (sprint.state === 'active' && sprint.added) head.appendChild(el('span', 'sp-meta sp-added-count', `${sprint.added} added mid-sprint`));
    const left = dayNumber(sprint.end_date) - dayNumber(state.today);
    if (sprint.state === 'active') {
      if (left < 0) head.appendChild(el('span', 'sp-meta sp-overdue', `ended ${-left} day${left === -1 ? '' : 's'} ago`));
      else head.appendChild(el('span', 'sp-meta', left === 0 ? 'ends today' : `${left} day${left === 1 ? '' : 's'} left`));
    }
  }
  head.appendChild(el('span', 'sp-spacer'));

  if (sprint.state === 'planned') {
    head.appendChild(button('▶ Start', 'sp-primary', () => run(() => api('POST', `/api/sprint/${sprint.id}/start`)),
      hasActive ? 'Complete the active sprint first' : 'Start this sprint'));
    if (hasActive) head.lastChild.disabled = true;
  }
  if (sprint.state === 'active') head.appendChild(button('✔ Complete', 'sp-primary', () => openComplete(sprint, items)));
  if (!closed) head.appendChild(button('Edit', '', () => openSprintDialog(sprint)));
  if (sprint.state === 'planned') {
    head.appendChild(button('Delete', 'sp-danger', () => {
      if (confirm(`Delete "${sprint.name}"? Its cards go back to the backlog.`)) run(() => api('DELETE', `/api/sprint/${sprint.id}`));
    }));
  }
  if (sprint.goal) head.appendChild(el('span', 'sp-goal', sprint.goal));
  box.appendChild(head);
  box.appendChild(listFor(box, items.filter(matchesProject), sprint.id, closed));
  if (!closed) wireDrop(box, sprint.id);
  return box;
}

function backlogSection() {
  const box = el('section', 'sp-section sp-backlog');
  box.dataset.sprint = 'backlog';
  const items = cardsFor(null, false);
  const head = el('div', 'sp-head');
  head.append(el('span', 'sp-kind', 'Backlog'), el('span', 'sp-title', 'Backlog'),
    el('span', 'sp-meta', countLabel(items)), el('span', 'sp-spacer'));
  head.appendChild(button('＋ Add card', '', () => openCardDialog('top'), 'Add a card at the top of the backlog'));
  box.appendChild(head);
  box.appendChild(listFor(box, items.filter(matchesProject), null, false));
  const bottom = button('＋ Add card', 'sp-add-bottom', () => openCardDialog('bottom'), 'Add a card at the bottom of the backlog');
  box.appendChild(bottom);
  wireDrop(box, 'backlog');
  return box;
}

function listFor(box, items, sprintId, closed) {
  const ul = el('ul', 'sp-list');
  if (!items.length) ul.appendChild(el('li', 'sp-empty', closed ? 'No cards.' : 'Drop cards here.'));
  items.forEach((card) => ul.appendChild(cardRow(card, closed)));
  return ul;
}

function cardRow(card, closed) {
  const li = el('li', 'sp-card' + (isDone(card) ? ' sp-done' : ''));
  li.dataset.id = card.id;
  li.append(el('span', 'sp-card-title', card.title));
  if (card.added_after_start) {
    const badge = el('span', 'sp-pill sp-added', '+ added mid-sprint');
    badge.title = 'Added to this sprint after it started' + (card.added_to_sprint_at ? ' (' + card.added_to_sprint_at + ')' : '');
    li.appendChild(badge);
  }
  if (card.project) {
    // The project colour lives on the pill, next to the name, the same way the status pill carries its column colour.
    const proj = state.projects.find((p) => p.name === card.project);
    const color = (proj && proj.color) || card.color || '#888';
    const pill = el('span', 'sp-pill sp-project', card.project);
    pill.style.background = color;
    pill.style.color = pillTextColor(color);
    li.appendChild(pill);
  }
  if (card.assignee) li.appendChild(el('span', 'sp-assignee', card.assignee));
  const col = state.columns[card.column];
  if (col) {
    const pill = el('span', 'sp-pill sp-status', col.title);
    pill.style.background = col.color || '#888';
    pill.style.color = pillTextColor(col.color || '#888');
    li.appendChild(pill);
  }
  if (!closed) {
    li.draggable = true;
    li.addEventListener('dragstart', (e) => {
      state.dragId = card.id;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', card.id);
      requestAnimationFrame(() => li.classList.add('sp-dragging'));
    });
    li.addEventListener('dragend', () => { state.dragId = null; clearMarks(); });
  }
  return li;
}

// ---------------------------------------------------------------- drag and drop

function clearMarks() {
  document.querySelectorAll('.sp-over').forEach((e) => e.classList.remove('sp-over'));
  document.querySelectorAll('.sp-drop-before,.sp-drop-after,.sp-dragging').forEach((e) => {
    e.classList.remove('sp-drop-before', 'sp-drop-after', 'sp-dragging');
  });
}

// index among the section's cards (excluding the dragged one) where the pointer is
function insertionIndex(box, y) {
  const rows = [...box.querySelectorAll('.sp-card')].filter((r) => r.dataset.id !== state.dragId);
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i].getBoundingClientRect();
    if (y < r.top + r.height / 2) return { index: i, rows };
  }
  return { index: rows.length, rows };
}

function wireDrop(box, target) {
  box.addEventListener('dragover', (e) => {
    if (!state.dragId) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    document.querySelectorAll('.sp-over').forEach((s) => { if (s !== box) s.classList.remove('sp-over'); });
    box.classList.add('sp-over');
    document.querySelectorAll('.sp-drop-before,.sp-drop-after').forEach((x) => x.classList.remove('sp-drop-before', 'sp-drop-after'));
    const { index, rows } = insertionIndex(box, e.clientY);
    if (rows[index]) rows[index].classList.add('sp-drop-before');
    else if (rows.length) rows[rows.length - 1].classList.add('sp-drop-after');
  });
  box.addEventListener('dragleave', (e) => {
    if (!box.contains(e.relatedTarget)) box.classList.remove('sp-over');
  });
  box.addEventListener('drop', (e) => {
    e.preventDefault();
    const id = state.dragId || e.dataTransfer.getData('text/plain');
    const { index, rows } = insertionIndex(box, e.clientY);
    const ids = rows.map((r) => r.dataset.id);
    clearMarks();
    if (id) dropCard(id, target, ids, index);
  });
}

async function dropCard(id, target, neighborIds, index) {
  const order = (cid) => state.cards.find((c) => c.id === cid).order;
  const card = state.cards.find((c) => c.id === id);
  const prev = index > 0 ? order(neighborIds[index - 1]) : null;
  const next = index < neighborIds.length ? order(neighborIds[index]) : null;
  let newOrder = null;
  if (prev !== null && next !== null) newOrder = (prev + next) / 2;
  else if (next !== null) newOrder = next - 1;
  else if (prev !== null) newOrder = prev + 1;

  const sameSprint = (card.sprint || 'backlog') === target;
  const body = {};
  if (!sameSprint) body.sprint = target;
  if (newOrder !== null && newOrder !== card.order) body.order = newOrder;
  if (!Object.keys(body).length) return;
  await run(() => api('PUT', `/api/card/${id}`, body));
}

// ---------------------------------------------------------------- dialogs

// New backlog card: `where` is 'top' or 'bottom' of the backlog.
let newCardWhere = 'bottom';

function openCardDialog(where) {
  newCardWhere = where;
  $('cardDialogTitle').textContent = where === 'top' ? 'New card at the top of the backlog' : 'New card at the bottom of the backlog';
  $('ncTitle').value = '';
  $('ncDescription').value = '';
  $('ncAssignee').value = '';
  const select = $('ncProject');
  select.replaceChildren(new Option('(no project)', ''));
  state.projects.forEach((p) => select.appendChild(new Option(p.name, p.name)));
  select.value = state.project || '';          // an active project filter pre-selects its project
  $('ncError').hidden = true;
  $('cardDialog').showModal();
  $('ncTitle').focus();
}

$('cardForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const title = $('ncTitle').value.trim();
  if (!title) { $('ncError').textContent = 'A title is required.'; $('ncError').hidden = false; return; }
  const body = { title, sprint: 'backlog', column: Object.keys(state.columns)[0] };
  if ($('ncDescription').value.trim()) body.description = $('ncDescription').value.trim();
  if ($('ncAssignee').value.trim()) body.assignee = $('ncAssignee').value.trim();
  if ($('ncProject').value) body.project = $('ncProject').value;
  if (newCardWhere === 'top') {                // just ahead of the first backlog card (all of them, filtered or not)
    const orders = cardsFor(null, false).map((c) => c.order);
    if (orders.length) body.order = Math.min(...orders) - 1;
  }                                            // bottom: the default puts it after everything
  try {
    await api('POST', '/api/card', body);
    $('cardDialog').close();
  } catch (err) {
    $('ncError').textContent = err.message;
    $('ncError').hidden = false;
    return;
  }
  await load();
});

async function run(fn) {
  try {
    await fn();
  } catch (err) {
    alert(err.message);
  }
  await load();
}

let editingSprint = null;

function openSprintDialog(sprint) {
  editingSprint = sprint || null;
  $('sprintDialogTitle').textContent = sprint ? `Edit ${sprint.name}` : 'New sprint';
  $('spSave').textContent = sprint ? 'Save' : 'Create';
  $('spBegin').value = sprint && sprint.start_date ? sprint.start_date : '';
  $('spEnd').value = sprint ? sprint.end_date : addDays(state.today, 6);   // a 7-day sprint counting today
  $('spName').value = sprint ? sprint.name : '';
  $('spGoal').value = sprint ? sprint.goal : '';
  $('spError').hidden = true;
  $('sprintDialog').showModal();
}

$('newSprintBtn').addEventListener('click', () => openSprintDialog(null));
$('showClosed').addEventListener('change', (e) => { state.showClosed = e.target.checked; render(); });
document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => b.closest('dialog').close()));

$('sprintForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = { start_date: $('spBegin').value, end_date: $('spEnd').value, name: $('spName').value.trim(), goal: $('spGoal').value.trim() };
  try {
    if (editingSprint) await api('PUT', `/api/sprint/${editingSprint.id}`, Object.assign(body, { name: body.name || editingSprint.name }));
    else await api('POST', '/api/sprint', body);
    $('sprintDialog').close();
    await load();
  } catch (err) {
    $('spError').textContent = err.message;
    $('spError').hidden = false;
  }
});

let completing = null;
let completingUnfinished = 0;

function openComplete(sprint, items) {
  completing = sprint;
  const unfinished = items.filter((c) => !isDone(c)).length;
  completingUnfinished = unfinished;
  // nothing to carry over: just ask for confirmation
  $('unfinishedFields').hidden = unfinished === 0;
  $('completeConfirm').hidden = unfinished !== 0;
  $('completeTitle').textContent = `Complete ${sprint.name}`;
  $('completeSummary').textContent = `${items.length} cards: ${items.length - unfinished} done, ${unfinished} unfinished.`;
  $('cpEnd').value = addDays(sprint.end_date, 7);
  $('cpBegin').value = '';
  const planned = state.sprints.filter((s) => s.state === 'planned');
  $('cpExisting').replaceChildren(...planned.map((s) => {
    const o = el('option', '', `${s.name} (ends ${fmtDate(s.end_date)})`);
    o.value = s.id;
    return o;
  }));
  document.querySelector('input[name=carryTo][value=existing]').disabled = !planned.length;
  document.querySelector('input[name=carryTo][value=new]').checked = true;
  document.querySelector('input[name=unfinished][value=carry_over]').checked = true;
  $('cpStart').checked = false;
  $('cpError').hidden = true;
  syncCarry();
  $('completeDialog').showModal();
}

function syncCarry() {
  const carry = document.querySelector('input[name=unfinished]:checked').value === 'carry_over';
  document.querySelectorAll('#carryOptions input, #carryOptions select').forEach((i) => {
    if (i.value === 'existing' && !$('cpExisting').options.length) { i.disabled = true; return; }
    i.disabled = !carry;
  });
}
document.querySelectorAll('input[name=unfinished]').forEach((r) => r.addEventListener('change', syncCarry));

$('completeForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const unfinished = completingUnfinished === 0 ? 'carry_over' : document.querySelector('input[name=unfinished]:checked').value;
  const body = { unfinished };
  if (unfinished === 'carry_over' && completingUnfinished > 0) {
    if (document.querySelector('input[name=carryTo]:checked').value === 'existing') body.next_sprint = $('cpExisting').value;
    else {
      body.next_end_date = $('cpEnd').value;
      if ($('cpBegin').value) body.next_start_date = $('cpBegin').value;
    }
    body.start_next = $('cpStart').checked;
  }
  try {
    await api('POST', `/api/sprint/${completing.id}/complete`, body);
    $('completeDialog').close();
    await load();
  } catch (err) {
    $('cpError').textContent = err.message;
    $('cpError').hidden = false;
  }
});

load().catch((err) => { $('sprintsMain').textContent = err.message; });

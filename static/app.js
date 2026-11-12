// Tag every change made from the browser so card history can tell the UI from API/MCP callers.
(function(){
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (url, opts) => {
    if (typeof url === 'string' && url.startsWith('/api/') && opts && opts.method && opts.method !== 'GET') {
      opts = Object.assign({}, opts, {headers: Object.assign({'X-Perkan-Source': 'ui'}, opts.headers || {})});
    }
    return nativeFetch(url, opts);
  };
})();
// Day of an ISO timestamp for display. Backfilled dates are midnight UTC, so show them as written;
// real timestamps show in the viewer's local time.
function fmtDay(iso){
  if(!iso) return '';
  if(iso.endsWith('T00:00:00Z')) return iso.slice(0, 10);
  return new Date(iso).toLocaleDateString('sv-SE');
}
// Compact age of a timestamp: 5m, 3h, 12d.
function fmtAge(iso){
  const mins = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60000));
  if(mins < 60) return mins + 'm';
  if(mins < 60 * 24) return Math.floor(mins / 60) + 'h';
  return Math.floor(mins / (60 * 24)) + 'd';
}
function fmtDateTime(iso){
  if(!iso) return '';
  if(iso.endsWith('T00:00:00Z')) return iso.slice(0, 10);
  return new Date(iso).toLocaleString('sv-SE').slice(0, 16);
}
let latestBoard = null;
let activeProjectFilter = null;
let cardTruncationEnabled = localStorage.getItem('cardTruncationEnabled') !== 'false';
// Cards the user flipped to the opposite of the global truncation mode. Kept in memory only: a reload or
// the header Truncate button puts every card back on the global mode.
const flippedCards = new Set();
function cardIsTruncated(id){ return cardTruncationEnabled !== flippedCards.has(id); }

// "Expand vertical" arrows pointing away from a centre line; collapse is the same with the arrows reversed.
function expandIconSvg(expand){
  const arrows = expand
    ? 'M8 6V2M5.5 4.5L8 2l2.5 2.5M8 10v4M5.5 11.5L8 14l2.5-2.5'
    : 'M8 2v4M5.5 3.5L8 6l2.5-2.5M8 14v-4M5.5 12.5L8 10l2.5 2.5';
  return `<svg class="expand-icon" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8h10"/><path d="${arrows}"/></svg>`;
}
function cardToggleLabel(truncated){ return truncated ? 'Expand this card' : 'Collapse this card'; }
let sprintOnlyEnabled = localStorage.getItem('sprintOnlyEnabled') === 'true';
let draggedCardId = null;
let draggedCardColumnId = null;
async function failMessage(res, fallback){
  try { const body = await res.json(); if(body && body.error) return body.error; } catch(e) {}
  return fallback;
}
async function fetchBoard() {
  const res = await fetch('/api/board?include_hidden=true');
  latestBoard = await res.json();
  return latestBoard;
}

function hexToRgba(hex, alpha){
  // accepts #rrggbb or #rgb
  if(!hex) return '';
  hex = hex.replace('#','');
  if(hex.length === 3){
    hex = hex.split('').map(c=>c+c).join('');
  }
  const r = parseInt(hex.slice(0,2),16);
  const g = parseInt(hex.slice(2,4),16);
  const b = parseInt(hex.slice(4,6),16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function sanitizeProjectColor(value){
  if(typeof value !== 'string'){
    return '#5b2e8a';
  }
  const trimmed = value.trim();
  if(/^#[0-9a-fA-F]{3}$/.test(trimmed) || /^#[0-9a-fA-F]{6}$/.test(trimmed)){
    return trimmed;
  }
  return '#5b2e8a';
}

function collectProjectCounts(board){
  const counts = new Map();
  if(!board || !Array.isArray(board.columns)){
    return counts;
  }
  board.columns.forEach(col => {
    if(col && col.hidden){
      return;
    }
    if(!Array.isArray(col.cards)) return;
    col.cards.forEach(card => {
      const projectName = card && card.project ? card.project.trim() : '';
      if(!projectName) return;
      counts.set(projectName, (counts.get(projectName) || 0) + 1);
    });
  });
  return counts;
}

// keep descriptions as plain escaped text — markdown/link parsing is intentionally disabled

let dropZonesVisible = false;

function makeDropZone(beforeOrder, afterOrder, colId, isSameCol){
  const zone = document.createElement('div');
  zone.className = 'drop-zone' + (isSameCol ? ' drop-zone-reorder' : '') + ' visible';
  if(isSameCol) zone.textContent = 'Drop here';
  if(beforeOrder !== null && !isNaN(beforeOrder)) zone.dataset.beforeOrder = beforeOrder;
  if(afterOrder !== null && !isNaN(afterOrder)) zone.dataset.afterOrder = afterOrder;
  zone.dataset.colId = colId;
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    zone.classList.remove('dragover');
    hideDropZones();
    const id = e.dataTransfer.getData('text/plain') || e.dataTransfer.getData('text') || draggedCardId;
    if(!id) return;
    if(isSameCol){
      const before = zone.dataset.beforeOrder !== undefined ? parseFloat(zone.dataset.beforeOrder) : null;
      const after = zone.dataset.afterOrder !== undefined ? parseFloat(zone.dataset.afterOrder) : null;
      let newOrder;
      if(before === null && after === null) newOrder = 1000000;
      else if(before === null) newOrder = parseFloat(zone.dataset.afterOrder) - 1000000;
      else if(after === null) newOrder = parseFloat(zone.dataset.beforeOrder) + 1000000;
      else newOrder = (parseFloat(zone.dataset.beforeOrder) + parseFloat(zone.dataset.afterOrder)) / 2;
      const res = await fetch('/api/card/' + id, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({order: newOrder})
      });
      if(!res.ok){ alert('Unable to reorder card. Please try again.'); render(); return; }
    } else {
      const res = await fetch('/api/card/' + id, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({column: colId})
      });
      if(!res.ok){ alert(await failMessage(res, 'Unable to move card. Please try again.')); render(); return; }
    }
    draggedCardId = null;
    draggedCardColumnId = null;
    render();
  });
  return zone;
}

function showDropZones(){
  if(dropZonesVisible) return;
  dropZonesVisible = true;
  document.querySelectorAll('.card-list').forEach(list => {
    const colEl = list.closest('[data-column]');
    const colId = colEl ? colEl.dataset.column : null;
    if(colId && colId === draggedCardColumnId){
      if(list.querySelector('.drop-zone')) return;
      // origin column: zones at top, between all cards (including placeholder), and at bottom
      const cards = Array.from(list.querySelectorAll('.card'));
      if(cards.length === 0){
        list.prepend(makeDropZone(null, null, colId, true));
      } else {
        list.prepend(makeDropZone(null, parseFloat(cards[0].dataset.order), colId, true));
        for(let i = 0; i < cards.length - 1; i++){
          const z = makeDropZone(
            parseFloat(cards[i].dataset.order),
            parseFloat(cards[i+1].dataset.order),
            colId, true
          );
          cards[i].insertAdjacentElement('afterend', z);
        }
        list.appendChild(makeDropZone(parseFloat(cards[cards.length-1].dataset.order), null, colId, true));
      }
    } else if(colEl && !colEl.querySelector('.drop-zone-overlay')){
      // foreign column: semi-transparent overlay covering the whole column
      const zone = document.createElement('div');
      zone.className = 'drop-zone drop-zone-overlay visible';
      zone.textContent = 'Move here';
      zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
      zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
      zone.addEventListener('drop', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.remove('dragover');
        hideDropZones();
        const id = e.dataTransfer.getData('text/plain') || e.dataTransfer.getData('text') || draggedCardId;
        if(!id) return;
        const res = await fetch('/api/card/' + id, {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({column: colId})
        });
        if(!res.ok){ alert(await failMessage(res, 'Unable to move card. Please try again.')); render(); return; }
        draggedCardId = null;
        draggedCardColumnId = null;
        render();
      });
      colEl.appendChild(zone);
    }
  });
}

function hideDropZones(){
  if(!dropZonesVisible) return;
  dropZonesVisible = false;
  document.querySelectorAll('.drop-zone').forEach(zone => zone.remove());
}

const linkPreview = document.createElement('div');
linkPreview.id = 'cardLinkPreview';
linkPreview.className = 'card-link-popup hidden';
document.body.appendChild(linkPreview);

let linkPreviewHideTimer = null;

function cancelLinkPreviewHide(){
  if(linkPreviewHideTimer){
    clearTimeout(linkPreviewHideTimer);
    linkPreviewHideTimer = null;
  }
}

function hideLinkPreviewImmediate(){
  cancelLinkPreviewHide();
  if(!linkPreview.classList.contains('hidden')){
    linkPreview.classList.add('hidden');
  }
}

function scheduleLinkPreviewHide(){
  cancelLinkPreviewHide();
  linkPreviewHideTimer = setTimeout(()=>{
    linkPreview.classList.add('hidden');
  }, 120);
}

function showLinkPreview(links, anchorEl){
  if(!Array.isArray(links) || !links.length){
    hideLinkPreviewImmediate();
    return;
  }
  cancelLinkPreviewHide();
  const items = links.map(link => {
    const safeText = escapeHtml((link && (link.text || link.url)) || 'Link');
    const safeUrl = escapeHtml((link && link.url) || '#');
    return `<li><a href="${safeUrl}" target="_blank" rel="noopener noreferrer">${safeText}</a></li>`;
  }).join('');
  linkPreview.innerHTML = `<ul>${items}</ul>`;
  const rect = anchorEl.getBoundingClientRect();
  linkPreview.style.top = `${window.scrollY + rect.bottom + 6}px`;
  linkPreview.style.left = `${window.scrollX + rect.left}px`;
  linkPreview.classList.remove('hidden');
}

linkPreview.addEventListener('mouseenter', cancelLinkPreviewHide);
linkPreview.addEventListener('mouseleave', scheduleLinkPreviewHide);

const projectPreview = document.createElement('div');
projectPreview.id = 'projectPreview';
projectPreview.className = 'project-info-popup hidden';
document.body.appendChild(projectPreview);

let projectPreviewHideTimer = null;

function cancelProjectPreviewHide(){
  if(projectPreviewHideTimer){
    clearTimeout(projectPreviewHideTimer);
    projectPreviewHideTimer = null;
  }
}

function hideProjectPreviewImmediate(){
  cancelProjectPreviewHide();
  if(!projectPreview.classList.contains('hidden')){
    projectPreview.classList.add('hidden');
  }
}

function scheduleProjectPreviewHide(){
  cancelProjectPreviewHide();
  projectPreviewHideTimer = setTimeout(()=>{
    projectPreview.classList.add('hidden');
  }, 120);
}

function showProjectPreview(projectDetails, anchorEl){
  if(!projectDetails){
    hideProjectPreviewImmediate();
    return;
  }
  cancelProjectPreviewHide();
  const rawName = projectDetails.name || '';
  const projectName = escapeHtml(rawName || 'Untitled project');
  const projectColor = escapeHtml(projectDetails.color || '#5b2e8a');
  projectPreview.dataset.project = rawName;
  projectPreview.innerHTML = `
    <div class="project-preview-top">
      <span class="project-preview-dot" style="background:${projectColor};"></span>
      <div class="project-preview-text">
        <div class="project-preview-name">${projectName}</div>
        <div class="project-preview-color">${projectColor}</div>
      </div>
      <button type="button" class="project-preview-edit" title="Edit project">✏️</button>
    </div>
  `;
  const rect = anchorEl.getBoundingClientRect();
  projectPreview.style.top = `${window.scrollY + rect.bottom + 6}px`;
  projectPreview.style.left = `${window.scrollX + rect.left}px`;
  projectPreview.classList.remove('hidden');
}

projectPreview.addEventListener('mouseenter', cancelProjectPreviewHide);
projectPreview.addEventListener('mouseleave', scheduleProjectPreviewHide);
projectPreview.addEventListener('click', (e) => {
  if(e.target.closest('.project-preview-edit')){
    const projectName = projectPreview.dataset.project || '';
    hideProjectPreviewImmediate();
    window.location.href = '/settings?tab=projects&project=' + encodeURIComponent(projectName);
  }
});

function createCardElement(card, projectMap) {
  const linkCount = Array.isArray(card.links) ? card.links.length : 0;
  const projectName = card.project || '';
  const assignee = card.assignee || '';
  const projectDetails = projectMap && projectMap.get(projectName);
  const projectColor = projectDetails && projectDetails.color ? projectDetails.color : null;
  const el = document.createElement('div');
  const truncated = cardIsTruncated(card.id);
  el.className = 'card' + (truncated ? ' card-truncated' : '') + (flippedCards.has(card.id) ? ' card-flipped' : '');
  el.draggable = true;
  el.dataset.id = card.id;
  el.dataset.order = card.order !== undefined ? card.order : 0;
  const dateLine = [card.started_at ? 'Started ' + fmtDay(card.started_at) : '',
                    card.completed_at ? 'Completed ' + fmtDay(card.completed_at) : ''].filter(Boolean).join(' · ');
  el.innerHTML = `
    <div class="card-title">${escapeHtml(card.title)}</div>
    <div class="card-desc">${escapeHtmlWithBr(card.description || '')}</div>
    ${dateLine ? `<div class="card-dates">${dateLine}</div>` : ''}
    ${card.added_after_start ? `<div class="card-added" title="Added to its sprint after the sprint started${card.added_to_sprint_at ? ' (' + fmtDateTime(card.added_to_sprint_at) + ')' : ''}">＋ added mid-sprint</div>` : ''}
    <div class="card-footer">
      <div class="card-footer-left">
        ${linkCount > 0 ? `<div class="card-links" title="${linkCount} link${linkCount!==1?'s':''}">🔗 ${linkCount}</div>` : ''}
        ${card.column_since ? `<div class="card-age" title="In this column since ${fmtDateTime(card.column_since)}">⏱ ${fmtAge(card.column_since)}</div>` : ''}
      </div>
      <div class="card-footer-center">
        ${projectName || assignee ? `
          <div class="card-footer-center-meta">
            ${projectName ? `<div class="card-project" title="Project: ${escapeHtml(projectName)}">${escapeHtml(projectName)}</div>` : ''}
            ${assignee ? `<div class="card-assignee" title="Assignee: ${escapeHtml(assignee)}">👤 ${escapeHtml(assignee)}</div>` : ''}
          </div>
        ` : ''}
      </div>
      <div class="card-footer-right">
        <button type="button" class="card-toggle" title="${cardToggleLabel(truncated)}" aria-label="${cardToggleLabel(truncated)}">${expandIconSvg(truncated)}</button>
        <button class="edit" title="Edit card">✏️</button>
      </div>
    </div>
  `;

  const cardColor = projectColor || card.color || '#5b2e8a';
  el.style.borderColor = readableOnDark(cardColor, 3);
  el.style.borderWidth = '2px';
  el.style.backgroundColor = hexToRgba(cardColor, 0.06);
  if(projectColor){
    el.style.setProperty('--project-pill-border', readableOnDark(projectColor, 3));
    el.style.setProperty('--project-pill-text', readableOnDark(projectColor));
    el.style.setProperty('--project-pill-bg', hexToRgba(projectColor, 0.12));
  } else {
    el.style.removeProperty('--project-pill-border');
    el.style.removeProperty('--project-pill-text');
    el.style.removeProperty('--project-pill-bg');
  }

  el.addEventListener('dragstart', (e) => {
    draggedCardId = card.id;
    const colEl = el.closest('[data-column]');
    draggedCardColumnId = colEl ? colEl.dataset.column : null;
    e.dataTransfer.setData('text/plain', card.id);
    e.dataTransfer.setData('text', card.id);
    e.dataTransfer.effectAllowed = 'move';
    setTimeout(() => {
      el.classList.add('dragging');
      el.addEventListener('dragover', (e2) => { e2.preventDefault(); el.classList.add('dragover-placeholder'); });
      el.addEventListener('dragleave', () => el.classList.remove('dragover-placeholder'));
      el.addEventListener('drop', (e2) => {
        e2.preventDefault();
        e2.stopPropagation();
        el.classList.remove('dragover-placeholder');
        hideDropZones();
        draggedCardId = null;
        draggedCardColumnId = null;
        render();
      });
      showDropZones();
    }, 0);
  });
  el.addEventListener('dragend', () => {
    el.classList.remove('dragging');
    el.classList.remove('dragover-placeholder');
    draggedCardId = null;
    draggedCardColumnId = null;
    hideDropZones();
  });

  el.querySelector('.card-toggle').addEventListener('click', (ev) => {
    ev.stopPropagation();
    if(flippedCards.has(card.id)) flippedCards.delete(card.id); else flippedCards.add(card.id);
    const nowTruncated = cardIsTruncated(card.id);
    el.classList.toggle('card-truncated', nowTruncated);
    el.classList.toggle('card-flipped', flippedCards.has(card.id));
    const toggle = ev.currentTarget;
    toggle.innerHTML = expandIconSvg(nowTruncated);
    toggle.title = cardToggleLabel(nowTruncated);
    toggle.setAttribute('aria-label', cardToggleLabel(nowTruncated));
  });

  el.querySelector('.edit').addEventListener('click', async (ev) => {
    ev.stopPropagation();
    await openCardEditModal(card);
  });

  const projectBadge = el.querySelector('.card-project');
  if(projectBadge && projectName){
    projectBadge.addEventListener('mouseenter', () => {
      const previewDetails = projectDetails || {name: projectName, color: projectColor || card.color || '#5b2e8a'};
      showProjectPreview(previewDetails, projectBadge);
    });
    projectBadge.addEventListener('mouseleave', () => {
      scheduleProjectPreviewHide();
    });
  }

  const linkBadge = el.querySelector('.card-links');
  if(linkBadge){
    linkBadge.addEventListener('mouseenter', () => {
      showLinkPreview(card.links, linkBadge);
    });
    linkBadge.addEventListener('mouseleave', () => {
      scheduleLinkPreviewHide();
    });
  }
  return el;
}

function escapeHtml(s){
  return s.replace(/[&<>"']/g, function(m){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;'}[m];});
}

function escapeHtmlWithBr(s){
  const escaped = escapeHtml(s);
  return escaped.replace(/\n/g, '<br />');
}

function renderProjectFilters(board, projectMap){
  const container = document.getElementById('projectFilters');
  if(!container){
    return;
  }
  const projectCounts = collectProjectCounts(board);
  if(activeProjectFilter && !projectCounts.has(activeProjectFilter)){
    activeProjectFilter = null;
  }
  container.innerHTML = '';
  if(projectCounts.size === 0){
    const placeholder = document.createElement('div');
    placeholder.className = 'project-filters-empty';
    placeholder.textContent = 'Tag tasks with a project to enable filtering.';
    container.appendChild(placeholder);
    return;
  }

  const seen = new Set();
  const orderedNames = [];
  if(Array.isArray(board.projects)){
    board.projects.forEach(proj => {
      const name = proj && proj.name ? proj.name.trim() : '';
      if(name && projectCounts.has(name) && !seen.has(name)){
        orderedNames.push(name);
        seen.add(name);
      }
    });
  }
  projectCounts.forEach((_, name) => {
    if(!seen.has(name)){
      orderedNames.push(name);
      seen.add(name);
    }
  });

  orderedNames.forEach(name => {
    const count = projectCounts.get(name) || 0;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'project-filter-pill' + (activeProjectFilter === name ? ' active' : '');
    button.dataset.project = name;
    button.setAttribute('aria-pressed', activeProjectFilter === name ? 'true' : 'false');

    const projectDetails = projectMap ? projectMap.get(name) : null;
    const baseColor = projectDetails && projectDetails.color ? projectDetails.color : '#5b2e8a';
    const safeColor = sanitizeProjectColor(baseColor);

    const dot = document.createElement('span');
    dot.className = 'project-filter-pill-dot';
    dot.style.backgroundColor = readableOnDark(safeColor, 3);

    const label = document.createElement('span');
    label.className = 'project-filter-pill-label';
    label.textContent = `${name} (${count})`;

    button.appendChild(dot);
    button.appendChild(label);

    button.addEventListener('click', () => {
      activeProjectFilter = activeProjectFilter === name ? null : name;
      render({useLatest: true});
    });

    container.appendChild(button);
  });
}

async function render(options = {}){
  const useLatest = !!options.useLatest;
  const board = useLatest && latestBoard ? latestBoard : await fetchBoard();
  const boardEl = document.getElementById('board');
  const projectMap = new Map();
  if(Array.isArray(board.projects)){
    board.projects.forEach(proj => {
      if(proj && proj.name){
        projectMap.set(proj.name, proj);
      }
    });
  }

  renderProjectFilters(board, projectMap);

  hideDropZones();
  hideLinkPreviewImmediate();
  hideProjectPreviewImmediate();
  boardEl.innerHTML = '';
  let lastSlot = null;    // the last element placed straight on the board: a column, or a stack of columns
  let lastStacked = false; // whether the last visible column is stacked (so the next stacked one joins it)
  let stackEl = null;     // the stack wrapper when lastSlot already holds a stack
  for(const col of board.columns){
    if(col.hidden){
      continue;
    }
    const cardsInColumn = Array.isArray(col.cards) ? col.cards : [];
    const filtering = !!activeProjectFilter || sprintOnlyEnabled;
    const visibleCards = !filtering
      ? cardsInColumn
      : cardsInColumn.filter(card => {
          if(sprintOnlyEnabled && !(board.active_sprint && card && card.sprint === board.active_sprint)){
            return false;
          }
          if(activeProjectFilter){
            const projectName = card && card.project ? card.project.trim() : '';
            return projectName === activeProjectFilter;
          }
          return true;
        });
    const colEl = document.createElement('div');
    colEl.className = 'column';
    colEl.dataset.column = col.id;

    // apply color styling
    const outline = col.color || '#e0e0e0';
    colEl.style.borderColor = readableOnDark(outline, 3);
    colEl.style.borderWidth = '2px';
    colEl.style.backgroundColor = hexToRgba(outline, 0.06);

    const header = document.createElement('div');
    header.className = 'col-header';
    const visibleCount = visibleCards.length;
    const totalCount = cardsInColumn.length;
    const countLabel = filtering ? `${visibleCount}/${totalCount || 0}` : `${visibleCount}`;
    header.textContent = `${col.title} (${countLabel})`;
    colEl.appendChild(header);

    const list = document.createElement('div');
    list.className = 'card-list';
    list.addEventListener('dragover', (e)=>{e.preventDefault();});
    list.addEventListener('drop', async (e)=>{
      e.preventDefault();
      hideDropZones();
      // same-column reorders are handled by the individual drop zones
      if(col.id === draggedCardColumnId) return;
      const id = e.dataTransfer.getData('text/plain') || e.dataTransfer.getData('text') || draggedCardId;
      if(!id){
        return;
      }
      const res = await fetch('/api/card/' + id, {
        method: 'PUT',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({column: col.id})
      });
      if(!res.ok){
        alert(await failMessage(res, 'Unable to move card. Please try again.'));
        return;
      }
      draggedCardId = null;
      draggedCardColumnId = null;
      render();
    });

    const sortedCards = [...visibleCards].sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
    for(const card of sortedCards){
      list.appendChild(createCardElement(card, projectMap));
    }

    colEl.appendChild(list);

    // per-column add button (bottom-right)
    const addBtn = document.createElement('button');
    addBtn.className = 'col-add';
    addBtn.title = 'Add card';
    addBtn.textContent = '+';
    addBtn.addEventListener('click', ()=>{
      openCardModal(col.id);
    });
    colEl.appendChild(addBtn);

    if(col.stacked && lastSlot && lastStacked){
      // stacked columns that sit next to each other share one horizontal slot (hidden columns are skipped)
      if(!stackEl){
        stackEl = document.createElement('div');
        stackEl.className = 'column-stack';
        boardEl.replaceChild(stackEl, lastSlot);
        stackEl.appendChild(lastSlot);
        lastSlot = stackEl;
      }
      stackEl.appendChild(colEl);
    } else {
      boardEl.appendChild(colEl);
      lastSlot = colEl;
      stackEl = null;
    }
    lastStacked = !!col.stacked;
  }

  refreshProjectInputs();
}

// Global add removed: use per-column + buttons to add cards.

const projectPickerRegistry = new Map();
let projectPickerDocListenerAttached = false;

function getProjectNames(){
  if(!latestBoard) return [];
  const seen = new Set();
  const names = [];
  if(Array.isArray(latestBoard.projects)){
    latestBoard.projects.forEach(proj => {
      const name = proj && proj.name ? proj.name.trim() : '';
      if(name && !seen.has(name)){
        seen.add(name);
        names.push(name);
      }
    });
  }
  if(Array.isArray(latestBoard.columns)){
    latestBoard.columns.forEach(col => {
      if(!Array.isArray(col.cards)) return;
      col.cards.forEach(card => {
        const projectName = card && card.project ? card.project.trim() : '';
        if(projectName && !seen.has(projectName)){
          seen.add(projectName);
          names.push(projectName);
        }
      });
    });
  }
  return names;
}

function populateProjectInput(inputEl, selectedValue){
  if(!inputEl) return;
  inputEl.value = selectedValue || '';
  const state = projectPickerRegistry.get(inputEl.id);
  if(state){
    buildProjectPickerMenu(state);
  }
}

function buildProjectPickerMenu(state){
  if(!state) return;
  const names = getProjectNames();
  const filter = (state.input.value || '').trim().toLowerCase();
  const ordered = [];
  if(filter){
    const matches = [];
    const remainder = [];
    names.forEach(name => {
      const match = name.toLowerCase().includes(filter);
      (match ? matches : remainder).push({name, match});
    });
    ordered.push(...matches, ...remainder);
  } else {
    names.forEach(name => ordered.push({name, match:false}));
  }
  state.menu.innerHTML = '';
  if(!ordered.length){
    const empty = document.createElement('div');
    empty.className = 'project-option empty';
    empty.textContent = 'No saved projects yet';
    state.menu.appendChild(empty);
    return;
  }
  ordered.forEach(item => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'project-option' + (item.match ? ' match' : '');
    btn.textContent = item.name;
    btn.addEventListener('click', () => {
      state.input.value = item.name;
      state.input.dispatchEvent(new Event('input', {bubbles:true}));
      closeProjectPicker(state);
      state.skipNextFocusOpen = true;
      setTimeout(() => state.input.focus(), 0);
    });
    state.menu.appendChild(btn);
  });
}

function openProjectPicker(state){
  if(!state) return;
  buildProjectPickerMenu(state);
  state.menu.classList.remove('hidden');
  state.toggle.setAttribute('aria-expanded', 'true');
  state.open = true;
}

function closeProjectPicker(state){
  if(!state) return;
  state.menu.classList.add('hidden');
  state.toggle.setAttribute('aria-expanded', 'false');
  state.open = false;
  state.suppressBlur = false;
}

function registerProjectPicker(inputId){
  if(projectPickerRegistry.has(inputId)) return projectPickerRegistry.get(inputId);
  const wrapper = document.querySelector(`.project-picker[data-project-input="${inputId}"]`);
  const input = document.getElementById(inputId);
  if(!wrapper || !input) return null;
  const menu = wrapper.querySelector(`[data-project-menu="${inputId}"]`);
  const toggle = wrapper.querySelector(`[data-project-toggle="${inputId}"]`);
  if(!menu || !toggle) return null;
  const state = {input, menu, toggle, wrapper, open:false, suppressBlur:false, skipNextFocusOpen:false};
  projectPickerRegistry.set(inputId, state);

  input.addEventListener('focus', () => {
    if(state.skipNextFocusOpen){
      state.skipNextFocusOpen = false;
      return;
    }
    openProjectPicker(state);
  });
  input.addEventListener('input', () => {
    buildProjectPickerMenu(state);
    if(!state.open){
      openProjectPicker(state);
    }
  });
  input.addEventListener('keydown', (e) => {
    if(e.key === 'ArrowDown'){
      e.preventDefault();
      openProjectPicker(state);
      focusFirstProjectOption(state);
    } else if(e.key === 'Escape'){
      closeProjectPicker(state);
    }
  });
  input.addEventListener('blur', () => {
    if(state.suppressBlur) return;
    setTimeout(() => {
      if(!state.suppressBlur){
        closeProjectPicker(state);
      }
    }, 120);
  });

  toggle.addEventListener('click', (e) => {
    e.preventDefault();
    if(state.open){
      closeProjectPicker(state);
    } else {
      openProjectPicker(state);
      input.focus();
    }
  });

  menu.addEventListener('mousedown', (e) => {
    state.suppressBlur = true;
    e.stopPropagation();
  });
  menu.addEventListener('mouseup', (e) => {
    e.stopPropagation();
    setTimeout(() => {
      state.suppressBlur = false;
      input.focus();
    }, 0);
  });

  if(!projectPickerDocListenerAttached){
    document.addEventListener('mousedown', (evt) => {
      projectPickerRegistry.forEach(pickerState => {
        if(pickerState.open && !pickerState.wrapper.contains(evt.target)){
          closeProjectPicker(pickerState);
        }
      });
    });
    projectPickerDocListenerAttached = true;
  }

  return state;
}

function focusFirstProjectOption(state){
  if(!state || !state.menu) return;
  const firstBtn = state.menu.querySelector('button.project-option');
  if(firstBtn){
    firstBtn.focus();
  }
}

function refreshProjectInputs(){
  projectPickerRegistry.forEach(state => buildProjectPickerMenu(state));
}

const sprintOnlyBtn = document.getElementById('sprintOnlyBtn');
if(sprintOnlyBtn && document.getElementById('board')){
  const syncSprintOnlyBtn = ()=>{
    sprintOnlyBtn.setAttribute('aria-pressed', sprintOnlyEnabled ? 'true' : 'false');
    sprintOnlyBtn.classList.toggle('active', sprintOnlyEnabled);
  };
  syncSprintOnlyBtn();
  sprintOnlyBtn.addEventListener('click', ()=>{
    sprintOnlyEnabled = !sprintOnlyEnabled;
    localStorage.setItem('sprintOnlyEnabled', sprintOnlyEnabled);
    syncSprintOnlyBtn();
    render({useLatest: true});
  });
}

// The header button shows what a click does, like the per-card one: expand while truncating, collapse otherwise.
function setTruncateBtnLabel(){
  const btn = document.getElementById('truncateBtn');
  if(btn) btn.innerHTML = expandIconSvg(cardTruncationEnabled) + '<span>Truncate</span>';
}

const truncateBtn = document.getElementById('truncateBtn');
if(truncateBtn){
  // Only attach listener if we're on the board view (boardEl exists)
  const boardEl = document.getElementById('board');
  if (boardEl) {
    truncateBtn.addEventListener('click', ()=>{
      cardTruncationEnabled = !cardTruncationEnabled;
      flippedCards.clear();                      // the global mode wins again for every card
      localStorage.setItem('cardTruncationEnabled', cardTruncationEnabled);
      const btn = document.getElementById('truncateBtn');
      btn.setAttribute('aria-pressed', cardTruncationEnabled ? 'true' : 'false');
      btn.classList.toggle('active', cardTruncationEnabled);
      setTruncateBtnLabel();
      render({useLatest: true});
    });
  }
}

// close modal when clicking backdrop
document.addEventListener('click', (e)=>{
  // card modal backdrop
  const cardModal = document.getElementById('cardModal');
  if(cardModal && e.target.classList.contains('modal-backdrop')) cardModal.classList.add('hidden');
  // card edit modal backdrop
  const cardEditModal = document.getElementById('cardEditModal');
  if(cardEditModal && e.target.classList.contains('modal-backdrop')) cardEditModal.classList.add('hidden');
  // split task modal backdrop
  const splitTaskModal = document.getElementById('splitTaskModal');
  if(splitTaskModal && e.target.classList.contains('modal-backdrop')) closeSplitTaskModal();
});

// Card edit modal
const cardEditModal = document.createElement('div');
cardEditModal.id = 'cardEditModal';
cardEditModal.className = 'modal hidden';
cardEditModal.innerHTML = `
  <div class="modal-backdrop"></div>
  <div class="modal-content">
    <h2 id="editCardModalTitle">Edit Task</h2>
    <div class="card-form">
      <label>Title:</label>
      <input id="editCardTitle" placeholder="Card title" />
      <label>Project:</label>
      <div class="project-picker" data-project-input="editCardProject">
        <input id="editCardProject" class="modal-input project-input" placeholder="Select or type a project" autocomplete="off" />
        <button type="button" class="project-picker-toggle" data-project-toggle="editCardProject" aria-label="Show saved projects">▼</button>
        <div class="project-picker-dropdown hidden" data-project-menu="editCardProject"></div>
      </div>
      <label>Assignee:</label>
      <input id="editCardAssignee" class="modal-input" placeholder="Assignee (optional)" />
      <label>Description:</label>
      <textarea id="editCardDesc" placeholder="Description (optional)"></textarea>
      <div id="editCardTimeline" class="timeline-section" style="display:none"></div>
      <div class="links-section">
        <div class="links-header">
          <span>Links</span>
          <button type="button" id="addLinkRowBtn">+ Link</button>
        </div>
        <div id="linkRowsContainer" class="link-rows"></div>
        <small class="link-hint">Add shortcuts to docs, tickets, or other resources. Leave both fields empty to remove a row.</small>
      </div>
      <div class="modal-actions"><button id="editCardSaveBtn">Save</button> <button id="editCardSplitBtn" style="display:none" title="Split this task into two">Split</button> <button id="editCardDuplicateBtn" style="display:none">Duplicate</button> <button id="editCardDeleteBtn" class="delete-btn" style="display:none">Delete</button> <button id="editCardCancelBtn">Cancel</button></div>
    </div>
  </div>
`;
document.body.appendChild(cardEditModal);
registerProjectPicker('editCardProject');

function createLinkRowElement(link, containerRef){
  const row = document.createElement('div');
  row.className = 'link-row';
  row.innerHTML = `
    <input class="link-text" placeholder="Display text" />
    <input class="link-url" placeholder="https://example.com" />
    <a class="link-visit" target="_blank" rel="noopener noreferrer">Visit</a>
    <button type="button" class="link-remove">✕</button>
  `;
  const textValue = link && link.text ? link.text : '';
  const urlValue = link && link.url ? link.url : '';
  row.querySelector('.link-text').value = textValue;
  row.querySelector('.link-url').value = urlValue;
  const visitAnchor = row.querySelector('.link-visit');
  const updateVisitState = () => {
    const currentUrl = row.querySelector('.link-url').value.trim();
    if(currentUrl){
      visitAnchor.classList.remove('disabled');
      visitAnchor.href = currentUrl;
    } else {
      visitAnchor.classList.add('disabled');
      visitAnchor.removeAttribute('href');
    }
  };
  updateVisitState();
  row.querySelector('.link-url').addEventListener('input', updateVisitState);
  row.querySelector('.link-remove').addEventListener('click', () => {
    row.remove();
    const container = containerRef || document.getElementById('linkRowsContainer');
    if(container && !container.querySelector('.link-row')){
      container.appendChild(createLinkRowElement({}, container));
    }
  });
  return row;
}

function renderLinkRows(links){
  const container = document.getElementById('linkRowsContainer');
  if(!container) return;
  container.innerHTML = '';
  if(links && links.length){
    links.forEach(link => container.appendChild(createLinkRowElement(link)));
  } else {
    container.appendChild(createLinkRowElement({}));
  }
}

function collectLinkRows(){
  const container = document.getElementById('linkRowsContainer');
  if(!container) return [];
  const links = [];
  container.querySelectorAll('.link-row').forEach(row => {
    const text = row.querySelector('.link-text').value.trim();
    const url = row.querySelector('.link-url').value.trim();
    if(!text && !url) return;
    if(!url) return;
    links.push({text: text || url, url});
  });
  return links;
}

document.getElementById('addLinkRowBtn').addEventListener('click', () => {
  const container = document.getElementById('linkRowsContainer');
  if(container){
    container.appendChild(createLinkRowElement({}));
  }
});
// 268 -> "4 h 28 m", 0 -> "< 1 min", 1500 -> "1 d 1 h"
function fmtDuration(min){
  if(min < 1) return '< 1 min';
  const d = Math.floor(min / 1440), h = Math.floor((min % 1440) / 60), m = min % 60;
  if(d) return h ? `${d} d ${h} h` : `${d} d`;
  if(h) return m ? `${h} h ${m} m` : `${h} h`;
  return `${m} m`;
}

// "Time in each column": wall-clock time per column from the card's history (not active effort)
function timeInColumnsHtml(t){
  if(!t) return '';
  if(!t.known){
    return '<div class="timeline-times"><small class="link-hint">Time in each column is unknown: no recorded column history.</small></div>';
  }
  const hasWork = t.columns.some(c => c.counts_as_work);
  const rows = t.columns.map(c =>
    `<li><span class="tt-col">${escapeHtml(c.title)}${c.counts_as_work ? ' <span class="tt-work" title="Counts as work">work</span>' : ''}</span>` +
    `<span class="tt-val">${fmtDuration(c.minutes)}${c.stays > 1 ? ` <span class="tt-stays">(${c.stays} stays)</span>` : ''}</span></li>`).join('');
  return `<div class="timeline-times">
      <div class="timeline-subheader">Time in each column <span class="tt-note">wall-clock, up to now</span></div>
      <ul class="timeline-time-list">${rows}</ul>
      ${hasWork ? `<div class="timeline-dates"><span>Time spent on work: <b>${fmtDuration(t.work_minutes)}</b></span></div>` : ''}
    </div>`;
}

async function renderCardTimeline(card, isNew){
  const box = document.getElementById('editCardTimeline');
  box.style.display = 'none';
  box.innerHTML = '';
  if(isNew || !card.id) return;
  try {
    const res = await fetch('/api/card/' + card.id + '/history');
    if(!res.ok) return;
    const h = await res.json();
    if(document.getElementById('cardEditModal').dataset.cardId !== card.id) return;   // modal moved on
    const titles = {};
    ((latestBoard && latestBoard.columns) || []).forEach(c => { titles[c.id] = c.title; });
    const name = id => id ? escapeHtml(titles[id] || id) : '—';
    const rows = h.history.map(e =>
      `<li><span class="tl-at">${fmtDateTime(e.at)}</span> ${e.from ? name(e.from) + ' → ' : 'Created in '}${name(e.to)}` +
      `<span class="tl-src">${escapeHtml(e.source)}</span></li>`).join('');
    box.innerHTML = `
      <div class="timeline-header"><span>Timeline</span></div>
      <div class="timeline-dates">
        <span>Created: <b>${h.created_at ? fmtDay(h.created_at) : '—'}</b></span>
        <span>Started: <b>${h.started_at ? fmtDay(h.started_at) : '—'}</b></span>
        <span>Completed: <b>${h.completed_at ? fmtDay(h.completed_at) : '—'}</b></span>
      </div>
      ${h.column_since ? `<div class="timeline-dates"><span>In ${name(h.column)} since <b>${fmtDay(h.column_since)}</b></span></div>` : ''}
      ${timeInColumnsHtml(h.time_in_columns)}
      ${rows ? `<ul class="timeline-list">${rows}</ul>` : '<small class="link-hint">No column changes recorded yet.</small>'}`;
    box.style.display = '';
  } catch(err) { /* history is informational; the modal works without it */ }
}
async function openCardEditModal(card, isNew = false){
  if(!latestBoard){
    await fetchBoard();
  }
  const modal = document.getElementById('cardEditModal');
  modal.dataset.cardId = card.id;
  modal.dataset.isNew = isNew;
  document.getElementById('editCardTitle').value = card.title || '';
  document.getElementById('editCardDesc').value = card.description || '';
  document.getElementById('editCardAssignee').value = card.assignee || '';
  const editProjectInput = document.getElementById('editCardProject');
  populateProjectInput(editProjectInput, card.project || '');
  renderLinkRows(card.links || []);
  renderCardTimeline(card, isNew);

  // Update title with more prominent styling
  const titleEl = document.getElementById('editCardModalTitle');
  if(isNew){
    titleEl.textContent = '✨ New Task';
    titleEl.style.color = '#4ade80';
    titleEl.style.padding = '8px 12px';
    titleEl.style.backgroundColor = 'rgba(74, 222, 128, 0.1)';
    titleEl.style.borderRadius = '6px';
    titleEl.style.display = 'inline-block';
    document.getElementById('editCardDuplicateBtn').style.display = 'none';
    document.getElementById('editCardDeleteBtn').style.display = 'none';
    document.getElementById('editCardSplitBtn').style.display = 'none';
  } else {
    titleEl.textContent = 'Edit Task';
    titleEl.style.color = '';
    titleEl.style.padding = '';
    titleEl.style.backgroundColor = '';
    titleEl.style.borderRadius = '';
    titleEl.style.display = '';
    document.getElementById('editCardDuplicateBtn').style.display = 'inline-block';
    document.getElementById('editCardDeleteBtn').style.display = 'inline-block';
    document.getElementById('editCardSplitBtn').style.display = 'inline-block';
  }

  // apply purple styling to modal
  const modalContent = modal.querySelector('.modal-content');
  const modalPurple = '#b78ef5';
  if(modalContent){
    modalContent.style.borderColor = modalPurple;
    modalContent.style.borderWidth = '2px';
    modalContent.style.backgroundColor = '#0f0f18';
  }

  modal.classList.remove('hidden');
  document.getElementById('editCardTitle').focus();
}
function closeCardEditModal(){
  const modal = document.getElementById('cardEditModal');
  const modalContent = modal.querySelector('.modal-content');
  if(modalContent){
    modalContent.style.borderColor = '';
    modalContent.style.borderWidth = '';
    modalContent.style.backgroundColor = '';
  }
  modal.classList.add('hidden');
}

document.getElementById('editCardCancelBtn').addEventListener('click', ()=>{ closeCardEditModal(); });
document.getElementById('editCardDeleteBtn').addEventListener('click', async ()=>{
  if(!confirm('Delete this task?')) return;
  const modal = document.getElementById('cardEditModal');
  const cardId = modal.dataset.cardId;
  await fetch('/api/card/' + cardId, {method:'DELETE'});
  closeCardEditModal();
  render();
});
document.getElementById('editCardDuplicateBtn').addEventListener('click', async ()=>{
  const modal = document.getElementById('cardEditModal');
  const originalCardId = modal.dataset.cardId;
  const title = document.getElementById('editCardTitle').value.trim();
  const description = document.getElementById('editCardDesc').value.trim();
  const assignee = document.getElementById('editCardAssignee').value.trim();
  if(!title) return alert('title required');
  const links = collectLinkRows();
  const project = document.getElementById('editCardProject').value;
  
  // Find the column of the original card
  const board = await fetchBoard();
  let columnId = null;
  for(const col of board.columns){
    for(const card of col.cards){
      if(card.id === originalCardId){
        columnId = col.id;
        break;
      }
    }
    if(columnId) break;
  }
  
  if(!columnId) return alert('Could not find column for duplicate');
  
  // Create new card with duplicated data
  try {
    const res = await fetch('/api/card', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title, description, links, project, assignee, column: columnId})});
    if(!res.ok) throw new Error('Failed to create card');
    const newCard = await res.json();
    if(!newCard || !newCard.id) throw new Error('No ID returned for new card');
    
    // Open the new card for editing
    await openCardEditModal(newCard, true);
  } catch(err) {
    alert('Error duplicating card: ' + err.message);
  }
});
document.getElementById('editCardSaveBtn').addEventListener('click', async ()=>{
  const modal = document.getElementById('cardEditModal');
  const cardId = modal.dataset.cardId;
  const title = document.getElementById('editCardTitle').value.trim();
  const description = document.getElementById('editCardDesc').value.trim();
  const assignee = document.getElementById('editCardAssignee').value.trim();
  if(!title) return alert('title required');
  const links = collectLinkRows();
  const project = document.getElementById('editCardProject').value;
  try {
    const res = await fetch('/api/card/' + cardId, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({title, description, links, project, assignee})});
    if(!res.ok) throw new Error('Failed to save card');
    closeCardEditModal();
    render();
  } catch(err) {
    alert('Error saving card: ' + err.message);
  }
});

// Split task modal
const splitTaskModal = document.createElement('div');
splitTaskModal.id = 'splitTaskModal';
splitTaskModal.className = 'modal hidden';
splitTaskModal.innerHTML = `
  <div class="modal-backdrop"></div>
  <div class="split-modal-content">
    <h2>Split Task</h2>
    <div class="split-container">
      <div class="split-pane split-left">
        <h3>Original Task</h3>
        <div class="card-form">
          <label>Title:</label>
          <input id="splitOriginalTitle" placeholder="Card title" />
          <label>Project:</label>
          <div class="project-picker" data-project-input="splitOriginalProject">
            <input id="splitOriginalProject" class="modal-input project-input" placeholder="Select or type a project" autocomplete="off" />
            <button type="button" class="project-picker-toggle" data-project-toggle="splitOriginalProject" aria-label="Show saved projects">▼</button>
            <div class="project-picker-dropdown hidden" data-project-menu="splitOriginalProject"></div>
          </div>
          <label>Assignee:</label>
          <input id="splitOriginalAssignee" class="modal-input" placeholder="Assignee (optional)" />
          <label>Description:</label>
          <textarea id="splitOriginalDesc" placeholder="Description (optional)"></textarea>
          <div class="links-section">
            <div class="links-header">
              <span>Links</span>
              <button type="button" id="splitOriginalAddLinkBtn">+ Link</button>
            </div>
            <div id="splitOriginalLinksContainer" class="link-rows"></div>
          </div>
        </div>
      </div>
      <div class="split-pane split-right">
        <h3>New Task</h3>
        <div class="card-form">
          <label>Title:</label>
          <input id="splitNewTitle" placeholder="Card title" />
          <label>Project:</label>
          <div class="project-picker" data-project-input="splitNewProject">
            <input id="splitNewProject" class="modal-input project-input" placeholder="Select or type a project" autocomplete="off" />
            <button type="button" class="project-picker-toggle" data-project-toggle="splitNewProject" aria-label="Show saved projects">▼</button>
            <div class="project-picker-dropdown hidden" data-project-menu="splitNewProject"></div>
          </div>
          <label>Assignee:</label>
          <input id="splitNewAssignee" class="modal-input" placeholder="Assignee (optional)" />
          <label>Description:</label>
          <textarea id="splitNewDesc" placeholder="Description (optional)"></textarea>
          <div class="links-section">
            <div class="links-header">
              <span>Links</span>
              <button type="button" id="splitNewAddLinkBtn">+ Link</button>
            </div>
            <div id="splitNewLinksContainer" class="link-rows"></div>
          </div>
        </div>
      </div>
    </div>
    <div class="modal-actions">
      <button id="splitTaskSaveBtn">Save Both</button>
      <button id="splitTaskCancelBtn">Cancel</button>
    </div>
  </div>
`;
document.body.appendChild(splitTaskModal);
registerProjectPicker('splitOriginalProject');
registerProjectPicker('splitNewProject');

document.getElementById('splitOriginalAddLinkBtn').addEventListener('click', () => {
  const container = document.getElementById('splitOriginalLinksContainer');
  if(container) container.appendChild(createLinkRowElement({}, container));
});
document.getElementById('splitNewAddLinkBtn').addEventListener('click', () => {
  const container = document.getElementById('splitNewLinksContainer');
  if(container) container.appendChild(createLinkRowElement({}, container));
});

function closeSplitTaskModal(){
  const modal = document.getElementById('splitTaskModal');
  const modalContent = modal.querySelector('.split-modal-content');
  if(modalContent){
    modalContent.style.borderColor = '';
    modalContent.style.borderWidth = '';
    modalContent.style.backgroundColor = '';
  }
  modal.classList.add('hidden');
}

async function openSplitTaskModal(card){
  if(!latestBoard){
    await fetchBoard();
  }
  const modal = document.getElementById('splitTaskModal');
  modal.dataset.cardId = card.id;
  
  // Populate original task fields (read-only feel but actually editable)
  document.getElementById('splitOriginalTitle').value = card.title || '';
  document.getElementById('splitOriginalDesc').value = card.description || '';
  document.getElementById('splitOriginalAssignee').value = card.assignee || '';
  const origProjectInput = document.getElementById('splitOriginalProject');
  populateProjectInput(origProjectInput, card.project || '');
  
  // Populate new task fields with same content as original
  document.getElementById('splitNewTitle').value = card.title || '';
  document.getElementById('splitNewDesc').value = card.description || '';
  document.getElementById('splitNewAssignee').value = card.assignee || '';
  const newProjectInput = document.getElementById('splitNewProject');
  populateProjectInput(newProjectInput, card.project || '');

  // Populate link rows — both panes start with the same links so user can prune per side
  const cardLinks = Array.isArray(card.links) && card.links.length ? card.links : [{}];
  const origLinksContainer = document.getElementById('splitOriginalLinksContainer');
  if(origLinksContainer){
    origLinksContainer.innerHTML = '';
    cardLinks.forEach(link => origLinksContainer.appendChild(createLinkRowElement(link, origLinksContainer)));
  }
  const newLinksContainer = document.getElementById('splitNewLinksContainer');
  if(newLinksContainer){
    newLinksContainer.innerHTML = '';
    cardLinks.forEach(link => newLinksContainer.appendChild(createLinkRowElement(link, newLinksContainer)));
  }

  // Apply purple styling to modal
  const modalContent = modal.querySelector('.split-modal-content');
  const modalPurple = '#b78ef5';
  if(modalContent){
    modalContent.style.borderColor = modalPurple;
    modalContent.style.borderWidth = '2px';
    modalContent.style.backgroundColor = '#0f0f18';
  }
  
  modal.classList.remove('hidden');
  document.getElementById('splitNewTitle').focus();
}

document.getElementById('editCardSplitBtn').addEventListener('click', async ()=>{
  const modal = document.getElementById('cardEditModal');
  const cardId = modal.dataset.cardId;
  const board = await fetchBoard();
  
  // Find the card
  let card = null;
  for(const col of board.columns){
    for(const c of col.cards){
      if(c.id === cardId){
        card = c;
        break;
      }
    }
    if(card) break;
  }
  
  if(!card) return alert('Could not find card');
  
  // Close the edit modal and open split modal
  closeCardEditModal();
  await openSplitTaskModal(card);
});

document.getElementById('splitTaskCancelBtn').addEventListener('click', ()=>{ closeSplitTaskModal(); });

document.getElementById('splitTaskSaveBtn').addEventListener('click', async ()=>{
  const modal = document.getElementById('splitTaskModal');
  const cardId = modal.dataset.cardId;
  
  // Get original task data
  const origTitle = document.getElementById('splitOriginalTitle').value.trim();
  const origDesc = document.getElementById('splitOriginalDesc').value.trim();
  const origAssignee = document.getElementById('splitOriginalAssignee').value.trim();
  const origProject = document.getElementById('splitOriginalProject').value;
  
  // Get new task data
  const newTitle = document.getElementById('splitNewTitle').value.trim();
  const newDesc = document.getElementById('splitNewDesc').value.trim();
  const newAssignee = document.getElementById('splitNewAssignee').value.trim();
  const newProject = document.getElementById('splitNewProject').value;
  
  if(!origTitle) return alert('Original task title required');
  if(!newTitle) return alert('New task title required');
  
  try {
    // Collect links independently from each pane's UI
    const origLinksContainer = document.getElementById('splitOriginalLinksContainer');
    const origLinks = [];
    if(origLinksContainer){
      origLinksContainer.querySelectorAll('.link-row').forEach(row => {
        const text = row.querySelector('.link-text').value.trim();
        const url = row.querySelector('.link-url').value.trim();
        if(!url) return;
        origLinks.push({text: text || url, url});
      });
    }
    const newLinksContainer = document.getElementById('splitNewLinksContainer');
    const newLinks = [];
    if(newLinksContainer){
      newLinksContainer.querySelectorAll('.link-row').forEach(row => {
        const text = row.querySelector('.link-text').value.trim();
        const url = row.querySelector('.link-url').value.trim();
        if(!url) return;
        newLinks.push({text: text || url, url});
      });
    }

    // Find the column of the original card
    const boardBeforeSplit = await fetchBoard();
    let columnId = null;
    for(const col of boardBeforeSplit.columns){
      for(const card of col.cards){
        if(card.id === cardId){ columnId = col.id; break; }
      }
      if(columnId) break;
    }
    if(!columnId) throw new Error('Could not find column for new card');

    // Update the original card
    const updateRes = await fetch('/api/card/' + cardId, {
      method:'PUT',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        title: origTitle,
        description: origDesc,
        assignee: origAssignee,
        project: origProject,
        links: origLinks
      })
    });
    if(!updateRes.ok) throw new Error('Failed to update original card');

    // Create the new card
    const createRes = await fetch('/api/card', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        title: newTitle,
        description: newDesc,
        assignee: newAssignee,
        project: newProject,
        links: newLinks,
        column: columnId
      })
    });
    if(!createRes.ok) throw new Error('Failed to create new card');
    
    closeSplitTaskModal();
    render();
  } catch(err) {
    alert('Error splitting task: ' + err.message);
  }
});

// Card modal (new task)
const cardModal = document.createElement('div');
cardModal.id = 'cardModal';
cardModal.className = 'modal hidden';
cardModal.innerHTML = `
  <div class="modal-backdrop"></div>
  <div class="modal-content">
    <h2>New Task</h2>
    <div class="card-form">
      <label>Status: <select id="cardColumnSelect" class="modal-input"></select></label>
      <label>Project:</label>
      <div class="project-picker" data-project-input="cardProjectInput">
        <input id="cardProjectInput" class="modal-input project-input" placeholder="Select or type a project" autocomplete="off" />
        <button type="button" class="project-picker-toggle" data-project-toggle="cardProjectInput" aria-label="Show saved projects">▼</button>
        <div class="project-picker-dropdown hidden" data-project-menu="cardProjectInput"></div>
      </div>
      <label>Assignee:</label>
      <input id="cardAssigneeInput" class="modal-input" placeholder="Assignee (optional)" />
      <input id="cardTitle" placeholder="Card title" />
      <textarea id="cardDesc" placeholder="Description (optional)"></textarea>
      <div class="modal-actions"><button id="cardAddBtn">Add</button> <button id="cardCancelBtn">Cancel</button></div>
    </div>
  </div>
`;
document.body.appendChild(cardModal);
registerProjectPicker('cardProjectInput');

async function openCardModal(columnId){
  const modal = document.getElementById('cardModal');
  modal.dataset.column = columnId;
  const board = await fetchBoard();
  const sel = document.getElementById('cardColumnSelect');
  sel.innerHTML = '';
  board.columns.forEach(c => {
    const opt = document.createElement('option');
    opt.value = c.id;
    opt.textContent = c.title;
    sel.appendChild(opt);
  });
  sel.value = columnId;
  const createProjectInput = document.getElementById('cardProjectInput');
  populateProjectInput(createProjectInput, '');

  const modalContent = modal.querySelector('.modal-content');
  const modalPurple = '#b78ef5';
  if(modalContent){
    modalContent.style.borderColor = modalPurple;
    modalContent.style.borderWidth = '2px';
    modalContent.style.backgroundColor = '#0f0f18';
  }

  modal.classList.remove('hidden');
  document.getElementById('cardTitle').value = '';
  document.getElementById('cardDesc').value = '';
  document.getElementById('cardAssigneeInput').value = '';
  document.getElementById('cardTitle').focus();
}
function closeCardModal(){
  const modal = document.getElementById('cardModal');
  const modalContent = modal.querySelector('.modal-content');
  if(modalContent){
    modalContent.style.borderColor = '';
    modalContent.style.borderWidth = '';
    modalContent.style.backgroundColor = '';
  }
  modal.classList.add('hidden');
}

document.getElementById('cardCancelBtn').addEventListener('click', ()=>{ closeCardModal(); });
document.getElementById('cardAddBtn').addEventListener('click', async ()=>{
  const modal = document.getElementById('cardModal');
  const column = document.getElementById('cardColumnSelect').value || modal.dataset.column;
  const title = document.getElementById('cardTitle').value.trim();
  const description = document.getElementById('cardDesc').value.trim();
  const assignee = document.getElementById('cardAssigneeInput').value.trim();
  const project = document.getElementById('cardProjectInput').value;
  if(!title) return alert('title required');
  await fetch('/api/card', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title, description, assignee, column, project})});
  closeCardModal();
  render();
});

// keyboard: ESC closes modals
document.addEventListener('keydown', (e)=>{
  if(e.key === 'Escape'){
    const cm = document.getElementById('cardModal');
    if(cm && !cm.classList.contains('hidden')) cm.classList.add('hidden');
    const cem = document.getElementById('cardEditModal');
    if(cem && !cem.classList.contains('hidden')) cem.classList.add('hidden');
    const stm = document.getElementById('splitTaskModal');
    if(stm && !stm.classList.contains('hidden')) closeSplitTaskModal();
  }
});

// Apply initial truncation state to button
const truncateBtn2 = document.getElementById('truncateBtn');
if(truncateBtn2 && cardTruncationEnabled){
  truncateBtn2.setAttribute('aria-pressed', 'true');
  truncateBtn2.classList.add('active');
}
setTruncateBtnLabel();

// Initial render (only on board view page)
const boardEl = document.getElementById('board');
if(boardEl){
  render();
}

// Auto repaint: every 15s re-fetch the board and repaint if something changed
// elsewhere (MCP tools, another tab). Skips while the tab is hidden, while a
// card is being dragged, or while any modal/dropdown is open, and keeps scroll.
const AUTO_REPAINT_INTERVAL_MS = 15000;
let autoRepaintInFlight = false;

function autoRepaintBusy(){
  if(document.visibilityState !== 'visible') return true;
  if(draggedCardId || (typeof listDraggedCardId !== 'undefined' && listDraggedCardId)) return true;
  if(document.querySelector('.modal:not(.hidden), .split-modal:not(.hidden), .project-picker-dropdown:not(.hidden)')) return true;
  const ae = document.activeElement;
  if(ae && ['INPUT','TEXTAREA','SELECT'].includes(ae.tagName)) return true;
  return false;
}

function captureScrollState(){
  const state = {win: [window.scrollX, window.scrollY], cols: {}, board: null, list: null};
  const boardEl = document.getElementById('board');
  if(boardEl) state.board = boardEl.scrollLeft;
  const listEl = document.getElementById('listView');
  if(listEl) state.list = listEl.scrollTop;
  document.querySelectorAll('.column').forEach(c => {
    const l = c.querySelector('.card-list');
    if(l) state.cols[c.dataset.column] = l.scrollTop;
  });
  return state;
}

function restoreScrollState(state){
  const boardEl = document.getElementById('board');
  if(boardEl && state.board !== null) boardEl.scrollLeft = state.board;
  const listEl = document.getElementById('listView');
  if(listEl && state.list !== null) listEl.scrollTop = state.list;
  document.querySelectorAll('.column').forEach(c => {
    const l = c.querySelector('.card-list');
    if(l && state.cols[c.dataset.column] !== undefined) l.scrollTop = state.cols[c.dataset.column];
  });
  window.scrollTo(state.win[0], state.win[1]);
}

async function autoRepaint(){
  if(autoRepaintInFlight || autoRepaintBusy()) return;
  const isList = !!document.getElementById('listView');
  if(!isList && !document.getElementById('board')) return;
  autoRepaintInFlight = true;
  try{
    const prev = JSON.stringify(isList ? listLatestBoard : latestBoard);
    const next = isList ? await listFetchBoard() : await fetchBoard();
    // state may have changed while the fetch was in flight
    if(autoRepaintBusy() || JSON.stringify(next) === prev) return;
    const scroll = captureScrollState();
    if(isList) await listRender({useLatest: true}); else await render({useLatest: true});
    restoreScrollState(scroll);
  }catch(err){
    console.warn('Auto repaint failed', err);
  }finally{
    autoRepaintInFlight = false;
  }
}

setInterval(autoRepaint, AUTO_REPAINT_INTERVAL_MS);
document.addEventListener('visibilitychange', () => {
  if(document.visibilityState === 'visible') autoRepaint();
});

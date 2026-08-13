let latestBoard = null;
let activeProjectFilter = null;
let cardTruncationEnabled = localStorage.getItem('cardTruncationEnabled') !== 'false';
let draggedCardId = null;
let draggedCardColumnId = null;
async function fetchBoard() {
  const res = await fetch('/api/board');
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
      if(!res.ok){ alert('Unable to move card. Please try again.'); render(); return; }
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
        if(!res.ok){ alert('Unable to move card. Please try again.'); render(); return; }
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
    openSettingsModal('projects');
    focusProjectInSettings(projectName);
  }
});

function formatDueDateBadge(card){
  if(!card.due_date) return '';
  const isAllDay = !!card.all_day;
  const d = new Date(isAllDay ? card.due_date + 'T00:00:00' : card.due_date);
  if(isNaN(d.getTime())) return '';
  const now = new Date();
  const overdue = isAllDay
    ? d < new Date(now.getFullYear(), now.getMonth(), now.getDate())
    : d < now;
  const label = isAllDay
    ? d.toLocaleDateString(undefined, {month:'short', day:'numeric'})
    : d.toLocaleString(undefined, {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
  const gcalIcon = card.gcal_event_id ? ' 🔗' : '';
  return `<div class="card-due${overdue ? ' card-due-overdue' : ''}" title="Due ${escapeHtml(label)}${card.gcal_event_id ? ' (synced to Google Calendar)' : ''}">📅 ${escapeHtml(label)}${gcalIcon}</div>`;
}

function createCardElement(card, projectMap) {
  const linkCount = Array.isArray(card.links) ? card.links.length : 0;
  const projectName = card.project || '';
  const assignee = card.assignee || '';
  const linkedUser = card.assigned_user_id ? latestUsers.find(u => u.id === card.assigned_user_id) : null;
  const linkedUserName = linkedUser ? linkedUser.display_name : '';
  const projectDetails = projectMap && projectMap.get(projectName);
  const projectColor = projectDetails && projectDetails.color ? projectDetails.color : null;
  const el = document.createElement('div');
  el.className = 'card' + (cardTruncationEnabled ? ' card-truncated' : '');
  el.draggable = true;
  el.dataset.id = card.id;
  el.dataset.order = card.order !== undefined ? card.order : 0;
  el.innerHTML = `
    <div class="card-title">${escapeHtml(card.title)}</div>
    <div class="card-desc">${escapeHtmlWithBr(card.description || '')}</div>
    <div class="card-footer">
      <div class="card-footer-left">
        ${linkCount > 0 ? `<div class="card-links" title="${linkCount} link${linkCount!==1?'s':''}">🔗 ${linkCount}</div>` : ''}
        ${formatDueDateBadge(card)}
      </div>
      <div class="card-footer-center">
        ${projectName || assignee || linkedUserName ? `
          <div class="card-footer-center-meta">
            ${projectName ? `<div class="card-project" title="Project: ${escapeHtml(projectName)}">${escapeHtml(projectName)}</div>` : ''}
            ${assignee ? `<div class="card-assignee" title="Assignee: ${escapeHtml(assignee)}">👤 ${escapeHtml(assignee)}</div>` : ''}
            ${linkedUserName ? `<div class="card-linked-user" title="Calendar syncs to ${escapeHtml(linkedUserName)}">📅 ${escapeHtml(linkedUserName)}</div>` : ''}
          </div>
        ` : ''}
      </div>
      <div class="card-footer-right">
        <button class="edit" title="Edit card">✏️</button>
      </div>
    </div>
  `;

  const cardColor = projectColor || card.color || '#5b2e8a';
  el.style.borderColor = cardColor;
  el.style.borderWidth = '2px';
  el.style.backgroundColor = hexToRgba(cardColor, 0.06);
  if(projectColor){
    el.style.setProperty('--project-pill-border', projectColor);
    el.style.setProperty('--project-pill-text', projectColor);
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
    dot.style.backgroundColor = safeColor;

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
  for(const col of board.columns){
    if(col.hidden){
      continue;
    }
    const cardsInColumn = Array.isArray(col.cards) ? col.cards : [];
    const visibleCards = !activeProjectFilter
      ? cardsInColumn
      : cardsInColumn.filter(card => {
          const projectName = card && card.project ? card.project.trim() : '';
          return projectName === activeProjectFilter;
        });
    const colEl = document.createElement('div');
    colEl.className = 'column';
    colEl.dataset.column = col.id;

    // apply color styling
    const outline = col.color || '#e0e0e0';
    colEl.style.borderColor = outline;
    colEl.style.borderWidth = '2px';
    colEl.style.backgroundColor = hexToRgba(outline, 0.06);

    const header = document.createElement('div');
    header.className = 'col-header';
    const visibleCount = visibleCards.length;
    const totalCount = cardsInColumn.length;
    const countLabel = activeProjectFilter ? `${visibleCount}/${totalCount || 0}` : `${visibleCount}`;
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
        alert('Unable to move card. Please try again.');
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

    boardEl.appendChild(colEl);
  }

  refreshProjectInputs();
}

// Global add removed: use per-column + buttons to add cards.

// Settings modal
const settingsModal = document.createElement('div');
settingsModal.id = 'settingsModal';
// hidden by default
settingsModal.className = 'modal hidden';
settingsModal.innerHTML = `
  <div class="modal-backdrop"></div>
  <div class="modal-content">
    <h2>Board Settings</h2>
    <div class="settings-tabs" role="tablist">
      <button type="button" class="settings-tab-btn active" data-tab="statuses">Statuses</button>
      <button type="button" class="settings-tab-btn" data-tab="projects">Projects</button>
      <button type="button" class="settings-tab-btn" data-tab="import">Import/Export</button>
      <button type="button" class="settings-tab-btn" data-tab="users">👤 Users</button>
      <button type="button" class="settings-tab-btn" data-tab="calendar">📅 Calendar</button>
      <button type="button" class="settings-tab-btn" data-tab="about">About</button>
    </div>
    <div class="settings-tab-panels">
      <div class="settings-tab-panel active" data-tab="statuses">
        <div id="columnsList" class="columns-list"></div>
        <div class="add-column">
          <input id="newColumnTitle" placeholder="New column title" />
          <input id="newColumnColor" type="color" value="#9aa0a6" />
          <button id="addColumnBtn">Add Column</button>
        </div>
      </div>
      <div class="settings-tab-panel" data-tab="projects">
        <div id="projectsList" class="projects-list"></div>
        <div class="add-project">
          <input id="newProjectName" placeholder="New project name" />
          <input id="newProjectColor" type="color" value="#5b2e8a" />
          <button id="addProjectBtn">Add Project</button>
        </div>
      </div>
      <div class="settings-tab-panel" data-tab="import">
        <div class="import-export-panel">
          <section class="import-block">
            <h3>Export Board</h3>
            <p>Download the current kanban.json file for safekeeping or migration.</p>
            <button type="button" id="downloadBoardBtn">Download kanban.json</button>
          </section>
          <section class="import-block">
            <h3>Import Board</h3>
            <p>Select a saved kanban.json file and choose whether to merge or replace your current board.</p>
            <input type="file" id="importFileInput" accept="application/json" />
            <div class="import-mode">
              <label><input type="radio" name="importMode" value="merge" checked /> Merge with existing data</label>
              <label><input type="radio" name="importMode" value="replace" /> Replace existing data</label>
            </div>
            <div class="import-actions">
              <button type="button" id="importBoardBtn">Import kanban.json</button>
            </div>
          </section>
        </div>
      </div>
      <div class="settings-tab-panel" data-tab="users">
        <div id="usersList" class="users-list"></div>
        <div class="add-user">
          <input id="newUserUsername" placeholder="Username" autocomplete="off" />
          <input id="newUserDisplayName" placeholder="Display name (optional)" autocomplete="off" />
          <input id="newUserPassword" type="password" placeholder="Password (min 8 chars)" autocomplete="new-password" />
          <button id="addUserBtn">Add User</button>
        </div>
        <p class="users-hint">
          Any logged-in user can add or remove other users. Only you can change your own password.
          Each user connects their own Google Calendar from the 📅 Calendar tab while logged in as them.
        </p>
      </div>
      <div class="settings-tab-panel" data-tab="calendar">
        <div class="gcal-settings-panel">
          <div class="gcal-status-card">
            <div class="gcal-status-row">
              <span class="gcal-status-dot" id="gcalStatusDot"></span>
              <span id="gcalStatusText">Checking status…</span>
            </div>
            <div class="gcal-status-detail" id="gcalStatusDetail"></div>
          </div>
          <div class="gcal-settings-actions">
            <button type="button" id="gcalConnectBtn">Connect Google Calendar</button>
            <button type="button" id="gcalSyncNowBtn" style="display:none">Sync now</button>
            <button type="button" id="gcalDisconnectBtn" style="display:none">Disconnect</button>
          </div>
          <p class="gcal-settings-hint">
            This connects <strong>your</strong> Google account<span id="gcalHintWhoami"></span> — each person
            connects their own. Cards with a due date <strong>assigned to you</strong> (via "Linked user" on the
            card) are pushed to your calendar as events, and changes made on the calendar side (including new
            events) are pulled back in roughly every minute.
          </p>
        </div>
      </div>
      <div class="settings-tab-panel" data-tab="about">
        <div style="padding: 20px; text-align: center;">
          <h3>Personal Kanban</h3>
          <p style="font-size: 14px; color: rgba(255, 255, 255, 0.7); margin: 16px 0;">
            Version 1.0
          </p>
          <p style="font-size: 14px; color: rgba(255, 255, 255, 0.7); margin: 16px 0;">
            Written by Stuart Weenig
          </p>
          <p style="font-size: 12px; color: rgba(255, 255, 255, 0.6); margin-top: 24px;">
            Licensed under GNU General Public License v3
          </p>
        </div>
      </div>
    </div>
    <div class="modal-actions"><button id="closeSettings">Close</button></div>
  </div>
`;
document.body.appendChild(settingsModal);

async function renderStatusSettings(){
  const board = await fetchBoard();
  const list = document.getElementById('columnsList');
  list.innerHTML = '';
  board.columns.forEach((col, idx)=>{
    const row = document.createElement('div');
    row.className = 'col-row';
    row.innerHTML = `
      <input class="col-title" data-id="${col.id}" value="${escapeHtml(col.title)}" />
      <input class="col-color" type="color" data-id="${col.id}" value="${col.color || '#9aa0a6'}" />
      <label class="col-hide-toggle">
        <input class="col-hidden" type="checkbox" data-id="${col.id}" ${col.hidden ? 'checked' : ''} />
        Hide
      </label>
      <button class="col-up" data-id="${col.id}" data-idx="${idx}" ${idx===0? 'disabled':''}>↑</button>
      <button class="col-down" data-id="${col.id}" data-idx="${idx}" ${idx===board.columns.length-1? 'disabled':''}>↓</button>
      <button class="col-delete" data-id="${col.id}">Delete</button>
    `;
    list.appendChild(row);
  });

  // attach handlers
  document.querySelectorAll('.col-title').forEach(inp=>{
    inp.addEventListener('change', async (e)=>{
      const id = e.target.dataset.id;
      const title = e.target.value.trim();
      if(!title) return alert('title required');
      await fetch('/api/column/' + id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})});
      render();
      renderStatusSettings();
    });
  });
  document.querySelectorAll('.col-color').forEach(inp=>{
    inp.addEventListener('change', async (e)=>{
      const id = e.target.dataset.id;
      const color = e.target.value;
      await fetch('/api/column/' + id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({color})});
      render();
      renderStatusSettings();
    });
  });
  document.querySelectorAll('.col-hidden').forEach(inp=>{
    inp.addEventListener('change', async (e)=>{
      const id = e.target.dataset.id;
      const hidden = e.target.checked;
      await fetch('/api/column/' + id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({hidden})});
      render();
      renderStatusSettings();
    });
  });
  document.querySelectorAll('.col-up').forEach(btn=>{
    btn.addEventListener('click', async (e)=>{
      const id = e.target.dataset.id;
      const idx = parseInt(e.target.dataset.idx, 10);
      const targetPos = Math.max(0, idx - 1);
      await fetch('/api/column/' + id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({position:targetPos})});
      // re-render to reflect the swap
      render();
      renderStatusSettings();
    });
  });
  document.querySelectorAll('.col-down').forEach(btn=>{
    btn.addEventListener('click', async (e)=>{
      const id = e.target.dataset.id;
      const idx = parseInt(e.target.dataset.idx, 10);
      const targetPos = idx + 1;
      await fetch('/api/column/' + id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({position:targetPos})});
      render();
      renderStatusSettings();
    });
  });
  document.querySelectorAll('.col-delete').forEach(btn=>{
    btn.addEventListener('click', async (e)=>{
      const id = e.target.dataset.id;
      if(!confirm('Delete this column? Cards in it will be removed unless moved.')) return;
      const boardNow = await fetchBoard();
      if(boardNow.columns.length <= 1){
        return alert('Cannot delete the last column');
      }
      await fetch('/api/column/' + id, {method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
      render();
      renderStatusSettings();
    });
  });

  refreshProjectInputs();
}

async function renderProjectSettings(){
  const board = await fetchBoard();
  const list = document.getElementById('projectsList');
  if(!list) return;
  list.innerHTML = '';
  const projects = Array.isArray(board.projects) ? board.projects : [];
  if(!projects.length){
    const empty = document.createElement('div');
    empty.className = 'project-empty';
    empty.textContent = 'No projects yet';
    list.appendChild(empty);
  }
  projects.forEach((proj, idx) => {
    const row = document.createElement('div');
    row.className = 'project-row';
    row.innerHTML = `
      <input class="project-name" data-index="${idx}" value="${escapeHtml(proj.name || '')}" placeholder="Project name" />
      <input class="project-color" type="color" data-index="${idx}" value="${proj.color || '#5b2e8a'}" />
      <button class="project-up" data-index="${idx}" ${idx===0? 'disabled':''}>↑</button>
      <button class="project-down" data-index="${idx}" ${idx===projects.length-1? 'disabled':''}>↓</button>
      <button class="project-delete" data-index="${idx}">Delete</button>
    `;
    list.appendChild(row);
  });

  list.querySelectorAll('.project-name').forEach(inp => {
    inp.addEventListener('change', async (e) => {
      const idx = parseInt(e.target.dataset.index, 10);
      const name = e.target.value.trim();
      if(!name){
        alert('name required');
        await renderProjectSettings();
        return;
      }
      await fetch('/api/project/' + idx, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
      await render();
      await renderProjectSettings();
    });
  });
  list.querySelectorAll('.project-color').forEach(inp => {
    inp.addEventListener('change', async (e) => {
      const idx = parseInt(e.target.dataset.index, 10);
      const color = e.target.value;
      await fetch('/api/project/' + idx, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({color})});
      await render();
      await renderProjectSettings();
    });
  });
  list.querySelectorAll('.project-up').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      const idx = parseInt(e.target.dataset.index, 10);
      const targetPos = Math.max(0, idx - 1);
      await fetch('/api/project/' + idx, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({position:targetPos})});
      await render();
      await renderProjectSettings();
    });
  });
  list.querySelectorAll('.project-down').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      const idx = parseInt(e.target.dataset.index, 10);
      const targetPos = idx + 1;
      await fetch('/api/project/' + idx, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({position:targetPos})});
      await render();
      await renderProjectSettings();
    });
  });
  list.querySelectorAll('.project-delete').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      const idx = parseInt(e.target.dataset.index, 10);
      if(!confirm('Delete this project?')) return;
      await fetch('/api/project/' + idx, {method:'DELETE'});
      await render();
      await renderProjectSettings();
    });
  });

  refreshProjectInputs();
}

const CURRENT_USER = window.PERKAN_CURRENT_USER || null;
let latestUsers = [];

async function fetchUsers(){
  const res = await fetch('/api/users');
  const data = await res.json();
  latestUsers = Array.isArray(data.users) ? data.users : [];
  return latestUsers;
}

async function renderUserSettings(){
  const list = document.getElementById('usersList');
  if(!list) return;
  const usersData = await fetchUsers();
  list.innerHTML = '';
  if(!usersData.length){
    const empty = document.createElement('div');
    empty.className = 'project-empty';
    empty.textContent = 'No users yet';
    list.appendChild(empty);
  }
  usersData.forEach(u => {
    const isSelf = CURRENT_USER && CURRENT_USER.id === u.id;
    const row = document.createElement('div');
    row.className = 'user-row';
    row.innerHTML = `
      <input class="user-display-name" data-id="${u.id}" value="${escapeHtml(u.display_name || '')}" placeholder="Display name" />
      <span class="user-username">@${escapeHtml(u.username)}${isSelf ? ' (you)' : ''}${u.google_linked ? ' <span class="user-google-badge" title="Signs in with Google">G</span>' : ''}</span>
      ${isSelf ? `<input class="user-new-password" type="password" data-id="${u.id}" placeholder="${u.has_password ? 'New password (optional)' : 'Set a password (optional)'}" autocomplete="new-password" />` : '<span></span>'}
      <button class="user-delete" data-id="${u.id}" ${isSelf ? 'disabled title="Log in as someone else to delete this account"' : ''}>Delete</button>
    `;
    list.appendChild(row);
  });

  list.querySelectorAll('.user-display-name').forEach(inp => {
    inp.addEventListener('change', async (e) => {
      const id = e.target.dataset.id;
      const display_name = e.target.value.trim();
      await fetch('/api/users/' + id, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({display_name})});
      await renderUserSettings();
      render({useLatest: true});
    });
  });
  list.querySelectorAll('.user-new-password').forEach(inp => {
    inp.addEventListener('change', async (e) => {
      const id = e.target.dataset.id;
      const password = e.target.value;
      if(!password) return;
      if(password.length < 8){ alert('password must be at least 8 characters'); e.target.value=''; return; }
      const res = await fetch('/api/users/' + id, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password})});
      if(res.ok){ alert('Password updated'); } else { const err = await res.json(); alert(err.error || 'Failed to update password'); }
      e.target.value = '';
    });
  });
  list.querySelectorAll('.user-delete').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      const id = e.target.dataset.id;
      if(!confirm('Delete this user? Cards assigned to them will be unassigned.')) return;
      const res = await fetch('/api/users/' + id, {method:'DELETE'});
      if(!res.ok){ const err = await res.json(); alert(err.error || 'Failed to delete user'); return; }
      await renderUserSettings();
      refreshAssignedUserSelects();
    });
  });

  refreshAssignedUserSelects();
}

const addUserBtn = document.getElementById('addUserBtn');
if(addUserBtn){
  addUserBtn.addEventListener('click', async () => {
    const username = document.getElementById('newUserUsername').value.trim();
    const display_name = document.getElementById('newUserDisplayName').value.trim();
    const password = document.getElementById('newUserPassword').value;
    if(!username || !password) return alert('username and password required');
    const res = await fetch('/api/users', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username, display_name, password})});
    if(!res.ok){ const err = await res.json(); alert(err.error || 'Failed to create user'); return; }
    document.getElementById('newUserUsername').value = '';
    document.getElementById('newUserDisplayName').value = '';
    document.getElementById('newUserPassword').value = '';
    await renderUserSettings();
  });
}

function refreshAssignedUserSelects(){
  document.querySelectorAll('select[data-assigned-user-select]').forEach(sel => {
    const current = sel.value;
    sel.innerHTML = '<option value="">Unassigned</option>' +
      latestUsers.map(u => `<option value="${u.id}">${escapeHtml(u.display_name)}</option>`).join('');
    if(latestUsers.some(u => u.id === current)){
      sel.value = current;
    }
  });
}

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

async function downloadBoardJson(){
  try {
    const res = await fetch('/api/board/export');
    if(!res.ok){
      throw new Error('Failed to download board');
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'kanban.json';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(()=>URL.revokeObjectURL(url), 0);
  } catch(err){
    console.error(err);
    alert('Unable to download the board data.');
  }
}

async function importBoardJson(){
  const fileInput = document.getElementById('importFileInput');
  if(!fileInput || !fileInput.files || !fileInput.files.length){
    alert('Select a kanban.json file to import.');
    return;
  }
  const modeInput = document.querySelector('input[name="importMode"]:checked');
  const mode = modeInput ? modeInput.value : 'merge';
  const confirmMsg = mode === 'replace'
    ? 'Replace the current board with the selected file? This cannot be undone.'
    : 'Merge the selected board into your current data?';
  if(!confirm(confirmMsg)){
    return;
  }
  const formData = new FormData();
  formData.append('file', fileInput.files[0]);
  formData.append('mode', mode);
  try {
    const res = await fetch('/api/board/import', {method:'POST', body: formData});
    const payload = await res.json().catch(()=>({}));
    if(!res.ok){
      const message = payload && payload.error ? payload.error : 'Import failed';
      alert(message);
      return;
    }
    fileInput.value = '';
    await render();
    const activeTabBtn = document.querySelector('.settings-tab-btn.active');
    if(activeTabBtn){
      if(activeTabBtn.dataset.tab === 'projects'){
        await renderProjectSettings();
      } else if(activeTabBtn.dataset.tab === 'statuses'){
        await renderStatusSettings();
      }
    }
    alert('Import complete.');
  } catch(err){
    console.error(err);
    alert('Unable to import the selected file.');
  }
}

function activateSettingsTab(tabId){
  const modal = document.getElementById('settingsModal');
  if(!modal) return;
  modal.querySelectorAll('.settings-tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  });
  modal.querySelectorAll('.settings-tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.dataset.tab === tabId);
  });
  if(tabId === 'statuses'){
    renderStatusSettings();
  } else if(tabId === 'projects'){
    renderProjectSettings();
  } else if(tabId === 'users'){
    renderUserSettings();
  } else if(tabId === 'calendar'){
    renderGcalSettings();
  }
}

async function renderGcalSettings(){
  const dot = document.getElementById('gcalStatusDot');
  const text = document.getElementById('gcalStatusText');
  const detail = document.getElementById('gcalStatusDetail');
  const connectBtn = document.getElementById('gcalConnectBtn');
  const syncBtn = document.getElementById('gcalSyncNowBtn');
  const disconnectBtn = document.getElementById('gcalDisconnectBtn');
  if(!dot) return;
  const whoami = document.getElementById('gcalHintWhoami');
  if(whoami){
    whoami.textContent = CURRENT_USER ? ` (you're logged in as ${CURRENT_USER.display_name})` : '';
  }
  try {
    const res = await fetch('/api/gcal/status');
    const status = await res.json();
    if(!status.configured){
      dot.className = 'gcal-status-dot disconnected';
      text.textContent = 'Not configured';
      detail.textContent = 'Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET on the server to enable Google Calendar sync.';
      connectBtn.style.display = 'none';
      syncBtn.style.display = 'none';
      disconnectBtn.style.display = 'none';
      return;
    }
    if(status.connected){
      dot.className = 'gcal-status-dot' + (status.last_sync_error ? ' error' : ' connected');
      text.textContent = status.connected_email ? `Connected as ${status.connected_email}` : 'Connected';
      const lastSync = status.last_sync ? new Date(status.last_sync * 1000).toLocaleString() : 'never';
      detail.textContent = status.last_sync_error
        ? `Last sync error: ${status.last_sync_error}`
        : `Calendar: ${status.calendar_id} · Last synced: ${lastSync}`;
      connectBtn.style.display = 'none';
      syncBtn.style.display = 'inline-block';
      disconnectBtn.style.display = 'inline-block';
    } else {
      dot.className = 'gcal-status-dot disconnected';
      text.textContent = 'Not connected';
      detail.textContent = '';
      connectBtn.style.display = 'inline-block';
      syncBtn.style.display = 'none';
      disconnectBtn.style.display = 'none';
    }
  } catch(err) {
    dot.className = 'gcal-status-dot error';
    text.textContent = 'Unable to reach server';
    detail.textContent = String(err.message || err);
  }
}

const gcalConnectBtn = document.getElementById('gcalConnectBtn');
if(gcalConnectBtn){
  gcalConnectBtn.addEventListener('click', () => {
    window.location.href = '/auth/google/login';
  });
}
const gcalDisconnectBtn = document.getElementById('gcalDisconnectBtn');
if(gcalDisconnectBtn){
  gcalDisconnectBtn.addEventListener('click', async () => {
    if(!confirm('Disconnect Google Calendar? Existing calendar events will not be deleted, but cards will stop syncing.')) return;
    await fetch('/auth/google/disconnect', {method: 'POST'});
    renderGcalSettings();
  });
}
const gcalSyncNowBtn = document.getElementById('gcalSyncNowBtn');
if(gcalSyncNowBtn){
  gcalSyncNowBtn.addEventListener('click', async () => {
    gcalSyncNowBtn.disabled = true;
    gcalSyncNowBtn.textContent = 'Syncing…';
    try {
      await fetch('/api/gcal/sync', {method: 'POST'});
      if(document.getElementById('board')){
        await render();
      } else if(typeof listRender === 'function'){
        await listRender();
      }
    } finally {
      gcalSyncNowBtn.disabled = false;
      gcalSyncNowBtn.textContent = 'Sync now';
      renderGcalSettings();
    }
  });
}

(function showGcalRedirectNotice(){
  const params = new URLSearchParams(window.location.search);
  if(params.has('gcal_connected')){
    window.history.replaceState({}, '', window.location.pathname);
  } else if(params.has('gcal_error')){
    alert('Google Calendar connection failed: ' + params.get('gcal_error'));
    window.history.replaceState({}, '', window.location.pathname);
  }
})();

function openSettingsModal(tabId){
  const modal = document.getElementById('settingsModal');
  if(!modal) return;
  const modalContent = modal.querySelector('.modal-content');
  const modalPurple = '#b78ef5';
  if(modal.classList.contains('hidden')){
    modal.classList.remove('hidden');
    if(modalContent){
      modalContent.style.borderColor = modalPurple;
      modalContent.style.borderWidth = '2px';
      modalContent.style.backgroundColor = '#0f0f18';
    }
  }
  activateSettingsTab(tabId || 'statuses');
}

function closeSettingsModal(){
  const modal = document.getElementById('settingsModal');
  if(!modal) return;
  const modalContent = modal.querySelector('.modal-content');
  if(modalContent){
    modalContent.style.borderColor = '';
    modalContent.style.borderWidth = '';
    modalContent.style.backgroundColor = '';
  }
  modal.classList.add('hidden');
}

function focusProjectInSettings(projectName){
  if(!projectName) return;
  let attempts = 0;
  const tryFocus = () => {
    const list = document.getElementById('projectsList');
    if(list){
      const inputs = Array.from(list.querySelectorAll('.project-name'));
      const target = inputs.find(inp => inp.value.trim() === projectName.trim());
      if(target){
        target.focus();
        target.select();
        return;
      }
    }
    attempts += 1;
    if(attempts < 5){
      setTimeout(tryFocus, 120);
    }
  };
  tryFocus();
}

document.querySelectorAll('.settings-tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    activateSettingsTab(btn.dataset.tab);
  });
});

const truncateBtn = document.getElementById('truncateBtn');
if(truncateBtn){
  // Only attach listener if we're on the board view (boardEl exists)
  const boardEl = document.getElementById('board');
  if (boardEl) {
    truncateBtn.addEventListener('click', ()=>{
      cardTruncationEnabled = !cardTruncationEnabled;
      localStorage.setItem('cardTruncationEnabled', cardTruncationEnabled);
      const btn = document.getElementById('truncateBtn');
      btn.setAttribute('aria-pressed', cardTruncationEnabled ? 'true' : 'false');
      btn.classList.toggle('active', cardTruncationEnabled);
      render({useLatest: true});
    });
  }
}

const settingsBtn = document.getElementById('settingsBtn');
if(settingsBtn){
  settingsBtn.addEventListener('click', ()=>{
    const modal = document.getElementById('settingsModal');
    if(modal.classList.contains('hidden')){
      openSettingsModal('statuses');
    } else {
      closeSettingsModal();
    }
  });
}

const closeSettingsBtn = document.getElementById('closeSettings');
if(closeSettingsBtn){
  closeSettingsBtn.addEventListener('click', ()=>{
    closeSettingsModal();
  });
}

const addColumnBtn = document.getElementById('addColumnBtn');
if(addColumnBtn){
  addColumnBtn.addEventListener('click', async ()=>{
    const title = document.getElementById('newColumnTitle').value.trim();
    const color = document.getElementById('newColumnColor').value;
    if(!title) return alert('title required');
    await fetch('/api/column', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title, color})});
    document.getElementById('newColumnTitle').value = '';
    document.getElementById('newColumnColor').value = '#9aa0a6';
    render();
    renderStatusSettings();
  });
}

const addProjectBtn = document.getElementById('addProjectBtn');
if(addProjectBtn){
  addProjectBtn.addEventListener('click', async ()=>{
    const name = document.getElementById('newProjectName').value.trim();
    const color = document.getElementById('newProjectColor').value;
    if(!name) return alert('name required');
    await fetch('/api/project', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name, color})});
    document.getElementById('newProjectName').value = '';
    document.getElementById('newProjectColor').value = '#5b2e8a';
    await render();
    await renderProjectSettings();
  });
}

const downloadBoardBtn = document.getElementById('downloadBoardBtn');
if(downloadBoardBtn){
  downloadBoardBtn.addEventListener('click', ()=>{
    downloadBoardJson();
  });
}

const importBoardBtn = document.getElementById('importBoardBtn');
if(importBoardBtn){
  importBoardBtn.addEventListener('click', ()=>{
    importBoardJson();
  });
}

// close modal when clicking backdrop
document.addEventListener('click', (e)=>{
  const modal = document.getElementById('settingsModal');
  if(modal && e.target.classList.contains('modal-backdrop') && modal.contains(e.target)) closeSettingsModal();
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
      <label>Linked user <span class="linked-user-hint">(whose Google Calendar this syncs to)</span>:</label>
      <select id="editCardAssignedUser" class="modal-input" data-assigned-user-select></select>
      <label>Due date: <span class="due-date-sync-badge hidden" id="editCardDueSyncBadge" title="Synced to Google Calendar">🔗 synced</span></label>
      <div class="due-date-row">
        <input id="editCardDueDate" class="modal-input" type="datetime-local" />
        <label class="due-date-allday"><input type="checkbox" id="editCardAllDay" /> All day</label>
        <button type="button" id="editCardClearDueDate" title="Clear due date">✕</button>
      </div>
      <label>Description:</label>
      <textarea id="editCardDesc" placeholder="Description (optional)"></textarea>
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
  refreshAssignedUserSelects();
  document.getElementById('editCardAssignedUser').value = card.assigned_user_id || '';
  const editProjectInput = document.getElementById('editCardProject');
  populateProjectInput(editProjectInput, card.project || '');
  renderLinkRows(card.links || []);
  document.getElementById('editCardAllDay').checked = !!card.all_day;
  document.getElementById('editCardDueDate').value = card.due_date
    ? (card.all_day ? card.due_date.slice(0,10) + 'T00:00' : card.due_date.slice(0,16))
    : '';
  document.getElementById('editCardDueSyncBadge').classList.toggle('hidden', !card.gcal_event_id);

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

function readDueDateFromForm(){
  const allDay = document.getElementById('editCardAllDay').checked;
  const raw = document.getElementById('editCardDueDate').value;
  if(!raw) return {due_date: '', all_day: allDay};
  return {due_date: allDay ? raw.slice(0,10) : raw, all_day: allDay};
}

document.getElementById('editCardClearDueDate').addEventListener('click', ()=>{
  document.getElementById('editCardDueDate').value = '';
  document.getElementById('editCardAllDay').checked = false;
});

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
  const assigned_user_id = document.getElementById('editCardAssignedUser').value;
  if(!title) return alert('title required');
  const links = collectLinkRows();
  const project = document.getElementById('editCardProject').value;
  const {due_date, all_day} = readDueDateFromForm();

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
    const res = await fetch('/api/card', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title, description, links, project, assignee, assigned_user_id, due_date, all_day, column: columnId})});
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
  const assigned_user_id = document.getElementById('editCardAssignedUser').value;
  if(!title) return alert('title required');
  const links = collectLinkRows();
  const project = document.getElementById('editCardProject').value;
  const {due_date, all_day} = readDueDateFromForm();
  try {
    const res = await fetch('/api/card/' + cardId, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({title, description, links, project, assignee, assigned_user_id, due_date, all_day})});
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

// Local users are needed for the card editor's linked-user dropdown on
// both the board and list pages, so fetch them regardless of view.
fetchUsers().then(refreshAssignedUserSelects);

// Initial render (only on board view page)
const boardEl = document.getElementById('board');
if(boardEl){
  render();
}

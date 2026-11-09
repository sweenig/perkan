// List view - tabular view of all tasks sorted by GUSV (Global Unified Sort Value)
// which is the order value across all columns

let listLatestBoard = null;
let listActiveProjectFilter = null;
let listDraggedCardId = null;
let listDraggedOriginalIndex = null;
let listDropZonesVisible = false;

async function listFetchBoard() {
  const res = await fetch('/api/board');
  listLatestBoard = await res.json();
  return listLatestBoard;
}

// Collect all cards from all columns in GUSV order
function getAllCardsInGUSVOrder(board) {
  const allCards = [];
  if (!board || !Array.isArray(board.columns)) {
    return allCards;
  }
  
  // Collect all cards with their column info
  for (const col of board.columns) {
    if (col.hidden || !Array.isArray(col.cards)) continue;
    for (const card of col.cards) {
      allCards.push({
        ...card,
        columnId: col.id,
        columnTitle: col.title,
        columnColor: col.color || '#e0e0e0'
      });
    }
  }
  
  // Sort by order (GUSV)
  allCards.sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
  return allCards;
}

function listHexToRgba(hex, alpha) {
  if (!hex) return '';
  hex = hex.replace('#', '');
  if (hex.length === 3) {
    hex = hex.split('').map(c => c + c).join('');
  }
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function listEscapeHtml(s) {
  return s.replace(/[&<>"']/g, function(m) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m];
  });
}

function listCreateTableRow(card, index, projectMap) {
  const row = document.createElement('tr');
  row.className = 'list-table-row';
  row.draggable = true;
  row.dataset.cardId = card.id;
  row.dataset.order = card.order !== undefined ? card.order : 0;
  row.dataset.rowIndex = index;

  // Get project details
  const projectName = card.project || '';
  const projectDetails = projectMap && projectMap.get(projectName);
  const projectColor = projectDetails && projectDetails.color ? projectDetails.color : null;
  const cardColor = projectColor || card.color || '#5b2e8a';

  // Create the background color for the row based on column color
  const columnColorBg = listHexToRgba(card.columnColor, 0.05);
  row.style.backgroundColor = columnColorBg;
  row.style.borderLeftColor = readableOnDark(card.columnColor, 3);
  row.style.borderLeftWidth = '4px';
  row.style.borderLeftStyle = 'solid';

  const linkCount = Array.isArray(card.links) ? card.links.length : 0;
  const assignee = card.assignee || '';

  // Title column
  const titleCell = document.createElement('td');
  titleCell.className = 'list-cell list-cell-title';
  titleCell.innerHTML = `
    <div class="list-cell-content">${listEscapeHtml(card.title)}</div>
  `;
  row.appendChild(titleCell);

  // Description column
  const descCell = document.createElement('td');
  descCell.className = 'list-cell list-cell-description';
  const desc = card.description || '';
  if (desc) {
    // Check if truncation is enabled (global variable from app.js)
    const isTruncated = typeof cardTruncationEnabled !== 'undefined' ? cardTruncationEnabled : localStorage.getItem('cardTruncationEnabled') !== 'false';
    const truncateClass = isTruncated ? ' list-desc-truncated' : '';
    descCell.innerHTML = `
      <div class="list-cell-content list-desc-content${truncateClass}">${listEscapeHtml(desc)}</div>
    `;
  } else {
    descCell.innerHTML = '<span class="list-muted">—</span>';
  }
  row.appendChild(descCell);

  // Project column
  const projectCell = document.createElement('td');
  projectCell.className = 'list-cell list-cell-project';
  if (projectName) {
    const badgeColor = projectColor || '#5b2e8a';
    projectCell.innerHTML = `
      <div class="list-project-badge" style="background: ${listHexToRgba(badgeColor, 0.12)}; border: 2px solid ${readableOnDark(badgeColor, 3)}; color: ${readableOnDark(badgeColor)};">
        ${listEscapeHtml(projectName)}
      </div>
    `;
  } else {
    projectCell.innerHTML = '<span class="list-muted">—</span>';
  }
  row.appendChild(projectCell);

  // Assignee column
  const assigneeCell = document.createElement('td');
  assigneeCell.className = 'list-cell list-cell-assignee';
  if (assignee) {
    assigneeCell.innerHTML = `
      <div class="list-assignee">👤 ${listEscapeHtml(assignee)}</div>
    `;
  } else {
    assigneeCell.innerHTML = '<span class="list-muted">—</span>';
  }
  row.appendChild(assigneeCell);

  // Column status
  const columnCell = document.createElement('td');
  columnCell.className = 'list-cell list-cell-column';
  columnCell.innerHTML = `
    <div class="list-column-badge" style="background: ${listHexToRgba(card.columnColor, 0.12)}; border: 2px solid ${readableOnDark(card.columnColor, 3)}; color: ${readableOnDark(card.columnColor)};">
      ${listEscapeHtml(card.columnTitle)}
    </div>
  `;
  row.appendChild(columnCell);

  // Started / completed dates
  [card.started_at, card.completed_at].forEach(iso => {
    const dateCell = document.createElement('td');
    dateCell.className = 'list-cell list-cell-date';
    dateCell.innerHTML = iso ? `<div class="list-cell-content">${listEscapeHtml(fmtDay(iso))}</div>` : '<span class="list-muted">—</span>';
    row.appendChild(dateCell);
  });

  // Links button
  const linksCell = document.createElement('td');
  linksCell.className = 'list-cell list-cell-links';
  if (linkCount > 0) {
    const linksBtn = document.createElement('button');
    linksBtn.className = 'list-links-btn';
    linksBtn.title = `${linkCount} link${linkCount !== 1 ? 's' : ''}`;
    linksBtn.textContent = `🔗 ${linkCount}`;
    linksBtn.addEventListener('mouseenter', () => {
      showLinkPreview(card.links, linksBtn);
    });
    linksBtn.addEventListener('mouseleave', () => {
      scheduleLinkPreviewHide();
    });
    linksCell.appendChild(linksBtn);
  } else {
    linksCell.innerHTML = '<span class="list-muted">—</span>';
  }
  row.appendChild(linksCell);

  // Edit button
  const editCell = document.createElement('td');
  editCell.className = 'list-cell list-cell-edit';
  const editBtn = document.createElement('button');
  editBtn.className = 'list-edit-btn';
  editBtn.title = 'Edit task';
  editBtn.textContent = '✏️';
  editBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    // Ensure modal exists (created by app.js) before trying to use it
    const modal = document.getElementById('cardEditModal');
    if (!modal) {
      console.error('Card edit modal not found - app.js may not have loaded');
      return;
    }
    await openCardEditModal(card);
  });
  editCell.appendChild(editBtn);
  row.appendChild(editCell);

  // Drag event handlers
  row.addEventListener('dragstart', (e) => {
    listDraggedCardId = card.id;
    listDraggedOriginalIndex = index;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', card.id);
    setTimeout(() => {
      row.classList.add('dragging');
      listShowDropZones();
    }, 0);
  });

  row.addEventListener('dragend', () => {
    row.classList.remove('dragging');
    listHideDropZones();
    listDraggedCardId = null;
    listDraggedOriginalIndex = null;
  });

  return row;
}

function listMakeDropZone(beforeIndex, afterIndex) {
  const zone = document.createElement('tr');
  zone.className = 'list-drop-zone';
  zone.dataset.beforeIndex = beforeIndex !== null ? beforeIndex : '';
  zone.dataset.afterIndex = afterIndex !== null ? afterIndex : '';

  const cell = document.createElement('td');
  cell.colSpan = 7;
  cell.className = 'list-drop-zone-cell';
  cell.textContent = 'Drop here';
  zone.appendChild(cell);

  zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    zone.classList.add('dragover');
  });
  zone.addEventListener('dragleave', () => {
    zone.classList.remove('dragover');
  });
  zone.addEventListener('drop', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    zone.classList.remove('dragover');
    listHideDropZones();

    const id = e.dataTransfer.getData('text/plain') || listDraggedCardId;
    if (!id) return;

    const board = await listFetchBoard();
    const allCards = getAllCardsInGUSVOrder(board);
    const draggedCard = allCards.find(c => c.id === id);
    if (!draggedCard) return;

    let newOrder;
    const beforeIndex = zone.dataset.beforeIndex !== '' ? parseInt(zone.dataset.beforeIndex) : null;
    const afterIndex = zone.dataset.afterIndex !== '' ? parseInt(zone.dataset.afterIndex) : null;

    if (beforeIndex === null && afterIndex === null) {
      newOrder = 1000000;
    } else if (beforeIndex === null) {
      newOrder = allCards[afterIndex].order - 1000000;
    } else if (afterIndex === null) {
      newOrder = allCards[beforeIndex].order + 1000000;
    } else {
      newOrder = (allCards[beforeIndex].order + allCards[afterIndex].order) / 2;
    }

    // Update the card's order (keep in same column)
    const res = await fetch('/api/card/' + id, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order: newOrder })
    });

    if (!res.ok) {
      alert('Unable to reorder card. Please try again.');
    }
    listDraggedCardId = null;
    listDraggedOriginalIndex = null;
    listRender();
  });

  return zone;
}

function listShowDropZones() {
  if (listDropZonesVisible) return;
  listDropZonesVisible = true;

  const tbody = document.querySelector('.list-table tbody');
  if (!tbody) return;

  const allRows = Array.from(tbody.querySelectorAll('tr.list-table-row'));
  if (allRows.length === 0) {
    tbody.appendChild(listMakeDropZone(null, null));
    return;
  }

  // Add drop zone at the beginning
  tbody.insertAdjacentElement('afterbegin', listMakeDropZone(null, 0));

  // Add drop zones between each pair of rows
  for (let i = 0; i < allRows.length - 1; i++) {
    const zone = listMakeDropZone(i, i + 1);
    allRows[i].insertAdjacentElement('afterend', zone);
  }

  // Add drop zone at the end
  tbody.appendChild(listMakeDropZone(allRows.length - 1, null));
}

function listHideDropZones() {
  if (!listDropZonesVisible) return;
  listDropZonesVisible = false;
  document.querySelectorAll('.list-drop-zone').forEach(zone => zone.remove());
}

async function listRender(options = {}) {
  const useLatest = !!options.useLatest;
  const board = useLatest && listLatestBoard ? listLatestBoard : await listFetchBoard();
  const listEl = document.getElementById('listView');

  // Build project map
  const projectMap = new Map();
  if (Array.isArray(board.projects)) {
    board.projects.forEach(proj => {
      if (proj && proj.name) {
        projectMap.set(proj.name, proj);
      }
    });
  }

  // Render project filters
  listRenderProjectFilters(board, projectMap);

  listHideDropZones();
  hideLinkPreviewImmediate();

  // Get all cards in GUSV order
  let allCards = getAllCardsInGUSVOrder(board);

  // Apply project filter if active
  if (listActiveProjectFilter) {
    allCards = allCards.filter(card => {
      const projectName = card.project ? card.project.trim() : '';
      return projectName === listActiveProjectFilter;
    });
  }

  // Create table
  listEl.innerHTML = '';
  const table = document.createElement('table');
  table.className = 'list-table';

  // Table header
  const thead = document.createElement('thead');
  const headerRow = document.createElement('tr');
  headerRow.className = 'list-table-header';

  const headers = ['Title', 'Description', 'Project', 'Assignee', 'Status', 'Started', 'Completed', '', ''];
  headers.forEach(headerText => {
    const th = document.createElement('th');
    th.className = 'list-table-header-cell';
    th.textContent = headerText;
    headerRow.appendChild(th);
  });
  thead.appendChild(headerRow);
  table.appendChild(thead);

  // Table body
  const tbody = document.createElement('tbody');
  allCards.forEach((card, index) => {
    tbody.appendChild(listCreateTableRow(card, index, projectMap));
  });
  table.appendChild(tbody);

  listEl.appendChild(table);

  if (allCards.length === 0) {
    const emptyMsg = document.createElement('div');
    emptyMsg.className = 'list-empty-message';
    emptyMsg.textContent = listActiveProjectFilter 
      ? `No tasks found for project "${listActiveProjectFilter}"`
      : 'No tasks yet. Add some from the board view.';
    listEl.appendChild(emptyMsg);
  }
}

function listRenderProjectFilters(board, projectMap) {
  const container = document.getElementById('projectFilters');
  if (!container) return;

  // Collect project counts from all cards
  const projectCounts = new Map();
  if (Array.isArray(board.columns)) {
    board.columns.forEach(col => {
      if (!col.hidden && Array.isArray(col.cards)) {
        col.cards.forEach(card => {
          const projectName = card && card.project ? card.project.trim() : '';
          if (projectName) {
            projectCounts.set(projectName, (projectCounts.get(projectName) || 0) + 1);
          }
        });
      }
    });
  }

  if (listActiveProjectFilter && !projectCounts.has(listActiveProjectFilter)) {
    listActiveProjectFilter = null;
  }

  container.innerHTML = '';
  if (projectCounts.size === 0) {
    const placeholder = document.createElement('div');
    placeholder.className = 'project-filters-empty';
    placeholder.textContent = 'Tag tasks with a project to enable filtering.';
    container.appendChild(placeholder);
    return;
  }

  const seen = new Set();
  const orderedNames = [];
  if (Array.isArray(board.projects)) {
    board.projects.forEach(proj => {
      const name = proj && proj.name ? proj.name.trim() : '';
      if (name && projectCounts.has(name) && !seen.has(name)) {
        orderedNames.push(name);
        seen.add(name);
      }
    });
  }
  projectCounts.forEach((_, name) => {
    if (!seen.has(name)) {
      orderedNames.push(name);
      seen.add(name);
    }
  });

  orderedNames.forEach(name => {
    const count = projectCounts.get(name) || 0;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'project-filter-pill' + (listActiveProjectFilter === name ? ' active' : '');
    button.dataset.project = name;
    button.setAttribute('aria-pressed', listActiveProjectFilter === name ? 'true' : 'false');

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
      listActiveProjectFilter = listActiveProjectFilter === name ? null : name;
      listRender({ useLatest: true });
    });

    container.appendChild(button);
  });
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
  listRender();

  // Setup truncate button
  const truncateBtn = document.getElementById('truncateBtn');
  if (truncateBtn) {
    truncateBtn.addEventListener('click', () => {
      cardTruncationEnabled = !cardTruncationEnabled;
      localStorage.setItem('cardTruncationEnabled', cardTruncationEnabled);
      const btn = document.getElementById('truncateBtn');
      btn.setAttribute('aria-pressed', cardTruncationEnabled ? 'true' : 'false');
      btn.classList.toggle('active', cardTruncationEnabled);
      listRender({ useLatest: true });
    });
    // Set initial state
    if (typeof cardTruncationEnabled !== 'undefined' && cardTruncationEnabled) {
      truncateBtn.setAttribute('aria-pressed', 'true');
      truncateBtn.classList.add('active');
    }
  }
});

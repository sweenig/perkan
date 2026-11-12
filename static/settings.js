// Settings page: board statuses (columns), projects, import/export and about.

async function fetchBoard() {
  const res = await fetch('/api/board?include_hidden=true');
  return res.json();
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, function(m){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;'}[m];});
}

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
      <label class="col-hide-toggle" title="Cards moved into this column get a completion date">
        <input class="col-done" type="checkbox" data-id="${col.id}" ${col.is_done ? 'checked' : ''} />
        Done
      </label>
      <label class="col-hide-toggle" title="The done column new finished cards go to (only one; setting it clears the others)">
        <input class="col-current-done" type="radio" name="col-current-done" data-id="${col.id}" ${col.is_current_done ? 'checked' : ''} />
        Current
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
      renderStatusSettings();
    });
  });
  document.querySelectorAll('.col-color').forEach(inp=>{
    inp.addEventListener('change', async (e)=>{
      const id = e.target.dataset.id;
      const color = e.target.value;
      await fetch('/api/column/' + id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({color})});
      renderStatusSettings();
    });
  });
  document.querySelectorAll('.col-done').forEach(inp=>{
    inp.addEventListener('change', async (e)=>{
      const id = e.target.dataset.id;
      const is_done = e.target.checked;
      await fetch('/api/column/' + id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({is_done})});
    });
  });
  document.querySelectorAll('.col-current-done').forEach(inp=>{
    inp.addEventListener('change', async (e)=>{
      const id = e.target.dataset.id;
      await fetch('/api/column/' + id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({is_current_done:true})});
    });
  });
  document.querySelectorAll('.col-hidden').forEach(inp=>{
    inp.addEventListener('change', async (e)=>{
      const id = e.target.dataset.id;
      const hidden = e.target.checked;
      await fetch('/api/column/' + id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({hidden})});
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
      renderStatusSettings();
    });
  });
  document.querySelectorAll('.col-down').forEach(btn=>{
    btn.addEventListener('click', async (e)=>{
      const id = e.target.dataset.id;
      const idx = parseInt(e.target.dataset.idx, 10);
      const targetPos = idx + 1;
      await fetch('/api/column/' + id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({position:targetPos})});
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
      renderStatusSettings();
    });
  });

}

// Project links are edited as text, one per line: "label | url" or just the url.
function projectLinksToText(links){
  return (Array.isArray(links) ? links : []).map(l => (l.text && l.text !== l.url) ? `${l.text} | ${l.url}` : l.url).join('\n');
}

function projectLinksFromText(text){
  return text.split('\n').map(line => line.trim()).filter(Boolean).map(line => {
    const bar = line.lastIndexOf('|');
    if(bar === -1) return {text: line, url: line};
    return {text: line.slice(0, bar).trim(), url: line.slice(bar + 1).trim()};
  });
}

let projectsCache = [];

// The list only shows the projects (the server sorts them alphabetically); adding, editing, recolouring and deleting happen in the modal.
async function renderProjectSettings(){
  const board = await fetchBoard();
  const list = document.getElementById('projectsList');
  if(!list) return;
  list.innerHTML = '';
  const projects = projectsCache = Array.isArray(board.projects) ? board.projects : [];
  if(!projects.length){
    const empty = document.createElement('div');
    empty.className = 'project-empty';
    empty.textContent = 'No projects yet';
    list.appendChild(empty);
  }
  projects.forEach((proj, idx) => {
    const color = proj.color || '#5b2e8a';
    const row = document.createElement('div');
    row.className = 'project-row';
    row.innerHTML = `
      <div class="project-badge" style="background:${hexToRgba(color, 0.12)};border:2px solid ${readableOnDark(color, 3)};color:${readableOnDark(color)};">
        <span class="project-badge-name">${escapeHtml(proj.name || '')}</span>
        ${proj.repo_path ? `<span class="project-badge-repo">${escapeHtml(proj.repo_path)}</span>` : ''}
      </div>
      <button type="button" class="project-edit" data-index="${idx}" title="Edit project">✏️</button>
    `;
    list.appendChild(row);
  });

  list.querySelectorAll('.project-edit').forEach(btn => {
    btn.addEventListener('click', () => openProjectModal(parseInt(btn.dataset.index, 10)));
  });
}

function hexToRgba(hex, alpha){
  const m = /^#?([0-9a-f]{6})$/i.exec((hex || '').trim());
  if(!m) return `rgba(91,46,138,${alpha})`;
  const n = parseInt(m[1], 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${alpha})`;
}

// Project modal: one form for adding (index null) and editing a project.
let editingProjectIndex = null;

function projectModalEl(id){ return document.getElementById(id); }

function openProjectModal(index){
  editingProjectIndex = index;
  const proj = index === null ? {} : (projectsCache[index] || {});
  projectModalEl('projectModalTitle').textContent = index === null ? 'Add project' : 'Edit project';
  projectModalEl('projectName').value = proj.name || '';
  projectModalEl('projectColor').value = proj.color || '#5b2e8a';
  projectModalEl('projectRepoPath').value = proj.repo_path || '';
  projectModalEl('projectRepoUrl').value = proj.repo_url || '';
  projectModalEl('projectNotes').value = proj.notes || '';
  projectModalEl('projectLinks').value = projectLinksToText(proj.links);
  projectModalEl('projectDeleteBtn').style.display = index === null ? 'none' : '';
  projectModalEl('projectError').textContent = '';
  projectModalEl('projectModal').classList.remove('hidden');
  projectModalEl('projectName').focus();
}

function closeProjectModal(){
  projectModalEl('projectModal').classList.add('hidden');
  editingProjectIndex = null;
}

async function saveProjectModal(){
  const name = projectModalEl('projectName').value.trim();
  const err = projectModalEl('projectError');
  if(!name){ err.textContent = 'A project name is required.'; return; }
  const body = {
    name,
    color: projectModalEl('projectColor').value,
    repo_path: projectModalEl('projectRepoPath').value.trim(),
    repo_url: projectModalEl('projectRepoUrl').value.trim(),
    notes: projectModalEl('projectNotes').value.trim(),
    links: projectLinksFromText(projectModalEl('projectLinks').value),
  };
  const isNew = editingProjectIndex === null;
  const res = await fetch(isNew ? '/api/project' : '/api/project/' + editingProjectIndex,
                          {method: isNew ? 'POST' : 'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if(!res.ok){
    const data = await res.json().catch(() => ({}));
    err.textContent = data.error || 'Could not save the project.';
    return;
  }
  closeProjectModal();
  await renderProjectSettings();
}

async function deleteProjectFromModal(){
  if(editingProjectIndex === null) return;
  const name = (projectsCache[editingProjectIndex] || {}).name || 'this project';
  if(!confirm(`Delete "${name}"? Its cards are kept but removed from the project.`)) return;
  await fetch('/api/project/' + editingProjectIndex, {method:'DELETE'});
  closeProjectModal();
  await renderProjectSettings();
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
  try { history.replaceState(null, '', '?tab=' + encodeURIComponent(tabId)); } catch(e) {}
  document.querySelectorAll('.settings-tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  });
  document.querySelectorAll('.settings-tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.dataset.tab === tabId);
  });
  if(tabId === 'statuses'){
    renderStatusSettings();
  } else if(tabId === 'projects'){
    renderProjectSettings();
  }
}

document.querySelectorAll('.settings-tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    activateSettingsTab(btn.dataset.tab);
  });
});


const addColumnBtn = document.getElementById('addColumnBtn');
if(addColumnBtn){
  addColumnBtn.addEventListener('click', async ()=>{
    const title = document.getElementById('newColumnTitle').value.trim();
    const color = document.getElementById('newColumnColor').value;
    if(!title) return alert('title required');
    await fetch('/api/column', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title, color})});
    document.getElementById('newColumnTitle').value = '';
    document.getElementById('newColumnColor').value = '#9aa0a6';
    renderStatusSettings();
  });
}

const addProjectBtn = document.getElementById('addProjectBtn');
if(addProjectBtn) addProjectBtn.addEventListener('click', () => openProjectModal(null));
const projectModal = document.getElementById('projectModal');
if(projectModal){
  projectModal.querySelector('.modal-backdrop').addEventListener('click', closeProjectModal);
  document.getElementById('projectCancelBtn').addEventListener('click', closeProjectModal);
  document.getElementById('projectSaveBtn').addEventListener('click', saveProjectModal);
  document.getElementById('projectDeleteBtn').addEventListener('click', deleteProjectFromModal);
  projectModal.addEventListener('keydown', (e) => {
    if(e.key === 'Escape') closeProjectModal();
    else if(e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); saveProjectModal(); }
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


// Deep links: /settings?tab=projects&project=<name> opens that tab and the project's edit form.
(function init(){
  const params = new URLSearchParams(window.location.search);
  const tabs = Array.from(document.querySelectorAll('.settings-tab-btn')).map(b => b.dataset.tab);
  const tab = tabs.includes(params.get('tab')) ? params.get('tab') : 'statuses';
  activateSettingsTab(tab);
  const project = params.get('project');
  if(project && tab === 'projects'){
    renderProjectSettings().then(() => {
      const idx = projectsCache.findIndex(p => p.name === project.trim());
      if(idx !== -1) openProjectModal(idx);
    });
  }
})();

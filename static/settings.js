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
      await renderProjectSettings();
    });
  });
  list.querySelectorAll('.project-color').forEach(inp => {
    inp.addEventListener('change', async (e) => {
      const idx = parseInt(e.target.dataset.index, 10);
      const color = e.target.value;
      await fetch('/api/project/' + idx, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({color})});
      await renderProjectSettings();
    });
  });
  list.querySelectorAll('.project-up').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      const idx = parseInt(e.target.dataset.index, 10);
      const targetPos = Math.max(0, idx - 1);
      await fetch('/api/project/' + idx, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({position:targetPos})});
      await renderProjectSettings();
    });
  });
  list.querySelectorAll('.project-down').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      const idx = parseInt(e.target.dataset.index, 10);
      const targetPos = idx + 1;
      await fetch('/api/project/' + idx, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({position:targetPos})});
      await renderProjectSettings();
    });
  });
  list.querySelectorAll('.project-delete').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      const idx = parseInt(e.target.dataset.index, 10);
      if(!confirm('Delete this project?')) return;
      await fetch('/api/project/' + idx, {method:'DELETE'});
      await renderProjectSettings();
    });
  });

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
if(addProjectBtn){
  addProjectBtn.addEventListener('click', async ()=>{
    const name = document.getElementById('newProjectName').value.trim();
    const color = document.getElementById('newProjectColor').value;
    if(!name) return alert('name required');
    await fetch('/api/project', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name, color})});
    document.getElementById('newProjectName').value = '';
    document.getElementById('newProjectColor').value = '#5b2e8a';
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


// Deep links: /settings?tab=projects&project=<name> opens that tab and focuses the project.
(function init(){
  const params = new URLSearchParams(window.location.search);
  const tabs = Array.from(document.querySelectorAll('.settings-tab-btn')).map(b => b.dataset.tab);
  const tab = tabs.includes(params.get('tab')) ? params.get('tab') : 'statuses';
  activateSettingsTab(tab);
  const project = params.get('project');
  if(project && tab === 'projects') focusProjectInSettings(project);
})();

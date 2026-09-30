// Notebook tab: Jupyter-style cells that run inside Studio (sharing its scope and target), .ipynb files, NewAE's tutorials.
import { h, get, post, put, del, upload, toast, fmtBytes } from './api.js';

const I = {
  play: '<svg class="i" viewBox="0 0 24 24"><polygon points="7,4 20,12 7,20" fill="currentColor"/></svg>',
  runall: '<svg class="i" viewBox="0 0 24 24"><polygon points="4,4 12,12 4,20" fill="currentColor"/><polygon points="12,4 20,12 12,20" fill="currentColor"/></svg>',
  stop: '<svg class="i" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor"/></svg>',
  restart: '<svg class="i" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 2.6-6.4M3 3v6h6"/></svg>',
  save: '<svg class="i" viewBox="0 0 24 24"><path d="M5 3h11l3 3v15H5zM8 3v6h8V3M8 21v-7h8v7"/></svg>',
  plus: '<svg class="i" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  up: '<svg class="i" viewBox="0 0 24 24"><path d="M6 15l6-6 6 6"/></svg>',
  down: '<svg class="i" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>',
  trash: '<svg class="i" viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>',
  dl: '<svg class="i" viewBox="0 0 24 24"><path d="M12 3v12M7 10l5 5 5-5M4 21h16"/></svg>',
  wave: '<svg class="i" viewBox="0 0 24 24"><path d="M2 12h4l3-8 4 16 3-8h6"/></svg>',
  eraser: '<svg class="i" viewBox="0 0 24 24"><path d="M7 21h10M5 15l9-9 5 5-9 9H7z"/></svg>',
};
const ib = (cls, icon, label, onclick, title) => h('button', { class: 'btn ' + cls, onclick, title: title || label, html: I[icon] + (label ? `<span>${label}</span>` : '') });
const stripAnsi = (s) => s.replace(/\x1b\[[0-9;]*[A-Za-z]/g, '');
// Apply carriage returns like a terminal so tqdm progress bars update in place.
function applyCR(text) {
  return text.split('\n').map((line) => { const i = line.lastIndexOf('\r'); return i >= 0 && i < line.length - 1 ? line.slice(i + 1) : line.replace(/\r$/, ''); }).join('\n');
}

export function renderMarkdown(src, baseDir) {
  const html = window.marked ? window.marked.parse(src || '', { gfm: true, breaks: false }) : h('pre', {}, src).outerHTML;
  const clean = window.DOMPurify ? window.DOMPurify.sanitize(html, { USE_PROFILES: { html: true } }) : '';
  const div = h('div', { class: 'md', html: clean });
  div.querySelectorAll('img').forEach((img) => {
    const s = img.getAttribute('src') || '';
    if (s && !/^(https?:|data:|\/)/.test(s)) img.src = `/api/notebooks/asset?path=${encodeURIComponent((baseDir ? baseDir + '/' : '') + s)}`;
  });
  div.querySelectorAll('a[href]').forEach((a) => { a.target = '_blank'; a.rel = 'noopener'; });
  return div;
}

export function initNotebook(ctx, sideEl, viewEl) {
  let nbPath = null, nb = null, dirty = false, saveTimer = null, kernel = { busy: false, queued: [] }, focusId = null;
  const cellEls = new Map();
  const tracesAtStart = { n: 0 };

  // ---------------- main view ----------------
  const nameEl = h('span', { class: 'nb-name' }, 'No notebook open');
  const dirtyEl = h('span', { class: 'muted nb-dirty' });
  const kernelPill = h('span', { class: 'badge' }, 'Kernel idle');
  const tracesBtn = ib('sm', 'wave', 'View traces', () => ctx.showTab('capture'), 'Show traces captured by the notebook in the Capture tab');
  tracesBtn.style.display = 'none';
  const cellsEl = h('div', { class: 'nb-cells' });
  const emptyEl = h('div', { class: 'nb-empty' },
    h('div', { html: '<svg viewBox="0 0 24 24"><path d="M5 3h11l3 3v15H5z"/><path d="M8 9h8M8 13h8M8 17h5"/></svg>' }),
    h('b', {}, 'Open or create a notebook'),
    h('div', {}, 'Pick one on the left, import an .ipynb, or download NewAE\'s tutorial notebooks.'));
  const toolbar = h('div', { class: 'toolbar nb-toolbar' },
    nameEl, dirtyEl, h('div', { class: 'sep' }),
    ib('sm', 'save', 'Save', () => save(true), 'Save (Ctrl+S)'),
    ib('sm', 'plus', 'Code', () => addCell('code'), 'Insert a code cell below (B)'),
    ib('sm', 'plus', 'Text', () => addCell('markdown'), 'Insert a text (markdown) cell below'),
    h('div', { class: 'sep' }),
    ib('sm primary', 'play', 'Run', () => runFocused(true), 'Run the selected cell and move on (Shift+Enter)'),
    ib('sm', 'runall', 'Run all', runAll),
    ib('sm danger', 'stop', 'Stop', () => post('/api/kernel/interrupt').catch((e) => toast(e.message, 'err')), 'Interrupt the running cell (also clears queued cells)'),
    ib('sm ghost', 'restart', 'Restart', restart, 'Restart the kernel: clears all variables'),
    ib('sm ghost', 'eraser', 'Clear outputs', clearOutputs),
    h('div', { class: 'spacer' }), tracesBtn, kernelPill,
    ib('sm ghost', 'dl', '', () => { if (nbPath) window.open(`/api/notebooks/download?path=${encodeURIComponent(nbPath)}`); }, 'Download .ipynb'));
  viewEl.append(toolbar, h('div', { class: 'nb-scroll' }, emptyEl, cellsEl));

  function baseDir() { return nbPath && nbPath.includes('/') ? nbPath.slice(0, nbPath.lastIndexOf('/')) : ''; }
  function markDirty() { dirty = true; dirtyEl.textContent = 'unsaved'; clearTimeout(saveTimer); saveTimer = setTimeout(() => save(false), 2500); }
  async function save(explicit) {
    if (!nbPath || !nb) return;
    clearTimeout(saveTimer);
    try { await put('/api/notebooks/file', { path: nbPath, notebook: nb }); dirty = false; dirtyEl.textContent = 'saved'; if (explicit) toast('Notebook saved', 'ok', 1500); } catch (e) { toast('Save failed: ' + e.message, 'err', 6000); }
  }

  function cellById(id) { return nb && nb.cells.find((c) => c.id === id); }
  function focusCell(id, edit) {
    focusId = id;
    cellEls.forEach((el, cid) => el.classList.toggle('focused', cid === id));
    const el = cellEls.get(id);
    if (!el) return;
    el.scrollIntoView({ block: 'nearest' });
    if (edit) { const ta = el.querySelector('textarea'); if (ta) { if (ta.style.display === 'none') el._edit(); ta.focus(); } }
  }
  function newId() { return Math.random().toString(16).slice(2, 10); }
  function addCell(type, after = focusId, source = '') {
    if (!nb) return;
    const cell = { id: newId(), cell_type: type, source, metadata: {}, ...(type === 'code' ? { outputs: [], execution_count: null } : {}) };
    const i = after ? nb.cells.findIndex((c) => c.id === after) : nb.cells.length - 1;
    nb.cells.splice(i + 1, 0, cell);
    renderCells(); markDirty(); focusCell(cell.id, true);
  }
  function moveCell(id, d) {
    const i = nb.cells.findIndex((c) => c.id === id), j = i + d;
    if (j < 0 || j >= nb.cells.length) return;
    [nb.cells[i], nb.cells[j]] = [nb.cells[j], nb.cells[i]];
    renderCells(); markDirty(); focusCell(id);
  }
  function deleteCell(id) {
    const i = nb.cells.findIndex((c) => c.id === id);
    if (i < 0) return;
    nb.cells.splice(i, 1);
    renderCells(); markDirty();
    const next = nb.cells[Math.min(i, nb.cells.length - 1)];
    if (next) focusCell(next.id);
  }

  function renderOutput(o) {
    const t = o.output_type;
    if (t === 'stream') return h('pre', { class: 'nb-out stream ' + (o.name || '') }, applyCR(stripAnsi(o.text || '')));
    if (t === 'error') return h('pre', { class: 'nb-out error' }, stripAnsi((o.traceback || []).join('\n') || `${o.ename}: ${o.evalue}`));
    const d = o.data || {};
    if (d['image/png']) return h('div', { class: 'nb-out img' }, h('img', { src: 'data:image/png;base64,' + d['image/png'] }));
    if (d['image/jpeg']) return h('div', { class: 'nb-out img' }, h('img', { src: 'data:image/jpeg;base64,' + d['image/jpeg'] }));
    if (d['image/svg+xml']) return h('div', { class: 'nb-out img', html: window.DOMPurify ? window.DOMPurify.sanitize(d['image/svg+xml'], { USE_PROFILES: { svg: true } }) : '' });
    if (d['text/html']) {
      // Untrusted HTML (e.g. saved outputs of imported notebooks) is shown in a script-free sandbox.
      const fr = h('iframe', { class: 'nb-out html', sandbox: '', srcdoc: `<!doctype html><meta charset="utf-8"><style>body{margin:0;font:13px system-ui,sans-serif;color:${getComputedStyle(document.body).color}}table{border-collapse:collapse}td,th{border:1px solid #8884;padding:2px 6px}</style>${d['text/html']}` });
      fr.addEventListener('load', () => { try { fr.style.height = Math.min(600, fr.contentDocument.documentElement.scrollHeight + 4) + 'px'; } catch (e) { fr.style.height = '200px'; } });
      return fr;
    }
    if (d['text/markdown']) return h('div', { class: 'nb-out' }, renderMarkdown(d['text/markdown'], baseDir()));
    if (d['text/plain'] != null) return h('pre', { class: 'nb-out result' }, stripAnsi(d['text/plain']));
    return null;
  }

  function renderCell(cell) {
    const el = h('div', { class: 'nb-cell ' + cell.cell_type, 'data-id': cell.id });
    const ta = h('textarea', { class: 'nb-src mono', spellcheck: 'false', rows: 1 });
    ta.value = cell.source || '';
    const autosize = () => { ta.style.height = 'auto'; ta.style.height = (ta.scrollHeight + 2) + 'px'; };
    ta.addEventListener('input', () => { cell.source = ta.value; autosize(); markDirty(); });
    ta.addEventListener('focus', () => focusCell(cell.id));
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Tab' && !e.shiftKey) { e.preventDefault(); const s = ta.selectionStart; ta.setRangeText('    ', s, ta.selectionEnd, 'end'); ta.dispatchEvent(new Event('input')); }
      else if (e.key === 'Enter' && e.shiftKey) { e.preventDefault(); runCell(cell.id, true); }
      else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runCell(cell.id, false); }
      else if (e.key === 'Enter' && e.altKey) { e.preventDefault(); runCell(cell.id, false); addCell('code', cell.id); }
      else if (e.key === 'Escape') { ta.blur(); if (cell.cell_type === 'markdown') el._render(); }
      else if (e.key === 'Enter') {
        // keep indentation, add one level after a colon
        const s = ta.selectionStart, line = ta.value.slice(ta.value.lastIndexOf('\n', s - 1) + 1, s);
        const ind = (line.match(/^\s*/) || [''])[0] + (/:\s*$/.test(line) ? '    ' : '');
        if (ind) { e.preventDefault(); ta.setRangeText('\n' + ind, s, ta.selectionEnd, 'end'); ta.dispatchEvent(new Event('input')); }
      }
    });
    const gutter = h('div', { class: 'nb-gutter' });
    const outs = h('div', { class: 'nb-outs' });
    const tools = h('div', { class: 'nb-tools' },
      h('button', { class: 'icon-btn sm', title: 'Run (Shift+Enter)', html: I.play, onclick: () => runCell(cell.id, false) }),
      h('button', { class: 'icon-btn sm', title: 'Move up', html: I.up, onclick: () => moveCell(cell.id, -1) }),
      h('button', { class: 'icon-btn sm', title: 'Move down', html: I.down, onclick: () => moveCell(cell.id, 1) }),
      h('button', { class: 'icon-btn sm', title: cell.cell_type === 'code' ? 'Make text cell' : 'Make code cell', html: cell.cell_type === 'code' ? '<b style="font-size:11px">M</b>' : '<b style="font-size:11px">Py</b>', onclick: () => { cell.cell_type = cell.cell_type === 'code' ? 'markdown' : 'code'; if (cell.cell_type === 'code') { cell.outputs = []; cell.execution_count = null; } else { delete cell.outputs; delete cell.execution_count; } renderCells(); markDirty(); focusCell(cell.id); } }),
      h('button', { class: 'icon-btn sm', title: 'Delete cell', html: I.trash, onclick: () => deleteCell(cell.id) }));
    const body = h('div', { class: 'nb-body' }, ta, outs);
    el.append(gutter, body, tools);
    el.addEventListener('mousedown', () => focusCell(cell.id));
    el._gutter = gutter;
    el._outs = outs;
    el._autosize = autosize;
    el._setOutputs = () => { outs.innerHTML = ''; (cell.outputs || []).forEach((o) => { const r = renderOutput(o); if (r) outs.append(r); }); };
    if (cell.cell_type === 'markdown') {
      const md = h('div', { class: 'nb-md', title: 'Double-click to edit' });
      body.insertBefore(md, ta);
      el._render = () => { md.innerHTML = ''; md.append(renderMarkdown(cell.source || '*Empty text cell: double-click to edit*', baseDir())); md.style.display = ''; ta.style.display = 'none'; };
      el._edit = () => { md.style.display = 'none'; ta.style.display = ''; autosize(); ta.focus(); };
      md.addEventListener('dblclick', el._edit);
      if (cell.source) el._render(); else el._edit();
    } else {
      el._edit = () => ta.focus();
    }
    setGutter(el, cell);
    el._setOutputs();
    requestAnimationFrame(autosize);
    return el;
  }
  function setGutter(el, cell, state) {
    if (cell.cell_type !== 'code') { el._gutter.textContent = ''; return; }
    el.classList.toggle('running', state === 'running');
    el.classList.toggle('queued', state === 'queued');
    el._gutter.textContent = state === 'running' ? '[*]' : state === 'queued' ? '[ ]' : `[${cell.execution_count ?? ' '}]`;
  }
  function renderCells() {
    cellsEl.innerHTML = '';
    cellEls.clear();
    emptyEl.style.display = nb ? 'none' : '';
    if (!nb) return;
    nb.cells.forEach((c) => { const el = renderCell(c); cellEls.set(c.id, el); cellsEl.append(el); });
    cellsEl.append(h('div', { class: 'nb-add' }, ib('ghost sm', 'plus', 'Code', () => addCell('code', nb.cells.length ? nb.cells[nb.cells.length - 1].id : null)), ib('ghost sm', 'plus', 'Text', () => addCell('markdown', nb.cells.length ? nb.cells[nb.cells.length - 1].id : null))));
    if (focusId) cellEls.forEach((el, id) => el.classList.toggle('focused', id === focusId));
  }

  // ---------------- execution ----------------
  async function execute(cells) {
    if (!cells.length) return;
    tracesAtStart.n = ctx.status ? ctx.status.traces.count : 0;
    try {
      await post('/api/kernel/execute', { cells: cells.map((c) => ({ id: c.id, code: c.source })), path: nbPath });
    } catch (e) { toast(e.message, 'err', 6000); }
  }
  function runCell(id, advance) {
    const cell = cellById(id);
    if (!cell) return;
    const el = cellEls.get(id);
    if (cell.cell_type === 'markdown') { el._render(); }
    else { cell.outputs = []; el._setOutputs(); execute([cell]); }
    if (advance) {
      const i = nb.cells.findIndex((c) => c.id === id);
      if (i === nb.cells.length - 1) addCell('code', id); else focusCell(nb.cells[i + 1].id, nb.cells[i + 1].cell_type === 'code');
    }
  }
  function runFocused(advance) { if (focusId) runCell(focusId, advance); else if (nb && nb.cells.length) runCell(nb.cells[0].id, advance); }
  function runAll() {
    if (!nb) return;
    const code = nb.cells.filter((c) => c.cell_type === 'code' && c.source.trim());
    code.forEach((c) => { c.outputs = []; const el = cellEls.get(c.id); if (el) el._setOutputs(); });
    nb.cells.filter((c) => c.cell_type === 'markdown').forEach((c) => { const el = cellEls.get(c.id); if (el && el._render) el._render(); });
    execute(code);
  }
  async function restart() {
    if (!confirm('Restart the kernel? All variables are cleared (Studio stays connected).')) return;
    try { await post('/api/kernel/restart'); toast('Kernel restarted', 'ok', 1500); } catch (e) { toast(e.message, 'err'); }
  }
  function clearOutputs() {
    if (!nb) return;
    nb.cells.forEach((c) => { if (c.cell_type === 'code') { c.outputs = []; c.execution_count = null; const el = cellEls.get(c.id); if (el) { el._setOutputs(); setGutter(el, c); } } });
    markDirty();
  }
  function setKernel(st) {
    kernel = st;
    kernelPill.className = 'badge ' + (st.busy ? 'warn' : 'ok');
    kernelPill.textContent = st.busy ? `Running${st.queued && st.queued.length ? ` (+${st.queued.length} queued)` : ''}` : 'Kernel idle';
    ctx.emit('kernel', st);
  }
  function onNb(ev) {
    const cell = cellById(ev.cell);
    const el = cellEls.get(ev.cell);
    if (ev.kind === 'restarted') { setKernel(ev); loadVars(); return; }
    if (ev.kind === 'queued') { kernel.queued = [...(kernel.queued || []), ev.cell]; setKernel({ ...kernel }); if (cell && el) setGutter(el, cell, 'queued'); return; }
    if (ev.kind === 'running') { kernel.queued = (kernel.queued || []).filter((c) => c !== ev.cell); setKernel({ ...kernel, busy: true, cell: ev.cell }); if (cell && el) { cell.outputs = []; el._setOutputs(); setGutter(el, cell, 'running'); } return; }
    if (ev.kind === 'output' && cell && el) {
      const o = ev.output;
      if (o.output_type === 'clear_output') cell.outputs = [];
      else if (o.output_type === 'stream' && cell.outputs.length && cell.outputs[cell.outputs.length - 1].output_type === 'stream' && cell.outputs[cell.outputs.length - 1].name === o.name) cell.outputs[cell.outputs.length - 1].text += o.text;
      else cell.outputs.push(o);
      el._setOutputs();
      return;
    }
    if (ev.kind === 'cancelled') { kernel.queued = (kernel.queued || []).filter((c) => c !== ev.cell); setKernel({ ...kernel }); if (cell && el) setGutter(el, cell); return; }
    if (ev.kind === 'done') {
      kernel.queued = (kernel.queued || []).filter((c) => c !== ev.cell);
      setKernel({ ...kernel, busy: (kernel.queued || []).length > 0 });
      if (cell && el) { cell.outputs = ev.outputs || cell.outputs; cell.execution_count = ev.execution_count; el._setOutputs(); setGutter(el, cell); markDirty(); }
      loadVars();
      ctx.refreshStatus().then(() => { const n = ctx.status ? ctx.status.traces.count : 0; if (n > tracesAtStart.n) { tracesBtn.style.display = ''; tracesBtn.querySelector('span').textContent = `View ${n} traces`; } });
    }
  }

  // ---------------- sidebar ----------------
  const listEl = h('div', { class: 'nb-list' });
  const varsEl = h('div', { class: 'nb-vars' });
  const tutInfo = h('div', { class: 'help' });
  const tutProgress = h('progress', { style: 'display:none' });
  const fileIn = h('input', { type: 'file', accept: '.ipynb', style: 'display:none', onchange: async () => {
    if (!fileIn.files.length) return;
    try { const r = await upload('/api/notebooks/import', fileIn.files[0]); toast(`Imported ${r.path}`, 'ok'); await loadList(); open(r.path); } catch (e) { toast(e.message, 'err', 6000); }
    fileIn.value = '';
  } });
  async function newNotebook() {
    const name = prompt('Notebook name', 'Untitled');
    if (name === null) return;
    try { const r = await post('/api/notebooks/new', { name }); await loadList(); open(r.path); } catch (e) { toast(e.message, 'err'); }
  }
  async function fetchTutorials() {
    try { renderTut(await post('/api/notebooks/tutorials/fetch')); } catch (e) { toast(e.message, 'err', 6000); }
  }
  function renderTut(t) {
    const busy = t.job && ['resolving', 'downloading', 'extracting'].includes(t.job.state);
    tutProgress.style.display = busy ? '' : 'none';
    if (busy) { if (t.job.total) { tutProgress.max = t.job.total; tutProgress.value = t.job.done; } else tutProgress.removeAttribute('value'); }
    tutInfo.textContent = busy ? `${t.job.state}…` : t.job && t.job.state === 'error' ? t.job.error : t.installed ? `chipwhisperer-jupyter ${t.installed.commit.slice(0, 8)}${t.firmware_linked ? '' : ' (download firmware sources in the Firmware tab so build cells work)'}` : 'NewAE\'s courses (SCA101, Fault101, ...) and demos, matched to your firmware sources.';
    if (t.job && t.job.state === 'installed' && !busy) loadList();
  }
  async function del_(path) {
    if (!confirm(`Delete ${path}?`)) return;
    try { await del(`/api/notebooks/file?path=${encodeURIComponent(path)}`); if (path === nbPath) { nb = null; nbPath = null; nameEl.textContent = 'No notebook open'; renderCells(); } loadList(); } catch (e) { toast(e.message, 'err'); }
  }
  async function loadList() {
    let r;
    try { r = await get('/api/notebooks'); } catch (e) { return; }
    renderTut(r.tutorials);
    const groups = {};
    r.notebooks.forEach((n) => { const d = n.path.includes('/') ? n.path.slice(0, n.path.lastIndexOf('/')) : ''; (groups[d] = groups[d] || []).push(n); });
    listEl.innerHTML = '';
    if (!r.notebooks.length) listEl.append(h('div', { class: 'help' }, 'No notebooks yet.'));
    Object.keys(groups).sort((a, b) => (a === '' ? -1 : b === '' ? 1 : a.localeCompare(b))).forEach((d) => {
      const items = groups[d].map((n) => {
        const name = n.path.slice(d ? d.length + 1 : 0).replace(/\.ipynb$/, '');
        return h('div', { class: 'nb-file' + (n.path === nbPath ? ' active' : ''), title: n.path, onclick: () => open(n.path) }, h('span', { class: 'n' }, name), h('button', { class: 'icon-btn sm', title: 'Delete', html: I.trash, onclick: (e) => { e.stopPropagation(); del_(n.path); } }));
      });
      if (!d) listEl.append(...items);
      else listEl.append(h('details', { class: 'nb-folder', open: nbPath && nbPath.startsWith(d + '/') }, h('summary', {}, d), ...items));
    });
  }
  async function open(path) {
    if (dirty) await save(false);
    try {
      nb = await get(`/api/notebooks/file?path=${encodeURIComponent(path)}`);
      nbPath = path; dirty = false; dirtyEl.textContent = '';
      nameEl.textContent = path.replace(/\.ipynb$/, '');
      focusId = null; tracesBtn.style.display = 'none';
      renderCells();
      try { localStorage.setItem('cw.nb', path); } catch (e) { /* ignore */ }
      listEl.querySelectorAll('.nb-file').forEach((f) => f.classList.toggle('active', f.title === path));
    } catch (e) { toast(e.message, 'err', 6000); }
  }
  async function loadVars() {
    try {
      const vs = await get('/api/kernel/variables');
      varsEl.innerHTML = '';
      if (!vs.length) { varsEl.append(h('div', { class: 'help' }, 'No variables yet.')); return; }
      varsEl.append(h('table', { class: 'tbl' }, h('tr', {}, h('th', {}, 'Name'), h('th', {}, 'Type'), h('th', {}, 'Value')),
        ...vs.map((v) => h('tr', {}, h('td', { class: 'mono' }, v.name), h('td', {}, v.type + (v.shape ? ` ${v.shape.join('x')}` : '')), h('td', { class: 'mono', title: v.repr, style: 'max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, v.repr)))));
    } catch (e) { /* ignore */ }
  }

  sideEl.append(
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'Notebooks'), h('div', { class: 'end' }, h('button', { class: 'btn sm', onclick: newNotebook }, 'New'), h('button', { class: 'btn sm', onclick: () => fileIn.click() }, 'Import'), fileIn)),
      listEl),
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'ChipWhisperer tutorials')),
      tutInfo, tutProgress,
      h('div', { class: 'row', style: 'margin-top:8px' }, h('button', { class: 'btn sm primary', onclick: fetchTutorials }, 'Download tutorials'))),
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'Variables'), h('div', { class: 'end' }, h('button', { class: 'btn ghost sm', onclick: loadVars }, 'Refresh'))),
      varsEl),
    h('div', { class: 'help' }, 'Cells run inside Studio: cw.scope() and cw.target() use the connected devices, captured traces appear in the Capture tab, and !make uses Studio\'s compilers. The studio object adds helpers such as studio.traces, studio.build_firmware() and studio.program().'));

  document.addEventListener('keydown', (e) => {
    if (!viewEl.offsetParent) return;
    if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save(true); }
  });
  ctx.on('nb', onNb);
  ctx.on('tutorials', renderTut);
  ctx.on('tab', (t) => { if (t === 'notebook') { loadList(); loadVars(); cellEls.forEach((el) => el._autosize && el._autosize()); } });
  window.addEventListener('beforeunload', () => { if (dirty) save(false); });

  loadList();
  get('/api/kernel').then(setKernel).catch(() => {});
  let last = null;
  try { last = localStorage.getItem('cw.nb'); } catch (e) { /* ignore */ }
  if (last) open(last).catch(() => {});
  renderCells();
  return { open, save };
}

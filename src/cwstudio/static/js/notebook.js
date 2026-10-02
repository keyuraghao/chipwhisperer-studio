// Notebook tab: Jupyter-style cells that run inside Studio (sharing its scope and target), .ipynb files, NewAE's tutorials. Several notebooks can be open at once as tabs, in one pane or two side by side; each notebook has its own kernel (namespace) on the server, and all kernels run their cells one at a time on Studio's hardware thread.
import { h, get, post, put, del, upload, toast, downloadUrl, fmtNum } from './api.js';
import { renderMarkdown as markdownHtml, sanitize } from './markdown.js';

const I = {
  play: '<svg class="i" viewBox="0 0 24 24"><polygon points="7,4 20,12 7,20" fill="currentColor"/></svg>',
  runall: '<svg class="i" viewBox="0 0 24 24"><polygon points="4,4 12,12 4,20" fill="currentColor"/><polygon points="12,4 20,12 12,20" fill="currentColor"/></svg>',
  stop: '<svg class="i" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor"/></svg>',
  restart: '<svg class="i" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 2.6-6.4M3 3v6h6"/></svg>',
  power: '<svg class="i" viewBox="0 0 24 24"><path d="M12 3v9M6.3 7.2a8 8 0 1 0 11.4 0"/></svg>',
  save: '<svg class="i" viewBox="0 0 24 24"><path d="M5 3h11l3 3v15H5zM8 3v6h8V3M8 21v-7h8v7"/></svg>',
  plus: '<svg class="i" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  up: '<svg class="i" viewBox="0 0 24 24"><path d="M6 15l6-6 6 6"/></svg>',
  down: '<svg class="i" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>',
  trash: '<svg class="i" viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>',
  dl: '<svg class="i" viewBox="0 0 24 24"><path d="M12 3v12M7 10l5 5 5-5M4 21h16"/></svg>',
  wave: '<svg class="i" viewBox="0 0 24 24"><path d="M2 12h4l3-8 4 16 3-8h6"/></svg>',
  eraser: '<svg class="i" viewBox="0 0 24 24"><path d="M7 21h10M5 15l9-9 5 5-9 9H7z"/></svg>',
  x: '<svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  split: '<svg class="i" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16"/></svg>',
  swap: '<svg class="i" viewBox="0 0 24 24"><path d="M4 8h14l-4-4M20 16H6l4 4"/></svg>',
};
const ib = (cls, icon, label, onclick, title) => h('button', { class: 'btn ' + cls, onclick, title: title || label, html: I[icon] + (label ? `<span>${label}</span>` : '') });
const stripAnsi = (s) => s.replace(/\x1b\[[0-9;]*[A-Za-z]/g, '');
const enc = encodeURIComponent;
const LAYOUT_KEY = 'cw.nb.layout';
const dirOf = (path) => (path && path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '');
const titleOf = (path) => path.replace(/\.ipynb$/, '').split('/').pop();
// Very long text outputs (a loop printing 100k lines) show only their end, so rendering stays fast; the full text stays in the notebook (Download .ipynb).
const MAX_OUT = 100000;
function clip(text) {
  if (text.length <= MAX_OUT) return text;
  let cut = text.indexOf('\n', text.length - MAX_OUT);
  if (cut < 0) cut = text.length - MAX_OUT;
  let lines = 0;
  for (let i = text.indexOf('\n'); i >= 0 && i <= cut; i = text.indexOf('\n', i + 1)) lines++;
  return `[${fmtNum(lines)} earlier lines not shown; download the notebook for the full output]\n` + text.slice(cut + 1);
}
// Apply carriage returns like a terminal so tqdm progress bars update in place.
function applyCR(text) {
  return text.split('\n').map((line) => { const i = line.lastIndexOf('\r'); return i >= 0 && i < line.length - 1 ? line.slice(i + 1) : line.replace(/\r$/, ''); }).join('\n');
}

export function renderMarkdown(src, baseDir) {
  const div = h('div', { class: 'md', html: markdownHtml(src) });
  div.querySelectorAll('img').forEach((img) => {
    const s = img.getAttribute('src') || '';
    if (s && !/^(https?:|data:|\/)/.test(s)) img.src = `/api/notebooks/asset?path=${enc((baseDir ? baseDir + '/' : '') + s)}`;
  });
  div.querySelectorAll('a[href]').forEach((a) => { a.target = '_blank'; a.rel = 'noopener'; });
  return div;
}

export function initNotebook(ctx, sideEl, viewEl) {
  const docs = new Map(); // notebook path (also its kernel id) -> open notebook
  let split = false, focused = 0, ratio = 0.5, dragPath = null;
  // Identifies this window to the server, which shuts a notebook's kernel down once no window has the notebook open.
  const clientId = Math.random().toString(36).slice(2) + Date.now().toString(36);

  // ================= one open notebook =================
  function createDoc(path) {
    const d = { path, nb: null, dirty: false, saveTimer: null, kernel: { busy: false, queued: [], started: false }, focusId: null, cellEls: new Map(), tracesAtStart: 0, tab: null };
    const dirtyEl = h('span', { class: 'muted nb-dirty' });
    const kernelPill = h('span', { class: 'badge' }, 'No kernel');
    const tracesBtn = ib('sm', 'wave', 'View traces', () => ctx.showTab('capture'), 'Show traces captured by the notebook in the Capture tab');
    tracesBtn.style.display = 'none';
    const cellsEl = h('div', { class: 'nb-cells' }, h('div', { class: 'help nb-loading' }, 'Loading…'));
    const toolbar = h('div', { class: 'toolbar nb-toolbar' },
      ib('sm', 'save', 'Save', () => save(true), 'Save (Ctrl+S)'),
      ib('sm', 'plus', 'Code', () => addCell('code'), 'Insert a code cell below'),
      ib('sm', 'plus', 'Text', () => addCell('markdown'), 'Insert a text (markdown) cell below'),
      h('div', { class: 'sep' }),
      ib('sm primary', 'play', 'Run', () => runFocused(true), 'Run the selected cell and move on (Shift+Enter)'),
      ib('sm', 'runall', 'Run all', runAll),
      ib('sm danger', 'stop', 'Stop', () => post('/api/kernel/interrupt', { kernel: d.path }).catch((e) => toast(e.message, 'err')), 'Interrupt this notebook\'s running cell (also clears its queued cells)'),
      ib('sm ghost', 'restart', 'Restart', restart, 'Restart this notebook\'s kernel: clears its variables'),
      ib('sm ghost', 'power', '', shutdown, 'Shut down this notebook\'s kernel and free its memory (it starts again when you run a cell)'),
      ib('sm ghost', 'eraser', 'Clear outputs', clearOutputs),
      dirtyEl,
      h('div', { class: 'spacer' }), tracesBtn, kernelPill,
      ib('sm ghost', 'dl', '', () => downloadUrl(`/api/notebooks/download?path=${enc(d.path)}`), 'Download .ipynb'));
    const noticeEl = h('div', { class: 'nb-notice', style: 'display:none' });
    const scrollEl = h('div', { class: 'nb-scroll' }, cellsEl);
    d.el = h('div', { class: 'nb-doc' }, toolbar, noticeEl, scrollEl);
    // A bar above the cells when the file changed elsewhere while this tab has unsaved edits, or was deleted.
    function notice(kind) {
      noticeEl.innerHTML = '';
      d.notice = kind || null;
      noticeEl.style.display = kind ? '' : 'none';
      if (!kind) return;
      noticeEl.dataset.kind = kind;
      if (kind === 'conflict') {
        noticeEl.append(h('span', {}, 'This notebook was changed outside this tab (a run from the API or an agent, or another window) while it has unsaved edits. Nothing is saved until you choose.'),
          h('button', { class: 'btn sm', onclick: () => d.load() }, 'Reload from disk'),
          h('button', { class: 'btn sm danger', onclick: () => save(true, true) }, 'Keep my version'));
      } else {
        noticeEl.append(h('span', {}, 'This notebook was deleted or renamed elsewhere; this tab\'s edits are not saved.'),
          h('button', { class: 'btn sm', onclick: () => save(true, true) }, 'Save it again'),
          h('button', { class: 'btn sm', onclick: () => closeDoc(d.path, true) }, 'Close'));
      }
    }

    const baseDir = () => dirOf(d.path);
    function markDirty() { d.dirty = true; d.version = (d.version || 0) + 1; dirtyEl.textContent = 'unsaved'; refreshTab(d); clearTimeout(d.saveTimer); if (!d.notice) d.saveTimer = setTimeout(() => save(false), 2500); }
    // Saves go one after another and send the file's modification time this tab last saw: the server refuses the save when the file changed since (a run, an agent, another window) or was deleted, instead of overwriting it or creating it again. force (an explicit choice in the notice) skips that check.
    let chain = Promise.resolve();
    function save(explicit, force) {
      chain = chain.then(() => saveNow(explicit, force));
      return chain;
    }
    async function saveNow(explicit, force) {
      if (!d.nb || (d.notice && !force)) return;
      clearTimeout(d.saveTimer);
      const version = d.version || 0;
      d.saving = true;
      try {
        const r = await put('/api/notebooks/file', { path: d.path, notebook: d.nb, base_mtime: force || d.mtime == null ? undefined : d.mtime, client: clientId });
        d.mtime = r.mtime;
        notice(null);
        if ((d.version || 0) === version) { d.dirty = false; dirtyEl.textContent = 'saved'; }
        refreshTab(d);
        if (explicit) toast('Notebook saved', 'ok', 1500);
      } catch (e) {
        if (/^NotebookConflict/.test(e.message)) notice('conflict');
        else if (/^NotebookDeleted/.test(e.message)) notice('deleted');
        else toast('Save failed: ' + e.message, 'err', 6000);
      } finally { d.saving = false; }
    }
    // The file changed on the server (nb "file" event from another window, the API or an agent).
    d.onFile = (ev) => {
      if (ev.action === 'deleted') {
        if (d.dirty) notice('deleted'); else { closeDoc(d.path, true); toast(`${d.path} was deleted`, 'info', 4000); }
        return;
      }
      if (ev.client === clientId || (d.mtime != null && Math.abs(ev.mtime - d.mtime) < 1e-3)) return; // this tab's own save
      if (d.dirty || d.saving) { clearTimeout(d.saveTimer); notice('conflict'); } else d.load(true, true);
    };

    const cellById = (id) => d.nb && d.nb.cells.find((c) => c.id === id);
    function focusCell(id, edit) {
      d.focusId = id;
      d.cellEls.forEach((el, cid) => el.classList.toggle('focused', cid === id));
      const el = d.cellEls.get(id);
      if (!el) return;
      el.scrollIntoView({ block: 'nearest' });
      if (edit) { const ta = el.querySelector('textarea'); if (ta) { if (ta.style.display === 'none') el._edit(); ta.focus(); } }
    }
    const newId = () => Math.random().toString(16).slice(2, 10);
    function addCell(type, after = d.focusId, source = '') {
      if (!d.nb) return;
      const cell = { id: newId(), cell_type: type, source, metadata: {}, ...(type === 'code' ? { outputs: [], execution_count: null } : {}) };
      const i = after ? d.nb.cells.findIndex((c) => c.id === after) : d.nb.cells.length - 1;
      d.nb.cells.splice(i + 1, 0, cell);
      renderCells(); markDirty(); focusCell(cell.id, true);
    }
    function moveCell(id, delta) {
      const cells = d.nb.cells, i = cells.findIndex((c) => c.id === id), j = i + delta;
      if (j < 0 || j >= cells.length) return;
      [cells[i], cells[j]] = [cells[j], cells[i]];
      renderCells(); markDirty(); focusCell(id);
    }
    function deleteCell(id) {
      const i = d.nb.cells.findIndex((c) => c.id === id);
      if (i < 0) return;
      d.nb.cells.splice(i, 1);
      renderCells(); markDirty();
      const next = d.nb.cells[Math.min(i, d.nb.cells.length - 1)];
      if (next) focusCell(next.id);
    }

    function renderOutput(o) {
      const t = o.output_type;
      if (t === 'stream') return h('pre', { class: 'nb-out stream ' + (o.name || '') }, applyCR(clip(stripAnsi(o.text || ''))));
      if (t === 'error') return h('pre', { class: 'nb-out error' }, clip(stripAnsi((o.traceback || []).join('\n') || `${o.ename}: ${o.evalue}`)));
      const data = o.data || {};
      if (data['image/png']) return h('div', { class: 'nb-out img' }, h('img', { src: 'data:image/png;base64,' + data['image/png'] }));
      if (data['image/jpeg']) return h('div', { class: 'nb-out img' }, h('img', { src: 'data:image/jpeg;base64,' + data['image/jpeg'] }));
      if (data['image/svg+xml']) return h('div', { class: 'nb-out img', html: sanitize(data['image/svg+xml'], { svg: true }) });
      if (data['text/html']) {
        // Untrusted HTML (e.g. saved outputs of imported notebooks) is shown in a script-free sandbox.
        const fr = h('iframe', { class: 'nb-out html', sandbox: '', srcdoc: `<!doctype html><meta charset="utf-8"><style>body{margin:0;font:13px system-ui,sans-serif;color:${getComputedStyle(document.body).color}}table{border-collapse:collapse}td,th{border:1px solid #8884;padding:2px 6px}</style>${data['text/html']}` });
        fr.addEventListener('load', () => { try { fr.style.height = Math.min(600, fr.contentDocument.documentElement.scrollHeight + 4) + 'px'; } catch (e) { fr.style.height = '200px'; } });
        return fr;
      }
      if (data['text/markdown']) return h('div', { class: 'nb-out' }, renderMarkdown(data['text/markdown'], baseDir()));
      if (data['text/plain'] != null) return h('pre', { class: 'nb-out result' }, clip(stripAnsi(String(data['text/plain']))));
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
        else if (e.key === 'Escape') { viewEl.focus({ preventScroll: true }); if (cell.cell_type === 'markdown') el._render(); } // leave the cell but keep the focus in the notebook, so single-key shortcuts stay off
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
      el._autosize = autosize;
      el._setOutputs = () => { cancelAnimationFrame(el._raf); el._raf = 0; el._full = false; outs.innerHTML = ''; (cell.outputs || []).forEach((o) => { const r = renderOutput(o); if (r) outs.append(r); }); };
      // Output events arrive many times a second while a cell prints: render at most once per frame, and when text was only appended to the last stream re-render just that one.
      el._queueOutputs = (full) => {
        el._full = el._full || full;
        if (el._raf) return;
        el._raf = requestAnimationFrame(() => {
          el._raf = 0;
          const outsList = cell.outputs || [], last = outsList[outsList.length - 1], lastEl = outs.lastElementChild;
          if (el._full || !last || !lastEl || outs.children.length !== outsList.length) { el._setOutputs(); return; }
          const r = renderOutput(last);
          if (r) lastEl.replaceWith(r);
        });
      };
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
      d.cellEls.clear();
      if (!d.nb) return;
      d.nb.cells.forEach((c) => { const el = renderCell(c); d.cellEls.set(c.id, el); cellsEl.append(el); });
      const last = () => (d.nb.cells.length ? d.nb.cells[d.nb.cells.length - 1].id : null);
      cellsEl.append(h('div', { class: 'nb-add' }, ib('ghost sm', 'plus', 'Code', () => addCell('code', last())), ib('ghost sm', 'plus', 'Text', () => addCell('markdown', last()))));
      if (d.focusId) d.cellEls.forEach((el, id) => el.classList.toggle('focused', id === d.focusId));
    }

    // ---------- execution (in this notebook's own kernel) ----------
    async function execute(cells) {
      if (!cells.length) return;
      d.tracesAtStart = ctx.status ? ctx.status.traces.count : 0;
      try {
        await post('/api/kernel/execute', { cells: cells.map((c) => ({ id: c.id, code: c.source })), path: d.path, kernel: d.path });
      } catch (e) { toast(e.message, 'err', 6000); }
    }
    function runCell(id, advance) {
      const cell = cellById(id);
      if (!cell) return;
      const el = d.cellEls.get(id);
      if (cell.cell_type === 'markdown') { el._render(); }
      else { cell.outputs = []; el._setOutputs(); execute([cell]); }
      if (advance) {
        const i = d.nb.cells.findIndex((c) => c.id === id);
        if (i === d.nb.cells.length - 1) addCell('code', id); else focusCell(d.nb.cells[i + 1].id, d.nb.cells[i + 1].cell_type === 'code');
      }
    }
    function runFocused(advance) { if (d.focusId) runCell(d.focusId, advance); else if (d.nb && d.nb.cells.length) runCell(d.nb.cells[0].id, advance); }
    function runAll() {
      if (!d.nb) return;
      const code = d.nb.cells.filter((c) => c.cell_type === 'code' && c.source.trim());
      code.forEach((c) => { c.outputs = []; const el = d.cellEls.get(c.id); if (el) el._setOutputs(); });
      d.nb.cells.filter((c) => c.cell_type === 'markdown').forEach((c) => { const el = d.cellEls.get(c.id); if (el && el._render) el._render(); });
      execute(code);
    }
    async function restart() {
      if (!confirm(`Restart the kernel of ${titleOf(d.path)}? Its variables are cleared; other notebooks and Studio's connection are not affected.`)) return;
      try { await post('/api/kernel/restart', { kernel: d.path }); toast('Kernel restarted', 'ok', 1500); } catch (e) { toast(e.message, 'err'); }
    }
    async function shutdown() {
      if (!confirm(`Shut down the kernel of ${titleOf(d.path)}? Its variables are freed; a new kernel starts when you run a cell.`)) return;
      try { await post('/api/kernels/shutdown', { kernel: d.path }); } catch (e) { toast(e.message, 'err'); }
    }
    function clearOutputs() {
      if (!d.nb) return;
      d.nb.cells.forEach((c) => { if (c.cell_type === 'code') { c.outputs = []; c.execution_count = null; const el = d.cellEls.get(c.id); if (el) { el._setOutputs(); setGutter(el, c); } } });
      markDirty();
    }
    function setKernel(st) {
      d.kernel = st;
      const queued = (st.queued || []).length;
      if (st.busy) { kernelPill.className = 'badge warn'; kernelPill.textContent = `Running${queued ? ` (+${queued} queued)` : ''}`; kernelPill.title = 'A cell of this notebook is running'; }
      else if (queued) { kernelPill.className = 'badge info'; kernelPill.textContent = `Queued (${queued})`; kernelPill.title = 'Cells run one at a time across all notebooks; these wait for another notebook\'s cell'; }
      else if (st.started === false) { kernelPill.className = 'badge'; kernelPill.textContent = 'No kernel'; kernelPill.title = 'This notebook\'s kernel starts when you run a cell'; }
      else { kernelPill.className = 'badge ok'; kernelPill.textContent = 'Kernel idle'; kernelPill.title = `This notebook's own kernel: ${st.execution_count ?? 0} cells run`; }
      refreshTab(d);
      if (d === focusedDoc()) ctx.emit('kernel', st);
    }
    d.onNb = (ev) => {
      const cell = cellById(ev.cell), el = d.cellEls.get(ev.cell), k = d.kernel;
      if (ev.kind === 'restarted') { setKernel(ev); if (d === focusedDoc()) loadVars(); return; }
      if (ev.kind === 'shutdown') { setKernel({ busy: false, queued: [], started: false, execution_count: 0 }); d.cellEls.forEach((cel, id) => { const c = cellById(id); if (c) setGutter(cel, c); }); if (d === focusedDoc()) loadVars(); return; }
      if (ev.kind === 'queued') { setKernel({ ...k, started: true, queued: [...(k.queued || []), ev.cell] }); if (cell && el) setGutter(el, cell, 'queued'); return; }
      if (ev.kind === 'running') { setKernel({ ...k, started: true, busy: true, cell: ev.cell, queued: (k.queued || []).filter((c) => c !== ev.cell) }); if (cell && el) { cell.outputs = []; el._setOutputs(); setGutter(el, cell, 'running'); } return; }
      if (ev.kind === 'output' && cell && el) {
        const o = ev.output, outs = cell.outputs || (cell.outputs = []), prev = outs[outs.length - 1];
        if (o.output_type === 'clear_output') { cell.outputs = []; el._queueOutputs(true); }
        else if (o.output_type === 'stream' && prev && prev.output_type === 'stream' && prev.name === o.name) { prev.text += o.text; el._queueOutputs(false); }
        else { outs.push(o); el._queueOutputs(true); }
        return;
      }
      if (ev.kind === 'cancelled') { setKernel({ ...k, queued: (k.queued || []).filter((c) => c !== ev.cell) }); if (cell && el) setGutter(el, cell); return; }
      if (ev.kind === 'done') {
        setKernel({ ...k, started: true, busy: false, cell: null, execution_count: ev.execution_count ?? k.execution_count, queued: (k.queued || []).filter((c) => c !== ev.cell) });
        if (cell && el) { cell.outputs = ev.outputs || cell.outputs; cell.execution_count = ev.execution_count; el._setOutputs(); setGutter(el, cell); markDirty(); }
        if (d === focusedDoc()) loadVars();
        ctx.refreshStatus().then(() => { const n = ctx.status ? ctx.status.traces.count : 0; if (n > d.tracesAtStart) { tracesBtn.style.display = ''; tracesBtn.querySelector('span').textContent = `View ${n} traces`; } });
      }
    };

    d.load = async (quiet, keepScroll) => {
      let nb;
      try {
        nb = await get(`/api/notebooks/file?path=${enc(d.path)}`);
      } catch (e) {
        if (!quiet) toast(`${d.path}: ${e.message}`, 'err', 6000);
        return false;
      }
      d.mtime = nb.mtime; delete nb.mtime;
      d.nb = nb;
      clearTimeout(d.saveTimer);
      d.dirty = false; dirtyEl.textContent = '';
      notice(null);
      const top = scrollEl.scrollTop;
      renderCells();
      if (keepScroll) scrollEl.scrollTop = top;
      refreshTab(d);
      get(`/api/kernel?kernel=${enc(d.path)}`).then(setKernel).catch(() => {});
      return true;
    };
    d.save = save;
    d.autosize = () => d.cellEls.forEach((el) => el._autosize && el._autosize());
    d.destroy = () => { clearTimeout(d.saveTimer); d.el.remove(); d.cellEls.clear(); d.tab = null; };
    return d;
  }

  // ================= panes and tabs =================
  const panes = [0, 1].map(makePane);
  const divider = h('div', { class: 'nb-divider', title: 'Drag to resize, double-click to make both panes equal' });
  const panesEl = h('div', { class: 'nb-panes' }, panes[0].el, divider, panes[1].el);
  viewEl.append(panesEl);

  const focusedDoc = () => { const p = panes[focused]; return p && p.active ? docs.get(p.active) : null; };
  const paneOf = (path) => panes.findIndex((p) => p.tabs.includes(path));
  const stacked = () => getComputedStyle(panesEl).flexDirection === 'column';

  function makePane(i) {
    const p = { i, tabs: [], active: null };
    p.strip = h('div', { class: 'nb-tabs', role: 'tablist' });
    p.splitBtn = h('button', { class: 'icon-btn sm nb-split', onclick: () => { if (p.active) moveDoc(p.active, split ? 1 - i : 1); } });
    p.empty = h('div', { class: 'nb-empty' },
      h('div', { html: '<svg viewBox="0 0 24 24"><path d="M5 3h11l3 3v15H5z"/><path d="M8 9h8M8 13h8M8 17h5"/></svg>' }),
      h('b', {}, 'Open or create a notebook'),
      h('div', {}, 'Pick one on the left, import an .ipynb, or download NewAE\'s tutorial notebooks. Open several to get tabs; drag a tab to the right half to see two side by side.'));
    p.drop = h('div', { class: 'nb-drop' });
    p.body = h('div', { class: 'nb-pane-body' }, p.empty, p.drop);
    p.el = h('section', { class: 'nb-pane', 'data-pane': String(i) }, h('div', { class: 'nb-tabbar' }, p.strip, p.splitBtn), p.body);
    p.el.addEventListener('mousedown', () => setFocused(i), true);
    p.el.addEventListener('focusin', () => setFocused(i));
    p.strip.addEventListener('dragover', (e) => { if (dragPath) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; } });
    p.strip.addEventListener('drop', (e) => { if (!dragPath) return; e.preventDefault(); placeDoc(dragPath, i); clearDrag(); });
    p.body.addEventListener('dragover', (e) => {
      if (!dragPath) return;
      const zone = dropZone(p, e);
      p.drop.className = 'nb-drop' + (zone ? ` show ${zone}${stacked() ? ' stacked' : ''}` : '');
      if (zone) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
    });
    p.body.addEventListener('dragleave', (e) => { if (!p.body.contains(e.relatedTarget)) p.drop.className = 'nb-drop'; });
    p.body.addEventListener('drop', (e) => {
      const zone = dragPath && dropZone(p, e);
      p.drop.className = 'nb-drop';
      if (!zone) return;
      e.preventDefault();
      if (zone === 'half') { if (!docs.has(dragPath)) openDoc(dragPath, 0); moveDoc(dragPath, 1); } else placeDoc(dragPath, i);
      clearDrag();
    });
    return p;
  }

  // Where a dragged notebook would land in a pane: 'full' (into this pane), 'half' (split: a new pane on the right, or below when stacked) or null.
  function dropZone(p, e) {
    const open = docs.has(dragPath), from = open ? paneOf(dragPath) : -1;
    if (split) return from === p.i ? null : 'full';
    const r = p.body.getBoundingClientRect();
    const far = stacked() ? e.clientY > r.top + r.height / 2 : e.clientX > r.left + r.width / 2;
    if (far && p.tabs.length >= (open ? 2 : 1)) return 'half';
    return open ? null : 'full';
  }
  function clearDrag() {
    dragPath = null;
    panesEl.classList.remove('dragging');
    viewEl.querySelectorAll('.nb-tab.drop-before, .nb-tab.drop-after, .nb-tab.dragging').forEach((t) => t.classList.remove('drop-before', 'drop-after', 'dragging'));
    panes.forEach((p) => { p.drop.className = 'nb-drop'; });
  }
  function startDrag(e, path) {
    dragPath = path;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('application/x-cwstudio-notebook', path); // not text/plain, so a cell editor never takes the drop as text
    panesEl.classList.add('dragging');
  }

  function tabEl(d, p) {
    const el = h('div', { class: 'nb-tab', role: 'tab', draggable: 'true', title: `${d.path}\nDouble-click to rename; drag to reorder or onto the other pane; middle-click to close` },
      h('span', { class: 'nb-tab-dot' }), h('span', { class: 'nb-tab-name' }, titleOf(d.path)),
      h('button', { class: 'nb-tab-close', title: 'Close', 'aria-label': 'Close ' + titleOf(d.path), html: I.x, onclick: (e) => { e.stopPropagation(); closeDoc(d.path); } }));
    el.addEventListener('click', () => activate(p.i, d.path));
    el.addEventListener('auxclick', (e) => { if (e.button === 1) { e.preventDefault(); closeDoc(d.path); } });
    el.addEventListener('dblclick', (e) => { if (!e.target.closest('.nb-tab-close')) renameDoc(d); });
    el.addEventListener('dragstart', (e) => { startDrag(e, d.path); el.classList.add('dragging'); });
    el.addEventListener('dragend', clearDrag);
    const after = (e) => { const r = el.getBoundingClientRect(); return e.clientX > r.left + r.width / 2; };
    el.addEventListener('dragover', (e) => {
      if (!dragPath) return;
      e.preventDefault(); e.stopPropagation();
      e.dataTransfer.dropEffect = 'move';
      const a = after(e);
      el.classList.toggle('drop-after', a); el.classList.toggle('drop-before', !a);
    });
    el.addEventListener('dragleave', () => el.classList.remove('drop-before', 'drop-after'));
    el.addEventListener('drop', (e) => {
      if (!dragPath) return;
      e.preventDefault(); e.stopPropagation();
      placeDoc(dragPath, p.i, p.tabs.indexOf(d.path) + (after(e) ? 1 : 0));
      clearDrag();
    });
    d.tab = el;
    refreshTab(d);
    return el;
  }
  function refreshTab(d) {
    const el = d.tab;
    if (!el) return;
    const k = d.kernel || {};
    el.classList.toggle('busy', !!k.busy);
    el.classList.toggle('queued', !k.busy && (k.queued || []).length > 0);
    el.classList.toggle('dirty', d.dirty);
    el.querySelector('.nb-tab-name').textContent = titleOf(d.path);
    const p = panes[paneOf(d.path)];
    const active = !!p && p.active === d.path;
    el.classList.toggle('active', active);
    el.setAttribute('aria-selected', active ? 'true' : 'false');
  }

  function layoutSizes() {
    panes[0].el.style.flex = split ? `${ratio} 1 0` : '1 1 0';
    panes[1].el.style.flex = `${1 - ratio} 1 0`;
  }
  // Collapse the split when a pane has no notebooks left.
  function normalize() {
    if (split && !panes[0].tabs.length) {
      Object.assign(panes[0], { tabs: panes[1].tabs, active: panes[1].active });
      Object.assign(panes[1], { tabs: [], active: null });
    }
    if (split && !panes[1].tabs.length) split = false;
    if (!split) {
      if (panes[1].tabs.length) { panes[0].tabs.push(...panes[1].tabs); Object.assign(panes[1], { tabs: [], active: null }); }
      focused = 0;
    }
    panes.forEach((p) => { if (!p.tabs.includes(p.active)) p.active = p.tabs[0] || null; });
  }
  function layout() {
    normalize();
    panesEl.classList.toggle('split', split);
    panes[1].el.style.display = split ? '' : 'none';
    divider.style.display = split ? '' : 'none';
    layoutSizes();
    panes.forEach((p) => {
      p.strip.innerHTML = '';
      p.tabs.forEach((path) => {
        const d = docs.get(path);
        p.strip.append(tabEl(d, p));
        if (d.el.parentNode !== p.body) p.body.insertBefore(d.el, p.drop);
        const show = path === p.active;
        if (show && d.el.style.display === 'none') requestAnimationFrame(d.autosize);
        d.el.style.display = show ? '' : 'none';
      });
      p.empty.style.display = p.tabs.length || p.i === 1 ? 'none' : '';
      p.el.classList.toggle('focused', split && p.i === focused);
      p.splitBtn.disabled = !split && p.tabs.length < 2;
      p.splitBtn.innerHTML = split ? I.swap : I.split;
      p.splitBtn.title = split ? 'Move this notebook to the other pane' : p.tabs.length < 2 ? 'Split: open another notebook first, then move it to a pane on the right' : 'Split right: show this notebook in a new pane next to the others';
      const at = p.strip.querySelector('.nb-tab.active');
      if (at) at.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    });
    focusChanged();
    saveLayout();
    syncKernels();
  }
  let lastFocused = null;
  function focusChanged() {
    panes.forEach((p) => p.el.classList.toggle('focused', split && p.i === focused));
    markList();
    const d = focusedDoc();
    if ((d && d.path) !== lastFocused) { lastFocused = d && d.path; loadVars(); if (d) ctx.emit('kernel', d.kernel); }
  }
  function setFocused(i) {
    if (focused === i || (i === 1 && !split)) return;
    focused = i;
    focusChanged();
    saveLayout();
  }
  function activate(i, path) {
    panes[i].active = path;
    focused = i;
    layout();
  }

  // Open a notebook (in the focused pane unless it is already open somewhere, then show it there). Returns a promise that resolves once its cells are loaded.
  function openDoc(path, paneIdx = focused) {
    const at = paneOf(path);
    if (at >= 0) { activate(at, path); return Promise.resolve(docs.get(path)); }
    const d = createDoc(path);
    docs.set(path, d);
    const p = panes[split ? paneIdx : 0];
    p.tabs.splice(p.active ? p.tabs.indexOf(p.active) + 1 : p.tabs.length, 0, path);
    activate(p.i, path);
    return d.load().then((ok) => { if (!ok) closeDoc(path, true); return ok ? d : null; });
  }
  // Put a notebook in pane i at index (opening it first when it is not open yet).
  function placeDoc(path, i, index) {
    if (!docs.has(path)) { openDoc(path, i); return; }
    moveDoc(path, i, index);
  }
  function moveDoc(path, to, index) {
    const from = paneOf(path);
    if (from < 0) return;
    const src = panes[from], dst = panes[to];
    const i = src.tabs.indexOf(path);
    if (index === undefined || index > dst.tabs.length) index = dst.tabs.length;
    if (from === to && index > i) index--;
    src.tabs.splice(i, 1);
    if (src.active === path) src.active = src.tabs[Math.min(i, src.tabs.length - 1)] || null;
    dst.tabs.splice(index, 0, path);
    if (to === 1) split = true;
    activate(to, path);
  }
  function closeDoc(path, discard) {
    const d = docs.get(path);
    if (!d) return;
    if (d.dirty && !discard) d.save(false);
    const p = panes[paneOf(path)], i = p.tabs.indexOf(path);
    p.tabs.splice(i, 1);
    if (p.active === path) p.active = p.tabs[Math.min(i, p.tabs.length - 1)] || null;
    d.destroy();
    docs.delete(path);
    layout();
  }
  async function renameDoc(d) {
    const cur = d.path.replace(/\.ipynb$/, '');
    const to = prompt('Rename notebook (sub-folders allowed). Its variables are kept.', cur);
    if (to === null || !to.trim() || to.trim() === cur) return;
    if (d.dirty) await d.save(false);
    try { const r = await post('/api/notebooks/rename', { path: d.path, to: to.trim() }); applyRename(r.old, r.path); loadList(); } catch (e) { toast(e.message, 'err', 6000); }
  }
  // Re-key an open notebook after a rename (here or in another window); its kernel moved with it on the server.
  function applyRename(old, path) {
    const d = docs.get(old);
    if (!d || old === path || docs.has(path)) return;
    docs.delete(old);
    d.path = path;
    docs.set(path, d);
    panes.forEach((p) => { p.tabs = p.tabs.map((x) => (x === old ? path : x)); if (p.active === old) p.active = path; });
    lastFocused = null;
    layout();
  }

  // ---------- divider ----------
  divider.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    divider.setPointerCapture(e.pointerId);
    panesEl.classList.add('resizing');
    const move = (ev) => {
      const r = panesEl.getBoundingClientRect();
      const v = stacked() ? (ev.clientY - r.top) / r.height : (ev.clientX - r.left) / r.width;
      ratio = Math.min(0.8, Math.max(0.2, v));
      layoutSizes();
    };
    const up = () => { divider.removeEventListener('pointermove', move); divider.removeEventListener('pointerup', up); panesEl.classList.remove('resizing'); saveLayout(); };
    divider.addEventListener('pointermove', move);
    divider.addEventListener('pointerup', up);
  });
  divider.addEventListener('dblclick', () => { ratio = 0.5; layoutSizes(); saveLayout(); });

  // ---------- remembering the layout, and telling the server what is open ----------
  function saveLayout() {
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify({ panes: panes.map((p) => ({ tabs: p.tabs, active: p.active })), split, focused, ratio })); } catch (e) { /* ignore */ }
  }
  function restoreLayout() {
    let st = null;
    try { st = JSON.parse(localStorage.getItem(LAYOUT_KEY) || 'null'); } catch (e) { /* ignore */ }
    if (!st) {
      let last = null;
      try { last = localStorage.getItem('cw.nb'); } catch (e) { /* ignore */ }
      if (last) st = { panes: [{ tabs: [last], active: last }] };
    }
    if (st && Array.isArray(st.panes)) {
      ratio = Math.min(0.8, Math.max(0.2, Number(st.ratio) || 0.5));
      st.panes.slice(0, 2).forEach((sp, i) => {
        (Array.isArray(sp && sp.tabs) ? sp.tabs : []).forEach((path) => {
          if (typeof path !== 'string' || docs.has(path)) return;
          docs.set(path, createDoc(path));
          panes[i].tabs.push(path);
        });
        panes[i].active = sp && panes[i].tabs.includes(sp.active) ? sp.active : panes[i].tabs[0] || null;
      });
      split = !!st.split;
      focused = st.focused === 1 ? 1 : 0;
    }
    layout();
    docs.forEach((d) => d.load(true).then((ok) => { if (!ok) closeDoc(d.path, true); }));
  }
  let syncTimer = null;
  function syncKernels() {
    clearTimeout(syncTimer);
    syncTimer = setTimeout(() => post('/api/kernels/attach', { client: clientId, kernels: [...docs.keys()] }).catch(() => {}), 300);
  }
  setInterval(syncKernels, 60000);
  window.addEventListener('pagehide', () => {
    try { navigator.sendBeacon('/api/kernels/attach', new Blob([JSON.stringify({ client: clientId, kernels: [] })], { type: 'application/json' })); } catch (e) { /* ignore */ }
  });
  window.addEventListener('pageshow', (e) => { if (e.persisted) syncKernels(); });

  // ================= sidebar =================
  const listEl = h('div', { class: 'nb-list' });
  const varsEl = h('div', { class: 'nb-vars' });
  const varsName = h('span', { class: 'muted nb-vars-name' });
  const tutInfo = h('div', { class: 'help' });
  const tutProgress = h('progress', { style: 'display:none' });
  const tutAsk = h('div', { class: 'nb-tut-ask', style: 'display:none' });
  const fileIn = h('input', { type: 'file', accept: '.ipynb', style: 'display:none', onchange: async () => {
    if (!fileIn.files.length) return;
    try { const r = await upload('/api/notebooks/import', fileIn.files[0]); toast(`Imported ${r.path}`, 'ok'); await loadList(); openDoc(r.path); } catch (e) { toast(e.message, 'err', 6000); }
    fileIn.value = '';
  } });
  async function newNotebook() {
    const name = prompt('Notebook name', 'Untitled');
    if (name === null) return;
    try { const r = await post('/api/notebooks/new', { name }); await loadList(); openDoc(r.path); } catch (e) { toast(e.message, 'err'); }
  }
  async function fetchTutorials(onModified) {
    tutAskDismissed = false;
    try { renderTut(await post('/api/notebooks/tutorials/fetch', onModified ? { on_modified: onModified } : {})); } catch (e) { toast(e.message, 'err', 6000); }
  }
  let tutState = null, tutAskDismissed = false;
  // A download that would replace tutorial files you edited stops in state "confirm" and asks here: update and keep your copies as backups, keep your copies as they are, or cancel.
  function renderTutAsk(t) {
    const conflicts = (t.job && t.job.state === 'confirm' && t.job.conflicts) || [];
    tutAsk.style.display = conflicts.length && !tutAskDismissed ? '' : 'none';
    if (!conflicts.length || tutAskDismissed) return;
    const shown = conflicts.slice(0, 8);
    tutAsk.innerHTML = '';
    tutAsk.append(
      h('div', { class: 'help' }, `You changed ${conflicts.length} tutorial file${conflicts.length === 1 ? '' : 's'} that the download would replace:`),
      h('ul', { class: 'help mono', style: 'margin:4px 0 4px 16px;padding:0' }, ...shown.map((c) => h('li', {}, c)), conflicts.length > shown.length ? h('li', {}, `and ${conflicts.length - shown.length} more`) : null),
      h('div', { class: 'row', style: 'gap:6px;flex-wrap:wrap' },
        h('button', { class: 'btn sm primary', title: 'Install the new versions and keep your copies next to them as <name>.local-<date>-<time>', onclick: () => fetchTutorials('backup') }, 'Update, back up mine'),
        h('button', { class: 'btn sm', title: 'Update everything else and leave your edited files as they are', onclick: () => fetchTutorials('keep') }, 'Keep mine'),
        h('button', { class: 'btn ghost sm', onclick: () => { tutAskDismissed = true; renderTutAsk(t); } }, 'Cancel')));
  }
  function renderTut(t, fromList) {
    const busy = t.job && ['resolving', 'downloading', 'extracting'].includes(t.job.state);
    tutProgress.style.display = busy ? '' : 'none';
    if (busy) { if (t.job.total) { tutProgress.max = t.job.total; tutProgress.value = t.job.done; } else tutProgress.removeAttribute('value'); }
    renderTutAsk(t);
    const backups = (t.job && t.job.state === 'installed' && t.job.backups) || [];
    if (backups.length && tutState && tutState !== 'installed' && !fromList) toast(`Kept your edited tutorial files as ${backups.join(', ')}`, 'ok', 8000);
    tutInfo.textContent = busy ? `${t.job.state}…` : t.job && t.job.state === 'error' ? t.job.error : t.job && t.job.state === 'confirm' && tutAskDismissed ? 'Download cancelled; your edited tutorial files were not changed.' : t.installed ? `chipwhisperer-jupyter ${t.installed.commit.slice(0, 8)}${t.firmware_linked ? '' : ' (download firmware sources in the Firmware tab so build cells work)'}` : 'NewAE\'s courses (SCA101, Fault101, ...) and demos, matched to your firmware sources.';
    // Reload the list once when a download finishes (the job keeps saying "installed" afterwards, and loadList renders this card again).
    const state = t.job ? t.job.state : null;
    if (state === 'installed' && tutState && tutState !== 'installed' && !fromList) loadList();
    tutState = state;
  }
  async function deleteFile(path) {
    if (!confirm(`Delete ${path}?`)) return;
    try { await del(`/api/notebooks/file?path=${enc(path)}`); closeDoc(path, true); loadList(); } catch (e) { toast(e.message, 'err'); }
  }
  function markList() {
    const d = focusedDoc();
    listEl.querySelectorAll('.nb-file').forEach((f) => { f.classList.toggle('active', !!d && f.dataset.path === d.path); f.classList.toggle('open', docs.has(f.dataset.path)); });
    varsName.textContent = d ? titleOf(d.path) : '';
  }
  let listTimer = null;
  const listSoon = () => { clearTimeout(listTimer); listTimer = setTimeout(loadList, 300); };
  async function loadList() {
    let r;
    try { r = await get('/api/notebooks'); } catch (e) { return; }
    renderTut(r.tutorials, true);
    const groups = {};
    r.notebooks.forEach((n) => { const dir = dirOf(n.path); (groups[dir] = groups[dir] || []).push(n); });
    const openDirs = new Set([...docs.keys()].map(dirOf));
    listEl.innerHTML = '';
    if (!r.notebooks.length) listEl.append(h('div', { class: 'help' }, 'No notebooks yet.'));
    Object.keys(groups).sort((a, b) => (a === '' ? -1 : b === '' ? 1 : a.localeCompare(b))).forEach((dir) => {
      const items = groups[dir].map((n) => {
        const name = n.path.slice(dir ? dir.length + 1 : 0).replace(/\.ipynb$/, '');
        const el = h('div', { class: 'nb-file', title: `${n.path}\nClick to open in the focused pane, or drag onto a pane`, 'data-path': n.path, draggable: 'true', onclick: () => openDoc(n.path) }, h('span', { class: 'n' }, name), h('button', { class: 'icon-btn sm', title: 'Delete', html: I.trash, onclick: (e) => { e.stopPropagation(); deleteFile(n.path); } }));
        el.addEventListener('dragstart', (e) => startDrag(e, n.path));
        el.addEventListener('dragend', clearDrag);
        return el;
      });
      if (!dir) listEl.append(...items);
      else listEl.append(h('details', { class: 'nb-folder', open: [...openDirs].some((o) => o === dir || o.startsWith(dir + '/')) }, h('summary', {}, dir), ...items));
    });
    markList();
  }
  async function loadVars() {
    const d = focusedDoc();
    if (!d) { varsEl.innerHTML = ''; varsEl.append(h('div', { class: 'help' }, 'Open a notebook to see its variables.')); return; }
    try {
      const path = d.path;
      const vs = await get(`/api/kernel/variables?kernel=${enc(path)}`);
      if (focusedDoc() !== d) return; // focus moved on while loading
      varsEl.innerHTML = '';
      if (!vs.length) { varsEl.append(h('div', { class: 'help' }, d.kernel && d.kernel.started === false ? 'Run a cell to start this notebook\'s kernel.' : 'No variables yet.')); return; }
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
      tutInfo, tutProgress, tutAsk,
      h('div', { class: 'row', style: 'margin-top:8px' }, h('button', { class: 'btn sm primary', title: 'Download or update the tutorials. Files you changed are never overwritten without asking.', onclick: () => fetchTutorials() }, 'Download tutorials'))),
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'Variables'), varsName, h('div', { class: 'end' }, h('button', { class: 'btn ghost sm', onclick: loadVars }, 'Refresh'))),
      varsEl),
    h('div', { class: 'help' }, 'Cells run inside Studio: cw.scope() and cw.target() use the connected devices, captured traces appear in the Capture tab, and !make uses Studio\'s compilers. Each open notebook has its own variables (its kernel); cells of all notebooks run one at a time. The studio object adds helpers such as studio.traces, studio.build_firmware() and studio.program().'));

  // Studio's single-key shortcuts (s single, r run, Esc stop, space pause, +/- zoom, arrows) must not fire while the notebook has the focus: in a cell, on a notebook toolbar button, after Esc leaves a cell, or after a click anywhere in the notebook (the view takes the focus, see tabindex). With the focus elsewhere (the tab bar, the header), S and R still capture as in every other tab.
  const CAPTURE_KEYS = new Set(['s', 'S', 'r', 'R', 'Escape', ' ', '+', '=', '-', '_', 'ArrowLeft', 'ArrowRight']);
  const captureKey = (e) => CAPTURE_KEYS.has(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey && !['INPUT', 'SELECT', 'TEXTAREA'].includes((e.target && e.target.tagName) || '');
  viewEl.tabIndex = -1;
  [viewEl, sideEl].forEach((el) => el.addEventListener('keydown', (e) => { if (captureKey(e) || e.key === 'Escape') e.stopPropagation(); }));
  document.addEventListener('keydown', (e) => {
    if (!viewEl.offsetParent) return;
    if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); const d = focusedDoc(); if (d) d.save(true); }
  });
  ctx.on('nb', (ev) => {
    if (ev.kind === 'renamed') { applyRename(ev.old, ev.kernel); loadList(); return; }
    if (ev.kind === 'file') { const fd = docs.get(ev.path); if (fd) fd.onFile(ev); if (ev.action === 'deleted' || !fd) listSoon(); return; }
    const d = docs.get(ev.kernel);
    if (d) d.onNb(ev);
  });
  ctx.on('tutorials', renderTut);
  ctx.on('tab', (t) => { if (t === 'notebook') { loadList(); loadVars(); panes.forEach((p) => { const d = p.active && docs.get(p.active); if (d) d.autosize(); }); } });
  window.addEventListener('beforeunload', () => docs.forEach((d) => { if (d.dirty) d.save(false); }));

  loadList();
  restoreLayout();
  return { open: (path) => openDoc(path), save: () => { const d = focusedDoc(); return d ? d.save(true) : undefined; } };
}

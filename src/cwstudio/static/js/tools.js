// Notes pad, calculator, and live statistics of whatever the user selects (text anywhere in Studio, or a range of the waveform).
import { h, get, post, put, del, toast } from './api.js';
import { renderMarkdown } from './notebook.js';

// ---------------- statistics ----------------
const NUM_RE = /[-+]?(?:0x[0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)/g;

export function parseNumbers(text) {
  const out = [];
  for (const m of (text || '').matchAll(NUM_RE)) {
    const s = m[0];
    const v = /0x/i.test(s) ? parseInt(s.replace(/^\+/, ''), 16) * (s.startsWith('-') ? -1 : 1) : Number(s);
    if (Number.isFinite(v)) out.push(v);
  }
  return out;
}

export function computeStats(values) {
  const a = Array.from(values).filter(Number.isFinite);
  const n = a.length;
  if (!n) return { count: 0 };
  let sum = 0, min = Infinity, max = -Infinity, sq = 0, amin = 0, amax = 0;
  a.forEach((v, i) => { sum += v; sq += v * v; if (v < min) { min = v; amin = i; } if (v > max) { max = v; amax = i; } });
  const mean = sum / n;
  let dev = 0;
  a.forEach((v) => { dev += (v - mean) * (v - mean); });
  const sorted = n <= 200000 ? [...a].sort((x, y) => x - y) : null;
  const median = sorted ? (n % 2 ? sorted[(n - 1) / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2) : null;
  return { count: n, sum, mean, median, min, max, pk_pk: max - min, std: n > 1 ? Math.sqrt(dev / (n - 1)) : 0, variance: n > 1 ? dev / (n - 1) : 0, rms: Math.sqrt(sq / n), argmin: amin, argmax: amax };
}

export const fmtNum = (v) => (v == null || Number.isNaN(v) ? '' : Number.isInteger(v) && Math.abs(v) < 1e15 ? String(v) : Math.abs(v) >= 1e6 || (Math.abs(v) < 1e-3 && v !== 0) ? v.toExponential(5) : v.toFixed(6).replace(/\.?0+$/, ''));
const LABELS = [['count', 'Count'], ['sum', 'Sum'], ['mean', 'Mean (average)'], ['median', 'Median'], ['min', 'Min'], ['max', 'Max'], ['pk_pk', 'Peak to peak'], ['std', 'Std deviation'], ['variance', 'Variance'], ['rms', 'RMS'], ['argmin', 'Index of min'], ['argmax', 'Index of max']];

function statsTable(st, extra) {
  if (!st || !st.count) return h('div', { class: 'help' }, 'Nothing selected yet.');
  const kv = h('div', { class: 'kv stats' });
  LABELS.forEach(([k, label]) => { if (st[k] != null) kv.append(h('span', { class: 'k' }, label), h('span', { class: 'mono' }, fmtNum(st[k]))); });
  (extra || []).forEach(([k, v]) => kv.append(h('span', { class: 'k' }, k), h('span', { class: 'mono' }, v)));
  return kv;
}

export function statsText(st, title) {
  if (!st || !st.count) return '';
  return `${title || 'Selection'}: ` + LABELS.filter(([k]) => st[k] != null).map(([k, l]) => `${l.toLowerCase()} ${fmtNum(st[k])}`).join(', ');
}

// Live stats of selected text anywhere in the app (like a spreadsheet status bar).
export function initSelectionStats(ctx, badgeEl) {
  let last = '';
  function currentSelectionText() {
    const ae = document.activeElement;
    if (ae && (ae.tagName === 'TEXTAREA' || (ae.tagName === 'INPUT' && /^(text|search|number)?$/.test(ae.type || ''))) && ae.selectionStart !== ae.selectionEnd) {
      try { return ae.value.slice(ae.selectionStart, ae.selectionEnd); } catch (e) { return ''; }
    }
    const sel = window.getSelection();
    return sel ? sel.toString() : '';
  }
  function update() {
    const text = currentSelectionText();
    if (text === last) return;
    last = text;
    const nums = text.length > 500000 ? [] : parseNumbers(text);
    const st = computeStats(nums);
    ctx.selection = { source: 'text', text, values: nums, stats: st };
    ctx.emit('selection', ctx.selection);
    if (st.count >= 1) {
      badgeEl.style.display = '';
      badgeEl.textContent = st.count === 1 ? `Selected: ${fmtNum(st.sum)}` : `n ${st.count}  ·  Σ ${fmtNum(st.sum)}  ·  mean ${fmtNum(st.mean)}  ·  min ${fmtNum(st.min)}  ·  max ${fmtNum(st.max)}`;
      badgeEl.title = 'Statistics of the numbers in your selection. Open the Calc tab for more.';
    } else badgeEl.style.display = 'none';
  }
  let t = null;
  const schedule = () => { clearTimeout(t); t = setTimeout(update, 60); };
  document.addEventListener('selectionchange', schedule);
  document.addEventListener('mouseup', schedule);
  document.addEventListener('keyup', schedule);
  badgeEl.addEventListener('click', () => ctx.showTab('calc'));
}

// ---------------- notes ----------------
export function initNotes(ctx, el) {
  let cur = null, timer = null, preview = false;
  const listSel = h('select', { class: 'flex' });
  const nameIn = h('input', { class: 'flex', placeholder: 'Note name' });
  const ta = h('textarea', { class: 'notes-text', placeholder: 'Write anything: keys you found, glitch settings that worked, to-dos. Saved automatically.', spellcheck: 'true' });
  const prev = h('div', { class: 'notes-preview', style: 'display:none' });
  const saved = h('span', { class: 'muted', style: 'font-size:12px' });
  const previewBtn = h('button', { class: 'btn sm ghost', onclick: () => { preview = !preview; render(); } }, 'Preview');

  function render() {
    ta.style.display = preview ? 'none' : '';
    prev.style.display = preview ? '' : 'none';
    previewBtn.classList.toggle('active', preview);
    if (preview) { prev.innerHTML = ''; prev.append(renderMarkdown(ta.value || '*Empty note*')); }
  }
  async function loadList(select) {
    const notes = await get('/api/notes');
    if (!notes.length) { const n = await post('/api/notes', { name: 'Notes' }); notes.push(n); }
    listSel.innerHTML = '';
    notes.forEach((n) => listSel.append(h('option', { value: n.name }, n.name.replace(/\.(md|txt)$/, ''))));
    const want = select || (cur && cur.name) || notes[0].name;
    listSel.value = notes.some((n) => n.name === want) ? want : notes[0].name;
    await open(listSel.value);
  }
  async function open(name) {
    if (timer) await flush();
    cur = await get(`/api/notes/${encodeURIComponent(name)}`);
    ta.value = cur.text; nameIn.value = cur.name.replace(/\.(md|txt)$/, ''); saved.textContent = '';
    render();
  }
  async function flush() {
    clearTimeout(timer); timer = null;
    if (!cur) return;
    try { await put(`/api/notes/${encodeURIComponent(cur.name)}`, { text: ta.value }); saved.textContent = 'Saved'; } catch (e) { saved.textContent = 'Save failed'; toast(e.message, 'err'); }
  }
  ta.addEventListener('input', () => { saved.textContent = 'Editing…'; clearTimeout(timer); timer = setTimeout(flush, 700); });
  nameIn.addEventListener('change', async () => {
    const want = nameIn.value.trim();
    if (!cur || !want || want === cur.name.replace(/\.(md|txt)$/, '')) return;
    try { const r = await put(`/api/notes/${encodeURIComponent(cur.name)}`, { text: ta.value, rename: want + (cur.name.endsWith('.txt') ? '.txt' : '.md') }); await loadList(r.name); } catch (e) { toast(e.message, 'err'); nameIn.value = cur.name.replace(/\.(md|txt)$/, ''); }
  });
  listSel.addEventListener('change', () => open(listSel.value));
  function insert(text) {
    const s = ta.selectionEnd ?? ta.value.length;
    ta.setRangeText((s && ta.value[s - 1] !== '\n' ? '\n' : '') + text + '\n', s, s, 'end');
    ta.dispatchEvent(new Event('input'));
    if (preview) render();
  }
  ctx.notes = { insert, open: () => ctx.showTab('notes') };

  el.append(
    h('div', { class: 'card notes-card' },
      h('div', { class: 'row' }, listSel,
        h('button', { class: 'btn sm', onclick: async () => { const n = await post('/api/notes', {}); await loadList(n.name); nameIn.focus(); nameIn.select(); } }, 'New'),
        h('button', { class: 'btn sm ghost', onclick: async () => { if (!cur || !confirm(`Delete note "${cur.name}"?`)) return; await del(`/api/notes/${encodeURIComponent(cur.name)}`); cur = null; loadList(); } }, 'Delete')),
      h('div', { class: 'row' }, h('label', {}, 'Name'), nameIn),
      h('div', { class: 'row' },
        h('button', { class: 'btn sm', title: 'Insert the statistics of the current selection', onclick: () => { const s = ctx.selection && ctx.selection.stats; if (s && s.count) insert(statsText(s, ctx.selection.label)); else toast('Select some numbers or a waveform range first', 'warn'); } }, 'Insert selection stats'),
        h('button', { class: 'btn sm', title: 'Insert the latest CPA key', onclick: async () => { try { const r = await get('/api/analysis/cpa'); if (r && r.best_key) insert(`CPA (${r.model}, ${r.traces_used} traces): key ${r.best_key}`); else toast('No CPA result yet', 'warn'); } catch (e) { toast(e.message, 'err'); } } }, 'Insert CPA key'),
        h('div', { class: 'spacer' }), previewBtn, saved),
      ta, prev),
    h('div', { class: 'help' }, 'Notes are Markdown text files in your Studio data folder (notes/). Selecting numbers here also updates the selection statistics.'));
  ctx.on('tab', (t) => { if (t === 'notes') setTimeout(() => ta.focus(), 50); });
  loadList().catch(() => {});
}

// ---------------- calculator ----------------
export function initCalc(ctx, el) {
  const exprIn = h('input', { class: 'flex mono calc-in', placeholder: 'e.g. 0x2b ^ 0x7e, hw(0xff), mean(1,2,3), 3.3/4096, x = 2**10', autocomplete: 'off', spellcheck: 'false' });
  const history = h('div', { class: 'calc-history' });
  const hist = [];
  let hIdx = -1;
  async function evaluate() {
    const expr = exprIn.value.trim();
    if (!expr) return;
    try {
      const r = await post('/api/calc', { expr });
      hist.unshift(expr); hIdx = -1;
      const row = h('div', { class: 'calc-row' },
        h('div', { class: 'e mono' }, expr),
        h('div', { class: 'r mono', title: 'Click to use as input' }, '= ' + r.text, r.hex ? h('span', { class: 'muted' }, `  ${r.hex}  ${r.bin.length <= 40 ? r.bin : ''}`) : null));
      row.querySelector('.r').addEventListener('click', () => { exprIn.value = r.hex && /0x/.test(expr) ? r.hex : r.text; exprIn.focus(); });
      history.prepend(row);
      exprIn.value = '';
    } catch (e) { toast(e.message, 'err', 5000); }
  }
  exprIn.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') evaluate();
    else if (e.key === 'ArrowUp' && hist.length) { hIdx = Math.min(hIdx + 1, hist.length - 1); exprIn.value = hist[hIdx]; e.preventDefault(); }
    else if (e.key === 'ArrowDown') { hIdx = Math.max(hIdx - 1, -1); exprIn.value = hIdx >= 0 ? hist[hIdx] : ''; e.preventDefault(); }
  });

  // selection statistics
  const srcSel = h('select', { class: 'flex' },
    h('option', { value: 'text' }, 'Selected text (anywhere in Studio)'),
    h('option', { value: 'cursors' }, 'Waveform: between cursors A and B'),
    h('option', { value: 'zoom' }, 'Waveform: visible (zoomed) range'),
    h('option', { value: 'trace' }, 'Waveform: whole displayed trace'),
    h('option', { value: 'column' }, 'Stored traces: value at cursor A across traces'),
    h('option', { value: 'list' }, 'Numbers I type or paste'));
  const listIn = h('textarea', { class: 'mono', rows: 4, placeholder: 'Paste numbers separated by spaces, commas or lines', style: 'display:none' });
  const out = h('div');
  const note = h('div', { class: 'help' });
  let lastStats = null, lastLabel = '';

  function waveRange(kind) {
    const w = ctx.wave, cur = w && w.current();
    if (!cur || !cur.samples || !cur.samples.length) throw new Error('No trace in the waveform view yet');
    const n = cur.samples.length;
    if (kind === 'trace') return [0, n, cur];
    if (kind === 'zoom') { const r = w.zoomRange() || [0, n]; return [Math.max(0, r[0]), Math.min(n, r[1]), cur]; }
    const { a, b } = w.cursors;
    if (a == null || b == null) throw new Error('Place cursor A (click) and cursor B (Shift+click) on the waveform');
    return [Math.min(a, b), Math.max(a, b) + 1, cur];
  }
  async function refresh() {
    const src = srcSel.value;
    listIn.style.display = src === 'list' ? '' : 'none';
    try {
      let st, extra = [], label = '';
      if (src === 'text') {
        const s = ctx.selection;
        st = s && s.source === 'text' ? s.stats : null;
        label = 'Selected text';
        note.textContent = st && st.count ? `${st.count} numbers found in the selection (decimal, scientific or 0x hex).` : 'Select text containing numbers anywhere: a notebook output, the log, a note, the serial console...';
      } else if (src === 'list') {
        st = computeStats(parseNumbers(listIn.value)); label = 'Typed numbers'; note.textContent = '';
      } else if (src === 'column') {
        const a = ctx.wave && ctx.wave.cursors.a;
        if (a == null) throw new Error('Place cursor A on the waveform (click) to pick a sample');
        st = await post('/api/calc/stats', { source: 'sample', sample: a });
        label = `Sample ${a} across stored traces`;
        note.textContent = `Distribution of sample ${a} over all ${st.count} stored traces.`;
      } else {
        const [s0, s1, cur] = waveRange(src);
        st = computeStats(cur.samples.subarray(s0, s1));
        const idx = cur.header && cur.header.index != null && cur.header.index >= 0 ? `trace #${cur.header.index}` : 'live trace';
        label = `${idx} samples ${s0} to ${s1 - 1}`;
        const sr = ctx.wave.sampleRate;
        extra.push(['Samples', `${s0} to ${s1 - 1}`]);
        if (sr) extra.push(['Duration', `${fmtNum((s1 - s0) / sr * 1e6)} µs`], ['1 / duration', `${fmtNum(sr / Math.max(1, s1 - s0) / 1e3)} kHz`]);
        note.textContent = '';
      }
      lastStats = st; lastLabel = label;
      if (src !== 'text') { ctx.selection = { source: src, stats: st, label }; }
      out.replaceChildren(statsTable(st, extra));
    } catch (e) { out.replaceChildren(h('div', { class: 'help err' }, e.message)); lastStats = null; }
  }
  srcSel.addEventListener('change', refresh);
  listIn.addEventListener('input', refresh);
  ctx.on('selection', (s) => { if (srcSel.value === 'text' && s.source === 'text') refresh(); });
  ctx.on('tab', (t) => { if (t === 'calc') refresh(); });
  ctx.on('capture', () => { if (['cursors', 'zoom', 'trace'].includes(srcSel.value)) refresh(); });

  el.append(
    h('h2', {}, 'Calculator'),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, exprIn, h('button', { class: 'btn primary sm', onclick: evaluate }, '=')),
      history,
      h('details', {}, h('summary', {}, 'What can it do?'),
        h('div', { class: 'help' }, 'Operators: + - * / // % ** and bitwise ^ (XOR) & | ~ << >>. Numbers in decimal, 0x hex, 0b binary or scientific. Functions: sqrt, log, log2, log10, exp, sin, cos, abs, round, floor, ceil, min, max, sum, mean, median, std, var, rms, hex, bin, int. For side-channel work: hw(x) Hamming weight, hd(a, b) Hamming distance, sbox(x) AES S-box, db(ratio), undb(dB). ans is the last result; name = expr stores a variable.'))),
    h('h2', {}, 'Selection statistics', h('span', { class: 'aside' }, h('button', { class: 'btn ghost sm', onclick: refresh }, 'Refresh'))),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, srcSel), listIn, note, out,
      h('div', { class: 'row', style: 'margin-top:10px' },
        h('button', { class: 'btn sm', onclick: async () => { const t = statsText(lastStats, lastLabel); if (!t) return; try { await navigator.clipboard.writeText(t); toast('Copied', 'ok', 1200); } catch (e) { toast('Copy failed', 'warn'); } } }, 'Copy'),
        h('button', { class: 'btn sm', onclick: () => { const t = statsText(lastStats, lastLabel); if (!t) return; ctx.notes && ctx.notes.insert(t); toast('Added to notes', 'ok', 1200); } }, 'Add to notes'))),
    h('div', { class: 'help' }, 'Tip: select numbers anywhere in Studio and the log bar shows count, sum, mean, min and max instantly.'));
}

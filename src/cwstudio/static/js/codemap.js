// Code tab: build the code map (emulate the firmware for a captured trace), align it with the traces, and see which functions and source lines a region of the waveform is.
import { h, get, post, put, toast, fmtHz, upload } from './api.js';
import { CodeBand, funcColor, refreshTheme } from './codeband.js';

const KW = new Set('if else for while do return switch case default break continue goto sizeof static const volatile extern inline struct union enum typedef register asm __asm__ __attribute__'.split(' '));
const TY = new Set('void char short int long float double signed unsigned bool uint8_t uint16_t uint32_t uint64_t int8_t int16_t int32_t int64_t size_t uintptr_t state_t'.split(' '));

function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

/** Syntax-highlight one line of C; ``st.c`` carries an open block comment to the next line. */
function hlLine(line, st) {
  let out = '', i = 0;
  const n = line.length;
  if (st.c) {
    const e = line.indexOf('*/');
    if (e < 0) return `<span class="co">${esc(line)}</span>`;
    out += `<span class="co">${esc(line.slice(0, e + 2))}</span>`; i = e + 2; st.c = false;
  }
  if (/^\s*#/.test(line.slice(i))) return out + `<span class="pp">${esc(line.slice(i))}</span>`;
  while (i < n) {
    const ch = line[i], rest = line.slice(i);
    let m;
    if (rest.startsWith('//')) { out += `<span class="co">${esc(rest)}</span>`; break; }
    if (rest.startsWith('/*')) {
      const e = line.indexOf('*/', i + 2);
      if (e < 0) { out += `<span class="co">${esc(rest)}</span>`; st.c = true; break; }
      out += `<span class="co">${esc(line.slice(i, e + 2))}</span>`; i = e + 2; continue;
    }
    if (ch === '"' || ch === "'") {
      let j = i + 1;
      while (j < n && line[j] !== ch) j += line[j] === '\\' ? 2 : 1;
      out += `<span class="st">${esc(line.slice(i, j + 1))}</span>`; i = j + 1; continue;
    }
    if ((m = /^(0x[0-9a-fA-F]+|\d+\.?\d*(?:[eE][-+]?\d+)?)[uUlLfF]*/.exec(rest)) && !/[A-Za-z_]/.test(line[i - 1] || '')) { out += `<span class="nu">${esc(m[0])}</span>`; i += m[0].length; continue; }
    if ((m = /^[A-Za-z_]\w*/.exec(rest))) {
      const w = m[0];
      out += KW.has(w) ? `<span class="kw">${w}</span>` : TY.has(w) ? `<span class="ty">${w}</span>` : esc(w);
      i += w.length; continue;
    }
    out += esc(ch); i++;
  }
  return out;
}

export function initCodeMap(ctx, el) {
  const band = new CodeBand(document.getElementById('code-band'), ctx.wave, ctx);
  ctx.codeBand = band;
  ctx.wave.onCodeToggle = (on) => { band.setVisible(on); if (on && !bandData) loadBand(); };

  let st = null, bandData = null, builtAt = null, lastRegion = null, curFile = null, curLine = null, regionFiles = {};
  const srcCache = {};

  // ----- firmware card -----
  const elfSel = h('select', { id: 'cm-elf', class: 'flex', onchange: () => { pathRow.style.display = elfSel.value === '__other' ? '' : 'none'; } });
  const elfPath = h('input', { id: 'cm-elf-path', class: 'flex mono', placeholder: '.elf or project folder on the Studio machine' });
  const elfFile = h('input', { id: 'cm-elf-file', type: 'file', accept: '.elf', class: 'flex', title: 'or upload an .elf from this computer' });
  const pathRow = h('div', { style: 'display:none' }, h('div', { class: 'row' }, h('label', {}, ''), elfPath), h('div', { class: 'row' }, h('label', {}, ''), elfFile));
  const srcIn = h('input', { id: 'cm-sources', class: 'flex mono', placeholder: 'source folder (default: the paths in the ELF)' });
  const traceIn = h('input', { id: 'cm-trace', type: 'number', min: 0, placeholder: 'newest', style: 'width:90px' });
  const coreSel = h('select', { id: 'cm-core', class: 'flex', title: 'timing model; auto picks it from the ELF' }, h('option', { value: '' }, 'auto'));
  const protoSel = h('select', { id: 'cm-proto', title: 'SimpleSerial version; auto asks the firmware' }, ...[['', 'auto'], ['2.1', '2.1'], ['1.1', '1.1'], ['1.0', '1.0']].map(([v, t]) => h('option', { value: v }, t)));
  const wsIn = h('input', { id: 'cm-ws', type: 'number', min: 0, max: 15, value: 0, style: 'width:64px', title: 'flash wait states (Arm and RISC-V timing)' });
  const buildBtn = h('button', { id: 'cm-build', class: 'btn primary', onclick: () => build() }, 'Build code map');
  const buildMsg = h('div', { class: 'help', id: 'cm-build-msg' }, 'Emulates the firmware for the inputs of a captured trace and maps its code onto the samples. Uses the firmware programmed in this session, else the newest build.');
  const info = h('div', { class: 'kv', id: 'cm-info', style: 'margin-top:10px' });

  // ----- mapping card -----
  const mapKv = h('div', { class: 'kv', id: 'cm-map' });
  const shiftIn = h('input', { id: 'cm-shift', type: 'number', step: 0.5, style: 'width:86px', onchange: () => setMapping({ shift: +shiftIn.value }) });
  const scaleIn = h('input', { id: 'cm-scale', type: 'number', step: 0.001, style: 'width:86px', onchange: () => setMapping({ scale: +scaleIn.value }) });
  const nudge = (label, title, body) => h('button', { class: 'btn sm', title, onclick: () => setMapping(body) }, label);
  const alignSrc = h('select', { id: 'cm-align-src' }, h('option', { value: 'mean' }, 'mean trace'), h('option', { value: 'trace' }, 'this trace'));
  const alignBtn = h('button', { id: 'cm-align', class: 'btn', onclick: () => align() }, 'Auto align');
  const meter = h('div', { class: 'cm-meter', id: 'cm-conf' }, h('i', { style: 'width:0%' }));
  const confTxt = h('div', { class: 'help mono', id: 'cm-conf-txt' }, 'not aligned yet');

  // ----- selection card -----
  const regionTxt = h('div', { class: 'help mono', id: 'cm-region' }, 'no region: ctrl+drag on the waveform, or use the cursors');
  const funcList = h('div', { class: 'cm-list', id: 'cm-funcs', 'data-empty': 'Ctrl+drag on the waveform (or pick a block in the Code band) to see which code a region is.' });
  const lineList = h('div', { class: 'cm-list', id: 'cm-lines', 'data-empty': 'Source lines of the region appear here.' });

  // ----- source card -----
  const fileTabs = h('div', { class: 'cm-tabs', id: 'cm-files' });
  const srcView = h('div', { class: 'cm-src', id: 'cm-source' }, h('div', { class: 'help', style: 'padding:10px' }, 'Pick a function or line to see its source.'));
  const disView = h('div', { class: 'cm-dis', id: 'cm-disasm', style: 'display:none' });

  // ----- exact mode -----
  const pcBtn = h('button', { class: 'btn sm', id: 'cm-pctrace', onclick: () => pcTrace() }, 'Record PC samples');
  const pcTxt = h('div', { class: 'help mono', id: 'cm-pc-txt' });
  const exactCard = h('div', { class: 'card', style: 'display:none' },
    h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'Exact mode'), h('span', { class: 'sub' }, 'real program counter samples from the Husky Arm trace port (SWO)')),
    h('div', { class: 'row' }, pcBtn, h('span', { class: 'muted', id: 'cm-pc-hint', style: 'font-size:12px' }, 'needs simpleserial-trace firmware on an Arm target')), pcTxt);

  el.append(
    h('h2', {}, 'Firmware'),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('label', {}, 'ELF'), elfSel, h('button', { class: 'btn sm ghost', title: 'Refresh the list', onclick: () => loadElfs() }, '↻')),
      pathRow,
      h('div', { class: 'row' }, h('label', {}, 'Sources'), srcIn),
      h('div', { class: 'row' }, h('label', {}, 'Trace'), traceIn, h('label', {}, 'Core'), coreSel),
      h('div', { class: 'row' }, h('label', {}, 'SimpleSerial'), protoSel, h('label', {}, 'Wait states'), wsIn),
      h('div', { class: 'row' }, buildBtn),
      buildMsg, info),
    h('h2', {}, 'Cycles to samples'),
    h('div', { class: 'card' }, mapKv,
      h('div', { class: 'row', style: 'margin-top:10px' }, h('label', {}, 'Shift'), shiftIn, h('span', { class: 'cm-nudge' }, nudge('−cyc', 'one cycle earlier', { dshift: 'cycle-' }), nudge('−1', 'one sample earlier', { dshift: -1 }), nudge('+1', 'one sample later', { dshift: 1 }), nudge('+cyc', 'one cycle later', { dshift: 'cycle+' }))),
      h('div', { class: 'row' }, h('label', {}, 'Scale'), scaleIn, h('span', { class: 'cm-nudge' }, nudge('−0.1%', 'shrink by 0.1%', { dscale: -0.001 }), nudge('+0.1%', 'stretch by 0.1%', { dscale: 0.001 }), nudge('1:1', 'shift 0, scale 1', { reset: true }))),
      h('div', { class: 'row' }, h('label', {}, 'Align on'), alignSrc, alignBtn),
      h('div', { class: 'row' }, h('label', {}, 'Confidence'), meter), confTxt),
    h('h2', {}, 'Selection'),
    h('div', { class: 'card' }, regionTxt,
      h('div', { class: 'row', style: 'margin:8px 0' },
        h('button', { class: 'btn sm', id: 'cm-from-cursors', onclick: () => fromCursors() }, 'Cursors A to B'),
        h('button', { class: 'btn sm', id: 'cm-window', onclick: () => triggerWindow() }, 'Trigger window'),
        h('button', { class: 'btn sm ghost', onclick: () => { ctx.wave.setRegion(null); ctx.wave.setHighlights([]); band.select(null); } }, 'Clear')),
      h('div', { class: 'help', style: 'margin:2px 0 4px' }, 'Functions'), funcList,
      h('div', { class: 'help', style: 'margin:10px 0 4px' }, 'Source lines'), lineList),
    h('h2', {}, 'Source'),
    h('div', { class: 'card' }, fileTabs, srcView, disView),
    exactCard,
  );

  // ----- helpers -----
  const kv = (box, rows) => { box.innerHTML = ''; rows.forEach(([k, v, cls]) => { if (v === undefined) return; box.append(h('span', { class: 'k' }, k), h('span', { class: cls || '' }, v)); }); };
  const mapping = () => (st && st.mapping) || null;
  const sample = (c) => { const m = mapping(); return c * m.samples_per_cycle + (-(m.adc_offset || 0) + (m.presamples || 0) + (m.shift || 0)); };
  const fmt = (v) => (v == null ? '?' : Math.round(v).toLocaleString());

  async function loadElfs() {
    try {
      const list = await get('/api/codemap/elfs');
      const cur = elfSel.value;
      elfSel.innerHTML = '';
      elfSel.append(h('option', { value: '' }, 'default (programmed or newest build)'));
      list.forEach((e) => elfSel.append(h('option', { value: e.path, title: e.path }, `${e.name}${e.source === 'programmed' ? ' (programmed)' : ''}`)));
      elfSel.append(h('option', { value: '__other' }, 'Other ELF file…'));
      if ([...elfSel.options].some((o) => o.value === cur)) elfSel.value = cur;
    } catch (e) { /* ignore */ }
  }

  function renderStatus() {
    if (!st) return;
    if (coreSel.options.length <= 1 && st.cores) Object.entries(st.cores).forEach(([k, v]) => coreSel.append(h('option', { value: k }, v)));
    const fw = st.firmware || {};
    const prog = st.program || {};
    if (!st.ready) {
      kv(info, [['State', 'no code map yet']]);
    } else {
      const aes = st.aes_ok == null ? (st.response ? st.response : 'no response') : (st.aes_ok ? 'correct AES ✓' : 'not the expected AES');
      const sim = st.sim_emulates == null ? undefined : (st.sim_emulates ? 'runs this firmware' : 'built-in AES model (program this ELF to the simulator for matching traces)');
      kv(info, [
        ['Firmware', st.name], ['Core', `${fw.core_label || fw.core || '?'}${prog.has_dwarf ? '' : ' (no debug info: functions only)'}`], ['Protocol', `SimpleSerial ${fw.protocol || '?'} · trigger by ${fw.trigger || '?'}`],
        ['Inputs', st.trace != null ? `trace #${st.trace}` : 'given key and plaintext'], ['Response', aes, st.aes_ok === false ? 'err' : (st.aes_ok ? 'ok' : '')],
        ['Stored trace', st.matches_stored == null ? undefined : (st.matches_stored ? 'same ciphertext ✓' : 'different ciphertext'), st.matches_stored === false ? 'warn' : 'ok'],
        ['Run', `${fmt(st.instructions)} instructions · ${fmt(st.cycles)} cycles · trigger window ${fmt(st.trigger_cycles)} cycles`], ['Simulator', sim, st.sim_emulates === false ? 'warn' : ''],
      ]);
    }
    const m = st.mapping;
    if (m) {
      kv(mapKv, [['Target clock', fmtHz(m.target_freq)], ['ADC clock', `${fmtHz(m.adc_freq)}${m.decimate > 1 ? ' / ' + m.decimate : ''}`], ['Samples/cycle', (m.spc).toFixed(4) + (m.scale !== 1 ? ` × ${m.scale.toFixed(4)}` : '')], ['ADC offset', `${m.adc_offset} · pre-trigger ${m.presamples}`]]);
      if (document.activeElement !== shiftIn) shiftIn.value = (+m.shift).toFixed(2);
      if (document.activeElement !== scaleIn) scaleIn.value = (+m.scale).toFixed(5);
    }
    const al = st.alignment;
    if (al && !al.error) {
      meter.className = 'cm-meter ' + al.label;
      meter.firstChild.style.width = Math.round(al.confidence * 100) + '%';
      confTxt.textContent = `${al.label} (${al.confidence.toFixed(2)}) · r ${al.r.toFixed(3)}${al.inverted ? ' (inverted, as on the shunt)' : ''} · shift ${al.shift.toFixed(1)} · scale ${al.scale.toFixed(4)} · on the ${al.source === 'trace' ? 'trace' : 'mean trace'}`;
    } else {
      meter.className = 'cm-meter'; meter.firstChild.style.width = '0%';
      confTxt.textContent = al && al.error ? `alignment failed: ${al.error}` : 'not aligned yet (needs stored traces)';
    }
    alignBtn.disabled = !st.ready;
  }

  async function refresh() {
    try { st = await get('/api/codemap'); } catch (e) { return; }
    renderStatus();
    if (st.ready && st.built !== builtAt) await loadBand();
    let pref = null;
    try { pref = localStorage.getItem('cw.codeband'); } catch (e) { /* ignore */ }
    if (st.ready && pref === null && !ctx.wave.showCode) ctx.wave.setShowCode(true); // a code map exists and the band was never switched off: show it
  }

  async function loadBand() {
    try {
      const r = await get('/api/codemap/band');
      bandData = r.band; builtAt = st && st.built;
      band.setData(r.band, r.mapping);
      if (ctx.wave.region) query(ctx.wave.region);
    } catch (e) { band.setData(null); }
  }

  async function build() {
    const p = { align: 'auto' };
    let elf = elfSel.value === '__other' ? elfPath.value.trim() : elfSel.value;
    if (elfSel.value === '__other' && elfFile.files.length) {
      try { elf = (await upload('/api/codemap/upload', elfFile.files[0])).path; } catch (e) { buildMsg.textContent = e.message; buildMsg.className = 'help err'; return; }
    }
    if (elf) p.elf = elf;
    if (srcIn.value.trim()) p.sources = srcIn.value.trim();
    if (traceIn.value !== '') p.trace = +traceIn.value;
    if (coreSel.value) p.core = coreSel.value;
    if (protoSel.value) p.protocol = protoSel.value;
    if (+wsIn.value) p.wait_states = +wsIn.value;
    buildBtn.disabled = true; buildMsg.className = 'help'; buildMsg.textContent = 'Emulating…';
    try {
      st = await post('/api/codemap/build', p);
      delete st.band;
      buildMsg.textContent = `Built in ${st.seconds}s: ${fmt(st.instructions)} instructions emulated.`;
      buildMsg.className = 'help ok';
      renderStatus();
      await loadBand();
      if (!ctx.wave.showCode) ctx.wave.setShowCode(true);
      loadElfs();
    } catch (e) {
      buildMsg.textContent = e.message; buildMsg.className = 'help err';
      toast(e.message, 'err', 7000);
    } finally { buildBtn.disabled = false; }
  }

  async function setMapping(body) {
    if (!st || !st.ready) return;
    const m = st.mapping;
    if (body.dshift === 'cycle-') body = { dshift: -m.samples_per_cycle };
    if (body.dshift === 'cycle+') body = { dshift: m.samples_per_cycle };
    try {
      const r = await put('/api/codemap/mapping', body);
      st.mapping = r.mapping; renderStatus(); band.setMapping(r.mapping);
      if (ctx.wave.region) query(ctx.wave.region);
    } catch (e) { toast(e.message, 'err'); }
  }

  async function align() {
    alignBtn.disabled = true; confTxt.textContent = 'aligning…';
    try {
      const r = await post('/api/codemap/align', { source: alignSrc.value, trace: st && st.trace != null ? st.trace : undefined });
      st.mapping = r.mapping; st.alignment = r.alignment; renderStatus(); band.setMapping(r.mapping);
      if (ctx.wave.region) query(ctx.wave.region);
    } catch (e) { confTxt.textContent = e.message; toast(e.message, 'err', 6000); } finally { alignBtn.disabled = !(st && st.ready); }
  }

  function fromCursors() {
    const { a, b } = ctx.wave.cursors;
    if (a == null || b == null) { toast('Place cursor A (click) and B (shift+click) on the waveform first', 'warn'); return; }
    ctx.wave.setRegion(a, b, 'cursors');
  }

  function triggerWindow() {
    if (!bandData || !st || !st.mapping) return;
    const t1 = bandData.t1 != null ? bandData.t1 : bandData.total;
    ctx.wave.setRegion(Math.max(0, Math.round(sample(0))), Math.round(sample(t1)), 'window');
  }

  let qSeq = 0;
  async function query(r) {
    if (!st || !st.ready) { regionTxt.textContent = r ? `samples ${r[0]} to ${r[1]} · build the code map to see its code` : 'no region'; return; }
    const seq = ++qSeq;
    let res;
    try { res = await post('/api/codemap/region', { start: r[0], end: r[1], limit: 300 }); } catch (e) { toast(e.message, 'err'); return; }
    if (seq !== qSeq) return;
    lastRegion = res;
    regionTxt.textContent = `samples ${fmt(r[0])} to ${fmt(r[1])} · cycles ${fmt(res.cycles[0])} to ${fmt(res.cycles[1])} · ${fmt(res.instructions)} instructions · ${res.functions.length} functions · ${res.lines_total || 0} lines`;
    renderRegion(res);
  }

  function renderRegion(res) {
    refreshTheme();
    funcList.innerHTML = '';
    const maxSelf = Math.max(1e-9, ...res.functions.map((f) => f.self_cycles));
    // functions that execute in the region first (in time order), then the callers it runs inside
    const own = res.functions.filter((f) => f.self_cycles > 0), callers = res.functions.filter((f) => f.self_cycles <= 0 && f.spans.length).sort((a, b) => a.depth - b.depth);
    (res.inlined || []).forEach((f) => {
      const item = h('div', { class: 'cm-item inl', 'data-func': f.name, title: `${f.name} was inlined into ${f.into.join(', ')}: ${fmt(f.cycles)} cycles of its code here` },
        h('span', { class: 'sw', style: `background:${funcColor(f.name)}` }), h('span', { class: 'nm' }, f.name), h('span', { class: 'loc' }, `${fmt(f.cycles)} cyc · inlined`),
        h('span', { class: 'code' }, `inlined into ${f.into.join(', ')} · samples ${fmt(f.samples[0])} to ${fmt(f.samples[1])}`));
      item.addEventListener('click', () => pickFunction(f.name, item));
      own.push({ inlinedItem: item, first: f.first });
    });
    own.sort((a, b) => a.first - b.first);
    own.concat(callers).forEach((f) => {
      if (f.inlinedItem) { funcList.append(f.inlinedItem); return; }
      const caller = f.self_cycles <= 0;
      const item = h('div', { class: 'cm-item' + (caller ? ' caller' : ''), 'data-func': f.name, title: caller ? `${f.name}: the region runs inside this call` : `${f.name}: ${fmt(f.self_cycles)} cycles of its own code here (${(f.share * 100).toFixed(1)}%), samples ${fmt(f.samples[0])} to ${fmt(f.samples[1])}` },
        h('span', { class: 'sw', style: `background:${funcColor(f.name)}` }), h('span', { class: 'nm' }, f.name), h('span', { class: 'loc' }, caller ? 'caller' : `${fmt(f.self_cycles)} cyc`),
        caller ? null : h('div', { class: 'bar' }, h('i', { style: `width:${Math.round((f.self_cycles / maxSelf) * 100)}%` })),
        h('span', { class: 'code' }, f.file ? `${f.file}:${f.line} · samples ${fmt(f.samples[0])} to ${fmt(f.samples[1])}` : `samples ${fmt(f.samples[0])} to ${fmt(f.samples[1])}`));
      item.addEventListener('click', () => pickFunction(f.name, item));
      funcList.append(item);
    });
    lineList.innerHTML = '';
    regionFiles = {};
    res.lines.forEach((l) => {
      if (l.file_index >= 0) (regionFiles[l.file_index] = regionFiles[l.file_index] || new Set()).add(l.line);
      const item = h('div', { class: 'cm-item', 'data-line': `${l.file}:${l.line}`, title: `${l.path || ''}:${l.line} in ${l.func || '?'}${l.inline ? ' (inlined ' + l.inline + ')' : ''}: ${fmt(l.cycles)} cycles, ${l.executions} runs, samples ${fmt(l.samples[0])} to ${fmt(l.samples[1])}` },
        h('span', { class: 'sw', style: `background:${funcColor(l.func || '?')}` }), h('span', { class: 'nm mono', style: 'font-weight:500' }, `${l.file || '?'}:${l.line}`), h('span', { class: 'loc' }, `${fmt(l.cycles)} cyc · ×${l.executions}`),
        h('span', { class: 'code' }, l.text || '(no source)'));
      item.addEventListener('click', () => pickLine(l.file_index, l.line, item));
      lineList.append(item);
    });
    renderTabs();
    if (curFile == null || !regionFiles[curFile]) {
      const first = res.lines.find((l) => l.file_index >= 0);
      if (first) openSource(first.file_index, null);
    } else openSource(curFile, curLine);
  }

  function markSel(list, item) { list.querySelectorAll('.cm-item.sel').forEach((x) => x.classList.remove('sel')); if (item) item.classList.add('sel'); }

  async function pickFunction(name, item) {
    markSel(funcList, item); markSel(lineList, null);
    try {
      const r = await post('/api/codemap/lookup', { function: name });
      const hl = r.ranges.map((x) => ({ a: x.samples[0], b: x.samples[1] }));
      ctx.wave.setHighlights(hl);
      band.select({ kind: 'function', name });
      const reg = ctx.wave.region;
      const target = (reg && hl.find((x) => x.b >= reg[0] && x.a <= reg[1])) || hl[0];
      if (target) ctx.wave.showRange(target.a, target.b);
      if (r.file_index >= 0) openSource(r.file_index, r.line, false);
      showDisasm({ function: name });
    } catch (e) { toast(e.message, 'err'); }
  }

  async function pickLine(fileIndex, line, item) {
    markSel(lineList, item); markSel(funcList, null);
    try {
      const r = await post('/api/codemap/lookup', { file: fileIndex, line });
      const hl = r.ranges.map((x) => ({ a: x.samples[0], b: x.samples[1] }));
      ctx.wave.setHighlights(hl);
      band.select({ kind: 'line', file: fileIndex, line });
      const reg = ctx.wave.region;
      const inReg = reg ? hl.filter((x) => x.b >= reg[0] && x.a <= reg[1]) : [];
      const t = inReg.length ? inReg : hl;
      if (t.length) ctx.wave.showRange(Math.min(...t.map((x) => x.a)), Math.max(...t.map((x) => x.b)));
      openSource(fileIndex, line);
      showDisasm({ file: fileIndex, line });
    } catch (e) { toast(e.message, 'err'); }
  }

  function renderTabs() {
    fileTabs.innerHTML = '';
    const names = (bandData && bandData.files) || [];
    const ids = Object.keys(regionFiles).map(Number);
    if (curFile != null && !ids.includes(curFile)) ids.push(curFile);
    ids.forEach((i) => {
      const f = names.find((x) => x.i === i);
      fileTabs.append(h('button', { class: i === curFile ? 'on' : '', title: f ? f.path : '', onclick: () => openSource(i, null) }, f ? f.name : `file ${i}`));
    });
  }

  async function openSource(fileIndex, line, scroll = true) {
    curFile = fileIndex; curLine = line;
    renderTabs();
    let src = srcCache[fileIndex];
    if (!src || src.built !== builtAt) {
      try { src = await get(`/api/codemap/source?file=${fileIndex}`); src.built = builtAt; srcCache[fileIndex] = src; } catch (e) { srcView.innerHTML = ''; srcView.append(h('div', { class: 'help err', style: 'padding:10px' }, e.message)); return; }
    }
    if (curFile !== fileIndex) return;
    srcView.innerHTML = '';
    if (src.text == null) { srcView.append(h('div', { class: 'help', style: 'padding:10px' }, `${src.path}: ${src.reason}`)); return; }
    const code = new Set(src.code_lines), exec = src.executed || {};
    const inReg = regionFiles[fileIndex] || new Set();
    const state = { c: false };
    const frag = document.createDocumentFragment();
    let target = null;
    src.text.split('\n').forEach((t, k) => {
      const n = k + 1;
      const cls = ['ln'];
      if (code.has(n)) cls.push('code');
      if (exec[n]) cls.push('exec');
      if (inReg.has(n)) cls.push('inreg');
      if (n === line) cls.push('cur');
      const row = h('div', { class: cls.join(' '), 'data-ln': n, title: exec[n] ? `${Math.round(exec[n])} cycles in this run` : '' }, h('span', { class: 'no' }, String(n)), h('span', { class: 'ht' }), h('span', { class: 'tx', html: hlLine(t, state) || ' ' }));
      if (code.has(n)) row.addEventListener('click', () => pickLine(fileIndex, n, null));
      if (n === line || (!line && !target && inReg.has(n))) target = row;
      frag.append(row);
    });
    srcView.append(frag);
    if (target && scroll !== false) srcView.scrollTop = Math.max(0, target.offsetTop - srcView.clientHeight / 3);
    else if (target) srcView.scrollTop = Math.max(0, target.offsetTop - 20);
  }

  async function showDisasm(q) {
    const qs = new URLSearchParams(Object.entries(q).filter(([, v]) => v != null)).toString();
    try {
      const r = await get(`/api/codemap/disasm?${qs}`);
      disView.innerHTML = '';
      disView.style.display = r.instructions.length ? '' : 'none';
      r.instructions.forEach((ins) => disView.append(h('div', { class: 'di' + (ins.executed ? '' : ' zero'), title: ins.bytes }, h('span', { class: 'a' }, ins.addr.toString(16).padStart(8, '0')), h('span', { class: 't' }, ins.text), h('span', { class: 'n' }, ins.executed ? '×' + ins.executed : ''))));
    } catch (e) { disView.style.display = 'none'; }
  }

  async function pcTrace() {
    pcBtn.disabled = true; pcTxt.textContent = 'recording…';
    try {
      const r = await post('/api/codemap/pctrace', {});
      pcTxt.textContent = `${r.mode}: ${r.samples} PC samples every ${r.interval} cycles · ${(r.agreement * 100).toFixed(1)}% in the emulated function · cycle scale ${r.scale.toFixed(4)}`;
      ctx.wave.setHighlights((r.rows || []).filter((x) => !x.match).map((x) => ({ a: x.sample - 1, b: x.sample + 1 })));
    } catch (e) { pcTxt.textContent = e.message; } finally { pcBtn.disabled = false; }
  }

  // ----- wiring -----
  ctx.wave.onRegionChange((r, source) => {
    if (!r) { regionTxt.textContent = 'no region: ctrl+drag on the waveform, or use the cursors'; funcList.innerHTML = ''; lineList.innerHTML = ''; band.draw(); return; }
    band.draw();
    if (source === 'drag' && st && st.ready && ctx.activeTab !== 'code') ctx.showTab('code');
    query(r);
  });
  ctx.on('codemap-pick', (p) => {
    if (ctx.activeTab !== 'code') ctx.showTab('code');
    if (p.kind === 'function') { ctx.wave.setRegion(Math.round(p.samples[0]), Math.round(p.samples[1]), 'band'); pickFunction(p.name, null); } else pickLine(p.file, p.line, null);
  });
  ctx.on('codemap', (ev) => { st = Object.assign(st || {}, ev); renderStatus(); if (ev.ready && ev.built !== builtAt) loadBand(); });
  ctx.on('programmed', () => loadElfs());
  ctx.on('tab', (t) => { if (t === 'code') { loadElfs(); refresh(); } });
  ctx.on('status', (s) => {
    const cap = ctx.caps && ctx.caps.trace;
    exactCard.style.display = s.scope && s.scope.connected && cap && cap.available ? '' : 'none';
    const hint = exactCard.querySelector('#cm-pc-hint');
    if (hint) hint.textContent = s.scope && s.scope.simulated ? 'simulator: samples taken from the emulated run' : 'needs simpleserial-trace firmware on an Arm target';
  });
  ctx.on('scope-connected', async () => { try { ctx.caps = await get('/api/capabilities'); } catch (e) { /* ignore */ } });
  ctx.on('theme', () => { band.draw(); if (lastRegion) renderRegion(lastRegion); });
  loadElfs();
  refresh();
  return { refresh, build, query };
}

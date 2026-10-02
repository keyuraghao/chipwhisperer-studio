// Logic tab: a logic analyser for any ChipWhisperer. Captures come from the Husky's built-in LA, a line on the analog input of any scope, the simulator, external analysers through sigrok-cli, or VCD/CSV/.sr files. The main area is a canvas viewer that asks the server only for what is visible (edges or per-pixel summaries), with cursors, buses, search, protocol decoders and a results table.
import { h, get, post, put, del, upload, toast, downloadUrl, fmtHz } from './api.js';

const LS = (k, d) => { try { const v = localStorage.getItem('cw.la.' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } };
const SAVE = (k, v) => { try { localStorage.setItem('cw.la.' + k, JSON.stringify(v)); } catch (e) { /* storage unavailable */ } };
const errMsg = (e) => String(e.message || e).replace(/^Unsupported: /, '');
const LABEL_W = 176;
const ROW_H = { ch: 30, dec: 24, bus: 28, an: 76 };
const I = {
  zin: '<svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M16 16l5 5M8 11h6M11 8v6"/></svg>',
  zout: '<svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M16 16l5 5M8 11h6"/></svg>',
  fit: '<svg class="i" viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>',
  cur: '<svg class="i" viewBox="0 0 24 24"><path d="M8 3v18M16 3v18"/></svg>',
  x: '<svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  prev: '<svg class="i" viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg>',
  next: '<svg class="i" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>',
  eye: '<svg class="i" viewBox="0 0 24 24"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  up: '<svg class="i" viewBox="0 0 24 24"><path d="M6 15l6-6 6 6"/></svg>',
  down: '<svg class="i" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>',
  dl: '<svg class="i" viewBox="0 0 24 24"><path d="M12 3v12M7 10l5 5 5-5M4 21h16"/></svg>',
  play: '<svg class="i" viewBox="0 0 24 24"><polygon points="7,4 20,12 7,20" fill="currentColor"/></svg>',
  repeat: '<svg class="i" viewBox="0 0 24 24"><path d="M17 2l4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 0 1-3 3H3"/></svg>',
  stop: '<svg class="i" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor"/></svg>',
  table: '<svg class="i" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18M9 4v16"/></svg>',
  wave: '<svg class="i" viewBox="0 0 24 24"><path d="M2 12h4l3-8 4 16 3-8h6"/></svg>',
};
const ib = (cls, icon, label, onclick, title) => h('button', { class: 'btn ' + cls, onclick, title: title || label, html: I[icon] + (label ? `<span>${label}</span>` : '') });
const nice = (x) => { const p = Math.pow(10, Math.floor(Math.log10(x))); const m = x / p; return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p; };
const UNITS = [[1, 's'], [1e-3, 'ms'], [1e-6, 'µs'], [1e-9, 'ns'], [1e-12, 'ps']];
function unitFor(x) { const a = Math.abs(x); for (const u of UNITS) if (a >= u[0] * 0.9999) return u; return UNITS[UNITS.length - 1]; }
/** Seconds with a unit picked from the magnitude (or from ``ref``) and enough digits. */
export function fmtSec(t, ref, digits) {
  if (t == null || !isFinite(t)) return '-';
  const [scale, name] = unitFor(ref != null ? ref : t || 1e-9);
  const v = t / scale;
  const d = digits != null ? digits : Math.abs(v) >= 100 ? 2 : Math.abs(v) >= 10 ? 3 : 4;
  return `${v.toFixed(d)} ${name}`;
}
const fmtF = (f) => (f == null || !isFinite(f) ? '-' : f >= 1e6 ? (f / 1e6).toFixed(4) + ' MHz' : f >= 1e3 ? (f / 1e3).toFixed(4) + ' kHz' : f.toFixed(3) + ' Hz');
const KIND_TOKEN = { data: '--info', addr: '--warn', cmd: '--accent', ack: '--ok', nack: '--err', error: '--err', warn: '--warn', start: '--c-cursor-b', stop: '--c-cursor-b', state: '--muted', info: '--fg-2', agg: '--muted' };

export function initLogic(ctx, sideEl, viewEl) {
  // ================= state =================
  let cap = null;            // summary of the current capture
  let data = null;           // last /api/la/view answer
  let view = { a: 0, b: 1 }; // visible sample range
  let srcInfo = null;        // /api/la/sources
  let decoders = [];
  let buses = [];
  const cursors = { A: null, B: null };
  let hover = null;          // { x, y, s }
  let axisSamples = LS('axisSamples', false);
  let repeat = false, running = false;
  let inflight = false, pending = false, rafQ = false, seq = 0;
  let layout = [];
  let colors = {};
  let segBounds = [];

  // ================= main view DOM =================
  const capSel = h('select', { title: 'captures kept in memory (newest last)', onchange: () => put('/api/la/current', { id: capSel.value }).then(loadCapture).catch((e) => toast(errMsg(e), 'err')) });
  const axisBtn = h('button', { class: 'btn sm' + (axisSamples ? ' active' : ''), title: 'Time axis in seconds or in sample indices', onclick: () => { axisSamples = !axisSamples; axisBtn.classList.toggle('active', axisSamples); SAVE('axisSamples', axisSamples); draw(); } }, 'Samples');
  const resultsBtn = ib('sm', 'table', 'Results', () => toggleResults(), 'Decoded results table');
  const toolbar = h('div', { class: 'toolbar la-toolbar' },
    h('label', {}, 'Capture', capSel),
    h('span', { class: 'sep' }),
    ib('sm', 'zin', '', () => zoom(0.5), 'Zoom in (+, wheel)'), ib('sm', 'zout', '', () => zoom(2), 'Zoom out (-)'), ib('sm', 'fit', 'Fit', fit, 'Show the whole capture (F)'),
    ib('sm', 'cur', 'A..B', zoomCursors, 'Zoom to the cursors'), ib('sm', 'x', 'Cursors', () => { cursors.A = cursors.B = null; cursorsChanged(); }, 'Clear the cursors'),
    axisBtn,
    h('span', { class: 'spacer' }),
    resultsBtn);
  // search bar
  const sMode = h('select', { onchange: () => paintSearch() }, h('option', { value: 'edge' }, 'Edge'), h('option', { value: 'pattern' }, 'Pattern'), h('option', { value: 'decoded' }, 'Decoded value'));
  const sChan = h('select', { title: 'channel' });
  const sEdge = h('select', { title: 'edge' }, ...['any', 'rising', 'falling'].map((x) => h('option', { value: x }, x)));
  const sPat = h('input', { class: 'mono', placeholder: '1X0 over the shown channels', style: 'width:150px', title: 'One character per shown channel, top first: 0, 1 or X (any)' });
  const sEdgeCh = h('select', { title: 'only at an edge of this channel' });
  const sText = h('input', { placeholder: 'e.g. 0x41, NACK, r 69', style: 'width:160px' });
  const sDec = h('select', { title: 'decoder' });
  const sInfo = h('span', { class: 'muted' });
  const searchBar = h('div', { class: 'toolbar la-search' }, h('label', {}, 'Find', sMode), sChan, sPat, sEdgeCh, sEdge, sText, sDec,
    ib('sm', 'prev', '', () => search(-1), 'Previous match (from cursor A or the view)'), ib('sm', 'next', '', () => search(1), 'Next match'), sInfo,
    h('span', { class: 'spacer' }),
    h('span', { class: 'muted la-hint', title: 'Keys: + and - zoom, arrows pan, F fits, A and B put a cursor at the mouse, Esc stops a capture' }, 'wheel: zoom · drag: pan · drag on the axis: zoom to range · click: cursor A · right-click: cursor B'));
  function paintSearch() {
    const m = sMode.value;
    sChan.style.display = m === 'edge' ? '' : 'none';
    sPat.style.display = m === 'pattern' ? '' : 'none';
    sEdgeCh.style.display = m === 'pattern' ? '' : 'none';
    sEdge.style.display = m === 'decoded' ? 'none' : '';
    sText.style.display = sDec.style.display = m === 'decoded' ? '' : 'none';
  }
  // plot area
  const axisCanvas = h('canvas', { class: 'la-axis-canvas' });
  const corner = h('div', { class: 'la-corner' });
  const labelsEl = h('div', { class: 'la-labels' });
  const plot = h('canvas', { id: 'la-plot', class: 'la-plot' });
  const plotWrap = h('div', { class: 'la-plotwrap' }, plot);
  const scroll = h('div', { class: 'la-scroll' }, h('div', { class: 'la-grid' }, labelsEl, plotWrap));
  const thumb = h('div', { class: 'la-thumb' });
  const hbar = h('div', { class: 'la-hscroll', title: 'drag to pan' }, thumb);
  const tip = h('div', { class: 'la-tip', style: 'display:none' });
  const empty = h('div', { class: 'overlay-msg la-empty' }, h('span', { html: '<svg viewBox="0 0 24 24"><path d="M2 17h3v-5h3v5h3V7h3v10h3v-4h5"/></svg>' }), h('b', {}, 'No logic capture yet'), h('span', {}, 'Pick a source on the left and press Capture, or import a VCD, CSV or sigrok .sr file.'));
  const body = h('div', { class: 'la-body' }, h('div', { class: 'la-axis' }, corner, axisCanvas), scroll, hbar, tip, empty);
  const footer = h('div', { id: 'la-footer', class: 'la-footer' });
  // results
  const rDec = h('select', { onchange: () => loadResults(true) });
  const rFilter = h('input', { class: 'small', placeholder: 'filter (text or 0x..)', oninput: () => { clearTimeout(rFilter.t); rFilter.t = setTimeout(() => loadResults(true), 250); } });
  const rCount = h('span', { class: 'muted' });
  const rBody = h('tbody', { id: 'la-results-body' });
  const rTable = h('div', { class: 'la-rtable' }, h('table', { class: 'tbl' }, h('thead', {}, h('tr', {}, h('th', {}, 'Time'), h('th', {}, 'Decoder'), h('th', {}, 'Row'), h('th', {}, 'Value'))), rBody));
  const results = h('div', { class: 'la-results' + (LS('results', true) ? '' : ' collapsed') },
    h('div', { class: 'toolbar' }, h('b', {}, 'Decoded'), rDec, rFilter, rCount, h('span', { class: 'spacer' }), ib('sm', 'dl', 'CSV', () => downloadUrl(`/api/la/annotations.csv?decoder=${encodeURIComponent(rDec.value || 'all')}&q=${encodeURIComponent(rFilter.value)}`), 'Export the decoded results as CSV')),
    rTable);
  viewEl.append(toolbar, searchBar, body, footer, results);
  paintSearch();

  // ================= sidebar =================
  const srcSel = h('select', { id: 'la-source', class: 'flex', onchange: () => { SAVE('source', srcSel.value); paintSource(); } });
  const srcReason = h('div', { class: 'iface-reason', style: 'display:none' });
  const srcForm = h('div');
  const srcInfoLine = h('div', { class: 'help la-rate' });
  const capBtn = ib('primary', 'play', 'Capture', () => startCapture(false), 'Capture once');
  capBtn.id = 'la-capture';
  const repBtn = ib('', 'repeat', 'Repeat', () => startCapture(true), 'Capture again and again until Stop');
  const stopBtn = ib('danger', 'stop', 'Stop', stopCapture, 'Stop the capture (Esc)');
  const persistChk = h('input', { type: 'checkbox', checked: LS('persist', false), onchange: () => SAVE('persist', persistChk.checked) });
  const status = h('div', { class: 'help' });
  const fileIn = h('input', { type: 'file', accept: '.vcd,.csv,.sr', style: 'display:none', onchange: importFile });
  const chList = h('div', { class: 'la-chlist' });
  const busList = h('div');
  const decList = h('div', { id: 'la-decoders' });
  const decForm = h('div', { class: 'la-decform' });
  const decType = h('select', { id: 'la-dec-type', class: 'flex', onchange: () => paintDecForm() });
  const measCh = h('select', { id: 'la-meas-ch', class: 'flex', onchange: () => measure() });
  const measRange = h('select', { onchange: () => measure() }, h('option', { value: 'cursors' }, 'between cursors'), h('option', { value: 'view' }, 'visible range'), h('option', { value: 'all' }, 'whole capture'));
  const measOut = h('div', { id: 'la-measure', class: 'kv la-meas' });
  const filesEl = h('div', { class: 'la-files' });
  sideEl.append(
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'Capture'), h('div', { class: 'end' }, h('span', { class: 'badge', id: 'la-run-badge' }, 'idle'))),
      h('div', { class: 'row' }, h('label', {}, 'Source'), srcSel), srcReason, srcForm, srcInfoLine,
      h('div', { class: 'row', style: 'margin-top:10px' }, capBtn, repBtn, stopBtn),
      h('div', { class: 'row' }, h('label', {}, persistChk, ' keep a copy on disk (.sr)')), status),
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'Channels'), h('span', { class: 'sub' }, 'double-click a row name in the view to rename, drag to reorder')),
      chList,
      h('div', { class: 'row', style: 'margin-top:8px' }, h('button', { class: 'btn sm', onclick: addTraceRow, title: 'Show the newest stored power trace as an analog row on this time base' }, 'Add stored trace'), h('button', { class: 'btn sm', onclick: addBus }, 'Add bus')),
      busList),
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'Decoders'), h('span', { class: 'sub' }, 'run in software on any capture')),
      decList, h('div', { class: 'row', style: 'margin-top:8px' }, h('label', {}, 'Add'), decType), decForm),
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'Measure')),
      h('div', { class: 'row' }, measCh, measRange), measOut),
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'Files'), h('span', { class: 'sub' }, 'VCD, CSV (Saleae or generic), sigrok .sr')),
      h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: () => fileIn.click() }, 'Import file'), fileIn,
        h('button', { class: 'btn sm', onclick: () => exportAs('vcd') }, 'VCD'), h('button', { class: 'btn sm', onclick: () => exportAs('csv') }, 'CSV'), h('button', { class: 'btn sm', onclick: () => exportAs('sr') }, '.sr')),
      filesEl));

  // ================= capture sources =================
  const fields = {}; // per source: {key: element}
  const saved = LS('settings', {});
  function num(id, label, value, attrs = {}) { const el = h('input', Object.assign({ type: 'number', value, style: 'width:110px' }, attrs)); return [id, label, el]; }
  function sel(id, label, values, value, labels) { const el = h('select', {}, ...values.map((v, i) => h('option', { value: v }, labels ? labels[i] : String(v)))); el.value = value; return [id, label, el]; }
  function chk(id, label, value) { const el = h('input', { type: 'checkbox', checked: !!value }); return [id, label, el]; }
  function txt(id, label, value, attrs = {}) { const el = h('input', Object.assign({ value: value == null ? '' : value, style: 'width:150px' }, attrs)); return [id, label, el]; }
  function val(el) { return el.getValue ? el.getValue() : el.type === 'checkbox' ? el.checked : el.value; }
  function chanPicker(id, label, all, chosen) {
    const boxes = all.map((n) => h('label', {}, h('input', { type: 'checkbox', value: n, checked: !chosen || chosen.includes(n) }), ' ' + n));
    const sum = h('summary', {});
    const el = h('details', { class: 'la-chanpick' }, sum, h('div', { class: 'la-boxes' }, ...boxes));
    el.getValue = () => { const v = boxes.map((b) => b.firstChild).filter((c) => c.checked).map((c) => c.value); return v.length === all.length ? '' : v; };
    const paint = () => { const v = el.getValue(); sum.textContent = v === '' ? `all ${all.length}` : `${v.length} of ${all.length}`; };
    el.addEventListener('change', paint); paint();
    return [id, label, el];
  }

  function buildForms() {
    const s = (src, k, d) => (saved[src] && saved[src][k] !== undefined ? saved[src][k] : d);
    const la = (srcInfo && srcInfo.scope && srcInfo.scope.la) || {};
    const trig = srcInfo ? srcInfo.la_triggers : ['capture', 'manual'];
    const maxDepth = la.max_depth || 16376;
    fields.native = [
      sel('group', 'Group', ['CW 20-pin', 'USERIO 20-pin', 'glitch'], s('native', 'group', 'CW 20-pin'), ['CW 20-pin: IO1-4, HS1, HS2, AUX, TRIG, ADC clock', 'USERIO 20-pin: D0-D7, CK', 'glitch internals']),
      sel('clk_source', 'Clock', ['usb', 'target', 'pll'], s('native', 'clk_source', 'usb'), ['USB 96 MHz', 'target (HS1/AUX)', 'Husky PLL']),
      num('oversampling', 'Oversampling', s('native', 'oversampling', 1), { min: 0.25, step: 0.25 }),
      num('downsample', 'Downsample', s('native', 'downsample', 96), { min: 1, max: 65536 }),
      num('depth', 'Depth', s('native', 'depth', maxDepth), { min: 2, max: maxDepth, step: 2 }),
      sel('trigger', 'Trigger', trig, s('native', 'trigger', 'capture')),
      sel('fire', 'Fire target', ['simpleserial', 'none'], s('native', 'fire', 'simpleserial'), ['send a SimpleSerial command', 'no (wait for the trigger)']),
      chk('with_analog', 'Also capture the ADC trace', s('native', 'with_analog', false)),
      num('timeout', 'Timeout (s)', s('native', 'timeout', 5), { min: 0.1, step: 0.5 }),
    ];
    fields.adc = [
      num('segments', 'Segments', s('adc', 'segments', 20), { min: 1, max: 2000 }),
      num('samples', 'Samples each', s('adc', 'samples', (srcInfo && srcInfo.scope && srcInfo.scope.adc_samples) || 5000), { min: 16 }),
      txt('level', 'Threshold', s('adc', 'level', 'auto'), { title: 'auto, or a level in the ADC range (-0.5..0.5)' }),
      txt('hysteresis', 'Hysteresis', s('adc', 'hysteresis', 'auto'), { title: 'auto, or the band width around the threshold' }),
      sel('fire', 'Fire target', ['none', 'simpleserial'], s('adc', 'fire', 'none'), ['no (wait for the scope trigger)', 'send a SimpleSerial command']),
      chk('invert', 'Invert', s('adc', 'invert', false)),
    ];
    if (ctx.status && ctx.status.scope && ctx.status.scope.connected && srcInfo && (srcInfo.sources.find((x) => x.id === 'sim') || {}).available) fields.adc.push(sel('sim_signal', 'Simulated line', srcInfo.sim_channels, s('adc', 'sim_signal', 'UART TX')));
    fields.sim = [
      sel('samplerate', 'Sample rate', [1e6, 2e6, 4e6, 8e6, 16e6, 25e6, 50e6, 100e6], s('sim', 'samplerate', 4e6), ['1 MHz', '2 MHz', '4 MHz', '8 MHz', '16 MHz', '25 MHz', '50 MHz', '100 MHz']),
      num('duration_ms', 'Duration (ms)', s('sim', 'duration_ms', 25), { min: 0.01, step: 1 }),
      num('pretrigger', 'Pre-trigger (%)', s('sim', 'pretrigger', 40), { min: 0, max: 100 }),
      num('jitter_ns', 'Edge jitter (ns)', s('sim', 'jitter_ns', 0), { min: 0 }),
      num('glitches_per_ms', 'Noise spikes per ms', s('sim', 'glitches_per_ms', 0), { min: 0, step: 0.5 }),
      sel('fire', 'Plaintext', ['none', 'simpleserial'], s('sim', 'fire', 'none'), ['fixed demo plaintext', 'send one to the simulated target']),
      chanPicker('channels', 'Channels', (srcInfo && srcInfo.sim_channels) || [], s('sim', 'channels', '') || null),
    ];
    const devs = (srcInfo && srcInfo.sigrokDevices) || [];
    const devSel = h('select', { style: 'width:150px' }, h('option', { value: '' }, devs.length ? 'choose a device' : 'press Scan'), ...devs.map((d) => h('option', { value: d.id }, `${d.id} (${d.description})`)));
    devSel.value = s('sigrok', 'device', '');
    fields.sigrok = [
      ['device', 'Device', devSel],
      num('samplerate', 'Sample rate (Hz)', s('sigrok', 'samplerate', 1000000), { min: 1 }),
      txt('channels', 'Channels', s('sigrok', 'channels', ''), { placeholder: 'D0,D1,D2 (empty: all)' }),
      num('samples', 'Samples', s('sigrok', 'samples', 100000), { min: 1 }),
      txt('triggers', 'Trigger', s('sigrok', 'triggers', ''), { placeholder: 'D0=r,D1=1' }),
      txt('config', 'Extra config', s('sigrok', 'config', ''), { placeholder: 'e.g. voltage_threshold=1.5-1.5' }),
      num('timeout', 'Timeout (s)', s('sigrok', 'timeout', 60), { min: 1 }),
    ];
    fields.files = [];
  }

  function paintSource() {
    if (!srcInfo) return;
    const id = srcSel.value;
    const entry = srcInfo.sources.find((x) => x.id === id) || {};
    const ok = !!entry.available;
    srcReason.style.display = ok ? 'none' : '';
    srcReason.textContent = ok ? '' : entry.reason || 'not available';
    srcForm.innerHTML = '';
    for (const [key, label, el] of fields[id] || []) {
      el.onchange = el.oninput = () => { saveSettings(); paintRate(); };
      srcForm.append(el.type === 'checkbox' ? h('div', { class: 'row' }, h('label', {}, ''), h('label', {}, el, ' ' + label)) : h('div', { class: 'row' }, h('label', {}, label), el));
      el.disabled = !ok;
    }
    if (id === 'sigrok') {
      if (ok) srcForm.append(h('div', { class: 'row' }, h('label', {}, ''), h('button', { class: 'btn sm', onclick: scanSigrok }, 'Scan for analysers'), h('span', { class: 'muted' }, `sigrok-cli ${entry.version || ''}`)));
      else if (entry.install) {
        srcForm.append(h('div', { class: 'help' }, entry.install.summary), h('ul', { class: 'la-install' }, ...(entry.install.steps || []).map((x) => h('li', {}, h('code', {}, x)))),
          entry.install.note ? h('div', { class: 'help' }, entry.install.note) : null, h('div', { class: 'help' }, 'Then restart Studio, or set CWSTUDIO_SIGROK_CLI to the sigrok-cli path.'));
      }
    }
    if (id === 'files') srcForm.append(h('div', { class: 'help' }, 'Import a capture from any analyser (Saleae Logic CSV export, PulseView .sr, VCD from simulators and other tools) with Import file below.'));
    const capable = ok && id !== 'files';
    capBtn.disabled = repBtn.disabled = !capable || running;
    paintRate();
  }
  function settingsOf(id) { const o = {}; for (const [key, , el] of fields[id] || []) o[key] = val(el); return o; }
  function saveSettings() { const id = srcSel.value; saved[id] = settingsOf(id); SAVE('settings', saved); }
  function paintRate() {
    const id = srcSel.value, s = settingsOf(id), sc = (srcInfo && srcInfo.scope) || {};
    let t = '';
    if (id === 'native' && sc.la) {
      const base = (sc.la.sources || {})[s.clk_source] || 0;
      const sr = (base * (+s.oversampling || 1)) / Math.max(1, +s.downsample || 1);
      if (sr) t = `${fmtHz(sr)} sampling · ${s.depth} samples = ${fmtSec(s.depth / sr)} after the trigger (no pre-trigger)`;
      if (base * (+s.oversampling || 1) > 250e6) t += ' · above 250 MHz';
      if (s.with_analog && s.trigger !== 'capture') t += ' · the ADC trace needs trigger "capture"';
    } else if (id === 'adc' && sc.adc_rate) {
      const n = (+s.samples || 0) * (+s.segments || 1);
      t = `${fmtHz(sc.adc_rate)} (scope ADC clock) · ${n} samples = ${fmtSec(n / sc.adc_rate)}${+s.segments > 1 ? ' in ' + s.segments + ' triggers' : ''}`;
    } else if (id === 'sim') {
      const n = Math.round((+s.duration_ms / 1e3) * +s.samplerate);
      t = `${fmtHz(+s.samplerate)} · ${n.toLocaleString()} samples, ${s.pretrigger}% before the trigger`;
    } else if (id === 'sigrok' && +s.samplerate) {
      t = `${fmtHz(+s.samplerate)} · ${(+s.samples || 0).toLocaleString()} samples = ${fmtSec((+s.samples || 0) / +s.samplerate)}`;
    }
    srcInfoLine.textContent = t;
  }
  async function loadSources() {
    try {
      const prev = srcSel.value || LS('source', null);
      const devs = srcInfo && srcInfo.sigrokDevices;
      srcInfo = await get('/api/la/sources');
      if (devs) srcInfo.sigrokDevices = devs;
      buildForms();
      srcSel.innerHTML = '';
      for (const s of srcInfo.sources) {
        const o = h('option', { value: s.id }, s.label + (s.available ? '' : ' (not available)'));
        if (!s.available) o.title = s.reason || '';
        srcSel.append(o);
      }
      const firstOk = (srcInfo.sources.find((x) => x.available && x.id !== 'files') || srcInfo.sources[0]).id;
      const keep = srcInfo.sources.find((x) => x.id === prev && x.available);
      srcSel.value = keep ? prev : firstOk;
      paintSource();
      paintDecTypes();
    } catch (e) { status.textContent = errMsg(e); }
  }
  async function scanSigrok() {
    status.textContent = 'scanning with sigrok-cli...';
    try { const r = await get('/api/la/sigrok/scan'); srcInfo.sigrokDevices = r.devices; buildForms(); paintSource(); status.textContent = r.devices.length ? `${r.devices.length} device(s) found` : 'no analyser found'; } catch (e) { status.textContent = errMsg(e); }
  }
  function setRunning(on, phase) {
    running = on;
    const b = document.getElementById('la-run-badge');
    if (b) { b.className = 'badge ' + (on ? 'warn' : ''); b.textContent = on ? (phase || 'capturing') : repeat ? 'repeat' : 'idle'; }
    stopBtn.disabled = !on && !repeat;
    repBtn.classList.toggle('active', repeat);
    paintSource();
  }
  async function startCapture(rep) {
    if (rep) repeat = !repeat ? true : repeat;
    if (running) return;
    const source = srcSel.value;
    saveSettings();
    const settings = settingsOf(source);
    for (const k of Object.keys(settings)) if (settings[k] === '') delete settings[k];
    if (source === 'sigrok' && settings.channels) settings.channels = settings.channels.split(',').map((x) => x.trim()).filter(Boolean);
    if (Array.isArray(settings.channels) && !settings.channels.length) { toast('choose at least one channel', 'warn'); return; }
    settings.persist = persistChk.checked;
    status.textContent = '';
    setRunning(true, 'starting');
    try { await post('/api/la/capture', { source, settings }); } catch (e) { setRunning(false); repeat = false; status.innerHTML = ''; status.append(h('span', { class: 'err' }, errMsg(e))); setRunning(false); }
  }
  async function stopCapture() { repeat = false; try { await post('/api/la/stop'); } catch (e) { /* ignore */ } setRunning(running); }
  ctx.on('la', (ev) => {
    if (ev.kind === 'job') {
      if (ev.state === 'running') { setRunning(true, ev.phase); return; }
      setRunning(false);
      if (ev.state === 'error') { repeat = false; status.innerHTML = ''; status.append(h('span', { class: 'err' }, errMsg(ev.error || 'capture failed'))); setRunning(false); }
      else if (ev.state === 'stopped') status.textContent = 'stopped';
      else status.textContent = (ev.warnings && ev.warnings.length ? ev.warnings.join('; ') : '') || 'done';
      if (ev.capture) loadCapture(true);
      if (repeat && ev.state === 'done') setTimeout(() => { if (repeat) startCapture(false); }, 120);
      return;
    }
    if (ev.kind === 'capture' || ev.kind === 'select') { if (!running) loadCapture(true); return; }
    if (ev.kind === 'channels' || ev.kind === 'decoders') loadStatus();
    if (ev.kind === 'decoded' && cap && ev.capture === cap.id) { requestView(); loadResults(true); }
  });

  // ================= capture, channels and decoders state =================
  async function loadStatus() {
    try {
      const st = await get('/api/la');
      decoders = st.decoders; buses = st.buses || [];
      capSel.innerHTML = '';
      st.captures.forEach((c) => capSel.append(h('option', { value: c.id }, `${c.name} · ${fmtSec(c.duration)}`)));
      if (st.current) capSel.value = st.current;
      if (st.capture && (!cap || cap.id !== st.capture.id)) { setCapture(st.capture, true); }
      else if (st.capture) { cap = st.capture; afterCapture(); }
      paintDecoders();
      paintBuses();
      if (st.running) setRunning(true, st.job && st.job.phase);
    } catch (e) { /* server down */ }
  }
  async function loadCapture(fitView) {
    try { const st = await get('/api/la'); decoders = st.decoders; buses = st.buses || []; capSel.innerHTML = ''; st.captures.forEach((c) => capSel.append(h('option', { value: c.id }, `${c.name} · ${fmtSec(c.duration)}`))); if (st.current) capSel.value = st.current; if (st.capture) setCapture(st.capture, fitView !== false && (!cap || cap.id !== st.capture.id || fitView === true)); paintDecoders(); paintBuses(); } catch (e) { /* ignore */ }
  }
  function setCapture(c, fitView) {
    const same = cap && cap.id === c.id;
    cap = c;
    segBounds = (c.meta && c.meta.segment_bounds) || [];
    if (fitView || !same) { const keep = same ? null : LS('view.' + c.source, null); view = { a: 0, b: c.samples }; if (keep && keep.n === c.samples && keep.b <= c.samples) view = { a: keep.a, b: keep.b }; }
    if (!same) { cursors.A = cursors.B = null; data = null; }
    afterCapture();
    loadResults(true);
  }
  function afterCapture() {
    empty.style.display = cap ? 'none' : '';
    paintChannels();
    const opts = cap ? cap.order.map((i) => h('option', { value: i }, cap.channels[i].name)) : [];
    for (const s of [sChan, measCh]) { const v = s.value; s.innerHTML = ''; opts.forEach((o) => s.append(o.cloneNode(true))); if (v && [...s.options].some((o) => o.value === v)) s.value = v; }
    const v2 = sEdgeCh.value; sEdgeCh.innerHTML = ''; sEdgeCh.append(h('option', { value: '' }, 'at any sample'), ...opts.map((o) => o.cloneNode(true))); sEdgeCh.value = v2 || '';
    paintDecForm();
    requestView();
    measure();
  }

  function chName(i) { return cap && cap.channels[i] ? cap.channels[i].name : `#${i}`; }
  function paintChannels() {
    chList.innerHTML = '';
    if (!cap) { chList.append(h('div', { class: 'help' }, 'No capture.')); return; }
    const order = cap.order;
    order.forEach((ci, pos) => {
      const c = cap.channels[ci];
      const color = h('input', { type: 'color', value: c.color || '#5aa2ff', title: 'colour', onchange: () => setCh({ index: ci, color: color.value }) });
      const name = h('input', { class: 'flex small', value: c.name, onchange: () => setCh({ index: ci, name: name.value }) });
      const hide = h('button', { class: 'icon-btn sm' + (c.hidden ? ' off' : ''), title: c.hidden ? 'show' : 'hide', html: I.eye, onclick: () => setCh({ index: ci, hidden: !c.hidden }) });
      chList.append(h('div', { class: 'la-chrow' + (c.hidden ? ' hidden' : '') }, color, name, h('span', { class: 'muted mono la-edges', title: 'edges' }, String(c.edges)), hide,
        h('button', { class: 'icon-btn sm', title: 'move up', html: I.up, disabled: pos === 0, onclick: () => move(pos, -1) }), h('button', { class: 'icon-btn sm', title: 'move down', html: I.down, disabled: pos === order.length - 1, onclick: () => move(pos, 1) })));
    });
    cap.analog.forEach((a) => {
      chList.append(h('div', { class: 'la-chrow analog' + (a.hidden ? ' hidden' : '') }, h('span', { class: 'la-swatch', style: `background:${a.color}` }), h('span', { class: 'flex' }, `${a.name} (analog, ${fmtHz(a.samplerate)})`),
        h('button', { class: 'icon-btn sm', title: 'open in the waveform view', html: I.wave, onclick: () => post('/api/la/analog/to_waveform', { index: a.index }).then((r) => { toast(`Added as trace #${r.index}`, 'ok'); ctx.showTab('capture'); }).catch((e) => toast(errMsg(e), 'err')) }),
        h('button', { class: 'icon-btn sm', title: a.hidden ? 'show' : 'hide', html: I.eye, onclick: () => put('/api/la/channels', { analog: [{ index: a.index, hidden: !a.hidden }] }).then(refreshAfterEdit) }),
        h('button', { class: 'icon-btn sm', title: 'remove', html: I.x, onclick: () => put('/api/la/channels', { analog: [{ index: a.index, remove: true }] }).then(refreshAfterEdit) })));
    });
  }
  async function setCh(upd) { try { await put('/api/la/channels', { channels: [upd] }); await refreshAfterEdit(); } catch (e) { toast(errMsg(e), 'err'); } }
  async function move(pos, d) { const o = cap.order.slice(); const [x] = o.splice(pos, 1); o.splice(pos + d, 0, x); try { await put('/api/la/channels', { order: o }); await refreshAfterEdit(); } catch (e) { toast(errMsg(e), 'err'); } }
  async function refreshAfterEdit() { try { const st = await get('/api/la'); if (st.capture) { cap = st.capture; buses = st.buses || []; decoders = st.decoders; afterCapture(); paintBuses(); paintDecoders(); } } catch (e) { /* ignore */ } }
  async function addTraceRow() { try { await post('/api/la/analog/from_trace', { index: -1 }); await loadCapture(false); } catch (e) { toast(errMsg(e), 'err', 6000); } }

  // ----- buses -----
  function paintBuses() {
    busList.innerHTML = '';
    buses.forEach((b, k) => busList.append(h('div', { class: 'la-chrow' }, h('span', { class: 'flex' }, h('b', {}, b.name), ' ', h('span', { class: 'muted' }, `${b.channels.map(chName).join(', ')} (${b.format})`)),
      h('button', { class: 'icon-btn sm', title: 'remove the bus', html: I.x, onclick: () => saveBuses(buses.filter((_, i) => i !== k)) }))));
  }
  async function saveBuses(list) { try { const r = await put('/api/la/channels', { buses: list }); buses = r.buses; paintBuses(); requestView(); } catch (e) { toast(errMsg(e), 'err'); } }
  function addBus() {
    if (!cap) return;
    const name = h('input', { class: 'small', value: `bus${buses.length}`, style: 'width:90px' });
    const fmt = h('select', {}, ...['hex', 'dec', 'bin'].map((x) => h('option', { value: x }, x)));
    const boxes = cap.order.map((ci) => { const c = h('input', { type: 'checkbox', value: ci }); return h('label', {}, c, ' ' + chName(ci)); });
    const form = h('div', { class: 'la-busform' }, h('div', { class: 'row' }, h('label', {}, 'Name'), name, fmt), h('div', { class: 'help' }, 'Channels, most significant first (top to bottom):'), h('div', { class: 'la-boxes' }, ...boxes),
      h('div', { class: 'row' }, h('button', { class: 'btn sm primary', onclick: () => { const chans = boxes.map((l) => l.firstChild).filter((c) => c.checked).map((c) => +c.value); if (!chans.length) { toast('choose channels', 'warn'); return; } form.remove(); saveBuses(buses.concat([{ name: name.value, channels: chans, format: fmt.value }])); } }, 'Add'), h('button', { class: 'btn sm ghost', onclick: () => form.remove() }, 'Cancel')));
    busList.append(form);
  }

  // ----- decoders -----
  function paintDecTypes() {
    decType.innerHTML = '';
    decType.append(h('option', { value: '' }, 'choose a decoder...'));
    const reg = (srcInfo && srcInfo.decoders) || {};
    for (const [k, d] of Object.entries(reg)) decType.append(h('option', { value: k }, d.label));
    const sg = srcInfo && srcInfo.sigrok;
    const o = h('option', { value: 'sigrok' }, 'sigrok decoder (sigrok-cli)' + (sg && sg.available ? '' : ' (not installed)'));
    if (!(sg && sg.available)) { o.disabled = true; o.title = sg ? sg.reason : ''; }
    decType.append(o);
  }
  function guess(kind) {
    const spec = srcInfo.decoders[kind]; const out = {};
    if (!cap) return out;
    const names = cap.channels.map((c) => c.name.toLowerCase()); const used = new Set();
    for (const ch of spec.channels) for (const hnt of ch.hint || []) {
      let hit = names.findIndex((n, i) => n === hnt && !used.has(i));
      if (hit < 0) hit = names.findIndex((n, i) => n.includes(hnt) && !used.has(i));
      if (hit >= 0) { out[ch.id] = hit; used.add(hit); break; }
    }
    return out;
  }
  function paintDecForm(existing) {
    decForm.innerHTML = '';
    const kind = existing ? existing.type : decType.value;
    if (!kind) return;
    const chans = {}, opts = {};
    if (kind === 'sigrok') {
      const spec = h('input', { class: 'flex mono', placeholder: 'uart:rx=D0:baudrate=115200', value: existing ? existing.options.spec || '' : '' });
      decForm.append(h('div', { class: 'row' }, h('label', {}, 'Decoder spec'), spec), h('div', { class: 'help' }, `sigrok-cli -P syntax; the channels are D0..D${cap ? cap.channels.length - 1 : 'n'} in capture order (${cap ? cap.channels.map((c, i) => `D${i} = ${c.name}`).join(', ') : ''}).`));
      opts.spec = spec;
    } else {
      const spec = srcInfo.decoders[kind];
      const g = existing ? Object.fromEntries(Object.entries(existing.channels).map(([k, v]) => [k, v.index])) : guess(kind);
      for (const ch of spec.channels) {
        const s = h('select', { class: 'flex' }, h('option', { value: '' }, ch.required ? 'choose...' : '(none)'), ...(cap ? cap.order.map((i) => h('option', { value: i }, cap.channels[i].name)) : []));
        if (g[ch.id] != null) s.value = g[ch.id];
        chans[ch.id] = s;
        decForm.append(h('div', { class: 'row' }, h('label', {}, ch.label + (ch.required ? '' : ' (optional)')), s));
      }
      for (const o of spec.options) {
        const cur = existing && existing.options[o.id] !== undefined ? existing.options[o.id] : o.default;
        let el;
        if (o.type === 'select') { el = h('select', {}, ...o.values.map((v) => h('option', { value: v }, String(v)))); el.value = cur; }
        else if (o.type === 'bool') el = h('input', { type: 'checkbox', checked: !!cur });
        else el = h('input', { type: o.type === 'number' ? 'number' : 'text', value: cur, style: 'width:110px', min: o.min, max: o.max });
        opts[o.id] = el;
        decForm.append(h('div', { class: 'row', title: o.help || '' }, h('label', {}, o.label), el));
      }
    }
    const go = h('button', { class: 'btn sm primary', onclick: async () => {
      const body = { type: kind, channels: {}, options: {} };
      for (const [k, s] of Object.entries(chans)) if (s.value !== '') body.channels[k] = +s.value;
      for (const [k, el] of Object.entries(opts)) body.options[k] = el.type === 'checkbox' ? el.checked : el.value;
      try {
        const r = existing ? await put(`/api/la/decoders/${existing.id}`, body) : await post('/api/la/decoders', body);
        if (r.error) toast(`${r.name}: ${r.error}`, 'warn', 6000);
        decType.value = ''; decForm.innerHTML = '';
        await refreshAfterEdit(); loadResults(true);
      } catch (e) { toast(errMsg(e), 'err', 6000); }
    } }, existing ? 'Apply' : 'Add decoder');
    decForm.append(h('div', { class: 'row' }, go, h('button', { class: 'btn sm ghost', onclick: () => { decType.value = ''; decForm.innerHTML = ''; } }, 'Cancel')));
  }
  function decSummary(d) {
    const dv = data && data.decoders ? data.decoders.find((x) => x.id === d.id) : null;
    if (dv && dv.error) return h('span', { class: 'err' }, dv.error);
    if (dv && dv.pending) return h('span', { class: 'muted' }, 'decoding...');
    const m = dv ? dv.meta : null;
    if (!m) return h('span', { class: 'muted' }, Object.entries(d.channels).map(([k, v]) => `${k}=${v.name || v.index}`).join(' '));
    const parts = [];
    if (m.baud) parts.push(`${Math.round(m.baud)} baud`);
    if (m.bitrate) parts.push(`${m.bitrate / 1000} kbit/s`);
    for (const k of ['tx_frames', 'rx_frames', 'words', 'transactions', 'frames', 'bytes', 'ir', 'dr', 'ok', 'tx_messages', 'rx_messages', 'resets']) if (m[k] != null) parts.push(`${k.replace('_', ' ')} ${m[k]}`);
    const errs = ['tx_errors', 'rx_errors', 'errors', 'nacks', 'fault', 'wait'].filter((k) => m[k]).map((k) => `${k.replace('_', ' ')} ${m[k]}`);
    return h('span', {}, h('span', { class: 'muted' }, parts.join(', ')), errs.length ? h('span', { class: 'warn' }, ' · ' + errs.join(', ')) : null);
  }
  function paintDecoders() {
    decList.innerHTML = '';
    if (!decoders.length) decList.append(h('div', { class: 'help' }, 'No decoders yet. Add UART, SPI, I2C, 1-Wire, JTAG, SWD, CAN or SimpleSerial; their results show under the channels and in the results table.'));
    decoders.forEach((d) => {
      const on = h('input', { type: 'checkbox', checked: d.enabled, title: 'show', onchange: () => put(`/api/la/decoders/${d.id}`, { enabled: on.checked }).then(refreshAfterEdit).then(() => loadResults(true)) });
      decList.append(h('div', { class: 'la-decrow' }, on, h('div', { class: 'flex' }, h('b', {}, d.name), ' ', decSummary(d)),
        h('button', { class: 'btn sm ghost', onclick: () => paintDecForm(d) }, 'Edit'),
        h('button', { class: 'icon-btn sm', title: 'remove', html: I.x, onclick: () => del(`/api/la/decoders/${d.id}`).then(refreshAfterEdit).then(() => loadResults(true)) })));
    });
    const v = rDec.value; rDec.innerHTML = ''; rDec.append(h('option', { value: 'all' }, 'all decoders'), ...decoders.map((d) => h('option', { value: d.id }, d.name))); rDec.value = [...rDec.options].some((o) => o.value === v) ? v : 'all';
    const v2 = sDec.value; sDec.innerHTML = ''; sDec.append(h('option', { value: 'all' }, 'any decoder'), ...decoders.map((d) => h('option', { value: d.id }, d.name))); sDec.value = [...sDec.options].some((o) => o.value === v2) ? v2 : 'all';
  }

  // ----- results table -----
  let rOffset = 0, rTotal = 0, rLoading = false;
  function toggleResults(on) { const show = on != null ? on : results.classList.contains('collapsed'); results.classList.toggle('collapsed', !show); resultsBtn.classList.toggle('active', show); SAVE('results', show); resize(); if (show) loadResults(true); }
  resultsBtn.classList.toggle('active', !results.classList.contains('collapsed'));
  async function loadResults(reset) {
    if (!cap || results.classList.contains('collapsed') || rLoading) return;
    if (reset) { rOffset = 0; rBody.innerHTML = ''; rTable.scrollTop = 0; }
    rLoading = true;
    try {
      const r = await get(`/api/la/annotations?decoder=${encodeURIComponent(rDec.value || 'all')}&q=${encodeURIComponent(rFilter.value)}&offset=${rOffset}&limit=200`);
      rTotal = r.total;
      rCount.textContent = `${r.total.toLocaleString()} ${r.total === 1 ? 'entry' : 'entries'}`;
      for (const it of r.items) {
        const tr = h('tr', { class: 'k-' + it.kind, onclick: () => goto(it.s, it.e) },
          h('td', { class: 'mono' }, axisSamples ? `#${it.s}` : fmtSec(it.t)), h('td', {}, it.decoder), h('td', {}, `${it.label}${it.channel_name ? ' (' + it.channel_name + ')' : ''}`), h('td', { class: 'mono' }, it.text));
        rBody.append(tr);
      }
      rOffset += r.items.length;
      if (!r.total && !decoders.length) rBody.append(h('tr', {}, h('td', { colspan: 4, class: 'muted' }, 'Add a decoder in the sidebar to see decoded values here.')));
    } catch (e) { rCount.textContent = errMsg(e); }
    rLoading = false;
  }
  rTable.addEventListener('scroll', () => { if (rTable.scrollTop + rTable.clientHeight > rTable.scrollHeight - 60 && rOffset < rTotal) loadResults(false); });

  // ================= measurements =================
  async function measure() {
    clearTimeout(measure.t);
    measure.t = setTimeout(async () => {
      if (!cap || measCh.value === '') { measOut.innerHTML = ''; return; }
      const body = { channel: +measCh.value };
      if (measRange.value === 'cursors' && cursors.A != null && cursors.B != null) { body.from = Math.min(cursors.A, cursors.B); body.to = Math.max(cursors.A, cursors.B); }
      else if (measRange.value === 'view' || (measRange.value === 'cursors')) { body.from = Math.max(0, view.a); body.to = Math.min(cap.samples, view.b); }
      try {
        const m = await post('/api/la/measure', body);
        const pw = (x) => (x ? `${fmtSec(x.min)} / ${fmtSec(x.avg)} / ${fmtSec(x.max)}` : '-');
        measOut.innerHTML = '';
        const kv = (k, v) => measOut.append(h('span', { class: 'k' }, k), h('span', {}, v));
        kv('Range', `${fmtSec(m.t_from)} .. ${fmtSec(m.t_to)}${measRange.value === 'cursors' && (cursors.A == null || cursors.B == null) ? ' (visible; place both cursors)' : ''}`);
        kv('Edges', `${m.edges} (${m.rising} rising, ${m.falling} falling)`);
        kv('Frequency', fmtF(m.frequency));
        kv('Period', m.period ? fmtSec(m.period.avg) : '-');
        kv('Duty cycle', m.duty == null ? '-' : (m.duty * 100).toFixed(2) + ' %');
        kv('High min/avg/max', pw(m.high));
        kv('Low min/avg/max', pw(m.low));
      } catch (e) { measOut.textContent = errMsg(e); }
    }, 150);
  }

  // ================= files =================
  async function importFile() {
    if (!fileIn.files.length) return;
    const f = fileIn.files[0];
    let fields_ = {};
    if (/\.csv$/i.test(f.name)) {
      const head = await f.slice(0, 200).text();
      if (!/time/i.test(head.split('\n')[0])) { const sr = prompt('Sample rate in Hz for this CSV (it has no time column)', '1000000'); if (sr === null) { fileIn.value = ''; return; } fields_ = { samplerate: sr }; }
    }
    status.textContent = `importing ${f.name}...`;
    try { await upload('/api/la/import/upload', f, fields_); status.textContent = `imported ${f.name}`; await loadCapture(true); loadFiles(); } catch (e) { status.textContent = ''; toast(errMsg(e), 'err', 8000); }
    fileIn.value = '';
  }
  function exportAs(fmt) { if (!cap) { toast('no capture to export', 'warn'); return; } downloadUrl(`/api/la/download/${fmt}`); setTimeout(loadFiles, 1500); }
  async function loadFiles() {
    try {
      const fs = await get('/api/la/files');
      filesEl.innerHTML = '';
      if (!fs.length) return;
      filesEl.append(h('div', { class: 'help' }, 'On disk (click to open):'), ...fs.slice(0, 8).map((f) => h('div', { class: 'la-file', title: f.path, onclick: () => post('/api/la/import', { path: f.path }).then(() => loadCapture(true)).catch((e) => toast(errMsg(e), 'err', 6000)) }, h('span', { class: 'flex' }, f.name), h('span', { class: 'muted' }, f.folder))));
    } catch (e) { /* ignore */ }
  }

  // ================= view geometry =================
  const plotW = () => Math.max(50, plotWrap.clientWidth);
  const xOf = (s) => ((s - view.a) / (view.b - view.a)) * plotW();
  const sOf = (x) => view.a + (x / plotW()) * (view.b - view.a);
  const tOf = (s) => (s - cap.trigger) / cap.samplerate;
  function clampView() {
    if (!cap) return;
    const n = cap.samples;
    let span = Math.max(Math.min(view.b - view.a, n * 1.0), Math.min(10, n));
    let a = view.a;
    if (a < 0) a = 0;
    if (a + span > n) a = Math.max(0, n - span);
    view = { a, b: a + span };
  }
  function setView(a, b) { view = { a, b }; clampView(); if (cap) SAVE('view.' + cap.source, { a: view.a, b: view.b, n: cap.samples }); draw(); requestView(); clearTimeout(setView.t); setView.t = setTimeout(() => { if (measRange.value !== 'all') measure(); }, 200); }
  function zoom(f, center) { if (!cap) return; const c = center != null ? center : (view.a + view.b) / 2; setView(c - (c - view.a) * f, c + (view.b - c) * f); }
  function fit() { if (cap) setView(0, cap.samples); }
  function zoomCursors() { if (cursors.A != null && cursors.B != null) { const a = Math.min(cursors.A, cursors.B), b = Math.max(cursors.A, cursors.B), m = (b - a) * 0.1 + 2; setView(a - m, b + m); } }
  function goto(s, e) {
    const span = view.b - view.a;
    const w = Math.max(e - s, 1);
    let ns = span;
    if (w > span * 0.8 || w < span / 400) ns = w * 8;
    const c = (s + e) / 2;
    cursors.A = s; cursorsChanged(false);
    setView(c - ns / 2, c + ns / 2);
  }

  // ================= fetching =================
  function requestView() {
    if (!cap) return;
    if (inflight) { pending = true; return; }
    if (rafQ) return;
    rafQ = true;
    requestAnimationFrame(() => { rafQ = false; fetchView(); });
  }
  async function fetchView() {
    if (!cap) return;
    const span = view.b - view.a;
    const a = Math.max(0, view.a - span * 0.5), b = Math.min(cap.samples, view.b + span * 0.5);
    const px = Math.max(16, Math.round(((b - a) / span) * plotW()));
    const my = ++seq;
    inflight = true;
    try {
      const r = await post('/api/la/view', { a, b, px, seq: my, capture: cap.id });
      if (my === seq || !data) { data = r; buildLayout(); draw(); paintDecoderMeta(); }
    } catch (e) { /* capture gone or server busy */ }
    inflight = false;
    if (pending) { pending = false; requestView(); }
  }
  let lastMetaSig = '';
  function paintDecoderMeta() { const sig = data.capture + JSON.stringify((data.decoders || []).map((d) => [d.id, d.error, d.pending, d.meta])); if (sig !== lastMetaSig) { lastMetaSig = sig; paintDecoders(); } }

  // ================= layout and labels =================
  function buildLayout() {
    const rows = [];
    if (!cap) { layout = rows; return; }
    const decRows = [];
    (data && data.decoders ? data.decoders : []).forEach((d) => (d.rows || []).forEach((r) => decRows.push({ d, r })));
    const placed = new Set();
    (data && data.analog ? data.analog : []).forEach((an, k) => rows.push({ t: 'an', k, h: ROW_H.an, key: 'an' + an.i }));
    const visible = cap.order.filter((i) => !cap.channels[i].hidden);
    visible.forEach((ci) => {
      rows.push({ t: 'ch', ci, h: ROW_H.ch, key: 'ch' + ci });
      decRows.forEach((x, j) => { if (x.r.channel === ci && !placed.has(j)) { placed.add(j); rows.push({ t: 'dec', did: x.d.id, rid: x.r.row, h: ROW_H.dec, key: `d${x.d.id}.${x.r.row}` }); } });
    });
    decRows.forEach((x, j) => { if (!placed.has(j)) rows.push({ t: 'dec', did: x.d.id, rid: x.r.row, h: ROW_H.dec, key: `d${x.d.id}.${x.r.row}` }); });
    buses.forEach((b, k) => rows.push({ t: 'bus', k, h: ROW_H.bus, key: 'bus' + k }));
    let y = 0;
    rows.forEach((r) => { r.y = y; y += r.h; });
    const sig = rows.map((r) => r.key).join('|') + '|' + cap.id + (cap.channels.map((c) => c.name + c.color).join(''));
    layout = rows;
    if (sig !== buildLayout.sig) { buildLayout.sig = sig; paintLabels(); resize(); }
  }
  let dragKey = null;
  function paintLabels() {
    labelsEl.innerHTML = '';
    for (const r of layout) {
      let el;
      if (r.t === 'ch') {
        const c = cap.channels[r.ci];
        const name = h('span', { class: 'n', title: `${c.name}\ndouble-click to rename, drag to reorder` }, c.name);
        el = h('div', { class: 'la-label ch', draggable: 'true', style: `height:${r.h}px` }, h('span', { class: 'la-swatch', style: `background:${c.color}` }), name,
          h('button', { class: 'la-lbtn', title: 'hide', html: I.eye, onclick: () => setCh({ index: r.ci, hidden: true }) }));
        name.addEventListener('dblclick', () => {
          const inp = h('input', { class: 'small', value: c.name, style: 'width:110px' });
          name.replaceWith(inp); inp.focus(); inp.select();
          const done = (ok) => { if (ok && inp.value.trim() && inp.value !== c.name) setCh({ index: r.ci, name: inp.value.trim() }); else paintLabels(); };
          inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') done(true); else if (e.key === 'Escape') done(false); e.stopPropagation(); });
          inp.addEventListener('blur', () => done(true));
        });
        el.addEventListener('dragstart', (e) => { dragKey = r.ci; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(r.ci)); });
        el.addEventListener('dragover', (e) => { if (dragKey != null) { e.preventDefault(); el.classList.add('drop'); } });
        el.addEventListener('dragleave', () => el.classList.remove('drop'));
        el.addEventListener('drop', (e) => {
          e.preventDefault(); el.classList.remove('drop');
          if (dragKey == null || dragKey === r.ci) return;
          const o = cap.order.filter((x) => x !== dragKey); o.splice(o.indexOf(r.ci), 0, dragKey); dragKey = null;
          put('/api/la/channels', { order: o }).then(refreshAfterEdit).catch((er) => toast(errMsg(er), 'err'));
        });
        el.addEventListener('dragend', () => { dragKey = null; });
      } else if (r.t === 'dec') {
        const d = data.decoders.find((x) => x.id === r.did); const row = d && d.rows.find((x) => x.row === r.rid);
        el = h('div', { class: 'la-label dec', style: `height:${r.h}px`, title: d ? `${d.name}: ${row ? row.label : ''}` : '' }, h('span', { class: 'n' }, row ? row.label : r.rid));
      } else if (r.t === 'bus') {
        const b = buses[r.k];
        el = h('div', { class: 'la-label bus', style: `height:${r.h}px`, title: b.channels.map(chName).join(', ') }, h('span', { class: 'n' }, b.name), h('span', { class: 'muted' }, b.format));
      } else {
        const an = data.analog[r.k];
        el = h('div', { class: 'la-label an', style: `height:${r.h}px` }, h('span', { class: 'la-swatch', style: `background:${an.color}` }), h('span', { class: 'n' }, an.name), h('span', { class: 'muted' }, 'analog'));
      }
      labelsEl.append(el);
    }
  }

  // ================= drawing =================
  function readColors() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    colors = { bg: v('--plot-bg'), grid: v('--plot-grid'), axis: v('--plot-axis'), tick: v('--plot-tick'), fg: v('--fg'), fg2: v('--fg-2'), muted: v('--muted'), border: v('--border'), surface: v('--surface'), surface2: v('--surface-2'),
      A: v('--c-cursor-a'), B: v('--c-cursor-b'), trig: v('--warn'), kind: Object.fromEntries(Object.entries(KIND_TOKEN).map(([k, t]) => [k, v(t)])), font: v('--mono') || 'monospace' };
  }
  readColors();
  ctx.on('theme', () => { readColors(); draw(); });
  function sizeCanvas(c, w, hgt) { const dpr = window.devicePixelRatio || 1; if (c.width !== Math.round(w * dpr) || c.height !== Math.round(hgt * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(hgt * dpr); c.style.width = w + 'px'; c.style.height = hgt + 'px'; } const g = c.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); return g; }
  function resize() { draw(); }
  function ticks() {
    const W = plotW();
    const span = view.b - view.a;
    if (axisSamples || !cap) {
      const st = nice((span / W) * 110);
      const out = [];
      for (let s = Math.ceil(view.a / st) * st; s <= view.b; s += st) out.push({ s, label: Math.round(s).toLocaleString() });
      return out;
    }
    const spanT = span / cap.samplerate;
    const st = nice((spanT / W) * 110);
    const t0 = tOf(view.a), t1 = tOf(view.b);
    const [scale, uname] = unitFor(st);
    const dec = Math.max(0, Math.min(6, -Math.floor(Math.log10(st / scale) + 1e-9)));
    const out = [];
    for (let k = Math.ceil(t0 / st); k * st <= t1 + st * 1e-9; k++) { const t = k * st; out.push({ s: t * cap.samplerate + cap.trigger, label: `${(t / scale).toFixed(dec)} ${uname}` }); }
    return out;
  }
  function draw() {
    const W = plotW();
    const totalH = Math.max(layout.reduce((m, r) => Math.max(m, r.y + r.h), 0), 10);
    plotWrap.style.height = totalH + 'px';
    labelsEl.style.minHeight = totalH + 'px';
    const g = sizeCanvas(plot, W, totalH);
    const ga = sizeCanvas(axisCanvas, W, 30);
    g.fillStyle = colors.bg; g.fillRect(0, 0, W, totalH);
    ga.fillStyle = colors.surface; ga.fillRect(0, 0, W, 30);
    paintScrollbar();
    if (!cap) { footer.innerHTML = ''; return; }
    // grid and axis
    const tk = ticks();
    ga.font = `11px ${colors.font}`; ga.fillStyle = colors.fg2; ga.strokeStyle = colors.tick; ga.textBaseline = 'top';
    g.strokeStyle = colors.grid; g.lineWidth = 1;
    for (const t of tk) {
      const x = Math.round(xOf(t.s)) + 0.5;
      g.beginPath(); g.moveTo(x, 0); g.lineTo(x, totalH); g.stroke();
      ga.beginPath(); ga.moveTo(x, 20); ga.lineTo(x, 30); ga.stroke();
      ga.fillText(t.label, x + 3, 5);
    }
    ga.strokeStyle = colors.border; ga.beginPath(); ga.moveTo(0, 29.5); ga.lineTo(W, 29.5); ga.stroke();
    // row separators
    g.strokeStyle = colors.border;
    for (const r of layout) { g.beginPath(); g.moveTo(0, r.y + r.h - 0.5); g.lineTo(W, r.y + r.h - 0.5); g.stroke(); }
    // segment boundaries (analog-to-logic captures made of several triggers)
    if (segBounds.length) { g.save(); g.setLineDash([2, 4]); g.strokeStyle = colors.muted; for (const s of segBounds) { if (s < view.a || s > view.b) continue; const x = Math.round(xOf(s)) + 0.5; g.beginPath(); g.moveTo(x, 0); g.lineTo(x, totalH); g.stroke(); } g.restore(); }
    // cursor range shade
    if (cursors.A != null && cursors.B != null) { const x0 = xOf(Math.min(cursors.A, cursors.B)), x1 = xOf(Math.max(cursors.A, cursors.B)); g.fillStyle = colors.kind.data; g.globalAlpha = 0.06; g.fillRect(x0, 0, x1 - x0, totalH); g.globalAlpha = 1; }
    if (data && data.capture === cap.id) {
      for (const r of layout) {
        if (r.t === 'ch') drawChannel(g, r, W);
        else if (r.t === 'dec') drawDecRow(g, r, W);
        else if (r.t === 'bus') drawBus(g, r, W);
        else if (r.t === 'an') drawAnalog(g, r, W);
      }
    }
    // trigger marker
    if (cap.trigger >= view.a && cap.trigger <= view.b) {
      const x = Math.round(xOf(cap.trigger)) + 0.5;
      g.save(); g.setLineDash([5, 4]); g.strokeStyle = colors.trig; g.beginPath(); g.moveTo(x, 0); g.lineTo(x, totalH); g.stroke(); g.restore();
      ga.fillStyle = colors.trig; ga.beginPath(); ga.moveTo(x - 6, 19); ga.lineTo(x + 6, 19); ga.lineTo(x, 29); ga.closePath(); ga.fill();
    }
    for (const k of ['A', 'B']) {
      const s = cursors[k];
      if (s == null || s < view.a || s > view.b) continue;
      const x = Math.round(xOf(s)) + 0.5;
      g.strokeStyle = colors[k]; g.lineWidth = 1.5; g.beginPath(); g.moveTo(x, 0); g.lineTo(x, totalH); g.stroke(); g.lineWidth = 1;
      ga.fillStyle = colors[k]; ga.fillRect(x - 8, 16, 16, 14); ga.fillStyle = colors.bg; ga.font = `bold 11px ${colors.font}`; ga.fillText(k, x - 3.5, 17);
    }
    paintFooter();
  }
  function edgeRunsX(c, W) {
    // yields [x0, x1, level] segments where level is 0, 1 or 2 (busy)
    const out = [];
    if (c.edges) {
      let v = c.v0, prev = data.a;
      for (const e of c.edges) { out.push([xOf(prev), xOf(e), v]); v ^= 1; prev = e; }
      out.push([xOf(prev), xOf(data.b), v]);
    } else {
      const s = c.b; let start = 0;
      for (let k = 1; k <= s.length; k++) {
        if (k === s.length || s.charCodeAt(k) !== s.charCodeAt(start)) { out.push([xOf(data.a + start * data.step), xOf(data.a + k * data.step), s.charCodeAt(start) - 48]); start = k; }
      }
    }
    return out.filter((r) => r[1] >= -2 && r[0] <= W + 2);
  }
  function drawChannel(g, r, W) {
    const c = data.channels.find((x) => x.i === r.ci);
    if (!c) return;
    const col = cap.channels[r.ci].color;
    const yh = r.y + 6, yl = r.y + r.h - 6;
    const runs = edgeRunsX(c, W);
    g.fillStyle = col; g.globalAlpha = 0.13;
    for (const [x0, x1, v] of runs) if (v === 1) g.fillRect(x0, yh, x1 - x0, yl - yh);
    g.globalAlpha = 0.45;
    for (const [x0, x1, v] of runs) if (v === 2) g.fillRect(x0, yh, Math.max(1, x1 - x0), yl - yh);
    g.globalAlpha = 1; g.strokeStyle = col; g.lineWidth = 1.4; g.beginPath();
    let last = null;
    for (const [x0, x1, v] of runs) {
      if (v === 2) { g.moveTo(x0, yh); g.lineTo(x1, yh); g.moveTo(x0, yl); g.lineTo(x1, yl); last = null; continue; }
      const y = v ? yh : yl;
      if (last != null && last !== y) { g.moveTo(x0, last); g.lineTo(x0, y); }
      g.moveTo(x0, y); g.lineTo(x1, y); last = y;
    }
    g.stroke(); g.lineWidth = 1;
  }
  function shape(g, x0, x1, y0, y1) {
    const m = Math.min(4, (x1 - x0) / 2), ym = (y0 + y1) / 2;
    g.beginPath(); g.moveTo(x0, ym); g.lineTo(x0 + m, y0); g.lineTo(x1 - m, y0); g.lineTo(x1, ym); g.lineTo(x1 - m, y1); g.lineTo(x0 + m, y1); g.closePath();
  }
  function fitText(g, text, w) {
    if (g.measureText(text).width <= w) return text;
    const short = text.split(/\s+/)[0];
    if (g.measureText(short).width <= w) return short;
    return null;
  }
  function drawDecRow(g, r, W) {
    const d = data.decoders.find((x) => x.id === r.did); const row = d && d.rows.find((x) => x.row === r.rid);
    if (!row) return;
    const y0 = r.y + 3, y1 = r.y + r.h - 3;
    g.font = `11px ${colors.font}`; g.textBaseline = 'middle';
    for (const it of row.items) {
      const [s, e, text, kind] = it;
      const x0 = Math.max(xOf(s), -5), x1 = Math.min(Math.max(xOf(e), xOf(s) + 1.5), W + 5);
      if (x1 < 0 || x0 > W) continue;
      const col = colors.kind[kind] || colors.kind.data;
      shape(g, x0, x1, y0, y1);
      g.fillStyle = col; g.globalAlpha = kind === 'agg' ? 0.25 : kind === 'state' ? 0.12 : 0.22; g.fill(); g.globalAlpha = 0.9; g.strokeStyle = col; g.stroke(); g.globalAlpha = 1;
      const label = kind === 'agg' ? fitText(g, `${text} items`, x1 - x0 - 8) || fitText(g, text, x1 - x0 - 6) : fitText(g, text, x1 - x0 - 8);
      if (label) { g.fillStyle = colors.fg; const cx = Math.max(x0, 0), cw = Math.min(x1, W) - cx; g.fillText(label, cx + Math.max(4, (cw - g.measureText(label).width) / 2), (y0 + y1) / 2 + 0.5); }
    }
  }
  function busText(b, v) { return v == null ? '' : b.format === 'dec' ? String(v) : b.format === 'bin' ? v.toString(2).padStart(b.channels.length, '0') : '0x' + v.toString(16).toUpperCase().padStart(Math.ceil(b.channels.length / 4), '0'); }
  function drawBus(g, r, W) {
    const bv = data.buses && data.buses[r.k]; const b = buses[r.k];
    if (!bv || !b) return;
    const y0 = r.y + 4, y1 = r.y + r.h - 4;
    g.font = `11px ${colors.font}`; g.textBaseline = 'middle';
    const col = cap.channels[b.channels[0]] ? cap.channels[b.channels[0]].color : colors.kind.data;
    for (const [s, e, v] of bv.runs) {
      const x0 = xOf(s), x1 = xOf(e);
      if (x1 < 0 || x0 > W) continue;
      shape(g, x0, Math.max(x1, x0 + 1), y0, y1);
      g.fillStyle = col; g.globalAlpha = v == null ? 0.4 : 0.1; g.fill(); g.globalAlpha = 1; g.strokeStyle = col; g.stroke();
      const t = fitText(g, busText(b, v), x1 - x0 - 8);
      if (t) { g.fillStyle = colors.fg; const cx = Math.max(x0, 0), cw = Math.min(x1, W) - cx; g.fillText(t, cx + Math.max(4, (cw - g.measureText(t).width) / 2), (y0 + y1) / 2 + 0.5); }
    }
  }
  function drawAnalog(g, r, W) {
    const an = data.analog[r.k];
    if (!an) return;
    const pad = 6, y0 = r.y + pad, y1 = r.y + r.h - pad;
    const lo = an.lo, hi = an.hi === an.lo ? an.lo + 1 : an.hi;
    const yOf = (v) => y1 - ((v - lo) / (hi - lo)) * (y1 - y0);
    if (an.span) {
      // where the analog trace lies on this time base (it can be much shorter than the logic capture)
      const x0 = Math.max(0, xOf(an.span[0])), x1 = Math.min(W, xOf(an.span[1]));
      if (x1 > x0) { g.fillStyle = an.color; g.globalAlpha = 0.07; g.fillRect(x0, r.y + 2, Math.max(2, x1 - x0), r.h - 4); g.globalAlpha = 0.5; g.fillRect(x0, r.y + 2, Math.max(2, x1 - x0), 2); g.globalAlpha = 1; }
      if (x1 - x0 < 40 && x1 > 0 && x0 < W) { g.font = `11px ${colors.font}`; g.fillStyle = colors.muted; g.textBaseline = 'middle'; g.fillText(`${an.name}: ${fmtSec((an.span[1] - an.span[0]) / cap.samplerate)} (zoom in)`, Math.min(W - 220, x1 + 6), r.y + r.h / 2); }
    }
    if (an.threshold) { g.save(); g.setLineDash([4, 3]); g.strokeStyle = colors.trig; const y = yOf(an.threshold[0]); g.beginPath(); g.moveTo(0, y); g.lineTo(W, y); g.stroke(); g.restore(); }
    g.strokeStyle = an.color; g.lineWidth = 1.2; g.beginPath();
    if (an.y) { an.x.forEach((x, i) => { const px = xOf(x), py = yOf(an.y[i]); if (i) g.lineTo(px, py); else g.moveTo(px, py); }); }
    else { an.x.forEach((x, i) => { const px = Math.round(xOf(x)) + 0.5; g.moveTo(px, yOf(an.min[i])); g.lineTo(px, yOf(an.max[i]) - 0.5); }); }
    g.stroke(); g.lineWidth = 1;
  }
  function paintScrollbar() {
    if (!cap) { thumb.style.display = 'none'; return; }
    thumb.style.display = '';
    const W = hbar.clientWidth - LABEL_W;
    const l = (view.a / cap.samples) * W, w = Math.max(14, ((view.b - view.a) / cap.samples) * W);
    thumb.style.left = LABEL_W + l + 'px'; thumb.style.width = w + 'px';
  }
  function valueAt(c, s) {
    if (c.edges) { let lo = 0, hi = c.edges.length; while (lo < hi) { const m = (lo + hi) >> 1; if (c.edges[m] <= s) lo = m + 1; else hi = m; } return lo & 1 ? 1 - c.v0 : c.v0; }
    const k = Math.floor((s - data.a) / data.step); const v = c.b.charCodeAt(Math.max(0, Math.min(c.b.length - 1, k))) - 48; return v === 2 ? null : v;
  }
  function paintFooter() {
    footer.innerHTML = '';
    if (!cap) return;
    const ft = (s) => (axisSamples ? `#${Math.round(s).toLocaleString()}` : fmtSec(tOf(s)));
    footer.append(h('span', { html: `<b>${cap.channels.length}</b> ch · <b>${fmtHz(cap.samplerate)}</b> · <b>${cap.samples.toLocaleString()}</b> samples (${fmtSec(cap.duration)}) · view ${ft(view.a)} .. ${ft(view.b)}` }));
    const { A, B } = cursors;
    if (A != null) footer.append(h('span', { style: `color:${colors.A}`, html: `A <b>${fmtSec(tOf(A))}</b> #${Math.round(A)}` }));
    if (B != null) footer.append(h('span', { style: `color:${colors.B}`, html: `B <b>${fmtSec(tOf(B))}</b> #${Math.round(B)}` }));
    if (A != null && B != null) { const d = Math.abs(B - A) / cap.samplerate; footer.append(h('span', { html: `Δ <b>${fmtSec(d)}</b> (${Math.round(Math.abs(B - A)).toLocaleString()} samples) · 1/Δ <b>${d ? fmtF(1 / d) : '-'}</b>` })); }
  }
  function showTip() {
    if (!hover || !cap || !data) { tip.style.display = 'none'; return; }
    const s = Math.round(hover.s);
    const lines = [`<b>${fmtSec(tOf(s))}</b> · sample #${s.toLocaleString()}`];
    const r = layout.find((x) => hover.y >= x.y && hover.y < x.y + x.h);
    const bits = [];
    for (const row of layout) if (row.t === 'ch') { const c = data.channels.find((x) => x.i === row.ci); if (c) { const v = valueAt(c, s); bits.push(`${cap.channels[row.ci].name}=${v == null ? '~' : v}`); } }
    if (r && r.t === 'dec') {
      const d = data.decoders.find((x) => x.id === r.did); const row = d && d.rows.find((x) => x.row === r.rid);
      const it = row && row.items.find((x) => x[0] <= hover.s && x[1] >= hover.s);
      if (it) lines.push(`${d.name} ${row.label}: <b>${escapeHtml(it[3] === 'agg' ? it[2] + ' items (zoom in)' : it[2])}</b>`);
    } else if (r && r.t === 'bus') {
      const b = buses[r.k]; const bv = data.buses && data.buses[r.k]; const run = bv && bv.runs.find((x) => x[0] <= s && x[1] > s);
      if (run) lines.push(`${b.name} = <b>${run[2] == null ? 'changing' : busText(b, run[2]) + (b.format === 'hex' ? ' (' + run[2] + ')' : '')}</b>`);
    }
    lines.push(`<span class="mono">${bits.slice(0, 20).join(' ')}</span>`);
    tip.innerHTML = lines.join('<br>');
    tip.style.display = '';
    const bodyR = body.getBoundingClientRect();
    const x = hover.cx - bodyR.left + 14, y = hover.cy - bodyR.top + 14;
    tip.style.left = Math.min(x, bodyR.width - tip.offsetWidth - 8) + 'px';
    tip.style.top = Math.min(y, bodyR.height - tip.offsetHeight - 8) + 'px';
  }
  function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

  // ================= interaction =================
  function snap(s, y) {
    const r = layout.find((x) => y >= x.y && y < x.y + x.h && x.t === 'ch');
    if (!r || !data) return s;
    const c = data.channels.find((x) => x.i === r.ci);
    if (!c || !c.edges || !c.edges.length) return s;
    let best = null, bd = 7;
    for (const e of c.edges) { const d = Math.abs(xOf(e) - xOf(s)); if (d < bd) { bd = d; best = e; } }
    return best != null ? best : s;
  }
  function cursorsChanged(redraw = true) { if (redraw) draw(); measure(); }
  function localXY(e) { const r = plot.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
  let drag = null;
  plot.addEventListener('pointerdown', (e) => {
    if (!cap) return;
    const { x, y } = localXY(e);
    plot.setPointerCapture(e.pointerId);
    for (const k of ['A', 'B']) if (cursors[k] != null && Math.abs(xOf(cursors[k]) - x) < 5 && e.button === 0) { drag = { kind: 'cursor', k }; return; }
    drag = { kind: e.shiftKey ? 'select' : 'pan', x0: x, y0: y, a: view.a, b: view.b, moved: false, button: e.button };
  });
  plot.addEventListener('pointermove', (e) => {
    const { x, y } = localXY(e);
    if (cap) { hover = { x, y, s: sOf(x), cx: e.clientX, cy: e.clientY }; showTip(); }
    if (!drag) return;
    if (drag.kind === 'cursor') { cursors[drag.k] = Math.max(0, Math.min(cap.samples - 1, snap(sOf(x), y))); cursorsChanged(); return; }
    if (Math.abs(x - drag.x0) > 3) drag.moved = true;
    if (!drag.moved) return;
    if (drag.kind === 'pan') { const ds = ((x - drag.x0) / plotW()) * (drag.b - drag.a); setView(drag.a - ds, drag.b - ds); }
    else { drawSelect(drag.x0, x); }
  });
  plot.addEventListener('pointerup', (e) => {
    if (!drag) return;
    const { x, y } = localXY(e);
    const d = drag; drag = null;
    if (d.kind === 'cursor') return;
    if (d.kind === 'select' && d.moved) { const a = sOf(Math.min(d.x0, x)), b = sOf(Math.max(d.x0, x)); setView(a, b); return; }
    if (!d.moved && d.button === 0) { cursors[e.altKey ? 'B' : 'A'] = Math.max(0, Math.min(cap.samples - 1, snap(sOf(x), y))); cursorsChanged(); }
  });
  plot.addEventListener('contextmenu', (e) => { e.preventDefault(); if (!cap) return; const { x, y } = localXY(e); cursors.B = Math.max(0, Math.min(cap.samples - 1, snap(sOf(x), y))); cursorsChanged(); });
  plot.addEventListener('pointerleave', () => { hover = null; tip.style.display = 'none'; });
  plot.addEventListener('wheel', (e) => {
    if (!cap) return;
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) { e.preventDefault(); const d = e.shiftKey ? e.deltaY : e.deltaX; const ds = (d / plotW()) * (view.b - view.a); setView(view.a + ds, view.b + ds); return; }
    if (e.ctrlKey || e.metaKey || !e.altKey) { e.preventDefault(); const { x } = localXY(e); zoom(Math.exp(e.deltaY * 0.0015), sOf(x)); }
  }, { passive: false });
  // zoom to a range by dragging on the axis
  let axDrag = null;
  function drawSelect(x0, x1) { draw(); const g = plot.getContext('2d'); g.fillStyle = colors.kind.data; g.globalAlpha = 0.15; g.fillRect(Math.min(x0, x1), 0, Math.abs(x1 - x0), plot.height); g.globalAlpha = 1; }
  axisCanvas.addEventListener('pointerdown', (e) => { if (!cap) return; axisCanvas.setPointerCapture(e.pointerId); const r = axisCanvas.getBoundingClientRect(); axDrag = { x0: e.clientX - r.left }; });
  axisCanvas.addEventListener('pointermove', (e) => { if (!axDrag) return; const r = axisCanvas.getBoundingClientRect(); axDrag.x1 = e.clientX - r.left; drawSelect(axDrag.x0, axDrag.x1); });
  axisCanvas.addEventListener('pointerup', () => { if (!axDrag) return; const d = axDrag; axDrag = null; if (d.x1 != null && Math.abs(d.x1 - d.x0) > 4) setView(sOf(Math.min(d.x0, d.x1)), sOf(Math.max(d.x0, d.x1))); else draw(); });
  axisCanvas.addEventListener('dblclick', fit);
  plot.addEventListener('dblclick', (e) => { if (e.shiftKey) fit(); });
  // scrollbar
  let sbDrag = null;
  hbar.addEventListener('pointerdown', (e) => {
    if (!cap) return;
    const r = hbar.getBoundingClientRect(); const W = r.width - LABEL_W; const x = e.clientX - r.left - LABEL_W;
    if (e.target === thumb) { hbar.setPointerCapture(e.pointerId); sbDrag = { x0: e.clientX, a: view.a, b: view.b, W }; return; }
    const span = view.b - view.a; const c = (x / W) * cap.samples; setView(c - span / 2, c + span / 2);
  });
  hbar.addEventListener('pointermove', (e) => { if (!sbDrag) return; const ds = ((e.clientX - sbDrag.x0) / sbDrag.W) * cap.samples; setView(sbDrag.a + ds, sbDrag.b + ds); });
  hbar.addEventListener('pointerup', () => { sbDrag = null; });
  // keyboard
  document.addEventListener('keydown', (e) => {
    if (!viewEl.offsetParent || !cap) return;
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
    const span = view.b - view.a;
    if ((e.key === '+' || e.key === '=') && !e.ctrlKey && !e.metaKey) zoom(0.5, hover ? hover.s : undefined);
    else if ((e.key === '-' || e.key === '_') && !e.ctrlKey && !e.metaKey) zoom(2, hover ? hover.s : undefined);
    else if (e.key === 'ArrowLeft') setView(view.a - span * 0.1, view.b - span * 0.1);
    else if (e.key === 'ArrowRight') setView(view.a + span * 0.1, view.b + span * 0.1);
    else if (e.key === 'Home') setView(0, span);
    else if (e.key === 'End') setView(cap.samples - span, cap.samples);
    else if (e.key === 'f' || e.key === 'F') fit();
    else if ((e.key === 'a' || e.key === 'b') && hover) { cursors[e.key.toUpperCase()] = Math.round(snap(hover.s, hover.y)); cursorsChanged(); }
    else if (e.key === 'Escape') stopCapture();
    else return;
    e.preventDefault();
  });

  // ================= search =================
  async function search(dir) {
    if (!cap) return;
    const from = cursors.A != null ? cursors.A : dir > 0 ? view.a : view.b;
    const body = { kind: sMode.value, from, direction: dir > 0 ? 'next' : 'prev' };
    if (sMode.value === 'edge') Object.assign(body, { channel: +sChan.value, edge: sEdge.value });
    else if (sMode.value === 'pattern') Object.assign(body, { pattern: sPat.value, edge_channel: sEdgeCh.value === '' ? null : +sEdgeCh.value, edge: sEdge.value });
    else Object.assign(body, { text: sText.value, decoder: sDec.value });
    try {
      const r = await post('/api/la/search', body);
      if (!r.found) { sInfo.textContent = 'no match'; return; }
      sInfo.textContent = `at ${fmtSec(r.t)}`;
      const end = r.end != null ? r.end : r.index;
      if (r.end != null) goto(r.index, Math.max(end, r.index + 1));
      else { const span = view.b - view.a; cursors.A = r.index; cursorsChanged(false); setView(r.index - span / 2, r.index + span / 2); }
    } catch (e) { sInfo.textContent = errMsg(e); }
  }

  new ResizeObserver(() => { if (viewEl.offsetParent) { draw(); requestView(); } }).observe(plotWrap.parentElement);
  ctx.on('tab', (t) => { if (t === 'logic') { loadSources(); loadStatus(); loadFiles(); setTimeout(() => { draw(); requestView(); }, 30); } });
  ctx.on('scope-connected', () => loadSources());
  ctx.on('scope-disconnected', () => loadSources());
  ctx.on('target-connected', () => loadSources());
  stopBtn.disabled = true;
  loadSources();
  loadStatus();
  return { capture: startCapture, stop: stopCapture, fit, get view() { return view; }, get cursors() { return cursors; }, setView, goto };
}

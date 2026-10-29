import { h, get, post, toast, themeColors } from './api.js';

// what the generic sweep knows about every path it can sweep; the connected model's profile (capabilities.profile.glitch.params) replaces this list
const FALLBACK_PARAMS = [
  { path: 'glitch.width', label: 'width', unit: '', min: null, max: null, int: false, note: '' }, { path: 'glitch.offset', label: 'offset', unit: '', min: null, max: null, int: false, note: '' },
  { path: 'glitch.ext_offset', label: 'ext offset', unit: 'cycles', min: 0, max: null, int: true, note: '' }, { path: 'glitch.repeat', label: 'repeat', unit: 'cycles', min: 1, max: 8192, int: true, note: '' },
];
const RESULTS = ['normal', 'success', 'reset', 'garbled', 'not_fired'];
const resultColors = () => { const c = themeColors(); return { normal: c.info, success: c.ok, reset: c.err, garbled: c.warn, not_fired: c.muted }; };
// default sweep rows per glitch engine: the Nano's firmware crowbar (offset and pulse length in 8.3 ns cycles), the Lite and Pro DCMs (percent of a period) and the Husky MMCMs (phase steps)
const DEFAULT_ROWS = {
  crowbar_firmware: [['glitch.ext_offset', 0, 200, 10], ['glitch.repeat', 1, 7, 1]],
  dcm_percent: [['glitch.width', 5, 45, 5], ['glitch.ext_offset', 0, 100, 10]],
  mmcm_steps: [['glitch.width', 100, 2000, 100], ['glitch.ext_offset', 0, 100, 10]],
};

export function initGlitch(ctx, el) {
  const rows = h('div');
  const cmdIn = h('input', { class: 'mono', value: 'g', style: 'width:40px', maxlength: 1 });
  const dataIn = h('input', { class: 'flex mono', placeholder: 'hex payload (optional)' });
  const expIn = h('input', { class: 'flex mono', value: 'c4090000', placeholder: 'expected response hex (blank = any valid)' });
  const lenIn = h('input', { type: 'number', value: 4, style: 'width:60px' });
  const repIn = h('input', { type: 'number', value: 1, min: 1, style: 'width:60px' });
  const orderSel = h('select', {}, h('option', { value: 'nested' }, 'nested'), h('option', { value: 'random' }, 'random'));
  const resetSel = h('select', {}, h('option', { value: 'nrst' }, 'nRST pin'), h('option', { value: 'pdic' }, 'PDIC pin'), h('option', { value: 'none' }, 'no reset'));
  const resetOn = h('select', {}, h('option', { value: 'reset' }, 'after reset outcome'), h('option', { value: 'always' }, 'before every attempt'), h('option', { value: 'never' }, 'never'));
  const timeoutIn = h('input', { type: 'number', value: 1000, style: 'width:70px', title: 'ms to wait for a response' });
  const status = h('div', { class: 'help mono' }, 'idle');
  const counts = h('div', { class: 'legend' });
  const canvas = h('canvas', { class: 'scatter' });
  const xSel = h('select', {}), ySel = h('select', {});
  const table = h('table', { class: 'tbl' });
  const engine = h('div', { class: 'help' }, 'Connect a scope to see its glitch engine.');
  const presets = h('div', { class: 'row' });
  const unitLine = h('div', { class: 'help mono' });
  let results = [], paramNames = [], params = FALLBACK_PARAMS, profile = null, model = null;

  const paramOf = (path) => params.find((p) => p.path === path) || FALLBACK_PARAMS.find((p) => p.path === path) || { path, label: path.replace('glitch.', ''), unit: '', int: false };
  function addRow(path, start, stop, step) {
    const sel = h('select', {}, ...params.map((p) => h('option', { value: p.path, selected: p.path === path, title: p.note || '' }, `${p.label}${p.unit ? ' (' + p.unit + ')' : ''}`)));
    const r = h('div', { class: 'param-row' }, sel,
      h('input', { type: 'number', value: start, placeholder: 'start', step: 'any' }), h('input', { type: 'number', value: stop, placeholder: 'stop', step: 'any' }), h('input', { type: 'number', value: step, placeholder: 'step', step: 'any' }),
      h('button', { class: 'btn sm', onclick: () => r.remove(), title: 'Remove', html: '<svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>' }));
    sel.addEventListener('change', () => { const p = paramOf(sel.value); const [s, e, st] = r.querySelectorAll('input'); if (p.min != null) s.min = e.min = p.min; if (p.max != null) s.max = e.max = p.max; st.step = p.int ? 1 : 'any'; });
    rows.append(r);
  }
  function defaultRows() {
    rows.innerHTML = '';
    const kind = profile ? profile.kind : 'dcm_percent';
    (DEFAULT_ROWS[kind] || DEFAULT_ROWS.dcm_percent).forEach(([p, a, b, c]) => addRow(p, a, b, c));
  }
  defaultRows();

  function sweepParams() {
    const ps = [...rows.querySelectorAll('.param-row')].map((r) => {
      const [sel, s, e, st] = r.querySelectorAll('select,input');
      return { path: sel.value, start: +s.value, stop: +e.value, step: +st.value, int: !!paramOf(sel.value).int };
    });
    return { parameters: ps, repeats: +repIn.value, order: orderSel.value, command: cmdIn.value || 'g', data: dataIn.value, expected: expIn.value || null, output_len: +lenIn.value, reset: resetSel.value, reset_on: resetOn.value, glitch_timeout: +timeoutIn.value };
  }
  async function start() {
    results = [];
    try { await post('/api/glitch/start', sweepParams()); } catch (e) { toast(e.message, 'err', 6000); }
  }
  async function exportCsv() { try { const r = await post('/api/glitch/export', { path: 'glitch_results.csv' }); toast('Saved ' + r.path, 'ok', 6000); } catch (e) { toast(e.message, 'err'); } }
  async function preset(action, label) {
    try { await post(`/api/scope/action/${action}`); toast(`${label} applied`, 'ok'); if (ctx.scopeTree && ctx.scopeTree.refreshValues) ctx.scopeTree.refreshValues(); await showUnits(); } catch (e) { toast(e.message, 'err', 6000); }
  }

  // the model's glitch engine: parameter list with units, preset buttons, and the size of one step in nanoseconds
  function applyProfile(caps) {
    const prof = caps && caps.profile ? caps.profile : null;
    const next = prof ? prof.glitch : null;
    const changed = (next ? next.kind : null) !== (profile ? profile.kind : null) || (caps && caps.model) !== model;
    profile = next; model = caps ? caps.model : null;
    params = (profile && profile.params && profile.params.length) ? profile.params : FALLBACK_PARAMS;
    presets.innerHTML = '';
    if (!profile) { engine.textContent = 'Connect a scope to see its glitch engine.'; unitLine.textContent = ''; if (changed) defaultRows(); return; }
    const what = { crowbar_firmware: 'firmware crowbar: one MOSFET, timing in 8.3 ns CPU cycles, about 200 ns of trigger jitter', dcm_percent: 'two phase-shifted DCM clocks: width and offset in percent of a period, 256 coarse steps (each one an ICAP reprogram) plus fine steps', mmcm_steps: 'MMCM phase shifter: width and offset in phase steps, up to 32 glitches per trigger with a fired counter' }[profile.kind] || profile.kind;
    engine.textContent = `${prof.label}: ${what}.` + (profile.note ? ' ' + profile.note + '.' : '');
    if (profile.clock_glitch) presets.append(h('button', { class: 'btn sm', onclick: () => preset('cglitch_setup', 'Clock glitch preset'), title: 'glitch clock on HS2, crowbar off, trigger from the capture' }, 'Clock glitch'));
    if (profile.kind !== 'crowbar_firmware') {
      presets.append(h('button', { class: 'btn sm', onclick: () => preset('vglitch_lp', 'Voltage glitch (LP) preset'), title: 'glitch_only output, low-power MOSFET' }, 'Voltage LP'),
        h('button', { class: 'btn sm', onclick: () => preset('vglitch_hp', 'Voltage glitch (HP) preset'), title: 'glitch_only output, high-power MOSFET' }, 'Voltage HP'),
        h('button', { class: 'btn sm', onclick: () => preset('vglitch_both', 'Voltage glitch (both) preset'), title: 'both MOSFETs' }, 'Both'));
    } else {
      presets.append(h('button', { class: 'btn sm', onclick: () => preset('vglitch_lp', 'Crowbar enabled'), title: 'repeat 1: the crowbar fires on the TIO4 edge after every arm' }, 'Enable crowbar'));
    }
    presets.append(h('button', { class: 'btn sm danger', onclick: () => preset('glitch_disable', 'Glitching disabled'), title: 'glitch output off, MOSFETs released' }, 'Disable'));
    if (changed) defaultRows(); else rows.querySelectorAll('.param-row select').forEach((sel) => { const cur = sel.value; sel.innerHTML = ''; params.forEach((p) => sel.append(h('option', { value: p.path, selected: p.path === cur }, `${p.label}${p.unit ? ' (' + p.unit + ')' : ''}`))); });
    showUnits();
  }
  async function showUnits() {
    unitLine.textContent = '';
    if (!profile) return;
    if (profile.kind === 'crowbar_firmware') { unitLine.textContent = 'ext_offset and repeat count 120 MHz CPU cycles: 1 count = 8.3 ns (repeat above 7 adds about 16 ns per count).'; return; }
    try {
      const flat = {};
      const walk = (ns) => (ns || []).forEach((n) => { if (n.kind === 'group') walk(n.children); else flat[n.path] = n.value; });
      walk(await get('/api/scope/settings'));
      const f = +flat['clock.clkgen_freq'] || 0;
      if (!f) return;
      const period = 1e9 / f;
      if (profile.kind === 'mmcm_steps') { const steps = +flat['glitch.phase_shift_steps'] || profile.phase_shift_steps || 0; if (steps) unitLine.textContent = `target clock ${(f / 1e6).toFixed(3)} MHz: one period is ${period.toFixed(1)} ns = ${steps} phase steps, so 1 step = ${(period / steps * 1000).toFixed(1)} ps; width ${flat['glitch.width']} = ${(period * (+flat['glitch.width'] || 0) / steps).toFixed(2)} ns`; }
      else unitLine.textContent = `target clock ${(f / 1e6).toFixed(3)} MHz: one period is ${period.toFixed(1)} ns, so 1 % = ${(period / 100).toFixed(2)} ns and one fine step is about ${(5000 / 510).toFixed(0)} ps; width ${flat['glitch.width']} % = ${(period * Math.abs(+flat['glitch.width'] || 0) / 100).toFixed(2)} ns`;
    } catch (e) { /* no scope */ }
  }

  function updateAxes(names) {
    if (names.join() === paramNames.join()) return;
    paramNames = names;
    [xSel, ySel].forEach((s) => { s.innerHTML = ''; names.forEach((n, i) => s.append(h('option', { value: i }, n.replace('glitch.', '')))); });
    xSel.value = 0; ySel.value = names.length > 1 ? 1 : 0;
  }
  function draw() {
    const ctx2 = canvas.getContext('2d');
    const W = canvas.width = canvas.clientWidth * devicePixelRatio, H = canvas.height = canvas.clientHeight * devicePixelRatio;
    ctx2.clearRect(0, 0, W, H);
    if (!results.length) return;
    const xi = +xSel.value, yi = +ySel.value;
    const xs = results.map((r) => r.values[xi]), ys = results.map((r) => r.values[yi]);
    const pad = 36 * devicePixelRatio;
    let xmin = Math.min(...xs), xmax = Math.max(...xs), ymin = Math.min(...ys), ymax = Math.max(...ys);
    if (xmin === xmax) { xmin -= 1; xmax += 1; } if (ymin === ymax) { ymin -= 1; ymax += 1; }
    const sx = (v) => pad + (v - xmin) / (xmax - xmin) * (W - 2 * pad), sy = (v) => H - pad - (v - ymin) / (ymax - ymin) * (H - 2 * pad);
    const TC = themeColors(), RESULT_COLORS = resultColors();
    ctx2.strokeStyle = TC.border; ctx2.lineWidth = devicePixelRatio;
    ctx2.strokeRect(pad, pad, W - 2 * pad, H - 2 * pad);
    ctx2.fillStyle = TC.muted; ctx2.font = `${11 * devicePixelRatio}px sans-serif`;
    ctx2.fillText(`${(paramNames[xi] || '').replace('glitch.', '')}  ${xmin} to ${xmax}`, pad, H - 8 * devicePixelRatio);
    ctx2.save(); ctx2.translate(10 * devicePixelRatio, H - pad); ctx2.rotate(-Math.PI / 2); ctx2.fillText(`${(paramNames[yi] || '').replace('glitch.', '')}  ${ymin} to ${ymax}`, 0, 0); ctx2.restore();
    const order = { not_fired: 0, normal: 1, garbled: 2, reset: 3, success: 4 };
    [...results].sort((a, b) => (order[a.result] || 0) - (order[b.result] || 0)).forEach((r) => {
      ctx2.fillStyle = RESULT_COLORS[r.result] || TC.fg;
      ctx2.globalAlpha = r.result === 'normal' || r.result === 'not_fired' ? 0.5 : 0.9;
      const rad = (r.result === 'success' ? 4 : 2.5) * devicePixelRatio;
      ctx2.beginPath(); ctx2.arc(sx(r.values[xi]), sy(r.values[yi]), rad, 0, Math.PI * 2); ctx2.fill();
    });
    ctx2.globalAlpha = 1;
  }
  function renderCounts(c) {
    counts.innerHTML = '';
    RESULTS.forEach((k) => { if (k === 'not_fired' && !(c[k] > 0)) return; counts.append(h('span', {}, h('i', { style: `background:${resultColors()[k]}` }), `${k.replace('_', ' ')}: ${c[k] || 0}`)); });
  }
  function renderTable() {
    table.innerHTML = '';
    const fired = results.some((r) => r.fired != null);
    table.append(h('tr', {}, h('th', {}, '#'), ...paramNames.map((n) => h('th', {}, n.replace('glitch.', ''))), h('th', {}, 'result'), h('th', {}, 'response'), ...(fired ? [h('th', {}, 'fired')] : [])));
    results.slice(-60).reverse().forEach((r) => table.append(h('tr', {}, h('td', {}, r.i), ...r.values.map((v) => h('td', {}, v)), h('td', {}, h('span', { class: 'badge ' + r.result }, r.result.replace('_', ' '))), h('td', { class: 'mono' }, r.response || '-'), ...(fired ? [h('td', {}, r.fired == null ? '-' : r.fired)] : []))));
  }
  let drawScheduled = false;
  function scheduleDraw() { if (drawScheduled) return; drawScheduled = true; requestAnimationFrame(() => { drawScheduled = false; draw(); renderTable(); }); }

  el.append(
    h('h2', {}, 'Glitch engine'),
    h('div', { class: 'card' }, engine, presets, unitLine,
      h('div', { class: 'help' }, 'The presets call the library setup (clock glitch: glitch clock on HS2; voltage: glitch_only output and the chosen MOSFET) and keep your capture settings. Anything else, such as glitch.trigger_src or io.hs2, is in the Scope tab.')),
    h('h2', {}, 'Sweep parameters'),
    h('div', { class: 'card' },
      h('div', { class: 'param-row', style: 'color:var(--muted);font-size:11px' }, h('span', {}, 'parameter'), h('span', {}, 'start'), h('span', {}, 'stop'), h('span', {}, 'step'), h('span')),
      rows,
      h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: () => addRow(params[0].path, 0, 10, 1) }, '+ parameter'), h('button', { class: 'btn sm', onclick: defaultRows, title: 'the usual starting sweep for this engine' }, 'Defaults'), h('label', {}, 'repeats'), repIn, h('label', {}, 'order'), orderSel)),
    h('h2', {}, 'Target interaction'),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('label', {}, 'Command'), cmdIn, dataIn),
      h('div', { class: 'row' }, h('label', {}, 'Expected'), expIn, lenIn),
      h('div', { class: 'row' }, h('label', {}, 'Reset via'), resetSel, resetOn),
      h('div', { class: 'row' }, h('label', {}, 'Timeout ms'), timeoutIn),
      h('div', { class: 'help' }, 'normal = valid response equal to expected (with no expected value: any valid response unless the scope timed out) · success = valid response but different · garbled = a reply that is not a valid frame · reset = no response · not fired = the Husky glitch counter did not move, so the point tested nothing'),
      h('div', { class: 'row' }, h('button', { class: 'btn primary', onclick: start }, 'Start sweep'), h('button', { class: 'btn danger', onclick: () => post('/api/capture/stop') }, 'Stop'), h('button', { class: 'btn', onclick: exportCsv }, 'Export CSV')),
      status),
    h('h2', {}, 'Results'),
    h('div', { class: 'card' }, counts,
      h('div', { class: 'row', style: 'margin-top:6px' }, h('label', {}, 'X'), xSel, h('label', {}, 'Y'), ySel),
      canvas,
      h('details', {}, h('summary', {}, 'Recent attempts'), table)),
  );
  xSel.addEventListener('change', draw); ySel.addEventListener('change', draw);
  new ResizeObserver(draw).observe(canvas);

  ctx.on('caps', applyProfile);
  ctx.on('scope-disconnected', () => applyProfile(null));
  ctx.on('glitch', (ev) => { status.textContent = `${ev.state}: point ${ev.point}/${ev.points}` + (ev.error ? ' · ' + ev.error : ''); status.className = 'help mono' + (ev.error ? ' err' : ''); updateAxes(ev.parameters || []); renderCounts(ev.counts || {}); if (ev.state !== 'running') get('/api/glitch/results').then(load).catch(() => {}); });
  ctx.on('glitch_result', (r) => { results.push(r); scheduleDraw(); });
  ctx.on('theme', () => draw());
  function load(res) { results = res.results || []; updateAxes(res.parameters || []); renderCounts(res.counts || {}); draw(); renderTable(); }
  get('/api/glitch/results').then(load).catch(() => {});
  if (ctx.caps) applyProfile(ctx.caps);
}

import { h, get, post, getBinary, toast, themeColors, axisStyle } from './api.js';

export function initAnalysis(ctx, el) {
  const models = ctx.meta.cpa_models;
  const modelSel = h('select', { class: 'flex' }, ...Object.entries(models).map(([k, v]) => h('option', { value: k }, v.label)));
  const tStart = h('input', { type: 'number', value: 0, min: 0, style: 'width:80px' });
  const tEnd = h('input', { type: 'number', placeholder: 'all', style: 'width:80px' });
  const pStart = h('input', { type: 'number', value: 0, min: 0, style: 'width:80px' });
  const pEnd = h('input', { type: 'number', placeholder: 'all', style: 'width:80px' });
  const knownKey = h('input', { class: 'flex mono', placeholder: 'auto (from stored traces) or hex' });
  const reportIn = h('input', { type: 'number', value: 50, min: 1, style: 'width:80px' });
  const exprIn = h('input', { class: 'flex mono', placeholder: 'HW[SBOX[tin[:, b][:, None] ^ g]]', title: ctx.meta.cpa_custom_help || '' });
  const exprRow = h('div', { class: 'row', style: 'display:none' }, h('label', {}, 'Expression'), exprIn);
  const byteBox = h('div', { class: 'pin-checks' });
  const byteChecks = Array.from({ length: 16 }, (_, b) => { const c = h('input', { type: 'checkbox', checked: true }); byteBox.append(h('label', {}, c, ' ' + b)); return c; });
  const bytesRow = h('div', { class: 'row' }, h('label', {}, 'Key bytes'), byteBox, h('button', { class: 'btn sm', onclick: () => byteChecks.forEach((c) => { c.checked = true; }) }, 'all'), h('button', { class: 'btn sm', onclick: () => byteChecks.forEach((c) => { c.checked = false; }) }, 'none'));
  modelSel.addEventListener('change', () => { exprRow.style.display = modelSel.value === 'custom' ? '' : 'none'; const m = models[modelSel.value] || {}; knownKey.placeholder = m.needs_known_key ? 'the first 16 key bytes (hex), recovered with sbox_hw' : 'auto (from stored traces) or hex'; });
  async function exportCpa() { try { const r = await post('/api/analysis/cpa/export', { path: 'cpa_result' }); toast(`Saved ${r.path} and ${r.summary}`, 'ok', 8000); } catch (e) { toast(e.message, 'err', 6000); } }
  const status = h('div', { class: 'help mono' }, 'no attack yet');
  const keyEl = h('div', { class: 'keybytes' });
  const recovered = h('div', { class: 'code', style: 'margin:6px 0' });
  const table = h('table', { class: 'tbl' });
  const conv = h('div', { class: 'miniplot' });
  const corrPlotEl = h('div', { class: 'miniplot', style: 'height:140px' });
  const overlayChk = h('input', { type: 'checkbox', title: 'overlay correlation on the main waveform' });
  let convPlot = null, corrPlot = null, selByte = 0, last = null;
  const btnRun = h('button', { class: 'btn primary', onclick: run }, 'Run CPA');
  const btnStop = h('button', { class: 'btn danger', onclick: () => post('/api/analysis/cpa/stop') }, 'Stop');

  async function run() {
    const pr = pEnd.value ? [+pStart.value, +pEnd.value] : (+pStart.value ? [+pStart.value, 0] : null);
    try {
      const bytes = byteChecks.map((c, b) => (c.checked ? b : null)).filter((b) => b != null);
      const r = await post('/api/analysis/cpa/start', { model: modelSel.value, trace_start: +tStart.value, trace_end: tEnd.value ? +tEnd.value : null, point_range: pr, known_key: knownKey.value || null, report_every: +reportIn.value, bytes: bytes.length === 16 ? null : bytes, expr: modelSel.value === 'custom' ? exprIn.value : null });
      status.textContent = `running on ${r.traces} traces x ${r.samples} samples`;
    } catch (e) { toast(e.message, 'err', 6000); }
  }
  function useZoom() {
    const z = ctx.wave.zoomRange();
    if (z) { pStart.value = z[0]; pEnd.value = z[1]; }
  }

  function render(res) {
    last = res;
    status.textContent = `${res.done ? (res.error ? 'error: ' + res.error : 'done') : 'running'} · ${res.traces_used}/${res.total} traces · ${res.elapsed}s${res.backend === 'cuda' ? ' · GPU' : ''}`;
    status.className = 'help mono' + (res.error ? ' err' : '');
    keyEl.innerHTML = '';
    res.bytes.forEach((b) => {
      const cls = b.known != null ? (b.best === b.known ? 'right' : 'wrong') : '';
      const kb = h('div', { class: `keybyte ${cls}${b.byte === selByte ? ' sel' : ''}`, onclick: () => { selByte = b.byte; render(last); loadCorr(); } },
        b.best.toString(16).padStart(2, '0').toUpperCase(), h('small', {}, b.pge != null ? `pge ${b.pge}` : `${b.top[0].corr.toFixed(2)}`));
      keyEl.append(kb);
    });
    recovered.innerHTML = `best guess: <b>${res.best_key}</b>` + (res.recovered_master_key ? `<br>master key: <b>${res.recovered_master_key}</b>` : '') + (res.known_key ? `<br>known: ${res.known_key}` : '');
    table.innerHTML = '';
    table.append(h('tr', {}, h('th', {}, 'byte'), h('th', {}, 'top guesses (corr)'), h('th', {}, 'sample')));
    res.bytes.forEach((b) => table.append(h('tr', {}, h('td', {}, b.byte), h('td', { class: 'mono' }, b.top.slice(0, 3).map((t) => `${t.guess.toString(16).padStart(2, '0')} (${t.corr.toFixed(3)})`).join('  ')), h('td', {}, b.top[0].sample))));
    renderConv(res);
  }
  function renderConv(res) {
    const hist = res.history || [];
    const x = hist.map((e) => e.traces);
    const hasPge = hist.length && hist[0].pge;
    const series = [{}];
    const data = [x];
    if (hasPge) {
      for (let b = 0; b < 16; b++) { series.push({ stroke: `hsl(${b * 22},70%,60%)`, width: 1, points: { show: false } }); data.push(hist.map((e) => e.pge[b])); }
    } else {
      for (let b = 0; b < 16; b++) { series.push({ stroke: `hsl(${b * 22},70%,60%)`, width: 1, points: { show: false } }); data.push(hist.map((e) => e.max_corr[b])); }
    }
    if (convPlot) convPlot.destroy();
    convPlot = new uPlot({ width: conv.clientWidth || 360, height: conv.clientHeight || 160, legend: { show: false }, cursor: { show: false },
      scales: { x: { time: false } }, axes: [axisStyle({ label: 'traces', labelSize: 14 }), axisStyle({ label: hasPge ? 'PGE' : 'max |corr|', labelSize: 14, size: 45 })], series }, data, conv);
  }
  async function loadCorr() {
    if (!last) return;
    try {
      const { header, samples } = await getBinary(`/api/analysis/cpa/corr/${selByte}`);
      const x = Float64Array.from({ length: samples.length }, (_, i) => i + header.offset);
      if (corrPlot) corrPlot.destroy();
      corrPlot = new uPlot({ width: corrPlotEl.clientWidth || 360, height: corrPlotEl.clientHeight || 140, legend: { show: false }, cursor: { show: false }, scales: { x: { time: false }, y: { range: [-1, 1] } },
        axes: [axisStyle(), axisStyle({ size: 45 })],
        series: [{}, { stroke: themeColors().corr, width: 1, points: { show: false } }] }, [x, samples], corrPlotEl);
      if (overlayChk.checked) ctx.wave.setCorr(samples, header.offset);
    } catch (e) { /* ignore */ }
  }
  // The plots may be drawn while the tab is hidden (results arriving over the WebSocket) or before the window is resized: follow the width of their box so they never stick out of the sidebar.
  const fit = (plot, box, hgt) => { if (plot && box.clientWidth > 0 && plot.width !== box.clientWidth) plot.setSize({ width: box.clientWidth, height: box.clientHeight || hgt }); };
  const ro = new ResizeObserver(() => { fit(convPlot, conv, 160); fit(corrPlot, corrPlotEl, 140); });
  ro.observe(conv); ro.observe(corrPlotEl);
  overlayChk.addEventListener('change', () => { if (!overlayChk.checked) ctx.wave.setCorr(null); else loadCorr(); });

  el.append(
    h('h2', {}, 'CPA attack'),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('label', {}, 'Leakage model'), modelSel),
      exprRow, bytesRow,
      h('div', { class: 'row' }, h('label', {}, 'Traces'), tStart, 'to', tEnd),
      h('div', { class: 'row' }, h('label', {}, 'Samples'), pStart, 'to', pEnd, h('button', { class: 'btn sm', onclick: useZoom }, 'use zoom')),
      h('div', { class: 'row' }, h('label', {}, 'Known key'), knownKey),
      h('div', { class: 'row' }, h('label', {}, 'Report every'), reportIn, h('span', { class: 'muted' }, 'traces')),
      h('div', { class: 'row' }, btnRun, btnStop, h('button', { class: 'btn', onclick: exportCpa, title: 'per-guess maxima, best correlation traces (.npz) and the summary (.json) in the data folder' }, 'Export result'), h('label', {}, overlayChk, ' overlay corr')),
      status),
    h('h2', {}, 'Result'),
    h('div', { class: 'card' }, keyEl, recovered,
      h('div', { class: 'help' }, 'Convergence (PGE per byte if the key is known, else max correlation):'), conv,
      h('div', { class: 'help' }, 'Correlation vs sample for the selected byte (click a byte above):'), corrPlotEl,
      h('details', {}, h('summary', {}, 'Top guesses table'), table)),
  );
  // ---------- leakage assessment and trace conditioning ----------
  const lkOut = h('div', { class: 'help mono' });
  const lkPlotEl = h('div', { class: 'miniplot', style: 'height:140px' });
  let lkPlot = null;
  function plotSeries(series, step, label, yRange) {
    const x = Float64Array.from({ length: series.length }, (_, i) => i * step);
    if (lkPlot) lkPlot.destroy();
    lkPlot = new uPlot({ width: lkPlotEl.clientWidth || 360, height: lkPlotEl.clientHeight || 140, legend: { show: false }, cursor: { show: false }, scales: { x: { time: false }, y: yRange ? { range: yRange } : {} },
      axes: [axisStyle({ label: 'sample', labelSize: 14 }), axisStyle({ label, labelSize: 14, size: 50 })], series: [{}, { stroke: themeColors().corr, width: 1, points: { show: false } }] }, [x, Float64Array.from(series)], lkPlotEl);
    if (lkOverlay.checked) ctx.wave.setCorr(Float32Array.from(series.map((v) => Math.max(-1, Math.min(1, v / (yRange ? yRange[1] : Math.max(...series.map(Math.abs)) || 1))))), 0);
  }
  const lkOverlay = h('input', { type: 'checkbox', title: 'overlay the result on the main waveform (scaled to -1..1)' });
  const fixedIn = h('input', { class: 'flex mono', placeholder: 'fixed plaintext hex (default: the tvla capture text)' });
  const snrByte = h('input', { type: 'number', value: 0, min: 0, max: 15, style: 'width:60px' });
  const snrCls = h('select', {}, h('option', { value: 'sbox_hw' }, 'S-box HW (known key)'), h('option', { value: 'byte' }, 'plaintext byte'));
  async function runTtest() {
    try { const r = await post('/api/analysis/ttest', { fixed_text: fixedIn.value || null }); lkOut.textContent = `t-test on ${r.fixed} fixed and ${r.random} random traces: max |t| ${r.max_abs_t.toFixed(1)} at sample ${r.argmax}, ${r.over_threshold} samples over ${r.threshold}: ${r.leaks ? 'LEAKS' : 'no leak detected'}`; lkOut.className = 'help mono' + (r.leaks ? ' warn' : ''); plotSeries(r.series, r.step, 't'); } catch (e) { toast(e.message, 'err', 8000); }
  }
  async function runSnr() {
    try { const r = await post('/api/analysis/snr', { byte: +snrByte.value, classes: snrCls.value, known_key: knownKey.value || null }); lkOut.textContent = `SNR for byte ${r.byte} (${r.classes}) over ${r.traces} traces: peak ${r.max_snr.toFixed(3)} at sample ${r.argmax}`; lkOut.className = 'help mono'; plotSeries(r.series, r.step, 'SNR'); } catch (e) { toast(e.message, 'err', 8000); }
  }
  const alRef = h('input', { type: 'number', value: 0, style: 'width:70px', title: 'reference trace index' });
  const alStart = h('input', { type: 'number', value: 0, min: 0, style: 'width:80px' });
  const alEnd = h('input', { type: 'number', placeholder: 'end', style: 'width:80px' });
  const alShift = h('input', { type: 'number', value: 50, min: 1, style: 'width:70px', title: 'largest shift searched, samples' });
  async function runAlign(apply) {
    try { const r = await post('/api/analysis/align', { reference: +alRef.value, start: +alStart.value, end: alEnd.value ? +alEnd.value : 0, max_shift: +alShift.value, apply }); lkOut.textContent = `${apply ? 'aligned' : 'alignment preview:'} ${r.traces} traces to trace ${r.reference} over samples ${r.start} to ${r.end}: shifts ${r.shift_min} to ${r.shift_max} (mean ${r.shift_mean.toFixed(1)}), ${r.moved} would move${apply ? ' and did' : ''}`; lkOut.className = 'help mono'; if (apply) ctx.refreshStatus(); } catch (e) { toast(e.message, 'err', 8000); }
  }
  const fKind = h('select', {}, ...['lowpass', 'highpass', 'bandpass', 'moving_average'].map((k) => h('option', { value: k }, k.replace('_', ' '))));
  const fCut = h('input', { type: 'number', value: 0.1, step: 'any', style: 'width:80px', title: 'cutoff as a fraction of the sample rate (0 to 0.5), or samples for the moving average' });
  const fCut2 = h('input', { type: 'number', placeholder: 'upper', step: 'any', style: 'width:80px', title: 'bandpass upper cutoff' });
  async function runFilter() {
    if (!confirm('Filter every stored trace in place? Export the traces first if you want the originals.')) return;
    try { const r = await post('/api/analysis/filter', { kind: fKind.value, cutoff: +fCut.value, cutoff2: fCut2.value ? +fCut2.value : null }); lkOut.textContent = `${r.kind} applied to ${r.traces} traces (${r.taps} taps)`; lkOut.className = 'help mono'; ctx.refreshStatus(); } catch (e) { toast(e.message, 'err', 8000); }
  }
  async function runSpectrum() {
    try { const r = await post('/api/analysis/spectrum', {}); const x = Float64Array.from(r.freq); if (lkPlot) lkPlot.destroy(); lkPlot = new uPlot({ width: lkPlotEl.clientWidth || 360, height: lkPlotEl.clientHeight || 140, legend: { show: false }, cursor: { show: false }, scales: { x: { time: false } }, axes: [axisStyle({ label: r.sample_rate ? 'Hz' : 'cycles per sample', labelSize: 14 }), axisStyle({ label: 'dB', labelSize: 14, size: 50 })], series: [{}, { stroke: themeColors().corr, width: 1, points: { show: false } }] }, [x, Float64Array.from(r.db)], lkPlotEl); lkOut.textContent = `spectrum of the mean trace: peak at ${r.peak_hz != null ? (r.sample_rate ? (r.peak_hz / 1e6).toFixed(3) + ' MHz' : r.peak_hz.toFixed(4) + ' cycles per sample') : '?'}`; lkOut.className = 'help mono'; } catch (e) { toast(e.message, 'err', 8000); }
  }
  // differential fault analysis: from the last glitch sweep's replies, or from pasted ciphertexts (correct first)
  const dfaIn = h('textarea', { class: 'mono', rows: 4, placeholder: 'one ciphertext per line in hex, the correct one first (or leave empty to use the glitch sweep)', style: 'width:100%' });
  const dfaOut = h('div', { class: 'help mono' });
  const dfaKey = h('div', { class: 'code', style: 'margin:6px 0;display:none' });
  async function runDfa() {
    try {
      const lines = dfaIn.value.split(/\s+/).map((x) => x.trim()).filter(Boolean);
      const body = lines.length ? { source: 'ciphertexts', correct: lines[0], faulty: lines.slice(1) } : { source: 'glitch' };
      const r = await post('/api/analysis/dfa', body);
      const cols = r.columns.map((c) => `column ${c.column}: ${c.key_bytes ? 'solved' : c.candidates + ' candidates'} from ${c.pairs} fault${c.pairs === 1 ? '' : 's'}`).join(' · ');
      dfaOut.textContent = `${r.faulty} faulty of ${r.replies} replies, ${r.faulty_used} used · ${cols}` + (r.unusable.length ? ` · ${r.unusable.length} unusable (${r.unusable[0].why})` : '');
      dfaOut.className = 'help mono';
      dfaKey.style.display = '';
      dfaKey.innerHTML = r.key ? `round key 10: ${r.round_key_10}<br>master key: <b>${r.key}</b>` : `round key 10 so far: ${r.round_key_10_partial}<br>${4 - r.solved_columns} column${4 - r.solved_columns === 1 ? '' : 's'} still open: collect more faults (about two per column), sweep ext_offset over the last AES rounds`;
    } catch (e) { dfaOut.textContent = e.message; dfaOut.className = 'help mono err'; dfaKey.style.display = 'none'; }
  }
  el.append(
    h('h2', {}, 'Differential fault analysis'),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: runDfa, title: 'AES-128, one faulted byte entering round 9' }, 'Run DFA'), h('span', { class: 'muted' }, 'from the glitch sweep (command p, fixed plaintext, expected = correct ciphertext, checker aes_dfa) or the ciphertexts below')),
      dfaIn, dfaOut, dfaKey,
      h('div', { class: 'help' }, 'A glitch that corrupts one byte of the AES state between the last two MixColumns changes exactly four ciphertext bytes, one column. Two such faults per column, about eight in all, give the tenth round key; the key schedule run backwards gives the master key. The AES fault campaign button in the Glitch tab sets the sweep up.')),
    h('h2', {}, 'Leakage assessment'),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: runTtest, title: 'capture with text mode tvla first' }, 'TVLA t-test'), fixedIn),
      h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: runSnr }, 'SNR'), h('label', {}, 'byte'), snrByte, snrCls),
      h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: runSpectrum }, 'Spectrum'), h('label', {}, lkOverlay, ' overlay on waveform')),
      h('div', { class: 'help' }, 'TVLA: |t| over 4.5 at any sample means the implementation leaks (capture with text mode tvla so fixed and random plaintexts alternate). SNR: how well one key byte stands out per sample under the known key.'),
      lkOut, lkPlotEl),
    h('h2', {}, 'Trace conditioning'),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('label', {}, 'Align to trace'), alRef, h('label', {}, 'samples'), alStart, 'to', alEnd, h('label', {}, 'max shift'), alShift),
      h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: () => runAlign(false) }, 'Preview shifts'), h('button', { class: 'btn sm', onclick: () => runAlign(true) }, 'Align in place'), h('button', { class: 'btn sm', onclick: () => { const z = ctx.wave.zoomRange(); if (z) { alStart.value = z[0]; alEnd.value = z[1]; } } }, 'use zoom')),
      h('div', { class: 'row' }, h('label', {}, 'Filter'), fKind, fCut, fCut2, h('button', { class: 'btn sm', onclick: runFilter }, 'Apply in place')),
      h('div', { class: 'help' }, 'Alignment shifts each trace so a window matches the reference by cross-correlation (jittered triggers). Filters are windowed-sinc FIR with cutoffs as a fraction of the sample rate; both change the stored traces, so export first if you need the originals.')),
  );
  ctx.on('cpa', (ev) => { render(ev); if (ev.done) loadCorr(); });
  ctx.on('theme', () => { if (last) { render(last); loadCorr(); } });
  ctx.on('status', (st) => { if (st.cpa && !last) get('/api/analysis/cpa').then((r) => { if (r && r.bytes) { render(r); loadCorr(); } }).catch(() => {}); });
}

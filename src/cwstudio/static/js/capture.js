import { h, get, put, post, del, upload, downloadUrl, toast, fmtBytes, fmtNum } from './api.js';

// the capture settings a model has (profile.adc decides which rows show): path, label, hint
const ADC_ROWS = [
  ['adc.samples', 'Samples', 'samples per trace'],
  ['adc.presamples', 'Presamples', 'samples kept before the trigger'],
  ['adc.offset', 'Offset', 'ADC cycles skipped after the trigger'],
  ['adc.decimate', 'Decimate', 'keep one sample in N'],
  ['adc.bits_per_sample', 'Bits', '12 for resolution, 8 for more samples and faster streaming'],
  ['adc.segments', 'Segments', 'traces per arm: the fast way to capture on a Husky'],
  ['adc.stream', 'Stream', 'no sample limit, about 10 MS/s'],
];

export function initCapture(ctx, el) {
  const countIn = h('input', { id: 'cap-count', type: 'number', value: 1000, min: 0, class: 'flex', title: '0 = continuous' });
  const modeSel = h('select', { class: 'flex' }, h('option', { value: 'simpleserial' }, 'SimpleSerial: send data, read reply'), h('option', { value: 'trigger_only' }, 'Trigger only: arm and wait'));
  const keyMode = h('select', { class: 'flex' }, h('option', { value: 'fixed' }, 'fixed'), h('option', { value: 'random' }, 'random'));
  const textMode = h('select', { class: 'flex' }, h('option', { value: 'random' }, 'random'), h('option', { value: 'fixed' }, 'fixed'), h('option', { value: 'counter' }, 'counter'));
  const keyIn = h('input', { class: 'flex mono', value: '2b7e151628aed2a6abf7158809cf4f3c' });
  const textIn = h('input', { class: 'flex mono', value: '00112233445566778899aabbccddeeff' });
  const storeChk = h('input', { type: 'checkbox', checked: true });
  const clearChk = h('input', { type: 'checkbox' });
  const ackChk = h('input', { type: 'checkbox', checked: true });
  const rateIn = h('input', { type: 'number', value: 25, min: 1, max: 60, style: 'width:70px', title: 'max displayed traces per second' });
  const throttleIn = h('input', { type: 'number', value: 0, min: 0, style: 'width:70px', title: 'max captures per second (0 = unlimited)' });
  const diskIn = h('input', { class: 'flex mono', placeholder: 'empty = memory only; a name or path writes <name>_waves.npy as traces arrive', title: 'for streamed or very long captures that would not fit in memory: every trace goes to disk as it arrives; untick store traces to keep memory flat, then import the set later' });
  const prog = h('progress', { value: 0, max: 1 });
  const progTxt = h('div', { class: 'help mono' }, 'idle');
  const banner = h('div', { class: 'help warn', style: 'display:none' });
  const adcCard = h('div', { class: 'card' });
  const adcHint = h('div', { class: 'help' }, 'Connect a scope to see its capture settings.');
  let profile = null, adcInputs = {};

  // the model's capture settings, written straight to the scope; the limit line follows bits_per_sample and stream
  async function loadAdc() {
    if (!profile) return;
    try {
      const flat = {};
      const walk = (ns) => (ns || []).forEach((n) => { if (n.kind === 'group') walk(n.children); else flat[n.path] = n.value; });
      walk(await get('/api/scope/settings'));
      Object.entries(adcInputs).forEach(([path, inp]) => { if (!(path in flat)) return; if (inp.type === 'checkbox') inp.checked = !!flat[path]; else inp.value = flat[path]; });
      const adc = profile.adc, live = adc.live || {};
      const bits = flat['adc.bits_per_sample'] || adc.bits;
      const max = live.max_samples_now || adc.max_samples[bits] || adc.max_samples[adc.bits];
      if (adcInputs['adc.samples']) adcInputs['adc.samples'].max = flat['adc.stream'] ? 100000000 : max;
      adcHint.textContent = `${adc.bits_choices.length > 1 ? bits + '-bit, ' : ''}up to ${fmtNum(max)} samples per capture` + (flat['adc.stream'] ? ' (streaming: no limit, about 10 MS/s)' : '') + (adc.segments && flat['adc.segments'] > 1 ? `; ${flat['adc.segments']} segments per arm` : '') + '. ' + (adc.note || '');
    } catch (e) { /* no scope */ }
  }
  function buildAdc(caps) {
    profile = caps && caps.profile ? caps.profile : null;
    adcCard.innerHTML = ''; adcInputs = {};
    if (!profile) { adcCard.append(adcHint); adcHint.textContent = 'Connect a scope to see its capture settings.'; return; }
    const adc = profile.adc;
    const show = { 'adc.samples': true, 'adc.presamples': adc.presamples, 'adc.offset': adc.offset, 'adc.decimate': adc.decimate, 'adc.bits_per_sample': adc.bits_choices.length > 1, 'adc.segments': adc.segments, 'adc.stream': adc.stream };
    ADC_ROWS.filter(([p]) => show[p]).forEach(([path, label, hint]) => {
      let inp;
      if (path === 'adc.stream') inp = h('input', { type: 'checkbox' });
      else if (path === 'adc.bits_per_sample') inp = h('select', {}, ...adc.bits_choices.map((b) => h('option', { value: b }, b)));
      else inp = h('input', { type: 'number', min: path === 'adc.samples' || path === 'adc.decimate' || path === 'adc.segments' ? 1 : 0, class: 'flex' });
      inp.addEventListener('change', async () => {
        const value = inp.type === 'checkbox' ? inp.checked : +inp.value;
        try { await put('/api/scope/settings', { path, value }); if (ctx.scopeTree && ctx.scopeTree.refreshValues) ctx.scopeTree.refreshValues(); } catch (e) { toast(e.message, 'err', 7000); }
        loadAdc();
      });
      adcInputs[path] = inp;
      adcCard.append(h('div', { class: 'row' }, h('label', { title: hint }, label), inp, h('span', { class: 'muted' }, hint)));
    });
    adcCard.append(adcHint);
    loadAdc();
  }
  ctx.on('caps', buildAdc);
  ctx.on('scope-disconnected', () => buildAdc(null));
  ctx.on('setting', (ev) => { if (ev.target === 'scope' && String(ev.path).startsWith('adc.')) loadAdc(); });
  async function clearErrors() { try { await put('/api/scope/settings', { path: 'adc.errors', value: 0 }); banner.style.display = 'none'; } catch (e) { toast(e.message, 'err'); } }
  const summary = h('div', { class: 'kv' });
  const trigLine = h('div', { class: 'help mono', title: 'set in the Interfaces tab (Triggers) or the Scope tab' });
  ctx.on('trigger-summary', (txt) => { trigLine.textContent = txt || ''; });
  const fmtSel = h('select', {}, h('option', { value: 'npz' }, 'NumPy .npz'), h('option', { value: 'cwp' }, 'ChipWhisperer project .cwp'), h('option', { value: 'csv' }, 'CSV'), h('option', { value: 'npy' }, 'NumPy .npy set'));
  const pathIn = h('input', { class: 'flex mono', value: 'traces', placeholder: 'file name or absolute path' });
  const importPath = h('input', { class: 'flex mono', placeholder: 'path to .npz, .cwp, .npy or .zip on the Studio machine' });
  const importFile = h('input', { type: 'file', accept: '.npz,.npy,.cwp,.zip', class: 'flex' });

  function params(extra) {
    return Object.assign({
      count: +countIn.value, mode: modeSel.value, key_mode: keyMode.value, text_mode: textMode.value,
      key: keyIn.value, text: textIn.value, store: storeChk.checked, clear: clearChk.checked, ack: ackChk.checked,
      display_rate: +rateIn.value, max_rate: +throttleIn.value, disk: diskIn.value.trim() || null,
    }, extra || {});
  }
  async function start(extra) {
    try { await post('/api/capture/start', params(extra)); } catch (e) { toast(e.message, 'err', 6000); }
  }
  async function single() {
    try { await post('/api/capture/single', params({ clear: false })); } catch (e) { toast(e.message, 'err', 6000); }
  }
  async function stop() { try { await post('/api/capture/stop'); } catch (e) { toast(e.message, 'err'); } }

  async function exportTraces() {
    try { const r = await post('/api/traces/export', { path: pathIn.value, format: fmtSel.value }); toast(`Exported ${r.count} traces to ${r.path}`, 'ok', 7000); } catch (e) { toast(e.message, 'err', 7000); }
  }
  function download() { downloadUrl(`/api/traces/download/${fmtSel.value}`); }
  async function importTraces() {
    try {
      let r;
      if (importFile.files.length) r = await upload('/api/traces/import/upload', importFile.files[0], { replace: true });
      else if (importPath.value) r = await post('/api/traces/import', { path: importPath.value, replace: true });
      else throw new Error('choose a file');
      toast(`Imported ${r.imported} traces`, 'ok'); ctx.refreshStatus();
    } catch (e) { toast(e.message, 'err', 7000); }
  }
  async function clearTraces() { if (!confirm('Discard all traces in memory?')) return; await del('/api/traces'); ctx.refreshStatus(); }

  el.append(
    h('h2', {}, 'Capture settings'),
    adcCard,
    h('h2', {}, 'Run'),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('label', {}, 'Traces'), countIn, h('span', { class: 'muted' }, '0 = continuous')),
      h('div', { class: 'row' }, h('label', {}, 'Mode'), modeSel),
      h('div', { class: 'row' }, h('label', {}, 'Key'), keyMode, keyIn),
      h('div', { class: 'row' }, h('label', {}, 'Plaintext'), textMode, textIn),
      h('div', { class: 'row' }, h('label', {}, storeChk, ' store traces'), h('label', {}, clearChk, ' clear first'), h('label', {}, ackChk, ' expect ack')),
      h('div', { class: 'row' }, h('label', {}, 'Display fps'), rateIn, h('label', {}, 'Max cap/s'), throttleIn),
      h('div', { class: 'row' }, h('label', { title: 'streamed and very long captures' }, 'Save to disk'), diskIn),
      h('div', { class: 'row' },
        h('button', { class: 'btn', onclick: single }, 'Single'),
        h('button', { class: 'btn primary', onclick: () => start() }, 'Run'),
        h('button', { class: 'btn', onclick: () => start({ count: 0 }) }, 'Continuous'),
        h('button', { class: 'btn danger', onclick: stop }, 'Stop')),
      prog, progTxt, trigLine, banner),
    h('h2', {}, 'Trace set'),
    h('div', { class: 'card' }, summary,
      h('div', { class: 'row', style: 'margin-top:8px' }, h('label', {}, 'Export'), fmtSel, pathIn),
      h('div', { class: 'row' }, h('button', { class: 'btn', onclick: exportTraces }, 'Save on Studio machine'), h('button', { class: 'btn', onclick: download }, 'Download in browser')),
      h('div', { class: 'row' }, h('label', {}, 'Import'), importFile),
      h('div', { class: 'row' }, h('label', {}, ''), importPath, h('button', { class: 'btn sm', onclick: importTraces }, 'Import')),
      h('div', { class: 'row' }, h('button', { class: 'btn danger sm', onclick: clearTraces }, 'Clear traces'))),
  );

  function updateSummary(t) {
    summary.innerHTML = '';
    [['Traces', t.count], ['Samples', t.samples + (t.uniform ? '' : ' (mixed lengths)')], ['Memory', fmtBytes(t.memory_bytes || 0)]]
      .forEach(([k, v]) => summary.append(h('span', { class: 'k' }, k), h('span', {}, String(v))));
  }
  ctx.on('status', (st) => { updateSummary(st.traces); if (st.job && st.job.name === 'capture') updateProgress(st.job, st.job.running ? 'running' : (st.job.error ? 'error' : 'done')); });
  ctx.on('traces', updateSummary);
  ctx.on('capture', (ev) => updateProgress(ev, ev.state));
  function updateProgress(p, state) {
    const target = p.target || 0;
    prog.max = target || 1; prog.value = target ? p.done : (state === 'running' ? 0 : 1);
    prog.removeAttribute('value'); if (target || state !== 'running') prog.value = target ? p.done : 1;
    const rate = p.rate ? ` · ${p.rate}/s` : '';
    const seg = p.segments > 1 ? ` · ${p.segments} segments per arm` : '';
    const disk = p.disk ? ` · ${fmtBytes(p.disk.bytes || 0)} on disk${p.disk.closed ? ' in ' + p.disk.path : ''}` : '';
    progTxt.textContent = `${state}: ${p.done}${target ? '/' + target : ''} traces${rate}${seg}${disk} · ${p.timeouts || 0} timeouts · ${p.elapsed || 0}s` + (p.error ? ` · ${p.error}` : '');
    progTxt.className = 'help mono' + (state === 'error' ? ' err' : '');
    if (p.hw_errors) {
      banner.innerHTML = '';
      banner.append(h('span', {}, `The scope reported a capture error${p.hw_error_count > 1 ? ` (${p.hw_error_count} captures)` : ''}: ${p.hw_errors}. `), h('button', { class: 'btn sm', onclick: clearErrors }, 'Clear errors'));
      banner.style.display = '';
    }
  }
  if (ctx.caps) buildAdc(ctx.caps);
  return { single, start, stop, params };
}

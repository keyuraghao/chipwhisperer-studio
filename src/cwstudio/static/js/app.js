import { get, put, post, getBinary, Socket, Emitter, toast, h } from './api.js';
import { Waveform } from './waveform.js';
import { SettingsTree } from './settings.js';
import { initConnect } from './connect.js';
import { initTarget } from './target.js';
import { initCapture } from './capture.js';
import { initAnalysis } from './analysis.js';
import { initGlitch } from './glitch.js';
import { initHelp } from './help.js';
import { initFirmware } from './firmware.js';
import { initNotebook } from './notebook.js';
import { initNotes, initCalc, initSelectionStats } from './tools.js';

const ctx = new Emitter();
ctx.status = null;
ctx.meta = null;

// ---------- panel headers ----------
const PANELS = {
  connect: ['Connect', 'Pick a ChipWhisperer (or the simulator) and the target protocol.'],
  scope: ['Scope', 'Every setting of the connected scope, read back from the hardware.'],
  target: ['Target', 'Program firmware and talk to the target over serial or SimpleSerial.'],
  firmware: ['Firmware', 'Build ChipWhisperer firmware with GCC or clang, no toolchain setup needed.'],
  capture: ['Capture', 'Record power traces while the waveform updates live.'],
  analysis: ['Analysis', 'Recover the AES key with correlation power analysis.'],
  notebook: ['Notebook', 'Run Python cell by cell with the connected hardware; traces land in the Capture tab.'],
  glitch: ['Glitch', 'Sweep glitch parameters and map where the target misbehaves.'],
  notes: ['Notes', 'A text pad for keys, settings that worked and to-dos. Saved automatically.'],
  calc: ['Calculator', 'Quick maths plus statistics of whatever you select.'],
  help: ['Help', 'Quick start, shortcuts, remote use and AI agent (MCP) setup.'],
};
for (const [k, [title, sub]] of Object.entries(PANELS)) {
  const el = document.getElementById('panel-' + k);
  if (el) el.append(h('div', { class: 'panel-head' }, h('h1', {}, title), h('p', {}, sub)));
}

// ---------- theme ----------
function currentTheme() { return document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'); }
const SUN = '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON = '<svg class="i" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
const themeBtn = document.getElementById('theme-toggle');
function paintThemeBtn() { themeBtn.innerHTML = currentTheme() === 'dark' ? SUN : MOON; }
paintThemeBtn();
themeBtn.addEventListener('click', () => {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('cw.theme', next); } catch (e) { /* ignore */ }
  paintThemeBtn();
  ctx.wave.rebuild(); ctx.wave.render && ctx.wave.render(true);
  ctx.emit('theme', next);
});

// ---------- tabs ----------
ctx.showTab = (name) => {
  document.querySelectorAll('#tabs .tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('#sidebar .panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-' + name));
  const nbMode = name === 'notebook';
  const main = document.getElementById('main');
  if (main.classList.contains('nb-mode') !== nbMode) { main.classList.toggle('nb-mode', nbMode); if (!nbMode && ctx.wave) setTimeout(() => ctx.wave.resize(), 30); }
  try { localStorage.setItem('cw.tab', name); } catch (e) { /* ignore */ }
  ctx.emit('tab', name);
};
document.querySelectorAll('#tabs .tab').forEach((b) => b.addEventListener('click', () => ctx.showTab(b.dataset.tab)));

// ---------- log drawer ----------
const logBody = document.getElementById('log-body');
const logFilter = document.getElementById('log-filter');
const logLines = [];
let logCount = 0;
function addLog(ev, replay = false) {
  const t = new Date((ev.ts || Date.now() / 1000) * 1000).toLocaleTimeString();
  const line = h('div', { class: 'log-line ' + (ev.level || 'INFO') }, h('span', { class: 't' }, t), h('span', { class: 'lv' }, ev.level || ''), h('span', { class: 'muted' }, (ev.logger || '').replace('ChipWhisperer ', 'cw.') + ' '), ev.msg || '');
  logLines.push({ el: line, text: (ev.msg || '') + (ev.logger || '') });
  if (logLines.length > 1500) logLines.shift().el.remove();
  if (logFilter.value && !line.textContent.toLowerCase().includes(logFilter.value.toLowerCase())) line.style.display = 'none';
  const atBottom = logBody.scrollHeight - logBody.scrollTop - logBody.clientHeight < 30;
  logBody.append(line);
  if (atBottom) logBody.scrollTop = logBody.scrollHeight;
  logCount++;
  document.getElementById('log-count').textContent = `${logCount}`;
  if (!replay && (ev.level === 'ERROR' || ev.level === 'CRITICAL') && !document.hidden) toast(ev.msg, 'err', 6000);
}
logFilter.addEventListener('input', () => { const f = logFilter.value.toLowerCase(); logLines.forEach((l) => { l.el.style.display = !f || l.text.toLowerCase().includes(f) ? '' : 'none'; }); });
document.getElementById('log-clear').addEventListener('click', () => { logBody.innerHTML = ''; logLines.length = 0; });
document.getElementById('log-toggle').addEventListener('click', () => { document.getElementById('app').classList.toggle('bottom-collapsed'); setTimeout(() => ctx.wave.resize(), 50); });

// ---------- waveform ----------
ctx.wave = new Waveform(document.getElementById('wave-plot'), document.getElementById('wave-toolbar'), document.getElementById('wave-footer'));
ctx.wave.onBrowse = async (i) => {
  try { const { header, samples } = await getBinary(`/api/traces/${i}`); ctx.wave.setBrowse(samples, header); } catch (e) { /* no such trace */ }
};
ctx.wave.onNeedStats = async () => {
  try {
    const { header, samples } = await getBinary('/api/traces/stats');
    if (!header.samples) return;
    const n = header.samples, st = {};
    header.fields.forEach((f, i) => { st[f] = samples.subarray(i * n, (i + 1) * n); });
    ctx.wave.setStats(st);
  } catch (e) { /* ignore */ }
};

// ---------- settings helpers ----------
ctx.saveSetting = async (which, path, value) => {
  const r = await put(`/api/${which}/settings`, { path, value });
  if (which === 'scope' && (path.includes('freq') || path.includes('adc_src') || path.includes('clk'))) setTimeout(updateSampleRate, 300);
  return r.value;
};
async function updateSampleRate() {
  try {
    const nodes = await get('/api/scope/settings');
    const find = (ns, p) => { for (const n of ns) { if (n.path === p) return n; if (n.children) { const r = find(n.children, p); if (r) return r; } } return null; };
    const n = find(nodes, 'clock.adc_freq') || find(nodes, 'adc.clk_freq') || find(nodes, 'clock.adc_rate');
    ctx.wave.sampleRate = n && typeof n.value === 'number' && n.value > 0 ? n.value : null;
    ctx.scopeSettings = nodes;
    ctx.wave.updateFooter();
  } catch (e) { ctx.wave.sampleRate = null; }
}

// ---------- scope panel ----------
function initScope(el) {
  const filter = h('input', { class: 'flex', placeholder: 'filter settings…' });
  const treeEl = h('div');
  const tree = new SettingsTree(treeEl, { load: () => get('/api/scope/settings'), save: (p, v) => ctx.saveSetting('scope', p, v) });
  tree.onError = (p, m) => toast(`${p}: ${m}`, 'err', 6000);
  filter.addEventListener('input', () => tree.setFilter(filter.value));
  const action = async (a, label) => { try { const r = await post(`/api/scope/action/${a}`); toast(label + (r.timeout ? ' (timeout)' : ' done'), r.timeout ? 'warn' : 'ok'); tree.refreshValues(); } catch (e) { toast(e.message, 'err', 6000); } };
  el.append(
    h('div', { class: 'row' }, filter, h('button', { class: 'btn sm', onclick: () => tree.refresh() }, '↻')),
    h('div', { class: 'row' },
      h('button', { class: 'btn sm', onclick: () => action('default_setup', 'default_setup()') }, 'default_setup()'),
      h('button', { class: 'btn sm', onclick: () => action('arm_capture', 'arm+capture') , title: 'arm the scope and wait for a trigger without talking to the target' }, 'test trigger'),
      h('button', { class: 'btn sm', onclick: () => action('reset_fpga', 'reset FPGA') }, 'reset FPGA')),
    h('div', { class: 'help' }, 'Hover a name for its documentation. Enter or Tab applies a value; Esc reverts. Values are read back from the hardware after every change.'),
    treeEl,
  );
  ctx.on('scope-connected', () => { tree.refresh(); updateSampleRate(); });
  ctx.on('scope-disconnected', () => tree.refresh());
  ctx.on('setting', (ev) => { if (ev.target === 'scope') tree.refreshValues(); });
  ctx.on('tab', (t) => { if (t === 'scope' && ctx.status && ctx.status.scope.connected) tree.refreshValues(); });
  return tree;
}

// ---------- header / status ----------
function setChip(id, cls, text) { const c = document.getElementById(id); c.className = 'chip ' + cls; c.querySelector('.txt').textContent = text; }
let prevScope = false, prevTarget = false;
function applyStatus(st) {
  ctx.status = st;
  const sc = st.scope, tg = st.target;
  setChip('chip-scope', sc.connected ? 'on' : '', sc.connected ? `${sc.name || sc.type}${sc.sn ? ' · ' + sc.sn : ''}` : 'No scope');
  setChip('chip-target', tg.connected ? 'on' : '', tg.connected ? tg.type : 'No target');
  const job = st.job;
  if (job && job.running) setChip('chip-job', 'busy', `${job.name}: ${job.done != null ? job.done + (job.target ? '/' + job.target : '') : (job.point != null ? job.point + '/' + job.points : '')}${job.rate ? ' · ' + job.rate + '/s' : ''}`);
  else if (job && job.error) setChip('chip-job', 'err', `${job.name} error`);
  else setChip('chip-job', '', 'Idle');
  document.getElementById('btn-stop').disabled = !(job && job.running);
  document.getElementById('btn-run').disabled = !sc.connected || (job && job.running);
  document.getElementById('btn-single').disabled = !sc.connected || (job && job.running);
  document.getElementById('trace-count').textContent = `${st.traces.count} traces`;
  ctx.wave.setTraceCount(st.traces.count);
  if (sc.connected !== prevScope) { prevScope = sc.connected; ctx.emit(sc.connected ? 'scope-connected' : 'scope-disconnected'); }
  if (tg.connected !== prevTarget) { prevTarget = tg.connected; ctx.emit(tg.connected ? 'target-connected' : 'target-disconnected'); }
  ctx.emit('status', st);
}
ctx.refreshStatus = async () => { try { applyStatus(await get('/api/status')); } catch (e) { /* server down */ } };

// ---------- boot ----------
async function boot() {
  ctx.meta = await get('/api/meta');
  document.title = `ChipWhisperer Studio ${ctx.meta.version}`;
  document.getElementById('app-version').textContent = 'v' + ctx.meta.version;
  initConnect(ctx, document.getElementById('panel-connect'));
  ctx.scopeTree = initScope(document.getElementById('panel-scope'));
  ctx.target = initTarget(ctx, document.getElementById('panel-target'));
  ctx.firmware = initFirmware(ctx, document.getElementById('panel-firmware'));
  ctx.capture = initCapture(ctx, document.getElementById('panel-capture'));
  initAnalysis(ctx, document.getElementById('panel-analysis'));
  initGlitch(ctx, document.getElementById('panel-glitch'));
  ctx.notebook = initNotebook(ctx, document.getElementById('panel-notebook'), document.getElementById('nb-view'));
  initNotes(ctx, document.getElementById('panel-notes'));
  initCalc(ctx, document.getElementById('panel-calc'));
  initSelectionStats(ctx, document.getElementById('sel-stats'));
  initHelp(ctx, document.getElementById('panel-help'));

  document.getElementById('btn-single').addEventListener('click', () => ctx.capture.single());
  document.getElementById('btn-run').addEventListener('click', () => ctx.capture.start());
  document.getElementById('btn-stop').addEventListener('click', () => ctx.capture.stop());
  document.addEventListener('keydown', (e) => {
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
    if (e.key === 's' || e.key === 'S') ctx.capture.single();
    else if (e.key === 'r' || e.key === 'R') ctx.capture.start();
    else if (e.key === 'Escape') ctx.capture.stop();
    else if (e.key === ' ') { e.preventDefault(); ctx.wave.pauseBtn.click(); }
    else if (e.key === 'ArrowLeft' && ctx.wave.mode === 'browse') ctx.wave.gotoIndex(+ctx.wave.idxInput.value - 1);
    else if (e.key === 'ArrowRight' && ctx.wave.mode === 'browse') ctx.wave.gotoIndex(+ctx.wave.idxInput.value + 1);
  });

  const sock = new Socket();
  ctx.sock = sock;
  const wsEl = document.getElementById('ws-state');
  sock.on('open', () => { wsEl.className = 'ws on'; ctx.refreshStatus(); });
  sock.on('close', () => { wsEl.className = 'ws off'; });
  sock.on('hello', (ev) => applyStatus(ev.status));
  sock.on('status', applyStatus);
  sock.on('log', addLog);
  sock.on('capture', (ev) => { ctx.emit('capture', ev); if (ev.state !== 'running') { ctx.refreshStatus(); if (ctx.wave.showMean || ctx.wave.showEnv) ctx.wave.onNeedStats(); } });
  sock.on('trace', (header, samples) => { ctx.wave.pushLive(samples, header); if (header.stored !== false && header.index >= 0) { document.getElementById('trace-count').textContent = `${header.index + 1} traces`; ctx.wave.setTraceCount(header.index + 1); } });
  sock.on('traces', (ev) => { ctx.emit('traces', ev); ctx.wave.setTraceCount(ev.count); });
  sock.on('serial', (ev) => ctx.emit('serial', ev));
  sock.on('setting', (ev) => ctx.emit('setting', ev));
  sock.on('cpa', (ev) => ctx.emit('cpa', ev));
  sock.on('glitch', (ev) => { ctx.emit('glitch', ev); if (ev.state !== 'running') ctx.refreshStatus(); });
  sock.on('glitch_result', (ev) => ctx.emit('glitch_result', ev));
  for (const k of ['toolchain', 'firmware_sources', 'build', 'build_log', 'nb', 'tutorials']) sock.on(k, (ev) => ctx.emit(k, ev));

  // Restore last tab; fall back to Connect.
  let tab = 'connect';
  try { tab = localStorage.getItem('cw.tab') || 'connect'; } catch (e) { /* ignore */ }
  ctx.showTab(tab);
  await ctx.refreshStatus();
  // Show the newest stored trace so a reload does not start with an empty plot.
  if (ctx.status && ctx.status.traces && ctx.status.traces.count > 0) {
    try { const { header, samples } = await getBinary(`/api/traces/${ctx.status.traces.count - 1}`); ctx.wave.pushLive(samples, Object.assign({ index: ctx.status.traces.count - 1 }, header)); } catch (e) { /* ignore */ }
  }
  // Recent log history for late joiners
  try { (await get('/api/logs')).slice(-200).forEach((ev) => { if (ev.type === 'log') addLog(ev, true); }); } catch (e) { /* ignore */ }
}
boot().catch((e) => { console.error(e); toast('Failed to start UI: ' + e.message, 'err', 10000); });

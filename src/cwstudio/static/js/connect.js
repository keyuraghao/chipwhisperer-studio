import { h, get, post, toast } from './api.js';

export function initConnect(ctx, el) {
  const meta = ctx.meta;
  const scopeSel = h('select', { class: 'flex' }, ...Object.entries(meta.scope_kinds).map(([k, v]) => h('option', { value: k }, v.label)));
  const snInput = h('input', { class: 'flex mono', placeholder: 'serial number (optional)' });
  // Which ChipWhisperer the simulator stands in for: decides the protocols, triggers and programmers Studio offers.
  const simSel = h('select', { class: 'flex', title: 'the simulator offers exactly what this model supports' }, ...Object.entries(meta.sim_models || { husky: 'ChipWhisperer-Husky' }).map(([k, v]) => h('option', { value: k }, v.replace('ChipWhisperer-', ''))));
  try { simSel.value = localStorage.getItem('cw.simModel') || 'husky'; } catch (e) { /* storage unavailable */ }
  simSel.addEventListener('change', () => { try { localStorage.setItem('cw.simModel', simSel.value); } catch (e) { /* ignore */ } });
  const simRow = h('div', { class: 'row' }, h('label', {}, 'Simulate as'), simSel);
  const forceChk = h('input', { type: 'checkbox' });
  const setupChk = h('input', { type: 'checkbox', checked: true });
  const devList = h('div', { class: 'help' }, 'Click Scan USB to list connected NewAE devices.');
  // The last connect error stays in the card (a toast disappears) until the next attempt.
  const connErr = h('div', { class: 'help err', style: 'white-space:pre-wrap' });
  const targetErr = h('div', { class: 'help err', style: 'white-space:pre-wrap' });
  const showErr = (box, msg) => { box.textContent = msg || ''; box.style.display = msg ? '' : 'none'; };
  showErr(connErr, ''); showErr(targetErr, '');
  const targetSel = h('select', { class: 'flex' }, ...Object.entries(meta.target_kinds).map(([k, v]) => h('option', { value: k }, v.label)));
  const info = h('div', { class: 'kv' });
  const btnConnect = h('button', { class: 'btn primary', onclick: connect }, 'Connect scope');
  const btnDisconnect = h('button', { class: 'btn', onclick: async () => {
    try { await post('/api/scope/disconnect'); showErr(connErr, ''); toast('Scope disconnected'); } catch (e) { showErr(connErr, e.message); toast(e.message, 'err', 8000); } finally { ctx.refreshStatus(); }
  } }, 'Disconnect');
  const btnTarget = h('button', { class: 'btn primary', onclick: connectTarget }, 'Connect target');
  const btnTargetDis = h('button', { class: 'btn', onclick: async () => {
    try { await post('/api/target/disconnect'); showErr(targetErr, ''); } catch (e) { showErr(targetErr, e.message); toast(e.message, 'err', 8000); } finally { ctx.refreshStatus(); }
  } }, 'Disconnect');
  const btnScan = h('button', { class: 'btn', onclick: scan }, 'Scan USB');

  if (new URLSearchParams(location.search).get('simulate') === '1' || meta.simulate_default) { scopeSel.value = 'sim'; targetSel.value = 'sim'; }
  scopeSel.addEventListener('change', () => { if (scopeSel.value === 'sim') targetSel.value = 'sim'; else if (targetSel.value === 'sim') targetSel.value = 'SimpleSerial2'; paintSim(); });
  function paintSim() { simRow.style.display = scopeSel.value === 'sim' ? '' : 'none'; }
  paintSim();

  const plat = meta.platform || {};
  const copyBtn = (text) => h('button', { class: 'btn sm', onclick: () => { navigator.clipboard.writeText(text); toast('Copied'); } }, 'Copy command');
  let scanned = false;
  async function scan() {
    btnScan.disabled = true;
    scanned = true;
    try {
      const devs = await get('/api/devices');
      devList.innerHTML = '';
      if (!devs.length) {
        devList.append(h('div', {}, 'No NewAE USB devices found.'), ...(plat.empty_hints || []).map((t) => h('div', {}, '- ' + t)));
      }
      devs.forEach((d) => {
        const line = `${d.name}${d.sn ? '  sn=' + d.sn : ''}${d.hw_loc ? '  (bus ' + d.hw_loc[0] + ', addr ' + d.hw_loc[1] + ')' : ''}${d.port ? '  ' + d.port : ''}${d.note ? '  [' + d.note + ']' : ''}`;
        const use = d.sn && !d.error && !d.in_use ? h('button', { class: 'link', onclick: () => { snInput.value = d.sn; if (d.kind && meta.scope_kinds[d.kind]) { scopeSel.value = d.kind; scopeSel.dispatchEvent(new Event('change')); } } }, 'use') : null;
        devList.append(h('div', { class: d.error ? 'err' : '' }, line, use));
        if (d.hint) devList.append(h('div', { class: d.error ? 'help err' : 'help', style: 'white-space:pre-wrap' }, d.hint));
        if (d.command) devList.append(h('pre', { class: 'code' }, d.command), copyBtn(d.command));
        if (d.url) devList.append(h('a', { href: d.url, target: '_blank', style: 'color:var(--info)' }, 'Driver instructions'));
      });
    } catch (e) { devList.textContent = 'Scan failed: ' + e.message; toast(e.message, 'err'); } finally { btnScan.disabled = false; }
  }
  // Scan once the first time the Connect tab is shown, so attached devices (or why none show up) are visible without a click.
  ctx.on('tab', (t) => { if (t === 'connect' && !scanned) scan(); });
  async function connect() {
    btnConnect.disabled = true; btnConnect.textContent = 'Connecting…';
    showErr(connErr, '');
    try {
      const r = await post('/api/scope/connect', { kind: scopeSel.value, sn: snInput.value || null, force: forceChk.checked, default_setup: setupChk.checked, sim_model: scopeSel.value === 'sim' ? simSel.value : null });
      toast(`Connected: ${r.name || r.type}`, 'ok');
      (r.warnings || []).forEach((w) => toast(w, 'warn', 10000));
      await ctx.refreshStatus();
      if (scopeSel.value === 'sim' || targetSel.value === 'sim') await connectTarget();
      ctx.showTab('scope');
    } catch (e) {
      showErr(connErr, e.message);
      toast(e.message, 'err', 8000);
      ctx.refreshStatus();  // a failed connect lets go of the previous scope: the header follows
    } finally { btnConnect.disabled = false; btnConnect.textContent = 'Connect scope'; }
  }
  async function connectTarget() {
    btnTarget.disabled = true;
    showErr(targetErr, '');
    try {
      await post('/api/target/connect', { kind: targetSel.value });
      toast('Target connected', 'ok');
      await ctx.refreshStatus();
    } catch (e) { showErr(targetErr, e.message); toast(e.message, 'err', 8000); ctx.refreshStatus(); } finally { btnTarget.disabled = false; }
  }

  // Quick Info: the pin diagram of the scope Studio has open (the active device, not the first one on the bus). Redrawn from /api/pinout whenever the status says the scope changed.
  const quick = h('div', { class: 'card' });
  const ROLES = [['power', 'power'], ['ground', 'ground'], ['io', 'target IO'], ['spi', 'SPI / programming'], ['clock', 'clock'], ['reset', 'reset'], ['nc', 'not connected']];
  let quickKey = null;
  function pinEl(p) {
    const el = h('div', { class: `pin ${p.role}${p.pin === 1 ? ' first' : ''}`, title: `${p.pin}: ${p.name}${p.dir ? ' (' + p.dir + ')' : ''}\n${p.function}${p.alias ? '\nscope.io: ' + p.alias : ''}` },
      h('span', { class: 'num' }, String(p.pin)), h('span', { class: 'sq' }), h('span', { class: 'nm' }, p.name));
    if (p.alias) el.append(h('span', { class: 'al' }, p.alias));
    return el;
  }
  function paintQuick(po) {
    quick.innerHTML = '';
    quick.append(h('h2', {}, 'Quick Info'));
    if (!po.connected) { quick.append(h('div', { class: 'help' }, po.message || 'No ChipWhisperer connected')); return; }
    const d = po.device || {};
    quick.append(h('div', { class: 'help' }, `${po.label || d.name || '?'}${d.sn ? ' · sn ' + d.sn : ''}${d.simulated ? ' (simulated)' : ''}${d.hw_loc ? ' · bus ' + d.hw_loc[0] + ' addr ' + d.hw_loc[1] : ''}`));
    if (po.message) { quick.append(h('div', { class: 'help warn' }, po.message)); return; }
    quick.append(h('div', { class: 'pinlegend' }, ...ROLES.map(([r, t]) => h('span', { class: 'pin ' + r }, h('span', { class: 'sq' }), t))));
    (po.connectors || []).forEach((c) => {
      quick.append(h('div', { class: 'card-head', style: 'margin:8px 0 2px' }, h('span', { class: 'title' }, c.name)));
      // two rows of the header side by side: odd pins left, even pins right, as on the board seen from above
      const grid = h('div', { class: 'pinmap' });
      const pins = c.pins.slice().sort((a, b) => a.pin - b.pin);
      for (let i = 0; i < pins.length; i += 2) { grid.append(pinEl(pins[i])); if (pins[i + 1]) grid.append(pinEl(pins[i + 1])); }
      quick.append(grid);
      if (c.note) quick.append(h('div', { class: 'help' }, c.note));
    });
    if (po.ports && po.ports.length) {
      quick.append(h('div', { class: 'card-head', style: 'margin:8px 0 2px' }, h('span', { class: 'title' }, 'Ports')));
      const kv = h('div', { class: 'kv' });
      po.ports.forEach((p) => kv.append(h('span', { class: 'k' }, `${p.name} (${p.kind})`), h('span', {}, p.function)));
      quick.append(kv);
    }
    (po.notes || []).forEach((n) => quick.append(h('div', { class: 'help' }, n)));
  }
  paintQuick({ connected: false });
  async function refreshQuick(sc) {
    const key = sc.connected ? [sc.type, sc.sim_model, sc.sn, sc.name, sc.pending ? 'p' : ''].join('|') : null;
    if (key === quickKey) return;
    quickKey = key;
    if (!sc.connected) { paintQuick({ connected: false }); return; }
    try { paintQuick(await get('/api/pinout')); } catch (e) { quickKey = null; paintQuick({ connected: true, label: sc.name || sc.type, message: 'Could not load the pin diagram: ' + e.message }); }
  }

  const platCard = h('div', { class: 'card' }, h('h2', {}, `Platform: ${plat.os || '?'}`), h('div', { class: 'help' }, plat.note || ''));
  if (plat.udev_install_cmd) {
    platCard.append(h('div', { class: 'help' }, 'Linux needs a udev rule so you can access the device without root:'),
      h('pre', { class: 'code' }, plat.udev_install_cmd), copyBtn(plat.udev_install_cmd));
  }
  if (plat.driver_url) platCard.append(h('a', { href: plat.driver_url, target: '_blank', style: 'color:var(--info)' }, 'Windows driver instructions'));

  el.append(
    h('h2', {}, 'Scope'),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('label', {}, 'Device'), scopeSel),
      simRow,
      h('div', { class: 'row' }, h('label', {}, 'Serial'), snInput, btnScan),
      devList,
      h('div', { class: 'row' }, h('label', {}, forceChk, ' force FPGA reprogram'), h('label', {}, setupChk, ' default_setup()')),
      h('div', { class: 'row' }, btnConnect, btnDisconnect),
      connErr),
    h('h2', {}, 'Target'),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('label', {}, 'Protocol'), targetSel),
      h('div', { class: 'help' }, 'Most current firmware (simpleserial-aes etc.) uses SimpleSerial v2. Older hex files use v1.'),
      h('div', { class: 'row' }, btnTarget, btnTargetDis),
      targetErr),
    h('h2', {}, 'Status'), h('div', { class: 'card' }, info),
    quick,
    platCard,
  );

  ctx.on('status', (st) => {
    refreshQuick(st.scope);
    info.innerHTML = '';
    const rows = [['Scope', st.scope.connected ? `${st.scope.name || st.scope.type}${st.scope.sn ? ' (sn ' + st.scope.sn + ')' : ''}` : (st.scope.lost ? 'lost: ' + st.scope.lost : 'not connected')],
      ['Firmware', st.scope.fw_version ? JSON.stringify(st.scope.fw_version) : '-'],
      ['Target', st.target.connected ? st.target.type : 'not connected'],
      ['Traces', `${st.traces.count} (${st.traces.samples} samples)`],
      ['Data dir', st.data_dir],
      ['Compute', (meta.accel && meta.accel.label) ? meta.accel.label + (meta.accel.reason ? ' · ' + meta.accel.reason : '') : '-']];
    if (st.hardware_stuck) rows.push(['Hardware', `stuck in ${st.hardware_stuck.name} for ${Math.round(st.hardware_stuck.seconds)} s: unplug the ChipWhisperer, plug it back in and restart Studio`]);
    const hl = st.health || {};
    if (st.scope.connected && hl.model) {
      if (hl.xadc) rows.push(['FPGA', `${hl.xadc.temp} C (max ${hl.xadc.max_temp} C) · VCCINT ${hl.xadc.rails.vccint} V · VCCAUX ${hl.xadc.rails.vccaux} V · VBRAM ${hl.xadc.rails.vccbram} V${hl.xadc.alarms ? ' · ALARM: ' + hl.xadc.alarms : ''}`]);
      if (hl.locked) rows.push(['Clocks', Object.entries(hl.locked).map(([k, v]) => `${k.replace('_locked', '')} ${v ? 'locked' : 'NOT LOCKED'}`).join(' · ') + (hl.freq_ctr != null ? ` · counter ${(hl.freq_ctr / 1e6).toFixed(3)} MHz` : '')]);
      if (hl.adc_errors) rows.push(['Capture errors', hl.adc_errors]);
      if (hl.sam_errors) rows.push(['USB controller', hl.sam_errors]);
      if (hl.glitches_fired != null) rows.push(['Glitches fired', String(hl.glitches_fired)]);
    }
    rows.forEach(([k, v]) => info.append(h('span', { class: 'k' }, k), h('span', {}, String(v))));
    if (st.scope.connected && hl.model) {
      const probs = hl.problems || [];
      const box = h('div', { class: 'help ' + (probs.length ? 'warn' : ''), style: 'grid-column: 1 / -1' }, probs.length ? h('ul', {}, ...probs.map((p) => h('li', {}, p))) : 'Health: no problems reported.',
        h('button', { class: 'btn sm', style: 'margin-left:8px', title: 'clear the sticky errors and alarms, then read again', onclick: async () => { try { await post('/api/scope/health/clear'); ctx.refreshStatus(); } catch (e) { toast(e.message, 'err'); } } }, 'Clear errors'));
      info.append(box);
    }
  });
}

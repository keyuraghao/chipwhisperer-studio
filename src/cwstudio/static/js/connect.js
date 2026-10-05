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
        if (d.url) devList.append(h('a', { href: d.url, target: '_blank', style: 'color:var(--accent2)' }, 'Driver instructions'));
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

  const platCard = h('div', { class: 'card' }, h('h2', {}, `Platform: ${plat.os || '?'}`), h('div', { class: 'help' }, plat.note || ''));
  if (plat.udev_install_cmd) {
    platCard.append(h('div', { class: 'help' }, 'Linux needs a udev rule so you can access the device without root:'),
      h('pre', { class: 'code' }, plat.udev_install_cmd), copyBtn(plat.udev_install_cmd));
  }
  if (plat.driver_url) platCard.append(h('a', { href: plat.driver_url, target: '_blank', style: 'color:var(--accent2)' }, 'Windows driver instructions'));

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
    platCard,
  );

  ctx.on('status', (st) => {
    info.innerHTML = '';
    const rows = [['Scope', st.scope.connected ? `${st.scope.name || st.scope.type}${st.scope.sn ? ' (sn ' + st.scope.sn + ')' : ''}` : (st.scope.lost ? 'lost: ' + st.scope.lost : 'not connected')],
      ['Firmware', st.scope.fw_version ? JSON.stringify(st.scope.fw_version) : '-'],
      ['Target', st.target.connected ? st.target.type : 'not connected'],
      ['Traces', `${st.traces.count} (${st.traces.samples} samples)`],
      ['Data dir', st.data_dir]];
    if (st.hardware_stuck) rows.push(['Hardware', `stuck in ${st.hardware_stuck.name} for ${Math.round(st.hardware_stuck.seconds)} s: unplug the ChipWhisperer, plug it back in and restart Studio`]);
    rows.forEach(([k, v]) => info.append(h('span', { class: 'k' }, k), h('span', {}, String(v))));
  });
}

// Interfaces tab: UART, SimpleSerial, SPI, GPIO and USERIO, triggers, bit-banger and 1-Wire, JTAG/SWD through OpenOCD, Arm trace. Every section and choice follows GET /api/capabilities: what the connected ChipWhisperer (or the simulator's model) cannot do stays visible but disabled, with the reason as a hint.
import { h, get, post, put, toast, fmtClock } from './api.js';

const LS = (k, d) => { try { const v = localStorage.getItem('cw.iface.' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } };
const SAVE = (k, v) => { try { localStorage.setItem('cw.iface.' + k, JSON.stringify(v)); } catch (e) { /* storage unavailable */ } };
const NO_SCOPE = { available: false, reason: 'connect a scope first' };
const PIN_LABEL = { tio1: 'TIO1', tio2: 'TIO2', tio3: 'TIO3', tio4: 'TIO4', nrst: 'nRST', pdic: 'PDIC', pdid: 'PDID', miso: 'MISO', mosi: 'MOSI', sck: 'SCK', sma: 'SMA/AUX' };
const pinLabel = (p) => PIN_LABEL[p] || p.replace('userio_d', 'USERIO D');
const errMsg = (e) => String(e.message || e).replace(/^Unsupported: /, '');

/** Serial terminal shared by the Target and Interfaces tabs: text or hex display, line endings, timestamps and send history. */
export function createTerminal(ctx, { id = 'term', height } = {}) {
  const opts = LS(id, { hexShow: false, ts: false, mode: 'text', eol: 'lf' });
  let history = LS(id + '.history', []);
  let hpos = -1;
  const out = h('div', { class: 'console term' + (opts.ts ? ' show-ts' : ''), style: height ? `height:${height}px` : null });
  const input = h('input', { class: 'flex mono', placeholder: 'text to send (Enter, Up/Down for history)' });
  const modeSel = h('select', { title: 'send as text or as hex bytes' }, h('option', { value: 'text' }, 'text'), h('option', { value: 'hex' }, 'hex'));
  const eolSel = h('select', { title: 'line ending appended to text' }, ...[['none', 'no line end'], ['lf', 'LF'], ['cr', 'CR'], ['crlf', 'CR LF']].map(([v, l]) => h('option', { value: v }, l)));
  const hexChk = h('input', { type: 'checkbox', checked: opts.hexShow });
  const tsChk = h('input', { type: 'checkbox', checked: opts.ts });
  const histSel = h('select', { class: 'flex', title: 'send history' });
  modeSel.value = opts.mode; eolSel.value = opts.eol;
  const recs = [];
  const persist = () => SAVE(id, { hexShow: hexChk.checked, ts: tsChk.checked, mode: modeSel.value, eol: eolSel.value });
  function line(rec) {
    const t = new Date((rec.t || Date.now() / 1000) * 1000);
    const ts = h('span', { class: 'ts' }, fmtClock(t, { hour12: false }) + '.' + String(t.getMilliseconds()).padStart(3, '0') + ' ');
    const body = hexChk.checked ? rec.hex.replace(/(..)/g, '$1 ').trim() : rec.data.replace(/\r/g, '␍').replace(/\n/g, '⏎');
    return h('div', { class: rec.dir }, ts, (rec.dir === 'tx' ? '→ ' : '← ') + body);
  }
  function append(rec) {
    recs.push(rec); if (recs.length > 1000) recs.shift();
    const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 30;
    out.append(line(rec));
    while (out.childNodes.length > 1000) out.removeChild(out.firstChild);
    if (atBottom) out.scrollTop = out.scrollHeight;
  }
  function redraw() { out.innerHTML = ''; out.classList.toggle('show-ts', tsChk.checked); recs.forEach((r) => out.append(line(r))); out.scrollTop = out.scrollHeight; }
  function paintHistory() { histSel.innerHTML = ''; histSel.append(h('option', { value: '' }, history.length ? `history (${history.length})` : 'no history yet'), ...history.map((x, i) => h('option', { value: i }, (x.mode === 'hex' ? '[hex] ' : '') + x.data))); }
  const sendBtn = h('button', { class: 'btn sm', onclick: () => send() }, 'Send');
  /** Writing needs a connected target: Send stays disabled until there is one, with the reason as its hint. */
  function gate() {
    const ok = !!(ctx.status && ctx.status.target && ctx.status.target.connected);
    sendBtn.disabled = !ok; sendBtn.title = ok ? '' : 'connect a target first (Connect tab)';
  }
  ctx.on('status', gate);
  async function send() {
    const data = input.value;
    if (sendBtn.disabled || (!data && modeSel.value === 'hex')) return;
    try {
      await post('/api/target/serial/write', { data, hex: modeSel.value === 'hex', eol: eolSel.value });
      history = [{ data, mode: modeSel.value }, ...history.filter((x) => x.data !== data || x.mode !== modeSel.value)].slice(0, 30);
      SAVE(id + '.history', history); paintHistory(); hpos = -1; input.value = '';
    } catch (e) { toast(errMsg(e), 'err'); }
  }
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') send();
    else if (e.key === 'ArrowUp' && history.length) { hpos = Math.min(history.length - 1, hpos + 1); input.value = history[hpos].data; modeSel.value = history[hpos].mode; e.preventDefault(); }
    else if (e.key === 'ArrowDown') { hpos = Math.max(-1, hpos - 1); input.value = hpos >= 0 ? history[hpos].data : ''; e.preventDefault(); }
  });
  histSel.addEventListener('change', () => { const x = history[+histSel.value]; if (x) { input.value = x.data; modeSel.value = x.mode; input.focus(); } histSel.value = ''; });
  [hexChk, tsChk].forEach((c) => c.addEventListener('change', () => { persist(); redraw(); }));
  [modeSel, eolSel].forEach((c) => c.addEventListener('change', persist));
  paintHistory();
  ctx.on('serial', append);
  get('/api/target/serial').then((rs) => rs.slice(-200).forEach(append)).catch(() => {});
  const el = h('div', { class: 'terminal' }, out,
    h('div', { class: 'row', style: 'margin-top:6px' }, input, sendBtn),
    h('div', { class: 'row' }, h('label', {}, 'Send as'), modeSel, eolSel),
    h('div', { class: 'row' }, h('label', {}, 'History'), histSel),
    h('div', { class: 'row' }, h('label', {}, hexChk, ' show hex'), h('label', {}, tsChk, ' timestamps'), h('button', { class: 'link', onclick: () => { recs.length = 0; out.innerHTML = ''; } }, 'clear')));
  gate();
  return { el, append, send, gate };
}

/** One gated section: a card whose controls are enabled only when its capability is available. */
function section(title, sub) {
  const badge = h('span', { class: 'badge' }, '...');
  const reason = h('div', { class: 'iface-reason', style: 'display:none' });
  const body = h('div', { class: 'iface-body' });
  const card = h('div', { class: 'card iface' }, h('div', { class: 'card-head' }, h('span', { class: 'title' }, title), sub ? h('span', { class: 'sub' }, sub) : null, h('div', { class: 'end' }, badge)), reason, body);
  function set(entry, label) {
    const ok = !!(entry && entry.available);
    card.classList.toggle('off', !ok);
    badge.className = 'badge ' + (ok ? 'ok' : 'err');
    badge.textContent = ok ? (label || 'available') : 'not available';
    reason.textContent = ok ? '' : (entry && entry.reason) || 'not supported by this hardware';
    reason.style.display = ok ? 'none' : '';
    card.title = ok ? '' : reason.textContent;
    body.querySelectorAll('input,select,button,textarea').forEach((e) => { e.disabled = !ok; });
    return ok;
  }
  return { card, body, set, badge };
}

/** Disable one option/button with a reason (or enable it). */
function gateEl(el, ok, reason) { el.disabled = !ok; el.title = ok ? '' : (reason || 'not supported by this hardware'); el.classList.toggle('gated', !ok); }

export function initInterfaces(ctx, el) {
  let caps = { connected: false };
  let state = {};
  ctx.caps = caps;

  // ---------- overview ----------
  const overview = h('div', { class: 'cap-badges' });
  const modelLine = h('div', { class: 'help' }, 'Connect a scope (or the simulator) to see which interfaces it offers.');
  const mpsseBanner = h('div', { class: 'iface-banner', style: 'display:none' });

  // ---------- UART ----------
  const uart = section('UART', 'target serial port');
  const baudIn = h('input', { class: 'flex mono', type: 'number', min: 500, max: 2000000, list: 'cw-bauds', value: 38400 });
  const bauds = h('datalist', { id: 'cw-bauds' }, ...[9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1000000].map((b) => h('option', { value: b })));
  const paritySel = h('select', {}, ...['none', 'odd', 'even', 'mark', 'space'].map((p) => h('option', { value: p }, p)));
  const stopSel = h('select', {}, ...['1', '1.5', '2'].map((p) => h('option', { value: p }, p)));
  const rxSel = h('select', {}, ...['tio1', 'tio2', 'tio3', 'tio4'].map((p) => h('option', { value: p }, pinLabel(p))));
  const txSel = h('select', {}, ...['tio1', 'tio2', 'tio3', 'tio4'].map((p) => h('option', { value: p }, pinLabel(p))));
  const uartInfo = h('div', { class: 'help mono' });
  async function uartApply() {
    try {
      const r = await put('/api/interfaces/uart', { baud: +baudIn.value, parity: paritySel.value, stop_bits: +stopSel.value, rx: rxSel.value, tx: txSel.value });
      showUart(r); toast('UART configured', 'ok');
    } catch (e) { toast(errMsg(e), 'err', 7000); }
  }
  function showUart(u) {
    if (!u || u.error) return;
    if (u.baud) baudIn.value = u.baud;
    if (u.parity) paritySel.value = u.parity;
    if (u.stop_bits) stopSel.value = String(u.stop_bits);
    if (u.rx) rxSel.value = u.rx;
    if (u.tx) txSel.value = u.tx;
    uartInfo.textContent = `${u.baud || '?'} baud, 8 data bits, parity ${u.parity}, ${u.stop_bits} stop bit${u.stop_bits === 1 ? '' : 's'}, RX ${u.rx ? pinLabel(u.rx) : '-'}, TX ${u.tx ? pinLabel(u.tx) : '-'}${u.target ? '' : ' (no target connected: the terminal needs one)'}`;
  }
  const term = createTerminal(ctx, { id: 'iface-term', height: 180 });
  uart.body.append(
    h('div', { class: 'row' }, h('label', {}, 'Baud'), baudIn, bauds),
    h('div', { class: 'row' }, h('label', {}, 'Parity'), paritySel, h('label', {}, 'Stop bits'), stopSel, h('label', {}, 'Data bits'), h('span', { class: 'code', title: 'the ChipWhisperer USART always uses 8 data bits' }, '8 (fixed)')),
    h('div', { class: 'row' }, h('label', {}, 'RX pin'), rxSel, h('label', {}, 'TX pin'), txSel),
    h('div', { class: 'row' }, h('button', { class: 'btn primary sm', onclick: uartApply }, 'Apply'), h('button', { class: 'btn sm', onclick: () => get('/api/interfaces/uart').then(showUart).catch((e) => toast(errMsg(e), 'err')) }, 'Read back')),
    uartInfo, term.el);

  // ---------- SimpleSerial ----------
  const ss = section('SimpleSerial', 'command protocol on the UART');
  let ssVer = LS('ssver', '2.1');
  const ssSeg = h('div', { class: 'seg' });
  const ssBtns = {};
  [['1.0', 'v1.0'], ['1.1', 'v1.1'], ['2.1', 'v2.1'], ['cdc', 'v2 over CDC']].forEach(([v, l]) => {
    const b = h('button', { onclick: () => { ssVer = v; SAVE('ssver', v); paintSeg(); } }, l);
    ssBtns[v] = b; ssSeg.append(b);
  });
  function paintSeg() { Object.entries(ssBtns).forEach(([v, b]) => b.classList.toggle('on', v === ssVer)); }
  paintSeg();
  const ssCmd = h('input', { class: 'mono', value: 'p', style: 'width:40px', maxlength: 1 });
  const ssData = h('input', { class: 'flex mono', value: '00112233445566778899aabbccddeeff', placeholder: 'hex payload' });
  const ssLen = h('input', { type: 'number', value: 16, style: 'width:64px', title: 'expected response length' });
  const ssResp = h('div', { class: 'code out', style: 'display:block;min-height:22px;margin-top:4px' });
  const ssState = h('div', { class: 'help' });
  async function ssConnect() {
    try { const r = await post('/api/interfaces/simpleserial/connect', { version: ssVer }); toast(`SimpleSerial ${r.version} target connected`, 'ok'); ctx.refreshStatus(); refresh(); } catch (e) { toast(errMsg(e), 'err', 7000); }
  }
  async function ssSend() {
    try { const r = await post('/api/interfaces/simpleserial/send', { cmd: ssCmd.value || 'p', data: ssData.value, read_len: +ssLen.value }); ssResp.textContent = r.response != null ? 'r ' + r.response : '(no response)'; } catch (e) { toast(errMsg(e), 'err'); }
  }
  const ssSendBtn = h('button', { class: 'btn sm', onclick: ssSend }, 'Send');
  ss.body.append(
    h('div', { class: 'row' }, h('label', {}, 'Version'), ssSeg),
    h('div', { class: 'row' }, h('button', { class: 'btn primary sm', onclick: ssConnect }, 'Connect target'), ssState),
    h('div', { class: 'row' }, h('label', {}, 'Command'), ssCmd, ssData, ssLen, ssSendBtn),
    ssResp,
    h('div', { class: 'help' }, 'v1.0 sends without waiting for an acknowledgement; v1.1 and v2.1 wait for it. The Connect tab target choice still works the same way.'));

  // ---------- SPI ----------
  const spi = section('SPI master', 'SCK, MOSI, MISO on the 20-pin header');
  const spiSpeed = h('input', { class: 'flex mono', type: 'number', value: LS('spispeed', 1000000), min: 1000, max: 20000000, title: 'clock in Hz' });
  const spiCs = h('select', {}, ...['pdid', 'pdic', 'tio3', 'tio4'].map((p) => h('option', { value: p }, pinLabel(p))));
  const spiData = h('input', { class: 'flex mono', value: '9f 00 00 00', placeholder: 'hex bytes for MOSI' });
  const spiSck = h('input', { type: 'number', value: 8, min: 1, max: 255, style: 'width:64px' });
  const spiOut = h('table', { class: 'tbl' });
  const spiState = h('span', { class: 'badge' }, 'off');
  async function spiEnable(on) {
    try {
      if (on) { SAVE('spispeed', +spiSpeed.value); await post('/api/interfaces/spi/enable', { speed: +spiSpeed.value, cs: spiCs.value }); } else await post('/api/interfaces/spi/disable');
      refresh();
    } catch (e) { toast(errMsg(e), 'err', 7000); }
  }
  async function spiXfer(data) {
    try {
      const r = await post('/api/interfaces/spi/transfer', { data: data || spiData.value });
      spiOut.prepend(h('tr', {}, h('td', { class: 'mono' }, r.mosi), h('td', { class: 'mono' }, r.miso)));
      while (spiOut.rows.length > 12) spiOut.deleteRow(spiOut.rows.length - 1);
    } catch (e) { toast(errMsg(e), 'err'); }
  }
  // these need the SPI master on: disabled (with the reason) until Enable succeeded
  const spiOnly = [
    h('button', { class: 'btn sm', onclick: () => spiEnable(false) }, 'Disable'),
    h('button', { class: 'btn sm', onclick: () => spiXfer() }, 'Send'),
    h('button', { class: 'btn sm', onclick: () => spiXfer('9f 00 00 00') }, 'JEDEC ID'), h('button', { class: 'btn sm', onclick: () => spiXfer('05 00') }, 'Status'), h('button', { class: 'btn sm', onclick: () => spiXfer('03 00 00 00' + ' 00'.repeat(16)) }, 'Read 16 B @0'),
    h('button', { class: 'btn sm', onclick: async () => { try { await post('/api/interfaces/spi/toggle_sck', { cycles: +spiSck.value }); toast('SCK toggled', 'ok'); } catch (e) { toast(errMsg(e), 'err'); } } }, 'Toggle')];
  const [spiOff, spiSend, spiJedec, spiStat, spiRead, spiToggle] = spiOnly;
  spi.body.append(
    h('div', { class: 'row' }, h('label', {}, 'Clock Hz'), spiSpeed, h('label', {}, 'CS'), spiCs),
    h('div', { class: 'row' }, h('button', { class: 'btn primary sm', onclick: () => spiEnable(true) }, 'Enable'), spiOff, spiState),
    h('div', { class: 'row' }, h('label', {}, 'Transfer'), spiData, spiSend),
    h('div', { class: 'row' }, h('label', {}, 'Flash'), spiJedec, spiStat, spiRead),
    h('div', { class: 'row' }, h('label', {}, 'Toggle SCK'), spiSck, spiToggle),
    h('details', { open: true }, h('summary', {}, 'MOSI / MISO'), spiOut),
    h('div', { class: 'help' }, 'nRST is held high while SPI is on. Chip select is driven low for each transfer. The simulator answers as a W25Q128 SPI flash (JEDEC ID EF 40 18).'));

  // ---------- GPIO ----------
  const gpio = section('GPIO', 'target pins');
  const gpioTable = h('table', { class: 'tbl pins' });
  const liveChk = h('input', { type: 'checkbox', checked: LS('gpiolive', false) });
  const pulseMs = h('input', { type: 'number', value: 50, min: 1, max: 5000, style: 'width:70px' });
  const pulsePin = h('select', {}, h('option', { value: 'nrst' }, 'nRST'), h('option', { value: 'pdic' }, 'PDIC'));
  const gpioNote = h('div', { class: 'help' });
  async function gpioSet(pin, st) { try { renderGpio(await put('/api/interfaces/gpio', { pin, state: st })); } catch (e) { toast(errMsg(e), 'err'); } }
  function renderGpio(g) {
    gpioTable.innerHTML = '';
    gpioTable.append(h('tr', {}, h('th', {}, 'Pin'), h('th', {}, 'Level'), h('th', {}, 'Mode'), h('th', {}, 'Drive')));
    for (const [p, info] of Object.entries(g.pins || {})) {
      const lvl = info.level;
      const modes = info.modes || [];
      const b = (st, label, want) => { const btn = h('button', { class: 'btn sm' + (info.mode === want ? ' active' : ''), onclick: () => gpioSet(p, st) }, label); gateEl(btn, modes.includes(want), `${pinLabel(p)} cannot be driven ${label.toLowerCase()} here`); return btn; };
      const hi = p.startsWith('tio') ? 'gpio_high' : 'high', lo = p.startsWith('tio') ? 'gpio_low' : 'low';
      gpioTable.append(h('tr', {}, h('td', {}, pinLabel(p)),
        h('td', {}, h('span', { class: 'lvl ' + (lvl === 1 ? 'hi' : lvl === 0 ? 'lo' : 'na'), title: lvl == null ? (g.read_reason || 'not readable') : String(lvl) }), lvl == null ? '' : ' ' + lvl),
        h('td', { class: 'mono muted' }, info.mode == null ? '-' : String(info.mode)),
        h('td', {}, modes.length ? h('div', { class: 'btn-row' }, b('high', 'High', hi), b('low', 'Low', lo), b('high_z', 'Hi-Z', 'high_z')) : h('span', { class: 'muted' }, 'read only'))));
    }
    gpioNote.textContent = g.readable ? 'Levels are read back from the scope.' : (g.read_reason || '');
  }
  async function gpioRead() { try { renderGpio(await get('/api/interfaces/gpio')); } catch (e) { /* section gated */ } }
  liveChk.addEventListener('change', () => SAVE('gpiolive', liveChk.checked));
  gpio.body.append(h('div', { class: 'tbl-wrap' }, gpioTable), gpioNote,
    h('div', { class: 'row', style: 'margin-top:8px' }, h('button', { class: 'btn sm', onclick: gpioRead }, 'Read'), h('label', {}, liveChk, ' live')),
    h('div', { class: 'row' }, h('label', {}, 'Reset pulse'), pulsePin, pulseMs, h('span', { class: 'muted' }, 'ms'), h('button', { class: 'btn sm', onclick: async () => { try { await post('/api/interfaces/gpio/pulse', { pin: pulsePin.value, ms: +pulseMs.value }); toast(`${pulsePin.value.toUpperCase()} pulsed`, 'ok'); gpioRead(); } catch (e) { toast(errMsg(e), 'err'); } } }, 'Pulse low')));

  // ---------- USERIO ----------
  const uio = section('Husky USERIO', 'D0-D7 and CK');
  const uioGrid = h('div', { class: 'uio-grid' });
  const uioMode = h('span', { class: 'code' }, '-');
  let uioState = { direction: 0, drive: 0, status: 0, pins: [] };
  function renderUio(u) {
    uioState = u; uioMode.textContent = u.mode;
    uioGrid.innerHTML = '';
    u.pins.forEach((p, i) => {
      const bit = 1 << i, out = !!(u.direction & bit), drv = !!(u.drive & bit), lvl = (u.status >> i) & 1;
      const dirBtn = h('button', { class: 'btn sm' + (out ? ' active' : ''), title: out ? 'driven by the Husky (click for input)' : 'input (click to drive)', onclick: () => uioSet({ direction: u.direction ^ bit }) }, out ? 'out' : 'in');
      const drvBtn = h('button', { class: 'btn sm', title: 'level to drive', onclick: () => uioSet({ drive: u.drive ^ bit }) }, drv ? '1' : '0');
      drvBtn.disabled = !out;
      uioGrid.append(h('div', { class: 'uio-pin' }, h('span', { class: 'name' }, p), h('span', { class: 'lvl ' + (lvl ? 'hi' : 'lo'), title: `reads ${lvl}` }), dirBtn, drvBtn));
    });
  }
  async function uioSet(chg) { try { renderUio(await put('/api/interfaces/userio', Object.assign({ mode: 'normal' }, chg))); } catch (e) { toast(errMsg(e), 'err'); } }
  async function uioRead() { try { renderUio(await get('/api/interfaces/userio')); } catch (e) { /* section gated */ } }
  uio.body.append(h('div', { class: 'row' }, h('label', {}, 'Mode'), uioMode, h('button', { class: 'btn sm', onclick: () => uioSet({}) }, 'Set normal'), h('button', { class: 'btn sm', onclick: uioRead }, 'Read')), uioGrid,
    h('div', { class: 'help' }, 'Direction and drive apply in normal mode. The live switch of the GPIO section refreshes these levels too.'));

  // ---------- triggers ----------
  const trig = section('Triggers', 'what starts a capture');
  const KINDS = [['basic', 'Edge / level on pins', 'basic'], ['uart_decode', 'UART byte pattern (Pro I/O decode)', 'uart_decode'], ['uart_pattern', 'UART pattern rules (Husky)', 'uart_pattern'], ['edge_counter', 'Edge counter (Husky)', 'edge_counter'], ['adc_level', 'ADC level (Husky)', 'adc_level'], ['sequencer', 'Trigger sequencer (Husky)', 'sequencer'], ['sad', 'SAD pattern match', 'sad']];
  const kindSel = h('select', { class: 'flex' }, ...KINDS.map(([k, l]) => h('option', { value: k }, l)));
  const trigForm = h('div');
  const trigNow = h('div', { class: 'help mono' });
  const pinChecks = {};
  const opSel = h('select', {}, ...['OR', 'AND', 'NAND'].map((o) => h('option', { value: o }, o)));
  const edgeSel = h('select', {}, ...['rising_edge', 'falling_edge', 'high', 'low'].map((o) => h('option', { value: o }, o.replace('_', ' '))));
  const tPin = h('select', {});
  const tPin2 = h('select', {});
  const tBaud = h('input', { type: 'number', value: 38400, class: 'flex mono' });
  const tPattern = h('input', { class: 'flex mono', value: "'r'", title: "quoted text such as 'r', or hex bytes such as 72 XX (XX = any byte, Pro only)" });
  const tRule = h('select', {});
  const tData = h('select', {}, ...[5, 6, 7, 8, 9].map((d) => h('option', { value: d, selected: d === 8 }, d)));
  const tStop = h('select', {}, h('option', { value: 1 }, '1'), h('option', { value: 2 }, '2'));
  const tParity = h('select', {}, ...['none', 'odd', 'even'].map((p) => h('option', { value: p }, p)));
  const tEdges = h('input', { type: 'number', value: 1, min: 1, max: 65536, class: 'flex' });
  const tLevel = h('input', { type: 'number', value: 0.1, step: 0.01, min: -0.5, max: 0.5, class: 'flex' });
  const tWs = h('input', { type: 'number', value: 0, min: 0, max: 65535, style: 'width:90px' });
  const tWe = h('input', { type: 'number', value: 0, min: 0, max: 65535, style: 'width:90px' });
  const tSeqOn = h('input', { type: 'checkbox', checked: true });
  const tThr = h('input', { type: 'number', value: 10, min: 0, class: 'flex' });
  const tStart = h('input', { type: 'number', value: 0, min: 0, class: 'flex' });
  function trigFormFor(kind) {
    trigForm.innerHTML = '';
    const pins = ((caps.triggers || {}).basic || {}).pins || [];
    const allPins = ['tio1', 'tio2', 'tio3', 'tio4', 'nrst', 'sma', ...Array.from({ length: 8 }, (_, i) => `userio_d${i}`)];
    const fillPins = (sel, def) => { sel.innerHTML = ''; allPins.forEach((p) => { const o = h('option', { value: p }, pinLabel(p)); gateEl(o, pins.includes(p), caps.model === 'nano' ? 'the CW-Nano triggers on TIO4 only' : (p === 'sma' ? 'needs a Pro or Husky' : 'USERIO pins are on the Husky only')); sel.append(o); }); sel.value = pins.includes(def) ? def : (pins[0] || 'tio4'); };
    if (kind === 'basic') {
      const box = h('div', { class: 'pin-checks' });
      allPins.forEach((p) => {
        const c = pinChecks[p] || (pinChecks[p] = h('input', { type: 'checkbox', checked: p === 'tio4', 'data-pin': p }));
        const ok = pins.includes(p);
        c.disabled = !ok; if (!ok) c.checked = false;
        box.append(h('label', { title: ok ? '' : (caps.model === 'nano' ? 'the CW-Nano triggers on TIO4 only' : 'not on this ChipWhisperer'), class: ok ? '' : 'gated' }, c, ' ' + pinLabel(p)));
      });
      const comb = (caps.triggers || {}).combinations || {};
      gateEl(opSel, !!comb.available, comb.reason);
      const edges = ((caps.triggers || {}).basic || {}).edges || [];
      [...edgeSel.options].forEach((o) => gateEl(o, edges.includes(o.value), 'the CW-Nano triggers on a rising edge only'));
      trigForm.append(h('div', { class: 'row' }, h('label', {}, 'Pins'), box), h('div', { class: 'row' }, h('label', {}, 'Combine'), opSel), h('div', { class: 'row' }, h('label', {}, 'Mode'), edgeSel));
    } else if (kind === 'uart_decode' || kind === 'uart_pattern') {
      fillPins(tPin, 'tio1');
      trigForm.append(h('div', { class: 'row' }, h('label', {}, 'RX pin'), tPin, h('label', {}, 'Baud'), tBaud), h('div', { class: 'row' }, h('label', {}, 'Pattern'), tPattern));
      if (kind === 'uart_pattern') {
        const n = ((caps.triggers || {}).uart_pattern || {}).rules || 2;
        tRule.innerHTML = ''; for (let i = 0; i < 8; i++) { const o = h('option', { value: i }, `rule ${i}`); gateEl(o, i < n, `this ChipWhisperer has ${n} rules`); tRule.append(o); }
        trigForm.append(h('div', { class: 'row' }, h('label', {}, 'Rule'), tRule, h('label', {}, 'Bits'), tData, h('label', {}, 'Stop'), tStop, h('label', {}, 'Parity'), tParity));
      } else trigForm.append(h('div', { class: 'help' }, 'Up to 8 bytes; XX matches any byte. Triggers on a rising edge after the match.'));
    } else if (kind === 'edge_counter') {
      fillPins(tPin, 'tio4');
      trigForm.append(h('div', { class: 'row' }, h('label', {}, 'Pin'), tPin, h('label', {}, 'Mode'), edgeSel), h('div', { class: 'row' }, h('label', {}, 'Edges'), tEdges));
    } else if (kind === 'adc_level') {
      trigForm.append(h('div', { class: 'row' }, h('label', {}, 'Level'), tLevel), h('div', { class: 'help' }, 'Triggers when the measured signal crosses this level (-0.5 to 0.5, like trace values).'));
    } else if (kind === 'sequencer') {
      fillPins(tPin, 'tio4'); fillPins(tPin2, 'tio3');
      trigForm.append(h('div', { class: 'row' }, h('label', {}, tSeqOn, ' enabled')), h('div', { class: 'row' }, h('label', {}, 'First'), tPin, h('label', {}, 'then'), tPin2), h('div', { class: 'row' }, h('label', {}, 'Window'), tWs, h('span', { class: 'muted' }, 'to'), tWe, h('span', { class: 'muted' }, 'ADC cycles (0 = no limit)')));
    } else if (kind === 'sad') {
      trigForm.append(h('div', { class: 'row' }, h('label', {}, 'From sample'), tStart, h('label', {}, 'Threshold'), tThr), h('div', { class: 'help' }, 'The reference is cut from the newest trace, starting at this sample (128 samples on the Pro).'));
    }
    const entry = (caps.triggers || {})[kind] || NO_SCOPE;
    trigForm.querySelectorAll('input,select').forEach((e) => { if (!entry.available) e.disabled = true; else if (!e.dataset.pin && !e.classList.contains('gated')) e.disabled = false; });
  }
  function trigParams() {
    const k = kindSel.value;
    if (k === 'basic') return { kind: k, pins: Object.entries(pinChecks).filter(([, c]) => c.checked && !c.disabled).map(([p]) => p), op: opSel.value, edge: edgeSel.value };
    if (k === 'uart_decode') return { kind: k, pin: tPin.value, baud: +tBaud.value, pattern: tPattern.value };
    if (k === 'uart_pattern') return { kind: k, pin: tPin.value, baud: +tBaud.value, pattern: tPattern.value, rule: +tRule.value, data_bits: +tData.value, stop_bits: +tStop.value, parity: tParity.value };
    if (k === 'edge_counter') return { kind: k, pin: tPin.value, edges: +tEdges.value, edge: edgeSel.value };
    if (k === 'adc_level') return { kind: k, level: +tLevel.value };
    if (k === 'sequencer') return { kind: k, enabled: tSeqOn.checked, pins: [tPin.value, tPin2.value], window_start: +tWs.value, window_end: +tWe.value };
    return { kind: k, threshold: +tThr.value, start: +tStart.value };
  }
  function trigSummary(t) {
    const c = (t && t.current) || {};
    const s = c['trigger.module'] ? `module ${c['trigger.module']}, ${c['trigger.triggers'] || '-'}, ${c['adc.basic_mode'] || '-'}` : '';
    trigNow.textContent = s ? 'Active: ' + s : '';
    ctx.emit('trigger-summary', s ? `Trigger: ${s}` : '');
  }
  async function trigApply() {
    try { const r = await put('/api/interfaces/trigger', trigParams()); trigSummary(r); toast('Trigger applied; captures use it now', 'ok'); } catch (e) { toast(errMsg(e), 'err', 7000); }
  }
  const trigApplyBtn = h('button', { class: 'btn primary sm', onclick: trigApply }, 'Apply trigger');
  /** The SAD reference is cut from the newest trace: Apply waits for one. */
  function gateTrigApply() {
    if (!caps.connected || !caps.model) return;
    const needTrace = kindSel.value === 'sad' && !((ctx.status && ctx.status.traces && ctx.status.traces.count) > 0);
    gateEl(trigApplyBtn, !needTrace, 'capture a trace first: the SAD reference is cut from the newest trace');
  }
  ctx.on('status', gateTrigApply);
  kindSel.addEventListener('change', () => { trigFormFor(kindSel.value); gateTrigApply(); });
  trig.body.append(h('div', { class: 'row' }, h('label', {}, 'Type'), kindSel), trigForm,
    h('div', { class: 'row' }, trigApplyBtn, h('button', { class: 'link', onclick: () => ctx.showTab('scope') }, 'all trigger settings in the Scope tab')), trigNow);

  // ---------- bit-banger and 1-Wire ----------
  const bb = section('Bit-banger and 1-Wire', 'Husky synchronous bit patterns');
  const bbData = h('select', {}, h('option', { value: 'USERIO_D0' }, 'USERIO D0')), bbClk = h('select', {}, h('option', { value: 'USERIO_CK' }, 'USERIO CK')), owPin = h('select', {}, h('option', { value: 'USERIO_D0' }, 'USERIO D0'));
  const bbDiv = h('input', { type: 'number', value: 30, min: 2, max: 65534, step: 2, style: 'width:90px', title: 'even divider of the ADC clock: one time slot' });
  const owDiv = h('input', { type: 'number', value: '', min: 2, max: 65534, step: 2, style: 'width:90px', placeholder: 'auto', title: 'even divider of the ADC clock; auto picks about 10 us per slot' });
  const bbBits = h('input', { class: 'flex mono', value: '1010110000000000', placeholder: 'bits to send, e.g. 10110010' });
  const bbRec = h('input', { class: 'flex mono', value: '0000000011111111', placeholder: 'per bit: 1 = release and record' });
  const bbOut = h('div', { class: 'code out', style: 'display:block;min-height:22px' });
  const owOut = h('div', { class: 'kv' });
  async function bbSend() {
    try { const r = await post('/api/interfaces/bitbang', { bits: bbBits.value, record: bbRec.value || null, data_pin: bbData.value, clock_pin: bbClk.value, clk_div: +bbDiv.value }); bbOut.textContent = `sent ${r.sent} · recorded ${r.recorded || '(none)'}`; } catch (e) { toast(errMsg(e), 'err', 7000); }
  }
  async function ow(action) {
    try {
      const r = await post('/api/interfaces/onewire', { action, data_pin: owPin.value, clk_div: owDiv.value ? +owDiv.value : null });
      owOut.innerHTML = '';
      const rows = [['Presence', r.presence ? 'device answered' : 'no device']];
      if (r.rom) rows.push(['ROM', r.rom], ['Family', r.family], ['Serial', r.serial], ['CRC', `${r.crc} ${r.crc_ok ? '(ok)' : '(BAD)'}`]);
      rows.forEach(([k, v]) => owOut.append(h('span', { class: 'k' }, k), h('span', { class: 'mono' }, v)));
    } catch (e) { toast(errMsg(e), 'err', 7000); }
  }
  bb.body.append(
    h('div', { class: 'row' }, h('label', {}, 'Data pin'), bbData),
    h('div', { class: 'row' }, h('label', {}, 'Clock pin'), bbClk),
    h('div', { class: 'row' }, h('label', {}, 'Clock div'), bbDiv, h('span', { class: 'muted' }, 'ADC clock cycles per slot')),
    h('div', { class: 'row' }, h('label', {}, 'Send bits'), bbBits),
    h('div', { class: 'row' }, h('label', {}, 'Record'), bbRec, h('button', { class: 'btn sm', onclick: bbSend }, 'Send')), bbOut,
    h('h3', { class: 'sub-h' }, '1-Wire'),
    h('div', { class: 'row' }, h('label', {}, 'Data pin'), owPin),
    h('div', { class: 'row' }, h('label', {}, 'Clock div'), owDiv, h('span', { class: 'muted' }, 'empty: about 10 us per slot')),
    h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: () => ow('reset') }, 'Reset / presence'), h('button', { class: 'btn sm', onclick: () => ow('read_rom') }, 'Read ROM (0x33)')), owOut);

  // ---------- JTAG / SWD through OpenOCD ----------
  const ocd = section('JTAG and SWD', 'through OpenOCD (MPSSE mode)');
  const ocdInfo = h('div', { class: 'kv' });
  const trSeg = h('select', {}, h('option', { value: 'jtag' }, 'JTAG'), h('option', { value: 'swd' }, 'SWD'));
  const hdrSel = h('select', {}, h('option', { value: 'target' }, '20-pin target header'), h('option', { value: 'userio' }, 'Husky USERIO header'));
  const tgtFilter = h('input', { class: 'flex', placeholder: 'filter target configs (e.g. stm32f3)' });
  const tgtSel = h('select', { class: 'flex' });
  const pGdb = h('input', { type: 'number', value: 3333, style: 'width:76px', title: 'GDB port' });
  const pTel = h('input', { type: 'number', value: 4444, style: 'width:76px', title: 'telnet port' });
  const pTcl = h('input', { type: 'number', value: 6666, style: 'width:76px', title: 'TCL port' });
  const ocdLog = h('div', { class: 'console build', style: 'height:180px' });
  const ocdCmd = h('input', { class: 'flex mono', placeholder: 'OpenOCD command, e.g. targets, halt, reg, mdw 0x08000000 4' });
  const flashPath = h('input', { class: 'flex mono', placeholder: '.hex / .elf on the Studio machine (or a .bin with an address)' });
  const flashAddr = h('input', { class: 'mono flex', placeholder: '.bin address, e.g. 0x08000000' });
  const flashVerify = h('input', { type: 'checkbox', checked: true }), flashReset = h('input', { type: 'checkbox', checked: true });
  const btnMpsseOn = h('button', { class: 'btn sm', onclick: () => mpsse(true) }, 'Enable MPSSE');
  const btnMpsseOff = h('button', { class: 'btn sm', onclick: () => mpsse(false) }, 'Restore normal mode');
  const btnInstall = h('button', { class: 'btn sm', onclick: async () => { try { await post('/api/toolchains/openocd/install'); toast('Downloading OpenOCD (about 3 MB)…'); } catch (e) { toast(errMsg(e), 'err'); } } }, 'Install OpenOCD');
  const btnStart = h('button', { class: 'btn primary sm', onclick: ocdStart }, 'Start server');
  const btnStop = h('button', { class: 'btn sm', onclick: async () => { try { await post('/api/interfaces/openocd/stop'); refreshOcd(); } catch (e) { toast(errMsg(e), 'err'); } } }, 'Stop');
  let ocdTargets = [], ocdSeq = 0, ocdStatus = {};
  function logLine(r) {
    const cls = r.stream === 'error' || /^Error/.test(r.line) ? 'e' : (/^Warn/.test(r.line) ? 'w' : (r.stream === 'cmd' ? 'c' : ''));
    ocdLog.append(h('div', { class: cls }, r.line));
    while (ocdLog.childNodes.length > 800) ocdLog.removeChild(ocdLog.firstChild);
    ocdLog.scrollTop = ocdLog.scrollHeight;
    ocdSeq = Math.max(ocdSeq, r.seq || 0);
  }
  async function mpsse(on) {
    try {
      if (on && !confirm('Switch the scope into MPSSE (JTAG/SWD) mode? Studio releases the scope: capture, USB-CDC serial and the native programmers are unavailable until you restore normal mode.')) return;
      toast(on ? 'Enabling MPSSE…' : 'Restoring normal mode and reconnecting…');
      await post('/api/interfaces/openocd/mpsse', { enable: on, transport: trSeg.value, header: hdrSel.value });
      await ctx.refreshStatus(); refresh();
    } catch (e) { toast(errMsg(e), 'err', 8000); }
  }
  async function ocdStart() {
    try { await post('/api/interfaces/openocd/start', { target_cfg: tgtSel.value || null, transport: trSeg.value, ports: { gdb: +pGdb.value, telnet: +pTel.value, tcl: +pTcl.value } }); refreshOcd(); } catch (e) { toast(errMsg(e), 'err', 8000); }
  }
  async function ocdSend() {
    if (!ocdCmd.value) return;
    try { await post('/api/interfaces/openocd/command', { command: ocdCmd.value }); ocdCmd.value = ''; } catch (e) { toast(errMsg(e), 'err'); }
  }
  async function ocdFlash() {
    try { const r = await post('/api/interfaces/openocd/program', { path: flashPath.value, verify: flashVerify.checked, reset: flashReset.checked, address: flashAddr.value || null }); toast(r.ok ? 'Flashed with OpenOCD' : 'Flashing failed: ' + r.output, r.ok ? 'ok' : 'err', 8000); } catch (e) { toast(errMsg(e), 'err', 8000); }
  }
  ocdCmd.addEventListener('keydown', (e) => { if (e.key === 'Enter') ocdSend(); });
  function paintTargets() {
    const f = tgtFilter.value.toLowerCase(), cur = tgtSel.value || LS('ocdtarget', 'target/stm32f3x.cfg');
    tgtSel.innerHTML = '';
    tgtSel.append(h('option', { value: '' }, '(no target config)'));
    ocdTargets.filter((t) => !f || t.toLowerCase().includes(f)).slice(0, 400).forEach((t) => tgtSel.append(h('option', { value: t }, t.replace(/^target\//, ''))));
    if ([...tgtSel.options].some((o) => o.value === cur)) tgtSel.value = cur;
  }
  tgtFilter.addEventListener('input', paintTargets);
  tgtSel.addEventListener('change', () => SAVE('ocdtarget', tgtSel.value));
  ocd.body.append(ocdInfo,
    h('div', { class: 'row', style: 'margin-top:8px' }, h('label', {}, 'Mode'), trSeg, hdrSel),
    h('div', { class: 'row' }, btnMpsseOn, btnMpsseOff, btnInstall),
    h('div', { class: 'row' }, h('label', {}, 'Target'), tgtFilter),
    h('div', { class: 'row' }, h('label', {}, ''), tgtSel),
    h('div', { class: 'row' }, h('label', {}, 'Ports'), pGdb, pTel, pTcl),
    h('div', { class: 'help', style: 'margin:-6px 0 10px 104px' }, 'GDB, telnet and TCL (commands) ports'),
    h('div', { class: 'row' }, btnStart, btnStop),
    ocdLog,
    h('div', { class: 'row', style: 'margin-top:6px' }, ocdCmd, h('button', { class: 'btn sm', onclick: ocdSend }, 'Run')),
    h('div', { class: 'row' }, h('label', {}, 'Flash'), flashPath),
    h('div', { class: 'row' }, h('label', {}, ''), flashAddr),
    h('div', { class: 'row' }, h('label', {}, ''), h('label', {}, flashVerify, ' verify'), h('label', {}, flashReset, ' reset'), h('button', { class: 'btn sm', onclick: ocdFlash }, 'Flash')),
    h('div', { class: 'help' }, 'Wiring on the 20-pin header: SCK to TCK/SWCLK, PDID to TMS/SWDIO, MISO to TDO, MOSI to TDI, PDIC to TRST. About 500 kHz. Connect GDB with: target extended-remote localhost:' + 3333));
  async function refreshOcd() {
    try { ocdStatus = await get('/api/interfaces/openocd'); } catch (e) { return; }
    const st = ocdStatus, m = st.mpsse;
    ocdInfo.innerHTML = '';
    [['OpenOCD', st.installed ? st.binary : 'not installed (install it here or from the Firmware tab toolchains)'], ['Server', st.running ? `running (pid ${st.pid}), GDB ${st.ports.gdb}, telnet ${st.ports.telnet}, TCL ${st.ports.tcl}` : (st.exit_code != null ? `stopped (exit ${st.exit_code})` : 'stopped')],
      ['MPSSE', m ? `${m.transport.toUpperCase()} on the ${m.header === 'userio' ? 'USERIO' : '20-pin target'} header (${m.model})` : 'off']].forEach(([k, v]) => ocdInfo.append(h('span', { class: 'k' }, k), h('span', { class: 'mono' }, v)));
    if ((st.log || []).length && !ocdLog.childNodes.length) st.log.forEach(logLine);
    mpsseBanner.style.display = m ? '' : 'none';
    mpsseBanner.textContent = m ? m.warning : '';
    applyOcdGates();
    if (st.installed && !ocdTargets.length) { try { ocdTargets = await get('/api/interfaces/openocd/targets'); paintTargets(); } catch (e) { /* ignore */ } }
  }
  function applyOcdGates() {
    const m = ocdStatus.mpsse, j = caps.jtag || NO_SCOPE, s = caps.swd || NO_SCOPE;
    const supported = !!m || j.available || s.available;
    ocd.set(supported ? { available: true } : (j.reason ? j : NO_SCOPE), m ? 'MPSSE on' : null);
    if (!supported) return;
    [...trSeg.options].forEach((o) => { const e = o.value === 'jtag' ? j : s; gateEl(o, !!m || e.available, e.reason); });
    gateEl(hdrSel.options[1], !!m || ((caps[trSeg.value] || {}).headers || []).includes('USERIO 20-pin'), 'the USERIO header (with SWD: SAM firmware 1.4 or newer) is on the Husky only');
    btnMpsseOn.disabled = !!m; btnMpsseOff.disabled = !m;
    trSeg.disabled = hdrSel.disabled = !!m;
    btnInstall.disabled = !!ocdStatus.installed;
    btnInstall.title = ocdStatus.installed ? 'already installed' : 'download the pinned, SHA-256 verified xPack OpenOCD';
    const canRun = !!m && ocdStatus.installed;
    btnStart.disabled = !canRun || ocdStatus.running; btnStop.disabled = !ocdStatus.running;
    btnStart.title = !m ? 'enable MPSSE first' : (!ocdStatus.installed ? 'install OpenOCD first' : '');
    [ocdCmd, flashPath, flashAddr, flashVerify, flashReset].forEach((x) => { x.disabled = !ocdStatus.running; });
    ocd.body.querySelectorAll('button').forEach((b) => { if (b.textContent === 'Run' || b.textContent === 'Flash') b.disabled = !ocdStatus.running; });
  }
  trSeg.addEventListener('change', applyOcdGates);

  // ---------- Arm trace and programmers ----------
  const trace = section('Arm trace (SWO / parallel)', 'Husky TraceWhisperer');
  const traceLink = h('a', { href: 'https://github.com/newaetech/chipwhisperer-jupyter/tree/main/demos/husky/trace', target: '_blank', rel: 'noopener', style: 'color:var(--info)' }, "NewAE's trace notebooks");
  trace.body.append(h('div', { class: 'help' }, 'Trace capture needs target firmware with trace support and per-target setup. Run it from the Notebook tab with ', traceLink, '; SWO wiring is TMS to D0, TCK to D1, TDO to D2 on the USERIO header.'));
  const progs = section('Programmers', 'what the Target tab can program');
  const progList = h('div', { class: 'kv' });
  progs.body.append(progList, h('div', { class: 'row', style: 'margin-top:8px' }, h('button', { class: 'link', onclick: () => ctx.showTab('target') }, 'open the Target tab')));

  el.append(mpsseBanner, h('div', { class: 'card' }, modelLine, overview), uart.card, ss.card, spi.card, gpio.card, uio.card, trig.card, bb.card, ocd.card, trace.card, progs.card);

  // ---------- capabilities and gating ----------
  function applyCaps() {
    const c = caps.connected && caps.model ? caps : null;
    const e = (k) => (c ? c[k] : null) || NO_SCOPE;
    overview.innerHTML = '';
    if (c) {
      modelLine.textContent = `${c.label}${c.simulated ? ' (simulated)' : ''}: available interfaces are enabled below; the rest show why not.`;
      [['UART', 'uart'], ['SimpleSerial', 'simpleserial'], ['SPI', 'spi'], ['GPIO', 'gpio'], ['USERIO', 'userio'], ['JTAG', 'jtag'], ['SWD', 'swd'], ['Bit-banger', 'bitbanger'], ['1-Wire', 'onewire'], ['Trace', 'trace']].forEach(([l, k]) => {
        const x = e(k); overview.append(h('span', { class: 'badge ' + (x.available ? 'ok' : 'err'), title: x.available ? 'available' : x.reason }, l));
      });
      overview.append(h('span', { class: 'badge', title: 'I2C, CAN and LIN have no ChipWhisperer hardware support' }, 'I2C / CAN: none'));
    } else modelLine.textContent = state.mpsse ? 'The scope is in MPSSE mode for OpenOCD; restore normal mode in the JTAG and SWD section.' : 'Connect a scope (or the simulator) to see which interfaces it offers.';
    const uartOk = uart.set(e('uart'));
    term.gate();
    if (uartOk) {
      const u = c.uart;
      [...rxSel.options].forEach((o) => gateEl(o, u.rx_pins.includes(o.value), u.remap ? 'TIO4 can only transmit' : 'the CW-Nano receives on TIO1 only'));
      [...txSel.options].forEach((o) => gateEl(o, u.tx_pins.includes(o.value), 'the CW-Nano transmits on TIO2 only'));
      [...paritySel.options].forEach((o) => gateEl(o, u.parity.includes(o.value)));
      showUart(state.uart);
    }
    if (ss.set(e('simpleserial'))) {
      const cdc = c.simpleserial.cdc || {};
      gateEl(ssBtns.cdc, !!cdc.available && !c.simulated, c.simulated ? 'the simulator has no USB-CDC port' : cdc.reason);
      if (ssBtns.cdc.disabled && ssVer === 'cdc') { ssVer = '2.1'; paintSeg(); }
      const sst = state.simpleserial || {};
      ssState.textContent = sst.target ? `target: ${sst.target}${sst.version ? ', SimpleSerial ' + sst.version : ''}` : 'no target connected';
      gateEl(ssSendBtn, !!sst.target, 'connect a target first (Connect target above)');
    }
    if (spi.set(e('spi'))) {
      [...spiCs.options].forEach((o) => gateEl(o, c.spi.pins.cs.includes(o.value)));
      const s = state.spi || {};
      spiState.className = 'badge ' + (s.enabled ? 'ok' : ''); spiState.textContent = s.enabled ? `on, ${s.speed / 1e6} MHz, CS ${pinLabel(s.cs)}` : 'off';
      spiOnly.forEach((b) => gateEl(b, !!s.enabled, 'enable the SPI master first'));
    }
    if (gpio.set(e('gpio'), c && !c.gpio.read.available ? 'drive only' : null)) gpioRead(); else gpioTable.innerHTML = '';
    if (uio.set(e('userio'))) uioRead(); else uioGrid.innerHTML = '';
    const anyTrig = c ? { available: true } : NO_SCOPE;
    if (trig.set(anyTrig)) {
      [...kindSel.options].forEach((o) => { const x = (c.triggers || {})[o.value] || {}; gateEl(o, !!x.available, x.reason); });
      if (kindSel.selectedOptions[0] && kindSel.selectedOptions[0].disabled) kindSel.value = 'basic';
      trigFormFor(kindSel.value);
      gateTrigApply();
      trigSummary(state.triggerNow);
    }
    if (bb.set(e('bitbanger'))) {
      const pins = c.bitbanger.pins;
      const all = ['USERIO_D0', 'USERIO_D1', 'USERIO_D2', 'USERIO_D3', 'USERIO_D4', 'USERIO_D5', 'USERIO_D6', 'USERIO_D7', 'USERIO_CK', 'TIO1', 'TIO2', 'TIO3', 'TIO4', 'target_pwr', 'nrst'];
      const fill = (sel, opts, def) => { const cur = sel.value; sel.innerHTML = ''; opts.forEach((p) => { const o = h('option', { value: p }, p.replace('USERIO_', 'USERIO ')); gateEl(o, p === 'disabled' || pins.includes(p), 'TIO, target_pwr and nRST bit-banging needs a Husky Plus'); sel.append(o); }); sel.value = cur && [...sel.options].some((o) => o.value === cur && !o.disabled) ? cur : def; };
      fill(bbData, all, 'USERIO_D0'); fill(bbClk, [...all.filter((p) => p !== 'target_pwr' && p !== 'nrst'), 'disabled'], 'USERIO_CK'); fill(owPin, all, 'USERIO_D0');
    }
    trace.set(e('trace'));
    progs.set(c ? { available: true } : NO_SCOPE, 'per model');
    progList.innerHTML = '';
    Object.entries((c && c.programmers) || {}).forEach(([k, v]) => progList.append(h('span', { class: 'k' }, k), h('span', { class: v.available ? 'ok' : 'muted', title: v.reason || '' }, v.available ? 'available' : v.reason)));
    applyOcdGates();
  }

  async function refresh() {
    try {
      const st = await get('/api/interfaces');
      state = st; caps = st.capabilities || { connected: false }; ctx.caps = caps;
      try { state.triggerNow = caps.connected ? await get('/api/interfaces/trigger') : null; } catch (e) { state.triggerNow = null; }
      ocdStatus = Object.assign({}, ocdStatus, st.openocd || {});
      applyCaps();
      ctx.emit('caps', caps);
      refreshOcd();
    } catch (e) { /* server down */ }
  }
  ctx.refreshCaps = refresh;
  let active = false, timer = null;
  function tick() {
    if (!active || !liveChk.checked || document.hidden) return;
    if (caps.gpio && caps.gpio.available && caps.gpio.read && caps.gpio.read.available) gpioRead();
    if (caps.userio && caps.userio.available) uioRead();
  }
  ctx.on('tab', (t) => { active = t === 'interfaces'; if (active) refresh(); clearInterval(timer); if (active) timer = setInterval(tick, 600); });
  ctx.on('scope-connected', refresh);
  ctx.on('scope-disconnected', refresh);
  ctx.on('target-connected', refresh);
  ctx.on('target-disconnected', refresh);
  ctx.on('openocd', (ev) => { if (ev.kind === 'log') { if (ev.seq > ocdSeq) logLine(ev); } else refreshOcd(); });
  ctx.on('toolchain', (ev) => { if (ev && (ev.id === 'openocd' || (ev.toolchain && ev.toolchain.id === 'openocd'))) refreshOcd(); });
  ctx.on('setting', (ev) => {
    if (ev.target !== 'scope' || !/^(io|trigger|adc\.basic_mode)/.test(ev.path || '')) return;
    if (active) refresh();
    else if (/^(trigger|adc\.basic_mode)/.test(ev.path || '')) get('/api/interfaces/trigger').then((t) => { state.triggerNow = t; trigSummary(t); }).catch(() => {});  // keep the Capture tab's trigger line current when the trigger changes from the Scope tab
  });
  refresh();
  return { refresh };
}

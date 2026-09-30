import { h, toast } from './api.js';

function copyBlock(text) {
  const pre = h('pre', { class: 'code' }, text);
  const b = h('button', { class: 'btn ghost sm', style: 'float:right;margin:-2px -4px 0 0', onclick: async () => { try { await navigator.clipboard.writeText(text); toast('Copied', 'ok', 1500); } catch (e) { toast('Copy failed: select the text instead', 'warn'); } } }, 'Copy');
  return h('div', {}, b, pre);
}

export function initHelp(ctx, el) {
  const mcpJson = JSON.stringify({ mcpServers: { 'chipwhisperer-studio': { command: 'cw-studio', args: ['mcp'] } } }, null, 2);
  el.append(
    h('h2', {}, 'Quick start'),
    h('div', { class: 'card' }, h('ol', { style: 'padding-left:18px;margin:0;display:grid;gap:6px' },
      h('li', {}, 'Plug in your ChipWhisperer. On Linux install the udev rule shown in the Connect tab first.'),
      h('li', {}, h('b', {}, 'Connect'), ': pick the device (or Auto-detect) and press Connect scope, then Connect target (SimpleSerial v2 for current firmware).'),
      h('li', {}, h('b', {}, 'Firmware'), ': choose a project and platform (e.g. simpleserial-aes for CWLITEARM) and press Build & program. Studio downloads the sources and compiler the first time.'),
      h('li', {}, h('b', {}, 'Scope'), ': tune gain, samples, offset and trigger; press Single to see a trace and adjust until the waveform looks right.'),
      h('li', {}, h('b', {}, 'Capture'), ': choose a trace count and press Run. The waveform updates live and traces are stored in memory.'),
      h('li', {}, h('b', {}, 'Analysis'), ': run a CPA attack. With a fixed key the PGE (partial guessing entropy) plot shows every byte converging to 0.'),
      h('li', {}, h('b', {}, 'Glitch'), ': define parameter ranges and sweep. Successes light up in the scatter plot.'),
      h('li', {}, h('b', {}, 'Notebook'), ': run your own Python cell by cell, or NewAE\'s tutorials, against the same hardware. Captured traces appear in the Capture tab.'),
      h('li', {}, h('b', {}, 'Notes'), ' and ', h('b', {}, 'Calc'), ': keep notes, do quick maths, and select numbers anywhere to see their statistics in the log bar.'),
      h('li', {}, 'Export traces as a ChipWhisperer project (.cwp) or .npz to continue in Python or Jupyter.'))),
    h('h2', {}, 'No hardware?'),
    h('div', { class: 'card' }, 'Choose "Simulator" as the device. It behaves like a CW-Lite attached to an unprotected AES target: traces leak the S-box output, CPA recovers the key in a few hundred traces and glitch sweeps find a success window around ext_offset 20 to 60 with width 5 to 40.'),
    h('h2', {}, 'AI agents (MCP)'),
    h('div', { class: 'card' },
      h('p', { style: 'margin:0 0 10px' }, 'Studio includes a Model Context Protocol server with tools for every feature in this window: connecting, all scope and target settings, programming, SimpleSerial, capture, traces, CPA, glitching, toolchains and firmware builds. Agents attach to this running Studio, so you can watch what they do live.'),
      h('div', { class: 'muted', style: 'margin-bottom:6px' }, 'Claude Code:'),
      copyBlock('claude mcp add chipwhisperer-studio -- cw-studio mcp'),
      h('div', { class: 'muted', style: 'margin:10px 0 6px' }, 'Claude Desktop, Cursor and other clients (mcpServers config):'),
      copyBlock(mcpJson),
      h('div', { class: 'help' }, 'Standalone bundle: use the full path to ChipWhispererStudio as the command. Add --simulate to try it without hardware, or --transport streamable-http to serve MCP over HTTP.')),
    h('h2', {}, 'Keyboard'),
    h('div', { class: 'card' }, h('div', { class: 'kv' },
      h('span', { class: 'k' }, 'S'), h('span', {}, 'single capture'),
      h('span', { class: 'k' }, 'R'), h('span', {}, 'run capture'),
      h('span', { class: 'k' }, 'Esc'), h('span', {}, 'stop'),
      h('span', { class: 'k' }, 'Space'), h('span', {}, 'pause or resume the display'),
      h('span', { class: 'k' }, 'Left / Right'), h('span', {}, 'previous or next trace (browse mode)'),
      h('span', { class: 'k' }, 'Drag'), h('span', {}, 'zoom X'),
      h('span', { class: 'k' }, 'Double-click'), h('span', {}, 'fit'),
      h('span', { class: 'k' }, 'Click / Shift+click'), h('span', {}, 'cursor A / B'))),
    h('h2', {}, 'Remote use and scripting'),
    h('div', { class: 'card' }, 'Start Studio with ', h('code', {}, '--host 0.0.0.0'), ' on the machine that has the hardware and open ', h('code', {}, 'http://<that-machine>:8765/'), ' from anywhere on the network. Everything the UI does is also available over HTTP: ', h('a', { href: '/api/docs', target: '_blank', style: 'color:var(--info)' }, 'interactive API docs'), '.'),
    h('div', { class: 'help', style: 'margin-top:14px' }, `ChipWhisperer Studio ${ctx.meta.version} · independent project, not affiliated with NewAE Technology`),
  );
}

import { h, toast } from './api.js';

function copyBlock(text) {
  const pre = h('pre', { class: 'code' }, text);
  const b = h('button', { class: 'btn ghost sm', style: 'float:right;margin:-2px -4px 0 0', onclick: async () => { try { await navigator.clipboard.writeText(text); toast('Copied', 'ok', 1500); } catch (e) { toast('Copy failed: select the text instead', 'warn'); } } }, 'Copy');
  return h('div', {}, b, pre);
}

export function initHelp(ctx, el) {
  // The command for this installation (from /api/meta): cw-studio for a pip install; in a bundle the full path of its executable, which in the Windows window build is the console program cw-studio.exe, as the windowed ChipWhispererStudio.exe has no standard input and output for MCP.
  const mcp = (ctx.meta && ctx.meta.mcp_command) || { command: 'cw-studio', args: ['mcp'] };
  const shellArg = (a) => (/^[\w@%+=:,./\\-]+$/.test(a) ? a : '"' + a.replace(/"/g, '\\"') + '"');
  const mcpJson = JSON.stringify({ mcpServers: { 'chipwhisperer-studio': { command: mcp.command, args: mcp.args } } }, null, 2);
  el.append(
    h('h2', {}, 'Quick start'),
    h('div', { class: 'card' }, h('ol', { style: 'padding-left:18px;margin:0;display:grid;gap:6px' },
      h('li', {}, 'Plug in your ChipWhisperer. On Linux install the udev rule shown in the Connect tab first.'),
      h('li', {}, h('b', {}, 'Connect'), ': pick the device (or Auto-detect) and press Connect scope, then Connect target (SimpleSerial v2 for current firmware).'),
      h('li', {}, h('b', {}, 'Interfaces'), ': UART settings and terminal, SimpleSerial, SPI, GPIO and USERIO pins, trigger set-up, the bit-banger, 1-Wire and JTAG/SWD through OpenOCD. Only what the connected ChipWhisperer supports is enabled; the rest is shown with the reason.'),
      h('li', {}, h('b', {}, 'Firmware'), ': choose a project and platform (e.g. simpleserial-aes for CWLITEARM) and press Build & program. Studio downloads the sources and compiler the first time.'),
      h('li', {}, h('b', {}, 'Scope'), ': tune gain, samples, offset and trigger; press Single to see a trace and adjust until the waveform looks right.'),
      h('li', {}, h('b', {}, 'Capture'), ': choose a trace count and press Run. The waveform updates live and traces are stored in memory.'),
      h('li', {}, h('b', {}, 'Code'), ': build a code map of the firmware (the one programmed in this session, else the newest build) to see which functions and source lines run when. Tick "code" in the waveform toolbar for the band under the plot, and ctrl+drag on the plot to look up a sample range.'),
      h('li', {}, h('b', {}, 'Analysis'), ': run a CPA attack. With a fixed key the PGE (partial guessing entropy) plot shows every byte converging to 0.'),
      h('li', {}, h('b', {}, 'Glitch'), ': define parameter ranges and sweep. Successes light up in the scatter plot.'),
      h('li', {}, h('b', {}, 'Notebook'), ': run your own Python cell by cell, or NewAE\'s tutorials, against the same hardware. Open several notebooks as tabs and drag one to the side to split the view; each notebook has its own variables, and cells of all notebooks run one at a time. Captured traces appear in the Capture tab.'),
      h('li', {}, h('b', {}, 'Logic'), ': capture digital signals from the Husky logic analyser, a threshold on the analog input of any ChipWhisperer, the simulator, sigrok-supported analysers or a VCD, CSV or .sr file, and decode UART, SimpleSerial, SPI, I2C, 1-Wire, CAN, JTAG and SWD.'),
      h('li', {}, h('b', {}, 'Notes'), ' and ', h('b', {}, 'Calc'), ': keep notes, do quick maths, and select numbers anywhere to see their statistics in the log bar.'),
      h('li', {}, 'Export traces as a ChipWhisperer project (.cwp) or .npz to continue in Python or Jupyter.'))),
    h('h2', {}, 'No hardware?'),
    h('div', { class: 'card' }, 'Choose "Simulator" as the device and, under "Simulate as", the ChipWhisperer it stands in for (Husky by default): Studio then offers exactly the protocols, triggers and programmers of that model. The simulated target is an unprotected AES implementation: traces leak the S-box output, CPA recovers the key in a few hundred traces and glitch sweeps find a success window around ext_offset 20 to 60 with width 5 to 40. Programming a firmware ELF (or a .hex built by Studio, which keeps its .elf) makes the simulator run that firmware in an emulator, so responses and traces come from its real code. The trigger only fires on TIO4, as the target drives it.'),
    h('h2', {}, 'AI agents (MCP)'),
    h('div', { class: 'card' },
      h('p', { style: 'margin:0 0 10px' }, 'Studio includes a Model Context Protocol server with tools for every feature in this window: connecting, all scope and target settings, programming, SimpleSerial, capture, traces, CPA, glitching, toolchains and firmware builds, notebooks and their kernels, the hardware interfaces (UART, SPI, GPIO, triggers, OpenOCD), the logic analyser and the code map. Agents attach to this running Studio, so you can watch what they do live.'),
      h('div', { class: 'muted', style: 'margin-bottom:6px' }, 'Claude Code:'),
      copyBlock('claude mcp add chipwhisperer-studio -- ' + [mcp.command, ...mcp.args].map(shellArg).join(' ')),
      h('div', { class: 'muted', style: 'margin:10px 0 6px' }, 'Claude Desktop, Cursor and other clients (mcpServers config):'),
      copyBlock(mcpJson),
      h('div', { class: 'help' }, 'These commands are for this installation. Elsewhere: after pip install the command is cw-studio. In a standalone bundle it is the full path of the bundle\'s program: on Windows cw-studio.exe in the window build (ChipWhispererStudio.exe there is a windowed program without a console, so it cannot serve MCP) and ChipWhispererStudio.exe in the Web build; on macOS ChipWhisperer Studio.app/Contents/MacOS/ChipWhispererStudio; on Linux ChipWhispererStudio in the bundle folder. Add --simulate to try it without hardware, or --transport streamable-http to serve MCP over HTTP.')),
    h('h2', {}, 'Keyboard'),
    h('div', { class: 'card' }, h('div', { class: 'kv' },
      h('span', { class: 'k' }, 'S'), h('span', {}, 'single capture (from every tab, except while a notebook has the focus: in a cell or after clicking in it)'),
      h('span', { class: 'k' }, 'R'), h('span', {}, 'run capture (from every tab, except while a notebook has the focus)'),
      h('span', { class: 'k' }, 'Esc'), h('span', {}, 'stop (in the Logic tab: stop the logic capture)'),
      h('span', { class: 'k' }, 'Space'), h('span', {}, 'pause or resume the display'),
      h('span', { class: 'k' }, 'Left / Right'), h('span', {}, 'previous or next trace (browse mode)'),
      h('span', { class: 'k' }, 'Drag'), h('span', {}, 'zoom X'),
      h('span', { class: 'k' }, 'Ctrl+drag'), h('span', {}, 'pick a sample range for the code map'),
      h('span', { class: 'k' }, '+ / -'), h('span', {}, 'zoom in or out (around cursor A)'),
      h('span', { class: 'k' }, 'Double-click'), h('span', {}, 'fit'),
      h('span', { class: 'k' }, 'Click / Shift+click'), h('span', {}, 'cursor A / B')),
      h('div', { class: 'help', style: 'margin:10px 0 6px' }, 'Logic tab (mouse over the plot):'),
      h('div', { class: 'kv' },
        h('span', { class: 'k' }, '+ / -'), h('span', {}, 'zoom in or out around the mouse'),
        h('span', { class: 'k' }, 'Left / Right'), h('span', {}, 'pan'),
        h('span', { class: 'k' }, 'Home / End'), h('span', {}, 'start or end of the capture'),
        h('span', { class: 'k' }, 'F'), h('span', {}, 'fit'),
        h('span', { class: 'k' }, 'A / B'), h('span', {}, 'place cursor A or B at the mouse')),
      h('div', { class: 'help', style: 'margin:10px 0 6px' }, 'Notebook tab:'),
      h('div', { class: 'kv' },
        h('span', { class: 'k' }, 'Shift+Enter'), h('span', {}, 'run the cell and move to the next one'),
        h('span', { class: 'k' }, 'Ctrl+Enter'), h('span', {}, 'run the cell'),
        h('span', { class: 'k' }, 'Ctrl+S'), h('span', {}, 'save the notebook'))),
    h('h2', {}, 'Remote use and scripting'),
    h('div', { class: 'card' }, 'Start Studio with ', h('code', {}, '--host 0.0.0.0'), ' on the machine that has the hardware and open ', h('code', {}, 'http://<that-machine>:8765/'), ' from anywhere on the network. Everything the UI does is also available over HTTP: ', h('a', { href: '/api/docs', target: '_blank', style: 'color:var(--info)' }, 'API endpoint list'), '.'),
    h('div', { class: 'help', style: 'margin-top:14px' }, `ChipWhisperer Studio ${ctx.meta.version} · independent project, not affiliated with NewAE Technology`),
  );
}

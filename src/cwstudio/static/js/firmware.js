// Firmware tab: build ChipWhisperer firmware with on-demand toolchains, keep the sources in sync with NewAE's GitHub, manage compilers.
import { h, get, post, put, del, toast, fmtBytes } from './api.js';

const ARCH_LABEL = { arm: 'Arm Cortex-M', avr: 'AVR / XMEGA', riscv: 'RISC-V', tricore: 'TriCore', ppc: 'PowerPC', rx: 'Renesas RX', pic24: 'PIC24' };
const ICON = {
  build: '<svg class="i" viewBox="0 0 24 24"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/></svg>',
  chip: '<svg class="i" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/></svg>',
  down: '<svg class="i" viewBox="0 0 24 24"><path d="M12 3v12M7 10l5 5 5-5M4 21h16"/></svg>',
  refresh: '<svg class="i" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6"/></svg>',
  trash: '<svg class="i" viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>',
  x: '<svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
};
const btn = (cls, icon, label, onclick, attrs = {}) => h('button', Object.assign({ class: 'btn ' + cls, onclick, html: (ICON[icon] || '') + `<span>${label}</span>` }, attrs));
const ago = (t) => { const s = (Date.now() / 1000) - t; if (s < 90) return 'just now'; if (s < 5400) return `${Math.round(s / 60)} min ago`; if (s < 172800) return `${Math.round(s / 3600)} h ago`; return `${Math.round(s / 86400)} days ago`; };

export function initFirmware(ctx, el) {
  let cat = null, tcs = null, lastBuild = null, logNext = 0;

  // ---------------- build card ----------------
  const projSel = h('select', { class: 'flex' });
  const platSel = h('select', { class: 'flex' });
  let compiler = 'gcc';
  const seg = h('div', { class: 'seg' });
  const segBtn = (v, label) => h('button', { onclick: () => { compiler = v; [...seg.children].forEach((b) => b.classList.toggle('on', b.dataset.v === v)); updateToolchainHint(); }, 'data-v': v, class: v === compiler ? 'on' : '' }, label);
  seg.append(segBtn('gcc', 'GCC'), segBtn('clang', 'Clang'));
  const cryptoSel = h('select', { class: 'flex' }, ...(ctx.meta.crypto_targets || []).map((c) => h('option', { value: c }, c)));
  const ssSel = h('select', { class: 'flex' }, ...(ctx.meta.ss_versions || []).map((c) => h('option', { value: c }, c.replace('SS_VER_', 'v').replace('_', '.'))));
  const cflagsIn = h('input', { class: 'flex mono', placeholder: 'e.g. -DMY_FLAG -O3' });
  const makeArgsIn = h('input', { class: 'flex mono', placeholder: 'e.g. OPT=2 EXTRA_OPTS=NO_EXTRA_OPTS' });
  const cleanChk = h('input', { type: 'checkbox', checked: true });
  const tcHint = h('div', { class: 'help' });
  const buildBtn = btn('primary', 'build', 'Build', () => build(false));
  const buildProgBtn = btn('', 'chip', 'Build & program', () => build(true), { title: 'Build, then program the connected target with the platform\'s programmer' });
  const cancelBtn = btn('danger', 'x', 'Cancel', async () => { try { await post('/api/firmware/build/cancel'); } catch (e) { toast(e.message, 'err'); } });
  cancelBtn.style.display = 'none';
  const progress = h('progress', { style: 'display:none;margin-top:10px' });
  const result = h('div');
  const logEl = h('div', { class: 'console build' });
  const logBox = h('details', {}, h('summary', {}, 'Build output'), logEl);

  function selectedPlatform() { return (cat && cat.platforms.find((p) => p.name === platSel.value)) || null; }
  function selectedProject() { return (cat && cat.projects.find((p) => p.name === projSel.value)) || null; }

  function fillCatalogue() {
    const keepProj = projSel.value || localGet('fw.project', 'simpleserial-aes');
    const keepPlat = platSel.value || localGet('fw.platform', 'CWLITEARM');
    projSel.innerHTML = ''; platSel.innerHTML = '';
    cat.projects.forEach((p) => projSel.append(h('option', { value: p.name }, p.name)));
    const groups = {};
    cat.platforms.forEach((p) => { (groups[p.arch] = groups[p.arch] || []).push(p); });
    Object.keys(ARCH_LABEL).filter((a) => groups[a]).forEach((a) => {
      const og = h('optgroup', { label: ARCH_LABEL[a] });
      groups[a].forEach((p) => og.append(h('option', { value: p.name, disabled: !p.available }, `${p.name}  ·  ${p.label.replace(/^CW308T?: /, '')}`)));
      platSel.append(og);
    });
    if ([...projSel.options].some((o) => o.value === keepProj)) projSel.value = keepProj;
    if ([...platSel.options].some((o) => o.value === keepPlat)) platSel.value = keepPlat;
    onProjectChange(); updateToolchainHint();
  }
  function onProjectChange() {
    const p = selectedProject();
    cryptoSel.disabled = p ? !p.crypto : false;
    localSet('fw.project', projSel.value);
  }
  projSel.addEventListener('change', onProjectChange);
  platSel.addEventListener('change', () => { localSet('fw.platform', platSel.value); updateToolchainHint(); });

  function findTc(arch, comp) {
    if (!tcs) return null;
    const list = tcs.toolchains.filter((t) => t.compiler === comp && (t.arch || []).includes(arch));
    return list.find((t) => t.installed) || list.find((t) => t.system) || null;
  }
  function updateToolchainHint() {
    const p = selectedPlatform();
    tcHint.innerHTML = '';
    if (!p || !tcs) return;
    const need = [['gcc', findTc(p.arch, 'gcc')]];
    if (compiler === 'clang') need.push(['clang', findTc(p.arch, 'clang')]);
    const parts = [];
    let missing = null;
    need.forEach(([comp, t]) => {
      if (t) parts.push(h('span', { class: 'badge ok' }, `${t.name.split(' (')[0]} ${t.installed ? t.version : '(system)'}`));
      else { missing = comp; parts.push(h('span', { class: 'badge warn' }, `No ${comp === 'gcc' ? 'GCC' : 'clang'} for ${ARCH_LABEL[p.arch] || p.arch}`)); }
    });
    tcHint.append(h('div', { class: 'row', style: 'margin:0;gap:6px' }, h('span', { class: 'muted' }, 'Toolchain'), ...parts));
    if (missing) {
      const cand = tcs.toolchains.find((t) => t.compiler === missing && (t.arch || []).includes(p.arch) && t.available);
      if (cand) tcHint.append(h('div', { class: 'help' }, `${cand.name} is not installed yet (${fmtBytes(cand.size || 0)} download). `, h('button', { class: 'link', onclick: () => install(cand.id) }, 'Install now')));
      else tcHint.append(h('div', { class: 'help' }, `Studio has no download for ${p.arch}; add your own under Toolchains below.`));
    }
    if (!p.programmer) tcHint.append(h('div', { class: 'help' }, 'Studio has no built-in programmer for this platform: flash the .hex with the vendor tool.'));
  }

  async function build(andProgram) {
    const params = { project: projSel.value, platform: platSel.value, compiler, crypto_target: cryptoSel.disabled ? null : cryptoSel.value, ss_ver: ssSel.value, cflags: cflagsIn.value, make_args: makeArgsIn.value, clean: cleanChk.checked };
    logEl.innerHTML = ''; logNext = 0; logBox.open = true; result.innerHTML = '';
    try {
      const r = await post('/api/firmware/build', params);
      pendingProgram = andProgram;
      applyBuild(r);
    } catch (e) { toast(e.message, 'err', 8000); result.replaceChildren(h('div', { class: 'result err' }, e.message)); }
  }
  let pendingProgram = false;
  async function programLast() {
    try {
      const r = await post('/api/firmware/program', {});
      toast(`Programmed ${r.bytes} bytes with ${r.programmer}${r.simulated ? ' (simulated)' : ''}`, 'ok', 6000);
    } catch (e) { toast(e.message, 'err', 8000); }
  }
  function appendLog(lines) {
    const frag = document.createDocumentFragment();
    lines.forEach((ln) => { const cls = /\berror\b|\*\*\*/i.test(ln) ? 'e' : (/\bwarning\b/i.test(ln) ? 'w' : (/^\s*(\$ |-e |rm -f|mkdir -p)/.test(ln) ? 'c' : '')); frag.append(h('div', { class: cls }, ln)); });
    const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    logEl.append(frag);
    while (logEl.childNodes.length > 3000) logEl.removeChild(logEl.firstChild);
    if (atBottom) logEl.scrollTop = logEl.scrollHeight;
  }
  function applyBuild(b) {
    lastBuild = b;
    const running = b.state === 'running';
    buildBtn.disabled = running; buildProgBtn.disabled = running;
    cancelBtn.style.display = running ? '' : 'none';
    progress.style.display = running ? '' : 'none';
    if (running) progress.removeAttribute('value');
    if (b.state === 'ok') {
      const sz = b.size || {};
      const total = (sz.text || 0) + (sz.data || 0) + (sz.bss || 0) || 1;
      const bar = h('div', { class: 'sizebar' }, ...[['text', 'var(--info)'], ['data', 'var(--warn)'], ['bss', 'var(--accent)']].map(([k, c]) => h('span', { style: `width:${100 * (sz[k] || 0) / total}%;background:${c}`, title: `${k}: ${sz[k] || 0} bytes` })));
      result.replaceChildren(h('div', { class: 'result ok' },
        h('div', { class: 'row', style: 'margin-bottom:6px' }, h('span', { class: 'badge ok' }, 'Build succeeded'), h('span', { class: 'muted' }, `${b.platform} · ${b.compiler} · ${b.seconds}s`)),
        b.size ? bar : null,
        b.size ? h('div', { class: 'legend' }, ...['text', 'data', 'bss'].map((k, i) => h('span', {}, h('i', { style: `background:${['var(--info)', 'var(--warn)', 'var(--accent)'][i]}` }), `${k} ${sz[k]} B`))) : null,
        h('div', { class: 'help mono', style: 'margin-top:8px', title: b.hex }, shortPath(b.hex)),
        h('div', { class: 'row', style: 'margin-top:10px' },
          b.programmer ? btn('primary sm', 'chip', `Program with ${b.programmer}`, programLast) : null,
          h('a', { class: 'btn sm', href: `/api/firmware/builds/${encodeURIComponent(b.hex.split(/[\\/]/).pop())}`, download: '', html: ICON.down + '<span>Download .hex</span>' }))));
      if (pendingProgram) { pendingProgram = false; if (b.programmer) programLast(); }
      logBox.open = false;
    } else if (b.state === 'failed' || b.state === 'cancelled') {
      pendingProgram = false;
      result.replaceChildren(h('div', { class: 'result err' }, h('div', { class: 'row', style: 'margin-bottom:4px' }, h('span', { class: 'badge err' }, b.state === 'cancelled' ? 'Cancelled' : 'Build failed'), h('span', { class: 'muted' }, `${b.platform} · ${b.compiler}`)), b.error ? h('div', { class: 'mono', style: 'font-size:12px;overflow-wrap:anywhere' }, b.error) : null));
      logBox.open = true;
    } else if (running) {
      result.replaceChildren(h('div', { class: 'help' }, `Building ${b.project} for ${b.platform} with ${b.compiler}…`));
    }
  }

  const buildCard = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'Build firmware'), h('span', { class: 'sub' }, 'ChipWhisperer makefiles, your choice of compiler')),
    h('div', { class: 'row' }, h('label', {}, 'Project'), projSel),
    h('div', { class: 'row' }, h('label', {}, 'Platform'), platSel),
    h('div', { class: 'row' }, h('label', {}, 'Compiler'), seg),
    h('div', { class: 'row' }, h('label', {}, 'Crypto'), cryptoSel),
    h('div', { class: 'row' }, h('label', {}, 'SimpleSerial'), ssSel),
    h('details', {}, h('summary', {}, 'Advanced options'),
      h('div', { class: 'row' }, h('label', {}, 'Extra CFLAGS'), cflagsIn),
      h('div', { class: 'row' }, h('label', {}, 'Make args'), makeArgsIn),
      h('div', { class: 'row' }, h('label', {}, ''), h('label', {}, cleanChk, 'Clean before building'))),
    tcHint,
    h('div', { class: 'row', style: 'margin-top:12px' }, buildBtn, buildProgBtn, cancelBtn),
    progress, result, h('div', { style: 'margin-top:10px' }, logBox));

  // ---------------- sources card ----------------
  const srcInfo = h('div');
  const channelSel = h('select', { class: 'flex' }, h('option', { value: 'develop' }, 'develop (latest changes)'), h('option', { value: 'latest-release' }, 'Latest release'), h('option', { value: '__custom' }, 'Tag or commit…'));
  const channelIn = h('input', { class: 'flex mono', placeholder: 'e.g. v6.0.0 or a commit hash', style: 'display:none' });
  const folderIn = h('input', { class: 'flex mono', placeholder: 'path to firmware/mcu of your ChipWhisperer checkout' });
  const srcProgress = h('progress', { style: 'display:none;margin:8px 0' });
  const checkBtn = btn('sm', 'refresh', 'Check for updates', async () => { checkBtn.disabled = true; try { renderSources(await post('/api/firmware/sources/check')); } catch (e) { toast(e.message, 'err', 7000); } finally { checkBtn.disabled = false; } });
  const fetchBtn = btn('primary sm', 'down', 'Download', async () => { try { renderSources(await post('/api/firmware/sources/fetch', {})); } catch (e) { toast(e.message, 'err', 7000); } });
  channelSel.addEventListener('change', async () => {
    channelIn.style.display = channelSel.value === '__custom' ? '' : 'none';
    if (channelSel.value !== '__custom') await setChannel(channelSel.value);
  });
  channelIn.addEventListener('keydown', (e) => { if (e.key === 'Enter' && channelIn.value.trim()) setChannel(channelIn.value.trim()); });
  async function setChannel(c) { try { renderSources(await put('/api/firmware/channel', { channel: c })); toast(`Following ${c}. Press Update to download it.`, 'ok'); } catch (e) { toast(e.message, 'err'); } }
  async function setFolder(root) { try { renderSources(await put('/api/firmware/sources', { root })); await loadCatalogue(); toast(root ? 'Using your firmware folder' : 'Using the downloaded sources', 'ok'); } catch (e) { toast(e.message, 'err', 8000); } }

  function renderSources(s) {
    const busy = s.job && ['resolving', 'downloading', 'extracting'].includes(s.job.state);
    srcProgress.style.display = busy ? '' : 'none';
    if (busy) { if (s.job.total) { srcProgress.max = s.job.total; srcProgress.value = s.job.done; } else srcProgress.removeAttribute('value'); }
    fetchBtn.disabled = busy || s.custom;
    checkBtn.disabled = busy || s.custom;
    const ch = s.channel || 'develop';
    if (['develop', 'latest-release'].includes(ch)) { channelSel.value = ch; channelIn.style.display = 'none'; } else { channelSel.value = '__custom'; channelIn.style.display = ''; channelIn.value = ch; }
    const inst = s.installed;
    const lc = s.last_check;
    const upd = lc && lc.update_available;
    fetchBtn.querySelector('span').textContent = inst ? (upd ? 'Update now' : 'Re-download') : 'Download';
    let status;
    if (s.custom) status = h('span', { class: 'badge info' }, 'Your folder');
    else if (!inst) status = h('span', { class: 'badge warn' }, 'Not downloaded');
    else if (lc) status = upd ? h('span', { class: 'badge warn' }, 'Update available') : h('span', { class: 'badge ok' }, 'Up to date');
    else status = h('span', { class: 'badge' }, 'Not checked');
    const kv = h('div', { class: 'kv' },
      h('span', { class: 'k' }, 'Repository'), h('span', {}, h('a', { href: `https://github.com/${s.repo}`, target: '_blank', style: 'color:var(--info)' }, s.repo)),
      h('span', { class: 'k' }, 'Folder'), h('span', { class: 'mono', style: 'font-size:11.5px', title: s.root }, shortPath(s.root)));
    if (inst && !s.custom) kv.append(h('span', { class: 'k' }, 'Installed'), h('span', {}, h('code', {}, inst.commit.slice(0, 8)), ` ${inst.message || ''}`, h('div', { class: 'muted', style: 'font-size:11.5px' }, `${inst.ref}${inst.date ? ' · ' + inst.date.slice(0, 10) : ''} · downloaded ${ago(inst.installed)}`)));
    if (lc && upd) kv.append(h('span', { class: 'k' }, 'Latest'), h('span', {}, h('code', {}, lc.latest.commit.slice(0, 8)), ` ${lc.latest.message || ''}`, h('div', { class: 'muted', style: 'font-size:11.5px' }, (lc.latest.date || '').slice(0, 10))));
    if (s.job && s.job.state === 'error') kv.append(h('span', { class: 'k' }, 'Error'), h('span', { class: 'err' }, s.job.error));
    srcInfo.replaceChildren(h('div', { class: 'row', style: 'margin-bottom:10px' }, status, busy ? h('span', { class: 'muted' }, `${s.job.state}${s.job.total ? ' ' + Math.round(100 * s.job.done / s.job.total) + '%' : ''}…`) : null), kv);
  }
  const srcCard = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'Firmware sources'), h('span', { class: 'sub' }, 'Straight from NewAE on GitHub')),
    srcInfo, srcProgress,
    h('div', { class: 'row', style: 'margin-top:12px' }, h('label', {}, 'Follow'), channelSel), channelIn,
    h('div', { class: 'row', style: 'margin-top:10px' }, fetchBtn, checkBtn),
    h('details', { style: 'margin-top:8px' }, h('summary', {}, 'Use my own firmware folder'),
      h('div', { class: 'row' }, folderIn), h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: () => setFolder(folderIn.value.trim()) }, 'Use this folder'), h('button', { class: 'btn ghost sm', onclick: () => setFolder(null) }, 'Back to downloaded sources'))),
    h('div', { class: 'help' }, 'Sources are not bundled with Studio. New examples and fixes from NewAE arrive with Update, no new Studio release needed.'));

  // ---------------- toolchains card ----------------
  const tcList = h('div');
  const tcHost = h('span', { class: 'sub' });
  async function install(id) { try { await post(`/api/toolchains/${id}/install`); await loadToolchains(); } catch (e) { toast(e.message, 'err', 8000); } }
  async function remove(id, custom) {
    if (!confirm(custom ? 'Remove this custom toolchain?' : 'Delete this toolchain from disk? You can install it again later.')) return;
    try { if (custom) await del(`/api/toolchains/custom/${id}`); else await del(`/api/toolchains/${id}`); await loadToolchains(); } catch (e) { toast(e.message, 'err'); }
  }
  function renderToolchains() {
    tcHost.textContent = `${tcs.host}`;
    tcList.replaceChildren(...tcs.toolchains.filter((t) => t.available || t.installed || t.custom).map((t) => {
      const j = t.job && ['downloading', 'extracting'].includes(t.job.state) ? t.job : null;
      let badge;
      if (j) badge = h('span', { class: 'badge info' }, j.state === 'extracting' ? 'Unpacking…' : `Downloading ${j.total ? Math.round(100 * j.done / j.total) + '%' : ''}`);
      else if (t.installed) badge = h('span', { class: 'badge ok' }, 'Installed');
      else if (t.system) badge = h('span', { class: 'badge accent', title: t.system }, 'On system PATH');
      else if (t.job && t.job.state === 'error') badge = h('span', { class: 'badge err', title: t.job.error }, 'Failed');
      else badge = h('span', { class: 'badge' }, 'Not installed');
      const actions = h('div', { class: 'actions' });
      if (j) actions.append(btn('ghost sm', 'x', 'Cancel', () => post(`/api/toolchains/${t.id}/cancel`)));
      else if (t.installed && !t.custom) actions.append(btn('ghost sm', 'trash', 'Remove', () => remove(t.id, false)));
      else if (!t.installed && t.available) actions.append(btn('sm', 'down', 'Install', () => install(t.id)));
      if (t.custom) actions.append(btn('ghost sm', 'trash', 'Delete', () => remove(t.id, true)));
      const row = h('div', { class: 'tc' },
        h('div', { class: 'name' }, t.name),
        actions,
        h('div', { class: 'meta' }, badge, h('span', {}, t.version || ''), ...(t.arch || []).map((a) => h('span', { class: 'arch' }, a)), t.size && !t.installed ? h('span', {}, fmtBytes(t.size)) : null, t.source ? h('span', {}, t.source) : null));
      if (j) { const p = h('progress', { max: j.total || 1, value: j.done || 0 }); if (j.state === 'extracting' || !j.total) p.removeAttribute('value'); row.append(p); }
      if (t.job && t.job.state === 'error') row.append(h('div', { class: 'help err', style: 'grid-column:1/-1' }, t.job.error));
      return row;
    }));
    updateToolchainHint();
  }
  // custom toolchain form
  const cName = h('input', { class: 'flex', placeholder: 'Name, e.g. TriCore GCC 11' });
  const cComp = h('select', {}, h('option', { value: 'gcc' }, 'GCC'), h('option', { value: 'clang' }, 'clang'));
  const cArch = h('input', { class: 'flex mono', placeholder: 'arch, e.g. tricore or arm,riscv' });
  const cPrefix = h('input', { class: 'flex mono', placeholder: 'tool prefix, e.g. tricore-elf-' });
  const cUrl = h('input', { class: 'flex mono', placeholder: 'archive URL (.tar.gz / .tar.xz / .zip)' });
  const cSha = h('input', { class: 'flex mono', placeholder: 'SHA-256 of the archive (recommended)' });
  const cPath = h('input', { class: 'flex mono', placeholder: 'or an existing install folder' });
  async function addCustom() {
    try {
      const t = await post('/api/toolchains/custom', { name: cName.value, compiler: cComp.value, arch: cArch.value, prefix: cPrefix.value, url: cUrl.value || null, sha256: cSha.value || null, path: cPath.value || null });
      toast(`Added ${t.name}`, 'ok'); [cName, cArch, cPrefix, cUrl, cSha, cPath].forEach((i) => { i.value = ''; });
      await loadToolchains();
    } catch (e) { toast(e.message, 'err', 8000); }
  }
  const tcCard = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('span', { class: 'title' }, 'Toolchains'), tcHost, h('div', { class: 'end' }, btn('ghost sm', 'refresh', 'Refresh list', async () => { try { const r = await post('/api/toolchains/refresh'); toast(r.updated ? `Toolchain list updated (revision ${r.revision})` : 'Toolchain list is up to date', 'ok'); await loadToolchains(); } catch (e) { toast(e.message, 'err', 7000); } }))),
    tcList,
    h('details', { style: 'margin-top:12px' }, h('summary', {}, 'Add a custom toolchain'),
      h('div', { class: 'row' }, cName, cComp), h('div', { class: 'row' }, cArch, cPrefix), h('div', { class: 'row' }, cUrl), h('div', { class: 'row' }, cSha), h('div', { class: 'row' }, cPath),
      h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: addCustom }, 'Add toolchain')),
      h('div', { class: 'help' }, 'Use this for targets without a free pinned toolchain (TriCore, PowerPC, RX) or to pin a specific compiler version.')),
    h('div', { class: 'help' }, 'Compilers are downloaded on demand from their official releases, checked against pinned SHA-256 sums and work offline afterwards.'));

  el.append(buildCard, h('h2', {}, 'Sources'), srcCard, h('h2', {}, 'Compilers'), tcCard);

  // ---------------- data ----------------
  async function loadCatalogue() { try { cat = await get('/api/firmware'); renderSources(cat.sources); fillCatalogue(); } catch (e) { /* server down */ } }
  async function loadToolchains() { try { tcs = await get('/api/toolchains'); renderToolchains(); } catch (e) { /* server down */ } }
  async function loadBuild() {
    try {
      const b = await get('/api/firmware/build');
      if (b.state && b.state !== 'idle') {
        applyBuild(b);
        const lg = await get('/api/firmware/build/log'); appendLog(lg.lines.slice(-400)); logNext = lg.next;
      }
    } catch (e) { /* ignore */ }
  }
  let catTimer = null;
  ctx.on('toolchain', (t) => { if (!tcs) return; const i = tcs.toolchains.findIndex((x) => x.id === t.id); if (i >= 0) tcs.toolchains[i] = t; else tcs.toolchains.push(t); renderToolchains(); });
  ctx.on('firmware_sources', (s) => { renderSources(s); if (s.job && s.job.state === 'installed') { clearTimeout(catTimer); catTimer = setTimeout(loadCatalogue, 300); } });
  ctx.on('build', (b) => applyBuild(b));
  ctx.on('build_log', (ev) => { if (ev.start >= logNext || ev.start === 0) { appendLog(ev.lines); logNext = ev.start + ev.lines.length; } });
  ctx.on('tab', (t) => { if (t === 'firmware') { loadCatalogue(); loadToolchains(); } });
  loadCatalogue(); loadToolchains(); loadBuild();
  return { refresh: () => { loadCatalogue(); loadToolchains(); } };
}

function shortPath(p) { const parts = (p || '').split(/[\\/]/).filter(Boolean); return parts.length > 3 ? '…/' + parts.slice(-3).join('/') : p; }
function localGet(k, d) { try { return localStorage.getItem(k) || d; } catch (e) { return d; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } }

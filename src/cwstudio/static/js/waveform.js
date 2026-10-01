// Live waveform viewer built on uPlot.
import { h, fmtTime, toast, themeColors, axisStyle } from './api.js';

let COLORS = themeColors();
const PAUSE_HTML = '<svg class="i" viewBox="0 0 24 24"><path d="M9 5v14M15 5v14"/></svg><span>Pause</span>';
const PLAY_HTML = '<svg class="i" viewBox="0 0 24 24"><polygon points="7,4 20,12 7,20" fill="currentColor"/></svg><span>Resume</span>';


export class Waveform {
  constructor(plotEl, toolbarEl, footerEl) {
    this.plotEl = plotEl;
    this.toolbarEl = toolbarEl;
    this.footerEl = footerEl;
    this.u = null;
    this.mode = 'live';          // live | browse
    this.overlayN = 0;
    this.showMean = false;
    this.showEnv = false;
    this.autoY = true;
    this.timeAxis = false;
    this.sampleRate = null;      // Hz
    this.paused = false;
    this.cursors = { a: null, b: null };
    this.latest = null;          // { samples, header }
    this.browse = null;
    this.ring = [];              // overlay buffer (Float32Array)
    this.stats = null;           // { mean, std, min, max }
    this.corr = null;            // { samples, offset } from CPA
    this.pendingLive = null;
    this.rafScheduled = false;
    this.userZoomed = false;
    this.traceCount = 0;
    this.onBrowse = null;        // callback(index) -> fetch trace
    this.buildToolbar();
    this.buildPlot();
    new ResizeObserver(() => this.resize()).observe(plotEl);
    this.updateFooter();
  }

  // ---------- toolbar ----------
  buildToolbar() {
    const tb = this.toolbarEl;
    const mk = (label, el) => h('label', {}, label, el);
    this.modeSel = h('select', { onchange: () => { this.mode = this.modeSel.value; this.syncMode(); this.render(true); } },
      h('option', { value: 'live' }, 'Live (latest capture)'),
      h('option', { value: 'browse' }, 'Browse stored traces'));
    this.idxInput = h('input', { type: 'number', min: 0, value: 0, onchange: () => this.gotoIndex(+this.idxInput.value) });
    this.idxRange = h('input', { type: 'range', min: 0, max: 0, value: 0, oninput: () => { this.idxInput.value = this.idxRange.value; this.gotoIndex(+this.idxRange.value); } });
    this.overlaySel = h('select', { onchange: () => { this.overlayN = +this.overlaySel.value; this.ring = this.ring.slice(-this.overlayN); this.rebuild(); } },
      ...[0, 5, 10, 25, 50].map((n) => h('option', { value: n }, n ? `overlay ${n}` : 'no overlay')));
    this.meanChk = h('input', { type: 'checkbox', onchange: () => { this.showMean = this.meanChk.checked; this.rebuild(); if (this.showMean && this.onNeedStats) this.onNeedStats(); } });
    this.envChk = h('input', { type: 'checkbox', onchange: () => { this.showEnv = this.envChk.checked; this.rebuild(); if (this.showEnv && this.onNeedStats) this.onNeedStats(); } });
    this.autoChk = h('input', { type: 'checkbox', checked: true, onchange: () => { this.autoY = this.autoChk.checked; this.render(true); } });
    this.timeChk = h('input', { type: 'checkbox', onchange: () => { this.timeAxis = this.timeChk.checked; this.rebuild(); } });
    this.pauseBtn = h('button', { class: 'btn sm', onclick: () => { this.paused = !this.paused; this.pauseBtn.innerHTML = this.paused ? PLAY_HTML : PAUSE_HTML; this.pauseBtn.classList.toggle('active', this.paused); }, html: PAUSE_HTML });
    this.resetBtn = h('button', { class: 'btn sm', title: 'Reset zoom (double-click plot)', onclick: () => { this.userZoomed = false; this.render(true); }, html: '<svg class="i" viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg><span>Fit</span>' });
    this.zoomInBtn = h('button', { class: 'btn sm', title: 'Zoom in (+)', onclick: () => this.zoom(0.5), html: '<svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M16 16l5 5M8 11h6M11 8v6"/></svg>' });
    this.zoomOutBtn = h('button', { class: 'btn sm', title: 'Zoom out (-)', onclick: () => this.zoom(2), html: '<svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M16 16l5 5M8 11h6"/></svg>' });
    this.pngBtn = h('button', { class: 'btn sm', onclick: () => this.exportPng(), html: '<svg class="i" viewBox="0 0 24 24"><path d="M12 3v12M7 10l5 5 5-5M4 21h16"/></svg><span>PNG</span>' });
    this.clearCurBtn = h('button', { class: 'btn sm', title: 'Clear cursors', onclick: () => { this.cursors = { a: null, b: null }; this.redraw(); }, html: '<svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg><span>Cursors</span>' });
    this.browseBox = h('span', { style: 'display:none;align-items:center;gap:6px' }, mk('#', this.idxInput), this.idxRange);
    tb.append(
      mk('Source', this.modeSel), this.browseBox,
      h('span', { class: 'sep' }),
      this.overlaySel,
      mk(this.meanChk, 'mean'), mk(this.envChk, 'min/max'),
      h('span', { class: 'sep' }),
      mk(this.autoChk, 'auto Y'), mk(this.timeChk, 'time axis'),
      h('span', { class: 'sep' }),
      this.pauseBtn, this.zoomInBtn, this.zoomOutBtn, this.resetBtn, this.clearCurBtn, this.pngBtn,
      h('span', { class: 'spacer' }),
      h('span', { class: 'muted', style: 'font-size:11px' }, 'drag or +/-: zoom · dbl-click: fit · click: cursor A · shift+click: cursor B'),
    );
  }

  syncMode() {
    this.browseBox.style.display = this.mode === 'browse' ? 'inline-flex' : 'none';
    if (this.mode === 'browse' && this.onBrowse) this.gotoIndex(+this.idxInput.value);
  }

  setTraceCount(n) {
    this.traceCount = n;
    this.idxRange.max = Math.max(0, n - 1);
    this.idxInput.max = Math.max(0, n - 1);
  }

  gotoIndex(i) {
    if (!this.onBrowse) return;
    i = Math.max(0, Math.min(i, Math.max(0, this.traceCount - 1)));
    this.idxInput.value = i; this.idxRange.value = i;
    this.onBrowse(i);
  }

  // ---------- plot ----------
  seriesConfig() {
    const s = [{}];
    s.push({ label: 'trace', stroke: COLORS.latest, width: 1, points: { show: false } });
    for (let i = 0; i < this.overlayN; i++) s.push({ label: 'o' + i, stroke: COLORS.overlay(i, this.overlayN), width: 1, points: { show: false } });
    if (this.showEnv) {
      s.push({ label: 'min', stroke: COLORS.envLine, width: 1, points: { show: false } });
      s.push({ label: 'max', stroke: COLORS.envLine, width: 1, points: { show: false }, fill: COLORS.env, band: true });
    }
    if (this.showMean) s.push({ label: 'mean', stroke: COLORS.mean, width: 1.2, points: { show: false } });
    if (this.corr) s.push({ label: 'corr', stroke: COLORS.corr, width: 1.2, points: { show: false }, scale: 'corr' });
    return s;
  }

  buildPlot() {
    COLORS = themeColors();
    if (this.u) { this.u.destroy(); this.u = null; }
    const self = this;
    const w = this.plotEl.clientWidth || 800, ht = this.plotEl.clientHeight || 400;
    const opts = {
      width: w, height: ht,
      cursor: {
        drag: { x: true, y: false, uni: 20 },
        points: { show: false },
        sync: { key: 'cw' },
      },
      select: { show: true },
      legend: { show: false },
      scales: {
        x: { time: false },
        y: { auto: () => self.autoY },
        corr: { range: [-1, 1] },
      },
      axes: [
        axisStyle({ values: (u, vals) => vals.map((v) => self.timeAxis && self.sampleRate ? fmtTime(v / self.sampleRate) : v) }),
        axisStyle({ size: 60 }),
      ],
      series: this.seriesConfig(),
      bands: this.showEnv ? [{ series: [this.envIdx() + 1, this.envIdx()], fill: COLORS.env }] : [],
      hooks: {
        setCursor: [(u) => self.onCursorMove(u)],
        setScale: [(u, key) => { if (key === 'x') { const s = u.scales.x; const full = u.data[0] ? u.data[0].length - 1 : 0; self.userZoomed = !(s.min <= 0 && s.max >= full); } }],
        draw: [(u) => self.drawCursors(u)],
        ready: [(u) => {
          u.over.addEventListener('click', (e) => {
            if (u.select.width > 0) return; // was a drag selection
            const idx = u.cursor.idx;
            if (idx == null) return;
            if (e.shiftKey) self.cursors.b = idx; else self.cursors.a = idx;
            self.redraw(); self.updateFooter();
          });
          u.over.addEventListener('dblclick', () => { self.userZoomed = false; self.render(true); });
        }],
      },
    };
    this.u = new uPlot(opts, this.emptyData(), this.plotEl);
    this.u.root.style.position = 'absolute';
    this.render(true);
  }

  emptyData() {
    const n = this.seriesConfig().length;
    const d = [[0]];
    for (let i = 1; i < n; i++) d.push([null]);
    return d;
  }

  envIdx() { return 2 + this.overlayN; }

  rebuild() { this.buildPlot(); }

  resize() {
    if (!this.u) return;
    const w = this.plotEl.clientWidth, ht = this.plotEl.clientHeight;
    if (w > 0 && ht > 0) this.u.setSize({ width: w, height: ht });
  }

  // ---------- data ----------
  pushLive(samples, header) {
    if (this.overlayN > 0 && this.latest) {
      this.ring.push(this.latest.samples);
      if (this.ring.length > this.overlayN) this.ring.shift();
    }
    this.latest = { samples, header };
    if (this.paused) return;
    if (this.mode !== 'live') return;
    this.pendingLive = true;
    if (!this.rafScheduled) {
      this.rafScheduled = true;
      requestAnimationFrame(() => { this.rafScheduled = false; this.render(false); });
    }
  }

  setBrowse(samples, header) {
    this.browse = { samples, header };
    if (this.mode === 'browse') this.render(false);
  }

  setStats(stats) {
    this.stats = stats;
    if (this.showMean || this.showEnv) this.render(false);
  }

  setCorr(samples, offset) {
    this.corr = samples ? { samples, offset: offset || 0 } : null;
    this.rebuild();
  }

  current() { return this.mode === 'browse' ? this.browse : this.latest; }

  render(resetScales) {
    if (!this.u) return;
    const cur = this.current();
    const n = cur ? cur.samples.length : 0;
    const overlayEl = this.plotEl.querySelector('.overlay-msg');
    if (!n) {
      if (!overlayEl) this.plotEl.appendChild(h('div', { class: 'overlay-msg', html: '<svg viewBox="0 0 24 24"><path d="M2 12h4l3-8 4 16 3-8h6"/></svg><b>No trace yet</b><div style="font-size:12.5px">Connect a scope, then press Single or Run</div>' }));
      this.u.setData(this.emptyData());
      this.updateFooter();
      return;
    }
    if (overlayEl) overlayEl.remove();
    const x = this.xCache && this.xCache.length === n ? this.xCache : (this.xCache = Float64Array.from({ length: n }, (_, i) => i));
    const data = [x, cur.samples];
    for (let i = 0; i < this.overlayN; i++) {
      const r = this.ring[this.ring.length - 1 - i];
      data.push(r && r.length === n ? r : null);
    }
    if (this.showEnv) {
      const ok = this.stats && this.stats.min && this.stats.min.length === n;
      data.push(ok ? this.stats.min : null); data.push(ok ? this.stats.max : null);
    }
    if (this.showMean) data.push(this.stats && this.stats.mean && this.stats.mean.length === n ? this.stats.mean : null);
    if (this.corr) {
      // The correlation series only changes with setCorr or the trace length, so it is built once instead of on every frame.
      const cc = this.corrCache;
      if (!cc || cc.src !== this.corr || cc.n !== n) {
        const c = new Float32Array(n).fill(NaN);
        c.set(this.corr.samples.subarray(0, Math.max(0, Math.min(this.corr.samples.length, n - this.corr.offset))), this.corr.offset);
        this.corrCache = { src: this.corr, n, data: c };
      }
      data.push(this.corrCache.data);
    }
    // uPlot requires arrays; null series share one cached array of nulls
    if (!this.nullCache || this.nullCache.length !== n) this.nullCache = new Array(n).fill(null);
    for (let i = 2; i < data.length; i++) if (data[i] === null) data[i] = this.nullCache;
    const keepZoom = this.userZoomed && !resetScales;
    this.u.setData(data, !keepZoom);
    if (keepZoom) {
      const s = this.u.scales.x;
      this.u.setScale('x', { min: Math.max(0, s.min), max: Math.min(n - 1, s.max) });
    }
    this.updateFooter();
  }

  redraw() { if (this.u) this.u.redraw(false, true); }

  /** Zoom the sample axis by a factor (0.5 = in, 2 = out), centred on cursor A when it is in view, otherwise on the middle of the view. */
  zoom(factor) {
    const u = this.u;
    const n = u && u.data[0] ? u.data[0].length : 0;
    if (n < 2) return;
    const s = u.scales.x, lo = s.min ?? 0, hi = s.max ?? n - 1;
    const a = this.cursors.a;
    const centre = a != null && a >= lo && a <= hi ? a : (lo + hi) / 2;
    const half = Math.max(5, ((hi - lo) / 2) * factor); // never narrower than about 10 samples
    if (half * 2 >= n - 1) { this.userZoomed = false; this.render(true); return; }
    let min = centre - half, max = centre + half;
    if (min < 0) { max -= min; min = 0; }
    if (max > n - 1) { min -= max - (n - 1); max = n - 1; }
    u.setScale('x', { min: Math.max(0, min), max });
  }

  // ---------- cursors ----------
  drawCursors(u) {
    const ctx = u.ctx;
    const draw = (idx, color, label) => {
      if (idx == null || !u.data[0] || idx >= u.data[0].length) return;
      const xv = u.data[0][idx];
      if (xv < u.scales.x.min || xv > u.scales.x.max) return;
      const xp = u.valToPos(xv, 'x', true);
      ctx.save();
      ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(xp, u.bbox.top); ctx.lineTo(xp, u.bbox.top + u.bbox.height); ctx.stroke();
      ctx.fillStyle = color; ctx.font = `${12 * devicePixelRatio}px sans-serif`;
      ctx.fillText(label, xp + 4, u.bbox.top + 14 * devicePixelRatio);
      ctx.restore();
    };
    draw(this.cursors.a, COLORS.cursorA, 'A');
    draw(this.cursors.b, COLORS.cursorB, 'B');
  }

  onCursorMove(u) {
    const idx = u.cursor.idx;
    if (idx == null || !this.hoverEl) return;
    const cur = this.current();
    if (!cur || idx >= cur.samples.length) { this.hoverEl.textContent = ''; return; }
    const v = cur.samples[idx];
    let t = '';
    if (this.sampleRate) t = ' · t=' + fmtTime(idx / this.sampleRate);
    this.hoverEl.innerHTML = `hover <b>#${idx}</b> = <b>${v.toFixed(4)}</b>${t}`;
  }

  cursorInfo() {
    const cur = this.current();
    const { a, b } = this.cursors;
    if (a == null && b == null) return '';
    const val = (i) => cur && i != null && i < cur.samples.length ? cur.samples[i].toFixed(4) : '?';
    let s = '';
    if (a != null) s += `A <b>#${a}</b>=${val(a)} `;
    if (b != null) s += `B <b>#${b}</b>=${val(b)} `;
    if (a != null && b != null) {
      const d = b - a;
      s += `Δ <b>${d}</b> samples`;
      if (this.sampleRate) s += ` = ${fmtTime(d / this.sampleRate)} (${d !== 0 ? (this.sampleRate / Math.abs(d)).toFixed(1) + ' Hz' : '-'})`;
      if (cur) s += ` ΔV ${(cur.samples[b] - cur.samples[a]).toFixed(4)}`;
    }
    return s;
  }

  updateFooter() {
    const cur = this.current();
    const f = this.footerEl;
    f.innerHTML = '';
    if (!cur) { f.append(h('span', {}, 'no data')); return; }
    const s = cur.samples; let mn = Infinity, mx = -Infinity, sum = 0;
    for (let i = 0; i < s.length; i++) { const v = s[i]; if (v < mn) mn = v; if (v > mx) mx = v; sum += v; }
    const hd = cur.header || {};
    const idx = hd.index != null && hd.index >= 0 ? `#${hd.index}` : (hd.stored === false ? 'not stored' : '');
    f.append(
      h('span', {}, `${this.mode === 'browse' ? 'trace' : 'live'} <b>${idx}</b>`.replace('<b>', '').replace('</b>', '') ),
      h('span', {}, `${s.length} samples${this.sampleRate ? ' @ ' + (this.sampleRate / 1e6).toFixed(3) + ' MS/s' : ''}`),
      h('span', { html: `min <b>${mn.toFixed(4)}</b> max <b>${mx.toFixed(4)}</b> pk-pk <b>${(mx - mn).toFixed(4)}</b> mean <b>${(sum / s.length).toFixed(4)}</b>` }),
    );
    if (hd.textin) f.append(h('span', { html: `pt <b>${hd.textin}</b>` }));
    if (hd.textout) f.append(h('span', { html: `ct <b>${hd.textout}</b>` }));
    if (hd.key) f.append(h('span', { html: `key <b>${hd.key}</b>` }));
    const ci = this.cursorInfo();
    if (ci) f.append(h('span', { html: ci, style: `color:${COLORS.cursorA}` }));
    this.hoverEl = h('span', {});
    f.append(this.hoverEl);
  }

  exportPng() {
    if (!this.u) return;
    const c = this.u.ctx.canvas;
    const out = document.createElement('canvas'); out.width = c.width; out.height = c.height;
    const ctx = out.getContext('2d'); ctx.fillStyle = COLORS.bg || '#0d1014'; ctx.fillRect(0, 0, out.width, out.height); ctx.drawImage(c, 0, 0);
    const a = document.createElement('a'); a.download = 'trace.png'; a.href = out.toDataURL('image/png'); a.click();
    toast('PNG exported', 'ok');
  }

  zoomRange() {
    if (!this.u || !this.u.data[0]) return null;
    const s = this.u.scales.x;
    return [Math.max(0, Math.floor(s.min)), Math.ceil(s.max) + 1];
  }
}

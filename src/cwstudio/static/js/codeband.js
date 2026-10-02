// The Code band under the waveform: which firmware function (flame chart rows) and source line runs at each sample, aligned with the plot's x axis.
import { h } from './api.js';

const ROWS = 5;        // function rows (call depths) shown at once
const ROW_H = 14;
const LINE_H = 14;
const TOP = 4;

function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

let THEME = null;
const HUES = new Map();
/** Re-read the function colour tokens (after a theme change). */
export function refreshTheme() { THEME = { sat: cssVar('--cm-sat') || '45%', lit: cssVar('--cm-lit') || '40%' }; }

/** Stable colour per function name, in the current theme. */
export function funcColor(name, alpha = 1) {
  if (!THEME) refreshTheme();
  let hue = HUES.get(name);
  if (hue === undefined) {
    let x = 2166136261;
    for (let i = 0; i < (name || '').length; i++) { x ^= name.charCodeAt(i); x = Math.imul(x, 16777619); }
    hue = (x >>> 0) % 360;
    HUES.set(name, hue);
  }
  return `hsla(${hue}, ${THEME.sat}, ${THEME.lit}, ${alpha})`;
}

export class CodeBand {
  constructor(el, wave, ctx) {
    this.el = el; this.wave = wave; this.ctx = ctx;
    this.canvas = h('canvas');
    this.tip = h('div', { class: 'cb-tip' });
    this.msg = h('div', { class: 'cb-msg' });
    el.append(this.canvas, this.tip, this.msg);
    this.band = null; this.map = null; this.geo = null; this.selected = null;
    wave.addViewListener((g) => { this.geo = g; this.draw(); });
    new ResizeObserver(() => this.draw()).observe(el);
    this.canvas.addEventListener('mousemove', (e) => this.hover(e));
    this.canvas.addEventListener('mouseleave', () => { this.tip.style.display = 'none'; });
    this.canvas.addEventListener('click', (e) => this.click(e));
    this.canvas.addEventListener('wheel', (e) => { if (e.ctrlKey) return; e.preventDefault(); this.wave.zoom(e.deltaY < 0 ? 0.8 : 1.25); }, { passive: false });
    this.setVisible(wave.showCode);
    this.empty('No code map yet: build one in the Code tab.');
  }

  setVisible(on) { this.el.classList.toggle('on', !!on); if (on) requestAnimationFrame(() => this.draw()); }

  empty(text, action) {
    this.msg.innerHTML = '';
    this.msg.append(h('span', {}, text));
    if (action) this.msg.append(h('button', { class: 'btn sm', onclick: action.onclick }, action.label));
    this.msg.style.display = 'flex';
  }

  /** band: /api/codemap/band 'band'; mapping: its 'mapping'. */
  setData(band, mapping) {
    this.band = band;
    if (!band) { this.empty('No code map yet: build one in the Code tab.', { label: 'Open Code tab', onclick: () => this.ctx.showTab('code') }); this.draw(); return; }
    this.msg.style.display = 'none';
    const sp = band.spans, ln = band.lines;
    this.sp = { start: Float64Array.from(sp.start), end: Float64Array.from(sp.end), depth: Int32Array.from(sp.depth), func: Int32Array.from(sp.func) };
    this.ln = { start: Float64Array.from(ln.start), end: Float64Array.from(ln.end), func: Int32Array.from(ln.func), file: Int32Array.from(ln.file), line: Int32Array.from(ln.line), count: Int32Array.from(ln.count) };
    const ir = band.inline_runs || { start: [], end: [], inline: [], depth: [] };
    this.ir = { start: Float64Array.from(ir.start), end: Float64Array.from(ir.end), inline: Int32Array.from(ir.inline), depth: Int32Array.from(ir.depth) };
    this.files = {}; (band.files || []).forEach((f) => { this.files[f.i] = f.name; });
    this.setMapping(mapping);
  }

  setMapping(m) {
    if (m) this.map = { a: m.samples_per_cycle || m.spc * (m.scale || 1), b: -(m.adc_offset || 0) + (m.presamples || 0) + (m.shift || 0) };
    this.draw();
  }

  sample(c) { return c * this.map.a + this.map.b; }
  cycle(s) { return (s - this.map.b) / this.map.a; }
  fname(i) { const f = this.band && this.band.functions[i]; return f ? f.name : '?'; }
  iname(i) { const f = this.band && this.band.inlines && this.band.inlines[i]; return f ? f.name : '?'; }

  layout() {
    const g = this.geo || this.wave.geometry();
    if (!g || g.max == null) return null;
    const W = this.el.clientWidth, H = this.el.clientHeight;
    const toX = (s) => g.left + ((s - g.min) / (g.max - g.min)) * g.width;
    return { g, W, H, toX };
  }

  /** Call depths shown: the deepest functions visible, up to ROWS levels. */
  depthRange(L) {
    const { sp } = this;
    const c0 = this.cycle(L.g.min), c1 = this.cycle(L.g.max);
    let dmin = 1e9, dmax = -1;
    for (let i = 0; i < sp.start.length; i++) {
      if (sp.end[i] < c0 || sp.start[i] > c1) continue;
      dmin = Math.min(dmin, sp.depth[i]); dmax = Math.max(dmax, sp.depth[i]);
    }
    const ir = this.ir;
    for (let i = 0; i < ir.start.length; i++) {
      if (ir.end[i] < c0 || ir.start[i] > c1) continue;
      dmax = Math.max(dmax, ir.depth[i]);
    }
    if (dmax < 0) return [0, ROWS - 1];
    const d0 = Math.max(dmin, dmax - ROWS + 1);
    return [d0, d0 + ROWS - 1];
  }

  draw() {
    const L = this.layout();
    const cv = this.canvas, dpr = devicePixelRatio || 1;
    if (!this.el.classList.contains('on')) return;
    const W = this.el.clientWidth, H = this.el.clientHeight;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    const c = cv.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, W, H);
    if (!L || !this.band || !this.map) return;
    refreshTheme();
    const { g, toX } = L;
    const font = cssVar('--font') || 'sans-serif', mono = cssVar('--mono') || 'monospace';
    const muted = cssVar('--muted'), text = cssVar('--cm-text'), border = cssVar('--border');
    const x0 = g.left, x1 = g.left + g.width;
    // gutter labels
    c.fillStyle = muted; c.font = `500 10.5px ${font}`; c.textBaseline = 'middle'; c.textAlign = 'right';
    const [d0, d1] = this.depthRange(L);
    this.rows = [d0, d1];
    const lineY = TOP + ROWS * ROW_H + 4;
    c.fillText('functions', x0 - 8, TOP + ROW_H / 2);
    c.fillText('lines', x0 - 8, lineY + LINE_H / 2);
    c.save();
    c.beginPath(); c.rect(x0, 0, g.width, H); c.clip();
    // trigger window markers
    const tA = toX(this.sample(0)), tB = this.band.t1 != null ? toX(this.sample(this.band.t1)) : null;
    c.strokeStyle = muted; c.setLineDash([3, 3]); c.lineWidth = 1;
    for (const x of [tA, tB]) if (x != null && x >= x0 && x <= x1) { c.beginPath(); c.moveTo(Math.round(x) + 0.5, 0); c.lineTo(Math.round(x) + 0.5, H); c.stroke(); }
    c.setLineDash([]);
    // function spans, one row per call depth
    const sp = this.sp;
    c.textAlign = 'left'; c.font = `600 10.5px ${font}`;
    this.drawn = [];
    for (let i = 0; i < sp.start.length; i++) {
      const d = sp.depth[i];
      if (d < d0 || d > d1) continue;
      const sa = this.sample(sp.start[i]), sb = this.sample(sp.end[i]);
      if (sb < g.min || sa > g.max) continue;
      let xa = toX(sa), xb = toX(sb);
      xa = Math.max(xa, x0 - 2); xb = Math.min(xb, x1 + 2);
      const y = TOP + (d - d0) * ROW_H;
      const name = this.fname(sp.func[i]);
      const sel = this.selected && this.selected.kind === 'function' && this.selected.name === name;
      c.fillStyle = funcColor(name, sel ? 1 : 0.92);
      const w = Math.max(1, xb - xa);
      c.fillRect(xa, y, w, ROW_H - 2);
      if (sel) { c.strokeStyle = cssVar('--fg'); c.lineWidth = 1.5; c.strokeRect(xa + 0.75, y + 0.75, Math.max(1, w - 1.5), ROW_H - 3.5); }
      if (w > 24) {
        const label = c.measureText(name).width + 8 < w ? name : null;
        if (label) { c.fillStyle = text; c.fillText(label, Math.max(xa, x0) + 4, y + (ROW_H - 2) / 2 + 0.5); }
      }
      this.drawn.push({ i, xa, xb, y });
    }
    // functions the compiler inlined, one row below the code they were inlined into (dashed outline)
    const ir = this.ir;
    for (let i = 0; i < ir.start.length; i++) {
      const d = ir.depth[i];
      if (d < d0 || d > d1) continue;
      const sa = this.sample(ir.start[i]), sb = this.sample(ir.end[i]);
      if (sb < g.min || sa > g.max) continue;
      const xa = Math.max(toX(sa), x0 - 2), xb = Math.min(toX(sb), x1 + 2);
      const y = TOP + (d - d0) * ROW_H;
      const name = this.iname(ir.inline[i]);
      const sel = this.selected && this.selected.kind === 'function' && this.selected.name === name;
      const w = Math.max(1, xb - xa);
      c.fillStyle = funcColor(name, sel ? 0.95 : 0.6);
      c.fillRect(xa, y, w, ROW_H - 2);
      if (w > 3) { c.save(); c.setLineDash([2, 2]); c.strokeStyle = sel ? cssVar('--fg') : funcColor(name, 1); c.lineWidth = 1; c.strokeRect(xa + 0.5, y + 0.5, w - 1, ROW_H - 3); c.restore(); }
      if (w > 24 && c.measureText(name).width + 8 < w) { c.fillStyle = text; c.fillText(name, Math.max(xa, x0) + 4, y + (ROW_H - 2) / 2 + 0.5); }
      this.drawn.push({ i, xa, xb, y, inline: true });
    }
    // source line runs (only when zoomed in far enough to tell them apart)
    const ln = this.ln;
    const c0 = this.cycle(g.min), c1 = this.cycle(g.max);
    const pxPerCycle = g.width / Math.max(1e-9, c1 - c0);
    c.fillStyle = cssVar('--surface-3') || border;
    c.fillRect(x0, lineY, g.width, LINE_H - 2);
    this.lineMode = pxPerCycle > 0.15;
    if (this.lineMode) {
      let lo = 0, hi = ln.start.length;
      while (lo < hi) { const m = (lo + hi) >> 1; if (ln.end[m] < c0) lo = m + 1; else hi = m; }
      c.font = `500 10px ${mono}`;
      for (let i = lo; i < ln.start.length && ln.start[i] <= c1; i++) {
        const xa = Math.max(toX(this.sample(ln.start[i])), x0 - 2), xb = Math.min(toX(this.sample(ln.end[i])), x1 + 2);
        const name = this.fname(ln.func[i]);
        const sel = this.selected && this.selected.kind === 'line' && this.selected.file === ln.file[i] && this.selected.line === ln.line[i];
        c.fillStyle = funcColor(name, sel ? 1 : (ln.line[i] % 2 ? 0.55 : 0.8));
        const w = Math.max(1, xb - xa - 0.5);
        c.fillRect(xa, lineY, w, LINE_H - 2);
        if (sel) { c.strokeStyle = cssVar('--fg'); c.lineWidth = 1.5; c.strokeRect(xa + 0.75, lineY + 0.75, Math.max(1, w - 1.5), LINE_H - 3.5); }
        if (w > 22 && ln.line[i] > 0) { c.fillStyle = text; c.fillText(String(ln.line[i]), xa + 3, lineY + (LINE_H - 2) / 2 + 0.5); }
      }
    } else {
      c.fillStyle = muted; c.font = `500 10.5px ${font}`;
      c.fillText('zoom in to see source lines', x0 + 6, lineY + (LINE_H - 2) / 2 + 0.5);
    }
    // region and highlights, as on the plot
    const shade = (a, b, color) => { if (b < g.min || a > g.max) return; const xa = Math.max(toX(a), x0), xb = Math.min(toX(b), x1); c.fillStyle = color; c.fillRect(xa, 0, Math.max(1, xb - xa), H); };
    for (const hl of this.wave.highlights || []) shade(hl.a, hl.b, cssVar('--cm-hl'));
    if (this.wave.region) shade(this.wave.region[0], this.wave.region[1], cssVar('--cm-sel'));
    c.restore();
  }

  hit(e) {
    const r = this.canvas.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const L = this.layout();
    if (!L || !this.band || !this.map) return null;
    const lineY = TOP + ROWS * ROW_H + 4;
    const s = L.g.min + ((x - L.g.left) / L.g.width) * (L.g.max - L.g.min);
    if (y >= lineY && y < lineY + LINE_H && this.lineMode) {
      const c = this.cycle(s), ln = this.ln;
      let lo = 0, hi = ln.start.length;
      while (lo < hi) { const m = (lo + hi) >> 1; if (ln.end[m] <= c) lo = m + 1; else hi = m; }
      if (lo < ln.start.length && ln.start[lo] <= c) return { kind: 'line', i: lo, x, y, sample: s };
      return null;
    }
    for (let k = (this.drawn || []).length - 1; k >= 0; k--) {
      const d = this.drawn[k];
      if (x >= d.xa && x <= Math.max(d.xb, d.xa + 1) && y >= d.y && y < d.y + ROW_H) return { kind: d.inline ? 'inline' : 'span', i: d.i, x, y, sample: s };
    }
    return null;
  }

  hover(e) {
    const t = this.hit(e);
    if (!t) { this.tip.style.display = 'none'; return; }
    const fmt = (v) => Math.round(v).toLocaleString();
    let html;
    if (t.kind === 'span') {
      const i = t.i, f = this.band.functions[this.sp.func[i]] || {};
      const a = this.sp.start[i], b = this.sp.end[i];
      html = `<b>${esc(f.name || '?')}</b>${f.file ? ` <span class="mono">${esc(f.file)}:${f.line}</span>` : ''} · cycles ${fmt(a)} to ${fmt(b)} (${fmt(b - a)}) · samples ${fmt(this.sample(a))} to ${fmt(this.sample(b))}`;
    } else if (t.kind === 'inline') {
      const i = t.i, f = (this.band.inlines || [])[this.ir.inline[i]] || {};
      const a = this.ir.start[i], b = this.ir.end[i];
      html = `<b>${esc(f.name || '?')}</b> inlined${f.call_file ? ` from <span class="mono">${esc(f.call_file)}:${f.call_line}</span>` : ''} · cycles ${fmt(a)} to ${fmt(b)} (${fmt(b - a)}) · samples ${fmt(this.sample(a))} to ${fmt(this.sample(b))}`;
    } else {
      const i = t.i, ln = this.ln;
      const a = ln.start[i], b = ln.end[i];
      html = `<span class="mono"><b>${esc(this.files[ln.file[i]] || '?')}:${ln.line[i]}</b></span> in ${esc(this.fname(ln.func[i]))} · cycles ${fmt(a)} to ${fmt(b)} · samples ${fmt(this.sample(a))} to ${fmt(this.sample(b))}`;
    }
    html += ` · cycle ${fmt(this.cycle(t.sample))}`;
    this.tip.innerHTML = html;
    this.tip.style.display = 'block';
    const W = this.el.clientWidth, tw = Math.min(this.tip.offsetWidth, 520);
    this.tip.style.left = Math.max(4, Math.min(W - tw - 4, t.x + 12)) + 'px';
    this.tip.style.top = (t.y > 40 ? 2 : 50) + 'px';
  }

  click(e) {
    const t = this.hit(e);
    if (!t) return;
    if (t.kind === 'span') {
      const i = t.i;
      this.ctx.emit('codemap-pick', { kind: 'function', name: this.fname(this.sp.func[i]), samples: [this.sample(this.sp.start[i]), this.sample(this.sp.end[i])] });
    } else if (t.kind === 'inline') {
      const i = t.i;
      this.ctx.emit('codemap-pick', { kind: 'function', name: this.iname(this.ir.inline[i]), samples: [this.sample(this.ir.start[i]), this.sample(this.ir.end[i])] });
    } else {
      const i = t.i, ln = this.ln;
      this.ctx.emit('codemap-pick', { kind: 'line', file: ln.file[i], line: ln.line[i], samples: [this.sample(ln.start[i]), this.sample(ln.end[i])] });
    }
  }

  select(sel) { this.selected = sel; this.draw(); }
}

function esc(s) { return String(s).replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch])); }

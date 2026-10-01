// Small Markdown renderer (CommonMark core plus GFM tables, task lists, strikethrough and autolinks; $math$ is left as text) and an allowlist HTML/SVG sanitizer.

const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const unesc = (s) => s.replace(/\\([!-/:-@[-`{-~])/g, '$1');
const norm = (s) => s.trim().replace(/\s+/g, ' ').toLowerCase();
const indent = (l) => /^ */.exec(l)[0].length;

const FENCE = /^( {0,3})(`{3,}|~{3,})(.*)$/;
const ATX = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
const HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const QUOTE = /^ {0,3}> ?/;
const SETEXT = /^ {0,3}(=+|-+)[ \t]*$/;
const ITEM = /^( {0,3})([*+-]|\d{1,9}[.)])(?:([ \t]+)(.*))?$/;
const DELIM = /^ {0,3}\|?(?:[ \t]*:?-+:?[ \t]*\|)*[ \t]*:?-+:?[ \t]*\|?[ \t]*$/;
const DEF = /^ {0,3}\[((?:\\.|[^\]\\])+)\]:[ \t]*(<[^>\n]*>|\S+)(?:[ \t]+("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\((?:[^()\\]|\\.)*\)))?[ \t]*$/;
const HTML_RAW = /^ {0,3}<(script|pre|style|textarea)(?=[\s>]|$)/i;
const HTML_BLOCK = /^ {0,3}(?:<!--|<\/?(?:address|article|aside|blockquote|body|caption|center|col|colgroup|dd|details|dialog|div|dl|dt|fieldset|figcaption|figure|footer|form|h[1-6]|header|hr|iframe|legend|li|main|nav|ol|p|section|summary|table|tbody|td|tfoot|th|thead|tr|ul)(?=[\s/>]|$))/i;
const HTML_TAG = /^ {0,3}(?:<[a-z][\w-]*(?:\s+[a-z_:][\w.:-]*(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*\s*\/?>|<\/[a-z][\w-]*\s*>)[ \t]*$/i;
const INLINE = new RegExp([
  /(?<tick>`+)(?<code>[\s\S]*?[^`])\k<tick>(?!`)/, /(?<ticks>`+)/,
  /(?<math>\$\$(?:[^\\]|\\[\s\S])+?\$\$|\$(?:[^$\\]|\\[\s\S])+?\$(?!\d))/,
  /\\(?<esc>[!-/:-@[-`{-~]|\n)/,
  /<(?<auto>[a-z][a-z\d+.-]{1,31}:[^\s<>]*|[\w.+-]+@[\w-]+(?:\.[\w-]+)+)>/,
  /(?<html><\/?[a-z][\w-]*(?:\s+[a-z_:][\w.:-]*(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*\s*\/?>|<!--[\s\S]*?-->)/,
  /(?<img>!?)\[(?<text>(?:\\.|[^[\]\\]|\[(?:\\.|[^[\]\\])*\])*)\](?:\(\s*(?<dest><[^<>\n]*>|(?:[^\s()\\]|\\.|\((?:[^\s()\\]|\\.)*\))*)(?:\s+(?<title>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\((?:[^()\\]|\\.)*\)))?\s*\)|\[(?<ref>(?:\\.|[^[\]\\])*)\])?/,
  /(?<ent>&(?:#\d{1,7}|#x[\da-f]{1,6}|[a-z][a-z\d]{1,31});)/,
  /(?<url>(?<![\w/])(?:https?:\/\/|www\.)[^\s<]*[^\s<?!.,:;*_~)'"\]])/,
].map((r) => r.source).join('|'), 'gi');

let refs = {};

function inline(s, inLink) {
  const keep = [];
  const ph = (html) => `\u0001${keep.push(html) - 1}\u0002`;
  s = s.replace(INLINE, (m, ...a) => {
    const g = a[a.length - 1];
    if (g.code !== undefined) {
      let c = g.code.replace(/\n/g, ' ');
      if (/^ [\s\S]* $/.test(c) && c.trim()) c = c.slice(1, -1);
      return ph(`<code>${esc(c)}</code>`);
    }
    if (g.ent) return ph(m);
    if (g.ticks || g.math || (inLink && (g.auto || g.url))) return ph(esc(m));
    if (g.esc !== undefined) return ph(g.esc === '\n' ? '<br>\n' : esc(g.esc));
    if (g.auto) return ph(`<a href="${esc(/@/.test(g.auto) && !/:/.test(g.auto) ? 'mailto:' + g.auto : g.auto)}">${esc(g.auto)}</a>`);
    if (g.html) return ph(m);
    if (g.url) return ph(`<a href="${esc(/^w/i.test(m) ? 'http://' + m : m)}">${esc(m)}</a>`);
    let href = g.dest, title = g.title && g.title.slice(1, -1);
    if (href === undefined) {
      const r = refs[norm(g.ref || g.text)];
      if (!r) return ph(esc(g.img) + '[' + inline(g.text, inLink) + ']') + (g.ref ? ph('[' + inline(g.ref, inLink) + ']') : g.ref === '' ? '[]' : '');
      ({ href, title } = r);
    }
    href = esc(unesc(href.replace(/^<([\s\S]*)>$/, '$1')).replace(/ /g, '%20'));
    const t = title ? ` title="${esc(unesc(title))}"` : '';
    if (g.img) return ph(`<img src="${href}" alt="${inline(g.text, true).replace(/<[^>]*>/g, '')}"${t}>`);
    return ph(`<a href="${href}"${t}>${inline(g.text, true)}</a>`);
  });
  return esc(s)
    .replace(/ {2,}\n/g, '<br>\n')
    .replace(/\*\*\*(?=[^\s*])([\s\S]*?[^\s*])\*\*\*/g, '<em><strong>$1</strong></em>')
    .replace(/\*\*(?=[^\s*])([\s\S]*?\S)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^\w])__(?=[^\s_])([\s\S]*?\S)__(?!\w)/g, '$1<strong>$2</strong>')
    .replace(/\*(?=[^\s*])([\s\S]*?[^\s*])\*/g, '<em>$1</em>')
    .replace(/(^|[^\w])_(?=[^\s_])([\s\S]*?[^\s_])_(?!\w)/g, '$1<em>$2</em>')
    .replace(/~~(?=\S)([\s\S]*?\S)~~/g, '<del>$1</del>')
    .replace(/\u0001(\d+)\u0002/g, (_, i) => keep[i]);
}

const cells = (r) => r.trim().replace(/^\|/, '').replace(/(?<!\\)\|$/, '').split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|'));
const isTable = (lines, i) => i + 1 < lines.length && lines[i].includes('|') && lines[i + 1].includes('|') && DELIM.test(lines[i + 1]) && cells(lines[i]).length === cells(lines[i + 1]).length;
// Lines that end a paragraph (and lazy continuation in quotes and list items) without a blank line.
function interrupts(lines, i) {
  const l = lines[i], m = ITEM.exec(l);
  return FENCE.test(l) || ATX.test(l) || HR.test(l) || QUOTE.test(l) || HTML_RAW.test(l) || HTML_BLOCK.test(l) || isTable(lines, i) || !!(m && m[4] && m[4].trim() && (!/\d/.test(m[2]) || /^0*1\D/.test(m[2])));
}

function list(lines, i) {
  const m0 = ITEM.exec(lines[i]), ord = /\d/.test(m0[2]), type = m0[2].slice(-1), items = [];
  let cur = null, loose = false;
  for (; i < lines.length; i++) {
    const l = lines[i], m = ITEM.exec(l), last = cur && cur.lines[cur.lines.length - 1];
    if (cur && (!l.trim() || indent(l) >= cur.w)) { cur.lines.push(l.slice(cur.w)); continue; }
    if (m && /\d/.test(m[2]) === ord && m[2].slice(-1) === type && !HR.test(l)) {
      if (cur && !last.trim()) loose = true;
      const pad = m[3] ? m[3].length : 1;
      cur = { w: m[1].length + m[2].length + (pad > 4 ? 1 : pad), lines: [(pad > 4 ? ' '.repeat(pad - 1) : '') + (m[4] || '')] };
      items.push(cur);
      continue;
    }
    if (cur && !m && last.trim() && !interrupts(lines, i)) { cur.lines.push(l); continue; }
    break;
  }
  while (cur.lines.length > 1 && !cur.lines[cur.lines.length - 1].trim()) { cur.lines.pop(); i--; }
  const bodies = items.map((it) => {
    while (it.lines.length > 1 && !it.lines[it.lines.length - 1].trim()) it.lines.pop();
    const t = /^\[([ xX])\](?:[ \t]+|$)/.exec(it.lines[0]), st = { gap: false };
    if (t) it.lines[0] = it.lines[0].slice(t[0].length);
    const body = blocks(it.lines, st);
    if (st.gap) loose = true;
    return t ? body.replace(/^\u0003?/, (x) => `${x}<input type="checkbox" disabled${t[1] === ' ' ? '' : ' checked'}> `) : body;
  });
  const tag = ord ? 'ol' : 'ul', start = ord && parseInt(m0[2], 10) !== 1 ? ` start="${parseInt(m0[2], 10)}"` : '';
  const html = bodies.map((b) => `<li>${b.replace(/\u0003([\s\S]*?)\u0004/g, loose ? '<p>$1</p>\n' : '$1').replace(/\n$/, '')}</li>\n`).join('');
  return { html: `<${tag}${start}>\n${html}</${tag}>\n`, i };
}

// Renders block structure; `st` is set inside list items, whose paragraphs are emitted as markers so the list can decide tight or loose.
function blocks(lines, st) {
  const n = lines.length, para = (t) => (st ? `\u0003${t}\u0004` : `<p>${t}</p>\n`);
  let out = '', i = 0, blank = false, m;
  while (i < n) {
    const l = lines[i];
    if (!l.trim()) { i++; blank = true; continue; }
    if (blank && out && st) st.gap = true;
    blank = false;
    if (indent(l) >= 4) {
      const s = i;
      while (i < n && (indent(lines[i]) >= 4 || !lines[i].trim())) i++;
      while (!lines[i - 1].trim()) i--;
      out += `<pre><code>${esc(lines.slice(s, i).map((x) => x.slice(4)).join('\n'))}\n</code></pre>\n`;
    } else if ((m = FENCE.exec(l)) && !(m[2][0] === '`' && m[3].includes('`'))) {
      const lang = unesc(m[3].trim().split(/\s+/)[0]), body = [], cut = new RegExp(`^ {0,${m[1].length}}`);
      const close = new RegExp(`^ {0,3}${m[2][0]}{${m[2].length},}[ \\t]*$`);
      // Code keeps its tabs: at the top level the lines before tab expansion are used (tabs only matter for block structure).
      const text = lines === topLines ? rawLines : lines;
      for (i++; i < n && !close.test(lines[i]); i++) body.push(text[i].replace(cut, ''));
      i++;
      out += `<pre><code${lang ? ` class="language-${esc(lang)}"` : ''}>${esc(body.join('\n'))}${body.length ? '\n' : ''}</code></pre>\n`;
    } else if (/^ {0,3}\$\$/.test(l) && !l.trim().slice(2).includes('$$')) {
      const s = i;
      for (i++; i < n && !lines[i].includes('$$'); i++);
      out += para(esc(lines.slice(s, ++i).join('\n').trim()));
    } else if ((m = ATX.exec(l))) {
      out += `<h${m[1].length}>${inline(m[2] || '')}</h${m[1].length}>\n`; i++;
    } else if (HR.test(l)) {
      out += '<hr>\n'; i++;
    } else if (QUOTE.test(l)) {
      const sub = [];
      for (; i < n && (QUOTE.test(lines[i]) || (lines[i].trim() && sub[sub.length - 1].trim() && !interrupts(lines, i))); i++) sub.push(lines[i].replace(QUOTE, ''));
      out += `<blockquote>\n${blocks(sub)}</blockquote>\n`;
    } else if (HTML_RAW.test(l) || HTML_BLOCK.test(l) || HTML_TAG.test(l)) {
      const raw = HTML_RAW.exec(l), end = raw ? new RegExp(`</${raw[1]}>`, 'i') : /^ {0,3}<!--/.test(l) ? /-->/ : null, s = i;
      if (end) { while (i < n && !end.test(lines[i])) i++; i++; } else while (i < n && lines[i].trim()) i++;
      out += lines.slice(s, i).join('\n') + '\n';
    } else if (isTable(lines, i)) {
      const al = cells(lines[i + 1]).map((c) => (/^:-+:$/.test(c) ? 'center' : /^:/.test(c) ? 'left' : /:$/.test(c) ? 'right' : ''));
      const row = (cs, tag) => '<tr>' + al.map((a, k) => `<${tag}${a ? ` align="${a}"` : ''}>${inline(cs[k] || '')}</${tag}>`).join('') + '</tr>\n';
      let body = '';
      out += `<table>\n<thead>\n${row(cells(l), 'th')}</thead>\n`;
      for (i += 2; i < n && lines[i].trim() && !interrupts(lines, i); i++) body += row(cells(lines[i]), 'td');
      out += (body ? `<tbody>\n${body}</tbody>\n` : '') + '</table>\n';
    } else if (ITEM.test(l)) {
      ({ html: m, i } = list(lines, i));
      out += m;
    } else if (DEF.test(l)) {
      i++;
    } else {
      const p = [l];
      let h = 0;
      for (i++; i < n && lines[i].trim(); i++) {
        if ((m = SETEXT.exec(lines[i]))) { h = m[1][0] === '=' ? 1 : 2; i++; break; }
        if (interrupts(lines, i)) break;
        p.push(lines[i]);
      }
      const text = inline(p.map((x) => x.replace(/^[ \t]+/, '')).join('\n').replace(/[ \t]+$/, ''));
      out += h ? `<h${h}>${text}</h${h}>\n` : para(text);
    }
  }
  return out;
}

let topLines = null, rawLines = null; // the document's lines after and before tab expansion

// Markdown to HTML. The result is NOT safe to insert as is (raw HTML passes through); use renderMarkdown for that.
export function markdownToHtml(src) {
  rawLines = String(src || '').replace(/\r\n?/g, '\n').replace(/[\u0000-\u0004]/g, '').split('\n');
  const lines = topLines = rawLines.map((l) => l.replace(/^[ \t]+/, (w) => w.replace(/ {0,3}\t/g, '    ')));
  refs = {};
  for (const l of lines) {
    const m = DEF.exec(l);
    if (m && !refs[norm(m[1])]) refs[norm(m[1])] = { href: m[2], title: m[3] && m[3].slice(1, -1) };
  }
  return blocks(lines);
}

export const renderMarkdown = (src) => sanitize(markdownToHtml(src));

// ---------------- sanitizer ----------------
const set = (s) => new Set(s.split(' '));
const HTML_NS = 'http://www.w3.org/1999/xhtml', SVG_NS = 'http://www.w3.org/2000/svg';
const HTML_TAGS = set('a abbr address article aside b bdi bdo blockquote br caption center cite code col colgroup dd del details dfn div dl dt em figcaption figure font footer h1 h2 h3 h4 h5 h6 header hr i img ins kbd li main mark nav ol p pre q rp rt ruby s samp section small span strike strong sub summary sup table tbody td tfoot th thead time tr tt u ul var wbr');
const HTML_ATTRS = set('href src alt title width height align valign class id name style colspan rowspan start type reversed open dir lang border cellpadding cellspacing color face size bgcolor datetime cite abbr scope span checked disabled');
// Enough for matplotlib's SVG output. <style> is left out on purpose: CSS in an inline SVG applies to the whole page. SVG attributes are free (no on*, links and url() only to #ids).
const SVG_TAGS = set('svg g path defs use clipPath symbol rect circle ellipse line polyline polygon text tspan title desc marker linearGradient radialGradient stop pattern mask image');
// Removed with their content; any other unknown HTML element is unwrapped (its children are kept).
const DROP = set('script style iframe object embed form input button select textarea option noscript template math svg frame frameset applet base link meta title head audio video source canvas noembed noframes xmp plaintext');
const DATA_IMG = /^data:image\/(png|jpeg|gif|webp)[;,]/i;
const safeUrl = (v) => { const u = v.replace(/[\x00-\x20\x7f-\xa0]/g, ''), m = /^([a-z][a-z\d+.-]*):/i.exec(u); return !m || /^(https?|mailto|tel|ftp)$/i.test(m[1]) || DATA_IMG.test(u); };
// Inline styles may format content but not load anything or escape their container (position: fixed overlays could cover and block the whole UI).
const safeCss = (v) => !/[\\]|@import|expression\s*\(|behavior|binding|javascript:|image-set|position\s*:\s*(fixed|sticky)/i.test(v) && !/url\(\s*(?!['"]?#)/i.test(v);

function keepAttr(el, name, v, svg) {
  if (svg ? /^on/i.test(name) || !/^[\w:-]+$/.test(name) : !HTML_ATTRS.has(name)) return false;
  if (!svg && name === 'src' && el.localName === 'img' && /^data:image\/svg\+xml[;,]/i.test(v)) return true; // an SVG shown through <img> cannot run scripts
  if (name === 'href' || name === 'src' || name === 'xlink:href' || name === 'cite') return svg ? v.startsWith('#') || (el.localName === 'image' && DATA_IMG.test(v)) : safeUrl(v);
  if (name === 'style' || /url\(/i.test(v)) return safeCss(v);
  if (name === 'type' && el.localName !== 'ol' && el.localName !== 'li' && el.localName !== 'input') return false;
  return svg || !((name === 'id' || name === 'name') && v in document);
}

function clean(node, svg) {
  for (const el of [...node.childNodes]) {
    if (el.nodeType === 3) continue;
    if (el.nodeType !== 1) { el.remove(); continue; }
    const name = el.localName, ns = el.namespaceURI;
    const ok = svg ? ns === SVG_NS && SVG_TAGS.has(name) : ns === HTML_NS && (HTML_TAGS.has(name) || (name === 'input' && el.getAttribute('type') === 'checkbox'));
    if (!ok) {
      if (svg || ns !== HTML_NS || DROP.has(name)) el.remove(); else { clean(el, svg); el.replaceWith(...el.childNodes); }
      continue;
    }
    for (const a of [...el.attributes]) if (!keepAttr(el, svg ? a.name : a.name.toLowerCase(), a.value, svg)) el.removeAttributeNode(a);
    if (name === 'input') el.setAttribute('disabled', '');
    if (name === 'a' && !svg) {
      el.setAttribute('rel', 'noopener noreferrer');
      if (/^([a-z][a-z\d+.-]*:|\/\/)/i.test(el.getAttribute('href') || '')) el.setAttribute('target', '_blank');
    }
    clean(el, svg);
  }
}

// Allowlist sanitizer for untrusted HTML (or, with {svg: true}, an SVG image). Parses into an inert <template>, so nothing loads or runs.
export function sanitize(html, { svg = false } = {}) {
  const t = document.createElement('template');
  t.innerHTML = String(html || '');
  clean(t.content, svg);
  return t.innerHTML;
}

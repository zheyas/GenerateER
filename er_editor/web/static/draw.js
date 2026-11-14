// Рисование диаграммы в SVG (порт draw_* из render.py). Все функции возвращают строки SVG в мировых координатах.
import { R } from './geom.js';
import { KIND_DATA, KIND_INFO, PALETTE, STYLE } from './model.js';
import { ATTR_SIZE, FAMILY, LABEL_SIZE, NN_TEXT, TITLE_SIZE, TYPE_SIZE, dataColumns } from './scene.js';

export const C = {
  ink: '#1f2933', muted: '#52606d', faint: '#9aa5b1', grid: '#d9dee4', dot: '#cbd2da', accent: '#2f6fed',
  rowSel: '#dbe8ff', brick: '#9a3412', white: '#ffffff', legendFill: '#f8fafc', orange: '#e67e22',
  flagError: '#cb3837', flagWarn: '#e08e18', errorTint: '#fde0e0', warnTint: '#fdefd6',
};
const CROW_LEN = 16, CROW_SPREAD = 9, BAR_OFFSET = 24, BAR_HALF = 10, ARROW_LEN = 15, ARROW_HALF = 6;

export const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const f = (v) => Math.round(v * 100) / 100;

export function text(x, y, s, size, o = {}) {
  if (!s) return '';
  const fill = o.fill || C.ink;
  return `<text x="${f(x)}" y="${f(y)}" font-size="${size}" fill="${fill}"${o.bold ? ' font-weight="700"' : ''}${o.italic ? ' font-style="italic"' : ''}` +
    ` text-anchor="${o.anchor || 'start'}" dominant-baseline="central">${esc(s)}</text>`;
}
export const line = (a, b, col = C.ink, w = 1.5) =>
  `<line x1="${f(a[0])}" y1="${f(a[1])}" x2="${f(b[0])}" y2="${f(b[1])}" stroke="${col}" stroke-width="${w}"/>`;
export const rect = (r, fill = 'none', stroke = 'none', w = 1.5, rx = 0) =>
  `<rect x="${f(r.x)}" y="${f(r.y)}" width="${f(r.w)}" height="${f(r.h)}" fill="${fill}" stroke="${stroke}" stroke-width="${w}"${rx ? ` rx="${rx}"` : ''}/>`;
const poly = (pts, col, w, dashed) =>
  `<polyline points="${pts.map((p) => `${f(p[0])},${f(p[1])}`).join(' ')}" fill="none" stroke="${col}" stroke-width="${w}" stroke-linejoin="round" stroke-linecap="butt"${dashed ? ' stroke-dasharray="7 5"' : ''}/>`;

export function crow(p, d, col = C.ink) {
  const cx = p[0] + d[0] * CROW_LEN, cy = p[1] + d[1] * CROW_LEN, nx = -d[1], ny = d[0];
  return line([cx, cy], [p[0] + nx * CROW_SPREAD, p[1] + ny * CROW_SPREAD], col) + line([cx, cy], p, col) +
    line([cx, cy], [p[0] - nx * CROW_SPREAD, p[1] - ny * CROW_SPREAD], col);
}
export function bar(p, d, col = C.ink) {
  const cx = p[0] + d[0] * BAR_OFFSET, cy = p[1] + d[1] * BAR_OFFSET, nx = -d[1], ny = d[0];
  return line([cx - nx * BAR_HALF, cy - ny * BAR_HALF], [cx + nx * BAR_HALF, cy + ny * BAR_HALF], col, 1.8);
}
// Наконечник стрелки: остриё в tip, основание снаружи (d — единичное направление от узла).
export function arrow(tip, d, col = C.ink) {
  const bx = tip[0] + d[0] * ARROW_LEN, by = tip[1] + d[1] * ARROW_LEN, nx = -d[1], ny = d[0];
  const pts = [tip, [bx + nx * ARROW_HALF, by + ny * ARROW_HALF], [bx - nx * ARROW_HALF, by - ny * ARROW_HALF]];
  return `<polygon points="${pts.map((p) => `${f(p[0])},${f(p[1])}`).join(' ')}" fill="${col}"/>`;
}
function endMark(p, d, dist, s) {
  const nx = -d[1], ny = d[0];
  return text(p[0] + d[0] * dist + nx * 10, p[1] + d[1] * dist + ny * 10, s, 12, { fill: C.muted, bold: true, anchor: 'middle' });
}

export function drawLink(lg, kind, selected, arrows) {
  const l = lg.link, col = selected ? C.accent : C.ink;
  let out = poly(lg.halves[0], col, 1.5, !l.must_a) + poly(lg.halves[1], col, 1.5, !l.must_b);
  if (arrows && kind === KIND_DATA) {
    out += arrow(lg.pb, lg.db, col) + endMark(lg.pa, lg.da, 16, l.card_a === 'M' ? 'N' : '1') +
      endMark(lg.pb, lg.db, ARROW_LEN + 8, l.card_b === 'M' ? 'N' : '1');
  } else {
    if (l.card_a === 'M') out += crow(lg.pa, lg.da, col);
    if (l.card_b === 'M') out += crow(lg.pb, lg.db, col);
  }
  if (kind === KIND_INFO) {
    if (l.bar_a) out += bar(lg.pa, lg.da, col);
    if (l.bar_b) out += bar(lg.pb, lg.db, col);
  }
  for (const lb of lg.labels) out += text(lb.rect.x, lb.rect.y + lb.rect.h / 2, lb.text, LABEL_SIZE, { fill: selected ? C.accent : C.muted, italic: true });
  return out;
}

export function drawNode(d, n, geo, selected, selRow, flag) {
  const r = geo.rect, [head, body] = PALETTE[n.color % PALETTE.length];
  const flagCol = flag ? (flag.level === 'error' ? C.flagError : C.flagWarn) : null;
  const outline = selected ? C.accent : (flagCol || C.ink);
  let out = '';
  if (d.kind === KIND_INFO) {
    out += rect(r, body, outline, selected ? 2.4 : 1.6, 14);
    out += text(R.cx(r), r.y + 20, n.title, TITLE_SIZE.info, { bold: true, anchor: 'middle' });
    n.rows.forEach((row, i) => {
      const rr = geo.rows[i];
      if (selected && selRow === i) out += rect({ x: r.x + 6, y: rr.y, w: r.w - 12, h: rr.h }, C.rowSel, 'none', 0, 4);
      out += text(r.x + 18, rr.y + rr.h / 2, row.mark, ATTR_SIZE.info, { bold: row.mark === '#', anchor: 'middle' });
      out += text(r.x + 32, rr.y + rr.h / 2, row.name, ATTR_SIZE.info);
    });
    return out;
  }
  const s = STYLE.data;
  out += rect(r, C.white) + rect({ x: r.x, y: r.y, w: r.w, h: s.head }, head);
  out += line([r.x + 52, r.y + s.head], [r.x + 52, r.y + r.h], C.grid, 1);
  n.rows.forEach((row, i) => {
    const rr = geo.rows[i], yc = rr.y + rr.h / 2, sel = selected && selRow === i;
    if (sel) out += rect(rr, C.rowSel);
    else if (flag && flag.rows[i]) out += rect(rr, flag.rows[i] === 'error' ? C.errorTint : C.warnTint);
    if (i && !sel) out += line([r.x, rr.y], [r.x + r.w, rr.y], C.grid, 1);
    const isPk = row.key.includes('PK');
    out += text(r.x + 26, yc, row.key, 10, { fill: isPk ? C.brick : C.muted, bold: true, anchor: 'middle' });
    out += text(r.x + 60, yc, row.name, ATTR_SIZE.data, { bold: isPk });
    out += text(geo.typeX, yc, row.type, TYPE_SIZE, { fill: C.muted });
    out += text(r.x + r.w - 10, yc, row.notnull ? NN_TEXT : 'NULL', 11, { fill: row.notnull ? C.ink : C.faint, anchor: 'end' });
  });
  out += rect(r, 'none', outline, selected || flagCol ? 2.4 : 1.4);
  out += text(R.cx(r), r.y + s.head / 2, n.title, TITLE_SIZE.data + 1, { bold: true, anchor: 'middle' });
  if (flagCol) {
    out += `<circle cx="${f(r.x + r.w - 16)}" cy="${f(r.y + s.head / 2)}" r="9" fill="${flagCol}"/>` +
      text(r.x + r.w - 16, r.y + s.head / 2, '!', 14, { fill: C.white, bold: true, anchor: 'middle' });
  }
  return out;
}

export function drawLegend(d, geo, selected, selItem) {
  const lg = d.legend, r = geo.rect;
  let out = rect(r, C.legendFill, selected ? C.accent : C.grid, selected ? 2 : 1);
  out += text(r.x + 12, r.y + 18, lg.title, 13, { bold: true });
  lg.items.forEach((it, i) => {
    const rr = geo.items[i], yc = rr.y + rr.h / 2;
    if (selected && selItem === i) out += rect({ x: r.x + 3, y: rr.y + 1, w: r.w - 6, h: rr.h - 2 }, C.rowSel, 'none', 0, 3);
    const x0 = r.x + 12, x1 = r.x + 62, k = it.kind;
    if (k.startsWith('mark_')) out += text((x0 + x1) / 2 - 12, yc, k.slice(5), 13, { bold: k === 'mark_#', anchor: 'middle' });
    else if (['solid', 'one', 'crow', 'bar', 'arrow'].includes(k)) {
      out += line([x0, yc], [x1, yc]);
      if (k === 'arrow') out += arrow([x1, yc], [-1, 0]);
      else if (k === 'crow') out += crow([x0, yc], [1, 0]);
      else if (k === 'bar') out += bar([x0 - 8, yc], [1, 0]);
    } else if (k === 'dashed') out += poly([[x0, yc], [x1, yc]], C.ink, 1.5, true);
    out += text(r.x + (k === 'text' ? 12 : 72), yc, it.text, 12);
  });
  return out;
}

// Содержимое камеры: связи, узлы, надписи, легенда, ручки выделенного объекта. z — масштаб (для ручек постоянного размера).
export function drawDiagram(sc, sel, flags, z) {
  const d = sc.d;
  let out = '';
  for (const [lid, lg] of Object.entries(sc.links)) out += drawLink(lg, d.kind, sel.kind === 'link' && sel.id === lid, d.arrows);
  for (const n of d.nodes) out += drawNode(d, n, sc.nodes[n.id], sel.kind === 'node' && sel.id === n.id, sel.row, flags[n.id]);
  for (const note of d.notes) {
    const r = sc.notes[note.id], on = sel.kind === 'note' && sel.id === note.id;
    out += text(r.x, r.y + r.h / 2, note.text, note.size, { fill: on ? C.accent : C.ink });
    if (on) out += rect(R.inflated(r, 3), 'none', C.accent, 1);
  }
  if (sc.legend) out += drawLegend(d, sc.legend, sel.kind === 'legend', sel.row);
  const hs = 1 / z;
  const circle = (p) => `<circle cx="${f(p[0])}" cy="${f(p[1])}" r="${6 * hs}" fill="#fff" stroke="${C.accent}" stroke-width="${2 * hs}"/>`;
  const square = (p) => `<rect x="${f(p[0] - 4 * hs)}" y="${f(p[1] - 4 * hs)}" width="${8 * hs}" height="${8 * hs}" fill="#fff" stroke="${C.accent}" stroke-width="${2 * hs}"/>`;
  if (sel.kind === 'link' && sc.links[sel.id]) {
    const lg = sc.links[sel.id];
    out += circle(lg.pa) + circle(lg.pb) + (lg.midPt ? square(lg.midPt) : '');
  } else if (sel.kind === 'node' && sc.nodes[sel.id]) {
    const r = sc.nodes[sel.id].rect;
    out += square([r.x + r.w, R.cy(r)]);
  } else if (sel.kind === 'legend' && sc.legend) {
    out += square([sc.legend.rect.x + sc.legend.rect.w, R.cy(sc.legend.rect)]);
  }
  return out;
}

export { FAMILY, dataColumns };

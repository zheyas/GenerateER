// Сцена: размеры, маршруты и прямоугольники всей диаграммы в мировых координатах (порт Scene из render.py).
import { R, anchorPoint, cleanPoints, polyDist, route, splitHalf } from './geom.js';
import { KIND_DATA, KIND_INFO, STYLE, nodeHeight } from './model.js';

export const FAMILY = 'Arial, "Helvetica Neue", Helvetica, "DejaVu Sans", "Liberation Sans", sans-serif';
export const TITLE_SIZE = { info: 15, data: 14 };
export const ATTR_SIZE = { info: 13, data: 13 };
export const TYPE_SIZE = 12;
export const NN_TEXT = 'NOT NULL';
export const LABEL_SIZE = 12;
export const LEGEND_ROW = 22, LEGEND_HEAD = 34;

const ctx = document.createElement('canvas').getContext('2d');
const cache = new Map();

// Ширина и высота текста в мировых единицах.
export function measure(text, size, bold = false, italic = false) {
  const k = `${size}|${bold}|${italic}|${text}`;
  let v = cache.get(k);
  if (!v) {
    ctx.font = `${italic ? 'italic ' : ''}${bold ? 'bold ' : ''}${size}px ${FAMILY}`;
    v = [Math.round(ctx.measureText(text).width), Math.round(size * 1.2)];
    if (cache.size > 20000) cache.clear();
    cache.set(k, v);
  }
  return v;
}

// Раскладка колонок даталогической таблицы: смещения колонок имени и типа, ширины колонки типа и «NOT NULL».
export function dataColumns(node) {
  let nameW = 40, typeW = 30;
  if (node.rows.length) {
    nameW = Math.max(...node.rows.map((r) => measure(r.name, ATTR_SIZE.data, r.key.includes('PK'))[0]));
    typeW = Math.max(...node.rows.map((r) => measure(r.type, TYPE_SIZE)[0]));
  }
  return { nameX: 60, typeX: 60 + nameW + 16, typeW, nnW: measure(NN_TEXT, 11)[0] };
}

export function neededWidth(kind, n) {
  const titleW = measure(n.title, TITLE_SIZE[kind], true)[0] + 50;
  let body;
  if (kind === KIND_INFO) {
    body = (n.rows.length ? Math.max(...n.rows.map((r) => measure(r.name, ATTR_SIZE.info)[0])) : 0) + 32 + 18;
  } else {
    const c = dataColumns(n);
    body = c.typeX + c.typeW + 16 + c.nnW + 10;
  }
  return Math.max(titleW, body);
}

// Подгоняет ширину узлов под текст (округляя вверх до 10); growOnly — только расширять.
export function fitWidths(d, growOnly = false) {
  const s = STYLE[d.kind];
  for (const n of d.nodes) {
    const w = Math.ceil(Math.max(s.min_w, neededWidth(d.kind, n)) / 10) * 10;
    n.w = growOnly ? Math.max(n.w, w) : w;
  }
}

export const nodeRect = (kind, n) => ({ x: n.x, y: n.y, w: n.w, h: nodeHeight(kind, n.rows.length) });

function endLabel(p, dir, text, off) {
  const [w, h] = measure(text, LABEL_SIZE, false, true);
  let r;
  if (dir[0] === 0) r = { x: p[0] + 8, y: p[1] + dir[1] * 36 - h / 2, w, h };
  else r = { x: dir[0] > 0 ? p[0] + 30 : p[0] - 30 - w, y: p[1] - h - 3, w, h };
  r.x += off[0];
  r.y += off[1];
  return r;
}

function linkGeo(d, l, nodes) {
  const ra = nodes[l.a].rect, rb = nodes[l.b].rect;
  const [pa, da] = anchorPoint(ra, l.sa, l.ta);
  const [pb, db] = anchorPoint(rb, l.sb, l.tb);
  const [raw, axis] = route(pa, da, pb, db, l.mid);
  const pts = cleanPoints(raw);
  const halves = splitHalf(pts);
  const lg = { link: l, pts, halves, pa, da, pb, db, axis, labels: [], midPt: null };
  if (axis && pts.length >= 4) lg.midPt = [(pts[1][0] + pts[2][0]) / 2, (pts[1][1] + pts[2][1]) / 2];
  if (d.kind === KIND_INFO) {
    for (const [which, p, dir, text, off] of [['a', pa, da, l.name_a, l.off_a], ['b', pb, db, l.name_b, l.off_b]]) {
      if (text) lg.labels.push({ which, text, rect: endLabel(p, dir, text, off) });
    }
  } else if (l.label) {
    const [w, h] = measure(l.label, LABEL_SIZE, false, true);
    let best = null, bestLen = -1;
    for (let i = 0; i < pts.length - 1; i++) {
      const len = Math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]);
      if (len > bestLen) { bestLen = len; best = [pts[i], pts[i + 1]]; }
    }
    const [[x1, y1], [x2, y2]] = best;
    const rect = Math.abs(y1 - y2) < 0.01 ? { x: (x1 + x2) / 2 - w / 2, y: y1 - h - 3, w, h }
      : { x: x1 + 6, y: (y1 + y2) / 2 - h / 2, w, h };
    rect.x += l.off_c[0];
    rect.y += l.off_c[1];
    lg.labels.push({ which: 'c', text: l.label, rect });
  }
  return lg;
}

function legendGeo(d) {
  const lg = d.legend;
  let need = measure(lg.title, 13, true)[0] + 24;
  for (const it of lg.items) need = Math.max(need, (it.kind === 'text' ? 12 : 72) + measure(it.text, 12)[0] + 14);
  const w = Math.max(lg.w, need);
  return {
    rect: { x: lg.x, y: lg.y, w, h: LEGEND_HEAD + lg.items.length * LEGEND_ROW + 10 },
    items: lg.items.map((_, i) => ({ x: lg.x, y: lg.y + LEGEND_HEAD + i * LEGEND_ROW, w, h: LEGEND_ROW })),
    title: { x: lg.x, y: lg.y, w, h: LEGEND_HEAD },
  };
}

export function buildScene(d) {
  const s = STYLE[d.kind];
  const sc = { d, nodes: {}, links: {}, legend: null, notes: {} };
  for (const n of d.nodes) {
    const r = nodeRect(d.kind, n);
    if (d.kind === KIND_INFO) {
      sc.nodes[n.id] = { rect: r, rows: n.rows.map((_, i) => ({ x: r.x + 4, y: r.y + s.head + i * s.row, w: r.w - 8, h: s.row })), typeX: 0 };
    } else {
      sc.nodes[n.id] = { rect: r, rows: n.rows.map((_, i) => ({ x: r.x, y: r.y + s.head + i * s.row, w: r.w, h: s.row })), typeX: r.x + dataColumns(n).typeX };
    }
  }
  for (const l of d.links) if (sc.nodes[l.a] && sc.nodes[l.b]) sc.links[l.id] = linkGeo(d, l, sc.nodes);
  sc.legend = d.legend.visible ? legendGeo(d) : null;
  for (const note of d.notes) {
    const [w, h] = measure(note.text, note.size);
    sc.notes[note.id] = { x: note.x, y: note.y, w, h: h + 6 };
  }
  return sc;
}

// ── поиск под курсором (мировые координаты) ──
export function nodeAt(sc, x, y) {
  for (let i = sc.d.nodes.length - 1; i >= 0; i--) if (R.contains(sc.nodes[sc.d.nodes[i].id].rect, x, y)) return sc.d.nodes[i].id;
  return null;
}
export function rowAt(sc, id, y) {
  const rows = sc.nodes[id].rows;
  for (let i = 0; i < rows.length; i++) if (y >= rows[i].y && y < rows[i].y + rows[i].h) return i;
  return null;
}
export function linkLabelAt(sc, x, y) {
  const lgs = Object.values(sc.links).reverse();
  for (const lg of lgs) for (const lb of lg.labels) if (R.contains(lb.rect, x, y, 3)) return [lg.link.id, lb.which];
  return null;
}
export function linkAt(sc, x, y, tol) {
  let best = null;
  for (const lg of Object.values(sc.links)) {
    const dist = polyDist(x, y, lg.pts);
    if (dist <= tol && (!best || dist < best[0])) best = [dist, lg.link.id];
  }
  return best ? best[1] : null;
}
export function noteAt(sc, x, y) {
  const ids = Object.keys(sc.notes).reverse();
  for (const id of ids) if (R.contains(sc.notes[id], x, y, 3)) return id;
  return null;
}

// Прямоугольник, охватывающий всю диаграмму (пустая — 400×300).
export function bounds(sc) {
  const rects = Object.values(sc.nodes).map((g) => g.rect).concat(Object.values(sc.notes));
  if (sc.legend) rects.push(sc.legend.rect);
  for (const lg of Object.values(sc.links)) {
    const xs = lg.pts.map((p) => p[0]), ys = lg.pts.map((p) => p[1]);
    rects.push({ x: Math.min(...xs), y: Math.min(...ys), w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys) });
    for (const lb of lg.labels) rects.push(lb.rect);
  }
  if (!rects.length) return { x: 0, y: 0, w: 400, h: 300 };
  return rects.reduce((a, b) => R.union(a, b));
}

// Геометрия: прямоугольники, ортогональные маршруты связей, попадание курсора.
// Порт er_editor/geom.py (только то, что нужно для рисования и работы мышью).
// Прямоугольник: {x, y, w, h}. Точка: [x, y]. Направление: [dx, dy] — единичный вектор выхода.

export const ANCHOR_MARGIN = 14;
export const SIDES = ['top', 'right', 'bottom', 'left'];
export const DIRS = { top: [0, -1], bottom: [0, 1], left: [-1, 0], right: [1, 0] };

export const R = {
  right: (r) => r.x + r.w,
  bottom: (r) => r.y + r.h,
  cx: (r) => r.x + r.w / 2,
  cy: (r) => r.y + r.h / 2,
  contains: (r, px, py, pad = 0) => px >= r.x - pad && px <= r.x + r.w + pad && py >= r.y - pad && py <= r.y + r.h + pad,
  inflated: (r, d) => ({ x: r.x - d, y: r.y - d, w: r.w + 2 * d, h: r.h + 2 * d }),
  union: (a, b) => {
    const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y);
    return { x, y, w: Math.max(a.x + a.w, b.x + b.w) - x, h: Math.max(a.y + a.h, b.y + b.h) - y };
  },
};

function sideLength(r, side) {
  return side === 'top' || side === 'bottom' ? r.w : r.h;
}

export function clampOffset(length, t) {
  const off = t * length;
  const m = length > 2 * ANCHOR_MARGIN ? ANCHOR_MARGIN : length / 2;
  return Math.max(m, Math.min(length - m, off));
}

// Точка привязки связи к стороне прямоугольника и направление выхода наружу: [точка, направление].
export function anchorPoint(r, side, t) {
  const off = clampOffset(sideLength(r, side), t);
  if (side === 'top') return [[r.x + off, r.y], DIRS.top];
  if (side === 'bottom') return [[r.x + off, r.y + r.h], DIRS.bottom];
  if (side === 'left') return [[r.x, r.y + off], DIRS.left];
  return [[r.x + r.w, r.y + off], DIRS.right];
}

// Ближайшая к точке сторона и положение на ней: [сторона, t].
export function nearestAnchor(r, px, py) {
  let best = null;
  for (const side of SIDES) {
    let qx, qy, t;
    if (side === 'top' || side === 'bottom') {
      qx = Math.min(Math.max(px, r.x), r.x + r.w);
      qy = side === 'top' ? r.y : r.y + r.h;
      t = r.w ? (qx - r.x) / r.w : 0.5;
    } else {
      qx = side === 'left' ? r.x : r.x + r.w;
      qy = Math.min(Math.max(py, r.y), r.y + r.h);
      t = r.h ? (qy - r.y) / r.h : 0.5;
    }
    const dist = Math.hypot(px - qx, py - qy);
    if (!best || dist < best[0]) best = [dist, side, t];
  }
  return [best[1], best[2]];
}

// Ортогональный маршрут: [точки, ось среднего звена ('x' | 'y' | null)].
export function route(p1, d1, p2, d2, mid = null) {
  const [x1, y1] = p1, [x2, y2] = p2;
  const v1 = d1[0] === 0, v2 = d2[0] === 0;
  if (v1 && v2) {
    let my;
    if (d1[0] === d2[0] && d1[1] === d2[1]) {
      const ext = d1[1] < 0 ? Math.min(y1, y2) - 30 : Math.max(y1, y2) + 30;
      my = mid !== null ? mid : ext;
    } else {
      if (x1 === x2 && mid === null) return [[p1, p2], null];
      my = mid !== null ? mid : (y1 + y2) / 2;
    }
    return [[p1, [x1, my], [x2, my], p2], 'y'];
  }
  if (!v1 && !v2) {
    let mx;
    if (d1[0] === d2[0] && d1[1] === d2[1]) {
      const ext = d1[0] < 0 ? Math.min(x1, x2) - 30 : Math.max(x1, x2) + 30;
      mx = mid !== null ? mid : ext;
    } else {
      if (y1 === y2 && mid === null) return [[p1, p2], null];
      mx = mid !== null ? mid : (x1 + x2) / 2;
    }
    return [[p1, [mx, y1], [mx, y2], p2], 'x'];
  }
  return [v1 ? [p1, [x1, y2], p2] : [p1, [x2, y1], p2], null];
}

// Убирает совпадающие подряд точки и промежуточные точки на одной прямой.
export function cleanPoints(pts) {
  const out = [];
  for (const p of pts) {
    const last = out[out.length - 1];
    if (last && Math.abs(last[0] - p[0]) < 0.01 && Math.abs(last[1] - p[1]) < 0.01) continue;
    out.push(p);
  }
  let i = 1;
  while (i < out.length - 1) {
    const [a, b, c] = [out[i - 1], out[i], out[i + 1]];
    if ((Math.abs(a[0] - b[0]) < 0.01 && Math.abs(b[0] - c[0]) < 0.01) ||
        (Math.abs(a[1] - b[1]) < 0.01 && Math.abs(b[1] - c[1]) < 0.01)) out.splice(i, 1);
    else i += 1;
  }
  return out;
}

// Делит ломаную по длине пополам: [половина у A, половина у B].
export function splitHalf(pts) {
  const lens = [];
  for (let i = 0; i < pts.length - 1; i++) lens.push(Math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]));
  const half = lens.reduce((a, b) => a + b, 0) / 2;
  let acc = 0;
  for (let i = 0; i < lens.length; i++) {
    const seg = lens[i];
    if (seg && acc + seg >= half) {
      const t = (half - acc) / seg;
      const a = pts[i], b = pts[i + 1];
      const m = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
      return [pts.slice(0, i + 1).concat([m]), [m].concat(pts.slice(i + 1))];
    }
    acc += seg;
  }
  return [pts.slice(), pts.slice(-1)];
}

export function pointSegDist(px, py, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const l2 = dx * dx + dy * dy;
  if (l2 === 0) return Math.hypot(px - a[0], py - a[1]);
  const t = Math.max(0, Math.min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / l2));
  return Math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy));
}

export function polyDist(px, py, pts) {
  let best = Infinity;
  for (let i = 0; i < pts.length - 1; i++) best = Math.min(best, pointSegDist(px, py, pts[i], pts[i + 1]));
  return best;
}

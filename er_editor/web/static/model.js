// Модель данных редактора (та же структура JSON, что у Project.to_json в Python) и вспомогательные функции.
// Узлы и строки хранятся ровно с теми полями, которые знает Python (лишние поля сервер отвергнет).

export const KIND_INFO = 'info';
export const KIND_DATA = 'data';
export const KIND_TITLES = { info: 'Инфологическая (Баркер)', data: 'Даталогическая' };

// Размеры узлов: высота шапки, высота строки, нижний отступ, минимальная ширина.
export const STYLE = {
  info: { head: 38, row: 19, pad: 10, min_w: 200 },
  data: { head: 30, row: 22, pad: 6, min_w: 220 },
};
// Палитра узлов: [цвет шапки, цвет тела].
export const PALETTE = [
  ['#e4e9ef', '#ffffff'], ['#cfe3fb', '#f4f9ff'], ['#d4efdc', '#f5fbf7'],
  ['#faeec2', '#fffdf3'], ['#f8d4d4', '#fff6f6'], ['#e2d8f5', '#f8f5fd'],
];
export const GRID_STEP = 10;
export const MIN_ZOOM = 0.15, MAX_ZOOM = 3;

export const nodeHeight = (kind, nRows) => STYLE[kind].head + nRows * STYLE[kind].row + STYLE[kind].pad;

export const clone = (o) => JSON.parse(JSON.stringify(o));

export function newId(d, prefix) {
  d.counter += 1;
  return prefix + d.counter;
}

export const newRow = (over = {}) => ({ name: 'поле', mark: 'o', type: '', key: '', notnull: false, default: '', ...over });
// Правила ON DELETE, которые понимает база данных; подпись связи с любым другим текстом означает NO ACTION (как dbgen._on_delete).
export const ON_DELETE_RULES = ['CASCADE', 'RESTRICT', 'SET NULL', 'SET DEFAULT', 'NO ACTION'];
export const onDeleteRule = (label) => { const r = String(label || '').toUpperCase().split(/\s+/).filter(Boolean).join(' '); return ON_DELETE_RULES.includes(r) ? r : 'NO ACTION'; };
// Подсказка, если правило противоречит столбцам внешнего ключа (как dbgen.link_rule_hint).
export function ruleHint(child, link) {
  const names = new Set((link.fk_cols || []).map((c) => c.trim().toLowerCase()));
  const rows = child ? child.rows.filter((r) => names.has(r.name.trim().toLowerCase())) : [];
  const rule = onDeleteRule(link.label);
  if (rule === 'SET NULL' && rows.some((r) => r.notnull)) return 'SET NULL не сработает: столбец внешнего ключа объявлен NOT NULL.';
  if (rule === 'SET DEFAULT' && rows.some((r) => !(r.default || '').trim())) return 'SET DEFAULT: у столбца внешнего ключа не задан DEFAULT (подставится NULL). Задайте значение, которое есть в родительской таблице.';
  return '';
}

export function newLink(d, a, b) {
  return {
    id: newId(d, 'l'), a, b, sa: 'right', ta: 0.5, sb: 'left', tb: 0.5, card_a: 'M', card_b: '1',
    must_a: true, must_b: false, bar_a: false, bar_b: false, name_a: '', name_b: '', label: '', mid: null,
    off_a: [0, 0], off_b: [0, 0], off_c: [0, 0], row_a: null, row_b: null, fk_cols: [], ref_cols: [],
  };
}

export const nodeById = (d, id) => d.nodes.find((n) => n.id === id) || null;
export const linkById = (d, id) => d.links.find((l) => l.id === id) || null;
export const noteById = (d, id) => d.notes.find((n) => n.id === id) || null;
export const linksOf = (d, id) => d.links.filter((l) => l.a === id || l.b === id);

export function removeNode(d, id) {
  d.nodes = d.nodes.filter((n) => n.id !== id);
  d.links = d.links.filter((l) => l.a !== id && l.b !== id);
}

// ── метки ключей и UNIQUE ──
export const keyTokens = (key) => key.split(',').map((t) => t.trim().toUpperCase()).filter(Boolean);
export const formatUniques = (groups) => groups.map((g) => g.join(', ')).join('; ');
export function parseUniques(text) {
  return text.split(';').map((p) => p.split(',').map((c) => c.trim()).filter(Boolean)).filter((g) => g.length);
}

// ── функциональные зависимости («a, b -> c») ──
const ARROW = /\s*(?:-+>|=+>|→|⟶|⇒)\s*/;
const names = (text) => text.trim().replace(/^[{( ]+|[}) ]+$/g, '').split(',').map((c) => c.trim()).filter(Boolean);
export function parseFd(text) {
  const parts = text.trim().split(ARROW);
  if (parts.length !== 2) return null;
  const lhs = names(parts[0]), rhs = names(parts[1]);
  return lhs.length && rhs.length ? { lhs, rhs } : null;
}
const fdText = (fd) => `${fd.lhs.join(', ')} -> ${fd.rhs.join(', ')}`;
export function normalizeFds(text) {
  const good = [], bad = [];
  for (const part of text.split(/[;\n]/).map((p) => p.trim()).filter(Boolean)) {
    const fd = parseFd(part);
    if (fd) good.push(fdText(fd)); else bad.push(part);
  }
  return [good, bad];
}
export const formatFds = (items) => items.join('; ');
export function renameInFds(items, oldName, newName) {
  const same = (c) => c.trim().toLowerCase() === oldName.trim().toLowerCase();
  return items.map((it) => {
    const fd = parseFd(it);
    return fd ? fdText({ lhs: fd.lhs.map((c) => (same(c) ? newName : c)), rhs: fd.rhs.map((c) => (same(c) ? newName : c)) }) : it;
  });
}

// ── подсказки для быстрых типов ──
export const QUICK_TYPES = [['INT', 'INTEGER'], ['TEXT', 'TEXT'], ['REAL', 'REAL'], ['DATE', 'DATE']];

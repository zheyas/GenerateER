// Ядро веб-редактора: состояние, операции над проектом (порт методов App), обмен с сервером, окна и сообщения.
import {
  GRID_STEP, KIND_DATA, KIND_INFO, MIN_ZOOM, MAX_ZOOM, clone, keyTokens, linkById, newId, newLink, newRow, nodeById,
  noteById, nodeHeight, normalizeFds, parseUniques, removeNode, renameInFds,
} from './model.js';
import { buildScene, bounds, fitWidths, nodeRect } from './scene.js';

export const S = {
  project: null, blank: null, meta: null, kind: KIND_DATA,
  sel: { kind: null, id: null, row: null }, tool: 'select', linkSrc: null,
  cams: { info: { x: 80, y: 90, z: 1 }, data: { x: 80, y: 90, z: 1 } },
  undo: [], redo: [], modified: false, name: 'schema', lastDb: null,
  nf: { key: null, report: null, seq: 0 }, welcomeOff: false, description: '',
  view: { w: 1000, h: 700 }, rev: 0, panelDirty: true, edit: null, drag: null, mouse: [0, 0], modal: null, busy: '',
};

export const D = () => S.project.diagrams[S.kind];
export const DATA = () => S.project.diagrams[KIND_DATA];
export const INFO = () => S.project.diagrams[KIND_INFO];
export const cam = () => S.cams[S.kind];
export const snap = (v) => (S.project.grid ? Math.round(v / GRID_STEP) * GRID_STEP : v);

// ───────────────────────── перерисовка ─────────────────────────
let renderHook = () => {};
let pending = false;
export const onRender = (fn) => { renderHook = fn; };
// Перерисовка в ближайшем кадре; запасной таймер нужен, потому что в фоновой вкладке requestAnimationFrame не вызывается.
export function scheduleRender() {
  if (pending) return;
  pending = true;
  const run = () => { if (!pending) return; pending = false; renderHook(); };
  requestAnimationFrame(run);
  setTimeout(run, 80);
}
// Любое изменение модели: геометрия (geomOnly) не требует пересчёта панели и проверки нормальной формы.
export function touch(geomOnly = false) {
  S.rev += 1;
  if (!geomOnly) { S.panelDirty = true; scheduleNf(); }
  scheduleRender();
}
export function setSel(kind = null, id = null, row = null) {
  S.sel = { kind, id, row };
  S.panelDirty = true;
  scheduleRender();
}
let sceneCache = { rev: -1, kind: '', sc: null };
export function scene() {
  if (sceneCache.rev !== S.rev || sceneCache.kind !== S.kind || sceneCache.sc.d !== D()) sceneCache = { rev: S.rev, kind: S.kind, sc: buildScene(D()) };
  return sceneCache.sc;
}

// ───────────────────────── сообщения и окна ─────────────────────────
let toastTimer = 0;
export function say(msg, secs = 3) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), secs * 1000);
}
export function setBusy(msg) {
  S.busy = msg;
  document.body.classList.toggle('busy', !!msg);
  if (msg) say(msg, 60); else document.getElementById('toast').classList.remove('show');
}

// Модальное окно: buttons — [{label, cb, keep}]; Enter — первая кнопка, Esc — последняя; keep — не закрывать окно по кнопке.
// Тело — text, html или готовый DOM-узел content.
export function showModal({ title, text = '', html = '', content = null, buttons = [{ label: 'Понятно' }], wide = false }) {
  const root = document.getElementById('modal-root');
  root.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'modal' + (wide ? ' wide' : '');
  const head = document.createElement('div');
  head.className = 'modal-head';
  head.textContent = title;
  const body = document.createElement('div');
  body.className = 'modal-body';
  if (content) body.append(content); else if (html) body.innerHTML = html; else body.textContent = text;
  const foot = document.createElement('div');
  foot.className = 'modal-foot';
  const pick = (b) => { if (!b.keep) closeModal(); if (b.cb) b.cb(); };
  buttons.forEach((b, i) => {
    const el = document.createElement('button');
    el.className = 'btn' + (i === 0 ? ' primary' : '');
    el.textContent = b.label;
    el.addEventListener('click', () => pick(b));
    foot.append(el);
  });
  box.append(head, body, foot);
  root.append(box);
  root.classList.add('open');
  S.modal = { buttons, pick, cancel: buttons.find((b) => b.cancel) || buttons[buttons.length - 1] };
  setTimeout(() => (content && content.querySelector('textarea') ? content.querySelector('textarea') : foot.firstChild)?.focus(), 0);
}
export function closeModal() {
  const root = document.getElementById('modal-root');
  root.classList.remove('open');
  root.innerHTML = '';
  S.modal = null;
}
const escHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
export function showProblems(problems, title = 'Схему нельзя превратить в базу данных') {
  showModal({ title, html: '<ul class="problems">' + problems.slice(0, 12).map((p) => `<li>${escHtml(p)}</li>`).join('') + '</ul>' +
    (problems.length > 12 ? `<p>… и ещё ${problems.length - 12}</p>` : '') });
}

// ───────────────────────── обмен с сервером ─────────────────────────
export class ApiError extends Error {
  constructor(status, message, data = {}) { super(message); this.status = status; this.data = data; }
}
export async function api(path, { method = 'POST', json, body, headers = {} } = {}) {
  const opts = { method, headers: { ...headers } };
  if (json !== undefined) { opts.body = JSON.stringify(json); opts.headers['Content-Type'] = 'application/json'; } else if (body !== undefined) opts.body = body;
  let res;
  try { res = await fetch(path, opts); } catch (e) { throw new ApiError(0, 'Нет связи с сервером. Он запущен? (python run_web.py)'); }
  if (!res.ok) {
    let data = {};
    try { data = await res.json(); } catch (e) { /* не JSON */ }
    throw new ApiError(res.status, data.error || res.statusText, data);
  }
  return res;
}
export const apiJson = async (path, opts) => (await api(path, opts)).json();
export function reportError(e) {
  if (e instanceof ApiError && e.data.problems) { switchKind(KIND_DATA); showProblems(e.data.problems); return; }
  say(e.message || String(e), 7);
}
export function saveBlob(blob, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 15000);
}
const filenameOf = (res, fallback) => {
  const m = /filename\*=UTF-8''([^;]+)/.exec(res.headers.get('Content-Disposition') || '');
  return m ? decodeURIComponent(m[1]) : fallback;
};

// ───────────────────────── проект: история, файлы ─────────────────────────
export function snapshot() {
  S.undo.push(JSON.stringify(S.project));
  if (S.undo.length > 150) S.undo.shift();
  S.redo = [];
  S.modified = true;
}
export function mutate(fn) { snapshot(); fn(); touch(); }
function restore(text) {
  S.project = JSON.parse(text);
  const d = D();
  const s = S.sel;
  if ((s.kind === 'node' && !nodeById(d, s.id)) || (s.kind === 'link' && !linkById(d, s.id)) || (s.kind === 'note' && !noteById(d, s.id))) S.sel = { kind: null, id: null, row: null };
  S.modified = true;
  touch();
}
export function undo() {
  if (!S.undo.length) return;
  S.redo.push(JSON.stringify(S.project));
  restore(S.undo.pop());
}
export function redo() {
  if (!S.redo.length) return;
  S.undo.push(JSON.stringify(S.project));
  restore(S.redo.pop());
}
export function switchKind(kind) {
  if (kind === S.kind) return;
  document.getElementById('inline').blur();
  S.kind = kind;
  S.sel = { kind: null, id: null, row: null };
  S.tool = 'select';
  S.linkSrc = null;
  S.panelDirty = true;
  touch(true);
}
export function installProject(project, name) {
  for (const k of [KIND_INFO, KIND_DATA]) if (!project.diagrams[k]) project.diagrams[k] = clone(S.blank.diagrams[k]);
  for (const k of [KIND_INFO, KIND_DATA]) {
    const d = project.diagrams[k];
    d.notes = d.notes || [];
    d.links = d.links || [];
    d.nodes = (d.nodes || []).map((n) => ({ table: '', uniques: [], fds: [], color: 0, ...n }));
    fitWidths(d, true);
  }
  if (![1, 2, 3, 4].includes(project.normal_form)) project.normal_form = 3;
  S.project = project;
  S.welcomeOff = true;
  S.name = name || 'schema';
  S.undo = []; S.redo = []; S.modified = false;
  S.sel = { kind: null, id: null, row: null };
  S.tool = 'select'; S.linkSrc = null;
  S.nf = { key: null, report: null, seq: S.nf.seq };
  S.kind = project.diagrams[KIND_DATA].nodes.length && !project.diagrams[KIND_INFO].nodes.length ? KIND_DATA : (project.db_path ? KIND_INFO : KIND_DATA);
  touch();
  fitView(KIND_INFO);
  fitView(KIND_DATA);
}
export async function newProject() {
  const project = await apiJson('/api/new', { method: 'GET' });
  S.lastDb = null;
  installProject(project, 'schema');
}
// Чистый холст для рисования с нуля (если есть несохранённые изменения — сначала спрашивает).
export function newCanvas(after = () => {}) {
  const go = async () => {
    try { await newProject(); S.kind = KIND_DATA; S.welcomeOff = true; touch(); after(); } catch (e) { reportError(e); }
  };
  if (!S.modified) { go(); return; }
  showModal({ title: 'Начать с чистого холста?', text: 'Несохранённые изменения будут потеряны. Сохраните проект кнопкой «Сохранить», если он нужен.',
    buttons: [{ label: 'Начать заново', cb: go }, { label: 'Отмена' }] });
}
// Строит обе диаграммы по описанию (SQL или текст). Возвращает {notes, format, tables, links}; при ошибке бросает ApiError.
export async function buildFromDescription(text) {
  const r = await apiJson('/api/describe', { json: { text } });
  S.lastDb = null;
  installProject(r.project, 'описание');
  S.kind = KIND_DATA;
  S.welcomeOff = true;
  touch();
  fitView(KIND_DATA);
  return r;
}
// Ищет по данным загруженной БД кандидатов в зависимости таблицы nid и предлагает выбрать; ничего не меняет без подтверждения.
export async function suggestFds(nid) {
  const n = nodeById(D(), nid);
  if (!n || !S.lastDb) { say('Откройте файл .db с данными: зависимости ищутся по его содержимому', 5); return; }
  setBusy('Ищу зависимости в данных…');
  try {
    const r = await apiJson('/api/fds', { body: S.lastDb.buf, headers: { 'Content-Type': 'application/octet-stream' } });
    const t = r.tables.find((x) => x.name.toLowerCase() === n.title.trim().toLowerCase());
    setBusy('');
    if (!t) { say(`В загруженной базе нет таблицы «${n.title}»`, 5); return; }
    const have = new Set(n.fds.map((f) => f.replace(/\s+/g, '').toLowerCase()));
    const fresh = t.fds.filter((f) => !have.has(f.text.replace(/\s+/g, '').toLowerCase()));
    const boxes = fresh.map((f) => { const cb = document.createElement('input'); cb.type = 'checkbox'; return [f, cb]; });
    const content = document.createElement('div');
    const p = (text, cls = '') => { const e = document.createElement('p'); e.className = cls; e.textContent = text; return e; };
    content.append(p(`Таблица «${t.name}», прочитано строк: ${t.rows}.` + (t.note ? ` ${t.note}.` : '')));
    if (fresh.length) {
      content.append(p('Эти зависимости выполняются во всех прочитанных строках. Данные не доказывают зависимость, а только не опровергают её: отметьте те, что верны по смыслу предметной области.', 'desc-warn'));
      for (const [f, cb] of boxes) { const l = document.createElement('label'); l.className = 'fd-pick'; l.append(cb, ' ' + f.text); content.append(l); }
    } else content.append(p(t.fds.length ? 'Все найденные зависимости уже записаны.' : 'Подходящих зависимостей в данных не найдено.'));
    showModal({ title: 'Зависимости по данным', content, wide: true,
      buttons: fresh.length ? [{ label: 'Добавить отмеченные', cb: () => {
        const picked = boxes.filter(([, cb]) => cb.checked).map(([f]) => f.text);
        if (picked.length) setFds(nid, [...n.fds, ...picked].join('; '));
      } }, { label: 'Отмена' }] : [{ label: 'Понятно' }] });
  } catch (e) { setBusy(''); reportError(e); }
}
export async function importFile(file) {
  const base = file.name.replace(/\.(db|sqlite3?|erd\.json|json)$/i, '');
  if (/\.json$/i.test(file.name)) {
    try {
      const project = JSON.parse(await file.text());
      if (!project.diagrams) throw new Error('нет раздела diagrams');
      S.lastDb = null;
      installProject(project, base.replace(/\.erd$/, ''));
      say(`Проект открыт: ${file.name}`);
    } catch (e) { say(`Не удалось открыть проект: ${e.message}`, 7); }
    return;
  }
  setBusy('Читаю схему базы данных…');
  try {
    const buf = await file.arrayBuffer();
    const project = await apiJson(`/api/import?name=${encodeURIComponent(file.name)}`, { body: buf, headers: { 'Content-Type': 'application/octet-stream' } });
    S.lastDb = { name: file.name, buf };
    installProject(project, base);
    say(`Схема загружена: таблиц — ${project.diagrams[KIND_DATA].nodes.length}`);
  } catch (e) { reportError(e); } finally { setBusy(''); }
}
export function reimport() {
  if (!S.lastDb) { say('Исходная база неизвестна: откройте файл .db заново', 5); return; }
  showModal({ title: 'Импортировать заново?', text: 'Все правки обеих диаграмм будут заменены схемой из базы данных.',
    buttons: [{ label: 'Импортировать', cb: () => importFile(new File([S.lastDb.buf], S.lastDb.name)) }, { label: 'Отмена' }] });
}
export function saveProject() {
  const blob = new Blob([JSON.stringify(S.project, null, 1)], { type: 'application/json' });
  saveBlob(blob, `${S.name}.erd.json`);
  S.modified = false;
  say(`Проект сохранён: ${S.name}.erd.json`);
  scheduleRender();
}
export async function exportPng(both = false) {
  const kinds = both ? [KIND_INFO, KIND_DATA] : [S.kind];
  setBusy('Рисую PNG…');
  try {
    for (const k of kinds) {
      const res = await api(`/api/png?name=${encodeURIComponent(S.name)}`, { json: { diagram: S.project.diagrams[k], scale: S.project.export_scale } });
      saveBlob(await res.blob(), filenameOf(res, `${S.name}_${k === KIND_INFO ? 'infological' : 'datalogical'}.png`));
    }
    say('PNG сохранён');
  } catch (e) { reportError(e); } finally { setBusy(''); }
}

// ───────────────────────── вид ─────────────────────────
export function fitView(kind = S.kind) {
  const d = S.project.diagrams[kind];
  const c = S.cams[kind];
  if (!d.nodes.length) { Object.assign(c, { x: 80, y: 90, z: 1 }); scheduleRender(); return; }
  const b = bounds(buildScene(d));
  const z = Math.max(MIN_ZOOM, Math.min((S.view.w - 80) / b.w, (S.view.h - 80) / b.h, 1));
  c.z = z;
  c.x = (S.view.w - b.w * z) / 2 - b.x * z;
  c.y = (S.view.h - b.h * z) / 2 - b.y * z;
  scheduleRender();
}
export function zoomAt(factor, sx, sy) {
  const c = cam();
  const wx = (sx - c.x) / c.z, wy = (sy - c.y) / c.z;
  c.z = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, c.z * factor));
  c.x = sx - wx * c.z;
  c.y = sy - wy * c.z;
  scheduleRender();
}
export const zoomCenter = (factor) => zoomAt(factor, S.view.w / 2, S.view.h / 2);
export const zoom100 = () => zoomCenter(1 / cam().z);
export const toWorld = (sx, sy) => [(sx - cam().x) / cam().z, (sy - cam().y) / cam().z];
export const toScreen = (wx, wy) => [wx * cam().z + cam().x, wy * cam().z + cam().y];
export const viewCenterWorld = () => toWorld(S.view.w / 2, S.view.h / 2);
export function revealNode(id) {
  const geo = scene().nodes[id];
  if (!geo) return;
  const [sx, sy] = toScreen(geo.rect.x + geo.rect.w / 2, geo.rect.y + geo.rect.h / 2);
  if (sx < 60 || sy < 60 || sx > S.view.w - 60 || sy > S.view.h - 60) { cam().x += S.view.w / 2 - sx; cam().y += S.view.h / 2 - sy; }
  scheduleRender();
}

// ───────────────────────── изменение модели ─────────────────────────
export const grow = () => fitWidths(D(), true);

export function nSet(nid, patch) {
  const n = nodeById(D(), nid);
  if (!n) return;
  mutate(() => { Object.assign(n, patch); grow(); });
}
function renameColumn(d, nid, oldName, newName) {
  for (const l of d.links) {
    if (l.a === nid) l.fk_cols = l.fk_cols.map((c) => (c === oldName ? newName : c));
    if (l.b === nid) l.ref_cols = l.ref_cols.map((c) => (c === oldName ? newName : c));
  }
  const n = nodeById(d, nid);
  if (n) {
    n.uniques = n.uniques.map((g) => g.map((c) => (c === oldName ? newName : c)));
    n.fds = renameInFds(n.fds, oldName, newName);
  }
}
export function rSet(nid, i, patch) {
  const n = nodeById(D(), nid);
  if (!n || i < 0 || i >= n.rows.length) return;
  mutate(() => {
    const old = n.rows[i].name;
    Object.assign(n.rows[i], patch);
    if ('name' in patch && patch.name !== old) renameColumn(D(), nid, old, patch.name);
    grow();
  });
}
export function lSet(lid, patch) {
  const l = linkById(D(), lid);
  if (l) mutate(() => Object.assign(l, patch));
}
export function noteSet(id, patch) {
  const n = noteById(D(), id);
  if (n) mutate(() => Object.assign(n, patch));
}
export function legendSet(patch) { mutate(() => Object.assign(D().legend, patch)); }
export function itemSet(i, patch) {
  const it = D().legend.items[i];
  if (it) mutate(() => Object.assign(it, patch));
}
export function toggleKey(nid, i, token) {
  const n = nodeById(D(), nid);
  if (!n || !n.rows[i]) return;
  let toks = keyTokens(n.rows[i].key);
  toks = toks.includes(token) ? toks.filter((t) => t !== token) : toks.concat(token);
  const order = { PK: 0, FK: 1, U: 2 };
  toks.sort((a, b) => (order[a] ?? 9) - (order[b] ?? 9));
  rSet(nid, i, { key: toks.join(',') });
}
export function setUniques(nid, text) {
  const n = nodeById(D(), nid);
  if (!n) return;
  mutate(() => {
    const groups = parseUniques(text);
    n.uniques = groups.filter((g) => g.length > 1);
    for (const g of groups) {
      if (g.length !== 1) continue;
      for (const r of n.rows) if (r.name.trim().toLowerCase() === g[0].toLowerCase() && !keyTokens(r.key).includes('U')) r.key = keyTokens(r.key).concat('U').join(',');
    }
  });
}
export function setFds(nid, text) {
  const n = nodeById(D(), nid);
  if (!n) return;
  const [good, bad] = normalizeFds(text);
  mutate(() => { n.fds = good; });
  if (bad.length) say(`Не распознано: ${bad.join('; ')}. Формат записи: город -> страна`, 7);
}

// Подбирает свободное место для нового узла: ближайшие к (x, y) позиции, сначала в видимой части холста.
export function freePosition(w, h, x, y) {
  const d = D();
  const rects = d.nodes.map((n) => nodeRect(d.kind, n));
  const lg = scene().legend;
  if (lg) rects.push(lg.rect);
  const [vx0, vy0] = toWorld(0, 0), [vx1, vy1] = toWorld(S.view.w, S.view.h);
  const cands = [];
  for (let dy = 0; dy < 900; dy += 60) for (let dx = -800; dx <= 1200; dx += 80) cands.push([dx, dy]);
  cands.sort((a, b) => Math.hypot(a[0], a[1] * 1.3) - Math.hypot(b[0], b[1] * 1.3));
  const free = ([dx, dy]) => {
    const c = { x: x + dx - 20, y: y + dy - 20, w: w + 40, h: h + 40 };
    return !rects.some((r) => c.x < r.x + r.w && r.x < c.x + c.w && c.y < r.y + r.h && r.y < c.y + c.h);
  };
  const inView = ([dx, dy]) => x + dx >= vx0 && y + dy >= vy0 && x + dx + w <= vx1 && y + dy + h <= vy1;
  const hit = cands.find((c) => free(c) && inView(c)) || cands.find(free);
  return hit ? [x + hit[0], y + hit[1]] : [x, y];
}
export function addNode() {
  const d = D();
  snapshot();
  let n;
  if (d.kind === KIND_INFO) {
    n = { id: newId(d, 'n'), title: 'СУЩНОСТЬ', x: 0, y: 0, w: 240, rows: [newRow({ name: 'код', mark: '#' }), newRow({ name: 'атрибут', mark: '*' })], color: 0, table: '', uniques: [], fds: [] };
  } else {
    const used = new Set(d.nodes.map((x) => x.title.trim().toLowerCase()));
    let title = 'table';
    for (let k = 2; used.has(title); k++) title = `table_${k}`;
    n = { id: newId(d, 'n'), title, x: 0, y: 0, w: 240, color: 0, table: '', uniques: [], fds: [],
      rows: [newRow({ name: 'id', mark: '', type: 'INTEGER', key: 'PK', notnull: true }), newRow({ name: 'name', mark: '', type: 'TEXT' })] };
  }
  fitWidths({ kind: d.kind, nodes: [n] });
  S.welcomeOff = true;
  const [cx, cy] = viewCenterWorld();
  const [x, y] = freePosition(n.w, nodeHeight(d.kind, n.rows.length), snap(cx - n.w / 2), snap(cy - 40));
  n.x = x; n.y = y;
  d.nodes.push(n);
  setSel('node', n.id);
  touch();
  return n.id;
}
export function addNote() {
  const d = D();
  snapshot();
  const [cx, cy] = viewCenterWorld();
  const note = { id: newId(d, 't'), x: snap(cx - 40), y: snap(cy), text: 'Надпись', size: 14 };
  d.notes.push(note);
  setSel('note', note.id);
  touch();
  return note.id;
}
export function duplicateNode() {
  const n = S.sel.kind === 'node' ? nodeById(D(), S.sel.id) : null;
  if (!n) return;
  snapshot();
  const c = clone(n);
  c.id = newId(D(), 'n');
  c.title = `${n.title} (копия)`;
  c.x += 40; c.y += 40;
  D().nodes.push(c);
  setSel('node', c.id);
  touch();
}
export function addRow() {
  const d = D(), s = S.sel;
  const n = s.kind === 'node' ? nodeById(d, s.id) : null;
  if (!n) return null;
  snapshot();
  const row = d.kind === KIND_INFO ? newRow({ name: 'атрибут' }) : newRow({ name: 'поле', mark: '', type: 'TEXT' });
  const at = s.row !== null ? s.row + 1 : n.rows.length;
  n.rows.splice(at, 0, row);
  grow();
  setSel('node', n.id, at);
  touch();
  return [n.id, at];
}
export function moveRow(delta) {
  const s = S.sel, n = s.kind === 'node' ? nodeById(D(), s.id) : null;
  if (!n || s.row === null || s.row + delta < 0 || s.row + delta >= n.rows.length) return;
  snapshot();
  const i = s.row;
  [n.rows[i], n.rows[i + delta]] = [n.rows[i + delta], n.rows[i]];
  setSel('node', n.id, i + delta);
  touch();
}
export function deleteSelection() {
  const s = S.sel, d = D();
  if (s.kind === 'node') {
    const n = nodeById(d, s.id);
    if (!n) return;
    snapshot();
    if (s.row !== null && s.row >= 0 && s.row < n.rows.length) { n.rows.splice(s.row, 1); setSel('node', n.id); } else { removeNode(d, n.id); setSel(); }
  } else if (s.kind === 'link' && linkById(d, s.id)) {
    snapshot(); d.links = d.links.filter((l) => l.id !== s.id); setSel();
  } else if (s.kind === 'note' && noteById(d, s.id)) {
    snapshot(); d.notes = d.notes.filter((n) => n.id !== s.id); setSel();
  } else if (s.kind === 'legend') {
    snapshot();
    if (s.row !== null && s.row >= 0 && s.row < d.legend.items.length) { d.legend.items.splice(s.row, 1); setSel('legend'); } else { d.legend.visible = false; setSel(); }
  } else return;
  touch();
}
export function deleteNodeWhole() {
  if (S.sel.kind === 'node') { S.sel.row = null; deleteSelection(); }
}
export function toggleLegend() {
  snapshot();
  const lg = D().legend;
  lg.visible = !lg.visible;
  if (lg.visible) setSel('legend');
  touch();
}
export function resetLegend() {
  snapshot();
  const old = D().legend;
  const src = S.kind === KIND_INFO ? S.meta.legends.info : (D().arrows ? S.meta.legends.data_arrows : S.meta.legends.data_crow);
  const lg = clone(src);
  lg.x = old.x; lg.y = old.y; lg.visible = true;
  D().legend = lg;
  setSel('legend');
  touch();
}
export function addLegendItem() {
  snapshot();
  D().legend.items.push({ kind: 'text', text: 'новый пункт' });
  setSel('legend', null, D().legend.items.length - 1);
  touch();
  return D().legend.items.length - 1;
}
export function moveItem(delta) {
  const s = S.sel, items = D().legend.items;
  if (s.kind !== 'legend' || s.row === null || s.row + delta < 0 || s.row + delta >= items.length) return;
  snapshot();
  const i = s.row;
  [items[i], items[i + delta]] = [items[i + delta], items[i]];
  setSel('legend', null, i + delta);
  touch();
}
export function toggleGrid(on) { mutate(() => { S.project.grid = on; }); }
export function setArrows(on) {
  const d = DATA();
  if (d.arrows === on) return;
  snapshot();
  const keyOf = (lg) => JSON.stringify(lg.items.map((i) => [i.kind, i.text]));
  const wasDefault = keyOf(d.legend) === keyOf(S.meta.legends[d.arrows ? 'data_arrows' : 'data_crow']);
  d.arrows = on;
  if (wasDefault) d.legend.items = clone(S.meta.legends[on ? 'data_arrows' : 'data_crow']).items;
  touch();
}
export function setNormalForm(level) {
  if (level === S.project.normal_form) return;
  mutate(() => { S.project.normal_form = level; });
  runNf().then((r) => r && say(r.verdict, 5)).catch(reportError);
}

// ── связи ──
export function bindForeignKey(l) {
  const d = D();
  const child = nodeById(d, l.a), parent = nodeById(d, l.b);
  if (!child || !parent) return;
  const pkRows = parent.rows.filter((r) => keyTokens(r.key).includes('PK') && r.name.trim());
  if (!pkRows.length) { say(`У таблицы «${parent.title}» нет первичного ключа: отметьте поле меткой PK и создайте связь заново`, 7); return; }
  const cols = [];
  for (const pr of pkRows) {
    const base = `${parent.title.trim()}_${pr.name.trim()}`;
    const existing = child.rows.find((r) => r.name === base);
    if (existing && keyTokens(existing.key).includes('FK')) { cols.push(base); continue; }
    let name = base;
    for (let k = 2; child.rows.some((r) => r.name === name); k++) name = `${base}_${k}`;
    child.rows.push(newRow({ name, mark: '', type: pr.type || 'INTEGER', key: 'FK', notnull: l.must_a }));
    cols.push(name);
  }
  l.fk_cols = cols;
  l.ref_cols = pkRows.map((r) => r.name);
  grow();
}
export async function createLink(a, b) {
  const d = D();
  snapshot();
  const l = newLink(d, a, b);
  if (d.kind === KIND_INFO) { l.name_a = 'относится к'; l.name_b = 'включает'; } else l.label = 'NO ACTION';
  d.links.push(l);
  if (d.kind === KIND_DATA) bindForeignKey(l);
  setSel('link', l.id);
  touch();
  say('Связь создана: подпишите концы двойным кликом или в панели справа');
  await routeLinks([l.id]);
}
export async function routeLinks(ids) {
  try {
    const r = await apiJson('/api/route', { json: { diagram: D(), ids } });
    for (const [id, g] of Object.entries(r.links)) {
      const l = linkById(D(), id);
      if (l) Object.assign(l, g);
    }
    touch(true);
  } catch (e) { /* маршрут по умолчанию остаётся */ }
}
export function swapLink(lid) {
  const l = linkById(D(), lid);
  if (!l) return;
  mutate(() => {
    [l.a, l.b] = [l.b, l.a]; [l.sa, l.sb] = [l.sb, l.sa]; [l.ta, l.tb] = [l.tb, l.ta];
    [l.card_a, l.card_b] = [l.card_b, l.card_a]; [l.must_a, l.must_b] = [l.must_b, l.must_a];
    [l.bar_a, l.bar_b] = [l.bar_b, l.bar_a]; [l.name_a, l.name_b] = [l.name_b, l.name_a];
    [l.off_a, l.off_b] = [l.off_b, l.off_a]; [l.row_a, l.row_b] = [l.row_b, l.row_a]; [l.fk_cols, l.ref_cols] = [l.ref_cols, l.fk_cols];
  });
}
export async function rerouteLink(lid) {
  const l = linkById(D(), lid);
  if (!l) return;
  snapshot();
  l.off_a = [0, 0]; l.off_b = [0, 0]; l.off_c = [0, 0];
  touch();
  await routeLinks([lid]);
}
export async function autoLayout() {
  setBusy('Расставляю таблицы…');
  try {
    const r = await apiJson('/api/layout', { json: { diagram: D() } });
    snapshot();
    S.project.diagrams[S.kind] = r.diagram;
    fitWidths(D(), true);
    touch();
    fitView();
    say('Автораскладка выполнена (Ctrl+Z — отменить)');
  } catch (e) { reportError(e); } finally { setBusy(''); }
}
export async function buildInfoFromSchema() {
  try {
    const r = await apiJson('/api/info', { json: { diagram: DATA() } });
    snapshot();
    S.project.diagrams[KIND_INFO] = r.diagram;
    fitWidths(INFO(), true);
    if (S.kind === KIND_INFO) { S.sel = { kind: null, id: null, row: null }; }
    touch();
    fitView(KIND_INFO);
    return true;
  } catch (e) { reportError(e); return false; }
}
export function rebuildInfoDialog() {
  if (!DATA().nodes.length) { say('Сначала нарисуйте таблицы на вкладке «Даталогическая»', 5); return; }
  if (!INFO().nodes.length) { buildInfoFromSchema(); return; }
  showModal({ title: 'Заменить инфологическую диаграмму?', text: 'Сущности и связи этой вкладки будут построены заново по таблицам. Изменение можно отменить (Ctrl+Z).',
    buttons: [{ label: 'Заменить', cb: buildInfoFromSchema }, { label: 'Отмена' }] });
}

// ───────────────────────── нормальные формы ─────────────────────────
function nfKey() {
  const d = DATA();
  return JSON.stringify([S.project.normal_form,
    d.nodes.map((n) => [n.id, n.title, n.rows.map((r) => [r.name, r.type, r.key, r.notnull]), n.uniques, n.fds]),
    d.links.map((l) => [l.id, l.a, l.b, l.fk_cols, l.ref_cols])]);
}
let nfTimer = 0;
export function scheduleNf() {
  if (!S.project || nfKey() === S.nf.key) return;
  clearTimeout(nfTimer);
  nfTimer = setTimeout(() => runNf().catch(() => {}), 120);
}
// Запрашивает у сервера проверку; возвращает актуальный отчёт.
export async function runNf() {
  const key = nfKey();
  if (S.nf.key === key && S.nf.report) return S.nf.report;
  const seq = ++S.nf.seq;
  const report = await apiJson('/api/check', { json: { diagram: DATA(), level: S.project.normal_form } });
  if (seq === S.nf.seq) { S.nf = { key, report, seq }; S.panelDirty = true; scheduleRender(); }
  return report;
}
// Подсветка таблиц и строк по замечаниям отчёта: {id узла: {level, rows}}.
export function nfFlags() {
  const rep = S.nf.report, out = {};
  if (S.kind !== KIND_DATA || !rep) return out;
  const d = DATA();
  for (const iss of rep.issues) {
    const n = iss.node_id ? nodeById(d, iss.node_id) : null;
    if (!n) continue;
    const flag = out[iss.node_id] || (out[iss.node_id] = { level: iss.severity, rows: {} });
    if (iss.severity === 'error') flag.level = 'error';
    for (const c of iss.columns) {
      const i = n.rows.findIndex((r) => r.name.trim().toLowerCase() === c.trim().toLowerCase());
      if (i >= 0 && flag.rows[i] !== 'error') flag.rows[i] = iss.severity;
    }
  }
  return out;
}
export function showNfReport() {
  switchKind(KIND_DATA);
  S.sel = { kind: null, id: null, row: null };
  S.tool = 'select'; S.linkSrc = null;
  S.panelDirty = true;
  document.getElementById('panel').scrollTop = 0;
  scheduleRender();
}
export function focusIssue(nid, columns) {
  switchKind(KIND_DATA);
  const n = nodeById(D(), nid);
  if (!n) return;
  const names = n.rows.map((r) => r.name.trim().toLowerCase());
  const row = columns.map((c) => names.indexOf(c.trim().toLowerCase())).find((i) => i >= 0);
  setSel('node', nid, row === undefined ? null : row);
  scene();
  revealNode(nid);
}
export async function saveNfReport() {
  try {
    const rep = await runNf();
    const lines = [`# Проверка нормальной формы: ${S.meta.levels[rep.target].name} — ${S.name}`, '', `**${rep.verdict}.**`, ''];
    lines.push(`Требования ${S.meta.levels[rep.target].name}: ${S.meta.levels[rep.target].rule}.`, '');
    if (!rep.issues.length) lines.push('Замечаний нет.');
    else {
      lines.push('| Тип | Правило | Таблица | Столбцы | Что не так | Как исправить |', '|---|---|---|---|---|---|');
      const cell = (s) => String(s || '—').replace(/\|/g, '/');
      for (const i of rep.issues) {
        lines.push(`| ${i.severity === 'error' ? 'нарушение' : 'подозрение'} | ${cell(`${S.meta.levels[i.level] ? S.meta.levels[i.level].name : 'схема'}: ${i.rule}`)} | ${cell(i.table)} | ${cell(i.columns.join(', '))} | ${cell(i.message)} | ${cell(i.hint)} |`);
      }
    }
    saveBlob(new Blob([lines.join('\n') + '\n'], { type: 'text/markdown' }), `${S.name}_normal_form.md`);
    say('Отчёт сохранён');
  } catch (e) { reportError(e); }
}

// ───────────────────────── скачивание базы данных ─────────────────────────
function confirmNf(rep, go) {
  const first = rep.issues.filter((i) => i.severity === 'error').slice(0, 3).map((i) => `${i.table}: ${i.rule}`).join('; ');
  showModal({
    title: `Схема не соответствует ${S.meta.levels[rep.target].name}`,
    text: `Нарушений: ${rep.violations} (${first}${rep.violations > 3 ? ' …' : ''}). Показать их на схеме или всё равно скачать?`,
    buttons: [{ label: 'Показать нарушения', cb: showNfReport }, { label: 'Скачать всё равно', cb: go }, { label: 'Отмена' }],
  });
}
async function gateExport(go) {
  if (!DATA().nodes.length) { switchKind(KIND_DATA); say('Сначала нарисуйте таблицы на вкладке «Даталогическая» (кнопка «+ Таблица»)', 6); return; }
  let rep;
  try { rep = await runNf(); } catch (e) { reportError(e); return; }
  const broken = rep.issues.filter((i) => i.level === 0 && i.severity === 'error');
  if (broken.length) { switchKind(KIND_DATA); showProblems(broken.map((i) => i.message)); return; }
  if (rep.ok) go(); else confirmNf(rep, go);
}
async function downloadNow(bundle) {
  setBusy(bundle ? 'Собираю комплект…' : 'Создаю базу данных…');
  try {
    if (bundle && !INFO().nodes.length) await buildInfoFromSchema();
    const res = await api(`/api/${bundle ? 'bundle' : 'database'}?name=${encodeURIComponent(S.name)}`, { json: { project: S.project, sample: S.project.fill_sample } });
    const blob = await res.blob();
    saveBlob(blob, filenameOf(res, bundle ? `${S.name}_bundle.zip` : `${S.name}.db`));
    let info = {};
    try { info = JSON.parse(decodeURIComponent(res.headers.get('X-Db-Report') || '{}')); } catch (e) { /* без отчёта */ }
    say(bundle ? 'Комплект сохранён: база, обе модели (PNG) и отчёт о нормальной форме (в ZIP)' :
      `База данных создана: таблиц — ${info.tables}${S.project.fill_sample ? `, записей: ${info.rows}` : ''}`, 7);
    if (info.warnings && info.warnings.length) showProblems(info.warnings, 'База создана, но есть предупреждения');
  } catch (e) { reportError(e); } finally { setBusy(''); }
}
export const downloadDb = () => gateExport(() => downloadNow(false));
export const downloadBundle = () => gateExport(() => downloadNow(true));
export function toggleSample() { mutate(() => { S.project.fill_sample = !S.project.fill_sample; }); }

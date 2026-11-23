// Панель справа: свойства выделенного объекта; без выделения — отчёт о нормальной форме, вид, экспорт, база данных.
import * as C from './core.js';
import { S, D, DATA } from './core.js';
import { KIND_DATA, KIND_INFO, KIND_TITLES, ON_DELETE_RULES, PALETTE, QUICK_TYPES, formatFds, formatUniques, keyTokens, linkById, linksOf, nodeById, noteById, onDeleteRule, ruleHint } from './model.js';

const LEGEND_KINDS_ORDER = () => Object.keys(S.meta.legend_kinds);
const MAX_ISSUES = 40;

export function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === false || v === null || v === undefined) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}

const header = (text, muted = false) => h('div', { class: 'p-header' + (muted ? ' muted' : '') }, text);
const note = (text, cls = '') => h('div', { class: 'p-note ' + cls }, text);
const gap = (n = 8) => h('div', { style: `height:${n}px` });
function field(label, fid, value, commit, attrs = {}) {
  const input = h('input', { class: 'p-input', type: attrs.type || 'text', value, 'data-fid': fid, spellcheck: 'false', autocomplete: 'off',
    onchange: (e) => commit(e.target.value), onkeydown: (e) => { if (e.key === 'Enter') e.target.blur(); } });
  return h('label', { class: 'p-row' }, h('span', { class: 'p-label' }, label), input);
}
function choice(label, options) {
  return h('div', { class: 'p-row' }, h('span', { class: 'p-label' }, label),
    h('div', { class: 'p-choice' }, options.map((o) => h('button', { class: 'btn small' + (o.active ? ' active' : ''), onclick: o.onclick, disabled: o.enabled === false }, o.text))));
}
const buttons = (items) => h('div', { class: 'p-buttons' }, items.map((b) =>
  h('button', { class: 'btn small' + (b.danger ? ' danger' : ''), disabled: b.enabled === false, onclick: b.onclick }, b.text)));

// ── карточки замечаний о нормальной форме ──
function issueCard(i) {
  const lvl = S.meta.levels[i.level] ? S.meta.levels[i.level].name : 'схема';
  const card = h('div', { class: 'issue ' + (i.severity === 'error' ? 'err' : 'warn') + (i.node_id ? ' clickable' : ''),
    title: i.node_id ? 'Показать на схеме' : '', onclick: () => i.node_id && C.focusIssue(i.node_id, i.columns) },
  h('div', { class: 'issue-head' }, `${lvl} · ${i.table ? `${i.table}: ` : ''}${i.rule}`), h('div', { class: 'issue-msg' }, i.message),
  i.hint ? h('div', { class: 'issue-hint' }, `Что делать: ${i.hint}`) : null);
  return card;
}

export function nfPanel(nodeId = null) {
  const rep = S.nf.report;
  const out = [];
  if (!rep) return [header('Нормальная форма'), note('Проверка…', 'muted')];
  const lvl = S.meta.levels[rep.target];
  if (nodeId === null) {
    out.push(header('Нормальная форма'));
    out.push(choice('Проверять', Object.keys(S.meta.levels).map((k) => ({ text: S.meta.levels[k].name, active: rep.target === +k, onclick: () => C.setNormalForm(+k) }))));
    out.push(note(`${lvl.name}: ${lvl.rule}.`, 'small'));
    out.push(note(rep.verdict, rep.tables ? (rep.ok ? 'good big' : 'bad big') : 'muted big'));
    if (rep.tables && !rep.ok) out.push(note(`Достигнута: ${rep.achieved ? S.meta.levels[rep.achieved].name : 'ни одна форма'}.`));
  } else {
    out.push(gap(6), header(`Проверка ${lvl.name}: эта таблица`, true));
  }
  const items = nodeId === null ? rep.issues : rep.issues.filter((i) => i.node_id === nodeId);
  if (nodeId !== null && !items.length) out.push(note('Замечаний нет.', 'good'));
  items.slice(0, MAX_ISSUES).forEach((i) => out.push(issueCard(i)));
  if (items.length > MAX_ISSUES) out.push(note(`… и ещё ${items.length - MAX_ISSUES}`));
  if (nodeId === null && rep.tables) {
    out.push(buttons([{ text: 'Отчёт в .md', onclick: C.saveNfReport }]));
    out.push(note('Нарушения — по записанным зависимостям и признакам в структуре схемы; «подозрение» не меняет вердикт. ' +
      'Зависимости таблицы записываются в её панели в поле «Зависимости».', 'small'));
  }
  return out;
}

// ── панель без выделения ──
function diagramPanel() {
  const d = D(), p = S.project, out = [];
  if (S.kind === KIND_DATA) out.push(...nfPanel(), gap(8));
  out.push(header(KIND_TITLES[S.kind]));
  out.push(note(`${S.kind === KIND_INFO ? 'Сущностей' : 'Таблиц'}: ${d.nodes.length}   связей: ${d.links.length}   надписей: ${d.notes.length}`));
  if (p.db_path) out.push(note(`БД: ${p.db_path}`, 'small'));
  out.push(gap(4), header('Вид и экспорт'));
  out.push(choice('Сетка', [{ text: 'Точки и привязка', active: p.grid, onclick: () => C.toggleGrid(true) }, { text: 'Нет', active: !p.grid, onclick: () => C.toggleGrid(false) }]));
  out.push(choice('Масштаб PNG', [1, 2, 3].map((k) => ({ text: `×${k}`, active: p.export_scale === k, onclick: () => C.mutate(() => { p.export_scale = k; }) }))));
  out.push(buttons([{ text: 'PNG (эта диаграмма)', onclick: () => C.exportPng(false), enabled: d.nodes.length > 0 }, { text: 'PNG (обе)', onclick: () => C.exportPng(true) }]));
  out.push(gap(6), header('Диаграмма'));
  out.push(buttons([{ text: 'Авто-раскладка', onclick: C.autoLayout, enabled: d.nodes.length > 0 }, { text: 'Заново из БД', onclick: C.reimport, enabled: !!S.lastDb, danger: true }]));
  if (S.kind === KIND_DATA) {
    out.push(choice('Концы связей', [{ text: 'Стрелка', active: d.arrows, onclick: () => C.setArrows(true) }, { text: 'Лапка', active: !d.arrows, onclick: () => C.setArrows(false) }]));
  } else out.push(buttons([{ text: 'Построить по таблицам', onclick: C.rebuildInfoDialog, enabled: DATA().nodes.length > 0 }]));
  out.push(gap(6), header('База данных'));
  out.push(choice('Тест. данные', [{ text: `${S.meta.sample_rows} записей`, active: p.fill_sample, onclick: () => p.fill_sample || C.toggleSample() }, { text: 'Нет', active: !p.fill_sample, onclick: () => p.fill_sample && C.toggleSample() }]));
  out.push(choice('Защита БД', [{ text: 'Триггеры', active: C.guardOn(), onclick: () => C.guardOn() || C.toggleGuard() }, { text: 'Нет', active: !C.guardOn(), onclick: () => C.guardOn() && C.toggleGuard() }]));
  out.push(note('Триггеры отклоняют в текстовых столбцах значения с сочетаниями признаков SQL-инъекции (кавычка + or + «=», union select, ; drop table, admin\'-- и т. п.); обычный текст проходит. Это последний рубеж: защищайтесь параметризованными запросами.', 'small'));
  const has = DATA().nodes.length > 0;
  out.push(buttons([{ text: 'Скачать БД (.db)', onclick: C.downloadDb, enabled: has }, { text: 'БД и модели', onclick: C.downloadBundle, enabled: has }]));
  out.push(note('База строится по даталогической диаграмме: таблицы, типы, PK, FK, UNIQUE, ON DELETE. «БД и модели» — ZIP с базой, обеими диаграммами в PNG и отчётом о нормальной форме.', 'small'));
  out.push(gap(10), header('Как редактировать', true));
  ['Кликните сущность, связь, легенду или надпись — справа появятся их свойства.',
    'Двойной клик по тексту — правка прямо на холсте.',
    'Кнопка «Связь»: нажмите на подчинённую («много») и протяните стрелку к главной.',
    'Выделенная связь: кружки на концах — переставить, квадрат — сдвинуть излом.',
    'F1 — краткая справка по клавишам, «О программе» — полное руководство.'].forEach((t) => out.push(note(`• ${t}`)));
  return out;
}

// ── панель узла ──
function nodePanel(n) {
  const d = D(), nid = n.id, s = S.sel, data = d.kind === KIND_DATA, out = [];
  out.push(header(data ? 'Таблица' : 'Сущность'));
  out.push(field('Название', `p.title:${nid}`, n.title, (v) => C.nSet(nid, { title: v })));
  out.push(h('div', { class: 'p-row' }, h('span', { class: 'p-label' }, 'Цвет'),
    h('div', { class: 'swatches' }, PALETTE.map(([head], i) => h('button', { class: 'swatch' + (n.color === i ? ' on' : ''), style: `background:${head}`, title: `Цвет ${i + 1}`, onclick: () => C.nSet(nid, { color: i }) })))));
  if (data) {
    out.push(field('UNIQUE', `p.uniq:${nid}`, formatUniques(n.uniques), (v) => C.setUniques(nid, v)));
    out.push(note('Составной UNIQUE: столбцы через запятую, группы через «;». Одиночный — кнопка UNIQUE у поля.', 'small'));
    out.push(field('Зависимости', `p.fds:${nid}`, formatFds(n.fds), (v) => C.setFds(nid, v)));
    out.push(note('Функциональные зависимости между столбцами для проверки 2НФ / 3НФ: «левая -> правая», группы через «;». Например: город -> страна.', 'small'));
    out.push(buttons([{ text: 'Найти по данным БД', onclick: () => C.suggestFds(nid), enabled: !!S.lastDb }]));
  }
  out.push(buttons([{ text: 'Дублировать', onclick: C.duplicateNode }, { text: 'Удалить сущность', onclick: C.deleteNodeWhole, danger: true }]));
  out.push(gap(4), header(data ? 'Поле' : 'Атрибут'));
  const row = s.row !== null && n.rows[s.row] ? n.rows[s.row] : null;
  if (!row) out.push(note('Кликните строку на холсте, чтобы править поле. Кнопка ниже добавляет новое.'));
  else {
    const i = s.row;
    out.push(field('Имя', `p.row.name:${nid}`, row.name, (v) => C.rSet(nid, i, { name: v })));
    if (!data) {
      out.push(choice('Маркер', [['#', '# UID'], ['*', '* обяз.'], ['o', 'o необяз.']].map(([m, t]) => ({ text: t, active: row.mark === m, onclick: () => C.rSet(nid, i, { mark: m }) }))));
    } else {
      const toks = keyTokens(row.key);
      out.push(choice('Ключ', [['PK', 'PK'], ['FK', 'FK'], ['U', 'UNIQUE']].map(([k, t]) => ({ text: t, active: toks.includes(k), onclick: () => C.toggleKey(nid, i, k) }))));
      out.push(field('Тип', `p.row.type:${nid}`, row.type, (v) => C.rSet(nid, i, { type: v })));
      out.push(field('DEFAULT', `p.row.default:${nid}`, row.default || '', (v) => C.rSet(nid, i, { default: v })));
      out.push(choice('Быстро', QUICK_TYPES.map(([short, tp]) => ({ text: short, active: row.type.toUpperCase() === tp, onclick: () => C.rSet(nid, i, { type: tp }) }))));
      out.push(choice('NOT NULL', [{ text: 'Да', active: row.notnull, onclick: () => C.rSet(nid, i, { notnull: true }) }, { text: 'Нет', active: !row.notnull, onclick: () => C.rSet(nid, i, { notnull: false }) }]));
    }
  }
  out.push(buttons([{ text: '+ Поле', onclick: () => { C.addRow(); S.focusFid = `p.row.name:${nid}`; } },
    { text: '↑', onclick: () => C.moveRow(-1), enabled: !!row }, { text: '↓', onclick: () => C.moveRow(1), enabled: !!row },
    { text: '− Поле', onclick: C.deleteSelection, enabled: !!row, danger: true }]));
  const links = linksOf(d, nid);
  if (links.length) {
    out.push(gap(6), header('Связи узла', true));
    for (const l of links) {
      const other = nodeById(d, l.a === nid ? l.b : l.a);
      out.push(h('button', { class: 'btn small wide', onclick: () => C.setSel('link', l.id) }, `${l.a === nid ? '→' : '←'} ${other ? other.title : '?'}`));
    }
  }
  if (data) out.push(...nfPanel(nid));
  return out;
}

// ── панель связи ──
function linkPanel(l) {
  const d = D(), lid = l.id, a = nodeById(d, l.a), b = nodeById(d, l.b), out = [];
  out.push(header('Связь'), note(`${a ? a.title : '?'}  →  ${b ? b.title : '?'}`, 'big'));
  if (d.kind === KIND_INFO) {
    out.push(field('Имя у A', `p.na:${lid}`, l.name_a, (v) => C.lSet(lid, { name_a: v })));
    out.push(field('Имя у B', `p.nb:${lid}`, l.name_b, (v) => C.lSet(lid, { name_b: v })));
  } else {
    const rule = onDeleteRule(l.label);
    const rules = ON_DELETE_RULES.map((r) => ({ text: r, active: rule === r, onclick: () => C.lSet(lid, { label: r }) }));
    out.push(choice('ON DELETE', rules.slice(0, 2)), choice('', rules.slice(2)));
    out.push(note('CASCADE — удалить и дочерние записи; RESTRICT / NO ACTION — запретить удаление родителя; SET NULL — записать NULL в ключ; SET DEFAULT — записать DEFAULT столбца.', 'small'));
    const hint = ruleHint(a, l);
    if (hint) out.push(note('⚠ ' + hint, 'small bad'));
    out.push(field('Подпись', `p.label:${lid}`, l.label, (v) => C.lSet(lid, { label: v })));
  }
  out.push(note('A — подчинённая сторона (где «лапка» / внешний ключ), B — главная.', 'small'));
  const two = (label, key, yes, no, val) => choice(label, [{ text: yes[0], active: val === yes[1], onclick: () => C.lSet(lid, { [key]: yes[1] }) }, { text: no[0], active: val === no[1], onclick: () => C.lSet(lid, { [key]: no[1] }) }]);
  out.push(two('Кратность A', 'card_a', ['Один', '1'], ['Много (лапка)', 'M'], l.card_a));
  out.push(two('Кратность B', 'card_b', ['Один', '1'], ['Много (лапка)', 'M'], l.card_b));
  out.push(two('Половина у A', 'must_a', ['Сплошная', true], ['Пунктир', false], l.must_a));
  out.push(two('Половина у B', 'must_b', ['Сплошная', true], ['Пунктир', false], l.must_b));
  if (d.kind === KIND_INFO) {
    out.push(two('Черта UID у A', 'bar_a', ['Есть', true], ['Нет', false], l.bar_a));
    out.push(two('Черта UID у B', 'bar_b', ['Есть', true], ['Нет', false], l.bar_b));
  }
  out.push(gap(4), buttons([{ text: 'Поменять A↔B', onclick: () => C.swapLink(lid) }, { text: 'Авто-маршрут', onclick: () => C.rerouteLink(lid) }]));
  out.push(buttons([{ text: 'Удалить связь', onclick: C.deleteSelection, danger: true }]));
  out.push(note('На холсте: кружки — переставить конец, квадрат — сдвинуть излом, подписи тянутся мышью.', 'small'));
  return out;
}

// ── панель надписи и легенды ──
function notePanel(nt) {
  return [header('Надпись'), field('Текст', `p.note:${nt.id}`, nt.text, (v) => C.noteSet(nt.id, { text: v })),
    field('Размер', `p.nsize:${nt.id}`, String(nt.size), (v) => C.noteSet(nt.id, { size: Math.max(6, Math.min(80, parseInt(v, 10) || nt.size)) }), { type: 'number' }),
    buttons([{ text: 'Удалить надпись', onclick: C.deleteSelection, danger: true }])];
}
function legendPanel() {
  const d = D(), lg = d.legend, s = S.sel, out = [header('Легенда')];
  out.push(field('Заголовок', 'p.legend.title', lg.title, (v) => C.legendSet({ title: v })));
  const i = s.row, it = i !== null ? lg.items[i] : null;
  if (!it) out.push(note('Кликните пункт на холсте, чтобы править его. Двойной клик — правка текста.'));
  else {
    const kinds = LEGEND_KINDS_ORDER();
    const next = kinds[(kinds.indexOf(it.kind) + 1) % kinds.length] || kinds[0];
    out.push(h('div', { class: 'p-row' }, h('span', { class: 'p-label' }, 'Значок'),
      h('button', { class: 'btn small grow', title: 'Сменить вид значка', onclick: () => C.itemSet(i, { kind: next }) }, `${S.meta.legend_kinds[it.kind] || it.kind}  ›`)));
    out.push(field('Текст', `p.item.text:${i}`, it.text, (v) => C.itemSet(i, { text: v })));
  }
  out.push(buttons([{ text: '+ Пункт', onclick: () => { C.addLegendItem(); S.focusFid = `p.item.text:${D().legend.items.length - 1}`; } },
    { text: '↑', onclick: () => C.moveItem(-1), enabled: i !== null }, { text: '↓', onclick: () => C.moveItem(1), enabled: i !== null },
    { text: 'Удалить', onclick: C.deleteSelection, enabled: i !== null, danger: true }]));
  out.push(buttons([{ text: 'Сбросить легенду', onclick: C.resetLegend }]));
  out.push(note('Ширина легенды — перетаскиванием правого края; положение — перетаскиванием.', 'small'));
  return out;
}

export function renderPanel(el) {
  const s = S.sel, d = D();
  let items;
  if (s.kind === 'node' && nodeById(d, s.id)) items = nodePanel(nodeById(d, s.id));
  else if (s.kind === 'link' && linkById(d, s.id)) items = linkPanel(linkById(d, s.id));
  else if (s.kind === 'legend') items = legendPanel();
  else if (s.kind === 'note' && noteById(d, s.id)) items = notePanel(noteById(d, s.id));
  else items = diagramPanel();
  const key = `${S.kind}|${s.kind}|${s.id}`;
  const top = el.dataset.key === key ? el.scrollTop : 0;
  el.replaceChildren(...items);
  el.dataset.key = key;
  el.scrollTop = top;
  if (S.focusFid) {
    const inp = el.querySelector(`[data-fid="${CSS.escape(S.focusFid)}"]`);
    S.focusFid = null;
    if (inp) { inp.focus(); inp.select(); }
  }
}

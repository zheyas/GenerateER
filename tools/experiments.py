#!/usr/bin/env python3
"""Воспроизводимые измерения для описания методики: проверка нормальных форм и поиск зависимостей.

Запуск (печатает таблицы результатов; семена генератора фиксированы)::

    python tools/experiments.py

Опыт 1 (объявлены все ключи) и опыт 2 (объявлен только первичный ключ, остальные программа выводит из зависимостей) сверяют вердикт :func:`er_editor.normalform.check` с независимым полным перебором
по учебным определениям 2НФ, 3НФ и НФБК. Опыт 3 проверяет :func:`er_editor.fdmine.discover` на
таблицах, в которые зависимости заложены заранее. Опыт 4 внедряет в чистые схемы дефекты с известной
по построению разметкой и измеряет, находит ли их проверка (при записанных зависимостях и без них, при привычных
и непривычных названиях столбцов). Опыт 5 измеряет время проверки и поиска зависимостей на схемах разного размера.
Опыт 6 проверяет круг «схема → описание → схема» для SQL и короткой записи. Опыт 7 измеряет ложные срабатывания
защитных триггеров на посторонних текстах и долю отклонённых нагрузок по семействам.
"""
from __future__ import annotations

import copy
import os
import random
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Callable, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from er_editor import dbgen, describe, fdmine, normalform, schema  # noqa: E402
from er_editor.model import KIND_DATA, Diagram, Node, Row  # noqa: E402
from er_editor.schema import FK, Col, Table  # noqa: E402

COLS = "abcdefg"
SEED_NORMAL_FORMS = 20260101
SEED_FD_MINING = 20260102
Fds = list[tuple[frozenset[str], frozenset[str]]]


def attr_closure(attrs: Iterable[str], fds: Fds) -> set[str]:
    """Замыкание множества атрибутов: повтор до неподвижной точки (независимо от ``normalform.closure``).

    Args:
        attrs (Iterable[str]): исходные атрибуты.
        fds (Fds): зависимости ``(левая часть, правая часть)``.

    Returns:
        set[str]: все атрибуты, определяемые из ``attrs``.
    """
    out = set(attrs)
    while True:
        new = {c for lhs, rhs in fds if lhs <= out for c in rhs} - out
        if not new:
            return out
        out |= new


def candidate_keys(cols: list[str], fds: Fds) -> list[set[str]]:
    """Все минимальные надключи полным перебором подмножеств.

    Args:
        cols (list[str]): атрибуты таблицы.
        fds (Fds): зависимости.

    Returns:
        list[set[str]]: ключи-кандидаты (по возрастанию размера).
    """
    keys: list[set[str]] = []
    for size in range(1, len(cols) + 1):
        for sub in combinations(cols, size):
            s = set(sub)
            if attr_closure(s, fds) >= set(cols) and not any(k <= s for k in keys):
                keys.append(s)
    return keys


def textbook_level(cols: list[str], fds: Fds) -> int:
    """Высшая форма (1 — 1НФ … 4 — НФБК) по учебным определениям: частичная зависимость, транзитивная, левая часть не ключ.

    Args:
        cols (list[str]): атрибуты таблицы.
        fds (Fds): зависимости.

    Returns:
        int: номер высшей формы, которой удовлетворяет таблица (1НФ предполагается выполненной).
    """
    keys = candidate_keys(cols, fds)
    prime = set().union(*keys)

    def superkey(x: Iterable[str]) -> bool:
        """Определяет ли ``x`` все атрибуты таблицы.

        Args:
            x (Iterable[str]): атрибуты.

        Returns:
            bool: ``True``, если замыкание ``x`` — вся таблица.
        """
        return attr_closure(x, fds) >= set(cols)

    for k in keys:
        for size in range(1, len(k)):
            for sub in combinations(sorted(k), size):
                if (attr_closure(sub, fds) - set(sub)) - prime:
                    return 1
    if any(not superkey(lhs) and (rhs - lhs) - prime for lhs, rhs in fds):
        return 2
    if any(not superkey(lhs) and rhs - lhs for lhs, rhs in fds):
        return 3
    return 4


def build_table(cols: list[str], fds: Fds, keys: list[set[str]]) -> Diagram:
    """Диаграмма из одной таблицы: первый ключ — PK, остальные — UNIQUE.

    Args:
        cols (list[str]): имена столбцов.
        fds (Fds): записанные зависимости.
        keys (list[set[str]]): объявляемые ключи (не пустой список).

    Returns:
        Diagram: даталогическая диаграмма для :func:`normalform.check`.
    """
    d = Diagram(KIND_DATA)
    rows = [Row(name=c, type="INTEGER", key="PK" if c in keys[0] else "", notnull=False) for c in cols]
    d.nodes.append(Node(d.new_id("n"), "t", rows=rows, uniques=[sorted(k) for k in keys[1:]],
                        fds=[f"{', '.join(sorted(lhs))} -> {', '.join(sorted(rhs))}" for lhs, rhs in fds]))
    return d


def random_fds(rnd: random.Random) -> tuple[list[str], Fds]:
    """Случайная таблица из 3–6 атрибутов и до четырёх зависимостей с левой частью из 1–2 атрибутов.

    Args:
        rnd (random.Random): генератор случайных чисел.

    Returns:
        tuple[list[str], Fds]: атрибуты и зависимости.
    """
    cols = list(COLS[:rnd.randint(3, 6)])
    fds: Fds = []
    for _ in range(rnd.randint(0, 4)):
        lhs = set(rnd.sample(cols, rnd.randint(1, 2)))
        rest = [c for c in cols if c not in lhs]
        if rest:
            fds.append((frozenset(lhs), frozenset(rnd.sample(rest, rnd.randint(1, min(2, len(rest)))))))
    return cols, fds


def verdict_experiment(declare_all_keys: bool, count: int = 1500) -> dict[str, object]:
    """Сверяет достигнутую форму программы с перебором на случайных таблицах.

    Args:
        declare_all_keys (bool): ``True`` — в схеме объявлены все ключи-кандидаты (PK и UNIQUE);
            ``False`` — только первый (как если бы человек не заметил остальные).
        count (int): число таблиц.

    Returns:
        dict[str, object]: ``schemas``, ``mismatches``, ``multi_key`` (таблиц с несколькими ключами),
        ``by_level`` (распределение эталона), ``kinds`` (расхождения ``(эталон, программа) -> число``), ``seconds``.
    """
    rnd = random.Random(SEED_NORMAL_FORMS)
    by_level = {1: 0, 2: 0, 3: 0, 4: 0}
    kinds: dict[tuple[int, int], int] = {}
    multi = diff = 0
    t0 = time.perf_counter()
    for _ in range(count):
        cols, fds = random_fds(rnd)
        keys = candidate_keys(cols, fds)
        want = textbook_level(cols, fds)
        by_level[want] += 1
        multi += len(keys) > 1
        got = normalform.check(build_table(cols, fds, keys if declare_all_keys else keys[:1]), 4).achieved
        if got != want:
            diff += 1
            kinds[(want, got)] = kinds.get((want, got), 0) + 1
    return {"schemas": count, "mismatches": diff, "multi_key": multi, "by_level": by_level, "kinds": kinds,
            "seconds": round(time.perf_counter() - t0, 2)}


def planted_table(rnd: random.Random, rows: int) -> tuple[list[str], list[tuple[int, ...]], list[tuple[int, int]]]:
    """Таблица с заложенными зависимостями: 2–3 независимых столбца и 3–5 столбцов-функций от одного независимого.

    Args:
        rnd (random.Random): генератор.
        rows (int): число строк.

    Returns:
        tuple[list[str], list[tuple[int, ...]], list[tuple[int, int]]]: имена столбцов, строки (первый элемент — номер строки)
        и заложенные зависимости ``(номер левого столбца, номер правого)``.
    """
    k = rnd.randint(5, 7)
    cols = [f"c{i}" for i in range(k)]
    base = rnd.sample(range(k), rnd.randint(2, 3))
    dom = {i: rnd.randint(4, 12) for i in base}
    maps: dict[int, tuple[int, dict[int, int]]] = {}
    planted: list[tuple[int, int]] = []
    for i in range(k):
        if i not in base:
            src = rnd.choice(base)
            maps[i] = (src, {v: rnd.randint(0, rnd.randint(2, 6)) for v in range(dom[src])})
            planted.append((src, i))
    data: list[tuple[int, ...]] = []
    for r in range(rows):
        v = {i: rnd.randrange(dom[i]) for i in base}
        for i, (src, m) in maps.items():
            v[i] = m[v[src]]
        data.append((r,) + tuple(v[i] for i in range(k)))
    return cols, data, planted


def mining_experiment(rows: int, tables: int = 100) -> dict[str, int]:
    """Проверяет поиск зависимостей по данным на таблицах с заложенными зависимостями.

    Args:
        rows (int): число строк в каждой таблице.
        tables (int): число таблиц.

    Returns:
        dict[str, int]: ``planted`` (заложено), ``found`` (из них найдено), ``missed``, ``extra`` (найдено сверх заложенного:
        такие зависимости выполняются в данных, но не заданы генератором), ``tables``.
    """
    rnd = random.Random(SEED_FD_MINING)
    found_n = missed = extra = 0
    for _ in range(tables):
        cols, data, planted = planted_table(rnd, rows)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.db")
            con = sqlite3.connect(path)
            con.execute(f"create table t(id integer primary key, {', '.join(cols)})")
            con.executemany(f"insert into t values(?{',?' * len(cols)})", data)
            con.commit()
            con.close()
            res = fdmine.discover(path)[0]
        got = {(f.lhs, r) for f in res.fds for r in f.rhs}
        want = {((cols[s],), cols[i]) for s, i in planted}
        found_n += len(want & got)
        missed += len(want - got)
        extra += len(got - want)
    return {"tables": tables, "planted": found_n + missed, "found": found_n, "missed": missed, "extra": extra}


# ───────────────────────── опыт 4: мутации с известной разметкой ─────────────────────────

@dataclass
class Base:
    """Чистая учебная схема и места, куда внедряются дефекты.

    Attributes:
        name (str): название схемы.
        text (str): описание в формате программы.
        junction (str): таблица-связка с составным ключом.
        junction_parent (str): родитель, на которого ссылается связка (в нём лежит копируемый атрибут).
        child (str): таблица с внешним ключом на ``child_parent`` (не входящим в ключ).
        child_parent (str): родитель таблицы ``child``.
        entity (str): обычная таблица для остальных дефектов.
        copy_junction (tuple[str, str]): название копии атрибута родителя в связке: привычное и непривычное.
        copy_child (tuple[str, str]): то же для таблицы ``child``.
    """

    name: str
    text: str
    junction: str
    junction_parent: str
    child: str
    child_parent: str
    entity: str
    copy_junction: tuple[str, str]
    copy_child: tuple[str, str]


BASES = [
    Base("shop_en", "customers: name, email!\nproducts: title, price:real\norders: created:date\ncustomers 1:N orders\norders N:M products через order_items: qty:int\n",
         "order_items", "products", "orders", "customers", "customers", ("products_title", "prd_ttl"), ("customers_name", "cust_nm")),
    Base("uni_en", "students: name, email!\ncourses: title, credits:int\nteachers: name\nteachers 1:N courses\nstudents N:M courses через enrollments: grade:int\n",
         "enrollments", "courses", "courses", "teachers", "students", ("courses_title", "crs_ttl"), ("teachers_name", "tchr_nm")),
    Base("lib_en", "authors: name\nbooks: title, year:int\nmembers: name, email!\nauthors 1:N books\nmembers N:M books через loans: due:date\n",
         "loans", "books", "books", "authors", "members", ("books_title", "bk_ttl"), ("authors_name", "auth_nm")),
    Base("shop_ru", "Клиент: имя, email!\nТовар: название, цена:вещественное\nЗаказ: дата:дата\nКлиент 1:N Заказ\nЗаказ N:M Товар через Позиция: количество:целое\n",
         "позиция", "товар", "заказ", "клиент", "клиент", ("товар_название", "тов_назв"), ("клиент_имя", "кл_им")),
    Base("clinic_ru", "Отделение: название\nВрач: фамилия\nПациент: фамилия, телефон?\nОтделение 1:N Врач\nПациент N:M Врач через Приём: дата:дата\n",
         "приём", "врач", "врач", "отделение", "пациент", ("врач_фамилия", "вр_фам"), ("отделение_название", "отд_назв")),
    Base("hr_en", "departments: name\nemployees: name, salary:real\nprojects: title\ndepartments 1:N employees\nemployees N:M projects через assignments: hours:int\n",
         "assignments", "projects", "employees", "departments", "employees", ("projects_title", "prj_ttl"), ("departments_name", "dept_nm")),
]
# вид дефекта -> (какая форма нарушена, нужна ли запись зависимости для обнаружения)
MUTATIONS: dict[str, tuple[int, bool]] = {
    "copy_key": (2, True), "copy_nonkey": (3, True), "id_name_pair": (3, True), "hierarchy": (3, True), "repeating_group": (1, False),
    "list_column": (1, False), "partial_fd": (2, True), "no_pk": (1, False), "bcnf_fd": (4, True),
}
# безобидные столбцы: по три набора на таблицы схемы (разные имена у родителя, потомка и связки) и один общий набор
BENIGN_SETS = [[("comment", "TEXT"), ("note", "TEXT"), ("created_at", "DATE")], [("updated_at", "DATE"), ("status", "TEXT"), ("total", "REAL")],
               [("quantity", "INTEGER"), ("rating", "INTEGER"), ("remark", "TEXT")]]
BENIGN_SHARED = [("total", "REAL"), ("quantity", "INTEGER"), ("rating", "INTEGER")]
NAMING_FREE = ("no_pk", "partial_fd", "bcnf_fd")       # виды дефектов, в которых названия не влияют на результат


def _table(tables: list[Table], name: str) -> Table:
    """Находит таблицу по имени.

    Args:
        tables (list[Table]): таблицы схемы.
        name (str): имя таблицы.

    Returns:
        Table: найденная таблица.
    """
    return next(t for t in tables if t.name == name)


def _fk_col(t: Table, parent: str) -> str:
    """Столбец внешнего ключа таблицы на указанного родителя.

    Args:
        t (Table): дочерняя таблица.
        parent (str): имя родителя.

    Returns:
        str: имя столбца.
    """
    return next(fk.cols[0] for fk in t.fks if fk.ref_table == parent)


def mutate(base: Base, kind: str, naming: str) -> tuple[list[Table], dict[str, list[str]]]:
    """Внедряет в чистую схему один дефект.

    Args:
        base (Base): чистая схема.
        kind (str): вид дефекта (ключ :data:`MUTATIONS`).
        naming (str): ``"conv"`` — привычные названия столбцов, ``"unconv"`` — непривычные (проверка их не знает).

    Returns:
        tuple[list[Table], dict[str, list[str]]]: схема с дефектом и зависимости, которые разработчик мог бы записать
        (``{таблица: [записи]}``); для дефектов без зависимостей словарь пуст.

    Raises:
        ValueError: если вид дефекта неизвестен.
    """
    tables = describe.describe(base.text).tables
    conv = naming == "conv"
    fds: dict[str, list[str]] = {}

    def add(t: Table, name: str, typ: str = "TEXT") -> None:
        """Добавляет обычный столбец.

        Args:
            t (Table): таблица.
            name (str): имя столбца.
            typ (str): тип.
        """
        t.cols.append(Col(name, typ, False, 0))

    if kind == "copy_key":
        t = _table(tables, base.junction)
        name = base.copy_junction[0 if conv else 1]
        add(t, name)
        fds[t.name] = [f"{_fk_col(t, base.junction_parent)} -> {name}"]
    elif kind == "copy_nonkey":
        t = _table(tables, base.child)
        name = base.copy_child[0 if conv else 1]
        add(t, name)
        fds[t.name] = [f"{_fk_col(t, base.child_parent)} -> {name}"]
    elif kind == "id_name_pair":
        t = _table(tables, base.entity)
        a, b = ("status_id", "status_name") if conv else ("st_code", "st_label")
        add(t, a, "INTEGER")
        add(t, b)
        fds[t.name] = [f"{a} -> {b}"]
    elif kind == "hierarchy":
        t = _table(tables, base.entity)
        a, b = ("city", "country") if conv else ("town", "nation")
        add(t, a)
        add(t, b)
        fds[t.name] = [f"{a} -> {b}"]
    elif kind == "repeating_group":
        t = _table(tables, base.entity)
        for n in (("phone1", "phone2") if conv else ("phone_home", "phone_work")):
            add(t, n)
    elif kind == "list_column":
        t = _table(tables, base.entity)
        add(t, "tags", "JSON") if conv else add(t, "keywords_csv")
    elif kind == "partial_fd":
        t = _table(tables, base.junction)
        add(t, "zone")
        fds[t.name] = [f"{_fk_col(t, base.junction_parent)} -> zone"]
    elif kind == "no_pk":
        t = _table(tables, base.entity)
        t.pk = []
        for c in t.cols:
            c.pk = 0
    elif kind == "bcnf_fd":
        t = _table(tables, base.junction)
        add(t, "zone")
        fds[t.name] = [f"zone -> {_fk_col(t, base.junction_parent)}"]
    else:
        raise ValueError(kind)
    return tables, fds


def check_tables(tables: list[Table], fds: Optional[dict[str, list[str]]] = None) -> normalform.Report:
    """Проверяет схему на все нормальные формы, при необходимости записав зависимости.

    Args:
        tables (list[Table]): схема.
        fds (Optional[dict[str, list[str]]]): записанные зависимости ``{таблица: [записи]}``.

    Returns:
        normalform.Report: отчёт.
    """
    d = schema.build_data(tables)
    for n in d.nodes:
        n.fds = list((fds or {}).get(n.title, []))
    return normalform.check(d, 4)


def mutation_experiment() -> dict[str, object]:
    """Опыт 4: находит ли проверка внедрённые дефекты и не ругается ли на чистые схемы.

    Дефект считается **найденным**, если есть нарушение (error) на ожидаемом уровне, **заподозренным** — если там только
    подозрение (warn), иначе **пропущенным**. Показатель «форма верна» — достигнутая форма совпала с ожидаемой по построению.

    Returns:
        dict[str, object]: ``rows`` — список ``{kind, naming, fds, n, found, suspected, missed, level_ok}``;
        ``clean`` — ``{schemas, errors, warnings}`` для чистых схем; ``benign`` — то же для чистых схем с безобидными
        дополнительными столбцами с разными именами; ``benign_shared_names`` — когда во все три таблицы добавлены одни и те же имена.
    """
    rows: list[dict[str, object]] = []
    for kind, (level, needs_fd) in MUTATIONS.items():
        for naming in ("conv", "unconv"):
            if naming == "unconv" and kind in NAMING_FREE:
                continue
            for with_fd in ((False, True) if needs_fd else (False,)):
                found = suspected = missed = ok = 0
                for base in BASES:
                    tables, fds = mutate(base, kind, naming)
                    rep = check_tables(tables, fds if with_fd else None)
                    at = [i for i in rep.issues if i.level == level]
                    if any(i.severity == normalform.ERROR for i in at):
                        found += 1
                    elif at:
                        suspected += 1
                    else:
                        missed += 1
                    expected = level - 1
                    ok += rep.achieved == expected
                rows.append({"kind": kind, "naming": naming, "fds": with_fd, "n": len(BASES), "found": found, "suspected": suspected,
                             "missed": missed, "level_ok": ok})
    errors = warnings = 0
    for base in BASES:
        rep = check_tables(describe.describe(base.text).tables)
        errors += sum(i.severity == normalform.ERROR for i in rep.issues)
        warnings += sum(i.severity == normalform.WARN for i in rep.issues)
    def benign(shared: bool) -> dict[str, int]:
        """Чистые схемы с безобидными дополнительными столбцами.

        Args:
            shared (bool): ``True`` — во все три таблицы добавлены одни и те же столбцы; ``False`` — разные.

        Returns:
            dict[str, int]: ``schemas``, ``errors``, ``warnings``.
        """
        err = warn = 0
        for base in BASES:
            tables = describe.describe(base.text).tables
            for t_name, cols_set in zip((base.entity, base.child, base.junction), [BENIGN_SHARED] * 3 if shared else BENIGN_SETS):
                for cname, typ in cols_set:
                    if not any(c.name == cname for c in _table(tables, t_name).cols):
                        _table(tables, t_name).cols.append(Col(cname, typ, False, 0))
            rep = check_tables(tables)
            err += sum(i.severity == normalform.ERROR for i in rep.issues)
            warn += sum(i.severity == normalform.WARN for i in rep.issues)
        return {"schemas": len(BASES), "errors": err, "warnings": warn}

    return {"rows": rows, "clean": {"schemas": len(BASES), "errors": errors, "warnings": warnings},
            "benign": benign(False), "benign_shared_names": benign(True)}


# ───────────────────────── опыт 5: время ─────────────────────────

def synthetic_tables(n_tables: int, n_cols: int, rnd: random.Random) -> list[Table]:
    """Случайная схема: у каждой таблицы ключ id, внешние ключи на более ранние таблицы и обычные столбцы.

    Args:
        n_tables (int): число таблиц.
        n_cols (int): число столбцов в каждой таблице.
        rnd (random.Random): генератор.

    Returns:
        list[Table]: схема.
    """
    tables: list[Table] = []
    for i in range(n_tables):
        cols = [Col("id", "INTEGER", True, 1)]
        fks: list[FK] = []
        for j in range(1, n_cols):
            if i and rnd.random() < 0.2:
                parent = rnd.choice(tables)
                cols.append(Col(f"{parent.name}_id{j}", "INTEGER", False, 0))
                fks.append(FK([cols[-1].name], parent.name, ["id"], "CASCADE"))
            else:
                cols.append(Col(f"col{j}", rnd.choice(["TEXT", "INTEGER", "REAL", "DATE"]), False, 0))
        tables.append(Table(f"t{i}", cols, pk=["id"], fks=fks))
    return tables


def _median_seconds(fn: Callable[[], object], repeats: int = 5) -> float:
    """Медиана времени вызова.

    Args:
        fn (Callable[[], object]): измеряемый вызов.
        repeats (int): число повторов.

    Returns:
        float: секунды.
    """
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times)


def timing_experiment() -> dict[str, list[dict[str, object]]]:
    """Опыт 5: время проверки нормальных форм и поиска зависимостей на схемах разного размера.

    Returns:
        dict[str, list[dict[str, object]]]: ``check`` — ``{tables, cols, fds, seconds}``; ``mining`` — ``{rows, cols, seconds}``.
    """
    rnd = random.Random(20260103)
    check_rows: list[dict[str, object]] = []
    for n_tables, n_cols in ((10, 10), (50, 10), (100, 20), (200, 20), (200, 40)):
        tables = synthetic_tables(n_tables, n_cols, rnd)
        d = schema.build_data(tables)
        sec = _median_seconds(lambda: normalform.check(d, 4))
        for node in d.nodes:
            node.fds = [f"col1 -> col2", f"col3 -> col4"]
        sec_fd = _median_seconds(lambda: normalform.check(d, 4))
        check_rows.append({"tables": n_tables, "cols": n_cols, "seconds": round(sec, 4), "seconds_with_fds": round(sec_fd, 4)})
    mining: list[dict[str, object]] = []
    for rows, cols in ((1000, 5), (1000, 10), (5000, 10), (5000, 20)):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.db")
            con = sqlite3.connect(path)
            names = [f"c{i}" for i in range(cols)]
            con.execute(f"create table t(id integer primary key, {', '.join(names)})")
            con.executemany(f"insert into t values(?{',?' * cols})", [(r,) + tuple(rnd.randrange(30) for _ in names) for r in range(rows)])
            con.commit()
            con.close()
            mining.append({"rows": rows, "cols": cols, "seconds": round(_median_seconds(lambda: fdmine.discover(path), 3), 3)})
    return {"check": check_rows, "mining": mining}


# ───────────────────────── опыт 6: круг «схема → описание → схема» ─────────────────────────

SQL_TYPES = ("INTEGER", "TEXT", "REAL", "DATE")


def roundtrip_sql(count: int = 300) -> dict[str, int]:
    """Опыт 6а: случайные схемы → ``CREATE TABLE`` → разбор описания → сравнение структуры.

    Сравниваются имена таблиц и столбцов, типы, ``NOT NULL`` (кроме столбца ``INTEGER PRIMARY KEY``, для которого SQLite не
    сообщает ``NOT NULL``), первичные ключи, ``UNIQUE``, внешние ключи с правилами ``ON DELETE`` и значения ``DEFAULT``.

    Args:
        count (int): число схем.

    Returns:
        dict[str, int]: ``schemas`` и ``mismatches``.
    """
    rnd = random.Random(20260104)
    bad = 0
    for _ in range(count):
        tables: list[Table] = []
        for i in range(rnd.randint(2, 5)):
            cols = [Col("id", "INTEGER", True, 1)]
            fks: list[FK] = []
            uniques: list[list[str]] = []
            for j in range(rnd.randint(1, 4)):
                typ = rnd.choice(SQL_TYPES)
                default = rnd.choice(["", "", "0", "'x'"]) if typ in ("INTEGER", "TEXT") else ""
                cols.append(Col(f"c{j}", typ, rnd.random() < 0.4, 0, default))
                if rnd.random() < 0.2:
                    uniques.append([f"c{j}"])
            if tables and rnd.random() < 0.7:
                parent = rnd.choice(tables)
                cols.append(Col("p_id", "INTEGER", rnd.random() < 0.5, 0))
                fks.append(FK(["p_id"], parent.name, ["id"], rnd.choice(dbgen.ON_DELETE_RULES)))
            tables.append(Table(f"t{i}", cols, pk=["id"], fks=fks, uniques=uniques))
        sql = ";\n".join(dbgen.create_sql(tables)) + ";"
        try:
            back = describe.describe(sql).tables
        except describe.DescribeError:
            bad += 1
            continue
        def sig(ts: list[Table]) -> object:
            """Сравнимое представление структуры.

            Args:
                ts (list[Table]): таблицы.

            Returns:
                object: кортеж для сравнения.
            """
            return sorted((t.name, tuple((c.name, c.type, c.notnull or c.pk > 0, c.default) for c in t.cols), tuple(t.pk),
                           tuple(sorted(tuple(u) for u in t.uniques)),
                           tuple(sorted((tuple(f.cols), f.ref_table, tuple(f.ref_cols), f.on_delete) for f in t.fks))) for t in ts)
        bad += sig(tables) != sig(back)
    return {"schemas": count, "mismatches": bad}


RU_ENTITIES = ["клиент", "заказ", "товар", "склад", "поставщик", "сотрудник", "отдел", "проект", "договор", "счёт"]
RU_ATTRS = ["имя", "фамилия", "город", "адрес", "телефон", "почта", "сумма", "количество", "вес", "цена", "статус", "номер", "описание", "срок"]
RU_TYPES = {"целое": "INTEGER", "вещественное": "REAL", "текст": "TEXT", "дата": "DATE"}


def roundtrip_text(count: int = 300) -> dict[str, int]:
    """Опыт 6б: случайная модель (сущности, атрибуты, связи) → короткая запись → разбор → сравнение с моделью.

    Ожидаемая схема строится по описанию независимо от разбора: у сущности есть ``id`` и заданные атрибуты с
    типом, ``UNIQUE`` (``!``) и обязательностью (``?`` — не обязателен); связь ``A 1:N B`` даёт внешний ключ в ``B``,
    ``A N:1 B`` — в ``A``, ``A N:M B через J`` — таблицу ``J`` с двумя внешними ключами.

    Args:
        count (int): число моделей.

    Returns:
        dict[str, int]: ``models``, ``mismatches``, ``relations``.
    """
    rnd = random.Random(20260105)
    bad = rels = 0
    for _ in range(count):
        ents = rnd.sample(RU_ENTITIES, rnd.randint(2, 6))
        spec: dict[str, list[tuple[str, str, bool, bool]]] = {}
        lines: list[str] = []
        for e in ents:
            attrs = []
            for a in rnd.sample(RU_ATTRS, rnd.randint(1, 4)):
                typ, uniq, nullable = rnd.choice(list(RU_TYPES)), rnd.random() < 0.2, rnd.random() < 0.3
                attrs.append((a, typ, uniq, nullable))
            spec[e] = attrs
            lines.append(f"{e.capitalize()}: " + ", ".join(f"{a}:{t}{'!' if u else ''}{'?' if n else ''}" for a, t, u, n in attrs))
        want_fk: list[tuple[str, str]] = []
        want_tables = set(ents)
        seen: set[frozenset[str]] = set()
        for _ in range(rnd.randint(1, len(ents))):
            a, b = rnd.sample(ents, 2)
            if frozenset((a, b)) in seen:
                continue
            seen.add(frozenset((a, b)))
            kind = rnd.choice(["1:N", "N:1", "N:M"])
            if kind == "1:N":
                lines.append(f"{a.capitalize()} 1:N {b.capitalize()}")
                want_fk.append((b, a))
            elif kind == "N:1":
                lines.append(f"{a.capitalize()} N:1 {b.capitalize()}")
                want_fk.append((a, b))
            else:
                j = f"{a}_{b}_связь"
                lines.append(f"{a.capitalize()} N:M {b.capitalize()} через {j}")
                want_tables.add(j)
                want_fk += [(j, a), (j, b)]
            rels += 1
        try:
            got = {t.name: t for t in describe.describe("\n".join(lines) + "\n").tables}
        except describe.DescribeError:
            bad += 1
            continue
        ok = set(got) == want_tables
        if ok:
            for e, attrs in spec.items():
                cols = {c.name: c for c in got[e].cols}
                for a, typ, uniq, nullable in attrs:
                    c = cols.get(a)
                    ok &= c is not None and c.type == RU_TYPES[typ] and c.notnull == (not nullable) and (any(u == [a] for u in got[e].uniques) == uniq)
            have_fk = sorted((t.name, fk.ref_table) for t in got.values() for fk in t.fks)
            ok &= have_fk == sorted(want_fk)
        bad += not ok
    return {"models": count, "mismatches": bad, "relations": rels}


# ───────────────────────── опыт 7: защитные триггеры ─────────────────────────

FAMILIES: dict[str, list[str]] = {
    "тавтология": ["' OR 1=1", "' OR '1'='1", '" OR ""="', "' OR 'a'='a", "') OR ('1'='1", "' OR 2>1", "' OR 1=1 LIMIT 1", "' OR true", "' OR 1 IS NOT NULL",
                   "admin' OR '1'='1' --", "1 OR 1=1", "1 AND 1=1"],
    "UNION": ["' UNION SELECT 1,2,3", "' UNION ALL SELECT NULL,NULL", "' UNION SELECT name FROM sqlite_master", "1 UNION SELECT username, password FROM users",
              "' UNION SELECT * FROM t", "-1 UNION SELECT 1", "x' union select@@version"],
    "стековые": ["'; DROP TABLE users", "1; DROP TABLE users", "'; DELETE FROM users", "1; UPDATE users SET admin=1", "'; INSERT INTO users VALUES (1)",
                 "'; ATTACH DATABASE 'x' AS y", "'; PRAGMA writable_schema=1", "'; SELECT * FROM users", "1; TRUNCATE TABLE users", "'; EXEC xp_cmdshell 'dir'", "'; SHUTDOWN"],
    "комментарий": ["admin'--", "admin' --", "admin' #", "admin'/*", "' OR 1=1--", "x' -- ", "admin') --"],
    "слепые": ["' AND SLEEP(5)", "1 AND SLEEP(5)", "' AND (SELECT COUNT(*) FROM users)>0", "' AND 1=1", "' AND 1=2",
               "1 AND ASCII(SUBSTR((SELECT name FROM sqlite_master),1,1))>64", "' OR BENCHMARK(1000000,MD5(1))", "' AND randomblob(1000000000)", "1 AND 1=randomblob(500000000)"],
    "по ошибке": ["' AND extractvalue(1,concat(0x7e,version()))", "' AND 1=CAST((SELECT version()) AS INT)", "' OR updatexml(1,concat(0x7e,(select user())),1)",
                  "' AND (SELECT 1 FROM (SELECT COUNT(*),CONCAT(version(),FLOOR(RAND(0)*2))x FROM information_schema.tables GROUP BY x)a)"],
    "схема SQLite": ["x' UNION SELECT sql FROM sqlite_master--", "SELECT load_extension('evil')", "' OR (SELECT sql FROM sqlite_schema) LIKE '%'", "1 OR 1=(SELECT 1 FROM sqlite_master)"],
    "HTML/XSS": ["<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "javascript:alert(1)", '"><svg onload=alert(1)>'],
}
OBFUSCATIONS: dict[str, Callable[[str], str]] = {
    "как есть": lambda p: p,
    "регистр": lambda p: "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(p)),
    "табуляции": lambda p: p.replace(" ", "\t"),
    "переводы строк": lambda p: p.replace(" ", "\n"),
    "много пробелов": lambda p: p.replace(" ", " " * 20),
    "/**/ вместо пробела": lambda p: p.replace(" ", "/**/"),
    "%27 вместо кавычки": lambda p: p.replace("'", "%27"),
}


def _corpus_lines(path: Path, limit: int = 20000) -> list[str]:
    """Читает строки файла длиной не меньше 6 символов без повторов.

    Args:
        path (Path): файл.
        limit (int): наибольшее число строк.

    Returns:
        list[str]: строки текста.
    """
    seen: dict[str, None] = {}
    for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        t = ln.strip()
        if len(t) >= 6:
            seen.setdefault(t, None)
    return list(seen)[:limit]


def trigger_experiment() -> dict[str, object]:
    """Опыт 7: ложные срабатывания защитных триггеров на посторонних текстах и доля отклонённых нагрузок.

    Посторонние тексты: описание языка Python из стандартной библиотеки (``pydoc_data.topics``: английская проза и код
    с кавычками и точками с запятой), руководство ``README.md`` (русский текст и код) и исходники модуля ``sqlite3`` из
    стандартной библиотеки (код, в котором есть настоящие SQL-запросы). Нагрузки составлены авторами по распространённым
    семействам и пропущены через преобразования, ослабляющие простые фильтры.

    Returns:
        dict[str, object]: ``benign`` — ``{корпус: {lines, blocked, examples}}``; ``attacks`` — ``{семейство: {payloads, blocked}}``
        по исходным нагрузкам и ``obfuscation`` — ``{преобразование: {total, blocked}}``; ``misses`` — не отклонённые исходные нагрузки.
    """
    import pydoc_data.topics as topics
    import sqlite3 as sqlite3_pkg
    d = Diagram(KIND_DATA)
    d.nodes.append(Node(d.new_id("n"), "t", rows=[Row("id", "", "INTEGER", "PK", True), Row("v", "", "TEXT")]))
    path = os.path.join(tempfile.mkdtemp(prefix="er_trig_"), "g.db")
    dbgen.create_database(path, dbgen.diagram_to_tables(d), 0, 1, guard=True)
    con = sqlite3.connect(path)

    def blocked(text: str) -> bool:
        """Отклоняет ли триггер значение.

        Args:
            text (str): значение.

        Returns:
            bool: ``True``, если вставка отклонена.
        """
        try:
            con.execute("insert into t(v) values(?)", (text,))
        except sqlite3.IntegrityError:
            return True
        return False

    root = Path(__file__).resolve().parents[1]
    sqlite_dir = Path(sqlite3_pkg.__file__).parent
    corpora = {"Python (pydoc_data.topics)": _corpus_lines(Path(topics.__file__)), "README.md": _corpus_lines(root / "README.md"),
               "sqlite3 (код с SQL)": [ln for f in sorted(sqlite_dir.glob("*.py")) for ln in _corpus_lines(f)]}
    benign: dict[str, dict[str, object]] = {}
    for name, lines in corpora.items():
        hits = [ln for ln in lines if blocked(ln)]
        benign[name] = {"lines": len(lines), "blocked": len(hits), "examples": hits[:5]}
    attacks: dict[str, dict[str, int]] = {}
    misses: list[str] = []
    obf: dict[str, dict[str, int]] = {k: {"total": 0, "blocked": 0} for k in OBFUSCATIONS}
    for fam, payloads in FAMILIES.items():
        n = 0
        for p in payloads:
            ok = blocked(p)
            n += ok
            if not ok:
                misses.append(f"{fam}: {p}")
            for k, fn in OBFUSCATIONS.items():
                obf[k]["total"] += 1
                obf[k]["blocked"] += blocked(fn(p))
        attacks[fam] = {"payloads": len(payloads), "blocked": n}
    return {"benign": benign, "attacks": attacks, "obfuscation": obf, "misses": misses}


def main() -> None:
    """Выполняет все опыты и печатает результаты."""
    for label, flag in (("Опыт 1: объявлены все ключи-кандидаты", True), ("Опыт 2: объявлен только первичный ключ", False)):
        print(label, verdict_experiment(flag))
    for n in (50, 200, 1000):
        print(f"Опыт 3: строк в таблице {n}", mining_experiment(n))
    import json
    for label, result in (("Опыт 4", mutation_experiment()), ("Опыт 5", timing_experiment()), ("Опыт 6а", roundtrip_sql()),
                          ("Опыт 6б", roundtrip_text()), ("Опыт 7", trigger_experiment())):
        print(label, json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

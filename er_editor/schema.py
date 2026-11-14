"""Чтение схемы SQLite и построение начальных диаграмм (инфологической и даталогической).

Схема читается через ``PRAGMA`` в режиме «только чтение» — базу данных программа не меняет.

Правила построения инфологической модели (:func:`build_info`):

* каждая таблица — сущность; внешние ключи не показываются атрибутами, их заменяют связи;
* ``#`` (UID) — столбцы первичного ключа, ``*`` — NOT NULL, ``o`` — остальные;
* суррогатный ключ (одиночный PK и UNIQUE, целиком состоящий из внешних ключей) скрывается:
  идентификатор сущности задают связи — на них ставится черта UID;
* связь 1:1 — если столбцы FK совпадают с PK или с UNIQUE-ограничением, иначе 1:M;
* сторона дочерней таблицы — сплошная, если FK обязателен (NOT NULL), иначе пунктир.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import layout
from .layout import Measure
from .model import KIND_DATA, KIND_INFO, Diagram, Link, Node, Project, Row, default_legend


@dataclass
class Col:
    """Столбец таблицы.

    Attributes:
        name (str): имя столбца.
        type (str): объявленный тип в верхнем регистре (пустая строка, если не задан).
        notnull (bool): признак NOT NULL.
        pk (int): номер столбца в составном первичном ключе (0 — не входит в PK).
        default (str): значение ``DEFAULT`` в виде SQL-литерала (пусто — не задано).
    """

    name: str
    type: str
    notnull: bool
    pk: int
    default: str = ""


@dataclass
class FK:
    """Внешний ключ.

    Attributes:
        cols (list[str]): столбцы дочерней таблицы.
        ref_table (str): родительская таблица.
        ref_cols (list[str]): столбцы родительской таблицы.
        on_delete (str): правило ``ON DELETE`` (``"NO ACTION"``, ``"CASCADE"``, ``"RESTRICT"``…).
    """

    cols: list[str]
    ref_table: str
    ref_cols: list[str]
    on_delete: str


@dataclass
class Table:
    """Таблица БД.

    Attributes:
        name (str): имя таблицы.
        cols (list[Col]): столбцы в порядке объявления.
        pk (list[str]): столбцы первичного ключа по порядку.
        fks (list[FK]): внешние ключи.
        uniques (list[list[str]]): уникальные ограничения (списки имён столбцов).
    """

    name: str
    cols: list[Col]
    pk: list[str] = field(default_factory=list)
    fks: list[FK] = field(default_factory=list)
    uniques: list[list[str]] = field(default_factory=list)


def read_schema(db_path: str) -> list[Table]:
    """Читает структуру БД SQLite.

    Служебные таблицы ``sqlite_*`` пропускаются. Внешний ключ без явных родительских
    столбцов получает столбцы первичного ключа родителя.

    Args:
        db_path (str): путь к файлу базы данных (открывается только для чтения).

    Returns:
        list[Table]: таблицы в порядке создания.

    Raises:
        sqlite3.Error: если файл не найден, недоступен или не является базой данных SQLite.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]
        tables: dict[str, Table] = {}
        for name in names:
            cols = [Col(r[1], (r[2] or "").upper(), bool(r[3]), r[5], str(r[4]) if r[4] is not None else "")
                    for r in conn.execute(f"PRAGMA table_info('{name}')")]
            t = Table(name, cols, pk=[c.name for c in sorted((c for c in cols if c.pk), key=lambda c: c.pk)])
            groups: dict[int, list[tuple[Any, ...]]] = {}
            for r in conn.execute(f"PRAGMA foreign_key_list('{name}')"):
                groups.setdefault(r[0], []).append(r)          # id, seq, table, from, to, on_update, on_delete
            for rows in groups.values():
                rows.sort(key=lambda r: r[1])
                t.fks.append(FK([r[3] for r in rows], rows[0][2], [r[4] for r in rows], rows[0][6] or "NO ACTION"))
            for idx in conn.execute(f"PRAGMA index_list('{name}')"):
                if idx[2] and idx[3] != "pk":                   # unique, origin: 'u' (UNIQUE) или 'c' (CREATE UNIQUE INDEX)
                    t.uniques.append([r[2] for r in conn.execute(f"PRAGMA index_info('{idx[1]}')")])
            tables[name] = t
        for t in tables.values():                               # FK без явных столбцов ссылаются на PK родителя
            for fk in t.fks:
                if any(c is None for c in fk.ref_cols) and fk.ref_table in tables:
                    fk.ref_cols = list(tables[fk.ref_table].pk) or fk.ref_cols
        return list(tables.values())
    finally:
        conn.close()


def _card_child(t: Table, cols: set[str]) -> str:
    """Определяет кратность у дочернего конца связи.

    Args:
        t (Table): дочерняя таблица (держит внешний ключ).
        cols (set[str]): столбцы внешнего ключа.

    Returns:
        str: ``"1"``, если столбцы FK совпадают с первичным ключом или с UNIQUE-ограничением
        (связь 1:1); иначе ``"M"`` (связь 1:M).
    """
    if cols == set(t.pk) and t.pk:
        return "1"
    return "1" if any(set(u) == cols for u in t.uniques) else "M"


def _fk_notnull(t: Table, cols: Iterable[str]) -> bool:
    """Проверяет, обязателен ли внешний ключ.

    Args:
        t (Table): дочерняя таблица.
        cols (Iterable[str]): столбцы внешнего ключа.

    Returns:
        bool: ``True``, если все столбцы NOT NULL или входят в первичный ключ.
    """
    by_name = {c.name: c for c in t.cols}
    return all(by_name[c].notnull or by_name[c].pk for c in cols if c in by_name)


def build_info(tables: list[Table]) -> Diagram:
    """Строит инфологическую диаграмму (сущности и связи по правилам модуля) без раскладки.

    Args:
        tables (list[Table]): результат :func:`read_schema`.

    Returns:
        Diagram: диаграмма ``KIND_INFO`` с нулевыми координатами узлов и стандартной легендой.
    """
    d = Diagram(KIND_INFO)
    by_name = {t.name: t for t in tables}
    ids: dict[str, str] = {}
    uid_fk: dict[str, set[str]] = {}
    for t in tables:
        fk_cols = {c for fk in t.fks for c in fk.cols}
        uid: set[str] = set()
        for u in t.uniques:
            if set(u) <= fk_cols:
                uid |= set(u)
        if t.pk and set(t.pk) <= fk_cols:
            uid |= set(t.pk)
        uid_fk[t.name] = uid
        hidden = set(fk_cols)
        if len(t.pk) == 1 and any(set(u) <= fk_cols for u in t.uniques):
            hidden.add(t.pk[0])                                  # суррогатный ключ: UID задают связи
        rows = []
        for c in t.cols:
            if c.name in hidden:
                continue
            mark = "#" if c.name in t.pk else ("*" if c.notnull else "o")
            rows.append(Row(name=c.name, mark=mark))
        node = Node(id=d.new_id("n"), title=t.name.upper(), rows=rows, table=t.name)
        ids[t.name] = node.id
        d.nodes.append(node)
    for t in tables:
        for fk in t.fks:
            if fk.ref_table not in by_name:
                continue
            cols = set(fk.cols)
            card = _card_child(t, cols)
            d.links.append(Link(
                id=d.new_id("l"), a=ids[t.name], b=ids[fk.ref_table],
                card_a=card, card_b="1", must_a=_fk_notnull(t, fk.cols), must_b=False,
                bar_a=cols <= uid_fk[t.name], bar_b=False,
                name_a="относится к", name_b="имеет" if card == "1" else "включает"))
    d.legend = default_legend(KIND_INFO)
    return d


def build_data(tables: list[Table]) -> Diagram:
    """Строит даталогическую диаграмму без раскладки.

    Таблицы показываются со всеми столбцами, ключами (``PK``, ``FK``, ``U``) и типами, связи
    подписываются правилом ``ON DELETE``.

    Args:
        tables (list[Table]): результат :func:`read_schema`.

    Returns:
        Diagram: диаграмма ``KIND_DATA`` с нулевыми координатами узлов и стандартной легендой.
    """
    d = Diagram(KIND_DATA, arrows=True)
    by_name = {t.name: t for t in tables}
    ids: dict[str, str] = {}
    for t in tables:
        fk_cols = {c for fk in t.fks for c in fk.cols}
        rows = []
        for c in t.cols:
            keys = []
            if c.name in t.pk:
                keys.append("PK")
            if c.name in fk_cols:
                keys.append("FK")
            if any(c.name in u for u in t.uniques):
                keys.append("U")
            rows.append(Row(name=c.name, type=c.type, key=",".join(keys), notnull=c.notnull or bool(c.pk), default=c.default))
        node = Node(id=d.new_id("n"), title=t.name, rows=rows, table=t.name, uniques=[list(u) for u in t.uniques])
        ids[t.name] = node.id
        d.nodes.append(node)
    for t in tables:
        names = [c.name for c in t.cols]
        for fk in t.fks:
            if fk.ref_table not in by_name:
                continue
            parent_names = [c.name for c in by_name[fk.ref_table].cols]
            cols = set(fk.cols)
            d.links.append(Link(
                id=d.new_id("l"), a=ids[t.name], b=ids[fk.ref_table],
                card_a=_card_child(t, cols), card_b="1", must_a=_fk_notnull(t, fk.cols), must_b=False,
                label=fk.on_delete, fk_cols=list(fk.cols), ref_cols=[c for c in fk.ref_cols if c],
                row_a=names.index(fk.cols[0]) if fk.cols[0] in names else None,
                row_b=parent_names.index(fk.ref_cols[0]) if fk.ref_cols and fk.ref_cols[0] in parent_names else None))
    d.legend = default_legend(KIND_DATA, arrows=True)
    return d


def project_from_tables(db_path: str, tables: list[Table], measure: Measure) -> Project:
    """Строит проект (обе диаграммы с раскладкой) по готовому списку таблиц.

    Args:
        db_path (str): путь к базе данных, записываемый в проект (может быть пустым).
        tables (list[Table]): описание таблиц (из :func:`read_schema` или из проектировщика).
        measure (Measure): функция измерения текста (``Fonts.measure``) для подгонки ширины узлов.

    Returns:
        Project: новый проект с инфологической и даталогической диаграммами.
    """
    project = Project(db_path=db_path)
    project.diagrams[KIND_INFO] = build_info(tables)
    project.diagrams[KIND_DATA] = build_data(tables)
    for d in project.diagrams.values():
        layout.fit_widths(d, measure)
        layout.auto_layout(d)
    return project


def build_project(db_path: str, measure: Measure) -> Project:
    """Читает БД и строит готовый проект: обе диаграммы, ширины узлов по тексту, автораскладка.

    Args:
        db_path (str): путь к базе данных SQLite.
        measure (Measure): функция измерения текста (``Fonts.measure``) для подгонки ширины узлов.

    Returns:
        Project: новый проект с заполненным ``db_path`` и двумя диаграммами.

    Raises:
        sqlite3.Error: если базу не удалось открыть или прочитать.
    """
    return project_from_tables(db_path, read_schema(db_path), measure)

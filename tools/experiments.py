#!/usr/bin/env python3
"""Воспроизводимые измерения для описания методики: проверка нормальных форм и поиск зависимостей.

Запуск (печатает таблицы результатов; семена генератора фиксированы)::

    python tools/experiments.py

Опыт 1 (объявлены все ключи) и опыт 2 (объявлен только первичный ключ, остальные программа выводит из зависимостей) сверяют вердикт :func:`er_editor.normalform.check` с независимым полным перебором
по учебным определениям 2НФ, 3НФ и НФБК. Опыт 3 проверяет :func:`er_editor.fdmine.discover` на
таблицах, в которые зависимости заложены заранее.
"""
from __future__ import annotations

import os
import random
import sqlite3
import sys
import tempfile
import time
from itertools import combinations
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from er_editor import fdmine, normalform  # noqa: E402
from er_editor.model import KIND_DATA, Diagram, Node, Row  # noqa: E402

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


def main() -> None:
    """Выполняет все опыты и печатает результаты."""
    for label, flag in (("Опыт 1: объявлены все ключи-кандидаты", True), ("Опыт 2: объявлен только первичный ключ", False)):
        print(label, verdict_experiment(flag))
    for n in (50, 200, 1000):
        print(f"Опыт 3: строк в таблице {n}", mining_experiment(n))


if __name__ == "__main__":
    main()

"""Поиск функциональных зависимостей по данным загруженной базы SQLite (кандидаты для поля «Зависимости»).

Модуль не зависит от pygame и не использует ИИ: это точный перебор. Для каждой таблицы он ищет
зависимости ``X -> A`` с левой частью из одного или двух столбцов, которые **выполняются во всех
прочитанных строках**, и оставляет только минимальные (если ``X -> A`` найдена, ``X, Y -> A`` не нужна).

Важно: данные могут лишь опровергнуть зависимость, но не доказать её. Найденное — кандидаты,
которые подтверждает человек по смыслу предметной области. Чтобы случайные совпадения не засоряли
список, не рассматриваются:

* таблицы меньше :data:`MIN_ROWS` строк;
* столбцы с одним значением и столбцы, уникальные по всем строкам (они ключ: определяют всё);
* левые части, значения которых почти не повторяются (меньше :data:`MIN_REPEATS` повторов и 5 % строк).

Пустые значения (``NULL``) считаются обычным значением. Читается не больше :data:`MAX_ROWS` строк
и :data:`MAX_COLS` столбцов таблицы, время ограничено :data:`MAX_SECONDS` секундами.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Optional

from .normalform import FD

MIN_ROWS = 8                             # меньше строк — выводы ненадёжны
MAX_ROWS = 5000                          # сколько строк таблицы читать
MAX_COLS = 20                            # сколько столбцов таблицы рассматривать
MIN_REPEATS = 3                          # наименьшее число повторов значений левой части
MAX_PER_TABLE = 30                       # наибольшее число кандидатов на таблицу
MAX_SECONDS = 5.0                        # общее время поиска


@dataclass
class TableFds:
    """Результат для одной таблицы.

    Attributes:
        name (str): имя таблицы.
        rows (int): сколько строк прочитано.
        fds (list[FD]): найденные зависимости (правые части с одинаковой левой объединены).
        note (str): пояснение, если поиск был неполным или невозможным (пусто — всё в порядке).
    """

    name: str
    rows: int = 0
    fds: list[FD] = field(default_factory=list)
    note: str = ""


def _quote(name: str) -> str:
    """Заключает имя в двойные кавычки для SQL.

    Args:
        name (str): имя таблицы или столбца.

    Returns:
        str: имя в кавычках с удвоенными кавычками внутри.
    """
    return '"' + name.replace('"', '""') + '"'


def _holds(groups: list[int], col: list[Any]) -> bool:
    """Проверяет, что внутри каждой группы строк столбец ``col`` принимает одно значение.

    Args:
        groups (list[int]): для каждой строки номер её группы (по значениям левой части), по порядку строк.
        col (list[Any]): значения проверяемого столбца по строкам.

    Returns:
        bool: ``True``, если ``левая часть -> столбец`` выполняется во всех строках.
    """
    seen: dict[int, Any] = {}
    for g, v in zip(groups, col):
        if seen.setdefault(g, v) != v:
            return False
    return True


def _mine_table(name: str, cols: list[str], data: list[tuple[Any, ...]], deadline: float) -> TableFds:
    """Ищет зависимости в уже прочитанных строках одной таблицы.

    Args:
        name (str): имя таблицы.
        cols (list[str]): имена столбцов.
        data (list[tuple[Any, ...]]): строки.
        deadline (float): момент (``time.monotonic``), после которого поиск прекращается.

    Returns:
        TableFds: кандидаты и пояснение.
    """
    n = len(data)
    res = TableFds(name, n)
    if n < MIN_ROWS:
        res.note = f"мало строк ({n}): по ним зависимости искать ненадёжно"
        return res
    columns = [[row[i] for row in data] for i in range(len(cols))]
    distinct = [len(set(c)) for c in columns]
    useful = [i for i in range(len(cols)) if 1 < distinct[i] < n]      # не константа и не ключ
    need = max(MIN_REPEATS, n // 20)
    found: dict[int, list[tuple[int, ...]]] = {a: [] for a in range(len(cols))}   # A -> найденные левые части

    def covered(lhs: tuple[int, ...], a: int) -> bool:
        """Уже ли A определяется подмножеством левой части.

        Args:
            lhs (tuple[int, ...]): номера столбцов левой части.
            a (int): номер правого столбца.

        Returns:
            bool: ``True``, если найдена зависимость с левой частью, входящей в ``lhs``.
        """
        return any(set(f) <= set(lhs) for f in found[a])

    out: dict[tuple[int, ...], list[int]] = {}
    for size in (1, 2):
        for lhs in combinations(useful, size):
            if time.monotonic() > deadline:
                res.note = "время поиска вышло: список неполный"
                break
            ids: dict[Any, int] = {}
            groups = [ids.setdefault(tuple(columns[c][r] for c in lhs), len(ids)) for r in range(n)]
            if n - len(ids) < need or len(ids) == n:
                continue
            for a in useful:
                if a in lhs or covered(lhs, a) or not _holds(groups, columns[a]):
                    continue
                found[a].append(lhs)
                out.setdefault(lhs, []).append(a)
        else:
            continue
        break
    for lhs, rhs in out.items():
        res.fds.append(FD(tuple(cols[i] for i in lhs), tuple(cols[i] for i in rhs)))
    if len(res.fds) > MAX_PER_TABLE:
        res.fds, res.note = res.fds[:MAX_PER_TABLE], res.note or f"показаны первые {MAX_PER_TABLE} кандидатов"
    if len(cols) > MAX_COLS:
        res.note = res.note or f"рассмотрены первые {MAX_COLS} столбцов"
    return res


def discover(db_path: str) -> list[TableFds]:
    """Ищет кандидатов в функциональные зависимости во всех таблицах базы (база только читается).

    Args:
        db_path (str): путь к файлу SQLite.

    Returns:
        list[TableFds]: по одному элементу на таблицу (в порядке имён).

    Raises:
        sqlite3.Error: если файл не читается как база SQLite.
    """
    deadline = time.monotonic() + MAX_SECONDS
    out: list[TableFds] = []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({_quote(name)})")][:MAX_COLS]
            if not cols:
                continue
            sel = ", ".join(_quote(c) for c in cols)
            data = [tuple(r) for r in con.execute(f"SELECT {sel} FROM {_quote(name)} LIMIT {MAX_ROWS}")]
            out.append(_mine_table(name, cols, data, deadline))
    finally:
        con.close()
    return out


def as_json(tables: list[TableFds]) -> list[dict[str, Any]]:
    """Представление результата для JSON-ответа сервера.

    Args:
        tables (list[TableFds]): результат :func:`discover`.

    Returns:
        list[dict[str, Any]]: ``{name, rows, note, fds: [{lhs, rhs, text}]}`` по таблицам.
    """
    return [{"name": t.name, "rows": t.rows, "note": t.note,
             "fds": [{"lhs": list(f.lhs), "rhs": list(f.rhs), "text": f.text()} for f in t.fds]} for t in tables]


def fd_text(tables: list[TableFds], table: str) -> Optional[list[str]]:
    """Тексты кандидатов одной таблицы (без учёта регистра имени).

    Args:
        tables (list[TableFds]): результат :func:`discover`.
        table (str): имя таблицы.

    Returns:
        Optional[list[str]]: записи вида ``a -> b`` или ``None``, если такой таблицы нет.
    """
    for t in tables:
        if t.name.lower() == table.lower():
            return [f.text() for f in t.fds]
    return None

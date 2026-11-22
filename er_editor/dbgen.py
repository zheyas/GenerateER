"""Создание базы данных SQLite по даталогической диаграмме и заполнение её случайными данными.

Конвейер::

    Diagram (KIND_DATA) ──diagram_to_tables──▶ list[Table] ──create_sql──▶ CREATE TABLE …
                                                    │
                                                    └──create_database──▶ файл .db (+ случайные записи)

Модуль не зависит от pygame, его можно использовать отдельно от окна редактора.

Что переносится из диаграммы в базу: имена таблиц и столбцов, типы, ``NOT NULL``, первичные
ключи (метка ``PK``), ``UNIQUE`` (метка ``U`` и составные ограничения узла), внешние ключи
(связи диаграммы) с правилом ``ON DELETE`` из подписи связи, значения ``DEFAULT`` столбцов. ``CHECK``
диаграмма не хранит, поэтому в базу он не попадает.
"""
from __future__ import annotations

import os
import random
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from .model import Diagram, Link, Node
from .schema import FK, Col, Table

SAMPLE_ROWS = 5                                               # записей в каждой таблице при включённом чекбоксе
ON_DELETE_RULES = ("CASCADE", "RESTRICT", "SET NULL", "SET DEFAULT", "NO ACTION")

Record = dict[str, Any]                                       # одна строка таблицы: {имя столбца: значение}

FIRST_NAMES = ["Иван", "Мария", "Алексей", "Ольга", "Дмитрий", "Анна", "Сергей", "Екатерина", "Андрей", "Наталья"]
LAST_NAMES = ["Иванов", "Петрова", "Смирнов", "Кузнецова", "Попов", "Соколова", "Лебедев", "Козлова", "Новиков", "Морозова"]
PATRONYMICS = ["Иванович", "Петровна", "Сергеевич", "Алексеевна", "Дмитриевич", "Андреевна"]
CITIES = ["Москва", "Санкт-Петербург", "Казань", "Новосибирск", "Екатеринбург", "Самара", "Омск", "Уфа"]
STREETS = ["Ленина", "Гагарина", "Пушкина", "Мира", "Советская", "Садовая", "Центральная", "Школьная"]
WORDS = ["Альфа", "Бета", "Гамма", "Дельта", "Омега", "Сигма", "Титан", "Орион", "Вектор", "Прайм", "Атлас", "Нова"]
STATUSES = ["pending", "processing", "shipped", "delivered", "cancelled"]
CATEGORIES = ["Протеины", "Гейнеры", "Аминокислоты", "Витамины", "Снеки", "Одежда"]
STATUSES_RU = ["Новый", "В обработке", "Отправлен", "Доставлен", "Отменён"]
COUNTRIES = ["Россия", "Беларусь", "Казахстан", "Германия", "Франция", "Италия"]
ROLES_RU = ["Покупатель", "Менеджер", "Администратор", "Курьер", "Оператор"]
POSITIONS = ["Менеджер", "Продавец", "Кладовщик", "Бухгалтер", "Курьер", "Аналитик"]
DEPARTMENTS = ["Продажи", "Склад", "Закупки", "Бухгалтерия", "Маркетинг", "Логистика"]
GENRES = ["Фантастика", "Детектив", "Роман", "Приключения", "Фэнтези", "Научпоп"]
BRANDS = ["Атлас", "Вектор", "Орион", "Нова", "Прайм", "Титан"]
COLORS = ["Красный", "Синий", "Зелёный", "Чёрный", "Белый", "Серый"]
KINDS_RU = ["Базовый", "Стандарт", "Премиум", "Эконом", "Бизнес"]
GENDERS = ["М", "Ж"]
GENERIC_NAME_COLS = {"название", "наименование", "name", "title", "значение", "value"}   # смысл берётся из имени таблицы


class SchemaError(ValueError):
    """Схему диаграммы нельзя превратить в базу данных (список проблем — в ``problems``).

    Attributes:
        problems (list[str]): понятные пользователю описания всех найденных проблем.
    """

    def __init__(self, problems: list[str]) -> None:
        """Создаёт исключение.

        Args:
            problems (list[str]): найденные проблемы схемы (непустой список).
        """
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class DbReport:
    """Итог создания базы данных.

    Attributes:
        path (str): путь к созданному файлу.
        tables (int): число созданных таблиц.
        rows (dict[str, int]): сколько записей добавлено в каждую таблицу (пусто без данных).
        warnings (list[str]): предупреждения (например, не удалось добавить какую-то запись).
        triggers (int): сколько защитных триггеров добавлено (0 — защита выключена или нет текстовых столбцов).
    """

    path: str
    tables: int
    rows: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    triggers: int = 0

    @property
    def total_rows(self) -> int:
        """Общее число добавленных записей.

        Returns:
            int: сумма записей по всем таблицам.
        """
        return sum(self.rows.values())


# ───────────────────────── диаграмма → схема ─────────────────────────

def key_tokens(key: str) -> list[str]:
    """Разбирает метки ключей строки таблицы.

    Args:
        key (str): строка вида ``"PK,FK"`` (регистр и пробелы не важны).

    Returns:
        list[str]: метки в верхнем регистре в порядке следования (``["PK", "FK"]``).
    """
    return [t.strip().upper() for t in key.split(",") if t.strip()]


def format_uniques(groups: list[list[str]]) -> str:
    """Записывает составные ограничения UNIQUE в виде текста для поля ввода.

    Args:
        groups (list[list[str]]): группы столбцов.

    Returns:
        str: например, ``"user_id, product_id; email"`` — группы через «;», столбцы через запятую.
    """
    return "; ".join(", ".join(g) for g in groups)


def parse_uniques(text: str) -> list[list[str]]:
    """Разбирает текст ограничений UNIQUE, введённый пользователем.

    Args:
        text (str): группы столбцов через «;», столбцы внутри группы через запятую.

    Returns:
        list[list[str]]: непустые группы без пустых имён (формат обратен :func:`format_uniques`).
    """
    groups = [[c.strip() for c in part.split(",") if c.strip()] for part in text.split(";")]
    return [g for g in groups if g]


def _lookup(names: list[str], table: Table) -> Optional[list[str]]:
    """Приводит имена столбцов к написанию, принятому в таблице (без учёта регистра).

    Args:
        names (list[str]): искомые имена.
        table (Table): таблица, в которой ищем.

    Returns:
        Optional[list[str]]: имена столбцов таблицы или ``None``, если список пуст или какого-то
        столбца нет.
    """
    by_lower = {c.name.lower(): c.name for c in table.cols}
    out = [by_lower.get(n.strip().lower()) for n in names]
    if not out or any(o is None for o in out):
        return None
    return [o for o in out if o is not None]


def _is_key(table: Table, cols: list[str]) -> bool:
    """Проверяет, образуют ли столбцы первичный ключ или ограничение UNIQUE таблицы.

    Args:
        table (Table): таблица.
        cols (list[str]): проверяемые столбцы.

    Returns:
        bool: ``True``, если на эти столбцы можно ссылаться внешним ключом.
    """
    s = set(cols)
    return s == set(table.pk) or any(s == set(u) for u in table.uniques)


def _infer_fk_columns(child: Node, parent: Node, ref: list[str], taken: set[str]) -> Optional[list[str]]:
    """Подбирает столбцы внешнего ключа в дочерней таблице по именам, если связь к ним не привязана.

    Ищутся ещё не занятые столбцы с меткой ``FK``, названные как родительский столбец
    (``id``, ``<родитель>_id`` и т. п.). Если подходящих по имени нет, но свободный
    FK-столбец единственный, а ключ родителя одиночный, берётся он.

    Args:
        child (Node): дочерний узел (с внешним ключом).
        parent (Node): родительский узел.
        ref (list[str]): столбцы родителя, на которые ссылаемся.
        taken (set[str]): столбцы дочернего узла, уже занятые другими связями.

    Returns:
        Optional[list[str]]: имена столбцов дочернего узла или ``None``, если определить не удалось.
    """
    flagged = [r.name.strip() for r in child.rows if "FK" in key_tokens(r.key) and r.name.strip() not in taken]
    ptitle = parent.title.strip().lower()
    out: list[str] = []
    for pc in ref:
        want = {pc.lower(), f"{ptitle}_{pc}".lower(), f"{ptitle}{pc}".lower(), f"{pc}_{ptitle}".lower()}
        hit = next((c for c in flagged if c.lower() in want and c not in out), None)
        if hit is None:
            break
        out.append(hit)
    if len(out) == len(ref):
        return out
    if len(ref) == 1 and len(flagged) == 1:
        return flagged
    return None


def diagram_to_tables(d: Diagram) -> list[Table]:
    """Превращает даталогическую диаграмму в описание таблиц базы данных.

    Проверяет схему целиком и собирает **все** найденные проблемы (а не только первую).

    Args:
        d (Diagram): даталогическая диаграмма (``KIND_DATA``): узлы — таблицы, связи — внешние ключи.

    Returns:
        list[Table]: таблицы в порядке узлов диаграммы с первичными и внешними ключами и UNIQUE.

    Raises:
        SchemaError: если таблица без имени или без столбцов, повторяются имена таблиц или
            столбцов, у связи нельзя определить столбцы ключа, на родительские столбцы нельзя
            ссылаться (они не PK и не UNIQUE) и т. п.
    """
    problems: list[str] = []
    tables: dict[str, Table] = {}
    seen: set[str] = set()
    for n in d.nodes:
        title = n.title.strip()
        if not title:
            problems.append("есть таблица без названия")
            continue
        if title.lower() in seen:
            problems.append(f"две таблицы называются «{title}»")
            continue
        seen.add(title.lower())
        cols: list[Col] = []
        pk: list[str] = []
        names: set[str] = set()
        for r in n.rows:
            cname = r.name.strip()
            if not cname:
                problems.append(f"«{title}»: есть столбец без имени")
                continue
            if cname.lower() in names:
                problems.append(f"«{title}»: столбец «{cname}» повторяется")
                continue
            names.add(cname.lower())
            is_pk = "PK" in key_tokens(r.key)
            if is_pk:
                pk.append(cname)
            cols.append(Col(cname, r.type.strip(), r.notnull or is_pk, len(pk) if is_pk else 0, r.default.strip()))
        if not cols:
            problems.append(f"«{title}»: нет ни одного столбца")
            continue
        t = Table(title, cols, pk=pk)
        grouped: set[str] = set()
        for g in n.uniques:
            found = _lookup(g, t)
            if found is None:
                problems.append(f"«{title}»: в UNIQUE({', '.join(g)}) есть несуществующий столбец")
            else:
                t.uniques.append(found)
                grouped.update(found)
        for r in n.rows:
            cname = r.name.strip()
            if "U" in key_tokens(r.key) and cname and cname not in grouped and cname.lower() in names:
                t.uniques.append([cname])
        tables[n.id] = t

    taken: dict[str, set[str]] = {nid: set() for nid in tables}
    pending: list[tuple[Any, Table, Table, Node, Node, list[str]]] = []
    for l in d.links:
        a, b = d.node(l.a), d.node(l.b)
        ta, tb = tables.get(l.a), tables.get(l.b)
        if a is None or b is None or ta is None or tb is None:
            continue
        label = f"связь «{a.title.strip()}» → «{b.title.strip()}»"
        ref = (_lookup(l.ref_cols, tb) if l.ref_cols else None) or list(tb.pk)
        if not ref:
            problems.append(f"{label}: у таблицы «{tb.name}» нет первичного ключа — отметьте столбец меткой PK")
            continue
        if not _is_key(tb, ref):
            problems.append(f"{label}: столбцы {', '.join(ref)} таблицы «{tb.name}» не являются PK или UNIQUE")
            continue
        child = _lookup(l.fk_cols, ta) if l.fk_cols else None
        if child is not None and len(child) == len(ref):
            taken[l.a].update(child)
            ta.fks.append(FK(child, tb.name, ref, _on_delete(l.label)))
            _one_to_one(l.card_a, ta, child)
        else:
            pending.append((l, ta, tb, a, b, ref))
    for l, ta, tb, a, b, ref in pending:
        label = f"связь «{a.title.strip()}» → «{b.title.strip()}»"
        child = _infer_fk_columns(a, b, ref, taken[l.a])
        if child is None:
            problems.append(f"{label}: не найден столбец внешнего ключа в таблице «{ta.name}» "
                            f"(добавьте столбец с меткой FK или создайте связь заново)")
            continue
        taken[l.a].update(child)
        ta.fks.append(FK(child, tb.name, ref, _on_delete(l.label)))
        _one_to_one(l.card_a, ta, child)
    if problems:
        raise SchemaError(problems)
    return [tables[n.id] for n in d.nodes if n.id in tables]


def _on_delete(label: str) -> str:
    """Определяет правило ``ON DELETE`` по подписи связи.

    Args:
        label (str): подпись связи на диаграмме (например, ``"cascade"``).

    Returns:
        str: правило из :data:`ON_DELETE_RULES` в верхнем регистре; ``"NO ACTION"``, если подпись
        не похожа на правило.
    """
    rule = " ".join(label.upper().split())
    return rule if rule in ON_DELETE_RULES else "NO ACTION"


def _one_to_one(card: str, table: Table, cols: list[str]) -> None:
    """Добавляет UNIQUE на столбцы внешнего ключа для связи «один к одному».

    Args:
        card (str): кратность на дочернем конце связи (``"1"`` — один).
        table (Table): дочерняя таблица.
        cols (list[str]): столбцы внешнего ключа.
    """
    if card == "1" and set(cols) != set(table.pk) and not any(set(u) == set(cols) for u in table.uniques):
        table.uniques.append(list(cols))


# ───────────────────────── схема → SQL ─────────────────────────

def quote(name: str) -> str:
    """Заключает идентификатор SQLite в двойные кавычки (кавычки внутри удваиваются).

    Args:
        name (str): имя таблицы или столбца (допускаются пробелы и кириллица).

    Returns:
        str: безопасная запись идентификатора.
    """
    return '"' + name.replace('"', '""') + '"'


_DEFAULT_WORDS = ("NULL", "TRUE", "FALSE", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP")
_DEFAULT_NUMBER = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def default_sql(text: str) -> str:
    """Превращает введённое значение по умолчанию в безопасный SQL-литерал для ``DEFAULT``.

    Число и слова ``NULL`` / ``TRUE`` / ``FALSE`` / ``CURRENT_*`` записываются как есть; строка в апострофах
    сохраняется; выражение в скобках (как в импортированной базе, например ``(datetime('now'))``) допускается
    только со сбалансированными скобками и апострофами и без «;»; всё остальное — обычная строка (апострофы
    удваиваются), поэтому введённый текст не может изменить SQL.

    Args:
        text (str): значение, записанное пользователем или прочитанное из базы.

    Returns:
        str: SQL-литерал; пустая строка, если значение пустое.
    """
    t = text.strip()
    if not t:
        return ""
    if t.upper() in _DEFAULT_WORDS:
        return t.upper()
    if _DEFAULT_NUMBER.fullmatch(t):
        return t
    if t.startswith("(") and t.endswith(")") and ";" not in t and t.count("'") % 2 == 0 and t.count("(") == t.count(")"):
        return t
    if len(t) >= 2 and t[0] == t[-1] == "'":
        t = t[1:-1].replace("''", "'")
    return "'" + t.replace("'", "''") + "'"


def rule_warnings(tables: list[Table]) -> list[str]:
    """Находит правила ``ON DELETE``, которые не сработают так, как ожидается.

    Args:
        tables (list[Table]): схема (результат :func:`diagram_to_tables`).

    Returns:
        list[str]: предупреждения: ``SET NULL`` при столбце ``NOT NULL`` (удаление родителя не удастся) и
        ``SET DEFAULT`` при столбце без ``DEFAULT`` (будет подставлен ``NULL`` либо удаление не удастся).
    """
    out: list[str] = []
    for t in tables:
        by = {c.name: c for c in t.cols}
        for fk in t.fks:
            cols = [by[c] for c in fk.cols if c in by]
            where = f"«{t.name}» → «{fk.ref_table}»"
            if fk.on_delete == "SET NULL":
                bad = [c.name for c in cols if c.notnull]
                if bad:
                    out.append(f"{where}: правило SET NULL, но {', '.join(bad)} — NOT NULL, поэтому родителя с дочерними записями нельзя будет удалить")
            elif fk.on_delete == "SET DEFAULT":
                bad = [c.name for c in cols if not c.default.strip()]
                if bad:
                    out.append(f"{where}: правило SET DEFAULT, но у {', '.join(bad)} нет значения DEFAULT (подставится NULL"
                               f"{' — а столбец NOT NULL, удаление не удастся' if any(c.notnull for c in cols if c.name in bad) else ''})")
    return out


def link_rule_hint(child: Node, link: Link) -> str:
    """Подсказка для панели связи: не противоречит ли правило ``ON DELETE`` столбцам внешнего ключа.

    Args:
        child (Node): подчинённая таблица связи (где лежит внешний ключ).
        link (Link): связь даталогической диаграммы.

    Returns:
        str: пояснение на русском или пустая строка, если проблем нет либо столбцы ключа не известны.
    """
    rule = _on_delete(link.label)
    rows = [r for r in child.rows if r.name.strip().lower() in {c.strip().lower() for c in link.fk_cols}]
    if rule == "SET NULL" and any(r.notnull for r in rows):
        return "SET NULL не сработает: столбец внешнего ключа объявлен NOT NULL."
    if rule == "SET DEFAULT" and any(not r.default.strip() for r in rows):
        return "SET DEFAULT: у столбца внешнего ключа не задан DEFAULT (подставится NULL). Задайте значение, которое есть в родительской таблице."
    return ""


_QUOTES = ("'", '"')
_COMPARISONS = ("=", ">", "<", " like ", " in (", " between ", " is null", " exists")
_STACKED = ("; drop table ", "; drop view ", "; drop index ", "; drop trigger ", "; delete from ", "; insert into ", "; alter table ",
            "; create table ", "; create trigger ", "; create view ", "; attach database ", "; pragma ")
_MARKERS = ("/**/", "/*!", "sqlite_master", "sqlite_schema", "load_extension(", "randomblob(", "or sleep(", "and sleep(", "or benchmark(",
            "and benchmark(", "xp_cmdshell", "information_schema.", "<script", "' or (select", "' and (select", "'||(select", "' || (select")


def guard_condition() -> str:
    """Собирает условие защитного триггера: значение ``v`` (нормализованное) похоже на SQL-инъекцию.

    Одно слово не считается признаком: фразы вроде «he said 'no' or 'maybe'», «credit union selection», «wait; drop by»
    или «time.sleep(5)» проходят. Условие срабатывает только на сочетаниях, которые в обычном тексте почти не встречаются:

    * кавычка + ``or`` / ``and`` / ``having`` + знак сравнения (``=``, ``>``, ``<``, ``like``, ``in (``…) где-либо в значении;
    * ``; drop table``, ``; delete from``, ``; insert into``, ``; update … set``, ``; select … from`` и другие стековые операторы;
    * ``union … select`` с продолжением (``from``, число, ``null``, ``*``, ``(``);
    * тавтологии без кавычки (`` or 1=1``, `` and 1=1``, `` or 'a'='a``);
    * кавычка с комментарием в конце значения (``admin'--``, ``x' #``), ``'; --``;
    * ``' order by 1``; конструкции ``/**/``, ``/*!``, ``sqlite_master``, ``load_extension(``, ``or sleep(``, ``<script`` и т. п.

    Returns:
        str: SQL-выражение над подзапросным столбцом ``v`` (в нижнем регистре, без табуляций, пробелы схлопнуты).
    """
    def has(*lits: str) -> str:
        """Условие «в ``v`` есть хотя бы одна из подстрок».

        Args:
            *lits (str): искомые подстроки.

        Returns:
            str: SQL-выражение.
        """
        return "(" + " OR ".join(f"instr(v, {_lit(x)}) > 0" for x in lits) + ")"

    def glob(*pats: str) -> str:
        """Условие «``v`` соответствует хотя бы одному шаблону GLOB» (``*`` — любые символы, ``?`` — один).

        Args:
            *pats (str): шаблоны GLOB.

        Returns:
            str: SQL-выражение.
        """
        return "(" + " OR ".join(f"v GLOB {_lit(x)}" for x in pats) + ")"

    breaks = [f"{q}{sp}{kw}{after}" for q in _QUOTES for sp in ("", " ") for kw in ("or", "and", "having") for after in (" ", q)]
    parts = [
        f"({has(*breaks)} AND {has(*_COMPARISONS)})",
        has(*_STACKED),
        glob("*; update *set *", "*; select *from *"),
        has("'; --", "';--", '"; --', '";--'),
        glob("*union *select *from *", "*union *select [0-9]*", "*union *select null*", "*union *select [*]*", "*union *select (*",
             "*union *select @*", "*union *select '*"),
        glob("* or [0-9]=[0-9]*", "* and [0-9]=[0-9]*", "* or [0-9] = [0-9]*", "* and [0-9] = [0-9]*", "* or '?'='?*", "* and '?'='?*",
             '* or "?"="?*', '* and "?"="?*'),
        glob("*'--", "*' --", "*'-- ", '*"--', '*" --', "*'#", "*' #", "*' order by [0-9]*", '*" order by [0-9]*'),
        has(*_MARKERS),
    ]
    return " OR ".join(parts)


def is_text_type(type_name: str) -> bool:
    """Определяет, хранит ли столбец текст по правилам аффинности SQLite (или тип не указан).

    Args:
        type_name (str): объявленный тип столбца.

    Returns:
        bool: ``True`` для типов со словами ``CHAR``, ``CLOB``, ``TEXT`` и для столбца без типа.
    """
    t = type_name.upper()
    return not t.strip() or any(w in t for w in ("CHAR", "CLOB", "TEXT"))


def _lit(text: str) -> str:
    """Записывает строку как SQL-литерал в апострофах.

    Args:
        text (str): произвольный текст.

    Returns:
        str: литерал с удвоенными апострофами.
    """
    return "'" + text.replace("'", "''") + "'"


def guard_sql(tables: list[Table]) -> list[str]:
    """Формирует защитные триггеры ``BEFORE INSERT`` и ``BEFORE UPDATE`` для текстовых столбцов таблиц.

    Триггер приводит значение к нижнему регистру, заменяет табуляции и переводы строк пробелами, схлопывает
    повторы пробелов (до 256 подряд) и отклоняет запись (``RAISE(ABORT)``), если значение удовлетворяет
    :func:`guard_condition` (сочетания признаков, а не отдельные слова). Это последний рубеж «в глубину»: он ловит
    типичные полезные нагрузки, но не заменяет параметризованные запросы в приложении; обойти эвристику
    изобретательной нагрузкой возможно, а редкий безобидный текст, в точности похожий на атаку, может быть отклонён.

    Args:
        tables (list[Table]): схема.

    Returns:
        list[str]: операторы ``CREATE TRIGGER`` (по два на таблицу с текстовыми столбцами).
    """
    out: list[str] = []
    cond = guard_condition()
    for t in tables:
        cols = [c.name for c in t.cols if is_text_type(c.type)]
        if not cols:
            continue
        body = []
        for c in cols:
            norm = f"lower(NEW.{quote(c)})"
            for a, b in (("char(9)", "' '"), ("char(10)", "' '"), ("char(13)", "' '")):
                norm = f"replace({norm}, {a}, {b})"
            for _ in range(8):
                norm = f"replace({norm}, '  ', ' ')"
            # LIMIT 1 запрещает SQLite «разворачивать» подзапрос: иначе нормализация значения вычислялась бы заново в каждом условии
            msg = _lit(f"Защита от инъекций: недопустимое значение в столбце {t.name}.{c}")
            body.append(f"    SELECT RAISE(ABORT, {msg}) FROM (SELECT {norm} AS v LIMIT 1) WHERE {cond};")
        text = "\n".join(body)
        out.append(f"CREATE TRIGGER {quote('er_guard_ins_' + t.name)} BEFORE INSERT ON {quote(t.name)}\nBEGIN\n{text}\nEND")
        out.append(f"CREATE TRIGGER {quote('er_guard_upd_' + t.name)} BEFORE UPDATE OF {_cols(cols)} ON {quote(t.name)}\nBEGIN\n{text}\nEND")
    return out


def _cols(names: list[str]) -> str:
    """Записывает список столбцов через запятую в кавычках.

    Args:
        names (list[str]): имена столбцов.

    Returns:
        str: например, ``"a", "b"``.
    """
    return ", ".join(quote(n) for n in names)


def create_sql(tables: list[Table]) -> list[str]:
    """Формирует операторы ``CREATE TABLE`` для таблиц.

    Одиночный первичный ключ типа ``INTEGER`` записывается как ``INTEGER PRIMARY KEY`` (псевдоним
    rowid), остальные ключи — табличными ограничениями; внешние ключи — ``FOREIGN KEY … REFERENCES``.

    Args:
        tables (list[Table]): результат :func:`diagram_to_tables`.

    Returns:
        list[str]: по одному оператору на таблицу (без завершающей «;»).
    """
    out: list[str] = []
    for t in tables:
        by_name = {c.name: c for c in t.cols}
        rowid_pk = len(t.pk) == 1 and by_name[t.pk[0]].type.upper() == "INTEGER"
        lines: list[str] = []
        for c in t.cols:
            parts = [quote(c.name)]
            if c.type:
                parts.append(c.type)
            if rowid_pk and c.name == t.pk[0]:
                parts.append("PRIMARY KEY")
            elif c.notnull:
                parts.append("NOT NULL")
            if c.default.strip():
                parts.append("DEFAULT " + default_sql(c.default))
            lines.append("    " + " ".join(parts))
        if t.pk and not rowid_pk:
            lines.append(f"    PRIMARY KEY ({_cols(t.pk)})")
        for u in t.uniques:
            lines.append(f"    UNIQUE ({_cols(u)})")
        for fk in t.fks:
            lines.append(f"    FOREIGN KEY ({_cols(fk.cols)}) REFERENCES {quote(fk.ref_table)} ({_cols(fk.ref_cols)})"
                         f" ON DELETE {fk.on_delete}")
        out.append(f"CREATE TABLE {quote(t.name)} (\n" + ",\n".join(lines) + "\n)")
    return out


def _parents_first(tables: list[Table]) -> list[Table]:
    """Упорядочивает таблицы так, чтобы родительские шли раньше дочерних.

    Самоссылки и циклы ссылок разрываются (обратное ребро игнорируется).

    Args:
        tables (list[Table]): таблицы схемы.

    Returns:
        list[Table]: те же таблицы в порядке «родители первыми».
    """
    by_name = {t.name: t for t in tables}
    state: dict[str, int] = {}
    order: list[Table] = []

    def visit(t: Table) -> None:
        """Обходит таблицу и её родителей в глубину.

        Args:
            t (Table): посещаемая таблица.
        """
        if state.get(t.name):
            return
        state[t.name] = 1
        for fk in t.fks:
            if fk.ref_table != t.name and fk.ref_table in by_name:
                visit(by_name[fk.ref_table])
        state[t.name] = 2
        order.append(t)

    for t in tables:
        visit(t)
    return order


# ───────────────────────── случайные данные ─────────────────────────

def _affinity(type_name: str) -> str:
    """Определяет родственность типа по правилам SQLite.

    Args:
        type_name (str): объявленный тип столбца (``"VARCHAR(50)"``, ``"int"``…).

    Returns:
        str: ``"INTEGER"``, ``"TEXT"``, ``"REAL"``, ``"BLOB"`` или ``"NUMERIC"`` (пустой тип — ``"TEXT"``).
    """
    t = type_name.upper()
    if not t:
        return "TEXT"
    if "INT" in t:
        return "INTEGER"
    if any(x in t for x in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in t:
        return "BLOB"
    if any(x in t for x in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _random_date(rng: random.Random, name: str) -> str:
    """Генерирует случайную дату-время в формате ISO-8601.

    Args:
        rng (random.Random): источник случайности.
        name (str): имя столбца (``birth`` — дата рождения, ``expire`` — в будущем).

    Returns:
        str: например, ``"2025-03-28T00:00:00"``.
    """
    n = name.lower()
    if "birth" in n or "рожд" in n:
        base, span = datetime(1960, 1, 1), 16000
    elif "expire" in n or "until" in n or "срок" in n:
        base, span = datetime(2026, 1, 1), 400
    else:
        base, span = datetime(2024, 1, 1), 800
    return (base + timedelta(days=rng.randint(0, span))).replace(microsecond=0).isoformat()


def _text_value(name: str, i: int, rng: random.Random) -> str:
    """Подбирает правдоподобный текст по имени столбца.

    Args:
        name (str): имя столбца (по нему определяется смысл: e-mail, имя, город, статус…).
        i (int): номер записи (используется в уникальных по смыслу значениях).
        rng (random.Random): источник случайности.

    Returns:
        str: сгенерированная строка.
    """
    n = name.lower()
    if "mail" in n or "почт" in n:
        return f"user{i + 1}_{rng.randint(10, 99)}@example.com"
    if "phone" in n or "tel" in n or "тел" in n:
        return f"+7 9{rng.randint(10, 99)} {rng.randint(100, 999)}-{rng.randint(10, 99)}-{rng.randint(10, 99)}"
    if "login" in n or "username" in n or "nick" in n or "логин" in n:
        return f"user{i + 1}_{rng.randint(100, 999)}"
    if "pass" in n or "hash" in n or "token" in n or "парол" in n:
        return "".join(rng.choice("0123456789abcdef") for _ in range(32))
    if "first" in n or n in ("name", "имя", "fname"):
        return rng.choice(FIRST_NAMES) if n != "name" else f"{rng.choice(WORDS)} {rng.choice(WORDS)} {i + 1}"
    if "last" in n or "surname" in n or "фамил" in n:
        return rng.choice(LAST_NAMES)
    if "middle" in n or "patronym" in n or "отчест" in n:
        return rng.choice(PATRONYMICS)
    if "title" in n or "name" in n or "назван" in n:
        return f"{rng.choice(WORDS)} {rng.choice(WORDS)} {i + 1}"
    if "country" in n or "стран" in n:
        return "Россия"
    if "city" in n or "город" in n:
        return rng.choice(CITIES)
    if "street" in n or "улиц" in n:
        return rng.choice(STREETS)
    if "house" in n or "building" in n or n.endswith("дом"):
        return str(rng.randint(1, 150))
    if "apartment" in n or "flat" in n:
        return str(rng.randint(1, 200))
    if "status" in n or "статус" in n:
        return rng.choice(STATUSES)
    if "role" in n or "роль" in n:
        return rng.choice(["user", "admin"])
    if "categ" in n or "катег" in n:
        return rng.choice(CATEGORIES)
    if "адрес" in n or "address" in n:
        return f"г. {rng.choice(CITIES)}, ул. {rng.choice(STREETS)}, д. {rng.randint(1, 150)}"
    if "apartment" in n or "flat" in n or "квартир" in n:
        return str(rng.randint(1, 200))
    if any(k in n for k in ("descr", "comment", "note", "text", "описан", "комментар", "примеч")):
        return " ".join(rng.choice(WORDS) for _ in range(6)) + "."
    if any(k in n for k in ("image", "photo", "avatar", "url", "path", "file", "фото", "изображ", "картин", "ссылк", "сайт")):
        return f"/static/img/{i + 1}.png"
    return f"{name}_{rng.randint(1, 999)}"


def _number_value(name: str, integer: bool, rng: random.Random) -> Any:
    """Подбирает число по имени столбца (цена, количество, вес, флаг…).

    Args:
        name (str): имя столбца.
        integer (bool): ``True`` — вернуть целое, иначе число с плавающей точкой.
        rng (random.Random): источник случайности.

    Returns:
        Any: ``int`` или ``float``.
    """
    n = name.lower()
    if any(k in n for k in ("vip", "is_", "has_", "flag", "active", "enabled", "deleted", "bool", "актив", "флаг", "признак")):
        return rng.randint(0, 1)
    if any(k in n for k in ("price", "cost", "total", "sum", "salary", "цен", "стоим", "сумм", "итог", "баланс", "зарплат")):
        v = rng.uniform(10, 5000)
    elif any(k in n for k in ("qty", "quantity", "count", "stock", "amount", "number", "кол", "остаток", "запас", "штук")):
        v = rng.randint(1, 100)
    elif "weight" in n or "вес" in n:
        v = rng.choice([250, 500, 900, 1000, 1200, 2000])
    elif "age" in n or "возраст" in n:
        v = rng.randint(18, 70)
    elif "year" in n:
        v = rng.randint(1990, 2025)
    elif "rating" in n or "score" in n or "рейтинг" in n or "оценк" in n or "балл" in n:
        v = rng.randint(1, 5)
    else:
        v = rng.randint(1, 1000) if integer else rng.uniform(1, 1000)
    return int(round(v)) if integer else round(float(v), 2)


def _pool_for(name: str) -> Optional[list[str]]:
    """Подбирает готовый список значений для справочных столбцов (статус, город, категория…).

    Args:
        name (str): имя столбца или (для общих «название» / «name») имя таблицы-справочника.

    Returns:
        Optional[list[str]]: список правдоподобных значений или ``None``, если для имени
        справочника нет.
    """
    n = name.lower()
    if "статус" in n:
        return STATUSES_RU
    if "катег" in n:
        return CATEGORIES
    if "город" in n:
        return CITIES
    if "стран" in n:
        return COUNTRIES
    if "роль" in n:
        return ROLES_RU
    if "должност" in n or "position" in n:
        return POSITIONS
    if "отдел" in n or "department" in n:
        return DEPARTMENTS
    if "жанр" in n or "genre" in n:
        return GENRES
    if "бренд" in n or "производител" in n or "brand" in n or "manufactur" in n:
        return BRANDS
    if "цвет" in n or "color" in n or "colour" in n:
        return COLORS
    if n in ("тип", "type", "вид", "уровень", "level"):
        return KINDS_RU
    if n in ("пол", "gender"):
        return GENDERS
    return None


def _unique_groups(t: Table) -> list[list[str]]:
    """Собирает все группы столбцов таблицы, значения которых должны быть уникальны.

    Args:
        t (Table): таблица.

    Returns:
        list[list[str]]: первичный ключ (если есть) и все ограничения UNIQUE.
    """
    return ([list(t.pk)] if t.pk else []) + [list(u) for u in t.uniques]


def _column_value(t: Table, c: Col, i: int, unique: bool, rng: random.Random) -> Any:
    """Генерирует значение обычного (не внешнего) столбца для записи номер ``i``.

    Args:
        t (Table): таблица.
        c (Col): столбец.
        i (int): номер записи (с нуля).
        unique (bool): столбец входит в PK / UNIQUE — значение должно отличаться у всех записей.
        rng (random.Random): источник случайности.

    Returns:
        Any: ``int``, ``float``, ``str``, ``bytes`` или ``None`` (для необязательных столбцов
        примерно в 15 % случаев).
    """
    aff, tp = _affinity(c.type), c.type.upper()
    single_pk = len(t.pk) == 1 and t.pk[0] == c.name
    if not unique and not c.notnull and rng.random() < 0.15:
        return None
    if aff == "INTEGER" and not any(k in tp for k in ("DATE", "TIME")):
        return i + 1 if unique else _number_value(c.name, True, rng)
    if aff == "BLOB":
        return bytes(rng.randint(0, 255) for _ in range(8))
    n = c.name.lower()
    if any(k in tp for k in ("DATE", "TIME")) or any(k in n for k in ("date", "time", "_at", "created", "updated", "expire", "birth", "дат", "время", "созд", "обнов", "рожд", "срок")):
        if unique:
            return (datetime(2025, 1, 1) + timedelta(days=i)).isoformat()
        return _random_date(rng, c.name)
    if aff in ("REAL", "NUMERIC"):
        return float(i + 1) if unique else _number_value(c.name, False, rng)
    if single_pk:
        prefix = "".join(ch for ch in t.name.upper() if ch.isalnum())[:3] or "ID"
        return f"{prefix}{i + 1}"
    pool = _pool_for(t.name if n in GENERIC_NAME_COLS else n)
    if pool:
        choice = pool[i % len(pool)] if unique else rng.choice(pool)
        return f"{choice} {i + 1}" if unique and i >= len(pool) else choice
    text = _text_value(c.name, i, rng)
    if unique and "mail" not in n and not text.endswith(str(i + 1)):
        text = f"{text}_{i + 1}"
    m = re.search(r"\((\d+)\)", c.type)
    return text[:int(m.group(1))] if m else text


def generate_rows(tables: list[Table], n: int, rng: random.Random) -> tuple[dict[str, list[Record]], list[str]]:
    """Генерирует случайные записи для всех таблиц с учётом ключей.

    Сначала заполняются обычные столбцы (первичные и уникальные ключи — гарантированно
    различные), затем столбцы внешних ключей получают значения из записей родительских таблиц
    (родители обрабатываются первыми). Для связей «один к одному» и составных ключей из
    внешних столбцов значения подбираются так, чтобы не нарушить уникальность.

    Args:
        tables (list[Table]): схема базы данных.
        n (int): сколько записей создать в каждой таблице.
        rng (random.Random): источник случайности (с фиксированным ``seed`` результат повторяется).

    Returns:
        tuple[dict[str, list[Record]], list[str]]: ``({таблица: записи}, предупреждения)``.
        Записей может оказаться меньше ``n``, если уникальное значение подобрать не удалось.
    """
    warnings: list[str] = []
    order = _parents_first(tables)
    fk_names = {t.name: {c for fk in t.fks for c in fk.cols} for t in tables}
    data: dict[str, list[Record]] = {}
    for t in order:
        in_unique = {c for g in _unique_groups(t) for c in g}
        rows: list[Record] = []
        for i in range(n):
            row: Record = {}
            for c in t.cols:
                if c.name in fk_names[t.name]:
                    row[c.name] = None
                else:
                    row[c.name] = _column_value(t, c, i, c.name in in_unique, rng)
            rows.append(row)
        data[t.name] = rows
    for t in order:
        if not t.fks:
            continue
        groups = [g for g in _unique_groups(t) if set(g) & fk_names[t.name]]
        used: list[set[tuple[Any, ...]]] = [set() for _ in groups]
        kept: list[Record] = []
        by_cols = {c.name: c for c in t.cols}
        for i, row in enumerate(data[t.name]):
            placed = False
            for _attempt in range(60):
                for fk in t.fks:
                    if fk.ref_table not in data:         # ссылка на неизвестную таблицу — оставляем NULL
                        continue
                    parents = data[fk.ref_table]
                    if fk.ref_table == t.name:
                        pool = kept if kept else [row]
                    else:
                        pool = parents
                    optional = not any(by_cols[c].notnull for c in fk.cols)
                    if fk.ref_table == t.name and not kept and optional:
                        picked: Optional[Record] = None
                    else:
                        picked = rng.choice(pool) if pool else None
                    for fc, rc in zip(fk.cols, fk.ref_cols):
                        row[fc] = picked[rc] if picked is not None else None
                keys = [tuple(row[c] for c in g) for g in groups]
                if all(k not in u for k, u in zip(keys, used)):
                    for k, u in zip(keys, used):
                        u.add(k)
                    placed = True
                    break
            if placed:
                kept.append(row)
            else:
                warnings.append(f"«{t.name}»: не удалось подобрать уникальные значения для записи {i + 1}")
        data[t.name] = kept
    return data, warnings


# ───────────────────────── создание файла ─────────────────────────

def create_database(path: str, tables: list[Table], sample_rows: int = 0, seed: Optional[int] = None, guard: bool = False) -> DbReport:
    """Создаёт файл базы данных SQLite по схеме и (по желанию) наполняет его случайными записями.

    База собирается во временном файле и подменяет ``path`` только после успешного завершения,
    поэтому при ошибке уже существующий файл не портится. После вставки данных выполняется
    ``PRAGMA foreign_key_check``; найденные нарушения попадают в предупреждения.

    Args:
        path (str): путь к создаваемому файлу (существующий файл заменяется).
        tables (list[Table]): схема (результат :func:`diagram_to_tables`).
        sample_rows (int): сколько случайных записей добавить в каждую таблицу; 0 — только схема.
        seed (Optional[int]): начальное значение генератора случайных чисел; ``None`` — каждый
            раз новые данные.
        guard (bool): добавить защитные триггеры от SQL-инъекций (:func:`guard_sql`); создаются после
            случайных данных, поэтому не мешают их вставке.

    Returns:
        DbReport: итог: число таблиц, число добавленных записей и предупреждения.

    Raises:
        sqlite3.Error: если SQLite отклонила схему (например, неверный тип или ссылка).
        OSError: если файл нельзя создать или заменить (нет прав, нет папки).
    """
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    report = DbReport(path=path, tables=len(tables))
    report.warnings += rule_warnings(tables)
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for sql in create_sql(tables):
            conn.execute(sql)
        if sample_rows > 0:
            data, warns = generate_rows(tables, sample_rows, random.Random(seed))
            report.warnings += warns
            for t in _parents_first(tables):
                names = [c.name for c in t.cols]
                stmt = f"INSERT INTO {quote(t.name)} ({_cols(names)}) VALUES ({', '.join('?' for _ in names)})"
                count = 0
                for row in data.get(t.name, []):
                    try:
                        conn.execute(stmt, [row[c] for c in names])
                        count += 1
                    except sqlite3.Error as exc:
                        report.warnings.append(f"«{t.name}»: запись пропущена ({exc})")
                report.rows[t.name] = count
        if guard:
            for sql in guard_sql(tables):
                conn.execute(sql)
                report.triggers += 1
        conn.commit()
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            report.warnings.append(f"нарушений внешних ключей: {len(bad)}")
    except BaseException:
        conn.close()
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    conn.close()
    os.replace(tmp, path)
    return report

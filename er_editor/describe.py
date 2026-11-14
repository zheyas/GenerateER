"""Построение схемы базы данных по описанию: SQL (``CREATE TABLE``) или короткий текст.

Модуль не зависит от pygame. Он превращает описание в список таблиц
(:class:`er_editor.schema.Table`), из которых ``schema.project_from_tables`` делает обе диаграммы.
Описание только **переводится в схему**: нормализация не выполняется — соответствие
нормальной форме потом проверяет :mod:`er_editor.normalform`.

Два формата (определяются автоматически: если в тексте есть ``CREATE TABLE`` — это SQL)::

    # текст: сущности, связи, обычные фразы
    Клиент: имя, email!, город, телефон?
    Заказ: дата:дата, сумма:вещественное
    Товар: название, цена:вещественное
    Клиент 1:N Заказ
    Заказ N:M Товар через Позиция: количество:целое
    у одного клиента может быть много заказов

Правила текста:

* ``Сущность: атрибут, атрибут…`` — таблица; ``id INTEGER PRIMARY KEY`` добавляется сам;
* у атрибута ``!`` — уникальный, ``?`` — может быть пустым, ``:тип`` — целое, вещественное, текст,
  дата (без типа он выбирается по названию);
* ``A 1:N B`` — у одного A много B (внешний ключ в B); также ``N:1``, ``1:1``, ``N:M``
  (создаётся таблица-связка, имя и атрибуты — после слова ``через``), ``A -> B`` (A ссылается на B),
  ``?`` после кратности (``1:N?``) — ссылка необязательна;
* обычные фразы распознаются по типовым оборотам («у одного A может быть много B»,
  «каждый B принадлежит одному A», «много A … много B»).
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

from .schema import FK, Col, Table, read_schema

MAX_TEXT = 200_000                       # наибольший размер описания, символов

EXAMPLE_TEXT = """# Магазин: сущности, их атрибуты и связи
Клиент: имя, email!, город, телефон?
Заказ: дата:дата, сумма:вещественное, статус
Товар: название, цена:вещественное, остаток:целое
Категория: название!

Клиент 1:N Заказ
Категория 1:N Товар
Заказ N:M Товар через Позиция: количество:целое, цена:вещественное
"""

EXAMPLE_PROSE = """Описание предметной области

Интернет-магазин парфюмерии (Код, Название, Контактные телефоны, электронный адрес, физический адрес) специализируется на продаже духов
и других товаров. По каждому товару поддерживается следующая информация:
1 продукт (Код, Название, Описание, Производитель, Страна, Бренд, Цена, Количество на складе, Объём флакона, Изображение);
— категория товара (например, парфюмерная вода, туалетная вода, духи);
— ароматические ноты товара: название ноты и группа (верхняя, средняя или базовая);
2 По каждому пользователю хранится информация:
— пользователь (Код, Фамилия, Имя, Отчество, Дата рождения, Телефон, Email, Роль, Фото);
— учётные данные (Код пользователя, Email используется как логин, Хеш пароля);
— адрес доставки (Код, Код пользователя, Страна, Город, Улица, Номер дома, Квартира, Этаж);
3 По каждому заказу фиксируется:
— заказ (Код, Код пользователя, Дата заказа, Общая стоимость, Статус);
— связь заказ—продукт (Код заказа, Код продукта, Количество).
"""

EXAMPLE_SQL = """CREATE TABLE clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    created DATE
);
"""

TYPE_WORDS = {
    "целое": "INTEGER", "int": "INTEGER", "integer": "INTEGER", "число": "INTEGER", "логическое": "INTEGER", "bool": "INTEGER",
    "вещественное": "REAL", "real": "REAL", "float": "REAL", "дробное": "REAL", "decimal": "REAL", "деньги": "REAL",
    "текст": "TEXT", "text": "TEXT", "строка": "TEXT", "string": "TEXT", "varchar": "TEXT",
    "дата": "DATE", "date": "DATE", "время": "DATETIME", "datetime": "DATETIME",
}
REAL_HINTS = ("объём", "объем", "volume", "цена", "price", "сумма", "стоимость", "вес", "weight", "amount", "баланс", "рейтинг", "rating", "total",
              "зарплата", "salary", "оклад", "скидка", "discount", "площадь")
INT_HINTS = ("этаж", "количество", "кол-во", "qty", "quantity", "count", "возраст", "age", "год", "year", "остаток", "stock", "порядок")
DATE_HINTS = ("дата", "date", "время", "_at")
ONE_WORDS = {"один", "одного", "одному", "одним", "одной", "одна", "одну", "каждый", "каждого", "каждому", "каждая", "каждой",
             "каждую", "каждое", "любой", "любого", "единственный"}
MANY_WORDS = {"много", "несколько", "нескольких", "нескольким", "множество", "многие", "многим", "многих", "многими"}
PARENT_VERBS = {"имеет", "содержит", "включает", "состоит", "владеет", "делает", "оформляет", "создаёт", "создает", "размещает"}
CHILD_VERBS = {"принадлежит", "относится", "входит", "привязан", "связан", "назначен"}

_WORDS = re.compile(r"[\w-]+", re.UNICODE)
_ENTITY = re.compile(r"^(?P<name>[^:]{1,60}?)\s*:\s*(?P<attrs>.*)$")
_CARD = r"[1NnMmНнМм]\s*[:\-–—]\s*[1NnMmНнМм]\??"
_WORD_CARD = r"(?:один|многие)[\s-]ко?[\s-](?:многим|одному)"
_RELATION = re.compile(rf"^(?P<a>.+?)\s+(?P<op>{_CARD}|->|→|=>|{_WORD_CARD})\s+(?P<b>.+)$", re.IGNORECASE)
_CREATE_AS = re.compile(r"\bAS\s*\(?\s*(?:SELECT|WITH|VALUES)\b", re.IGNORECASE)
MAX_SQL_SECONDS = 3.0                    # сколько секунд можно выполнять SQL описания
_STATEMENT = re.compile(r"^\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\b", re.IGNORECASE | re.DOTALL)


class DescribeError(ValueError):
    """Описание нельзя превратить в схему (список причин — в ``problems``).

    Attributes:
        problems (list[str]): понятные пользователю причины.
    """

    def __init__(self, problems: list[str]) -> None:
        """Создаёт исключение.

        Args:
            problems (list[str]): причины (непустой список).
        """
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Described:
    """Результат разбора описания.

    Attributes:
        tables (list[Table]): таблицы построенной схемы.
        notes (list[str]): как поняты фразы и что пропущено (показывается пользователю).
        fmt (str): ``"sql"``, ``"text"`` (короткая запись) или ``"prose"`` (словесное описание предметной области).
    """

    tables: list[Table]
    notes: list[str] = field(default_factory=list)
    fmt: str = "text"


@dataclass
class _Entity:
    """Сущность текстового описания.

    Attributes:
        title (str): название как написано пользователем.
        name (str): имя таблицы.
        attrs (list[tuple[str, str, bool, bool]]): атрибуты ``(имя, тип, обязателен, уникален)``.
    """

    title: str
    name: str
    attrs: list[tuple[str, str, bool, bool]] = field(default_factory=list)


def ident(text: str) -> str:
    """Превращает название в имя таблицы или столбца: строчные буквы, пробелы и дефисы — в ``_``.

    Args:
        text (str): произвольное название.

    Returns:
        str: имя из букв, цифр и ``_`` (пустая строка, если подходящих символов нет).
    """
    return re.sub(r"\W+", "_", text.strip().lower(), flags=re.UNICODE).strip("_")


def stem(word: str) -> str:
    """Основа слова для сопоставления падежей и чисел («клиентов» → «клие»).

    Args:
        word (str): слово (название сущности).

    Returns:
        str: начало слова, общее у его форм.
    """
    w = word.lower()
    return w if len(w) <= 3 else w[:-1] if len(w) <= 5 else w[:-2]


def guess_type(name: str) -> str:
    """Выбирает тип столбца по его названию.

    Args:
        name (str): название атрибута.

    Returns:
        str: ``DATE``, ``REAL``, ``INTEGER`` или ``TEXT``.
    """
    n = name.lower()
    if any(h in n for h in DATE_HINTS):
        return "DATE"
    if any(h in n for h in REAL_HINTS):
        return "REAL"
    if any(h in n for h in INT_HINTS):
        return "INTEGER"
    return "TEXT"


def parse_attr(token: str) -> Optional[tuple[str, str, bool, bool]]:
    """Разбирает один атрибут вида ``имя[:тип][!][?]``.

    Args:
        token (str): текст атрибута.

    Returns:
        Optional[tuple[str, str, bool, bool]]: ``(имя, тип, обязателен, уникален)`` или ``None``, если имени нет.
    """
    t = token.strip()
    unique = nullable = False
    while t and t[-1] in "!?":
        unique, nullable = unique or t[-1] == "!", nullable or t[-1] == "?"
        t = t[:-1].rstrip()
    name, _, typ = t.partition(":")
    name = ident(name)
    if not name:
        return None
    typ = typ.strip().lower()
    return name, TYPE_WORDS.get(typ, typ.upper() if typ else guess_type(name)), not nullable, unique


def split_attrs(text: str) -> list[str]:
    """Делит список атрибутов по запятым, не трогая запятые внутри скобок.

    Args:
        text (str): часть строки после двоеточия.

    Returns:
        list[str]: непустые фрагменты.
    """
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth <= 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [p.strip() for p in out if p.strip()]


def normalize_op(op: str) -> tuple[str, bool]:
    """Приводит запись кратности к виду ``1:N`` / ``N:1`` / ``1:1`` / ``N:M``.

    Args:
        op (str): запись между сущностями (``1:N``, ``->``, «один-ко-многим»…).

    Returns:
        tuple[str, bool]: ``(кратность, ссылка необязательна)``.
    """
    o = op.strip().lower()
    if o in ("->", "→", "=>"):
        return "N:1", False
    words = {"один-ко-многим": "1:N", "многие-ко-многим": "N:M", "один-к-одному": "1:1", "многие-к-одному": "N:1"}
    key = re.sub(r"\s+", "-", o)
    if key in words:
        return words[key], False
    optional = o.endswith("?")
    o = o.rstrip("?").replace("н", "n").replace("м", "m").replace("–", ":").replace("—", ":").replace("-", ":")
    parts = [p.strip().upper() for p in o.split(":")]
    card = ":".join(parts)
    return ("N:M" if card == "M:N" else card), optional


class _TextBuilder:
    """Накапливает сущности и связи текстового описания и строит таблицы.

    Attributes:
        entities (dict[str, _Entity]): сущности по имени таблицы.
        notes (list[str]): пояснения для пользователя.
        problems (list[str]): ошибки разбора.
    """

    def __init__(self) -> None:
        """Создаёт пустой построитель."""
        self.entities: dict[str, _Entity] = {}
        self.notes: list[str] = []
        self.problems: list[str] = []
        self.tables: dict[str, Table] = {}
        self.relations: list[tuple[int, str, str, str, bool, str, str]] = []

    def find(self, text: str) -> Optional[_Entity]:
        """Находит сущность по названию (без учёта регистра, числа и падежа).

        Args:
            text (str): название из описания.

        Returns:
            Optional[_Entity]: сущность или ``None``.
        """
        key = ident(text)
        if key in self.entities:
            return self.entities[key]
        return next((e for e in self.entities.values() if key.startswith(stem(e.name)) or e.name.startswith(stem(key))), None)

    def add_entity(self, lineno: int, name: str, attrs_text: str) -> None:
        """Добавляет сущность из строки ``Имя: атрибуты``.

        Args:
            lineno (int): номер строки (для сообщений).
            name (str): название сущности.
            attrs_text (str): перечень атрибутов.
        """
        tname = ident(name)
        if not tname:
            self.problems.append(f"строка {lineno}: пустое название сущности")
            return
        if tname in self.entities:
            self.problems.append(f"строка {lineno}: сущность «{name.strip()}» описана дважды")
            return
        ent = _Entity(name.strip(), tname)
        seen = {"id"}
        for tok in split_attrs(attrs_text):
            a = parse_attr(tok)
            if a is None:
                self.problems.append(f"строка {lineno}: не удалось разобрать атрибут «{tok}»")
            elif a[0] in seen:
                self.notes.append(f"строка {lineno}: атрибут «{a[0]}» пропущен: столбец уже есть (id добавляется сам)")
            else:
                seen.add(a[0])
                ent.attrs.append(a)
        self.entities[tname] = ent

    def add_relation(self, lineno: int, a: str, op: str, b: str, via: str = "", via_attrs: str = "") -> None:
        """Запоминает связь между сущностями; таблицы строятся позже, когда известны все сущности.

        Args:
            lineno (int): номер строки.
            a (str): название левой сущности.
            op (str): запись кратности.
            b (str): название правой сущности.
            via (str): название таблицы-связки (для ``N:M``).
            via_attrs (str): атрибуты связки.
        """
        card, optional = normalize_op(op)
        if card not in ("1:N", "N:1", "1:1", "N:M"):
            self.problems.append(f"строка {lineno}: неизвестная кратность «{op}» (допустимо 1:N, N:1, 1:1, N:M)")
            return
        self.relations.append((lineno, a, card, b, optional, via, via_attrs))

    def add_sentence(self, lineno: int, line: str) -> None:
        """Разбирает обычную фразу («у одного клиента может быть много заказов») и запоминает связь.

        Args:
            lineno (int): номер строки.
            line (str): фраза.
        """
        words = [w.lower() for w in _WORDS.findall(line)]
        hits: list[tuple[int, _Entity]] = []
        for i, w in enumerate(words):
            for e in self.entities.values():
                if w.startswith(stem(e.name)) and all(h[1] is not e for h in hits):
                    hits.append((i, e))
                    break
        if len(hits) < 2:
            self.problems.append(f"строка {lineno}: не понята фраза «{line}» (нужны две описанные сущности; надёжнее запись «A 1:N B»)")
            return
        (i1, e1), (i2, e2) = hits[0], hits[1]

        def qual(i: int, first: bool) -> str:
            """Определяет, стоит ли перед упоминанием сущности слово «один» или «много».

            Args:
                i (int): позиция слова.
                first (bool): первая ли это сущность во фразе («у клиента …» тоже значит «один»).

            Returns:
                str: ``"one"``, ``"many"`` или пустая строка.
            """
            window = words[max(0, i - 3):i]
            if any(w in MANY_WORDS for w in window):
                return "many"
            if any(w in ONE_WORDS for w in window) or (first and words[:1] == ["у"]):
                return "one"
            return ""
        q1, q2 = qual(i1, True), qual(i2, False)
        between = set(words[i1 + 1:i2])
        if not q1 and not q2:
            q1 = "one" if between & PARENT_VERBS else "many" if between & CHILD_VERBS else ""
            q2 = {"one": "many", "many": "one", "": ""}[q1]
        elif not q1:
            q1 = {"one": "many", "many": "one"}[q2]
        elif not q2:
            q2 = {"one": "many", "many": "one"}[q1]
        card = {("one", "many"): "1:N", ("many", "one"): "N:1", ("many", "many"): "N:M", ("one", "one"): "1:1"}.get((q1, q2))
        if (q1, q2) == ("one", "one") and between & CHILD_VERBS:
            card = "N:1"                      # «каждый товар принадлежит одной категории»
        if card is None:
            self.problems.append(f"строка {lineno}: во фразе «{line}» не ясно, кто «один», а кто «много»; запишите «{e1.title} 1:N {e2.title}»")
            return
        self.notes.append(f"строка {lineno}: «{line}» понято как {e1.title} {card} {e2.title}")
        self.add_relation(lineno, e1.title, card, e2.title)

    def link(self, lineno: int, parent: _Entity, child: _Entity, optional: bool, unique: bool) -> None:
        """Добавляет в таблицу ребёнка внешний ключ на родителя.

        Args:
            lineno (int): номер строки.
            parent (_Entity): родитель («один»).
            child (_Entity): ребёнок («много», хранит ключ).
            optional (bool): ссылка может быть пустой.
            unique (bool): связь «один к одному» (UNIQUE на ключе).
        """
        optional = optional or parent is child       # у корня дерева родителя нет
        t = self.tables[child.name]
        base = "parent_id" if parent is child else f"{parent.name}_id"
        col, k = base, 2
        while any(c.name == col for c in t.cols):
            col, k = f"{base}_{k}", k + 1
        t.cols.append(Col(col, "INTEGER", not optional, 0))
        t.fks.append(FK([col], parent.name, ["id"], "SET NULL" if optional else "RESTRICT"))
        if unique:
            t.uniques.append([col])
        if parent is child:
            self.notes.append(f"строка {lineno}: самосвязь «{child.title}»: столбец {col} может быть пустым")

    def build(self) -> list[Table]:
        """Строит таблицы по накопленным сущностям и связям.

        Returns:
            list[Table]: таблицы в порядке описания (таблицы-связки — в конце).
        """
        for e in self.entities.values():
            cols = [Col("id", "INTEGER", True, 1)] + [Col(n, t, nn, 0) for n, t, nn, _ in e.attrs]
            tbl = Table(e.name, cols, pk=["id"])
            tbl.uniques = [[n] for n, _, _, uq in e.attrs if uq]
            self.tables[e.name] = tbl
        for lineno, a, card, b, optional, via, via_attrs in self.relations:
            ea, eb = self.find(a), self.find(b)
            missing = [x for x, e in ((a, ea), (b, eb)) if e is None]
            if ea is None or eb is None:
                self.problems.append(f"строка {lineno}: не найдена сущность «{missing[0]}»; опишите её строкой «{missing[0]}: атрибуты»")
                continue
            if card == "N:M":
                self.junction(lineno, ea, eb, via, via_attrs)
            elif card == "1:N":
                self.link(lineno, ea, eb, optional, False)
            elif card == "N:1":
                self.link(lineno, eb, ea, optional, False)
            else:
                self.link(lineno, ea, eb, optional, True)
        return list(self.tables.values())

    def junction(self, lineno: int, a: _Entity, b: _Entity, via: str, via_attrs: str) -> None:
        """Создаёт таблицу-связку для связи «многие ко многим».

        Args:
            lineno (int): номер строки.
            a (_Entity): первая сущность.
            b (_Entity): вторая сущность.
            via (str): название связки (пусто — ``<a>_<b>``).
            via_attrs (str): атрибуты связки.
        """
        name = ident(via) or f"{a.name}_{b.name}"
        if name in self.tables:
            self.problems.append(f"строка {lineno}: таблица-связка «{name}» уже существует; задайте другое имя после слова «через»")
            return
        ca, cb = (f"{a.name}_id", f"{b.name}_id") if a is not b else ("first_id", "second_id")
        cols = [Col(ca, "INTEGER", True, 1), Col(cb, "INTEGER", True, 2)]
        for tok in split_attrs(via_attrs):
            attr = parse_attr(tok)
            if attr and attr[0] not in (ca, cb):
                cols.append(Col(attr[0], attr[1], attr[2], 0))
        tbl = Table(name, cols, pk=[ca, cb])
        tbl.fks = [FK([ca], a.name, ["id"], "CASCADE"), FK([cb], b.name, ["id"], "CASCADE")]
        self.tables[name] = tbl
        self.notes.append(f"строка {lineno}: связь {a.title} N:M {b.title} реализована таблицей «{name}»")


def tables_from_text(text: str) -> Described:
    """Строит схему по текстовому описанию (сущности, связи, обычные фразы).

    Args:
        text (str): описание в формате, показанном в :data:`EXAMPLE_TEXT`.

    Returns:
        Described: таблицы и пояснения.

    Raises:
        DescribeError: если не описано ни одной сущности или есть неразобранные строки.
    """
    b = _TextBuilder()
    pending: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", "//", "--")):
            continue
        rel = _RELATION.match(line)
        if rel:
            pending.append((lineno, line))
            continue
        ent = _ENTITY.match(line)
        if ent:
            b.add_entity(lineno, ent.group("name"), ent.group("attrs"))
        else:
            pending.append((lineno, line))
    for lineno, line in pending:
        rel = _RELATION.match(line)
        if rel:
            rest, via, via_attrs = rel.group("b"), "", ""
            m = re.split(r"\s+через\s+", rest, maxsplit=1, flags=re.IGNORECASE)
            if len(m) == 2:
                rest = m[0]
                via, _, via_attrs = m[1].partition(":")
            b.add_relation(lineno, rel.group("a"), rel.group("op"), rest, via.strip(), via_attrs)
        else:
            b.add_sentence(lineno, line)
    tables = b.build()
    if not b.entities:
        b.problems.insert(0, "не описано ни одной сущности: напишите строки вида «Клиент: имя, email»")
    if b.problems:
        raise DescribeError(b.problems)
    return Described(tables, b.notes, "text")


def _balanced(line: str) -> bool:
    """Проверяет, закрыты ли все скобки в строке.

    Args:
        line (str): строка текста.

    Returns:
        bool: ``True``, если открывающих и закрывающих скобок поровну.
    """
    return line.count("(") <= line.count(")")


def prose_lines(text: str) -> list[str]:
    """Готовит словесное описание к разбору: убирает маркеры списков и склеивает строки, разорванные внутри скобок.

    Args:
        text (str): текст описания предметной области.

    Returns:
        list[str]: логические строки без маркеров («1», «—», «•»).
    """
    out: list[str] = []
    buf = ""
    for raw in text.replace("\t", " ").splitlines():
        line = raw.strip()
        for _ in range(2):
            line = re.sub(r"^(?:\d+[.)]?\s+|[—–•*-]\s*)", "", line)
        buf = f"{buf} {line}".strip() if buf else line
        if _balanced(buf):
            if buf:
                out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _paren_body(line: str) -> Optional[tuple[str, str]]:
    """Находит в строке первую пару скобок (с учётом вложенных) и делит строку на название и содержимое.

    Args:
        line (str): строка вида ``название (содержимое) …``.

    Returns:
        Optional[tuple[str, str]]: ``(название, содержимое)`` или ``None``, если скобок нет или они не закрыты.
    """
    i = line.find("(")
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(line)):
        depth += (line[j] == "(") - (line[j] == ")")
        if depth == 0:
            return line[:i].strip(), line[i + 1:j]
    return None


def _prose_items(body: str) -> list[str]:
    """Делит перечень атрибутов на элементы по запятым и союзу «и» (вне скобок).

    Args:
        body (str): текст перечня.

    Returns:
        list[str]: элементы без пробелов по краям.
    """
    items: list[str] = []
    for part in split_attrs(body):
        depth, cur = 0, ""
        for token in re.split(r"(\s+и\s+)", part):
            depth += token.count("(") - token.count(")")
            if token.strip() == "и" and depth == 0:
                items.append(cur.strip())
                cur = ""
            else:
                cur += token
        items.append(cur.strip())
    return [i for i in items if i]


def _clean_attr(item: str) -> str:
    """Убирает из названия атрибута пояснения в скобках и обороты вроде «используется как логин».

    Args:
        item (str): атрибут как он написан («Email используется как логин», «цена (в рублях)»).

    Returns:
        str: короткое название атрибута.
    """
    t = re.sub(r"\([^)]*\)", "", item)
    t = re.split(r"\s+(?:используется|является|хранится)\b", t)[0]
    return re.sub(r"^(?:её|его|их)\s+", "", t.strip(), flags=re.IGNORECASE).strip()


def tables_from_prose(text: str) -> Described:
    """Строит схему по словесному описанию предметной области («сущность (Код, Название, …)»).

    Правила: строка ``название (атрибуты)`` или ``название: атрибуты`` — сущность; атрибут «Код» — первичный
    ключ, «Код X» — внешний ключ на сущность X (если она описана); сущность с двумя и более «Код X» и без собственного «Код»
    становится таблицей-связкой с составным ключом; строки-примеры («например, …») и
    предложения без перечня атрибутов пропускаются (и перечисляются в пояснениях).

    Args:
        text (str): текст описания.

    Returns:
        Described: таблицы и пояснения.

    Raises:
        DescribeError: если не найдено ни одной сущности.
    """
    ents: dict[str, tuple[str, list[str]]] = {}
    notes: list[str] = []
    for line in prose_lines(text):
        body = _paren_body(line)
        name, items = "", []
        if body and body[0] and len(body[0].split()) <= 6 and not body[1].lower().lstrip().startswith("например"):
            name, items = body[0], _prose_items(body[1])
            if len(items) < 2:
                name = ""
        elif ":" in line and len(line.split(":")[0].split()) <= 6:
            name, items = line.split(":")[0].strip(), _prose_items(line.split(":", 1)[1].rstrip(".;"))
        if not name or not items or not ident(name):
            if body or ":" in line:
                notes.append(f"пропущено: «{line[:70]}»")
            continue
        key = ident(name)
        if key in ents:
            notes.append(f"сущность «{name}» описана повторно — использовано первое описание")
        else:
            ents[key] = (name, items)
    if not ents:
        raise DescribeError(["не найдено ни одной сущности: пишите «название (Код, Атрибут, …)» или «название: атрибуты»"])

    def target(word: str) -> Optional[str]:
        """Находит сущность по слову из атрибута «Код X» (в любом падеже, по любому слову названия).

        Args:
            word (str): слово или слова после «Код».

        Returns:
            Optional[str]: имя таблицы сущности или ``None``.
        """
        for w in (x.lower() for x in _WORDS.findall(word)):
            for key in ents:
                if any(w.startswith(stem(part)) for part in key.split("_") if len(part) > 2):
                    return key
        return None

    tables: list[Table] = []
    fks: dict[str, list[tuple[str, str]]] = {}
    for key, (name, items) in ents.items():
        cols: list[Col] = []
        refs: list[tuple[str, str]] = []
        plain: list[tuple[str, str]] = []
        has_code = False
        for item in items:
            attr = _clean_attr(item)
            low = attr.lower()
            if low in ("код", "id"):
                has_code = True
            elif low.startswith("код ") and (tgt := target(attr[4:])) and tgt != key:
                refs.append((f"{tgt}_id", tgt))
            elif attr and ident(attr):
                plain.append((ident(attr), guess_type(attr)))
            else:
                notes.append(f"«{name}»: не разобран атрибут «{item}»")
        junction = len(refs) >= 2 and not has_code
        pk_cols: list[str] = []
        if not junction:
            cols.append(Col("id", "INTEGER", True, 1))
            pk_cols = ["id"]
        seen = {c.name for c in cols}
        for n, tgt in refs:
            if n not in seen:
                seen.add(n)
                cols.append(Col(n, "INTEGER", True, len(pk_cols) + 1 if junction else 0))
                if junction:
                    pk_cols.append(n)
        for n, ty in plain:
            if n not in seen:
                seen.add(n)
                cols.append(Col(n, ty, True, 0))
        t = Table(key, cols, pk=pk_cols)
        fks[key] = refs
        tables.append(t)
        if junction:
            notes.append(f"«{name}» — таблица-связка (составной ключ из {', '.join(n for n, _ in refs)})")
    by_name = {t.name: t for t in tables}
    for key, refs in fks.items():
        seen_fk: set[str] = set()
        for n, tgt in refs:
            if n not in seen_fk:
                seen_fk.add(n)
                by_name[key].fks.append(FK([n], tgt, ["id"], "CASCADE" if not by_name[key].pk == ["id"] else "RESTRICT"))
    for key, (name, items) in ents.items():
        for item in items:
            a = _clean_attr(item)
            if a.lower().startswith("код ") and not target(a[4:]):
                notes.append(f"«{name}»: «{a}» — сущность не описана, столбец оставлен обычным")
    return Described(tables, notes, "prose")


def split_statements(sql: str) -> list[str]:
    """Делит SQL-текст на отдельные операторы (с учётом строк в кавычках и комментариев).

    Args:
        sql (str): текст с несколькими операторами.

    Returns:
        list[str]: операторы; незавершённый хвост тоже возвращается.
    """
    out: list[str] = []
    buf = ""
    for ch in sql:
        buf += ch
        if ch == ";" and sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def tables_from_sql(sql: str) -> Described:
    """Строит схему по SQL: выполняет только ``CREATE TABLE`` / ``CREATE INDEX`` во временной базе и читает её структуру.

    Остальные операторы (``INSERT``, ``PRAGMA``, ``ATTACH`` и т. п.) не выполняются, а попадают в пояснения.

    Args:
        sql (str): текст с операторами ``CREATE TABLE``.

    Returns:
        Described: таблицы с ключами, типами и внешними ключами и пояснения.

    Raises:
        DescribeError: если SQL содержит ошибку или не создаёт ни одной таблицы.
    """
    notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="er_sql_") as tmp:
        path = os.path.join(tmp, "described.db")
        con = sqlite3.connect(path)
        deadline = time.monotonic() + MAX_SQL_SECONDS
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 100_000)
        try:
            for st in split_statements(sql):
                if not _STATEMENT.match(st) or _CREATE_AS.search(st):
                    notes.append(f"пропущен оператор (выполняются только CREATE TABLE и CREATE INDEX без AS SELECT): {' '.join(st.split())[:60]}")
                    continue
                try:
                    con.execute(st)
                except sqlite3.Error as exc:
                    raise DescribeError([f"ошибка в SQL: {exc} — «{' '.join(st.split())[:70]}»"]) from exc
            con.commit()
        finally:
            con.close()
        tables = read_schema(path)
    if not tables:
        raise DescribeError(["в SQL нет ни одного оператора CREATE TABLE", *notes])
    return Described(tables, notes, "sql")


def describe(text: str) -> Described:
    """Строит схему по описанию, определяя формат: SQL (есть ``CREATE TABLE``) или текст.

    Args:
        text (str): описание.

    Returns:
        Described: таблицы, пояснения и формат.

    Raises:
        DescribeError: если описание пустое, слишком большое или не разобрано.
    """
    if not text.strip():
        raise DescribeError(["описание пустое: опишите сущности и связи или вставьте SQL"])
    if len(text) > MAX_TEXT:
        raise DescribeError([f"описание слишком большое (больше {MAX_TEXT} символов)"])
    if re.search(r"\bcreate\s+table\b", text, re.IGNORECASE):
        return tables_from_sql(text)
    if any(_paren_body(ln) and len(_prose_items(_paren_body(ln)[1])) >= 2 for ln in prose_lines(text) if _paren_body(ln) and "(" in ln):  # type: ignore[index]
        return tables_from_prose(text)
    return tables_from_text(text)

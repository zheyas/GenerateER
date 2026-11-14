"""Проверка нормальных форм (1НФ, 2НФ, 3НФ, НФБК) для нарисованной даталогической схемы.

Модуль **только проверяет** схему и ничего в ней не меняет. Он не зависит от pygame.

Что известно о схеме и чего не известно
---------------------------------------

На диаграмме есть таблицы, столбцы, типы, ключи (``PK`` / ``FK`` / ``U``) и связи со стрелками.
Функциональные зависимости между обычными столбцами (например, ``город -> страна``) на ней
не нарисованы, поэтому проверка опирается на два источника:

* **заявленные зависимости** — их можно записать у таблицы в поле «Зависимости» в виде
  ``левая часть -> правая часть``. Нарушения, найденные по ним, — достоверные (``error``);
* **признаки по структуре и именам** — повторяющиеся группы столбцов (``телефон1``,
  ``телефон2``), столбец, копирующий атрибут родительской таблицы (``product_name`` рядом
  с ``product_id`` при том, что в ``products`` есть ``name``), пары ``X_id`` + ``X_name`` и т. п.
  Сильные признаки считаются нарушением (``error``), слабые — подозрением (``warn``).

Правила по уровням (каждый следующий уровень включает предыдущие):

* **0 — схема** (всегда): у таблиц есть имена, у связей находятся ключи родителя;
* **1НФ**: есть первичный ключ, имена столбцов не повторяются, типы не составные,
  нет повторяющихся групп столбцов;
* **2НФ**: неключевой столбец не зависит от части составного ключа;
* **3НФ**: неключевой столбец не зависит от другого неключевого столбца (транзитивность);
* **НФБК**: левая часть любой зависимости — ключ.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from itertools import combinations
from typing import Any, Optional

from .dbgen import key_tokens
from .model import Diagram, Node

LEVELS = (1, 2, 3, 4)
LEVEL_NAMES: dict[int, str] = {0: "схема", 1: "1НФ", 2: "2НФ", 3: "3НФ", 4: "НФБК"}
LEVEL_RULES: dict[int, str] = {
    1: "у каждой таблицы есть первичный ключ; значения столбцов атомарны; нет повторяющихся групп столбцов",
    2: "1НФ + каждый неключевой столбец зависит от всего составного ключа, а не от его части",
    3: "2НФ + неключевые столбцы зависят только от ключа, а не друг от друга (нет транзитивных зависимостей)",
    4: "3НФ + левая часть любой функциональной зависимости является ключом таблицы",
}
STRUCTURE = 0
ERROR, WARN = "error", "warn"

NAME_WORDS = {"name", "title", "label", "caption", "название", "наименование", "имя", "фио"}
GENERIC_ATTRS = {"id", "name", "title", "label", "название", "наименование", "имя", "описание", "description", "comment",
                 "комментарий", "note", "заметка", "примечание", "status", "статус", "created_at", "updated_at", "дата",
                 "date", "created", "updated", "тип", "type", "код", "code"}
HIERARCHIES: list[tuple[set[str], set[str]]] = [
    ({"city", "город"}, {"country", "страна"}),
    ({"region", "регион", "область"}, {"country", "страна"}),
    ({"zip", "zipcode", "postal_code", "индекс", "почтовый_индекс"}, {"city", "город", "region", "регион"}),
    ({"street", "улица"}, {"city", "город"}),
    ({"department", "отдел"}, {"department_head", "начальник_отдела", "руководитель_отдела"}),
]
_ATOMIC_BAD_TYPE = re.compile(r"\b(ARRAY|JSON|JSONB|LIST|SET|MAP|XML|HSTORE|OBJECT|STRUCT)\b|\[\]")
_NUM_SUFFIX = re.compile(r"^(.*?)[\s_]*(\d+)$")
_ID_SUFFIX = re.compile(r"[\s_]*(id|ид|код|key|no|num|number|номер)$")
_LIST_NAME = re.compile(r"(^|_)(list|array|ids|tags|phones|emails)$|^(список|перечень|теги|телефоны)|_(список|перечень)$")
_COMPOSITE_NAMES = {"фио", "full_name", "fullname", "полное_имя", "address", "адрес", "full_address", "полный_адрес"}
_ARROW = re.compile(r"\s*(?:-+>|=+>|→|⟶|⇒)\s*")

Dep = tuple[frozenset[str], frozenset[str]]


# ───────────────────────── функциональные зависимости ─────────────────────────

@dataclass(frozen=True)
class FD:
    """Функциональная зависимость ``lhs -> rhs``.

    Attributes:
        lhs (tuple[str, ...]): имена столбцов левой части (детерминант).
        rhs (tuple[str, ...]): имена столбцов правой части (зависимые).
    """

    lhs: tuple[str, ...]
    rhs: tuple[str, ...]

    def text(self) -> str:
        """Записывает зависимость в стандартном виде.

        Returns:
            str: например, ``"город -> страна, регион"``.
        """
        return f"{', '.join(self.lhs)} -> {', '.join(self.rhs)}"


def key(name: str) -> str:
    """Приводит имя столбца к ключу сравнения (без пробелов по краям, в нижнем регистре).

    Args:
        name (str): имя столбца.

    Returns:
        str: ключ для сравнения имён без учёта регистра.
    """
    return name.strip().lower()


def split_fds(text: str) -> list[str]:
    """Делит текст поля «Зависимости» на отдельные записи.

    Args:
        text (str): записи через «;» или перевод строки.

    Returns:
        list[str]: непустые записи без пробелов по краям.
    """
    return [p.strip() for p in re.split(r"[;\n]", text) if p.strip()]


def _names(text: str) -> list[str]:
    """Разбирает перечень столбцов через запятую (фигурные и круглые скобки допускаются).

    Args:
        text (str): например, ``"{a, b}"``.

    Returns:
        list[str]: непустые имена.
    """
    return [c.strip() for c in text.strip().strip("{}() ").split(",") if c.strip()]


def parse_fd(text: str) -> Optional[FD]:
    """Разбирает одну запись вида ``a, b -> c`` (стрелка: ``->``, ``=>``, ``→``).

    Args:
        text (str): запись зависимости.

    Returns:
        Optional[FD]: зависимость или ``None``, если стрелки нет либо одна из частей пуста.
    """
    parts = _ARROW.split(text.strip())
    if len(parts) != 2:
        return None
    lhs, rhs = _names(parts[0]), _names(parts[1])
    return FD(tuple(lhs), tuple(rhs)) if lhs and rhs else None


def normalize_fds(text: str) -> tuple[list[str], list[str]]:
    """Приводит введённый пользователем текст зависимостей к стандартному виду.

    Args:
        text (str): записи через «;» или перевод строки.

    Returns:
        tuple[list[str], list[str]]: ``(распознанные записи в виде "a, b -> c", нераспознанные)``.
    """
    good: list[str] = []
    bad: list[str] = []
    for part in split_fds(text):
        fd = parse_fd(part)
        (good if fd else bad).append(fd.text() if fd else part)
    return good, bad


def format_fds(items: list[str]) -> str:
    """Записывает зависимости таблицы одной строкой для поля ввода.

    Args:
        items (list[str]): записи зависимостей.

    Returns:
        str: записи через «; ».
    """
    return "; ".join(items)


def rename_in_fds(items: list[str], old: str, new: str) -> list[str]:
    """Переименовывает столбец во всех зависимостях (регистр имён не важен).

    Args:
        items (list[str]): записи зависимостей.
        old (str): прежнее имя столбца.
        new (str): новое имя.

    Returns:
        list[str]: записи с заменённым именем; нераспознанные записи не меняются.
    """
    out: list[str] = []
    for item in items:
        fd = parse_fd(item)
        if fd is None:
            out.append(item)
            continue
        out.append(FD(_swap(fd.lhs, old, new), _swap(fd.rhs, old, new)).text())
    return out


def _plural(n: int, one: str, many: str) -> str:
    """Выбирает форму слова по числу (один / несколько).

    Args:
        n (int): количество.
        one (str): форма для одного.
        many (str): форма для нескольких.

    Returns:
        str: ``one`` при ``n == 1``, иначе ``many``.
    """
    return one if n == 1 else many


def _swap(cols: tuple[str, ...], old: str, new: str) -> tuple[str, ...]:
    """Заменяет имя столбца в перечне (регистр не важен).

    Args:
        cols (tuple[str, ...]): имена столбцов.
        old (str): прежнее имя.
        new (str): новое имя.

    Returns:
        tuple[str, ...]: перечень с заменой.
    """
    return tuple(new if key(c) == key(old) else c for c in cols)


def closure(attrs: frozenset[str], deps: list[Dep]) -> frozenset[str]:
    """Вычисляет замыкание множества столбцов относительно зависимостей.

    Args:
        attrs (frozenset[str]): исходные столбцы (ключи сравнения).
        deps (list[Dep]): зависимости ``(левая часть, правая часть)``.

    Returns:
        frozenset[str]: все столбцы, функционально определяемые из ``attrs``.
    """
    out = set(attrs)
    changed = True
    while changed:
        changed = False
        for lhs, rhs in deps:
            if lhs <= out and not rhs <= out:
                out |= rhs
                changed = True
    return frozenset(out)


def derive_keys(cols: list[str], declared: list[frozenset[str]], deps: list[Dep], limit: int = 32) -> list[frozenset[str]]:
    """Находит все ключи-кандидаты таблицы, следующие из записанных зависимостей и объявленных ключей.

    Объявленные ключи входят в вывод как зависимости «ключ → все столбцы». Применяется алгоритм
    Лучези — Освальда: из первого найденного ключа порождаются остальные заменой правой части
    любой зависимости её левой частью, каждый кандидат сокращается до минимального ключа.

    Args:
        cols (list[str]): все столбцы таблицы (ключи сравнения).
        declared (list[frozenset[str]]): объявленные ключи (PK и UNIQUE); без них вывод не имеет смысла.
        deps (list[Dep]): записанные зависимости.
        limit (int): наибольшее число ключей (защита от экспоненциального числа ключей).

    Returns:
        list[frozenset[str]]: минимальные ключи-кандидаты (не больше ``limit``).
    """
    everything = frozenset(cols)
    full = deps + [(k, everything) for k in declared]

    def shrink(s: frozenset[str]) -> frozenset[str]:
        """Сокращает надключ до минимального ключа.

        Args:
            s (frozenset[str]): надключ.

        Returns:
            frozenset[str]: ключ, из которого нельзя убрать ни одного столбца.
        """
        cur = set(s)
        for c in cols:
            if c in cur and everything <= closure(frozenset(cur - {c}), full):
                cur.discard(c)
        return frozenset(cur)

    keys = [shrink(everything)]
    i = 0
    while i < len(keys) and len(keys) < limit:
        k = keys[i]
        i += 1
        for lhs, rhs in full:
            cand = lhs | (k - rhs)
            if not any(m <= cand for m in keys):
                keys.append(shrink(cand))
                if len(keys) >= limit:
                    break
    return keys


def id_stem(name: str) -> str:
    """Отделяет от имени столбца-идентификатора окончание ``_id`` / ``_код`` и т. п.

    Args:
        name (str): имя столбца.

    Returns:
        str: основа в нижнем регистре (``"product"`` для ``product_id``); пустая строка, если
        у имени нет такого окончания или от него ничего не остаётся.
    """
    k = key(name)
    m = _ID_SUFFIX.search(k)
    return k[:m.start()] if m and m.start() > 0 else ""


# ───────────────────────── результаты ─────────────────────────

@dataclass
class Issue:
    """Одно замечание проверки.

    Attributes:
        level (int): 0 — ошибка схемы; 1–4 — нормальная форма, которую нарушает находка.
        severity (str): ``"error"`` — нарушение; ``"warn"`` — подозрение (нужно проверить глазами).
        node_id (str): идентификатор таблицы на диаграмме (пустая строка — замечание ко всей схеме).
        table (str): название таблицы.
        columns (list[str]): затронутые столбцы.
        rule (str): короткое название правила.
        message (str): что именно не так.
        hint (str): как исправить.
    """

    level: int
    severity: str
    node_id: str
    table: str
    columns: list[str]
    rule: str
    message: str
    hint: str = ""


@dataclass
class Report:
    """Результат проверки схемы.

    Attributes:
        target (int): проверяемая нормальная форма (1–4).
        tables (int): число проверенных таблиц.
        issues (list[Issue]): все найденные замечания всех уровней (уровни выше ``target``
            нужны для вывода «какая форма достигнута»).
    """

    target: int
    tables: int
    issues: list[Issue] = field(default_factory=list)

    @property
    def violations(self) -> list[Issue]:
        """Нарушения, мешающие выбранной форме (``error`` с уровнем не выше проверяемого).

        Returns:
            list[Issue]: нарушения схемы и форм до ``target`` включительно.
        """
        return [i for i in self.issues if i.severity == ERROR and i.level <= self.target]

    @property
    def warnings(self) -> list[Issue]:
        """Подозрения, относящиеся к проверяемой форме.

        Returns:
            list[Issue]: ``warn`` с уровнем не выше проверяемого.
        """
        return [i for i in self.issues if i.severity == WARN and i.level <= self.target]

    @property
    def ok(self) -> bool:
        """Соответствует ли схема выбранной нормальной форме.

        Returns:
            bool: ``True``, если таблицы есть и нарушений нет (подозрения вердикт не меняют).
        """
        return self.tables > 0 and not self.violations

    @property
    def achieved(self) -> int:
        """Высшая нормальная форма, которой схема соответствует по найденным нарушениям.

        Returns:
            int: 0 — схема не проходит даже 1НФ (или в ней есть ошибки уровня 0), 1…4 — номер формы.
        """
        if any(i.severity == ERROR and i.level == STRUCTURE for i in self.issues):
            return 0
        bad = [i.level for i in self.issues if i.severity == ERROR and i.level > STRUCTURE]
        return min(bad) - 1 if bad else 4

    def for_node(self, node_id: str, up_to_target: bool = True) -> list[Issue]:
        """Выбирает замечания одной таблицы.

        Args:
            node_id (str): идентификатор таблицы.
            up_to_target (bool): ``True`` — только замечания уровней до проверяемой формы.

        Returns:
            list[Issue]: замечания таблицы (нарушения, затем подозрения).
        """
        got = [i for i in self.issues if i.node_id == node_id and (not up_to_target or i.level <= self.target)]
        return sorted(got, key=lambda i: (i.severity != ERROR, i.level))

    def ordered(self) -> list[Issue]:
        """Упорядочивает замечания для показа: сначала нарушения, потом подозрения; по уровню и таблице.

        Returns:
            list[Issue]: замечания до проверяемой формы включительно.
        """
        got = [i for i in self.issues if i.level <= self.target]
        return sorted(got, key=lambda i: (i.severity != ERROR, i.level, i.table.lower()))

    def verdict(self) -> str:
        """Формулирует вердикт одной строкой.

        Returns:
            str: например, «Схема соответствует 3НФ» или «Схема не соответствует 2НФ: нарушений — 2».
        """
        name = LEVEL_NAMES[self.target]
        if self.tables == 0:
            return "Нарисуйте таблицы на вкладке «Даталогическая»"
        if self.ok:
            tail = f" (подозрений: {len(self.warnings)})" if self.warnings else ""
            return f"Схема соответствует {name}{tail}"
        return f"Схема не соответствует {name}: нарушений — {len(self.violations)}"

    def to_dict(self) -> dict[str, Any]:
        """Преобразует отчёт в словарь для передачи по сети (JSON).

        Returns:
            dict[str, Any]: форма, число таблиц, вердикт и его короткая подпись, достигнутая форма,
            числа нарушений и подозрений и список замечаний (:meth:`ordered`) в виде словарей.
        """
        return {"target": self.target, "tables": self.tables, "ok": self.ok, "achieved": self.achieved,
                "verdict": self.verdict(), "short": self.short(), "violations": len(self.violations),
                "warnings": len(self.warnings), "issues": [asdict(i) for i in self.ordered()]}

    def short(self) -> str:
        """Короткая подпись для кнопки на панели инструментов.

        Returns:
            str: например, ``"3НФ: соответствует"``, ``"2НФ: нарушений 3"`` или ``"НФ: нет таблиц"``.
        """
        name = LEVEL_NAMES[self.target]
        if self.tables == 0:
            return "НФ: нет таблиц"
        if self.ok:
            return f"{name}: соответствует" + (f" (подозрений {len(self.warnings)})" if self.warnings else "")
        return f"{name}: нарушений {len(self.violations)}"


# ───────────────────────── внутреннее представление таблицы ─────────────────────────

@dataclass
class _Fk:
    """Внешний ключ таблицы по нарисованной связи.

    Attributes:
        cols (list[str]): столбцы внешнего ключа (ключи сравнения; пусто, если не определены).
        parent (_Tab): родительская таблица.
        stem (str): основа имени столбца ключа (``"product"`` для ``product_id``).
    """

    cols: list[str]
    parent: "_Tab"
    stem: str


@dataclass
class _Tab:
    """Таблица в удобном для проверки виде; все имена столбцов — ключи сравнения (нижний регистр).

    Attributes:
        node (Node): таблица на диаграмме.
        title (str): название таблицы.
        names (dict[str, str]): ключ → имя столбца как оно написано.
        types (dict[str, str]): ключ → тип в верхнем регистре.
        pk (list[str]): столбцы первичного ключа.
        keys (list[frozenset[str]]): все ключи-кандидаты (объявленные PK и UNIQUE и выведенные из зависимостей).
        flagged_fk (set[str]): столбцы с меткой FK.
        fks (list[_Fk]): внешние ключи по связям.
        fds (list[tuple[frozenset[str], frozenset[str], str]]): заявленные зависимости
            ``(левая, правая, текст)``.
        derived (list[frozenset[str]]): ключи-кандидаты, следующие из зависимостей, но не объявленные
            (они добавлены в ``keys``).
    """

    node: Node
    title: str
    names: dict[str, str] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)
    pk: list[str] = field(default_factory=list)
    keys: list[frozenset[str]] = field(default_factory=list)
    flagged_fk: set[str] = field(default_factory=set)
    fks: list[_Fk] = field(default_factory=list)
    fds: list[tuple[frozenset[str], frozenset[str], str]] = field(default_factory=list)
    derived: list[frozenset[str]] = field(default_factory=list)

    @property
    def prime(self) -> frozenset[str]:
        """Простые (ключевые) столбцы — входящие хотя бы в один ключ-кандидат.

        Returns:
            frozenset[str]: объединение всех ключей-кандидатов.
        """
        return frozenset().union(*self.keys) if self.keys else frozenset()

    @property
    def fk_cols(self) -> frozenset[str]:
        """Все столбцы внешних ключей (по связям и по метке FK).

        Returns:
            frozenset[str]: ключи сравнения столбцов.
        """
        out = set(self.flagged_fk)
        for fk in self.fks:
            out.update(fk.cols)
        return frozenset(out)

    def deps(self) -> list[Dep]:
        """Заявленные зависимости без текстов.

        Returns:
            list[Dep]: пары ``(левая, правая)``.
        """
        return [(a, b) for a, b, _ in self.fds]

    def is_superkey(self, attrs: frozenset[str]) -> bool:
        """Проверяет, определяют ли столбцы всю таблицу (с учётом ключей и заявленных зависимостей).

        Args:
            attrs (frozenset[str]): проверяемые столбцы.

        Returns:
            bool: ``True``, если замыкание содержит все столбцы таблицы.
        """
        everything = frozenset(self.names)
        deps = self.deps() + [(k, everything) for k in self.keys]
        return everything <= closure(attrs, deps)

    def show(self, cols: list[str] | frozenset[str] | set[str]) -> list[str]:
        """Переводит ключи сравнения в имена столбцов как они написаны (в порядке столбцов таблицы).

        Args:
            cols (list[str] | frozenset[str] | set[str]): ключи сравнения.

        Returns:
            list[str]: имена столбцов; неизвестные ключи пропускаются.
        """
        want = set(cols)
        return [self.names[c] for c in self.names if c in want]


def _forms(prefix: str, attr: str) -> set[str]:
    """Перечисляет варианты имени «копии» атрибута родителя в дочерней таблице.

    Args:
        prefix (str): основа имени родителя (``"product"``).
        attr (str): имя атрибута родителя (``"name"``).

    Returns:
        set[str]: ``product_name``, ``productname``, ``name_product``, ``nameproduct``.
    """
    return {f"{prefix}_{attr}", f"{prefix}{attr}", f"{attr}_{prefix}", f"{attr}{prefix}"}


def _prefixes(fk: _Fk) -> set[str]:
    """Основы имён, под которыми родитель может встретиться в именах столбцов дочерней таблицы.

    Args:
        fk (_Fk): внешний ключ.

    Returns:
        set[str]: основа столбца ключа, название родителя и его единственное число.
    """
    title = key(fk.parent.title)
    out = {title}
    for tail in ("es", "s", "ы", "и", "а", "я"):
        if title.endswith(tail) and len(title) - len(tail) >= 3:
            out.add(title[:-len(tail)])
    if fk.stem:
        out.add(fk.stem)
    return out


class _Checker:
    """Выполняет все проверки одной диаграммы и накапливает замечания.

    Attributes:
        d (Diagram): проверяемая даталогическая диаграмма.
        issues (list[Issue]): найденные замечания.
        tabs (list[_Tab]): таблицы в виде, удобном для проверки.
    """

    def __init__(self, d: Diagram) -> None:
        """Создаёт проверку; сама проверка запускается методом :meth:`run`.

        Args:
            d (Diagram): даталогическая диаграмма.
        """
        self.d = d
        self.issues: list[Issue] = []
        self.tabs: list[_Tab] = []
        self._by_id: dict[str, _Tab] = {}

    def add(self, level: int, severity: str, t: Optional[_Tab], cols: list[str], rule: str, message: str,
            hint: str = "") -> None:
        """Добавляет замечание.

        Args:
            level (int): уровень (0–4).
            severity (str): ``"error"`` или ``"warn"``.
            t (Optional[_Tab]): таблица замечания (``None`` — замечание ко всей схеме).
            cols (list[str]): затронутые столбцы (имена как написаны).
            rule (str): короткое название правила.
            message (str): что не так.
            hint (str): как исправить.
        """
        self.issues.append(Issue(level, severity, t.node.id if t else "", t.title if t else "", cols, rule, message, hint))

    def run(self) -> list[Issue]:
        """Запускает все проверки.

        Returns:
            list[Issue]: замечания всех уровней.
        """
        self._build()
        self._links()
        for t in self.tabs:
            self._level1(t)
            self._level2(t)
            self._level3(t)
            self._level4(t)
        return self.issues

    # ----- разбор диаграммы -----

    def _build(self) -> None:
        """Строит внутренние таблицы по узлам диаграммы и сразу находит ошибки схемы (уровень 0)."""
        seen: set[str] = set()
        for n in self.d.nodes:
            title = n.title.strip()
            t = _Tab(n, title or "(без названия)")
            if not title:
                self.add(STRUCTURE, ERROR, t, [], "Нет названия", "У таблицы нет названия.", "Дважды кликните заголовок таблицы.")
            elif key(title) in seen:
                self.add(STRUCTURE, ERROR, t, [], "Повтор названия", f"Две таблицы называются «{title}».", "Переименуйте одну из них.")
            seen.add(key(title))
            singles: list[str] = []
            for r in n.rows:
                name = r.name.strip()
                if not name:
                    self.add(STRUCTURE, ERROR, t, [], "Столбец без имени", "В таблице есть столбец без имени.", "Задайте имя или удалите строку.")
                    continue
                k = key(name)
                if k in t.names:
                    self.add(1, ERROR, t, [name], "Повтор столбца", f"Столбец «{name}» объявлен дважды.",
                             "Оставьте один столбец или переименуйте.")
                    continue
                t.names[k], t.types[k] = name, r.type.strip().upper()
                toks = key_tokens(r.key)
                if "PK" in toks:
                    t.pk.append(k)
                if "FK" in toks:
                    t.flagged_fk.add(k)
                if "U" in toks:
                    singles.append(k)
            if not t.names:
                self.add(STRUCTURE, ERROR, t, [], "Нет столбцов", "В таблице нет ни одного столбца.", "Добавьте столбцы кнопкой «+ Поле».")
            if t.pk:
                t.keys.append(frozenset(t.pk))
            for g in n.uniques:
                ks = [key(c) for c in g]
                if ks and all(c in t.names for c in ks):
                    t.keys.append(frozenset(ks))
                else:
                    self.add(STRUCTURE, ERROR, t, list(g), "UNIQUE", f"В UNIQUE({', '.join(g)}) есть несуществующий столбец.",
                             "Поправьте поле UNIQUE в панели таблицы.")
            t.keys.extend(frozenset([k]) for k in singles)
            for text in n.fds:
                self._read_fd(t, text)
            self._derive(t)
            self.tabs.append(t)
            self._by_id[n.id] = t

    def _derive(self, t: _Tab) -> None:
        """Добавляет в таблицу ключи-кандидаты, которые следуют из зависимостей, но не объявлены, и предупреждает о них.

        Без объявленного ключа вывод не выполняется (об отсутствии ключа сообщает проверка 1НФ).

        Args:
            t (_Tab): таблица с уже разобранными ключами и зависимостями.
        """
        if not t.keys or not t.fds:
            return
        known = set(t.keys)
        t.derived = [k for k in derive_keys(list(t.names), t.keys, t.deps()) if k not in known]
        t.keys.extend(t.derived)
        for k in t.derived:
            cols = t.show(k)
            self.add(1, WARN, t, cols, "Необъявленный ключ",
                     f"Из записанных зависимостей следует, что {', '.join(cols)} — ключ-кандидат таблицы «{t.title}», но он не объявлен.",
                     f"Отметьте {', '.join(cols)} как UNIQUE, иначе база данных не обеспечит его уникальность.")

    def _read_fd(self, t: _Tab, text: str) -> None:
        """Разбирает заявленную зависимость таблицы; ошибки записи — замечания уровня 0.

        Args:
            t (_Tab): таблица.
            text (str): запись вида ``a, b -> c``.
        """
        fd = parse_fd(text)
        if fd is None:
            self.add(STRUCTURE, ERROR, t, [], "Зависимость", f"Не удалось разобрать зависимость «{text}».",
                     "Формат записи: левая часть -> правая часть, например город -> страна.")
            return
        missing = [c for c in fd.lhs + fd.rhs if key(c) not in t.names]
        if missing:
            self.add(STRUCTURE, ERROR, t, [], "Зависимость", f"В зависимости «{fd.text()}» нет столбцов: {', '.join(missing)}.",
                     "Исправьте имена или добавьте столбцы.")
            return
        t.fds.append((frozenset(key(c) for c in fd.lhs), frozenset(key(c) for c in fd.rhs), fd.text()))

    def _links(self) -> None:
        """Разбирает связи как внешние ключи и проверяет их согласованность (уровень 0)."""
        claimed: dict[str, set[str]] = {t.node.id: set() for t in self.tabs}
        pending: list[tuple[_Tab, _Tab, list[str]]] = []
        for l in self.d.links:
            child, parent = self._by_id.get(l.a), self._by_id.get(l.b)
            if child is None or parent is None:
                continue
            label = f"связь «{child.title}» → «{parent.title}»"
            ref = [key(c) for c in l.ref_cols if key(c) in parent.names] or list(parent.pk)
            if not ref:
                self.add(STRUCTURE, ERROR, parent, [], "Нет ключа у родителя",
                         f"{label}: у таблицы «{parent.title}» нет первичного ключа, на неё нельзя сослаться.",
                         "Отметьте ключевой столбец меткой PK.")
                continue
            cols = [key(c) for c in l.fk_cols if key(c) in child.names]
            if l.fk_cols and len(cols) != len(l.fk_cols):
                self.add(STRUCTURE, ERROR, child, list(l.fk_cols), "Нет столбца FK", f"{label}: столбец внешнего ключа не найден в «{child.title}».",
                         "Пересоздайте связь или верните столбец.")
                continue
            if not cols:
                pending.append((child, parent, ref))
                continue
            if len(cols) != len(ref):
                self.add(STRUCTURE, ERROR, child, child.show(cols), "Ключ не совпадает",
                         f"{label}: в ключе {len(cols)} столбцов, а у родителя {len(ref)}.", "Пересоздайте связь.")
                continue
            claimed[child.node.id].update(cols)
            child.fks.append(_Fk(cols, parent, id_stem(child.names[cols[0]])))
            for c, p in zip(cols, ref):
                tc, tp = _canon_type(child.types[c]), _canon_type(parent.types[p])
                if tc and tp and tc != tp:
                    self.add(STRUCTURE, WARN, child, [child.names[c]], "Тип FK",
                             f"{label}: тип «{child.names[c]}» ({child.types[c]}) не совпадает с типом «{parent.names[p]}» ({parent.types[p]}).",
                             "Сделайте типы одинаковыми.")
        for child, parent, ref in pending:
            free = [c for c in child.flagged_fk if c not in claimed[child.node.id]]
            if len(free) == len(ref) == 1:
                claimed[child.node.id].update(free)
                child.fks.append(_Fk(free, parent, id_stem(child.names[free[0]])))
            else:
                child.fks.append(_Fk([], parent, ""))

    # ----- 1НФ -----

    def _level1(self, t: _Tab) -> None:
        """Проверки 1НФ: первичный ключ, типы, повторяющиеся группы, списки в столбце.

        Args:
            t (_Tab): проверяемая таблица.
        """
        if t.names and not t.pk:
            self.add(1, ERROR, t, [], "Нет первичного ключа", f"У таблицы «{t.title}» нет первичного ключа: строки нельзя различить.",
                     "Отметьте столбец (или несколько) меткой PK.")
        groups: dict[str, list[str]] = {}
        for k, name in t.names.items():
            ty = t.types[k]
            if not ty:
                self.add(1, WARN, t, [name], "Тип не указан", f"У столбца «{name}» не указан тип.",
                         "Укажите тип: INTEGER, TEXT, REAL, DATE…")
            elif _ATOMIC_BAD_TYPE.search(ty):
                self.add(1, ERROR, t, [name], "Неатомарный тип", f"Тип {ty} столбца «{name}» хранит несколько значений в одной ячейке.",
                         "Вынесите значения в отдельную таблицу со связью один-ко-многим.")
            m = _NUM_SUFFIX.match(k)
            if m and len(m.group(1)) >= 2:
                groups.setdefault(m.group(1), []).append(k)
            if _LIST_NAME.search(k) and not _ATOMIC_BAD_TYPE.search(ty):
                self.add(1, WARN, t, [name], "Список в столбце", f"Имя «{name}» похоже на список значений в одной ячейке.",
                         "Если здесь несколько значений — вынесите их в отдельную таблицу.")
            elif k in _COMPOSITE_NAMES and ty in ("", "TEXT", "VARCHAR", "STRING", "CHAR"):
                self.add(1, WARN, t, [name], "Составное значение", f"«{name}» обычно содержит несколько частей (атомарность под вопросом).",
                         "Разбейте на отдельные столбцы (например, фамилия, имя, отчество).")
        for stem, ks in groups.items():
            if len(ks) >= 2:
                cols = t.show(ks)
                self.add(1, ERROR, t, cols, "Повторяющаяся группа",
                         f"Столбцы {', '.join(cols)} повторяют один и тот же атрибут.",
                         "Вынесите их в отдельную таблицу и свяжите с этой (один-ко-многим).")

    # ----- 2НФ -----

    def _level2(self, t: _Tab) -> None:
        """Проверки 2НФ: зависимость от части составного ключа (заявленная и по именам).

        Args:
            t (_Tab): проверяемая таблица.
        """
        prime = t.prime
        done: list[tuple[frozenset[str], frozenset[str]]] = []
        for k in t.keys:
            if len(k) < 2 or len(k) > 8:
                continue
            cols = sorted(k)
            for size in range(1, len(cols)):
                for sub in combinations(cols, size):
                    s = frozenset(sub)
                    bad = frozenset(a for a in closure(s, t.deps()) - s if a not in prime)
                    if not bad or any(s0 <= s and bad <= b0 for s0, b0 in done):
                        continue
                    done.append((s, bad))
                    self.add(2, ERROR, t, t.show(s | bad), "Зависимость от части ключа",
                             f"{_plural(len(bad), 'Столбец', 'Столбцы')} {', '.join(t.show(bad))} "
                             f"{_plural(len(bad), 'зависит', 'зависят')} только от {', '.join(t.show(s))}, "
                             f"а не от всего ключа ({', '.join(t.show(k))}).",
                             f"Вынесите {', '.join(t.show(s | bad))} в отдельную таблицу с ключом {', '.join(t.show(s))}.")
        reported = self._copies(t) | self._described(t)
        composite = [k for k in t.keys if len(k) > 1]
        for k in composite:
            for kc in sorted(k):
                stem = id_stem(t.names[kc])
                if len(stem) < 3:
                    continue
                for c in t.names:
                    if c in prime or c in reported or c in t.fk_cols or not c.startswith(stem + "_"):
                        continue
                    reported.add(c)
                    self.add(2, WARN, t, [t.names[c], t.names[kc]], "Возможная частичная зависимость",
                             f"«{t.names[c]}» по имени относится к «{t.names[kc]}», то есть к части ключа ({', '.join(t.show(k))}).",
                             "Если значение определяется одним «" + t.names[kc] + "», вынесите его в таблицу этого объекта.")

    def _described(self, t: _Tab) -> set[str]:
        """Собирает столбцы таблицы, по которым уже есть замечания уровней 2 и 3.

        Args:
            t (_Tab): таблица.

        Returns:
            set[str]: ключи сравнения столбцов (по ним слабые признаки не повторяются).
        """
        return {key(c) for i in self.issues if i.node_id == t.node.id and i.level in (2, 3) for c in i.columns}

    def _copies(self, t: _Tab) -> set[str]:
        """Ищет столбцы, копирующие атрибуты родительской таблицы, и сообщает о них.

        Если столбец внешнего ключа — часть составного ключа, копия даёт нарушение 2НФ; если неключевой
        столбец — нарушение 3НФ. Столбец с точно таким же именем, как у атрибута родителя, считается подозрением.

        Args:
            t (_Tab): проверяемая таблица.

        Returns:
            set[str]: ключи уже описанных столбцов (другие проверки их пропускают).
        """
        reported: set[str] = set()
        prime = t.prime
        for fk in t.fks:
            if not fk.cols:
                continue
            fkset = frozenset(fk.cols)
            partial = any(fkset <= k and fkset != k for k in t.keys)
            if not partial and fkset <= prime:
                continue
            parent = fk.parent
            skip = parent.prime | parent.fk_cols
            attrs = [a for a in parent.names if a not in skip]
            for c in t.names:
                if c in prime or c in fk.cols or c in reported or c in t.fk_cols:
                    continue
                hit = next((a for a in attrs for p in _prefixes(fk) if a != c and c in _forms(p, a)), None)
                weak = c in attrs and c not in GENERIC_ATTRS and parent is not t
                if hit is None and not weak:
                    continue
                reported.add(c)
                level = 2 if partial else 3
                owner = ", ".join(t.show(fkset))
                if hit is not None:
                    where = "частью ключа" if partial else "неключевым столбцом"
                    self.add(level, ERROR, t, [t.names[c], *t.show(fkset)], "Копия атрибута родителя",
                             f"«{t.names[c]}» дублирует атрибут «{parent.names[hit]}» таблицы «{parent.title}»: "
                             f"значение определяется {where} {owner}, а не ключом «{t.title}».",
                             f"Удалите «{t.names[c]}» и берите значение из «{parent.title}» по связи.")
                else:
                    self.add(level, WARN, t, [t.names[c], *t.show(fkset)], "Возможная копия атрибута",
                             f"«{t.names[c]}» называется так же, как атрибут таблицы «{parent.title}», связанной через {owner}.",
                             "Если это не значение «на момент события» (например, цена в заказе), возьмите его из родительской таблицы.")
        return reported

    # ----- 3НФ -----

    def _level3(self, t: _Tab) -> None:
        """Проверки 3НФ: транзитивные зависимости (заявленные, копии родителя, пары ``X_id`` + ``X_name``, иерархии).

        Args:
            t (_Tab): проверяемая таблица.
        """
        prime = t.prime
        for lhs, rhs, text in t.fds:
            if t.is_superkey(lhs) or any(lhs < k for k in t.keys):
                continue
            bad = frozenset(a for a in rhs - lhs if a not in prime)
            if bad:
                self.add(3, ERROR, t, t.show(lhs | bad), "Транзитивная зависимость",
                         f"{', '.join(t.show(lhs))} не является ключом, но определяет {', '.join(t.show(bad))} (зависимость «{text}»); "
                         f"значит, {', '.join(t.show(bad))} зависит от ключа через {', '.join(t.show(lhs))}.",
                         f"Вынесите {', '.join(t.show(lhs | bad))} в отдельную таблицу с ключом {', '.join(t.show(lhs))} и свяжите стрелкой.")
        reported = self._described(t)
        for k, name in t.names.items():
            stem = id_stem(name)
            if not stem or k in prime or k in reported:
                continue
            for w in sorted(NAME_WORDS):
                for cand in (f"{stem}_{w}", f"{stem}{w}", f"{w}_{stem}"):
                    if cand in t.names and cand not in prime and cand not in reported:
                        reported.update({k, cand})
                        self.add(3, ERROR, t, [name, t.names[cand]], "Транзитивная зависимость",
                                 f"«{t.names[cand]}» определяется столбцом «{name}», а не ключом «{t.title}».",
                                 f"Вынесите «{name}» и «{t.names[cand]}» в отдельную таблицу и свяжите стрелкой.")
        low = {k: name for k, name in t.names.items() if k not in prime}
        for lower, upper in HIERARCHIES:
            a = next((k for k in low if k in lower), None)
            b = next((k for k in low if k in upper), None)
            if a and b and a != b and not {a, b} <= reported and not (t.is_superkey(frozenset([a])) or t.is_superkey(frozenset([b]))):
                self.add(3, WARN, t, [low[a], low[b]], "Возможная зависимость между атрибутами",
                         f"«{low[a]}» обычно определяет «{low[b]}» — получается транзитивная зависимость от ключа.",
                         f"Если так, запишите в поле «Зависимости»: {low[a]} -> {low[b]}, и вынесите их в справочник.")

    # ----- НФБК -----

    def _level4(self, t: _Tab) -> None:
        """Проверка НФБК: у каждой заявленной зависимости левая часть должна быть ключом.

        Нарушения с неключевой правой частью уже описаны на уровнях 2 и 3; здесь добавляются
        только зависимости, определяющие ключевые столбцы.

        Args:
            t (_Tab): проверяемая таблица.
        """
        prime = t.prime
        for lhs, rhs, text in t.fds:
            if t.is_superkey(lhs):
                continue
            bad = frozenset(a for a in rhs - lhs if a in prime)
            if bad:
                self.add(4, ERROR, t, t.show(lhs | bad), "Левая часть не ключ",
                         f"В зависимости «{text}» левая часть {', '.join(t.show(lhs))} не является ключом, "
                         f"но определяет ключевой столбец {', '.join(t.show(bad))}.",
                         "Разбейте таблицу так, чтобы левая часть стала ключом новой таблицы.")


def _canon_type(t: str) -> str:
    """Приводит тип столбца к семейству для сравнения типов ключа и внешнего ключа.

    Args:
        t (str): тип в верхнем регистре (может быть пустым).

    Returns:
        str: ``"INT"``, ``"TEXT"``, ``"REAL"`` или исходный тип; пустая строка, если тип не задан.
    """
    if not t:
        return ""
    base = re.split(r"[\s(]", t, maxsplit=1)[0]
    if "INT" in base:
        return "INT"
    if any(w in base for w in ("CHAR", "TEXT", "CLOB", "STRING")):
        return "TEXT"
    if any(w in base for w in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
        return "REAL"
    return base


def check(d: Diagram, level: int = 3) -> Report:
    """Проверяет даталогическую диаграмму на соответствие нормальной форме.

    Диаграмму функция не меняет. Замечания собираются по **всем** уровням, чтобы можно было
    сказать, какая форма достигнута (:attr:`Report.achieved`), а вердикт строится по ``level``.

    Args:
        d (Diagram): даталогическая диаграмма (``KIND_DATA``).
        level (int): проверяемая форма: 1 — 1НФ, 2 — 2НФ, 3 — 3НФ, 4 — НФБК.

    Returns:
        Report: отчёт с замечаниями и вердиктом.

    Raises:
        ValueError: если ``level`` не из ``LEVELS``.
    """
    if level not in LEVELS:
        raise ValueError(f"нормальная форма должна быть 1, 2, 3 или 4, а не {level}")
    checker = _Checker(d)
    issues = checker.run()
    return Report(level, len(checker.tabs), issues)


def fingerprint(d: Diagram, level: int) -> tuple[object, ...]:
    """Строит отпечаток частей диаграммы, влияющих на результат проверки (для кэша результата).

    Положение и размеры узлов, цвета и подписи связей в отпечаток не входят.

    Args:
        d (Diagram): даталогическая диаграмма.
        level (int): проверяемая форма.

    Returns:
        tuple[object, ...]: сравнимый кортеж; совпадение двух отпечатков означает тот же результат проверки.
    """
    nodes = tuple((n.id, n.title, tuple((r.name, r.type, r.key, r.notnull) for r in n.rows),
                   tuple(tuple(u) for u in n.uniques), tuple(n.fds)) for n in d.nodes)
    links = tuple((l.id, l.a, l.b, tuple(l.fk_cols), tuple(l.ref_cols)) for l in d.links)
    return (level, nodes, links)


def format_markdown(report: Report, project_name: str = "") -> str:
    """Оформляет отчёт о проверке в виде Markdown-документа.

    Args:
        report (Report): результат :func:`check`.
        project_name (str): название проекта для заголовка (может быть пустым).

    Returns:
        str: текст документа: вердикт, правила формы, таблица замечаний.
    """
    name = LEVEL_NAMES[report.target]
    lines = [f"# Проверка нормальной формы: {name}" + (f" — {project_name}" if project_name else ""), "",
             f"**{report.verdict()}.**", ""]
    if report.tables:
        achieved = report.achieved
        lines += [f"Достигнута форма: {LEVEL_NAMES[achieved] if achieved else 'ни одна (нужно исправить ошибки схемы или 1НФ)'}.", ""]
    lines += [f"Требования {name}: {LEVEL_RULES[report.target]}.", ""]
    items = report.ordered()
    if not items:
        lines.append("Замечаний нет.")
    else:
        lines += ["| Тип | Правило | Таблица | Столбцы | Что не так | Как исправить |", "|---|---|---|---|---|---|"]
        for i in items:
            kind = "нарушение" if i.severity == ERROR else "подозрение"
            row = [kind, f"{LEVEL_NAMES[i.level]}: {i.rule}", i.table or "—", ", ".join(i.columns) or "—", i.message, i.hint or "—"]
            lines.append("| " + " | ".join(c.replace("|", "/") for c in row) + " |")
    return "\n".join(lines) + "\n"

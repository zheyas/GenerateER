"""Модель данных редактора: проект, две диаграммы, узлы, связи, легенда, надписи.

Модуль не зависит от pygame, поэтому его можно использовать и тестировать отдельно.

Структура::

    Project
      └─ diagrams: {"info": Diagram, "data": Diagram}
            ├─ nodes:  list[Node]   (сущности / таблицы), у каждого rows: list[Row]
            ├─ links:  list[Link]   (связи: a — сторона «много», b — главная сторона)
            ├─ legend: Legend       (items: list[LegendItem])
            └─ notes:  list[Note]   (свободные надписи)

Все координаты и размеры — в «мировых» единицах (при масштабе 100 % одна единица = один пиксель).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional, TypeVar

T = TypeVar("T")
JsonDict = dict[str, Any]

KIND_INFO = "info"
KIND_DATA = "data"
KIND_TITLES: dict[str, str] = {KIND_INFO: "Инфологическая (Баркер)", KIND_DATA: "Даталогическая"}

#: Размеры узлов в мировых единицах: высота шапки, высота строки, нижний отступ, минимальная ширина.
STYLE: dict[str, dict[str, int]] = {
    KIND_INFO: dict(head=38, row=19, pad=10, min_w=200),
    KIND_DATA: dict(head=30, row=22, pad=6, min_w=220),
}

#: Палитра узлов: (цвет шапки, цвет тела) в hex. Индекс хранится в ``Node.color``.
PALETTE: list[tuple[str, str]] = [
    ("#e4e9ef", "#ffffff"),
    ("#cfe3fb", "#f4f9ff"),
    ("#d4efdc", "#f5fbf7"),
    ("#faeec2", "#fffdf3"),
    ("#f8d4d4", "#fff6f6"),
    ("#e2d8f5", "#f8f5fd"),
]

#: Виды пунктов легенды и их названия в интерфейсе.
LEGEND_KINDS: dict[str, str] = {
    "mark_#": "значок #",
    "mark_*": "значок *",
    "mark_o": "значок o",
    "solid": "сплошная линия",
    "dashed": "пунктирная линия",
    "crow": "«воронья лапка»",
    "arrow": "стрелка (FK → PK)",
    "one": "один (простой конец)",
    "bar": "линия с чертой (UID)",
    "text": "только текст",
}


def node_height(kind: str, n_rows: int) -> float:
    """Вычисляет высоту узла диаграммы.

    Args:
        kind (str): вид диаграммы — ``KIND_INFO`` или ``KIND_DATA``.
        n_rows (int): количество строк (атрибутов или столбцов).

    Returns:
        float: высота в мировых единицах: шапка + строки + нижний отступ.

    Raises:
        KeyError: если ``kind`` — неизвестный вид диаграммы.
    """
    s = STYLE[kind]
    return s["head"] + n_rows * s["row"] + s["pad"]


@dataclass
class Row:
    """Строка узла: атрибут сущности или столбец таблицы.

    Attributes:
        name (str): имя атрибута / столбца.
        mark (str): (инфологическая) ``"#"`` — входит в UID, ``"*"`` — обязательный,
            ``"o"`` — необязательный.
        type (str): (даталогическая) тип столбца, например ``"INTEGER"``.
        key (str): (даталогическая) метки ключей через запятую: ``"PK"``, ``"FK"``,
            ``"PK,FK"``, ``"U"``.
        notnull (bool): (даталогическая) признак NOT NULL.
        default (str): (даталогическая) значение ``DEFAULT``: число, строка в апострофах или слово ``NULL`` /
            ``CURRENT_TIMESTAMP`` (пусто — без значения по умолчанию); нужно, в частности, для ``ON DELETE SET DEFAULT``.
    """

    name: str = "поле"
    mark: str = "o"
    type: str = ""
    key: str = ""
    notnull: bool = False
    default: str = ""


@dataclass
class Node:
    """Узел диаграммы: сущность (инфологическая) или таблица (даталогическая).

    Attributes:
        id (str): уникальный в пределах диаграммы идентификатор (``"n1"``, ``"n2"``…).
        title (str): заголовок.
        x (float): абсцисса левого верхнего угла.
        y (float): ордината левого верхнего угла.
        w (float): ширина (высота вычисляется по числу строк, см. :func:`node_height`).
        rows (list[Row]): строки узла.
        color (int): индекс цвета в :data:`PALETTE`.
        table (str): имя исходной таблицы БД (справочно, для импортированных узлов).
        uniques (list[list[str]]): составные (и одиночные) ограничения UNIQUE таблицы — списки
            имён столбцов (даталогическая; используется при создании базы данных).
        fds (list[str]): заявленные функциональные зависимости таблицы в виде ``"a, b -> c"``
            (даталогическая; используются при проверке нормальной формы).
    """

    id: str
    title: str
    x: float = 0.0
    y: float = 0.0
    w: float = 240.0
    rows: list[Row] = field(default_factory=list)
    color: int = 0
    table: str = ""
    uniques: list[list[str]] = field(default_factory=list)
    fds: list[str] = field(default_factory=list)


@dataclass
class Link:
    """Связь между двумя узлами.

    Конец **A** — подчинённая сторона («много», где лежит внешний ключ), конец **B** — главная.

    Attributes:
        id (str): идентификатор (``"l1"``…).
        a (str): идентификатор узла на конце A.
        b (str): идентификатор узла на конце B.
        sa (str): сторона узла A, к которой привязан конец:
            ``"top" | "right" | "bottom" | "left"``.
        ta (float): положение точки привязки на стороне ``sa``, доля длины (0..1).
        sb (str): сторона узла B.
        tb (float): положение точки привязки на стороне ``sb``, доля длины (0..1).
        card_a (str): кратность у A: ``"M"`` — «воронья лапка» (много), ``"1"`` — один.
        card_b (str): кратность у B.
        must_a (bool): половина линии у A сплошная («должен») или пунктирная («может»).
        must_b (bool): то же у B.
        bar_a (bool): черта поперёк линии у A — связь входит в UID сущности (инфологическая).
        bar_b (bool): то же у B.
        name_a (str): имя связи у A (инфологическая).
        name_b (str): имя связи у B (инфологическая).
        label (str): подпись посередине линии (даталогическая: правило ``ON DELETE``).
        mid (Optional[float]): положение среднего звена Z-образного маршрута (``None`` —
            автоматически).
        off_a (list[float]): ручной сдвиг подписи у A, ``[dx, dy]``.
        off_b (list[float]): ручной сдвиг подписи у B.
        off_c (list[float]): ручной сдвиг центральной подписи.
        row_a (Optional[int]): подсказка автораскладки — индекс строки внешнего ключа в узле A.
        row_b (Optional[int]): подсказка автораскладки — индекс строки первичного ключа в узле B.
        fk_cols (list[str]): (даталогическая) имена столбцов внешнего ключа в узле A.
        ref_cols (list[str]): (даталогическая) имена столбцов узла B, на которые ссылается ключ.
            Пустые списки — программа попробует определить столбцы по именам при создании БД.
    """

    id: str
    a: str
    b: str
    sa: str = "right"
    ta: float = 0.5
    sb: str = "left"
    tb: float = 0.5
    card_a: str = "M"
    card_b: str = "1"
    must_a: bool = True
    must_b: bool = False
    bar_a: bool = False
    bar_b: bool = False
    name_a: str = ""
    name_b: str = ""
    label: str = ""
    mid: Optional[float] = None
    off_a: list[float] = field(default_factory=lambda: [0.0, 0.0])
    off_b: list[float] = field(default_factory=lambda: [0.0, 0.0])
    off_c: list[float] = field(default_factory=lambda: [0.0, 0.0])
    row_a: Optional[int] = None
    row_b: Optional[int] = None
    fk_cols: list[str] = field(default_factory=list)
    ref_cols: list[str] = field(default_factory=list)


@dataclass
class LegendItem:
    """Пункт легенды.

    Attributes:
        kind (str): вид значка — один из ключей :data:`LEGEND_KINDS`.
        text (str): текст пункта.
    """

    kind: str = "text"
    text: str = ""


@dataclass
class Legend:
    """Легенда диаграммы: рамка с заголовком и пунктами.

    Attributes:
        x (float): абсцисса левого верхнего угла.
        y (float): ордината левого верхнего угла.
        w (float): минимальная ширина (рамка расширяется под длинный текст).
        title (str): заголовок.
        items (list[LegendItem]): пункты.
        visible (bool): показывать ли легенду.
    """

    x: float = 0.0
    y: float = 0.0
    w: float = 280.0
    title: str = "Условные обозначения"
    items: list[LegendItem] = field(default_factory=list)
    visible: bool = True


@dataclass
class Note:
    """Свободная текстовая надпись на холсте.

    Attributes:
        id (str): идентификатор (``"t1"``…).
        x (float): абсцисса.
        y (float): ордината.
        text (str): текст.
        size (int): размер шрифта.
    """

    id: str
    x: float = 0.0
    y: float = 0.0
    text: str = "Надпись"
    size: int = 14


def default_legend(kind: str, arrows: bool = False) -> Legend:
    """Создаёт стандартную легенду для вида диаграммы.

    Args:
        kind (str): ``KIND_INFO`` — обозначения нотации Баркера; любое другое значение
            (``KIND_DATA``) — обозначения ключей, участия и ``ON DELETE``.
        arrows (bool): (даталогическая) связи нарисованы стрелками, а не «вороньей лапкой».

    Returns:
        Legend: новая легенда с набором пунктов по умолчанию.
    """
    if kind == KIND_INFO:
        items = [
            ("mark_#", "атрибут уникального идентификатора (UID)"),
            ("mark_*", "обязательный атрибут"),
            ("mark_o", "необязательный атрибут"),
            ("solid", "должен (обязательно)"),
            ("dashed", "может (необязательно)"),
            ("crow", "один или несколько"),
            ("one", "один и только один"),
            ("bar", "связь входит в UID"),
        ]
    else:
        items = [
            ("text", "PK — первичный ключ;  FK — внешний ключ"),
            ("text", "U — входит в ограничение UNIQUE"),
            ("solid", "FK обязателен (NOT NULL)"),
            ("dashed", "у родителя может не быть строк"),
            *([("arrow", "стрелка от внешнего ключа к первичному"),
               ("text", "N — «много», 1 — «один» у концов связи")] if arrows else
              [("crow", "много (сторона внешнего ключа)"),
               ("one", "один (сторона первичного ключа)")]),
            ("text", "подпись на линии — правило ON DELETE"),
        ]
    return Legend(items=[LegendItem(k, t) for k, t in items], w=300 if kind == KIND_DATA else 280)


@dataclass
class Diagram:
    """Одна диаграмма: узлы, связи, легенда и надписи.

    Attributes:
        kind (str): ``KIND_INFO`` или ``KIND_DATA``.
        nodes (list[Node]): сущности / таблицы.
        links (list[Link]): связи.
        legend (Legend): легенда.
        notes (list[Note]): свободные надписи.
        counter (int): счётчик для генерации идентификаторов (см. :meth:`new_id`).
        arrows (bool): (даталогическая) рисовать связи стрелками «внешний ключ → первичный ключ»,
            а не «воронья лапка».
    """

    kind: str
    nodes: list[Node] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    legend: Legend = field(default_factory=Legend)
    notes: list[Note] = field(default_factory=list)
    counter: int = 0
    arrows: bool = False

    def new_id(self, prefix: str) -> str:
        """Выдаёт новый уникальный идентификатор и увеличивает счётчик.

        Args:
            prefix (str): ``"n"`` для узлов, ``"l"`` для связей, ``"t"`` для надписей.

        Returns:
            str: идентификатор вида ``prefix + номер``, например ``"n7"``.
        """
        self.counter += 1
        return f"{prefix}{self.counter}"

    def node(self, nid: str) -> Optional[Node]:
        """Находит узел по идентификатору.

        Args:
            nid (str): идентификатор узла.

        Returns:
            Optional[Node]: узел или ``None``, если такого нет.
        """
        return next((n for n in self.nodes if n.id == nid), None)

    def link(self, lid: str) -> Optional[Link]:
        """Находит связь по идентификатору.

        Args:
            lid (str): идентификатор связи.

        Returns:
            Optional[Link]: связь или ``None``, если такой нет.
        """
        return next((l for l in self.links if l.id == lid), None)

    def note(self, nid: str) -> Optional[Note]:
        """Находит надпись по идентификатору.

        Args:
            nid (str): идентификатор надписи.

        Returns:
            Optional[Note]: надпись или ``None``, если такой нет.
        """
        return next((n for n in self.notes if n.id == nid), None)

    def links_of(self, nid: str) -> list[Link]:
        """Собирает все связи, у которых узел находится на любом из концов.

        Args:
            nid (str): идентификатор узла.

        Returns:
            list[Link]: связи узла (пустой список, если связей нет).
        """
        return [l for l in self.links if l.a == nid or l.b == nid]

    def remove_node(self, nid: str) -> None:
        """Удаляет узел вместе со всеми его связями (несуществующий узел игнорируется).

        Args:
            nid (str): идентификатор узла.
        """
        self.nodes = [n for n in self.nodes if n.id != nid]
        self.links = [l for l in self.links if l.a != nid and l.b != nid]

    def to_dict(self) -> JsonDict:
        """Преобразует диаграмму во вложенные словари.

        Returns:
            JsonDict: словарь, пригодный для ``json.dumps`` (все вложенные датаклассы развёрнуты).
        """
        return asdict(self)

    @staticmethod
    def from_dict(d: JsonDict) -> "Diagram":
        """Восстанавливает диаграмму из словаря, созданного :meth:`to_dict`.

        Неизвестные ключи игнорируются (совместимость версий файла); отсутствующая или пустая
        легенда заменяется стандартной.

        Args:
            d (JsonDict): словарь диаграммы; обязателен ключ ``"kind"``.

        Returns:
            Diagram: восстановленная диаграмма.

        Raises:
            KeyError: если в ``d`` нет ключа ``"kind"`` или у узла / связи нет обязательного поля.
            TypeError: если у узла есть неизвестное поле (узлы создаются без фильтрации ключей).
        """
        return Diagram(
            kind=d["kind"],
            nodes=[Node(**{**n, "rows": [_make(Row, r) for r in n.get("rows", [])]}) for n in d.get("nodes", [])],
            links=[_make(Link, l) for l in d.get("links", [])],
            legend=Legend(**{**d["legend"], "items": [_make(LegendItem, i) for i in d["legend"].get("items", [])]})
            if d.get("legend") else default_legend(d["kind"]),
            notes=[_make(Note, n) for n in d.get("notes", [])],
            counter=d.get("counter", 0),
            arrows=d.get("arrows", False),
        )


def _make(cls: type[T], data: JsonDict) -> T:
    """Создаёт датакласс из словаря, отбрасывая неизвестные ключи.

    Args:
        cls (type[T]): класс датакласса.
        data (JsonDict): значения полей (лишние ключи игнорируются).

    Returns:
        T: экземпляр ``cls``.

    Raises:
        TypeError: если не хватает обязательного поля датакласса.
    """
    names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class Project:
    """Проект редактора: обе диаграммы и общие настройки.

    Attributes:
        db_path (str): путь к исходной базе данных SQLite (может быть пустым).
        diagrams (dict[str, Diagram]): ``{KIND_INFO: Diagram, KIND_DATA: Diagram}``.
        export_scale (int): множитель разрешения PNG при экспорте (1, 2 или 3).
        grid (bool): показывать сетку и привязывать перетаскиваемые объекты к ней.
        fill_sample (bool): при создании базы данных добавлять в каждую таблицу случайные записи.
        normal_form (int): выбранная для проверки нормальная форма: 1, 2 или 3.
    """

    db_path: str = ""
    diagrams: dict[str, Diagram] = field(default_factory=dict)
    export_scale: int = 2
    grid: bool = True
    fill_sample: bool = False
    normal_form: int = 3

    def to_json(self) -> str:
        """Сериализует проект в компактную JSON-строку (используется и для снимков undo/redo).

        Returns:
            str: JSON в UTF-8 без экранирования кириллицы.
        """
        return json.dumps({
            "version": 1,
            "db_path": self.db_path,
            "export_scale": self.export_scale,
            "grid": self.grid,
            "fill_sample": self.fill_sample,
            "normal_form": self.normal_form,
            "diagrams": {k: d.to_dict() for k, d in self.diagrams.items()},
        }, ensure_ascii=False)

    @staticmethod
    def from_json(text: str) -> "Project":
        """Разбирает строку, созданную :meth:`to_json`.

        Args:
            text (str): JSON-текст проекта.

        Returns:
            Project: восстановленный проект.

        Raises:
            json.JSONDecodeError: если текст не является корректным JSON.
            KeyError: если в JSON нет раздела ``"diagrams"``.
        """
        return Project.from_dict(json.loads(text))

    @staticmethod
    def from_dict(d: JsonDict) -> "Project":
        """Восстанавливает проект из словаря (разобранного JSON проекта).

        Args:
            d (JsonDict): словарь вида, который выдаёт :meth:`to_json`.

        Returns:
            Project: восстановленный проект.

        Raises:
            KeyError: если в словаре нет раздела ``"diagrams"`` или у вложенного объекта нет обязательного поля.
            TypeError: если у узла есть неизвестное поле или значение не словарь.
        """
        return Project(
            db_path=d.get("db_path", ""),
            export_scale=d.get("export_scale", 2),
            grid=d.get("grid", True),
            fill_sample=d.get("fill_sample", False),
            normal_form=d.get("normal_form", 3),
            diagrams={k: Diagram.from_dict(v) for k, v in d["diagrams"].items()},
        )

    def save(self, path: str) -> None:
        """Записывает проект в файл: JSON с отступами, UTF-8.

        Args:
            path (str): путь к файлу ``.erd.json`` (перезаписывается).

        Raises:
            OSError: если файл нельзя создать или записать (нет прав, нет папки, нет места).
        """
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(json.loads(self.to_json()), ensure_ascii=False, indent=1))

    @staticmethod
    def load(path: str) -> "Project":
        """Читает проект из файла, созданного :meth:`save`.

        Args:
            path (str): путь к файлу ``.erd.json``.

        Returns:
            Project: загруженный проект.

        Raises:
            OSError: если файл не найден или не читается.
            json.JSONDecodeError: если содержимое не JSON (это подкласс ``ValueError``).
            KeyError: если в JSON нет раздела ``"diagrams"``.
        """
        with open(path, encoding="utf-8") as fh:
            return Project.from_json(fh.read())


def empty_project() -> Project:
    """Создаёт пустой проект.

    Returns:
        Project: проект с двумя пустыми диаграммами и стандартными легендами (даталогическая —
        со связями-стрелками).
    """
    return Project(diagrams={k: Diagram(k, legend=default_legend(k, k == KIND_DATA), arrows=k == KIND_DATA)
                             for k in (KIND_INFO, KIND_DATA)})

"""Автораскладка узлов и подбор маршрутов связей.

Раскладка следует правилу Баркера «dead crows fly east» («мёртвые вороны летят на восток»):
подчинённые («много») сущности стоят левее, главные — правее, поэтому «вороньи лапки»
смотрят влево или вверх.

Алгоритм :func:`auto_layout`:

1. Глубина узла = длина самой длинной цепочки ссылок до узла без внешних ключей
   (циклы разрываются); колонка = ``макс. глубина − глубина``.
2. Порядок узлов в колонках — барицентрическая сортировка по средней высоте соседей.
3. Маршруты связей — :func:`auto_route`: перебор сторон и положений среднего звена с
   оценкой штрафами (:func:`_score`).
"""
from __future__ import annotations

from math import ceil
from typing import NamedTuple, Optional, Protocol

from . import geom
from .geom import FRect, Point, anchor_point, clean_points, route
from .model import KIND_DATA, KIND_INFO, STYLE, Diagram, Link, Node, node_height

TITLE_SIZE: dict[str, int] = {KIND_INFO: 15, KIND_DATA: 14}   # размер шрифта заголовка узла
ATTR_SIZE: dict[str, int] = {KIND_INFO: 13, KIND_DATA: 13}    # размер шрифта строки (имя атрибута / столбца)
TYPE_SIZE = 12                                                # размер шрифта типа столбца
NN_TEXT = "NOT NULL"


class Measure(Protocol):
    """Функция измерения текста (``Fonts.measure``): размер строки в мировых единицах."""

    def __call__(self, text: str, size: float, bold: bool = False, italic: bool = False) -> tuple[int, int]:
        """Измеряет текст.

        Args:
            text (str): измеряемая строка.
            size (float): размер шрифта.
            bold (bool): жирное начертание.
            italic (bool): курсив.

        Returns:
            tuple[int, int]: ``(ширина, высота)`` текста.
        """
        ...


class Candidate(NamedTuple):
    """Вариант маршрута связи.

    Attributes:
        score (float): штраф (меньше — лучше).
        sa (str): сторона узла A.
        sb (str): сторона узла B.
        mid (Optional[int]): положение среднего звена или ``None``.
        pts (list[Point]): точки ломаной.
    """

    score: float
    sa: str
    sb: str
    mid: Optional[int]
    pts: list[Point]


def node_rect(d: Diagram, n: Node) -> FRect:
    """Прямоугольник узла в мировых координатах.

    Args:
        d (Diagram): диаграмма (определяет вид и, значит, размеры строк).
        n (Node): узел.

    Returns:
        FRect: прямоугольник; высота вычисляется по числу строк узла.

    Raises:
        KeyError: если вид диаграммы ``d.kind`` неизвестен.
    """
    return FRect(n.x, n.y, n.w, node_height(d.kind, len(n.rows)))


def data_columns(node: Node, measure: Measure) -> tuple[int, float, int, int]:
    """Вычисляет раскладку колонок даталогической таблицы.

    Args:
        node (Node): таблица.
        measure (Measure): функция измерения текста.

    Returns:
        tuple[int, float, int, int]: ``(смещение колонки имени, смещение колонки типа,
        ширина колонки типа, ширина надписи «NOT NULL»)``; смещения отсчитываются от
        левого края узла.
    """
    name_w = max((measure(r.name, ATTR_SIZE[KIND_DATA], "PK" in r.key)[0] for r in node.rows), default=40)
    type_w = max((measure(r.type, TYPE_SIZE)[0] for r in node.rows), default=30)
    return 60, 60 + name_w + 16, type_w, measure(NN_TEXT, 11)[0]


def needed_width(d: Diagram, n: Node, measure: Measure) -> float:
    """Вычисляет минимальную ширину узла, при которой весь его текст помещается.

    Args:
        d (Diagram): диаграмма.
        n (Node): узел.
        measure (Measure): функция измерения текста.

    Returns:
        float: ширина в мировых единицах (без округления и без учёта минимума стиля).

    Raises:
        KeyError: если вид диаграммы ``d.kind`` неизвестен.
    """
    title_w = measure(n.title, TITLE_SIZE[d.kind], True)[0] + 50
    body: float
    if d.kind == KIND_INFO:
        body = max((measure(r.name, ATTR_SIZE[KIND_INFO])[0] for r in n.rows), default=0) + 32 + 18
    else:
        _, type_x, type_w, nn_w = data_columns(n, measure)
        body = type_x + type_w + 16 + nn_w + 10
    return max(title_w, body)


def fit_widths(d: Diagram, measure: Measure, grow_only: bool = False) -> None:
    """Подгоняет ширину узлов диаграммы под их текст (округляя вверх до 10).

    Args:
        d (Diagram): диаграмма; ширина узлов изменяется на месте.
        measure (Measure): функция измерения текста.
        grow_only (bool): ``True`` — только расширять, не уменьшая ширину, выбранную пользователем.

    Raises:
        KeyError: если вид диаграммы ``d.kind`` неизвестен.
    """
    s = STYLE[d.kind]
    for n in d.nodes:
        w = float(ceil(max(s["min_w"], needed_width(d, n, measure)) / 10.0) * 10)
        n.w = max(n.w, w) if grow_only else w


# ───────────────────────── автораскладка ─────────────────────────

def _depths(d: Diagram) -> dict[str, int]:
    """Вычисляет глубину каждого узла по цепочкам внешних ключей.

    Args:
        d (Diagram): диаграмма.

    Returns:
        dict[str, int]: ``{id узла: глубина}``; узлы без внешних ключей — 0; циклы ссылок
        и самоссылки игнорируются.
    """
    parents: dict[str, set[str]] = {n.id: set() for n in d.nodes}
    for l in d.links:
        if l.a != l.b and l.a in parents and l.b in parents:
            parents[l.a].add(l.b)
    depth: dict[str, int] = {}
    state: dict[str, int] = {}

    def visit(nid: str) -> int:
        """Глубина узла поиском в глубину.

        Args:
            nid (str): идентификатор узла.

        Returns:
            int: глубина узла; ``-1`` для узла, уже находящегося в обработке (цикл).
        """
        if nid in depth:
            return depth[nid]
        if state.get(nid) == 1:                      # цикл ссылок: обратное ребро игнорируем
            return -1
        state[nid] = 1
        val = 0
        for p in parents[nid]:
            val = max(val, 1 + visit(p))
        state[nid] = 2
        depth[nid] = val
        return val

    for n in d.nodes:
        visit(n.id)
    return depth


def auto_layout(d: Diagram, gap_x: float = 260.0, gap_y: float = 56.0, route_links: bool = True) -> None:
    """Расставляет узлы по колонкам, раскладывает легенду и (по умолчанию) маршруты связей.

    Args:
        d (Diagram): диаграмма; координаты узлов, легенды и параметры связей изменяются на месте.
        gap_x (float): зазор между колонками (в нём идут линии и подписи связей).
        gap_y (float): зазор между узлами в колонке.
        route_links (bool): ``False`` — не пересчитывать маршруты связей.
    """
    if not d.nodes:
        return
    depth = _depths(d)
    top = max(depth.values())
    cols: list[list[Node]] = [[] for _ in range(top + 1)]
    for n in d.nodes:
        cols[top - depth[n.id]].append(n)
    heights = {n.id: node_height(d.kind, len(n.rows)) for n in d.nodes}

    neighbours: dict[str, list[str]] = {n.id: [] for n in d.nodes}
    for l in d.links:
        if l.a != l.b:
            neighbours[l.a].append(l.b)
            neighbours[l.b].append(l.a)
    ycenter: dict[str, float] = {}

    def stack() -> None:
        """Укладывает каждую колонку сверху вниз и центрирует её относительно самой высокой."""
        total = max((sum(heights[n.id] for n in c) + gap_y * (len(c) - 1)) for c in cols if c)
        for c in cols:
            h = sum(heights[n.id] for n in c) + gap_y * max(0, len(c) - 1)
            y = (total - h) / 2
            for n in c:
                n.y = y
                ycenter[n.id] = y + heights[n.id] / 2
                y += heights[n.id] + gap_y

    stack()
    for _ in range(8):                                # барицентрическая сортировка: меньше пересечений
        for c in cols:
            def key(n: Node) -> float:
                """Ключ сортировки: средняя высота центров соседей узла.

                Args:
                    n (Node): узел колонки.

                Returns:
                    float: средняя ордината центров соседей (или собственного центра, если соседей нет).
                """
                ys = [ycenter[o] for o in neighbours[n.id]]
                return sum(ys) / len(ys) if ys else ycenter[n.id]
            c.sort(key=key)
        stack()

    x = 0.0                                           # внутри колонки узлы выровнены по правому краю
    for c in cols:
        w = max((n.w for n in c), default=0)
        for n in c:
            n.x = x + (w - n.w)
        x += w + gap_x

    right = max(n.x + n.w for n in d.nodes)
    bottom = max(n.y + heights[n.id] for n in d.nodes)
    d.legend.x = max(0.0, right - d.legend.w)
    d.legend.y = bottom + 60
    if route_links:
        auto_route(d)


# ───────────────────────── маршруты связей ─────────────────────────

def _row_t(d: Diagram, n: Node, idx: int) -> float:
    """Вычисляет, на какой доле высоты узла лежит середина строки (для привязки FK к строке).

    Args:
        d (Diagram): диаграмма.
        n (Node): узел.
        idx (int): индекс строки.

    Returns:
        float: доля высоты узла (0..1).
    """
    s = STYLE[d.kind]
    h = node_height(d.kind, len(n.rows))
    return (s["head"] + idx * s["row"] + s["row"] / 2) / h


def _mid_candidates(axis: str, p1: Point, d1: geom.Direction, p2: Point, d2: geom.Direction,
                    rects: list[FRect]) -> list[int]:
    """Составляет список возможных положений среднего звена маршрута.

    Кандидаты: середина между точками, отступ от концов, края всех узлов (для обхода) и
    равномерная сетка «каналов» с шагом 24.

    Args:
        axis (str): ``"x"`` или ``"y"`` — вдоль какой оси ищется положение.
        p1 (Point): точка привязки у конца A.
        d1 (geom.Direction): направление выхода у A.
        p2 (Point): точка привязки у конца B.
        d2 (geom.Direction): направление выхода у B.
        rects (list[FRect]): прямоугольники всех узлов.

    Returns:
        list[int]: отсортированные положения (не более 40, ближайшие к середине).
    """
    i = 0 if axis == "x" else 1
    lo, hi = min(p1[i], p2[i]), max(p1[i], p2[i])
    cands = {(p1[i] + p2[i]) / 2, p1[i] + d1[i] * 26, p2[i] + d2[i] * 26}
    for r in rects:
        a, b = (r.x, r.right) if axis == "x" else (r.y, r.bottom)
        cands.add(a - 18)
        cands.add(b + 18)
    k = lo - 40
    while k <= hi + 40:
        cands.add(k)
        k += 24
    ordered = sorted(c for c in cands if lo - 60 <= c <= hi + 60)
    if len(ordered) > 40:                                # слишком много — оставляем ближние к середине
        centre = (lo + hi) / 2
        ordered = sorted(sorted(ordered, key=lambda c: abs(c - centre))[:40])
    return [round(c) for c in ordered] or [round((lo + hi) / 2)]


def _score(pts: list[Point], own: set[str], rects: dict[str, FRect], placed: list[geom.Segment],
           ends: tuple[tuple[geom.Direction, str], tuple[geom.Direction, str]]) -> float:
    """Оценивает маршрут штрафами (меньше — лучше).

    Штрафуются: пересечение узлов, длина и изломы, короткие концевые звенья, наложение и
    пересечение уже проложенных связей, выход линии «внутрь» узла и «воронья лапка» на левой
    или верхней стороне (нарушение «dead crows fly east»).

    Args:
        pts (list[Point]): ломаная маршрута.
        own (set[str]): идентификаторы узлов-концов (их границы допустимо касаться).
        rects (dict[str, FRect]): прямоугольники всех узлов.
        placed (list[geom.Segment]): отрезки уже проложенных связей.
        ends (tuple): ``((направление, кратность) у A, (направление, кратность) у B)``.

    Returns:
        float: суммарный штраф маршрута.
    """
    pts = clean_points(pts)
    s = 0.0
    segs = list(zip(pts, pts[1:]))
    for nid, r in rects.items():
        box = r.inflated(-1.5) if nid in own else r.inflated(6)
        for a, b in segs:
            if geom.seg_hits_rect(a, b, box):
                s += 1000
                break
    s += geom.poly_length(pts) * 0.5 + (len(pts) - 2) * 18
    if len(pts) > 2:                                      # короткие концевые звенья: негде разместить подписи
        for a, b in (segs[0], segs[-1]):
            if geom.poly_length([a, b]) < 60:
                s += 22
    for a, b in segs:
        for pa, pb in placed:
            par = geom.parallel_overlap(a, b, pa, pb)
            if par is not None:
                gap, ov = par
                if gap < 3:
                    s += 80 + ov * 0.6
                elif gap < 14:
                    s += 30 + ov * 0.2
            elif geom.segs_cross(a, b, pa, pb):
                s += 14
    (d1, card1), (d2, card2) = ends
    if len(pts) > 1:
        v = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
        if v[0] * d1[0] + v[1] * d1[1] <= 0:
            s += 600
        v = (pts[-2][0] - pts[-1][0], pts[-2][1] - pts[-1][1])
        if v[0] * d2[0] + v[1] * d2[1] <= 0:
            s += 600
    for d_, card in ((d1, card1), (d2, card2)):
        if card == "M" and d_ in ((-1, 0), (0, -1)):  # лапка на левой/верхней стороне: «ворона летит на запад/вверх»
            s += 90
    return s


_SLOTS = (0.5, 0.3, 0.7, 0.2, 0.8)


def _best_route(d: Diagram, l: Link, rects: dict[str, FRect], placed: list[geom.Segment],
                fixed_sides: bool = False, idx: int = 0) -> Candidate:
    """Находит лучший по штрафу маршрут связи перебором сторон и положений среднего звена.

    Args:
        d (Diagram): диаграмма.
        l (Link): связь.
        rects (dict[str, FRect]): прямоугольники всех узлов.
        placed (list[geom.Segment]): отрезки уже проложенных связей (препятствия).
        fixed_sides (bool): ``True`` — стороны и точки привязки связи заданы, ищется только
            среднее звено.
        idx (int): порядковый номер связи; разводит стартовые точки, чтобы связи не штрафовались
            друг за друга на первом проходе.

    Returns:
        Candidate: победивший вариант маршрута.

    Raises:
        KeyError: если концы связи ссылаются на узлы, отсутствующие в ``rects``.
    """
    ra, rb = rects[l.a], rects[l.b]
    own = {l.a, l.b}
    best: Optional[Candidate] = None
    sides_a = [l.sa] if fixed_sides else list(geom.SIDES_ORDER)
    sides_b = [l.sb] if fixed_sides else list(geom.SIDES_ORDER)
    for sa in sides_a:
        for sb in sides_b:
            ta = l.ta if fixed_sides else _SLOTS[idx % 5]
            tb = l.tb if fixed_sides else _SLOTS[(idx + 2) % 5]
            p1, d1 = anchor_point(ra, sa, ta)
            p2, d2 = anchor_point(rb, sb, tb)
            if l.a == l.b and sa == sb and sa not in ("right", "left"):
                continue
            axis = "y" if d1[0] == 0 and d2[0] == 0 else "x" if d1[1] == 0 and d2[1] == 0 else None
            mids: list[Optional[int]] = list(_mid_candidates(axis, p1, d1, p2, d2, list(rects.values()))) if axis else [None]
            for mid in mids:
                pts, _ = route(p1, d1, p2, d2, mid)
                sc = _score(pts, own, rects, placed, ((d1, l.card_a), (d2, l.card_b)))
                if mid is not None:                       # при равенстве — ближе к середине между узлами
                    i = 0 if axis == "x" else 1
                    sc += 0.04 * abs(mid - (p1[i] + p2[i]) / 2)
                if best is None or sc < best.score:
                    best = Candidate(sc, sa, sb, mid, pts)
    assert best is not None
    return best


def _default_mid(l: Link, rects: dict[str, FRect]) -> tuple[Optional[str], list[Point]]:
    """Определяет ось среднего звена и точки маршрута связи при автоматическом положении звена.

    Args:
        l (Link): связь (с заданными сторонами и точками привязки).
        rects (dict[str, FRect]): прямоугольники всех узлов.

    Returns:
        tuple[Optional[str], list[Point]]: ``(ось "x" | "y" | None, точки маршрута)``.

    Raises:
        KeyError: если концы связи отсутствуют в ``rects``.
    """
    p1, d1 = anchor_point(rects[l.a], l.sa, l.ta)
    p2, d2 = anchor_point(rects[l.b], l.sb, l.tb)
    pts, axis = route(p1, d1, p2, d2, None)
    return axis, pts


def auto_route(d: Diagram, only: Optional[list[Link]] = None) -> None:
    """Подбирает стороны, точки привязки и средние звенья связей.

    Три прохода: (1) выбор сторон — короткие связи первыми, длинные обходят их;
    (2) распределение точек привязки по сторонам узлов; (3) уточнение средних звеньев
    при окончательных привязках. Самоссылки получают петлю справа от узла.

    Args:
        d (Diagram): диаграмма; параметры связей изменяются на месте.
        only (Optional[list[Link]]): связи для (пере)маршрутизации. ``None`` — все; иначе
            остальные остаются как есть и считаются препятствиями (так прокладывается
            одна новая связь).

    Raises:
        KeyError: если связь ссылается на узел, которого нет в диаграмме.
    """
    rects = {n.id: node_rect(d, n) for n in d.nodes}
    targets = list(only) if only is not None else list(d.links)
    target_ids = {l.id for l in targets}

    def placed_segments() -> list[geom.Segment]:
        """Собирает отрезки связей, которые не пересчитываются (они — препятствия для остальных).

        Returns:
            list[geom.Segment]: отрезки ломаных непересчитываемых связей.
        """
        segs: list[geom.Segment] = []
        for o in d.links:
            if o.id in target_ids:
                continue
            p1, d1 = anchor_point(rects[o.a], o.sa, o.ta)
            p2, d2 = anchor_point(rects[o.b], o.sb, o.tb)
            pts = clean_points(route(p1, d1, p2, d2, o.mid)[0])
            segs += list(zip(pts, pts[1:]))
        return segs

    def dist(l: Link) -> float:
        """Манхэттенское расстояние между центрами узлов связи (для порядка обработки).

        Args:
            l (Link): связь.

        Returns:
            float: расстояние между центрами концевых узлов.
        """
        a, b = rects[l.a], rects[l.b]
        return abs(a.cx - b.cx) + abs(a.cy - b.cy)

    # 1) стороны
    placed = placed_segments()
    for idx, l in enumerate(sorted(targets, key=dist)):
        if l.a == l.b:                                # самоссылка: петля справа от узла
            l.sa = l.sb = "right"
            l.ta, l.tb = 0.3, 0.7
            continue
        best = _best_route(d, l, rects, placed, idx=idx)
        l.sa, l.sb = best.sa, best.sb
        l.mid = None
        pts = clean_points(best.pts)
        placed += list(zip(pts, pts[1:]))

    # 2) точки привязки на сторонах
    groups: dict[tuple[str, str], list[tuple[Link, str]]] = {}
    for l in targets:
        if l.a == l.b:
            continue
        groups.setdefault((l.a, l.sa), []).append((l, "a"))
        groups.setdefault((l.b, l.sb), []).append((l, "b"))
    existing: dict[tuple[str, str], list[float]] = {}
    for o in d.links:
        if o.id not in target_ids:
            existing.setdefault((o.a, o.sa), []).append(o.ta)
            existing.setdefault((o.b, o.sb), []).append(o.tb)
    for (nid, side), items in groups.items():
        node = d.node(nid)
        assert node is not None

        def other_coord(item: tuple[Link, str]) -> float:
            """Координата противоположного конца связи вдоль стороны (для порядка точек привязки).

            Args:
                item (tuple[Link, str]): ``(связь, конец "a" | "b")`` на этой стороне узла.

            Returns:
                float: ордината центра противоположного узла для боковых сторон, абсцисса — для верха и низа.
            """
            l, end = item
            o = rects[l.b if end == "a" else l.a]
            return o.cy if side in ("left", "right") else o.cx

        items.sort(key=other_coord)
        taken = existing.get((nid, side), [])
        if only is not None:                          # одиночная связь: свободная позиция
            slots = [0.5, 0.3, 0.7, 0.2, 0.8, 0.12, 0.88]
            for (l, end) in items:
                t = next((s for s in slots if all(abs(s - x) > 0.08 for x in taken)), 0.5)
                taken = taken + [t]
                setattr(l, "ta" if end == "a" else "tb", t)
            continue
        n = len(items)
        for k, (l, end) in enumerate(items):
            t = (k + 1) / (n + 1)
            hint = l.row_a if end == "a" else l.row_b
            if d.kind == KIND_DATA and side in ("left", "right") and hint is not None and n <= 2:
                t = _row_t(d, node, hint)
            setattr(l, "ta" if end == "a" else "tb", t)

    # 3) средние звенья при окончательных привязках
    placed = placed_segments()
    for l in sorted(targets, key=dist):
        if l.a == l.b:
            l.mid = rects[l.a].right + 110
            continue
        best = _best_route(d, l, rects, placed, fixed_sides=True)
        axis, default_pts = _default_mid(l, rects)
        if best.mid is not None and axis is not None:
            default = default_pts[1][0] if axis == "x" else default_pts[1][1]
            l.mid = None if abs(default - best.mid) < 1 else float(best.mid)
        else:
            l.mid = None
        pts = clean_points(best.pts)
        placed += list(zip(pts, pts[1:]))

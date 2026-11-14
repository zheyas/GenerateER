"""Геометрия: прямоугольники, ортогональные маршруты связей, попадание курсора.

Все функции чистые (без pygame), работают в мировых координатах. Маршруты связей —
ломаные из горизонтальных и вертикальных отрезков.
"""
from __future__ import annotations

from math import hypot
from typing import Optional

Point = tuple[float, float]
Direction = tuple[int, int]            # единичный вектор выхода: (0, -1) вверх, (1, 0) вправо…
Segment = tuple[Point, Point]
Route = tuple[list[Point], Optional[str]]

ANCHOR_MARGIN = 14.0                   # минимальный отступ точки связи от угла узла
SIDES_ORDER: tuple[str, ...] = ("top", "right", "bottom", "left")
DIRS: dict[str, Direction] = {"top": (0, -1), "bottom": (0, 1), "left": (-1, 0), "right": (1, 0)}


class FRect:
    """Прямоугольник с вещественными координатами (в отличие от ``pygame.Rect``).

    Attributes:
        x (float): абсцисса левого верхнего угла.
        y (float): ордината левого верхнего угла.
        w (float): ширина.
        h (float): высота.
    """

    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        """Создаёт прямоугольник; все значения приводятся к ``float``.

        Args:
            x (float): абсцисса левого верхнего угла.
            y (float): ордината левого верхнего угла.
            w (float): ширина.
            h (float): высота.

        Raises:
            ValueError: если значение нельзя привести к числу (например, пустая строка).
            TypeError: если передан ``None`` или объект неподходящего типа.
        """
        self.x, self.y, self.w, self.h = float(x), float(y), float(w), float(h)

    @property
    def right(self) -> float:
        """Правая граница прямоугольника.

        Returns:
            float: ``x + w``.
        """
        return self.x + self.w

    @property
    def bottom(self) -> float:
        """Нижняя граница прямоугольника.

        Returns:
            float: ``y + h``.
        """
        return self.y + self.h

    @property
    def cx(self) -> float:
        """Абсцисса центра прямоугольника.

        Returns:
            float: ``x + w / 2``.
        """
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        """Ордината центра прямоугольника.

        Returns:
            float: ``y + h / 2``.
        """
        return self.y + self.h / 2

    def contains(self, px: float, py: float, pad: float = 0.0) -> bool:
        """Проверяет, лежит ли точка внутри прямоугольника, расширенного на ``pad``.

        Args:
            px (float): абсцисса точки.
            py (float): ордината точки.
            pad (float): допуск — на сколько расширить прямоугольник со всех сторон
                (удобно для «толстого» попадания курсором).

        Returns:
            bool: ``True``, если точка внутри (граница включается).
        """
        return self.x - pad <= px <= self.right + pad and self.y - pad <= py <= self.bottom + pad

    def inflated(self, d: float) -> "FRect":
        """Возвращает новый прямоугольник, расширенный на ``d`` со всех сторон.

        Args:
            d (float): величина расширения; отрицательная — сжатие.

        Returns:
            FRect: новый прямоугольник (исходный не меняется).
        """
        return FRect(self.x - d, self.y - d, self.w + 2 * d, self.h + 2 * d)

    def union(self, o: "FRect") -> "FRect":
        """Находит наименьший прямоугольник, содержащий оба.

        Args:
            o (FRect): второй прямоугольник.

        Returns:
            FRect: объединяющий прямоугольник.
        """
        x, y = min(self.x, o.x), min(self.y, o.y)
        return FRect(x, y, max(self.right, o.right) - x, max(self.bottom, o.bottom) - y)

    def __repr__(self) -> str:
        """Строковое представление для отладки.

        Returns:
            str: строка вида ``FRect(x,y,w,h)`` с округлёнными координатами.
        """
        return f"FRect({self.x:.0f},{self.y:.0f},{self.w:.0f},{self.h:.0f})"


def side_length(rect: FRect, side: str) -> float:
    """Длина стороны прямоугольника.

    Args:
        rect (FRect): прямоугольник.
        side (str): ``"top"`` / ``"bottom"`` (длина — ширина) или ``"left"`` / ``"right"`` (высота).

    Returns:
        float: ширина для горизонтальных сторон, высота для вертикальных.
    """
    return rect.w if side in ("top", "bottom") else rect.h


def clamp_offset(length: float, t: float) -> float:
    """Смещение точки привязки вдоль стороны с отступом от углов.

    Args:
        length (float): длина стороны.
        t (float): желаемое положение, доля длины (0..1).

    Returns:
        float: смещение ``t * length``, ограниченное отступом :data:`ANCHOR_MARGIN` от обоих
        углов (для очень короткой стороны — её середина).
    """
    off = t * length
    m = ANCHOR_MARGIN if length > 2 * ANCHOR_MARGIN else length / 2
    return max(m, min(length - m, off))


def anchor_point(rect: FRect, side: str, t: float) -> tuple[Point, Direction]:
    """Точка привязки связи к стороне прямоугольника и направление выхода линии наружу.

    Args:
        rect (FRect): прямоугольник узла.
        side (str): ``"top"``, ``"right"``, ``"bottom"`` или ``"left"`` (любое иное значение
            трактуется как ``"right"``).
        t (float): положение вдоль стороны, доля её длины (0..1); ограничивается отступом
            от углов.

    Returns:
        tuple[Point, Direction]: ``(точка, единичный вектор выхода наружу)``.
    """
    off = clamp_offset(side_length(rect, side), t)
    if side == "top":
        return (rect.x + off, rect.y), DIRS["top"]
    if side == "bottom":
        return (rect.x + off, rect.bottom), DIRS["bottom"]
    if side == "left":
        return (rect.x, rect.y + off), DIRS["left"]
    return (rect.right, rect.y + off), DIRS["right"]


def nearest_anchor(rect: FRect, px: float, py: float) -> tuple[str, float]:
    """Находит ближайшую к точке сторону прямоугольника и положение на ней.

    Используется при перетаскивании конца связи мышью: точка приводится к ближайшей стороне.

    Args:
        rect (FRect): прямоугольник узла.
        px (float): абсцисса точки (мировая).
        py (float): ордината точки (мировая).

    Returns:
        tuple[str, float]: ``(сторона, t)``, где ``t`` — доля длины стороны (0..1); при
        вырожденной стороне (нулевой длины) ``t = 0.5``.
    """
    best: Optional[tuple[float, str, float]] = None
    for side in SIDES_ORDER:
        if side in ("top", "bottom"):
            qx = min(max(px, rect.x), rect.right)
            qy = rect.y if side == "top" else rect.bottom
            t = (qx - rect.x) / rect.w if rect.w else 0.5
        else:
            qx = rect.x if side == "left" else rect.right
            qy = min(max(py, rect.y), rect.bottom)
            t = (qy - rect.y) / rect.h if rect.h else 0.5
        dist = hypot(px - qx, py - qy)
        if best is None or dist < best[0]:
            best = (dist, side, t)
    assert best is not None
    return best[1], best[2]


def route(p1: Point, d1: Direction, p2: Point, d2: Direction, mid: Optional[float] = None) -> Route:
    """Строит ортогональный маршрут между двумя точками привязки.

    Args:
        p1 (Point): точка привязки у конца A.
        d1 (Direction): направление выхода у конца A.
        p2 (Point): точка привязки у конца B.
        d2 (Direction): направление выхода у конца B.
        mid (Optional[float]): положение среднего звена (абсцисса или ордината в зависимости
            от оси); ``None`` — выбрать автоматически (посередине или обход снаружи).

    Returns:
        Route: ``(точки ломаной, ось среднего звена)``. Ось ``"x"`` — среднее звено
        вертикальное и ``mid`` — его абсцисса; ``"y"`` — горизонтальное; ``None`` — у маршрута
        нет перетаскиваемого среднего звена (прямая или Г-образный).
    """
    (x1, y1), (x2, y2) = p1, p2
    v1, v2 = d1[0] == 0, d2[0] == 0
    if v1 and v2:
        if tuple(d1) == tuple(d2):                       # оба конца смотрят в одну сторону: П-образный обход
            ext = (min(y1, y2) - 30) if d1[1] < 0 else (max(y1, y2) + 30)
            my = mid if mid is not None else ext
        else:
            if x1 == x2 and mid is None:
                return [p1, p2], None
            my = mid if mid is not None else (y1 + y2) / 2
        return [p1, (x1, my), (x2, my), p2], "y"
    if not v1 and not v2:
        if tuple(d1) == tuple(d2):
            ext = (min(x1, x2) - 30) if d1[0] < 0 else (max(x1, x2) + 30)
            mx = mid if mid is not None else ext
        else:
            if y1 == y2 and mid is None:
                return [p1, p2], None
            mx = mid if mid is not None else (x1 + x2) / 2
        return [p1, (mx, y1), (mx, y2), p2], "x"
    return ([p1, (x1, y2), p2] if v1 else [p1, (x2, y1), p2]), None


def clean_points(pts: list[Point]) -> list[Point]:
    """Убирает из ломаной совпадающие подряд точки и промежуточные точки на одной прямой.

    Args:
        pts (list[Point]): вершины ломаной.

    Returns:
        list[Point]: упрощённая ломаная (новый список); начало и конец сохраняются.
    """
    out: list[Point] = []
    for p in pts:
        if out and abs(out[-1][0] - p[0]) < 0.01 and abs(out[-1][1] - p[1]) < 0.01:
            continue
        out.append(p)
    i = 1
    while i < len(out) - 1:
        a, b, c = out[i - 1], out[i], out[i + 1]
        if (abs(a[0] - b[0]) < 0.01 and abs(b[0] - c[0]) < 0.01) or (abs(a[1] - b[1]) < 0.01 and abs(b[1] - c[1]) < 0.01):
            out.pop(i)
        else:
            i += 1
    return out


def poly_length(pts: list[Point]) -> float:
    """Суммарная длина ломаной.

    Args:
        pts (list[Point]): вершины ломаной.

    Returns:
        float: сумма длин звеньев (0 для списка из менее чем двух точек).
    """
    return sum(hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


def split_half(pts: list[Point]) -> tuple[list[Point], list[Point]]:
    """Делит ломаную по длине пополам.

    Args:
        pts (list[Point]): вершины ломаной от конца A к концу B.

    Returns:
        tuple[list[Point], list[Point]]: ``(половина у конца A, половина у конца B)``; точка
        деления входит в обе половины. Для вырожденной ломаной (нулевой длины) —
        ``(копия ломаной, последняя точка)``.
    """
    lens = [hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])]
    half, acc = sum(lens) / 2, 0.0
    for i, seg in enumerate(lens):
        if seg and acc + seg >= half:
            t = (half - acc) / seg
            a, b = pts[i], pts[i + 1]
            m = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            return pts[:i + 1] + [m], [m] + pts[i + 1:]
        acc += seg
    return list(pts), list(pts[-1:])


def point_seg_dist(px: float, py: float, a: Point, b: Point) -> float:
    """Расстояние от точки до отрезка.

    Args:
        px (float): абсцисса точки.
        py (float): ордината точки.
        a (Point): начало отрезка.
        b (Point): конец отрезка (может совпадать с началом).

    Returns:
        float: наименьшее расстояние от точки до любой точки отрезка.
    """
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return hypot(px - (ax + t * dx), py - (ay + t * dy))


def poly_dist(px: float, py: float, pts: list[Point]) -> float:
    """Расстояние от точки до ломаной.

    Args:
        px (float): абсцисса точки.
        py (float): ордината точки.
        pts (list[Point]): вершины ломаной.

    Returns:
        float: расстояние до ближайшего звена; ``1e9`` для ломаной без звеньев.
    """
    return min((point_seg_dist(px, py, a, b) for a, b in zip(pts, pts[1:])), default=1e9)


def seg_hits_rect(a: Point, b: Point, r: FRect) -> bool:
    """Проверяет, пересекает ли ортогональный отрезок внутренность прямоугольника.

    Касание границы не считается пересечением. Наклонные отрезки не поддерживаются.

    Args:
        a (Point): начало отрезка.
        b (Point): конец отрезка (отрезок горизонтальный или вертикальный).
        r (FRect): прямоугольник.

    Returns:
        bool: ``True``, если отрезок заходит внутрь прямоугольника; для наклонного отрезка
        всегда ``False``.
    """
    (x1, y1), (x2, y2) = a, b
    if abs(x1 - x2) < 0.01:
        return r.x < x1 < r.right and max(min(y1, y2), r.y) < min(max(y1, y2), r.bottom)
    if abs(y1 - y2) < 0.01:
        return r.y < y1 < r.bottom and max(min(x1, x2), r.x) < min(max(x1, x2), r.right)
    return False


def collinear_overlap(a: Point, b: Point, c: Point, d: Point) -> float:
    """Длина наложения двух ортогональных отрезков, лежащих на одной прямой.

    Args:
        a (Point): начало первого отрезка.
        b (Point): конец первого отрезка.
        c (Point): начало второго отрезка.
        d (Point): конец второго отрезка.

    Returns:
        float: длина общей части; ``0.0``, если отрезки не лежат на одной прямой (допуск 3)
        или не пересекаются.
    """
    if abs(a[0] - b[0]) < 0.01 and abs(c[0] - d[0]) < 0.01 and abs(a[0] - c[0]) < 3:
        lo, hi = max(min(a[1], b[1]), min(c[1], d[1])), min(max(a[1], b[1]), max(c[1], d[1]))
        return max(0.0, hi - lo)
    if abs(a[1] - b[1]) < 0.01 and abs(c[1] - d[1]) < 0.01 and abs(a[1] - c[1]) < 3:
        lo, hi = max(min(a[0], b[0]), min(c[0], d[0])), min(max(a[0], b[0]), max(c[0], d[0]))
        return max(0.0, hi - lo)
    return 0.0


def segs_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Проверяет, пересекаются ли два перпендикулярных ортогональных отрезка во внутренних точках.

    Args:
        a (Point): начало первого отрезка.
        b (Point): конец первого отрезка.
        c (Point): начало второго отрезка.
        d (Point): конец второго отрезка.

    Returns:
        bool: ``True``, если один отрезок вертикальный, другой горизонтальный и они
        пересекаются (с отступом 1 от концов); для параллельных отрезков ``False``.
    """
    v1, v2 = abs(a[0] - b[0]) < 0.01, abs(c[0] - d[0]) < 0.01
    if v1 == v2:
        return False
    if v1:
        vx, (vy0, vy1), hy, (hx0, hx1) = a[0], sorted((a[1], b[1])), c[1], sorted((c[0], d[0]))
    else:
        vx, (vy0, vy1), hy, (hx0, hx1) = c[0], sorted((c[1], d[1])), a[1], sorted((a[0], b[0]))
    return hx0 + 1 < vx < hx1 - 1 and vy0 + 1 < hy < vy1 - 1


def parallel_overlap(a: Point, b: Point, c: Point, d: Point) -> Optional[tuple[float, float]]:
    """Оценивает сближение двух параллельных ортогональных отрезков.

    Args:
        a (Point): начало первого отрезка.
        b (Point): конец первого отрезка.
        c (Point): начало второго отрезка.
        d (Point): конец второго отрезка.

    Returns:
        Optional[tuple[float, float]]: ``(расстояние между отрезками, длина общей проекции)``
        или ``None``, если отрезки не параллельны либо их проекции не пересекаются.
    """
    if abs(a[0] - b[0]) < 0.01 and abs(c[0] - d[0]) < 0.01:
        lo, hi = max(min(a[1], b[1]), min(c[1], d[1])), min(max(a[1], b[1]), max(c[1], d[1]))
        return (abs(a[0] - c[0]), hi - lo) if hi - lo > 1 else None
    if abs(a[1] - b[1]) < 0.01 and abs(c[1] - d[1]) < 0.01:
        lo, hi = max(min(a[0], b[0]), min(c[0], d[0])), min(max(a[0], b[0]), max(c[0], d[0]))
        return (abs(a[1] - c[1]), hi - lo) if hi - lo > 1 else None
    return None

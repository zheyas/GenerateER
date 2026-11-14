"""Отрисовка диаграмм на pygame: шрифты, графический контекст с камерой, сцена, экспорт PNG.

Поток данных: :class:`~er_editor.model.Diagram` → :class:`Scene` (все размеры, маршруты и
прямоугольники в мировых координатах) → :func:`draw_diagram` рисует через :class:`Gfx`
(мировые координаты → экран с учётом сдвига и масштаба). Тот же код рисует и на экран, и в PNG.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from math import ceil, hypot
from typing import Optional

import pygame

from . import geom
from .geom import Direction, FRect, Point, anchor_point, clean_points, route, split_half
from .layout import ATTR_SIZE, NN_TEXT, TITLE_SIZE, TYPE_SIZE, data_columns, node_rect
from .model import KIND_DATA, KIND_INFO, PALETTE, STYLE, Diagram, Link, Node

Color = tuple[int, int, int]

INK: Color = (31, 41, 51)
MUTED: Color = (82, 96, 109)
FAINT: Color = (154, 165, 177)
GRID_COL: Color = (217, 222, 228)
DOT_COL: Color = (203, 210, 218)
ACCENT: Color = (47, 111, 237)
ROW_SEL: Color = (219, 232, 255)
BRICK: Color = (154, 52, 18)
WHITE: Color = (255, 255, 255)
LEGEND_FILL: Color = (248, 250, 252)
FLAG_ERROR: Color = (203, 56, 55)
FLAG_WARN: Color = (224, 142, 24)
FLAG_ERROR_TINT: Color = (253, 224, 224)
FLAG_WARN_TINT: Color = (253, 239, 214)

CROW_LEN, CROW_SPREAD, BAR_OFFSET, BAR_HALF = 16, 9, 24, 10   # размеры «вороньей лапки» и черты UID
ARROW_LEN, ARROW_HALF = 15, 6                                  # размеры наконечника стрелки связи
LABEL_SIZE = 12                                                # шрифт подписей связей
LEGEND_ROW, LEGEND_HEAD = 22, 34                               # высота строки и шапки легенды


def hexrgb(s: str) -> Color:
    """Переводит цвет из записи ``"#rrggbb"`` в кортеж ``(r, g, b)``.

    Args:
        s (str): цвет в шестнадцатеричной записи, с ``#`` или без.

    Returns:
        Color: компоненты красного, зелёного и синего (0..255).

    Raises:
        ValueError: если строка не содержит шести шестнадцатеричных цифр.
    """
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


# ───────────────────────── шрифты ─────────────────────────

class Fonts:
    """Кэш шрифтов, измерений и готовых текстовых поверхностей.

    Подбирает системный шрифт с кириллицей (Arial / Helvetica / DejaVu…) и моноширинный
    (Menlo / Courier New / DejaVu Sans Mono…).
    """

    FAMILIES = "arial,helveticaneue,helvetica,dejavusans,liberationsans,freesans,segoeui,calibri"
    MONO_FAMILIES = "menlo,couriernew,dejavusansmono,liberationmono,consolas,monospace"

    def __init__(self) -> None:
        """Создаёт пустые кэши шрифтов, измерений и текстовых поверхностей."""
        self._fonts: dict[tuple[int, bool, bool, bool], pygame.font.Font] = {}
        self._measure: dict[tuple[str, int, bool, bool], tuple[int, int]] = {}
        self._surf: dict[tuple[str, int, bool, bool, bool, Color], pygame.Surface] = {}

    def font(self, px: int, bold: bool = False, italic: bool = False, mono: bool = False) -> pygame.font.Font:
        """Возвращает шрифт нужного размера и начертания (создаётся один раз и кэшируется).

        Args:
            px (int): размер шрифта в пикселях.
            bold (bool): жирное начертание.
            italic (bool): курсив.
            mono (bool): ``True`` — моноширинный шрифт.

        Returns:
            pygame.font.Font: готовый шрифт; если подходящего системного шрифта нет,
            используется шрифт pygame по умолчанию.

        Raises:
            pygame.error: если модуль шрифтов pygame не инициализирован.
        """
        key = (px, bold, italic, mono)
        f = self._fonts.get(key)
        if f is None:
            path: Optional[str] = None
            for fam in (self.MONO_FAMILIES if mono else self.FAMILIES).split(","):
                path = pygame.font.match_font(fam, bold=bold, italic=italic)
                if path and os.path.exists(path):
                    break
            f = pygame.font.Font(path, px)
            self._fonts[key] = f
        return f

    def measure(self, text: str, size: float, bold: bool = False, italic: bool = False) -> tuple[int, int]:
        """Измеряет текст шрифтом базового размера (без учёта масштаба камеры).

        Args:
            text (str): измеряемая строка.
            size (float): размер шрифта в мировых единицах (округляется до целого).
            bold (bool): жирное начертание.
            italic (bool): курсив.

        Returns:
            tuple[int, int]: ``(ширина, высота)`` в мировых единицах.

        Raises:
            pygame.error: если модуль шрифтов pygame не инициализирован.
        """
        key = (text, int(round(size)), bold, italic)
        m = self._measure.get(key)
        if m is None:
            m = self.font(int(round(size)), bold, italic).size(text)
            self._measure[key] = m
        return m

    def surface(self, text: str, px: int, bold: bool, italic: bool, color: Color, mono: bool = False) -> pygame.Surface:
        """Возвращает готовую сглаженную поверхность с текстом (кэшируется; кэш сбрасывается при росте).

        Args:
            text (str): отображаемый текст.
            px (int): размер шрифта в пикселях.
            bold (bool): жирное начертание.
            italic (bool): курсив.
            color (Color): цвет текста.
            mono (bool): ``True`` — моноширинный шрифт.

        Returns:
            pygame.Surface: поверхность с прозрачным фоном.

        Raises:
            pygame.error: если шрифты pygame не инициализированы или текст нельзя отрисовать.
        """
        key = (text, px, bold, italic, mono, color)
        s = self._surf.get(key)
        if s is None:
            if len(self._surf) > 4000:
                self._surf.clear()
            s = self.font(px, bold, italic, mono).render(text, True, color)
            self._surf[key] = s
        return s


# ───────────────────────── графический контекст ─────────────────────────

class Gfx:
    """Рисует в мировых координатах: экранная точка = мировая × ``z`` + (``ox``, ``oy``).

    Attributes:
        surf (pygame.Surface): поверхность, на которую рисуем (экран или буфер экспорта).
        fonts (Fonts): кэш шрифтов.
        ox (float): экранное смещение начала мировых координат по горизонтали.
        oy (float): то же по вертикали.
        z (float): масштаб (при экспорте — множитель разрешения).
    """

    def __init__(self, surf: pygame.Surface, fonts: Fonts, ox: float, oy: float, zoom: float) -> None:
        """Создаёт графический контекст.

        Args:
            surf (pygame.Surface): поверхность для рисования.
            fonts (Fonts): кэш шрифтов.
            ox (float): экранное смещение начала мировых координат по горизонтали.
            oy (float): экранное смещение по вертикали.
            zoom (float): масштаб.
        """
        self.surf, self.fonts, self.ox, self.oy, self.z = surf, fonts, ox, oy, zoom

    def p(self, pt: Point) -> Point:
        """Переводит мировую точку в экранную.

        Args:
            pt (Point): точка в мировых координатах.

        Returns:
            Point: точка на поверхности рисования.
        """
        return (pt[0] * self.z + self.ox, pt[1] * self.z + self.oy)

    def w(self, width: float) -> int:
        """Переводит толщину линии из мировых единиц в пиксели.

        Args:
            width (float): толщина в мировых единицах.

        Returns:
            int: толщина в пикселях (не менее 1).
        """
        return max(1, int(round(width * self.z)))

    def line(self, a: Point, b: Point, color: Color = INK, width: float = 1.5) -> None:
        """Рисует отрезок.

        Args:
            a (Point): начало (мировые координаты).
            b (Point): конец (мировые координаты).
            color (Color): цвет линии.
            width (float): толщина в мировых единицах.
        """
        pygame.draw.line(self.surf, color, self.p(a), self.p(b), self.w(width))

    def poly(self, pts: list[Point], color: Color = INK, width: float = 1.5, dashed: bool = False,
             dash: float = 7.0, gap: float = 5.0) -> None:
        """Рисует ломаную; углы толстых линий закрашиваются кружками, чтобы не оставалось щелей.

        Args:
            pts (list[Point]): вершины (мировые координаты).
            color (Color): цвет линии.
            width (float): толщина в мировых единицах.
            dashed (bool): ``True`` — пунктиром; рисунок штрихов продолжается через изломы.
            dash (float): длина штриха (мировые единицы).
            gap (float): длина промежутка (мировые единицы).
        """
        sp = [self.p(q) for q in pts]
        w = self.w(width)
        if not dashed:
            for a, b in zip(sp, sp[1:]):
                pygame.draw.line(self.surf, color, a, b, w)
            if w > 2:
                for q in sp[1:-1]:
                    pygame.draw.circle(self.surf, color, (round(q[0]), round(q[1])), w // 2)
            return
        d_on, d_off = dash * self.z, gap * self.z
        on, remaining = True, d_on
        for a, b in zip(sp, sp[1:]):
            length = hypot(b[0] - a[0], b[1] - a[1])
            if length < 1e-6:
                continue
            ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
            pos = 0.0
            while pos < length - 1e-6:
                step = min(remaining, length - pos)
                if on:
                    pygame.draw.line(self.surf, color, (a[0] + ux * pos, a[1] + uy * pos),
                                     (a[0] + ux * (pos + step), a[1] + uy * (pos + step)), w)
                pos += step
                remaining -= step
                if remaining <= 1e-6:
                    on = not on
                    remaining = d_on if on else d_off

    def rect(self, r: FRect, fill: Optional[Color] = None, outline: Optional[Color] = None,
             width: float = 1.5, radius: float = 0) -> None:
        """Рисует прямоугольник (заливку и/или контур) со скруглёнными углами.

        Args:
            r (FRect): прямоугольник в мировых координатах.
            fill (Optional[Color]): цвет заливки; ``None`` — без заливки.
            outline (Optional[Color]): цвет контура; ``None`` — без контура.
            width (float): толщина контура в мировых единицах.
            radius (float): радиус скругления в мировых единицах.
        """
        x, y = self.p((r.x, r.y))
        sr = pygame.Rect(round(x), round(y), max(1, round(r.w * self.z)), max(1, round(r.h * self.z)))
        rad = round(radius * self.z)
        if fill is not None:
            pygame.draw.rect(self.surf, fill, sr, 0, border_radius=rad)
        if outline is not None:
            pygame.draw.rect(self.surf, outline, sr, self.w(width), border_radius=rad)

    def circle(self, pt: Point, radius_px: int, fill: Color = WHITE, outline: Color = ACCENT, width: int = 2) -> None:
        """Рисует круглую ручку.

        Args:
            pt (Point): центр (мировые координаты).
            radius_px (int): радиус в экранных пикселях (не зависит от масштаба).
            fill (Color): цвет заливки.
            outline (Color): цвет контура.
            width (int): толщина контура в пикселях.
        """
        c = self.p(pt)
        pygame.draw.circle(self.surf, fill, c, radius_px)
        pygame.draw.circle(self.surf, outline, c, radius_px, width)

    def polygon(self, pts: list[Point], fill: Color = INK) -> None:
        """Рисует закрашенный многоугольник.

        Args:
            pts (list[Point]): вершины (мировые координаты).
            fill (Color): цвет заливки.
        """
        pygame.draw.polygon(self.surf, fill, [self.p(q) for q in pts])

    def handle(self, pt: Point, fill: Color = WHITE, outline: Color = ACCENT, half: int = 4) -> None:
        """Рисует квадратную ручку.

        Args:
            pt (Point): центр (мировые координаты).
            fill (Color): цвет заливки.
            outline (Color): цвет контура.
            half (int): половина стороны квадрата в экранных пикселях.
        """
        x, y = self.p(pt)
        r = pygame.Rect(round(x) - half, round(y) - half, 2 * half, 2 * half)
        pygame.draw.rect(self.surf, fill, r)
        pygame.draw.rect(self.surf, outline, r, 2)

    def text(self, s: str, x: float, y: float, size: float, color: Color = INK, bold: bool = False,
             italic: bool = False, anchor: str = "l") -> None:
        """Рисует текст; слишком мелкий (меньше 5 px) и пустой текст пропускаются.

        Args:
            s (str): текст.
            x (float): мировая абсцисса точки привязки.
            y (float): мировая ордината середины строки.
            size (float): размер шрифта в мировых единицах.
            color (Color): цвет текста.
            bold (bool): жирное начертание.
            italic (bool): курсив.
            anchor (str): выравнивание относительно ``x``: ``"l"`` — слева, ``"m"`` — по
                центру, ``"r"`` — справа.
        """
        px = int(round(size * self.z))
        if not s or px < 5:
            return
        surf = self.fonts.surface(s, px, bold, italic, color)
        sx, sy = self.p((x, y))
        rect = surf.get_rect()
        rect.centery = round(sy)
        rect.left = round(sx) if anchor == "l" else round(sx - rect.w / 2) if anchor == "m" else round(sx - rect.w)
        self.surf.blit(surf, rect)


# ───────────────────────── сцена: геометрия диаграммы ─────────────────────────

@dataclass
class Sel:
    """Выделение на диаграмме.

    Attributes:
        kind (Optional[str]): ``"node" | "link" | "legend" | "note"`` или ``None`` (ничего не выделено).
        id (Optional[str]): идентификатор выделенного объекта (для легенды — ``None``).
        row (Optional[int]): выделенная строка узла или пункт легенды.
    """

    kind: Optional[str] = None
    id: Optional[str] = None
    row: Optional[int] = None


@dataclass
class Flag:
    """Подсветка проблемной таблицы на экране (в PNG не попадает).

    Attributes:
        level (str): ``"error"`` (нарушение, красная рамка) или ``"warn"`` (подозрение, оранжевая).
        rows (dict[int, str]): подсвечиваемые строки: индекс строки → ``"error"`` | ``"warn"``.
    """

    level: str = "error"
    rows: dict[int, str] = field(default_factory=dict)


@dataclass
class NodeGeo:
    """Геометрия узла.

    Attributes:
        rect (FRect): прямоугольник узла.
        rows (list[FRect]): прямоугольники строк.
        type_x (float): абсцисса колонки типа (даталогическая диаграмма).
    """

    rect: FRect
    rows: list[FRect]
    type_x: float = 0.0


@dataclass
class LinkGeo:
    """Геометрия связи.

    Attributes:
        link (Link): сама связь.
        pts (list[Point]): точки ломаной.
        halves (tuple[list[Point], list[Point]]): половины ломаной (у A и у B).
        pa (Point): точка привязки у конца A.
        da (Direction): направление выхода у A.
        pb (Point): точка привязки у конца B.
        db (Direction): направление выхода у B.
        axis (Optional[str]): ось перетаскиваемого среднего звена (``"x"``, ``"y"`` или ``None``).
        labels (list[tuple[str, str, FRect]]): подписи ``(какая: "a" | "b" | "c", текст, прямоугольник)``.
        mid_pt (Optional[Point]): центр среднего звена (место ручки) или ``None``.
    """

    link: Link
    pts: list[Point]
    halves: tuple[list[Point], list[Point]]
    pa: Point
    da: Direction
    pb: Point
    db: Direction
    axis: Optional[str]
    labels: list[tuple[str, str, FRect]] = field(default_factory=list)
    mid_pt: Optional[Point] = None


@dataclass
class LegendGeo:
    """Геометрия легенды.

    Attributes:
        rect (FRect): рамка легенды.
        items (list[FRect]): прямоугольники пунктов.
        title (FRect): шапка (заголовок).
    """

    rect: FRect
    items: list[FRect]
    title: FRect


class Scene:
    """Размеры, маршруты и прямоугольники всей диаграммы в мировых координатах.

    Пересоздаётся при каждом изменении (это быстро); используется и для рисования, и для
    поиска объекта под курсором.

    Attributes:
        d (Diagram): диаграмма.
        nodes (dict[str, NodeGeo]): геометрия узлов по идентификатору.
        links (dict[str, LinkGeo]): геометрия связей по идентификатору.
        legend (Optional[LegendGeo]): геометрия легенды (``None``, если скрыта).
        notes (dict[str, FRect]): прямоугольники надписей.
    """

    def __init__(self, d: Diagram, fonts: Fonts) -> None:
        """Вычисляет геометрию диаграммы.

        Args:
            d (Diagram): диаграмма.
            fonts (Fonts): кэш шрифтов (нужен для измерения текста).

        Raises:
            KeyError: если вид диаграммы ``d.kind`` неизвестен.
            pygame.error: если шрифты pygame не инициализированы.
        """
        self.d, self.fonts = d, fonts
        m = fonts.measure
        s = STYLE[d.kind]
        self.nodes: dict[str, NodeGeo] = {}
        for n in d.nodes:
            r = node_rect(d, n)
            if d.kind == KIND_INFO:
                rows = [FRect(r.x + 4, r.y + s["head"] + i * s["row"], r.w - 8, s["row"]) for i in range(len(n.rows))]
                self.nodes[n.id] = NodeGeo(r, rows)
            else:
                rows = [FRect(r.x, r.y + s["head"] + i * s["row"], r.w, s["row"]) for i in range(len(n.rows))]
                _, type_off, _, _ = data_columns(n, m)
                self.nodes[n.id] = NodeGeo(r, rows, r.x + type_off)
        self.links: dict[str, LinkGeo] = {}
        for l in d.links:
            if l.a in self.nodes and l.b in self.nodes:
                self.links[l.id] = self._link_geo(l)
        self.legend: Optional[LegendGeo] = self._legend_geo() if d.legend.visible else None
        self.notes: dict[str, FRect] = {}
        for note in d.notes:
            w, h = m(note.text, note.size)
            self.notes[note.id] = FRect(note.x, note.y, w, h + 6)

    def _link_geo(self, l: Link) -> LinkGeo:
        """Вычисляет маршрут связи, её половины, ручку среднего звена и подписи.

        Args:
            l (Link): связь; оба её узла должны быть в ``self.nodes``.

        Returns:
            LinkGeo: геометрия связи.
        """
        ra, rb = self.nodes[l.a].rect, self.nodes[l.b].rect
        pa, da = anchor_point(ra, l.sa, l.ta)
        pb, db = anchor_point(rb, l.sb, l.tb)
        pts, axis = route(pa, da, pb, db, l.mid)
        pts = clean_points(pts)
        ha, hb = split_half(pts)
        lg = LinkGeo(l, pts, (ha, hb), pa, da, pb, db, axis)
        if axis and len(pts) >= 4:
            lg.mid_pt = ((pts[1][0] + pts[2][0]) / 2, (pts[1][1] + pts[2][1]) / 2)
        m = self.fonts.measure
        if self.d.kind == KIND_INFO:
            for which, p, dd, text, off in (("a", pa, da, l.name_a, l.off_a), ("b", pb, db, l.name_b, l.off_b)):
                if text:
                    lg.labels.append((which, text, self._end_label(p, dd, text, off)))
        elif l.label:
            w, h = m(l.label, LABEL_SIZE, False, True)
            best = max(zip(pts, pts[1:]), key=lambda s: hypot(s[1][0] - s[0][0], s[1][1] - s[0][1]))
            (x1, y1), (x2, y2) = best
            if abs(y1 - y2) < 0.01:
                rect = FRect((x1 + x2) / 2 - w / 2, y1 - h - 3, w, h)
            else:
                rect = FRect(x1 + 6, (y1 + y2) / 2 - h / 2, w, h)
            rect.x += l.off_c[0]
            rect.y += l.off_c[1]
            lg.labels.append(("c", l.label, rect))
        return lg

    def _end_label(self, p: Point, d: Direction, text: str, off: list[float]) -> FRect:
        """Вычисляет прямоугольник подписи у конца связи.

        Подпись ставится справа от вертикальной линии или над горизонтальной.

        Args:
            p (Point): точка привязки конца.
            d (Direction): направление выхода линии наружу.
            text (str): текст подписи.
            off (list[float]): ручной сдвиг подписи ``[dx, dy]``.

        Returns:
            FRect: прямоугольник подписи с учётом сдвига.
        """
        w, h = self.fonts.measure(text, LABEL_SIZE, False, True)
        if d[0] == 0:
            r = FRect(p[0] + 8, p[1] + d[1] * 36 - h / 2, w, h)
        else:
            x = p[0] + 30 if d[0] > 0 else p[0] - 30 - w
            r = FRect(x, p[1] - h - 3, w, h)
        r.x += off[0]
        r.y += off[1]
        return r

    def _legend_geo(self) -> LegendGeo:
        """Вычисляет рамку легенды (ширина расширяется под самый длинный текст) и строки пунктов.

        Returns:
            LegendGeo: геометрия легенды.
        """
        lg, m = self.d.legend, self.fonts.measure
        need = m(lg.title, 13, True)[0] + 24
        for it in lg.items:
            icon = 12 if it.kind == "text" else 72
            need = max(need, icon + m(it.text, 12)[0] + 14)
        w = max(lg.w, need)
        rect = FRect(lg.x, lg.y, w, LEGEND_HEAD + len(lg.items) * LEGEND_ROW + 10)
        items = [FRect(lg.x, lg.y + LEGEND_HEAD + i * LEGEND_ROW, w, LEGEND_ROW) for i in range(len(lg.items))]
        return LegendGeo(rect, items, FRect(lg.x, lg.y, w, LEGEND_HEAD))

    # ----- поиск под курсором (координаты — мировые) -----

    def node_at(self, wx: float, wy: float) -> Optional[str]:
        """Находит верхний узел под точкой.

        Args:
            wx (float): мировая абсцисса.
            wy (float): мировая ордината.

        Returns:
            Optional[str]: идентификатор узла или ``None``.
        """
        for n in reversed(self.d.nodes):
            if self.nodes[n.id].rect.contains(wx, wy):
                return n.id
        return None

    def row_at(self, nid: str, wy: float) -> Optional[int]:
        """Находит строку узла на заданной высоте.

        Args:
            nid (str): идентификатор узла.
            wy (float): мировая ордината.

        Returns:
            Optional[int]: индекс строки или ``None`` (шапка, нижний отступ).

        Raises:
            KeyError: если узла ``nid`` нет в сцене.
        """
        for i, r in enumerate(self.nodes[nid].rows):
            if r.y <= wy < r.bottom:
                return i
        return None

    def link_label_at(self, wx: float, wy: float) -> Optional[tuple[str, str]]:
        """Находит подпись связи под точкой (с допуском 3 единицы).

        Args:
            wx (float): мировая абсцисса.
            wy (float): мировая ордината.

        Returns:
            Optional[tuple[str, str]]: ``(идентификатор связи, подпись "a" | "b" | "c")`` или ``None``.
        """
        for lg in reversed(list(self.links.values())):
            for which, _, rect in lg.labels:
                if rect.contains(wx, wy, 3):
                    return lg.link.id, which
        return None

    def link_at(self, wx: float, wy: float, tol: float) -> Optional[str]:
        """Находит ближайшую связь, линия которой проходит рядом с точкой.

        Args:
            wx (float): мировая абсцисса.
            wy (float): мировая ордината.
            tol (float): допуск — наибольшее расстояние от точки до линии.

        Returns:
            Optional[str]: идентификатор связи или ``None``, если рядом нет линий.
        """
        best: Optional[tuple[float, str]] = None
        for lg in self.links.values():
            dist = geom.poly_dist(wx, wy, lg.pts)
            if dist <= tol and (best is None or dist < best[0]):
                best = (dist, lg.link.id)
        return best[1] if best else None

    def note_at(self, wx: float, wy: float) -> Optional[str]:
        """Находит надпись под точкой (с допуском 3 единицы).

        Args:
            wx (float): мировая абсцисса.
            wy (float): мировая ордината.

        Returns:
            Optional[str]: идентификатор надписи или ``None``.
        """
        for nid, r in reversed(list(self.notes.items())):
            if r.contains(wx, wy, 3):
                return nid
        return None

    def bounds(self) -> FRect:
        """Вычисляет прямоугольник, охватывающий всю диаграмму.

        Учитываются узлы, связи с подписями, легенда и надписи.

        Returns:
            FRect: охватывающий прямоугольник; для пустой диаграммы — ``FRect(0, 0, 400, 300)``.
        """
        rects = [g.rect for g in self.nodes.values()] + list(self.notes.values())
        if self.legend:
            rects.append(self.legend.rect)
        for lg in self.links.values():
            xs = [p[0] for p in lg.pts]
            ys = [p[1] for p in lg.pts]
            rects.append(FRect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)))
            rects += [r for _, _, r in lg.labels]
        if not rects:
            return FRect(0, 0, 400, 300)
        out = rects[0]
        for r in rects[1:]:
            out = out.union(r)
        return out


# ───────────────────────── рисование элементов ─────────────────────────

def draw_crow(g: Gfx, p: Point, d: Direction, color: Color = INK) -> None:
    """Рисует «воронью лапку»: пальцы касаются стороны узла, сходятся в точке на линии связи.

    Args:
        g (Gfx): графический контекст.
        p (Point): точка привязки на стороне узла.
        d (Direction): направление выхода линии наружу.
        color (Color): цвет линий.
    """
    cx, cy = p[0] + d[0] * CROW_LEN, p[1] + d[1] * CROW_LEN
    nx, ny = -d[1], d[0]
    g.line((cx, cy), (p[0] + nx * CROW_SPREAD, p[1] + ny * CROW_SPREAD), color)
    g.line((cx, cy), p, color)
    g.line((cx, cy), (p[0] - nx * CROW_SPREAD, p[1] - ny * CROW_SPREAD), color)


def draw_bar(g: Gfx, p: Point, d: Direction, color: Color = INK) -> None:
    """Рисует черту поперёк связи: связь входит в уникальный идентификатор сущности.

    Args:
        g (Gfx): графический контекст.
        p (Point): точка привязки на стороне узла.
        d (Direction): направление выхода линии наружу.
        color (Color): цвет черты.
    """
    cx, cy = p[0] + d[0] * BAR_OFFSET, p[1] + d[1] * BAR_OFFSET
    nx, ny = -d[1], d[0]
    g.line((cx - nx * BAR_HALF, cy - ny * BAR_HALF), (cx + nx * BAR_HALF, cy + ny * BAR_HALF), color, 1.8)


def draw_arrow(g: Gfx, tip: Point, d: tuple[float, float], color: Color = INK) -> None:
    """Рисует наконечник стрелки: остриё в точке на стороне узла, основание — снаружи.

    Args:
        g (Gfx): графический контекст.
        tip (Point): остриё (мировые координаты).
        d (tuple[float, float]): единичное направление от узла наружу (вдоль линии связи).
        color (Color): цвет заливки.
    """
    bx, by = tip[0] + d[0] * ARROW_LEN, tip[1] + d[1] * ARROW_LEN
    nx, ny = -d[1], d[0]
    g.polygon([tip, (bx + nx * ARROW_HALF, by + ny * ARROW_HALF), (bx - nx * ARROW_HALF, by - ny * ARROW_HALF)], color)


def _end_mark(g: Gfx, p: Point, d: Direction, dist: float, text: str, color: Color) -> None:
    """Подписывает кратность («1» или «N») рядом с концом связи.

    Args:
        g (Gfx): графический контекст.
        p (Point): точка привязки конца.
        d (Direction): направление от узла наружу.
        dist (float): расстояние от узла вдоль линии.
        text (str): подпись.
        color (Color): цвет текста.
    """
    nx, ny = -d[1], d[0]
    g.text(text, p[0] + d[0] * dist + nx * 10, p[1] + d[1] * dist + ny * 10, 12, color, True, anchor="m")


def draw_link(g: Gfx, lg: LinkGeo, kind: str, selected: bool = False, arrows: bool = False) -> None:
    """Рисует связь: две половины (сплошная — «должен», пунктир — «может»), лапки или стрелку, черты UID и подписи.

    Args:
        g (Gfx): графический контекст.
        lg (LinkGeo): геометрия связи.
        kind (str): вид диаграммы; черты UID рисуются только на инфологической.
        selected (bool): ``True`` — выделенная связь рисуется акцентным цветом.
        arrows (bool): ``True`` (только даталогическая) — вместо «вороньей лапки» стрелка от
            внешнего ключа (A) к первичному (B) и кратность «N» / «1» у концов.
    """
    l = lg.link
    col = ACCENT if selected else INK
    ha, hb = lg.halves
    g.poly(ha, col, 1.5, dashed=not l.must_a)
    g.poly(hb, col, 1.5, dashed=not l.must_b)
    if arrows and kind == KIND_DATA:
        draw_arrow(g, lg.pb, lg.db, col)
        _end_mark(g, lg.pa, lg.da, 16, "N" if l.card_a == "M" else "1", MUTED)
        _end_mark(g, lg.pb, lg.db, ARROW_LEN + 8, "N" if l.card_b == "M" else "1", MUTED)
    else:
        if l.card_a == "M":
            draw_crow(g, lg.pa, lg.da, col)
        if l.card_b == "M":
            draw_crow(g, lg.pb, lg.db, col)
    if kind == KIND_INFO:
        if l.bar_a:
            draw_bar(g, lg.pa, lg.da, col)
        if l.bar_b:
            draw_bar(g, lg.pb, lg.db, col)
    for _which, text, r in lg.labels:
        g.text(text, r.x, r.y + r.h / 2, LABEL_SIZE, ACCENT if selected else MUTED, italic=True)


def draw_node(g: Gfx, d: Diagram, n: Node, geo: NodeGeo, selected: bool, sel_row: Optional[int],
              flag: Optional[Flag] = None) -> None:
    """Рисует узел: сущность со скруглёнными углами (Баркер) или таблицу с колонками ключ / имя / тип / NULL.

    Args:
        g (Gfx): графический контекст.
        d (Diagram): диаграмма (определяет вид оформления).
        n (Node): узел.
        geo (NodeGeo): геометрия узла.
        selected (bool): ``True`` — рисовать рамку выделения.
        sel_row (Optional[int]): индекс подсвечиваемой строки (учитывается, если узел выделен).
        flag (Optional[Flag]): подсветка нарушений нормальной формы (только даталогическая; ``None`` — без неё).
    """
    r = geo.rect
    head, body = PALETTE[n.color % len(PALETTE)]
    outline = ACCENT if selected else INK
    flag_col = (FLAG_ERROR if flag.level == "error" else FLAG_WARN) if flag else None
    if flag_col and not selected:
        outline = flag_col
    if d.kind == KIND_INFO:
        g.rect(r, hexrgb(body), outline, 2.4 if selected else 1.6, radius=14)
        g.text(n.title, r.cx, r.y + 20, TITLE_SIZE[KIND_INFO], INK, True, anchor="m")
        for i, row in enumerate(n.rows):
            rr = geo.rows[i]
            if selected and sel_row == i:
                g.rect(FRect(r.x + 6, rr.y, r.w - 12, rr.h), ROW_SEL, radius=4)
            g.text(row.mark, r.x + 18, rr.y + rr.h / 2, ATTR_SIZE[KIND_INFO], INK, row.mark == "#", anchor="m")
            g.text(row.name, r.x + 32, rr.y + rr.h / 2, ATTR_SIZE[KIND_INFO], INK)
    else:
        s = STYLE[KIND_DATA]
        g.rect(r, WHITE)
        g.rect(FRect(r.x, r.y, r.w, s["head"]), hexrgb(head))
        g.line((r.x + 52, r.y + s["head"]), (r.x + 52, r.bottom), GRID_COL, 1)
        for i, row in enumerate(n.rows):
            rr = geo.rows[i]
            if selected and sel_row == i:
                g.rect(rr, ROW_SEL)
            elif flag and i in flag.rows:
                g.rect(rr, FLAG_ERROR_TINT if flag.rows[i] == "error" else FLAG_WARN_TINT)
            if i and not (selected and sel_row == i):
                g.line((r.x, rr.y), (r.right, rr.y), GRID_COL, 1)
            yc = rr.y + rr.h / 2
            is_pk = "PK" in row.key
            g.text(row.key, r.x + 26, yc, 10, BRICK if is_pk else MUTED, True, anchor="m")
            g.text(row.name, r.x + 60, yc, ATTR_SIZE[KIND_DATA], INK, is_pk)
            g.text(row.type, geo.type_x, yc, TYPE_SIZE, MUTED)
            g.text(NN_TEXT if row.notnull else "NULL", r.right - 10, yc, 11, INK if row.notnull else FAINT, anchor="r")
        g.rect(r, None, outline, 2.4 if selected or flag_col else 1.4)
        g.text(n.title, r.cx, r.y + s["head"] / 2, TITLE_SIZE[KIND_DATA] + 1, INK, True, anchor="m")
        if flag_col:
            g.circle((r.right - 16, r.y + s["head"] / 2), 9, flag_col, flag_col, 1)
            g.text("!", r.right - 16, r.y + s["head"] / 2, 14, WHITE, True, anchor="m")


def draw_legend(g: Gfx, d: Diagram, geo: LegendGeo, selected: bool, sel_item: Optional[int]) -> None:
    """Рисует легенду: рамку, заголовок и пункты с образцами значков (маркеры, линии, лапка, черта).

    Args:
        g (Gfx): графический контекст.
        d (Diagram): диаграмма (из неё берётся содержимое легенды).
        geo (LegendGeo): геометрия легенды.
        selected (bool): ``True`` — рисовать рамку выделения.
        sel_item (Optional[int]): индекс подсвечиваемого пункта (учитывается, если легенда выделена).
    """
    lg = d.legend
    r = geo.rect
    g.rect(r, LEGEND_FILL, ACCENT if selected else GRID_COL, 2 if selected else 1)
    g.text(lg.title, r.x + 12, r.y + 18, 13, INK, True)
    for i, it in enumerate(lg.items):
        rr = geo.items[i]
        yc = rr.y + rr.h / 2
        if selected and sel_item == i:
            g.rect(FRect(r.x + 3, rr.y + 1, r.w - 6, rr.h - 2), ROW_SEL, radius=3)
        x0, x1 = r.x + 12, r.x + 62
        k = it.kind
        if k.startswith("mark_"):
            g.text(k[5:], (x0 + x1) / 2 - 12, yc, 13, INK, k == "mark_#", anchor="m")
        elif k in ("solid", "one", "crow", "bar", "arrow"):
            g.line((x0, yc), (x1, yc))
            if k == "arrow":
                draw_arrow(g, (x1, yc), (-1, 0))
            elif k == "crow":
                draw_crow(g, (x0, yc), (1, 0))
            elif k == "bar":
                draw_bar(g, (x0 - 8, yc), (1, 0))
        elif k == "dashed":
            g.poly([(x0, yc), (x1, yc)], dashed=True)
        tx = r.x + (12 if k == "text" else 72)
        g.text(it.text, tx, yc, 12, INK)


def draw_grid(g: Gfx, view: pygame.Rect) -> None:
    """Рисует точечную сетку в видимой области (шаг 20 единиц, удваивается при мелком масштабе).

    Args:
        g (Gfx): графический контекст.
        view (pygame.Rect): видимая экранная область, которую нужно покрыть точками.
    """
    step = 20.0
    while step * g.z < 12:
        step *= 2
    x0, y0 = (view.left - g.ox) / g.z, (view.top - g.oy) / g.z
    x1, y1 = (view.right - g.ox) / g.z, (view.bottom - g.oy) / g.z
    gx = int(x0 // step) * step
    while gx <= x1:
        gy = int(y0 // step) * step
        while gy <= y1:
            sx, sy = g.p((gx, gy))
            g.surf.fill(DOT_COL, (round(sx) - 1, round(sy) - 1, 2, 2))
            gy += step
        gx += step


def draw_diagram(g: Gfx, scene: Scene, sel: Optional[Sel] = None, flags: Optional[dict[str, Flag]] = None) -> None:
    """Рисует диаграмму целиком: связи, узлы, надписи, легенду и ручки выделенного объекта.

    Args:
        g (Gfx): графический контекст.
        scene (Scene): геометрия диаграммы.
        sel (Optional[Sel]): выделение; ``None`` (например, при экспорте) — без рамок и ручек.
        flags (Optional[dict[str, Flag]]): подсветка проблемных таблиц по идентификатору узла
            (на экране); ``None`` — без подсветки (так рисуется экспорт).
    """
    d = scene.d
    sel = sel or Sel()
    flags = flags or {}
    for lid, lg in scene.links.items():
        draw_link(g, lg, d.kind, sel.kind == "link" and sel.id == lid, d.arrows)
    for n in d.nodes:
        draw_node(g, d, n, scene.nodes[n.id], sel.kind == "node" and sel.id == n.id, sel.row, flags.get(n.id))
    for note in d.notes:
        r = scene.notes[note.id]
        g.text(note.text, r.x, r.y + r.h / 2, note.size, ACCENT if (sel.kind == "note" and sel.id == note.id) else INK)
        if sel.kind == "note" and sel.id == note.id:
            g.rect(r.inflated(3), None, ACCENT, 1)
    if scene.legend:
        draw_legend(g, d, scene.legend, sel.kind == "legend", sel.row)
    # ручки выделенных объектов
    if sel.kind == "link" and sel.id in scene.links:
        lg = scene.links[sel.id]
        g.circle(lg.pa, 6)
        g.circle(lg.pb, 6)
        if lg.mid_pt:
            g.handle(lg.mid_pt, ACCENT, ACCENT, 4)
    elif sel.kind == "node" and sel.id in scene.nodes:
        r = scene.nodes[sel.id].rect
        g.handle((r.right, r.cy))
    elif sel.kind == "legend" and scene.legend:
        r = scene.legend.rect
        g.handle((r.right, r.cy))


# ───────────────────────── экспорт PNG ─────────────────────────

MAX_EXPORT_PIXELS = 60_000_000


def export_png(d: Diagram, fonts: Fonts, path: str, scale: float = 2.0, pad: float = 30.0) -> tuple[int, int]:
    """Рисует диаграмму целиком и сохраняет PNG с суперсэмплингом ×2 (сглаживание линий).

    Размер ограничен :data:`MAX_EXPORT_PIXELS`: при необходимости сначала отключается
    суперсэмплинг, затем уменьшается масштаб.

    Args:
        d (Diagram): диаграмма.
        fonts (Fonts): кэш шрифтов.
        path (str): путь к выходному файлу ``.png``.
        scale (float): множитель разрешения относительно мировых единиц (1, 2, 3).
        pad (float): поля вокруг диаграммы в мировых единицах.

    Returns:
        tuple[int, int]: размер сохранённого изображения ``(ширина, высота)`` в пикселях.

    Raises:
        pygame.error: если файл не удалось записать или не хватает памяти под поверхность.
        KeyError: если вид диаграммы ``d.kind`` неизвестен.
    """
    scene = Scene(d, fonts)
    b = scene.bounds()
    bw, bh = b.w + 2 * pad, b.h + 2 * pad
    ss = 2
    while ss > 1 and bw * bh * (scale * ss) ** 2 > MAX_EXPORT_PIXELS:
        ss -= 1
    while bw * bh * (scale * ss) ** 2 > MAX_EXPORT_PIXELS and scale > 1:
        scale -= 1
    z = scale * ss
    W, H = ceil(bw * z), ceil(bh * z)
    surf = pygame.Surface((W, H), 0, 32)
    surf.fill(WHITE)
    g = Gfx(surf, fonts, (pad - b.x) * z, (pad - b.y) * z, z)
    draw_diagram(g, scene, None)
    out = pygame.transform.smoothscale(surf, (W // ss, H // ss)) if ss > 1 else surf
    pygame.image.save(out, path)
    return out.get_size()

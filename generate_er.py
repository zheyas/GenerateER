#!/usr/bin/env python3
"""Инфологическая (нотация Баркера) и даталогическая модели БД SportPit.db.

Запуск:   python3 generate_er.py [путь_к_БД]
Результат — каталог out/:
    infological.svg / .png   — инфологическая модель (ER, нотация Баркера)
    datalogical.svg / .png   — даталогическая модель (таблицы SQLite, ключи, типы)
    models.md                — текстовое описание обеих моделей для отчёта

Даталогическая модель читается из самой БД (PRAGMA), поэтому после изменения схемы
достаточно перезапустить скрипт. Расположение сущностей задано вручную по правилу
Баркера «dead crows fly east»: «вороньи лапки» смотрят только вверх или влево —
детальные сущности (многие) стоят выше/левее, главные (справочные) — ниже/правее.

Это первая, «ручная» версия генератора. Интерактивный редактор с автоматической
раскладкой для любой базы данных — пакет ``er_editor`` (см. README.md).
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import sys
from math import hypot
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape

Point = tuple[float, float]
Direction = tuple[int, int]            # единичный вектор выхода линии: (0, -1) вверх, (1, 0) вправо…
Entity = dict[str, Any]                # описание сущности инфологической модели (см. ENTITIES)
Relationship = dict[str, Any]          # описание связи Баркера (см. RELATIONSHIPS)
TableInfo = dict[str, Any]             # описание таблицы БД, прочитанное из PRAGMA (см. read_schema)

DB_PATH = Path(sys.argv[1] if len(sys.argv) > 1 else "/Users/evgenijasakov/NutriSport/SportPit.db")
OUT = Path(__file__).resolve().parent / "out"

FONT = "Arial, Helvetica, sans-serif"
INK = "#1f2933"
MUTED = "#52606d"
GRID = "#d9dee4"


# ───────────────────────── геометрия и примитивы ─────────────────────────

class Box:
    """Прямоугольник сущности или таблицы с умением выдавать точки привязки связей.

    Attributes:
        x: абсцисса левого верхнего угла.
        y: ордината левого верхнего угла.
        w: ширина.
        h: высота.
    """

    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        """Создаёт прямоугольник.

        Args:
            x (float): абсцисса левого верхнего угла.
            y (float): ордината левого верхнего угла.
            w (float): ширина.
            h (float): высота.
        """
        self.x, self.y, self.w, self.h = x, y, w, h

    def anchor(self, side: str, offset: float) -> tuple[Point, Direction]:
        """Точка на стороне прямоугольника и направление выхода линии наружу.

        Args:
            side (str): сторона — ``"top"``, ``"bottom"``, ``"left"``; любое другое значение
                считается правой стороной (``"right"``).
            offset (float): смещение точки от левого (для верхней и нижней сторон) или
                верхнего (для левой и правой сторон) угла.

        Returns:
            tuple[Point, Direction]: пара ``((x, y), (dx, dy))`` — точка привязки и единичный
            вектор, направленный от прямоугольника наружу.
        """
        if side == "top":
            return (self.x + offset, self.y), (0, -1)
        if side == "bottom":
            return (self.x + offset, self.y + self.h), (0, 1)
        if side == "left":
            return (self.x, self.y + offset), (-1, 0)
        return (self.x + self.w, self.y + offset), (1, 0)


def route(p1: Point, d1: Direction, p2: Point, d2: Direction, mid: Optional[float] = None) -> list[Point]:
    """Ортогональный маршрут между двумя точками выхода (прямая, L или Z).

    Args:
        p1 (Point): точка выхода у конца A.
        d1 (Direction): направление выхода у конца A.
        p2 (Point): точка выхода у конца B.
        d2 (Direction): направление выхода у конца B.
        mid (Optional[float]): положение среднего звена Z-образного маршрута (ордината для
            вертикальных выходов, абсцисса для горизонтальных); ``None`` — посередине.

    Returns:
        list[Point]: вершины ломаной от ``p1`` до ``p2`` (две точки для прямой, три для L,
        четыре для Z).
    """
    (x1, y1), (x2, y2) = p1, p2
    v1, v2 = d1[0] == 0, d2[0] == 0
    if v1 and v2:
        if x1 == x2:
            return [p1, p2]
        my = mid if mid is not None else (y1 + y2) / 2
        return [p1, (x1, my), (x2, my), p2]
    if not v1 and not v2:
        if y1 == y2:
            return [p1, p2]
        mx = mid if mid is not None else (x1 + x2) / 2
        return [p1, (mx, y1), (mx, y2), p2]
    return [p1, (x1, y2), p2] if v1 else [p1, (x2, y1), p2]


def split_half(pts: list[Point]) -> tuple[list[Point], list[Point]]:
    """Делит ломаную пополам по длине: половина у A и половина у B.

    Args:
        pts (list[Point]): вершины ломаной от конца A к концу B.

    Returns:
        tuple[list[Point], list[Point]]: ``(половина у A, половина у B)``; точка деления входит
        в обе половины. Для вырожденной ломаной (нулевой длины) — ``(pts, последняя точка)``.
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
    return pts, pts[-1:]


def polyline(pts: list[Point], dashed: bool = False, color: str = INK, width: float = 1.5) -> str:
    """SVG-элемент ``<polyline>`` для ломаной линии.

    Args:
        pts (list[Point]): вершины ломаной.
        dashed (bool): ``True`` — пунктирная линия (штрих 7, промежуток 5).
        color (str): цвет обводки (``#rrggbb``).
        width (float): толщина линии.

    Returns:
        str: готовая SVG-строка элемента.
    """
    dash = ' stroke-dasharray="7 5"' if dashed else ""
    p = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return f'<polyline points="{p}" fill="none" stroke="{color}" stroke-width="{width}"{dash}/>'


def line(x1: float, y1: float, x2: float, y2: float, color: str = INK, width: float = 1.5) -> str:
    """SVG-элемент ``<line>`` — отрезок.

    Args:
        x1 (float): абсцисса начала.
        y1 (float): ордината начала.
        x2 (float): абсцисса конца.
        y2 (float): ордината конца.
        color (str): цвет линии.
        width (float): толщина линии.

    Returns:
        str: готовая SVG-строка элемента.
    """
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="{width}"/>'


def text(x: float, y: float, s: str, size: float = 13, weight: str = "normal", anchor: str = "start",
         color: str = INK, style: str = "normal") -> str:
    """SVG-элемент ``<text>`` с экранированием спецсимволов.

    Args:
        x (float): абсцисса точки привязки текста.
        y (float): ордината базовой линии текста.
        s (str): сам текст (символы ``& < >`` экранируются).
        size (float): размер шрифта.
        weight (str): ``"normal"`` или ``"bold"``.
        anchor (str): выравнивание относительно ``x``: ``"start"``, ``"middle"``, ``"end"``.
        color (str): цвет текста.
        style (str): ``"normal"`` или ``"italic"``.

    Returns:
        str: готовая SVG-строка элемента.
    """
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
            f'font-weight="{weight}" font-style="{style}" text-anchor="{anchor}" fill="{color}">{escape(s)}</text>')


CROW_LEN, CROW_SPREAD = 16, 9


def crows_foot(p: Point, d: Direction) -> list[str]:
    """«Воронья лапка»: пальцы касаются сущности, сходятся в точке на линии.

    Args:
        p (Point): точка привязки на стороне сущности.
        d (Direction): направление выхода линии наружу.

    Returns:
        list[str]: три SVG-отрезка (два боковых пальца и средний).
    """
    cx, cy = p[0] + d[0] * CROW_LEN, p[1] + d[1] * CROW_LEN
    nx, ny = -d[1], d[0]
    return [line(cx, cy, p[0] + nx * CROW_SPREAD, p[1] + ny * CROW_SPREAD),
            line(cx, cy, p[0], p[1]),
            line(cx, cy, p[0] - nx * CROW_SPREAD, p[1] - ny * CROW_SPREAD)]


def uid_bar(p: Point, d: Direction) -> str:
    """Черта поперёк связи: связь входит в уникальный идентификатор сущности.

    Args:
        p (Point): точка привязки на стороне сущности.
        d (Direction): направление выхода линии наружу.

    Returns:
        str: SVG-отрезок, перпендикулярный линии связи.
    """
    cx, cy = p[0] + d[0] * (CROW_LEN + 8), p[1] + d[1] * (CROW_LEN + 8)
    nx, ny = -d[1], d[0]
    return line(cx - nx * 10, cy - ny * 10, cx + nx * 10, cy + ny * 10, width=1.8)


def end_label(p: Point, d: Direction, s: str, pos: str, dist: Optional[float] = None) -> str:
    """Имя связи у своего конца: справа/слева от вертикали, над/под горизонталью.

    Args:
        p (Point): точка привязки конца связи.
        d (Direction): направление выхода линии наружу.
        s (str): имя связи («относится к», «имеет»…).
        pos (str): сторона размещения: ``"left"`` / ``"right"`` для вертикального выхода,
            ``"above"`` / ``"below"`` для горизонтального.
        dist (Optional[float]): расстояние подписи от сущности вдоль линии (по умолчанию 40 для
            вертикального и 30 для горизонтального выхода).

    Returns:
        str: SVG-элемент курсивной подписи.
    """
    if d[0] == 0:
        dist = dist or 40
        y = p[1] + d[1] * dist + 4
        if pos == "left":
            return text(p[0] - 8, y, s, 12.5, anchor="end", color=MUTED, style="italic")
        return text(p[0] + 8, y, s, 12.5, color=MUTED, style="italic")
    dist = dist or 30
    x = p[0] + d[0] * dist
    y = p[1] - 7 if pos == "above" else p[1] + 17
    return text(x, y, s, 12.5, anchor="start" if d[0] > 0 else "end", color=MUTED, style="italic")


def relationship(box_a: Box, box_b: Box, r: Relationship) -> list[str]:
    """Связь в нотации Баркера.

    Половина линии у сущности сплошная, если участие обязательно («должен»),
    пунктирная — если нет («может»). Лапка у сущности — «один или несколько».

    Args:
        box_a (Box): прямоугольник сущности на конце A (подчинённой, «много»).
        box_b (Box): прямоугольник сущности на конце B (главной).
        r (Relationship): описание связи. Обязательные ключи: ``sa``, ``oa``, ``sb``, ``ob``
            (сторона и смещение привязки), ``card_a``, ``card_b`` (``"M"`` / ``"1"``),
            ``must_a``, ``must_b``. Необязательные: ``mid``, ``bar_a``, ``bar_b``, ``name_a``,
            ``pos_a``, ``dist_a``, ``name_b``, ``pos_b``, ``dist_b``, ``note``, ``note_text``.

    Returns:
        list[str]: SVG-элементы связи: половины линии, лапки, черты UID, подписи.

    Raises:
        KeyError: если в ``r`` нет обязательного ключа (или нет ``pos_a`` / ``pos_b`` при
            заданных именах концов).
    """
    pa, da = box_a.anchor(r["sa"], r["oa"])
    pb, db = box_b.anchor(r["sb"], r["ob"])
    pts = route(pa, da, pb, db, r.get("mid"))
    half_a, half_b = split_half(pts)
    out = [polyline(half_a, dashed=not r["must_a"]), polyline(half_b, dashed=not r["must_b"])]
    if r["card_a"] == "M":
        out += crows_foot(pa, da)
    if r["card_b"] == "M":
        out += crows_foot(pb, db)
    if r.get("bar_a"):
        out.append(uid_bar(pa, da))
    if r.get("bar_b"):
        out.append(uid_bar(pb, db))
    if r.get("name_a"):
        out.append(end_label(pa, da, r["name_a"], r["pos_a"], r.get("dist_a")))
    if r.get("name_b"):
        out.append(end_label(pb, db, r["name_b"], r["pos_b"], r.get("dist_b")))
    if r.get("note"):
        nx, ny, anchor = r["note"]
        out.append(text(nx, ny, r["note_text"], 11, anchor=anchor, color="#7b8794"))
    return out


def svg_doc(width: float, height: float, body: list[str]) -> str:
    """Оборачивает SVG-элементы в готовый документ с белым фоном.

    Args:
        width (float): ширина холста.
        height (float): высота холста.
        body (list[str]): SVG-элементы (результат ``relationship``, ``draw_entity`` и т. п.).

    Returns:
        str: полный текст SVG-файла.
    """
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">\n<rect width="100%" height="100%" fill="#ffffff"/>\n'
            + "\n".join(body) + "\n</svg>\n")


# ───────────────────────── инфологическая модель ─────────────────────────
# Маркеры атрибутов Баркера: # — входит в уникальный идентификатор,
# * — обязательный, o — необязательный (по NOT NULL в схеме БД).

ENTITY_W = 240

ENTITIES: dict[str, Entity] = {
    "order_reg": dict(title="ОФОРМЛЕНИЕ ЗАКАЗА", table="user_order_address", x=440, y=40,
                      attrs=[("*", "дата оформления")],
                      uid="ПОЛЬЗОВАТЕЛЬ + ЗАКАЗ + АДРЕС (через связи)"),
    "order": dict(title="ЗАКАЗ", table="orders", x=1240, y=40,
                  attrs=[("#", "номер заказа"), ("*", "количество"), ("*", "дата заказа"),
                         ("*", "общая стоимость"), ("o", "статус")],
                  uid="номер заказа"),
    "address": dict(title="АДРЕС", table="addresses", x=40, y=230,
                    attrs=[("#", "код адреса"), ("*", "страна"), ("*", "тип населённого пункта"),
                           ("*", "населённый пункт"), ("*", "тип улицы"), ("*", "улица"),
                           ("*", "номер дома"), ("o", "квартира")],
                    uid="код адреса"),
    "cart": dict(title="ПОЗИЦИЯ КОРЗИНЫ", table="cart", x=840, y=270,
                 attrs=[("*", "количество"), ("*", "дата добавления")],
                 uid="ПОЛЬЗОВАТЕЛЬ + ТОВАР (через связи)"),
    "product": dict(title="ТОВАР", table="products", x=1240, y=290,
                    attrs=[("#", "код товара"), ("*", "название"), ("o", "описание"), ("*", "цена"),
                           ("*", "остаток на складе"), ("*", "вес"), ("o", "изображение"),
                           ("o", "категория")],
                    uid="код товара"),
    "user": dict(title="ПОЛЬЗОВАТЕЛЬ", table="users", x=440, y=500,
                 attrs=[("#", "код пользователя"), ("*", "фамилия"), ("*", "имя"), ("o", "отчество"),
                        ("o", "дата рождения"), ("o", "телефон"), ("o", "e-mail"), ("o", "VIP-статус"),
                        ("o", "фото")],
                 uid="код пользователя"),
    "session": dict(title="СЕССИЯ", table="user_sessions", x=40, y=560,
                    attrs=[("#", "идентификатор сессии"), ("o", "дата создания"), ("*", "срок действия"),
                           ("o", "последняя активность")],
                    uid="идентификатор сессии"),
    "account": dict(title="УЧЁТНАЯ ЗАПИСЬ", table="user_credentials", x=840, y=600,
                    attrs=[("*", "логин (уникальный)"), ("*", "хеш пароля"), ("*", "роль"),
                           ("o", "дата создания"), ("o", "дата изменения")],
                    uid="ПОЛЬЗОВАТЕЛЬ (через связь)"),
}

# a — сущность «многие» (детальная), b — главная. sa/sb — сторона, oa/ob — смещение точки.
# Лапки висят только на нижней или правой стороне детальной сущности → смотрят вверх/влево.
RELATIONSHIPS: list[Relationship] = [
    dict(a="order_reg", sa="right", oa=32, b="order", sb="left", ob=32,
         card_a="M", must_a=True, card_b="1", must_b=False, bar_a=True,
         name_a="относится к", pos_a="above", name_b="имеет", pos_b="above",
         read_a="Каждое ОФОРМЛЕНИЕ ЗАКАЗА должно относиться к одному и только одному ЗАКАЗУ",
         read_b="Каждый ЗАКАЗ может иметь одно или несколько ОФОРМЛЕНИЙ ЗАКАЗА"),
    dict(a="order_reg", sa="bottom", oa=40, b="address", sb="top", ob=120, mid=170,
         card_a="M", must_a=True, card_b="1", must_b=False, bar_a=True,
         name_a="указывает", pos_a="left", name_b="указан в", pos_b="right", dist_b=22,
         read_a="Каждое ОФОРМЛЕНИЕ ЗАКАЗА должно указывать один и только один АДРЕС",
         read_b="Каждый АДРЕС может быть указан в одном или нескольких ОФОРМЛЕНИЯХ ЗАКАЗА"),
    dict(a="order_reg", sa="bottom", oa=150, b="user", sb="top", ob=150,
         card_a="M", must_a=True, card_b="1", must_b=False, bar_a=True,
         name_a="выполнено", pos_a="right", name_b="выполняет", pos_b="right",
         read_a="Каждое ОФОРМЛЕНИЕ ЗАКАЗА должно быть выполнено одним и только одним ПОЛЬЗОВАТЕЛЕМ",
         read_b="Каждый ПОЛЬЗОВАТЕЛЬ может выполнять одно или несколько ОФОРМЛЕНИЙ ЗАКАЗА"),
    dict(a="order", sa="bottom", oa=120, b="product", sb="top", ob=120,
         card_a="M", must_a=True, card_b="1", must_b=False,
         name_a="оформлен на", pos_a="right", name_b="заказан в", pos_b="left", dist_b=22,
         read_a="Каждый ЗАКАЗ должен быть оформлен на один и только один ТОВАР",
         read_b="Каждый ТОВАР может быть заказан в одном или нескольких ЗАКАЗАХ"),
    dict(a="cart", sa="right", oa=45, b="product", sb="left", ob=25,
         card_a="M", must_a=True, card_b="1", must_b=False, bar_a=True,
         name_a="содержит", pos_a="above", name_b="входит в", pos_b="below",
         read_a="Каждая ПОЗИЦИЯ КОРЗИНЫ должна содержать один и только один ТОВАР",
         read_b="Каждый ТОВАР может входить в одну или несколько ПОЗИЦИЙ КОРЗИНЫ"),
    dict(a="cart", sa="bottom", oa=60, b="user", sb="right", ob=60,
         card_a="M", must_a=True, card_b="1", must_b=False, bar_a=True,
         name_a="добавлена", pos_a="right", name_b="добавляет", pos_b="above",
         read_a="Каждая ПОЗИЦИЯ КОРЗИНЫ должна быть добавлена одним и только одним ПОЛЬЗОВАТЕЛЕМ",
         read_b="Каждый ПОЛЬЗОВАТЕЛЬ может добавлять одну или несколько ПОЗИЦИЙ КОРЗИНЫ"),
    dict(a="session", sa="right", oa=60, b="user", sb="left", ob=120,
         card_a="M", must_a=True, card_b="1", must_b=False,
         name_a="открыта", pos_a="above", name_b="открывает", pos_b="below", dist_b=12,
         read_a="Каждая СЕССИЯ должна быть открыта одним и только одним ПОЛЬЗОВАТЕЛЕМ",
         read_b="Каждый ПОЛЬЗОВАТЕЛЬ может открывать одну или несколько СЕССИЙ"),
    dict(a="account", sa="left", oa=70, b="user", sb="right", ob=170,
         card_a="1", must_a=True, card_b="1", must_b=False, bar_a=True,
         name_a="принадлежит", pos_a="below", name_b="имеет", pos_b="above", dist_b=12,
         read_a="Каждая УЧЁТНАЯ ЗАПИСЬ должна принадлежать одному и только одному ПОЛЬЗОВАТЕЛЮ",
         read_b="Каждый ПОЛЬЗОВАТЕЛЬ может иметь одну и только одну УЧЁТНУЮ ЗАПИСЬ"),
]


def entity_height(n_attrs: int) -> int:
    """Высота прямоугольника сущности в зависимости от числа атрибутов.

    Args:
        n_attrs (int): количество атрибутов (не менее 1).

    Returns:
        int: высота в пикселях: 64 для одного атрибута, +19 на каждый следующий.
    """
    return 64 + (n_attrs - 1) * 19


def draw_entity(e: Entity) -> tuple[Box, list[str]]:
    """Рисует сущность: скруглённый прямоугольник, заголовок и атрибуты с маркерами.

    Args:
        e (Entity): описание сущности с ключами ``x``, ``y``, ``title`` и ``attrs``
            (список пар ``(маркер, имя)``).

    Returns:
        tuple[Box, list[str]]: прямоугольник сущности (для привязки связей) и SVG-элементы.

    Raises:
        KeyError: если в ``e`` нет одного из обязательных ключей.
    """
    box = Box(e["x"], e["y"], ENTITY_W, entity_height(len(e["attrs"])))
    out = [f'<rect x="{box.x}" y="{box.y}" width="{box.w}" height="{box.h}" rx="16" ry="16" '
           f'fill="#ffffff" stroke="{INK}" stroke-width="1.6"/>',
           text(box.x + box.w / 2, box.y + 25, e["title"], 15, "bold", "middle")]
    for i, (mark, name) in enumerate(e["attrs"]):
        y = box.y + 48 + i * 19
        out.append(text(box.x + 18, y, mark, 13.5, "bold" if mark == "#" else "normal", "middle"))
        out.append(text(box.x + 32, y, name, 13.5))
    return box, out


def draw_barker_legend(x: float, y: float) -> list[str]:
    """Рисует легенду обозначений нотации Баркера.

    Args:
        x (float): абсцисса левого верхнего угла легенды.
        y (float): ордината левого верхнего угла легенды.

    Returns:
        list[str]: SVG-элементы легенды (рамка, заголовок, образцы линий и маркеров).
    """
    w, h = 240, 214
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#f8fafc" stroke="{GRID}"/>',
           text(x + 12, y + 22, "Условные обозначения", 13, "bold")]
    rows = [("#", "атрибут UID"), ("*", "обязательный атрибут"),
            ("o", "необязательный атрибут")]
    for i, (m, s) in enumerate(rows):
        yy = y + 44 + i * 18
        out += [text(x + 20, yy, m, 13, "bold", "middle"), text(x + 34, yy, s, 12)]
    yy = y + 104
    out += [line(x + 12, yy, x + 62, yy), text(x + 72, yy + 4, "должен (обязательно)", 12)]
    yy += 22
    out += [polyline([(x + 12, yy), (x + 62, yy)], dashed=True), text(x + 72, yy + 4, "может (необязательно)", 12)]
    yy += 24
    out += [line(x + 12, yy, x + 62, yy)] + crows_foot((x + 12, yy), (1, 0))[::2]
    out.append(text(x + 72, yy + 4, "один или несколько", 12))
    yy += 22
    out += [line(x + 12, yy, x + 62, yy), text(x + 72, yy + 4, "один и только один", 12)]
    yy += 24
    out += [line(x + 12, yy, x + 62, yy), line(x + 36, yy - 9, x + 36, yy + 9, width=1.8),
            text(x + 72, yy + 4, "связь входит в UID", 12)]
    return out


def build_infological() -> str:
    """Собирает SVG инфологической модели из ``ENTITIES`` и ``RELATIONSHIPS``.

    Связи рисуются под сущностями, чтобы концы линий не перекрывали рамки.

    Returns:
        str: полный текст SVG-документа (1520 × 790).

    Raises:
        KeyError: если связь ссылается на несуществующую сущность или в описании не хватает ключей.
    """
    boxes: dict[str, Box] = {}
    body: list[str] = []
    for key, e in ENTITIES.items():
        boxes[key], shapes = draw_entity(e)
        body += shapes
    rel_svg: list[str] = []
    for r in RELATIONSHIPS:
        rel_svg += relationship(boxes[r["a"]], boxes[r["b"]], r)
    body = rel_svg + body          # сущности поверх линий
    body += draw_barker_legend(1240, 540)
    return svg_doc(1520, 790, body)


# ───────────────────────── даталогическая модель ─────────────────────────

TABLE_W, HEAD_H, ROW_H = 320, 30, 22

TABLE_POS: dict[str, tuple[int, int]] = {
    "user_order_address": (490, 40),
    "orders": (1390, 84),
    "addresses": (40, 200),
    "cart": (940, 330),
    "products": (1390, 374),
    "users": (490, 560),
    "user_sessions": (40, 538),
    "user_credentials": (940, 679),
}

# (таблица, FK-поле) → маршрут линии. Смещение "row:<поле>" — середина строки поля.
FK_ROUTES: dict[tuple[str, str], dict[str, Any]] = {
    ("user_order_address", "order_id"): dict(sa="right", oa="row:order_id", sb="left", ob="row:id",
                                             note=(1100, 118, "middle")),
    ("user_order_address", "address_id"): dict(sa="bottom", oa=50, sb="right", ob="row:id",
                                               note=(372, 234, "start")),
    ("user_order_address", "user_id"): dict(sa="bottom", oa=210, sb="top", ob=210,
                                            note=(708, 390, "start")),
    ("orders", "product_id"): dict(sa="bottom", oa=160, sb="top", ob=160, note=(1558, 318, "start")),
    ("cart", "product_id"): dict(sa="right", oa="row:product_id", sb="left", ob="row:id",
                                 note=(1325, 432, "middle")),
    ("cart", "user_id"): dict(sa="bottom", oa=60, sb="right", ob="row:id", note=(1008, 540, "start")),
    ("user_sessions", "user_id"): dict(sa="right", oa="row:user_id", sb="left", ob="row:id",
                                       note=(425, 594, "middle")),
    ("user_credentials", "user_id"): dict(sa="left", oa="row:user_id", sb="right", ob=160,
                                          note=(875, 714, "middle")),
}


def read_schema(conn: sqlite3.Connection) -> dict[str, TableInfo]:
    """Читает структуру всех таблиц БД через ``PRAGMA``.

    Args:
        conn (sqlite3.Connection): открытое соединение с БД (``row_factory`` будет заменён
            на ``sqlite3.Row``).

    Returns:
        dict[str, TableInfo]: ``{имя таблицы: описание}``; описание содержит ключи ``cols``
        (``PRAGMA table_info``), ``fks`` (внешние ключи), ``uniques`` (столбцы UNIQUE),
        ``indexes`` (обычные индексы), ``checks`` (CHECK по столбцам), ``autoinc`` (признак
        AUTOINCREMENT) и ``rows`` (число строк).

    Raises:
        sqlite3.Error: если соединение закрыто или файл не является базой данных SQLite.
    """
    conn.row_factory = sqlite3.Row
    tables: dict[str, TableInfo] = {}
    for (name, sql) in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"):
        cols = [dict(r) for r in conn.execute(f"PRAGMA table_info('{name}')")]
        fks = [dict(r) for r in conn.execute(f"PRAGMA foreign_key_list('{name}')")]
        uniques = []
        for idx in conn.execute(f"PRAGMA index_list('{name}')"):
            if idx["unique"] and idx["origin"] == "u":
                uniques.append([r["name"] for r in conn.execute(f"PRAGMA index_info('{idx['name']}')")])
        indexes = [(r["name"], [c["name"] for c in conn.execute(f"PRAGMA index_info('{r['name']}')")])
                   for r in conn.execute(f"PRAGMA index_list('{name}')") if r["origin"] == "c"]
        checks = {}
        for raw in sql.splitlines():
            m = re.match(r"\s*(\w+)\s+\w+.*?(CHECK\s*\(.*\))", raw, re.I)
            if m:
                checks[m.group(1)] = m.group(2).rstrip(",")
        rows = conn.execute(f"SELECT COUNT(*) FROM '{name}'").fetchone()[0]
        tables[name] = dict(cols=cols, fks=fks, uniques=uniques, indexes=indexes, checks=checks,
                            autoinc="AUTOINCREMENT" in sql.upper(), rows=rows)
    return tables


def key_marks(t: TableInfo, col: dict[str, Any]) -> str:
    """Метки ключей столбца для колонки «Ключ».

    Args:
        t (TableInfo): описание таблицы (из ``read_schema``).
        col (dict[str, Any]): строка ``PRAGMA table_info`` — столбец этой таблицы.

    Returns:
        str: метки через запятую: ``PK`` (первичный ключ), ``FK`` (внешний), ``U`` (UNIQUE);
        пустая строка, если столбец не ключевой.

    Raises:
        KeyError: если в ``t`` или ``col`` нет ожидаемых ключей.
    """
    marks = []
    if col["pk"]:
        marks.append("PK")
    if any(fk["from"] == col["name"] for fk in t["fks"]):
        marks.append("FK")
    if any(col["name"] in u for u in t["uniques"]):
        marks.append("U")
    return ",".join(marks)


def table_box(name: str, t: TableInfo) -> Box:
    """Прямоугольник таблицы на диаграмме: положение из ``TABLE_POS``, высота — по числу столбцов.

    Args:
        name (str): имя таблицы.
        t (TableInfo): описание таблицы (нужен список ``cols``).

    Returns:
        Box: прямоугольник таблицы.

    Raises:
        KeyError: если таблицы нет в ``TABLE_POS`` (для неё не задано положение).
    """
    x, y = TABLE_POS[name]
    return Box(x, y, TABLE_W, HEAD_H + len(t["cols"]) * ROW_H + 6)


def resolve_offset(box: Box, t: TableInfo, off: Any) -> float:
    """Переводит смещение привязки из описания маршрута в число.

    Args:
        box (Box): прямоугольник таблицы (в текущей реализации не используется, оставлен для
            единообразия вызовов).
        t (TableInfo): описание таблицы (нужен список ``cols``).
        off (Any): число — возвращается как есть; строка ``"row:<столбец>"`` — смещение
            середины строки этого столбца от верха таблицы.

    Returns:
        float: смещение точки привязки.

    Raises:
        ValueError: если столбец из ``"row:<столбец>"`` отсутствует в таблице.
    """
    if isinstance(off, str) and off.startswith("row:"):
        idx = [c["name"] for c in t["cols"]].index(off[4:])
        return HEAD_H + idx * ROW_H + ROW_H / 2
    return float(off)


def draw_table(name: str, t: TableInfo) -> tuple[Box, list[str]]:
    """Рисует таблицу: рамка, шапка с именем и строки (ключ, имя, тип, NOT NULL / NULL).

    Args:
        name (str): имя таблицы.
        t (TableInfo): описание таблицы (из ``read_schema``).

    Returns:
        tuple[Box, list[str]]: прямоугольник таблицы (для привязки связей) и SVG-элементы.

    Raises:
        KeyError: если для таблицы не задано положение в ``TABLE_POS``.
    """
    box = table_box(name, t)
    x, y = box.x, box.y
    out = [f'<rect x="{x}" y="{y}" width="{box.w}" height="{box.h}" fill="#ffffff" stroke="{INK}" stroke-width="1.4"/>',
           f'<rect x="{x}" y="{y}" width="{box.w}" height="{HEAD_H}" fill="#e4e9ef" stroke="{INK}" stroke-width="1.4"/>',
           text(x + box.w / 2, y + 20, name, 14.5, "bold", "middle"),
           line(x + 52, y + HEAD_H, x + 52, y + box.h, GRID, 1)]
    for i, c in enumerate(t["cols"]):
        ry = y + HEAD_H + i * ROW_H
        if i:
            out.append(line(x, ry, x + box.w, ry, GRID, 1))
        base = ry + 15.5
        marks = key_marks(t, c)
        is_pk = bool(c["pk"])
        out.append(text(x + 26, base, marks, 10.5, "bold", "middle", "#9a3412" if is_pk else MUTED))
        out.append(text(x + 60, base, c["name"], 12.5, "bold" if is_pk else "normal"))
        out.append(text(x + 168, base, c["type"] or "", 12, color=MUTED))
        nn = c["notnull"] or c["pk"]
        out.append(text(x + box.w - 10, base, "NOT NULL" if nn else "NULL", 11,
                        anchor="end", color=INK if nn else "#9aa5b1"))
    return box, out


def draw_datalogical_legend(x: float, y: float) -> list[str]:
    """Рисует легенду даталогической диаграммы (ключи, сплошная и пунктирная половины, ``ON DELETE``).

    Args:
        x (float): абсцисса левого верхнего угла легенды.
        y (float): ордината левого верхнего угла легенды.

    Returns:
        list[str]: SVG-элементы легенды.
    """
    w, h = 320, 118
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#f8fafc" stroke="{GRID}"/>',
           text(x + 12, y + 22, "Условные обозначения", 13, "bold")]
    items = ["PK — первичный ключ;  FK — внешний ключ", "U — входит в ограничение UNIQUE",
             "сплошная половина — FK обязателен (NOT NULL)",
             "пунктир — у родителя может не быть строк", "подпись на линии — правило ON DELETE"]
    for i, s in enumerate(items):
        out.append(text(x + 12, y + 42 + i * 17, s, 12))
    return out


def build_datalogical(tables: dict[str, TableInfo]) -> str:
    """Собирает SVG даталогической модели по схеме БД и описанию маршрутов ``FK_ROUTES``.

    Args:
        tables (dict[str, TableInfo]): результат ``read_schema``.

    Returns:
        str: полный текст SVG-документа (1750 × 880).

    Raises:
        KeyError: если для таблицы нет положения в ``TABLE_POS``, для внешнего ключа нет
            маршрута в ``FK_ROUTES`` или ключ ссылается на неизвестную таблицу.
        ValueError: если маршрут привязан к строке несуществующего столбца.
        StopIteration: если внешний ключ ссылается на несуществующий столбец своей таблицы.
    """
    boxes: dict[str, Box] = {}
    body: list[str] = []
    for name in TABLE_POS:
        boxes[name], shapes = draw_table(name, tables[name])
        body += shapes
    rel_svg: list[str] = []
    for name, t in tables.items():
        for fk in t["fks"]:
            spec = FK_ROUTES[(name, fk["from"])]
            child, parent = boxes[name], boxes[fk["table"]]
            pk_cols = [c["name"] for c in t["cols"] if c["pk"]]
            one_to_one = pk_cols == [fk["from"]]
            col = next(c for c in t["cols"] if c["name"] == fk["from"])
            r = dict(sa=spec["sa"], oa=resolve_offset(child, t, spec["oa"]),
                     sb=spec["sb"], ob=resolve_offset(parent, tables[fk["table"]], spec["ob"]),
                     card_a="1" if one_to_one else "M", must_a=bool(col["notnull"] or col["pk"]),
                     card_b="1", must_b=False, note=spec["note"],
                     note_text=fk["on_delete"])
            rel_svg += relationship(child, parent, r)
    body = rel_svg + body
    body += draw_datalogical_legend(40, 722)
    return svg_doc(1750, 880, body)


# ───────────────────────── текстовое описание ─────────────────────────

COLUMN_INFO: dict[str, dict[str, str]] = {
    "users": {"id": "код пользователя", "first_name": "имя", "last_name": "фамилия",
              "middle_name": "отчество", "birthdate": "дата рождения (ISO-8601)", "phone": "телефон",
              "email": "электронная почта", "vip": "VIP-статус (0/1)", "photo": "путь к фото",
              "login": "устаревшее поле, дублирует user_credentials.login"},
    "user_credentials": {"user_id": "код пользователя", "login": "логин", "password_hash": "хеш пароля (bcrypt)",
                         "role": "роль: user / admin", "created_at": "дата создания",
                         "updated_at": "дата изменения"},
    "products": {"id": "код товара", "name": "название", "description": "описание", "price": "цена, руб.",
                 "stock": "остаток на складе, шт.", "weight": "вес, г", "image": "путь к изображению",
                 "category": "категория (ПРОТЕИНЫ, ГЕЙНЕРЫ, …)"},
    "addresses": {"id": "код адреса", "country": "страна", "city_type": "тип населённого пункта",
                  "city": "населённый пункт", "street_type": "тип улицы", "street": "улица",
                  "house_number": "номер дома", "apartment": "квартира"},
    "orders": {"id": "номер заказа", "product_id": "заказанный товар", "quantity": "количество",
               "order_date": "дата заказа (ISO-8601)", "total_price": "общая стоимость, руб.",
               "status": "pending / processing / shipped / delivered / cancelled"},
    "user_sessions": {"session_id": "идентификатор сессии", "user_id": "владелец сессии",
                      "created_at": "дата создания", "expires_at": "срок действия",
                      "last_activity": "последняя активность"},
    "user_order_address": {"id": "суррогатный ключ", "user_id": "пользователь", "order_id": "заказ",
                           "address_id": "адрес доставки", "created_at": "дата оформления"},
    "cart": {"id": "суррогатный ключ", "user_id": "владелец корзины", "product_id": "товар",
             "quantity": "количество", "created_at": "дата добавления"},
}

NOTATION = """\
Правила, по которым построена инфологическая диаграмма:

- **Сущность** — прямоугольник со скруглёнными углами, имя в единственном числе ПРОПИСНЫМИ буквами.
- **Атрибуты**: `#` — входит в уникальный идентификатор (UID), `*` — обязательный, `o` — необязательный.
- **Связь** — линия из двух половин, каждая половина описывает участие ближайшей сущности:
  сплошная — «должен», пунктирная — «может».
- **«Воронья лапка»** у сущности — «один или несколько»; простой конец — «один и только один».
- **Имя связи** пишется у той сущности, с которой начинается чтение:
  «Каждый A *должен/может* быть *<имя у A>* *один и только один / один или несколько* B».
- **Черта поперёк связи** у сущности — связь входит в её UID (сущность идентифицируется через родителя).
- **«Dead crows fly east»** («мёртвые вороны летят на восток»): лапки направлены только вверх или влево.
  Детальные сущности («многие», с большим числом экземпляров) стоят выше и левее,
  главные (справочные) — ниже и правее. На диаграмме все лапки висят на нижней или правой
  стороне детальной сущности.
- Связей «многие-ко-многим» нет: ПОЛЬЗОВАТЕЛЬ ⇄ ТОВАР разрешена сущностью-пересечением
  ПОЗИЦИЯ КОРЗИНЫ, тройная связь ПОЛЬЗОВАТЕЛЬ–ЗАКАЗ–АДРЕС — сущностью ОФОРМЛЕНИЕ ЗАКАЗА.
- Суррогатные ключи (`cart.id`, `user_order_address.id`) на инфологическом уровне не показаны:
  естественный UID этих сущностей — комбинация связей (ограничения UNIQUE в БД).
"""

REMARKS = """\
1. `users.login` — остаток миграции: логин перенесён в `user_credentials.login`, а старое поле
   осталось и дублирует его. В инфологической модели не показано; его стоит удалить.
2. `user_order_address` не запрещает несколько строк на один заказ (нет `UNIQUE(order_id)`),
   хотя по данным каждому заказу соответствует ровно одна строка. Если заказ всегда оформляет
   один пользователь на один адрес, проще хранить `user_id` и `address_id` прямо в `orders`.
3. Приложение не выполняет `PRAGMA foreign_keys = ON`, поэтому SQLite не проверяет внешние ключи
   и не выполняет `ON DELETE CASCADE / RESTRICT`. Сейчас «висячих» ссылок нет
   (`PRAGMA foreign_key_check` пуст), но целостность держится только на коде.
4. У `cart.user_id` и `cart.product_id` не задано `ON DELETE` (по умолчанию NO ACTION).
5. `users.vip`, `orders.status`, поля `created_at` имеют DEFAULT, но допускают NULL.
6. Категория товара — свободный текст; для контроля значений её можно вынести в справочник КАТЕГОРИЯ.
7. Даталогическая схема в отчёте (рис. А2) не совпадает с БД: в ней есть `orders.user_id`,
   `cart.updated_at`, `user_credentials.id`, которых в базе нет, а `orders.product_id` отсутствует.
"""


def md_table(header: list[str], rows: list[list[Any]]) -> str:
    """Формирует таблицу в формате Markdown.

    Args:
        header (list[str]): заголовки столбцов.
        rows (list[list[Any]]): строки таблицы; значения приводятся к ``str``.

    Returns:
        str: текст таблицы (заголовок, разделитель, строки), строки разделены ``\\n``.
    """
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def build_markdown(tables: dict[str, TableInfo]) -> str:
    """Собирает текстовое описание обеих моделей в формате Markdown (для вставки в отчёт).

    Включает нотацию Баркера, список сущностей и связей с «чтением» в обе стороны, структуру
    каждой таблицы (поле, тип, ключ, NOT NULL, значение по умолчанию, ограничения) и замечания по схеме.

    Args:
        tables (dict[str, TableInfo]): результат ``read_schema``.

    Returns:
        str: полный текст документа ``models.md``.

    Raises:
        KeyError: если для таблицы из ``TABLE_POS`` нет описания в ``tables`` или в описании
            сущностей / связей не хватает ключей.
    """
    titles = {k: e["title"] for k, e in ENTITIES.items()}
    md = [f"# Модели данных БД `{DB_PATH.name}`", "",
          "## 1. Нотация Баркера", "", NOTATION,
          "## 2. Инфологическая модель", "", "![Инфологическая модель](infological.png)", "",
          "### Сущности и атрибуты", ""]
    for e in ENTITIES.values():
        attrs = ", ".join(f"{m} {a}" for m, a in e["attrs"])
        md.append(f"- **{e['title']}** (`{e['table']}`): {attrs}. UID: {e['uid']}.")
    md += ["", "### Связи", ""]
    rows: list[list[Any]] = []
    for r in RELATIONSHIPS:
        kind = "1:1" if r["card_a"] == r["card_b"] == "1" else "1:M"
        rows.append([f"{titles[r['b']]} — {titles[r['a']]}", kind, r["read_b"], r["read_a"]])
    md += [md_table(["Связь", "Тип", "Чтение от главной сущности", "Чтение от подчинённой сущности"], rows), ""]

    md += ["## 3. Даталогическая модель", "",
           "Реляционная модель для СУБД SQLite 3 (типы — классы хранения SQLite: TEXT, INTEGER, REAL; "
           "даты хранятся как TEXT в формате ISO-8601).", "",
           "![Даталогическая модель](datalogical.png)", ""]
    for name in TABLE_POS:
        t = tables[name]
        md += [f"### Таблица `{name}` (строк: {t['rows']})", ""]
        rows = []
        for c in t["cols"]:
            default = c["dflt_value"] if c["dflt_value"] is not None else ""
            extra = t["checks"].get(c["name"], "")
            if c["pk"] and t["autoinc"] and (c["type"] or "").upper() == "INTEGER":
                extra = "AUTOINCREMENT"
            rows.append([f"`{c['name']}`", c["type"], key_marks(t, c) or "",
                         "да" if c["notnull"] or c["pk"] else "нет", default, extra,
                         COLUMN_INFO.get(name, {}).get(c["name"], "")])
        md.append(md_table(["Поле", "Тип", "Ключ", "NOT NULL", "По умолчанию", "Ограничения", "Описание"], rows))
        notes = []
        for u in t["uniques"]:
            notes.append(f"UNIQUE({', '.join(u)})")
        for fk in t["fks"]:
            notes.append(f"FK `{fk['from']}` → `{fk['table']}.{fk['to']}`, ON DELETE {fk['on_delete']}")
        for iname, icols in t["indexes"]:
            notes.append(f"индекс `{iname}` ({', '.join(icols)})")
        if notes:
            md += [""] + [f"- {n}" for n in notes]
        md.append("")
    md += ["## 4. Замечания по схеме", "", REMARKS]
    return "\n".join(md)


# ───────────────────────── запуск ─────────────────────────

def to_png(svg_path: Path) -> Optional[Path]:
    """Конвертирует SVG в PNG утилитой ``rsvg-convert`` (в двойном разрешении).

    Args:
        svg_path (Path): путь к исходному ``.svg``.

    Returns:
        Optional[Path]: путь к созданному ``.png`` рядом с исходным файлом или ``None``,
        если ``rsvg-convert`` не установлен.

    Raises:
        subprocess.CalledProcessError: если ``rsvg-convert`` завершился с ошибкой
            (например, SVG повреждён).
    """
    if shutil.which("rsvg-convert"):
        png = svg_path.with_suffix(".png")
        subprocess.run(["rsvg-convert", "-z", "2", "-o", str(png), str(svg_path)], check=True)
        return png
    return None


def main() -> None:
    """Точка входа: читает схему БД, пишет SVG, PNG и ``models.md`` в каталог ``out/``.

    Raises:
        sqlite3.Error: если базу данных ``DB_PATH`` не удалось открыть или прочитать.
        KeyError: если схема БД не совпадает с жёстко заданными в скрипте таблицами и связями.
        OSError: если нельзя создать каталог ``out/`` или записать файлы.
        subprocess.CalledProcessError: если ``rsvg-convert`` завершился с ошибкой.
    """
    OUT.mkdir(exist_ok=True)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    tables = read_schema(conn)
    conn.close()

    files = {"infological.svg": build_infological(), "datalogical.svg": build_datalogical(tables)}
    for fname, svg in files.items():
        path = OUT / fname
        path.write_text(svg, encoding="utf-8")
        print("✓", path, "|", to_png(path) or "PNG пропущен: нет rsvg-convert")
    (OUT / "models.md").write_text(build_markdown(tables), encoding="utf-8")
    print("✓", OUT / "models.md")


if __name__ == "__main__":
    main()

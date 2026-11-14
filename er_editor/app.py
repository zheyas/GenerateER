"""Главное окно редактора: события мыши и клавиатуры, панель свойств, undo/redo, файлы, экспорт.

Устройство окна::

    ┌──────────────────────── панель инструментов (draw_toolbar) ────────────────────────┐
    │ холст с диаграммой (draw_canvas)                    │ панель свойств (draw_panel)  │
    ├──────────────────────── строка состояния (draw_status) ───────────────────────────┤

Интерфейс «режима немедленной отрисовки»: каждый кадр :meth:`App.draw` заново рисует всё и
заполняет список кликабельных областей ``App.regions``; ``on_mouse_down`` ищет в нём
сработавшую кнопку. Холст обрабатывается отдельно (:meth:`App.canvas_down`).

Любое изменение модели вызывает :meth:`App.snapshot` — снимок проекта для undo/redo.
"""
from __future__ import annotations

import copy
import os
import platform
import sqlite3
import sys
import time
from dataclasses import dataclass
from functools import partial
from math import hypot
from pathlib import Path
from typing import Any, Callable, Optional

import pygame

from . import __version__, dbgen, layout, normalform, schema
from .geom import FRect, nearest_anchor
from .markdown import MarkdownView
from .model import (KIND_DATA, KIND_INFO, KIND_TITLES, LEGEND_KINDS, PALETTE, STYLE, Diagram, LegendItem, Link,
                    Node, Note, Project, Row, default_legend, empty_project, node_height)
from .render import (ACCENT, FAINT, FLAG_ERROR, FLAG_WARN, INK, MUTED, Color, Flag, Fonts, Gfx, Scene, Sel, draw_arrow,
                     draw_diagram, draw_grid, export_png, hexrgb)
from .ui import AsyncJob, DialogResult, TextEdit, choose_file, choose_save, letter

Callback = Callable[[], None]
CommitFn = Callable[[str], None]
InlineRect = Callable[[Scene], Optional[FRect]]
Region = tuple[pygame.Rect, Callback, str]
Pos = tuple[int, int]

TOOLBAR_H, STATUS_H, PANEL_W = 46, 28, 340
BG: Color = (226, 230, 236)
PANEL_BG: Color = (248, 249, 251)
BAR_BG: Color = (243, 245, 248)
BORDER: Color = (200, 206, 214)
BTN: Color = (255, 255, 255)
BTN_HOT: Color = (232, 239, 252)
BTN_ON: Color = (214, 228, 255)
DANGER: Color = (192, 57, 43)
ORANGE: Color = (230, 126, 34)
GOOD: Color = (39, 144, 84)
#: Оттенки кнопки-вердикта: (заливка, рамка).
TONES: dict[str, tuple[Color, Color]] = {"ok": ((214, 240, 224), GOOD), "warn": ((253, 239, 214), FLAG_WARN),
                                         "bad": ((253, 224, 224), FLAG_ERROR)}
NF_PANEL_LIMIT = 40                            # сколько замечаний показывать в панели
MIN_ZOOM, MAX_ZOOM = 0.15, 3.0
GRID_STEP = 10
TOOLBAR_PX = 12

README_PATH = Path(__file__).resolve().parent.parent / "README.md"

ABOUT_FALLBACK = """# ER Editor

Редактор инфологических (нотация Баркера) и даталогических моделей баз данных SQLite.

Файл README.md не найден рядом с программой. Краткая справка по клавишам — кнопка **?** (F1).

- Откройте базу данных кнопкой «Открыть БД» или перетащите файл `.db` в окно.
- Двойной клик по тексту — правка; кнопка «Связь» — новая связь; «Экспорт PNG» — картинка.
"""

HELP_LINES: list[tuple[str, str]] = [
    ("Мышь", ""),
    ("Левая кнопка", "выделить, перетащить сущность / надпись / легенду / подпись связи"),
    ("Двойной клик", "править текст: название, поле, тип, подпись связи, пункт легенды"),
    ("Тянуть за правый край", "изменить ширину сущности или легенды"),
    ("Выделенная связь", "кружки — переставить конец по сторонам узла, квадрат — сдвинуть среднее звено"),
    ("Тянуть по пустому месту / ПКМ", "двигать холст"),
    ("Колесо / жест трекпада", "прокрутка;  Cmd/Ctrl + колесо — масштаб"),
    ("Клавиши", ""),
    ("Cmd/Ctrl + Z, Shift+Z (или Y)", "отменить / вернуть"),
    ("Cmd/Ctrl + S", "сохранить проект (.erd.json);  Cmd+O — открыть БД;  Cmd+E — экспорт PNG;  Cmd+Shift+E — обе диаграммы"),
    ("Delete", "удалить выделенное (выделено поле — удалится поле)"),
    ("F2 / Enter", "править выделенное;   Tab — переключить диаграмму"),
    ("Стрелки", "сдвиг сущности (Shift — на 1);  Alt+↑/↓ — переставить поле"),
    ("Cmd/Ctrl + D", "дублировать сущность;   Cmd+0 — 100 %;   Cmd+1 — вписать;   + / − — масштаб"),
    ("Cmd/Ctrl + B", "скачать базу данных SQLite по даталогической диаграмме (чекбокс — по 5 случайных записей в таблицу)"),
    ("Cmd/Ctrl + Shift + B", "скачать комплект: база + инфологическая и даталогическая модели (PNG) + отчёт о нормальной форме"),
    ("Кнопки 1НФ … НФБК", "выбрать нормальную форму: программа проверяет нарисованные таблицы, нарушения подсвечены на схеме и перечислены справа"),
    ("Кнопка «Связь»", "нажать на дочернюю таблицу (где внешний ключ) и протянуть стрелку к главной; можно и двумя кликами"),
    ("Esc", "отмена текущего действия / снять выделение"),
]


@dataclass
class Cam:
    """Камера холста.

    Attributes:
        ox (float): экранное смещение начала мировых координат по горизонтали.
        oy (float): то же по вертикали.
        z (float): масштаб (1.0 = 100 %).
    """

    ox: float = 80.0
    oy: float = 90.0
    z: float = 1.0


@dataclass
class Modal:
    """Модальное окно с вопросом. Первая кнопка срабатывает по Enter, последняя — по Esc.

    Attributes:
        title (str): заголовок.
        text (str): поясняющий текст (до трёх строк).
        buttons (list[tuple[str, Optional[Callback]]]): кнопки ``(подпись, действие | None)``;
            ``None`` — просто закрыть окно.
    """

    title: str
    text: str
    buttons: list[tuple[str, Optional[Callback]]]


@dataclass
class AboutWindow:
    """Состояние окна «О программе».

    Attributes:
        view (MarkdownView): отображаемый документ (README).
        scroll (int): прокрутка в пикселях от верха документа.
    """

    view: MarkdownView
    scroll: int = 0


@dataclass
class ToolItem:
    """Элемент панели инструментов: кнопка или чекбокс.

    Attributes:
        label (str): подпись.
        cb (Callback): действие при клике.
        active (bool): кнопка «нажата» / чекбокс отмечен.
        enabled (bool): ``False`` — элемент серый и не реагирует на клики.
        tip (str): подсказка в строке состояния при наведении.
        check (bool): ``True`` — рисовать чекбокс, иначе кнопку.
        tone (str): оттенок кнопки: ``"ok"`` (зелёный), ``"warn"`` (оранжевый), ``"bad"`` (красный)
            или пустая строка.
        static (bool): ``True`` — просто надпись без рамки и кликов.
    """

    label: str
    cb: Callback
    active: bool = False
    enabled: bool = True
    tip: str = ""
    check: bool = False
    tone: str = ""
    static: bool = False


class Edit:
    """Активное редактирование текста: в панели свойств (``inline is None``) или прямо на холсте.

    Attributes:
        fid (str): идентификатор поля (поле панели узнаёт по нему, что редактируется).
        ted (TextEdit): сам редактор текста.
        commit (CommitFn): функция, которой передаётся итоговый текст при завершении.
        inline (Optional[InlineRect]): для правки на холсте — функция, возвращающая мировой
            прямоугольник редактируемого текста.
        rect (Optional[pygame.Rect]): экранный прямоугольник поля (заполняется при рисовании;
            нужен для установки каретки кликом).
        scroll (int): горизонтальная прокрутка длинного текста в пикселях.
    """

    def __init__(self, fid: str, text: str, commit: CommitFn, inline: Optional[InlineRect] = None) -> None:
        """Начинает редактирование.

        Args:
            fid (str): идентификатор поля.
            text (str): исходный текст (выделяется целиком).
            commit (CommitFn): получает итоговый текст при завершении правки.
            inline (Optional[InlineRect]): для правки на холсте — функция расположения поля.
        """
        self.fid, self.ted, self.commit, self.inline = fid, TextEdit(text), commit, inline
        self.rect: Optional[pygame.Rect] = None
        self.scroll = 0


def snap(v: float, on: bool = True) -> float:
    """Привязывает значение к сетке.

    Args:
        v (float): исходное значение.
        on (bool): ``False`` — вернуть значение без изменений.

    Returns:
        float: значение, округлённое до ближайшего кратного :data:`GRID_STEP` (если ``on``).
    """
    return round(v / GRID_STEP) * GRID_STEP if on else v


class App:
    """Приложение: владеет окном pygame, проектом, выделением, историей изменений и главным циклом.

    Attributes:
        project (Project): открытый проект.
        kind (str): текущая диаграмма (``KIND_INFO`` / ``KIND_DATA``).
        sel (Sel): выделение.
        tool (str): ``"select"`` или ``"link"`` (создание связи).
        edit (Optional[Edit]): активное редактирование текста.
        modal (Optional[Modal]): открытое модальное окно.
        about (Optional[AboutWindow]): открытое окно «О программе».
        undo_stack (list[str]): снимки проекта (JSON) для отмены.
        redo_stack (list[str]): снимки проекта для повтора.
        modified (bool): есть несохранённые изменения.
        regions (list[Region]): кликабельные области текущего кадра.
        scene (Scene): геометрия текущей диаграммы.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        """Создаёт окно и (по желанию) открывает файл.

        Args:
            path (Optional[str]): путь к базе данных SQLite или к файлу проекта ``.erd.json``,
                который нужно открыть при старте; ``None`` — пустое окно.

        Raises:
            pygame.error: если не удалось создать окно (нет графической подсистемы).
        """
        pygame.init()
        pygame.display.set_caption("ER Editor — диаграммы Баркера и даталогические модели")
        self.screen: pygame.Surface = pygame.display.set_mode((1500, 920), pygame.RESIZABLE)
        pygame.key.set_repeat(380, 32)
        try:
            pygame.key.start_text_input()
        except Exception:
            pass
        self.fonts = Fonts()
        self.clock = pygame.time.Clock()
        self.project = empty_project()
        self.kind = KIND_INFO
        self.cams: dict[str, Cam] = {KIND_INFO: Cam(), KIND_DATA: Cam()}
        self.sel = Sel()
        self.tool: str = "select"
        self.link_src: Optional[str] = None
        self.edit: Optional[Edit] = None
        self.drag: Optional[dict[str, Any]] = None
        self.modal: Optional[Modal] = None
        self.about: Optional[AboutWindow] = None
        self.help = False
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []
        self.modified = False
        self.project_path: Optional[str] = None
        self.regions: list[Region] = []
        self.modal_regions: list[pygame.Rect] = []
        self._clip: Optional[pygame.Rect] = None
        self.panel_scroll, self._py, self._panel_h = 0, 0, 0
        self.toast_msg, self.toast_until = "", 0.0
        self.jobs: list[AsyncJob] = []
        self.last_click: tuple[float, Pos] = (0.0, (0, 0))
        self.hover_tip = ""
        self._caption = ""
        self._sel_key: Optional[tuple[Any, ...]] = None
        self._tb_key: Optional[tuple[int, str]] = None
        self._tb_rows = 1
        self._nf_cache: Optional[tuple[tuple[object, ...], normalform.Report]] = None
        self.running, self.dirty = True, True
        self.scene = Scene(self.d, self.fonts)
        if path:
            self.open_path(path)

    # ───────────────────────── состояние ─────────────────────────

    @property
    def d(self) -> Diagram:
        """Текущая (отображаемая) диаграмма.

        Returns:
            Diagram: диаграмма вида ``self.kind``.
        """
        return self.project.diagrams[self.kind]

    @property
    def cam(self) -> Cam:
        """Камера текущей диаграммы (у каждой диаграммы своя).

        Returns:
            Cam: камера вида ``self.kind``.
        """
        return self.cams[self.kind]

    @property
    def size(self) -> tuple[int, int]:
        """Размер окна.

        Returns:
            tuple[int, int]: ``(ширина, высота)`` в пикселях.
        """
        return self.screen.get_size()

    @property
    def toolbar_h(self) -> int:
        """Высота панели инструментов (кнопки переносятся на вторую строку, если не помещаются).

        Returns:
            int: ``TOOLBAR_H`` × число строк панели.
        """
        key = (self.size[0], self.kind)
        if self._tb_key != key:
            self._tb_rows = self.toolbar_layout()[0]
            self._tb_key = key
        return TOOLBAR_H * self._tb_rows

    @property
    def canvas_rect(self) -> pygame.Rect:
        """Экранная область холста (между панелью инструментов, строкой состояния и панелью свойств).

        Returns:
            pygame.Rect: прямоугольник холста.
        """
        w, h = self.size
        th = self.toolbar_h
        return pygame.Rect(0, th, max(100, w - PANEL_W), max(100, h - th - STATUS_H))

    @property
    def panel_rect(self) -> pygame.Rect:
        """Экранная область панели свойств справа.

        Returns:
            pygame.Rect: прямоугольник панели.
        """
        w, h = self.size
        th = self.toolbar_h
        return pygame.Rect(w - PANEL_W, th, PANEL_W, h - th - STATUS_H)

    def w2s(self, wx: float, wy: float) -> tuple[float, float]:
        """Переводит мировые координаты в экранные (с учётом камеры).

        Args:
            wx (float): мировая абсцисса.
            wy (float): мировая ордината.

        Returns:
            tuple[float, float]: экранные ``(x, y)``.
        """
        c = self.cam
        return wx * c.z + c.ox, wy * c.z + c.oy

    def s2w(self, pos: tuple[float, float]) -> tuple[float, float]:
        """Переводит экранные координаты в мировые.

        Args:
            pos (tuple[float, float]): экранная точка ``(x, y)``.

        Returns:
            tuple[float, float]: мировые ``(x, y)``.
        """
        c = self.cam
        return (pos[0] - c.ox) / c.z, (pos[1] - c.oy) / c.z

    def say(self, msg: str, secs: float = 3.0) -> None:
        """Показывает всплывающее сообщение внизу холста.

        Args:
            msg (str): текст сообщения.
            secs (float): сколько секунд показывать.
        """
        self.toast_msg, self.toast_until = msg, time.time() + secs
        self.dirty = True

    def rescene(self) -> None:
        """Пересоздаёт геометрию (:class:`Scene`) текущей диаграммы после изменений."""
        self.scene = Scene(self.d, self.fonts)

    # ───────────────────────── undo / redo ─────────────────────────

    def snapshot(self) -> None:
        """Запоминает состояние проекта для отмены (вызывается **до** каждого изменения модели).

        Сбрасывает стек повтора, помечает проект изменённым; хранится не более 150 снимков.
        """
        self.undo_stack.append(self.project.to_json())
        if len(self.undo_stack) > 150:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self.modified = True

    def _restore(self, snap: str) -> None:
        """Заменяет проект снимком.

        Настройки вида (сетка, масштаб экспорта) сохраняются; выделение сбрасывается, если
        выделенный объект исчез.

        Args:
            snap (str): JSON-снимок проекта (см. :meth:`snapshot`).

        Raises:
            json.JSONDecodeError: если снимок повреждён (в нормальной работе не бывает).
        """
        proj = Project.from_json(snap)
        proj.grid, proj.export_scale = self.project.grid, self.project.export_scale
        proj.fill_sample = self.project.fill_sample
        proj.normal_form = self.project.normal_form
        self.project = proj
        s = self.sel
        ok = (s.kind is None or s.kind == "legend"
              or (s.kind == "node" and self.d.node(s.id or "")) or (s.kind == "link" and self.d.link(s.id or ""))
              or (s.kind == "note" and self.d.note(s.id or "")))
        if not ok:
            self.sel = Sel()
        self.modified = True
        self.rescene()

    def undo(self) -> None:
        """Отменяет последнее изменение (Cmd+Z); без истории ничего не делает."""
        if self.undo_stack:
            self.end_edit(False)
            self.redo_stack.append(self.project.to_json())
            self._restore(self.undo_stack.pop())

    def redo(self) -> None:
        """Возвращает отменённое изменение (Cmd+Shift+Z); без отменённых действий ничего не делает."""
        if self.redo_stack:
            self.end_edit(False)
            self.undo_stack.append(self.project.to_json())
            self._restore(self.redo_stack.pop())

    # ───────────────────────── файлы ─────────────────────────

    def base_name(self) -> str:
        """Определяет имя проекта без расширения (для заголовка окна и имён экспортируемых файлов).

        Returns:
            str: имя файла проекта или базы данных без суффикса; ``"diagram"``, если путей нет.
        """
        src = self.project_path or self.project.db_path
        name = os.path.basename(src) if src else "diagram"
        for suf in (".erd.json", ".json", ".db", ".sqlite", ".sqlite3"):
            if name.lower().endswith(suf):
                return name[:-len(suf)]
        return name

    def default_project_path(self, db_path: str) -> str:
        """Вычисляет путь файла проекта по умолчанию.

        Args:
            db_path (str): путь к базе данных.

        Returns:
            str: путь рядом с базой, ``<имя>.erd.json``.
        """
        return os.path.splitext(db_path)[0] + ".erd.json"

    def open_path(self, path: str) -> None:
        """Открывает файл: ``.json`` — проект; иначе база данных.

        Если рядом с базой уже есть сохранённый проект, предлагает его открыть или
        импортировать схему заново. Ошибки показываются сообщением, исключения не
        пробрасываются.

        Args:
            path (str): путь к файлу (``~`` раскрывается, путь делается абсолютным).
        """
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(path):
            self.say(f"Файл не найден: {path}")
            return
        if path.lower().endswith(".json"):
            self.load_project(path)
            return
        saved = self.default_project_path(path)
        if os.path.exists(saved):
            self.modal = Modal(
                title="Найден сохранённый проект",
                text=f"Рядом с базой есть {os.path.basename(saved)}. Открыть его (с вашими правками) или импортировать схему заново?",
                buttons=[("Открыть проект", lambda: self.load_project(saved)),
                         ("Импортировать заново", lambda: self.import_db(path)),
                         ("Отмена", None)])
        else:
            self.import_db(path)

    def import_db(self, path: str) -> None:
        """Импортирует схему БД, строит обе диаграммы и сбрасывает историю изменений.

        При ошибке чтения базы проект не меняется, пользователю показывается сообщение.

        Args:
            path (str): путь к базе данных SQLite.
        """
        try:
            proj = schema.build_project(path, self.fonts.measure)
        except Exception as exc:                      # noqa: BLE001
            self.say(f"Не удалось прочитать БД: {exc}", 6)
            return
        proj.grid, proj.export_scale = self.project.grid, self.project.export_scale
        proj.fill_sample = self.project.fill_sample
        proj.normal_form = self.project.normal_form
        self.project = proj
        self.project_path = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.sel, self.tool, self.link_src = Sel(), "select", None
        self.modified = False
        self.rescene()
        for k in (KIND_INFO, KIND_DATA):
            self.fit_view(k)
        self.say(f"Импортировано таблиц: {len(proj.diagrams[KIND_INFO].nodes)}, связей: {len(proj.diagrams[KIND_INFO].links)}")

    def load_project(self, path: str) -> None:
        """Загружает проект из файла ``.erd.json`` и сбрасывает историю изменений.

        При ошибке чтения текущий проект не меняется, пользователю показывается сообщение.

        Args:
            path (str): путь к файлу проекта.
        """
        try:
            proj = Project.load(path)
        except Exception as exc:                      # noqa: BLE001
            self.say(f"Не удалось открыть проект: {exc}", 6)
            return
        self.install_project(proj, path)
        self.say(f"Открыт проект {os.path.basename(path)}")

    def install_project(self, proj: Project, path: Optional[str]) -> None:
        """Делает проект текущим: сбрасывает историю, выделение и режимы, вписывает обе диаграммы в окно.

        Args:
            proj (Project): новый проект (недостающие диаграммы создаются пустыми).
            path (Optional[str]): путь файла проекта ``.erd.json`` (``None`` — проект ещё не сохранён).
        """
        for k in (KIND_INFO, KIND_DATA):
            proj.diagrams.setdefault(k, Diagram(k, legend=default_legend(k)))
        self.project, self.project_path = proj, path
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.sel, self.tool, self.link_src = Sel(), "select", None
        self.modified = False
        self.rescene()
        for k in (KIND_INFO, KIND_DATA):
            self.fit_view(k)

    def save_project(self, then: Optional[Callback] = None) -> None:
        """Сохраняет проект: в известный путь, рядом с БД, а если путей нет — спрашивает диалогом.

        Args:
            then (Optional[Callback]): вызывается после успешного сохранения
                (например, «сохранить и выйти»).
        """
        path = self.project_path or (self.default_project_path(self.project.db_path) if self.project.db_path else None)
        if path is None:
            def done(res: DialogResult) -> None:
                """Записывает проект по пути, выбранному в диалоге.

                Args:
                    res (DialogResult): ``(путь | None, диалог_недоступен)``.
                """
                p, unavailable = res
                if unavailable:
                    p = os.path.join(os.getcwd(), "diagram.erd.json")
                if p:
                    self._write_project(p if p.endswith(".json") else p + ".erd.json", then)
            self.jobs.append(AsyncJob(lambda: choose_save("Сохранить проект", "diagram.erd.json"), done))
            return
        self._write_project(path, then)

    def _write_project(self, path: str, then: Optional[Callback] = None) -> None:
        """Записывает проект в файл и обновляет состояние «сохранено».

        При ошибке записи показывает сообщение и ничего не меняет.

        Args:
            path (str): путь к файлу ``.erd.json``.
            then (Optional[Callback]): вызывается после успешной записи.
        """
        try:
            self.project.save(path)
        except OSError as exc:
            self.say(f"Не удалось сохранить: {exc}", 6)
            return
        self.project_path, self.modified = path, False
        self.say(f"Сохранено: {path}")
        if then:
            then()

    def open_db_dialog(self) -> None:
        """Показывает системный диалог выбора базы данных или проекта (выполняется в потоке)."""
        def done(res: DialogResult) -> None:
            """Открывает выбранный файл или сообщает, что диалог недоступен.

            Args:
                res (DialogResult): ``(путь | None, диалог_недоступен)``.
            """
            p, unavailable = res
            if unavailable:
                self.say("Диалог недоступен: перетащите файл .db в окно или запустите с путём в аргументе", 6)
            elif p:
                self.open_path(p)
        self.jobs.append(AsyncJob(lambda: choose_file("Выберите базу данных SQLite или проект", ["db", "sqlite", "sqlite3", "json"]), done))

    def export_dialog(self, both: bool = False) -> None:
        """Показывает диалог «Сохранить как» и экспортирует PNG.

        Args:
            both (bool): ``True`` — экспортировать обе диаграммы
                (``…_infological.png`` и ``…_datalogical.png``).
        """
        kind_name = "infological" if self.kind == KIND_INFO else "datalogical"
        default = f"{self.base_name()}_{kind_name}.png"
        src = self.project_path or self.project.db_path
        folder = os.path.dirname(src) if src else os.getcwd()

        def done(res: DialogResult) -> None:
            """Запускает экспорт по пути из диалога (или в папку проекта, если диалога нет).

            Args:
                res (DialogResult): ``(путь | None, диалог_недоступен)``.
            """
            p, unavailable = res
            if unavailable:
                p = os.path.join(folder, default)
            if p:
                self.do_export(p, both)
        self.jobs.append(AsyncJob(lambda: choose_save("Экспорт PNG", default, folder), done))

    def do_export(self, path: str, both: bool) -> None:
        """Экспортирует диаграмму (или обе) в PNG с масштабом проекта и показывает итог.

        Ошибки записи показываются сообщением, исключения не пробрасываются.

        Args:
            path (str): путь к ``.png`` (расширение добавляется при необходимости).
            both (bool): ``True`` — обе диаграммы; имена строятся из ``path`` с суффиксами
                ``_infological`` и ``_datalogical``.
        """
        root = path[:-4] if path.lower().endswith(".png") else path
        for suf in ("_infological", "_datalogical"):
            if root.endswith(suf):
                root = root[:-len(suf)]
        targets = ([(KIND_INFO, f"{root}_infological.png"), (KIND_DATA, f"{root}_datalogical.png")] if both
                   else [(self.kind, path if path.lower().endswith(".png") else path + ".png")])
        done: list[str] = []
        try:
            for kind, out in targets:
                w, h = export_png(self.project.diagrams[kind], self.fonts, out, self.project.export_scale)
                done.append(f"{os.path.basename(out)} ({w}×{h})")
        except Exception as exc:                      # noqa: BLE001
            self.say(f"Ошибка экспорта: {exc}", 6)
            return
        self.say("PNG сохранён: " + ", ".join(done), 5)

    def request_quit(self) -> None:
        """Выход из программы: сразу, если нет изменений, иначе — вопрос о сохранении."""
        if not self.modified:
            self.running = False
            return
        self.modal = Modal(
            title="Есть несохранённые изменения",
            text="Сохранить проект перед выходом?",
            buttons=[("Сохранить и выйти", lambda: self.save_project(then=lambda: setattr(self, "running", False))),
                     ("Выйти без сохранения", lambda: setattr(self, "running", False)),
                     ("Отмена", None)])

    # ───────────────────────── вид ─────────────────────────

    def fit_view(self, kind: Optional[str] = None) -> None:
        """Подбирает камеру так, чтобы вся диаграмма поместилась в холст (масштаб не больше 100 %).

        Args:
            kind (Optional[str]): вид диаграммы; ``None`` — текущая.

        Raises:
            KeyError: если ``kind`` — неизвестный вид диаграммы.
        """
        kind = kind or self.kind
        d = self.project.diagrams[kind]
        b = Scene(d, self.fonts).bounds()
        cr = self.canvas_rect
        z = max(MIN_ZOOM, min((cr.w - 80) / b.w, (cr.h - 80) / b.h, 1.0))
        cam = self.cams[kind]
        cam.z = z
        cam.ox = cr.x + (cr.w - b.w * z) / 2 - b.x * z
        cam.oy = cr.y + (cr.h - b.h * z) / 2 - b.y * z

    def zoom_at(self, factor: float, sx: float, sy: float) -> None:
        """Масштабирует вид так, чтобы точка экрана осталась на месте (масштаб ограничен 15–300 %).

        Args:
            factor (float): во сколько раз изменить масштаб (>1 — приблизить).
            sx (float): экранная абсцисса неподвижной точки.
            sy (float): экранная ордината неподвижной точки.
        """
        cam = self.cam
        wx, wy = self.s2w((sx, sy))
        cam.z = max(MIN_ZOOM, min(MAX_ZOOM, cam.z * factor))
        cam.ox, cam.oy = sx - wx * cam.z, sy - wy * cam.z

    def zoom_center(self, factor: float) -> None:
        """Масштабирует вид относительно центра холста.

        Args:
            factor (float): во сколько раз изменить масштаб.
        """
        cr = self.canvas_rect
        self.zoom_at(factor, cr.centerx, cr.centery)

    def set_zoom_100(self) -> None:
        """Устанавливает масштаб 100 % относительно центра холста."""
        cr = self.canvas_rect
        self.zoom_at(1.0 / self.cam.z, cr.centerx, cr.centery)

    def switch_kind(self, kind: str) -> None:
        """Переключает отображаемую диаграмму (инфологическая / даталогическая).

        Args:
            kind (str): ``KIND_INFO`` или ``KIND_DATA``; текущий вид игнорируется.
        """
        if kind != self.kind:
            self.end_edit(True)
            self.kind = kind
            self.sel, self.tool, self.link_src = Sel(), "select", None
            self.panel_scroll = 0
            self.rescene()

    # ───────────────────────── изменение модели ─────────────────────────

    def grow(self) -> None:
        """Расширяет узлы, если после правки текст перестал помещаться (ширину не уменьшает)."""
        layout.fit_widths(self.d, self.fonts.measure, grow_only=True)

    def n_set(self, nid: str, **kw: Any) -> None:
        """Задаёт поля узла (``title``, ``color``…) с записью в историю.

        Args:
            nid (str): идентификатор узла; если узла нет — ничего не делает.
            **kw (Any): имена полей и новые значения.
        """
        n = self.d.node(nid)
        if n:
            self.snapshot()
            for k, v in kw.items():
                setattr(n, k, v)
            self.grow()

    def r_set(self, nid: str, i: int, **kw: Any) -> None:
        """Задаёт поля строки узла (``name``, ``mark``, ``type``, ``key``, ``notnull``, ``default``) с записью в историю.

        Args:
            nid (str): идентификатор узла.
            i (int): индекс строки; при выходе за границы ничего не делает.
            **kw (Any): имена полей и новые значения.
        """
        n = self.d.node(nid)
        if n and 0 <= i < len(n.rows):
            self.snapshot()
            old = n.rows[i].name
            for k, v in kw.items():
                setattr(n.rows[i], k, v)
            if "name" in kw and kw["name"] != old:
                self._rename_column(nid, old, kw["name"])
            self.grow()

    def l_set(self, lid: str, **kw: Any) -> None:
        """Задаёт поля связи (кратность, участие, подписи…) с записью в историю.

        Args:
            lid (str): идентификатор связи; если связи нет — ничего не делает.
            **kw (Any): имена полей и новые значения.
        """
        l = self.d.link(lid)
        if l:
            self.snapshot()
            for k, v in kw.items():
                setattr(l, k, v)

    def note_set(self, nid: str, **kw: Any) -> None:
        """Задаёт поля надписи (``text``, ``size``) с записью в историю.

        Args:
            nid (str): идентификатор надписи; если надписи нет — ничего не делает.
            **kw (Any): имена полей и новые значения.
        """
        n = self.d.note(nid)
        if n:
            self.snapshot()
            for k, v in kw.items():
                setattr(n, k, v)

    def legend_set(self, **kw: Any) -> None:
        """Задаёт поля легенды (``title``, ``visible``) с записью в историю.

        Args:
            **kw (Any): имена полей и новые значения.
        """
        self.snapshot()
        for k, v in kw.items():
            setattr(self.d.legend, k, v)

    def item_set(self, i: int, **kw: Any) -> None:
        """Задаёт поля пункта легенды (``text``, ``kind``) с записью в историю.

        Args:
            i (int): индекс пункта; при выходе за границы ничего не делает.
            **kw (Any): имена полей и новые значения.
        """
        items = self.d.legend.items
        if 0 <= i < len(items):
            self.snapshot()
            for k, v in kw.items():
                setattr(items[i], k, v)

    def view_center_world(self) -> tuple[float, float]:
        """Вычисляет центр видимой части холста (сюда ставятся новые объекты).

        Returns:
            tuple[float, float]: мировые координаты центра.
        """
        cr = self.canvas_rect
        return self.s2w((cr.centerx, cr.centery))

    def add_node(self) -> None:
        """Добавляет сущность / таблицу в центр вида и сразу открывает правку заголовка."""
        d = self.d
        self.snapshot()
        if d.kind == KIND_INFO:
            n = Node(id=d.new_id("n"), title="СУЩНОСТЬ", rows=[Row("код", "#"), Row("атрибут", "*")])
        else:
            used = {x.title.strip().lower() for x in d.nodes}
            title = next(t for t in ("table", *(f"table_{k}" for k in range(2, 10_000))) if t not in used)
            n = Node(id=d.new_id("n"), title=title, rows=[Row("id", "", "INTEGER", "PK", True), Row("name", "", "TEXT", "", False)])
        layout.fit_widths(Diagram(d.kind, nodes=[n]), self.fonts.measure)
        cx, cy = self.view_center_world()
        n.x, n.y = self.free_position(n.w, node_height(d.kind, len(n.rows)), snap(cx - n.w / 2), snap(cy - 40))
        d.nodes.append(n)
        self.sel = Sel("node", n.id)
        self.rescene()
        self.edit_node_title(n.id)

    def free_position(self, w: float, h: float, x: float, y: float) -> tuple[float, float]:
        """Подбирает свободное место для нового узла, чтобы он не лёг поверх существующих.

        Перебирает сдвиги от желаемой точки вправо и вниз (с шагом 80 и 60) и берёт первую
        позицию, где прямоугольник узла (с зазором 20) не пересекает другие узлы и легенду.

        Args:
            w (float): ширина нового узла.
            h (float): высота нового узла.
            x (float): желаемая абсцисса левого верхнего угла.
            y (float): желаемая ордината левого верхнего угла.

        Returns:
            tuple[float, float]: найденная позиция; если свободного места нет — исходная ``(x, y)``.
        """
        rects = [layout.node_rect(self.d, n) for n in self.d.nodes]
        if self.scene.legend:
            rects.append(self.scene.legend.rect)
        for dy in range(0, 900, 60):
            for dx in range(0, 1200, 80):
                cand = FRect(x + dx, y + dy, w, h).inflated(20)
                if not any(
                    cand.x < r.right and r.x < cand.right and cand.y < r.bottom and r.y < cand.bottom for r in rects
                ):
                    return x + dx, y + dy
        return x, y

    def add_note(self) -> None:
        """Добавляет свободную надпись в центр вида и открывает её правку."""
        d = self.d
        self.snapshot()
        cx, cy = self.view_center_world()
        note = Note(id=d.new_id("t"), x=snap(cx - 40), y=snap(cy), text="Надпись")
        d.notes.append(note)
        self.sel = Sel("note", note.id)
        self.rescene()
        self.edit_note(note.id)

    def duplicate_node(self) -> None:
        """Дублирует выделенный узел (без связей) со сдвигом (Cmd+D); если узел не выделен — ничего не делает."""
        s = self.sel
        n = self.d.node(s.id or "") if s.kind == "node" else None
        if not n:
            return
        self.snapshot()
        c = copy.deepcopy(n)
        c.id, c.title, c.x, c.y = self.d.new_id("n"), n.title + " (копия)", n.x + 40, n.y + 40
        self.d.nodes.append(c)
        self.sel = Sel("node", c.id)

    def create_link(self, a: str, b: str) -> None:
        """Создаёт связь от подчинённого узла к главному и прокладывает её маршрут.

        Args:
            a (str): идентификатор подчинённого узла («много», конец A).
            b (str): идентификатор главного узла (конец B).
        """
        d = self.d
        self.snapshot()
        l = Link(id=d.new_id("l"), a=a, b=b)
        if d.kind == KIND_INFO:
            l.name_a, l.name_b = "относится к", "включает"
        else:
            l.label = "NO ACTION"
        d.links.append(l)
        if d.kind == KIND_DATA:
            self.bind_foreign_key(l)
        layout.auto_route(d, [l])
        self.sel = Sel("link", l.id)
        self.say("Связь создана: подпишите концы двойным кликом или в панели справа")

    def swap_link(self, lid: str) -> None:
        """Меняет концы связи A и B местами вместе со всеми их свойствами.

        Args:
            lid (str): идентификатор связи; если связи нет — ничего не делает.
        """
        l = self.d.link(lid)
        if not l:
            return
        self.snapshot()
        l.a, l.b, l.sa, l.sb, l.ta, l.tb = l.b, l.a, l.sb, l.sa, l.tb, l.ta
        l.card_a, l.card_b, l.must_a, l.must_b = l.card_b, l.card_a, l.must_b, l.must_a
        l.bar_a, l.bar_b, l.name_a, l.name_b = l.bar_b, l.bar_a, l.name_b, l.name_a
        l.off_a, l.off_b, l.row_a, l.row_b = l.off_b, l.off_a, l.row_b, l.row_a
        l.fk_cols, l.ref_cols = l.ref_cols, l.fk_cols

    def reroute_link(self, lid: str) -> None:
        """Сбрасывает ручные сдвиги подписей и пересчитывает маршрут одной связи.

        Args:
            lid (str): идентификатор связи; если связи нет — ничего не делает.
        """
        l = self.d.link(lid)
        if l:
            self.snapshot()
            l.off_a, l.off_b, l.off_c = [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]
            layout.auto_route(self.d, [l])

    def add_row(self) -> None:
        """Добавляет строку в выделенный узел (после выделенной) и открывает правку её имени."""
        s = self.sel
        n = self.d.node(s.id or "") if s.kind == "node" else None
        if not n:
            return
        self.snapshot()
        row = Row("атрибут", "o") if self.d.kind == KIND_INFO else Row("поле", "", "TEXT", "", False)
        at = s.row + 1 if s.row is not None else len(n.rows)
        n.rows.insert(at, row)
        self.sel = Sel("node", n.id, at)
        self.rescene()
        nid = n.id
        self.begin_edit(f"row.name:{nid}", row.name, lambda v: self.r_set(nid, at, name=v))

    def move_row(self, delta: int) -> None:
        """Сдвигает выделенную строку узла.

        Args:
            delta (int): на сколько позиций сдвинуть (−1 вверх, +1 вниз); если выходит за
                границы или строка не выделена — ничего не делает.
        """
        s = self.sel
        n = self.d.node(s.id or "") if s.kind == "node" else None
        if not n or s.row is None or not 0 <= s.row + delta < len(n.rows):
            return
        self.snapshot()
        i = s.row
        n.rows[i], n.rows[i + delta] = n.rows[i + delta], n.rows[i]
        self.sel = Sel("node", n.id, i + delta)

    def delete_selection(self) -> None:
        """Удаляет выделенное: строку (если выделена), иначе узел со связями, связь, надпись или пункт легенды."""
        s, d = self.sel, self.d
        if s.kind == "node":
            n = d.node(s.id or "")
            if not n:
                return
            self.snapshot()
            if s.row is not None and 0 <= s.row < len(n.rows):
                del n.rows[s.row]
                self.sel = Sel("node", n.id, None)
            else:
                d.remove_node(n.id)
                self.sel = Sel()
        elif s.kind == "link" and d.link(s.id or ""):
            self.snapshot()
            d.links = [l for l in d.links if l.id != s.id]
            self.sel = Sel()
        elif s.kind == "note" and d.note(s.id or ""):
            self.snapshot()
            d.notes = [n for n in d.notes if n.id != s.id]
            self.sel = Sel()
        elif s.kind == "legend":
            self.snapshot()
            if s.row is not None and 0 <= s.row < len(d.legend.items):
                del d.legend.items[s.row]
                self.sel = Sel("legend")
            else:
                d.legend.visible = False
                self.sel = Sel()
        self.rescene()

    def delete_node_whole(self) -> None:
        """Удаляет выделенный узел целиком (даже если выделена его строка)."""
        s = self.sel
        if s.kind == "node":
            self.sel = Sel("node", s.id, None)
            self.delete_selection()

    def auto_layout(self) -> None:
        """Выполняет автораскладку текущей диаграммы (узлы, легенда, связи) и «вписывает» вид."""
        self.snapshot()
        layout.fit_widths(self.d, self.fonts.measure, grow_only=True)
        layout.auto_layout(self.d)
        self.rescene()
        self.fit_view()
        self.say("Автораскладка выполнена (Cmd+Z — отменить)")

    def reimport(self) -> None:
        """Предлагает заново импортировать схему из БД, отбросив все правки (с подтверждением)."""
        db = self.project.db_path
        if not db or not os.path.exists(db):
            self.say("Путь к БД неизвестен или файл не найден")
            return
        self.modal = Modal(title="Импортировать заново?",
                           text="Все правки обеих диаграмм будут заменены схемой из базы данных.",
                           buttons=[("Импортировать", lambda: self.import_db(db)), ("Отмена", None)])

    def toggle_legend(self) -> None:
        """Показывает или скрывает легенду (показанная легенда выделяется)."""
        self.snapshot()
        lg = self.d.legend
        lg.visible = not lg.visible
        if lg.visible:
            self.sel = Sel("legend")

    def reset_legend(self) -> None:
        """Возвращает легенде стандартное содержимое, сохраняя её положение."""
        self.snapshot()
        old = self.d.legend
        new = default_legend(self.kind, self.d.arrows)
        new.x, new.y, new.visible = old.x, old.y, True
        self.d.legend = new
        self.sel = Sel("legend")

    def add_legend_item(self) -> None:
        """Добавляет в легенду текстовый пункт и открывает правку его текста."""
        self.snapshot()
        items = self.d.legend.items
        items.append(LegendItem("text", "новый пункт"))
        self.sel = Sel("legend", None, len(items) - 1)
        self.rescene()
        last = len(items) - 1
        self.begin_edit("item.text", "новый пункт", lambda v: self.item_set(last, text=v))

    def move_item(self, delta: int) -> None:
        """Сдвигает выделенный пункт легенды.

        Args:
            delta (int): на сколько позиций сдвинуть (−1 вверх, +1 вниз); если выходит за
                границы или пункт не выделен — ничего не делает.
        """
        s, items = self.sel, self.d.legend.items
        if s.kind == "legend" and s.row is not None and 0 <= s.row + delta < len(items):
            self.snapshot()
            i = s.row
            items[i], items[i + delta] = items[i + delta], items[i]
            self.sel = Sel("legend", None, i + delta)

    def toggle_grid(self) -> None:
        """Включает / выключает сетку и привязку к ней."""
        self.project.grid = not self.project.grid

    def cycle_export_scale(self) -> None:
        """Переключает масштаб экспорта PNG по кругу: ×1 → ×2 → ×3 → ×1."""
        self.project.export_scale = self.project.export_scale % 3 + 1

    # ───────────────────────── база данных из диаграммы ─────────────────────────

    def toggle_fill_sample(self) -> None:
        """Включает / выключает заполнение таблиц случайными записями при создании базы данных."""
        self.project.fill_sample = not self.project.fill_sample

    def show_problems(self, problems: list[str], title: str = "Схему нельзя превратить в базу данных") -> None:
        """Показывает окно со списком проблем схемы или предупреждений.

        Args:
            problems (list[str]): тексты проблем (показываются первые 8).
            title (str): заголовок окна.
        """
        text = "  ".join(f"• {p}" if p.endswith(".") else f"• {p}." for p in problems[:8])
        if len(problems) > 8:
            text += f"  … и ещё {len(problems) - 8}"
        self.modal = Modal(title=title, text=text, buttons=[("Понятно", None)])

    def gate_export(self, go: Callback) -> None:
        """Проверяет схему перед созданием базы данных и вызывает ``go``, если можно продолжать.

        Порядок: таблицы нарисованы → схема превращается в базу данных (иначе список проблем) →
        соблюдена выбранная нормальная форма (иначе вопрос «скачать всё равно?»).

        Args:
            go (Callback): действие, которое нужно выполнить после успешной проверки.
        """
        data = self.project.diagrams[KIND_DATA]
        if not data.nodes:
            self.switch_kind(KIND_DATA)
            self.say("Сначала нарисуйте таблицы на вкладке «Даталогическая» (кнопка «+ Таблица»)", 6)
            return
        try:
            dbgen.diagram_to_tables(data)
        except dbgen.SchemaError as exc:
            self.switch_kind(KIND_DATA)
            self.show_problems(exc.problems)
            return
        if self.nf_report().ok:
            go()
        else:
            self.confirm_nf(go)

    def confirm_nf(self, go: Callback) -> None:
        """Спрашивает, продолжать ли, если схема не соответствует выбранной нормальной форме.

        Args:
            go (Callback): действие, которое выполняется при ответе «Скачать всё равно».
        """
        rep = self.nf_report()
        first = "; ".join(f"{i.table}: {i.rule}" for i in rep.violations[:3])
        more = " …" if len(rep.violations) > 3 else ""
        self.modal = Modal(
            title=f"Схема не соответствует {normalform.LEVEL_NAMES[rep.target]}",
            text=f"Нарушений: {len(rep.violations)} ({first}{more}). Показать их на схеме или всё равно скачать?",
            buttons=[("Показать нарушения", self.show_nf_report), ("Скачать всё равно", go), ("Отмена", None)])

    def download_db_dialog(self) -> None:
        """Создаёт файл базы данных SQLite по даталогической диаграмме (кнопка «Скачать БД», Cmd+B).

        Сначала схема проверяется (:meth:`gate_export`), затем открывается диалог «Сохранить как».
        Если отмечен чекбокс, в каждую таблицу добавляется по несколько случайных записей
        (:data:`er_editor.dbgen.SAMPLE_ROWS`).
        """
        self.gate_export(self.pick_db_path)

    def download_bundle_dialog(self) -> None:
        """Скачивает комплект: база данных + инфологическая и даталогическая модели (PNG) + отчёт о форме.

        Кнопка «БД и модели», Cmd+Shift+B. Схема проверяется так же, как в :meth:`download_db_dialog`.
        """
        self.gate_export(partial(self.pick_db_path, True))

    def pick_db_path(self, bundle: bool = False) -> None:
        """Показывает диалог «Сохранить как» и создаёт базу данных (или весь комплект).

        Args:
            bundle (bool): ``True`` — создать комплект (:meth:`create_bundle`), иначе только базу.
        """
        default = f"{self.base_name()}_new.db"
        src = self.project_path or self.project.db_path
        folder = os.path.dirname(src) if src else os.getcwd()

        def done(res: DialogResult) -> None:
            """Создаёт базу по пути, выбранному в диалоге (или в папке проекта, если диалога нет).

            Args:
                res (DialogResult): ``(путь | None, диалог_недоступен)``.
            """
            p, unavailable = res
            if unavailable:
                p = os.path.join(folder, default)
            if p:
                self.create_bundle(p) if bundle else self.create_database_at(p)
        title = "Скачать базу данных и модели" if bundle else "Создать базу данных SQLite"
        self.jobs.append(AsyncJob(lambda: choose_save(title, default, folder), done))

    def build_info_from_schema(self) -> bool:
        """Строит инфологическую диаграмму (Баркер) по нарисованным таблицам и ставит её на вкладку.

        Прежняя инфологическая диаграмма заменяется (действие можно отменить).

        Returns:
            bool: ``True``, если диаграмма построена; ``False`` — схему нельзя прочитать (показан список проблем).
        """
        try:
            tables = dbgen.diagram_to_tables(self.project.diagrams[KIND_DATA])
        except dbgen.SchemaError as exc:
            self.show_problems(exc.problems)
            return False
        info = schema.build_info(tables)
        layout.fit_widths(info, self.fonts.measure)
        layout.auto_layout(info)
        self.snapshot()
        self.project.diagrams[KIND_INFO] = info
        if self.kind == KIND_INFO:
            self.sel = Sel()
            self.rescene()
            self.fit_view(KIND_INFO)
        return True

    def rebuild_info(self) -> None:
        """Строит инфологическую диаграмму по таблицам (обёртка для кнопки окна подтверждения)."""
        self.build_info_from_schema()

    def rebuild_info_dialog(self) -> None:
        """Предлагает построить инфологическую диаграмму по таблицам (спрашивает, если на ней уже есть сущности)."""
        if not self.project.diagrams[KIND_DATA].nodes:
            self.say("Сначала нарисуйте таблицы на вкладке «Даталогическая»", 5)
            return
        if not self.project.diagrams[KIND_INFO].nodes:
            self.build_info_from_schema()
            return
        self.modal = Modal(
            title="Заменить инфологическую диаграмму?",
            text="Сущности и связи этой вкладки будут построены заново по таблицам. Изменение можно отменить (Cmd+Z).",
            buttons=[("Заменить", self.rebuild_info), ("Отмена", None)])

    def create_bundle(self, path: str) -> bool:
        """Создаёт комплект файлов рядом с выбранным путём.

        В комплект входят: база данных ``<имя>.db``, ``<имя>_infological.png``, ``<имя>_datalogical.png``
        и ``<имя>_normal_form.md``. Если инфологическая вкладка пуста, диаграмма строится по таблицам;
        нарисованная вручную используется как есть. Ошибки показываются сообщением.

        Args:
            path (str): путь к файлу базы данных (при отсутствии расширения добавляется ``.db``).

        Returns:
            bool: ``True``, если все файлы созданы.
        """
        report = self.create_database_at(path)
        if report is None:
            return False
        root = os.path.splitext(report.path)[0]
        generated = not self.project.diagrams[KIND_INFO].nodes
        if generated and not self.build_info_from_schema():
            return False
        names = [os.path.basename(report.path)]
        try:
            for kind, suffix in ((KIND_INFO, "_infological"), (KIND_DATA, "_datalogical")):
                out = f"{root}{suffix}.png"
                export_png(self.project.diagrams[kind], self.fonts, out, self.project.export_scale)
                names.append(os.path.basename(out))
        except Exception as exc:                      # noqa: BLE001
            self.say(f"База создана, но PNG не сохранены: {exc}", 7)
            return False
        if not self.write_nf_report(f"{root}_normal_form.md"):
            return False
        names.append(os.path.basename(f"{root}_normal_form.md"))
        note = " (инфологическая построена по таблицам)" if generated else ""
        self.say("Сохранено: " + ", ".join(names) + note, 9)
        return True

    def create_database_at(self, path: str) -> Optional[dbgen.DbReport]:
        """Создаёт файл базы данных по даталогической диаграмме и показывает итог.

        Исходную базу, из которой импортирован проект, перезаписать нельзя. Ошибки показываются
        сообщением, исключения не пробрасываются.

        Args:
            path (str): путь к создаваемому файлу (при отсутствии расширения добавляется ``.db``).

        Returns:
            Optional[dbgen.DbReport]: отчёт о создании или ``None``, если создать не удалось.
        """
        if not path.lower().endswith((".db", ".sqlite", ".sqlite3")):
            path += ".db"
        src = self.project.db_path
        if src and os.path.exists(path) and os.path.exists(src) and os.path.samefile(path, src):
            self.say("Нельзя перезаписать исходную базу данных — выберите другое имя файла", 6)
            return None
        sample = dbgen.SAMPLE_ROWS if self.project.fill_sample else 0
        try:
            tables = dbgen.diagram_to_tables(self.project.diagrams[KIND_DATA])
            report = dbgen.create_database(path, tables, sample)
        except dbgen.SchemaError as exc:
            self.show_problems(exc.problems)
            return None
        except (sqlite3.Error, OSError) as exc:
            self.say(f"Не удалось создать базу данных: {exc}", 7)
            return None
        msg = f"База данных создана: {os.path.basename(path)} — таблиц: {report.tables}"
        if sample:
            msg += f", записей: {report.total_rows}"
        self.say(msg, 6)
        if report.warnings:
            self.show_problems(report.warnings, "База создана, но есть предупреждения")
        return report

    def bind_foreign_key(self, l: Link) -> None:
        """Добавляет в дочернюю таблицу столбцы внешнего ключа для новой связи (даталогическая).

        Для каждого столбца первичного ключа родителя создаётся столбец ``<родитель>_<столбец>`` того
        же типа с меткой ``FK`` (если такого столбца ещё нет); связь запоминает, какие столбцы
        образуют ключ. Без первичного ключа у родителя столбцы не добавляются, показывается подсказка.

        Args:
            l (Link): только что созданная связь (A — дочерняя таблица, B — родительская).
        """
        d = self.d
        child, parent = d.node(l.a), d.node(l.b)
        if child is None or parent is None:
            return
        pk_rows = [r for r in parent.rows if "PK" in dbgen.key_tokens(r.key) and r.name.strip()]
        if not pk_rows:
            self.say(f"У таблицы «{parent.title}» нет первичного ключа: отметьте поле меткой PK и создайте связь заново", 7)
            return
        cols: list[str] = []
        for pr in pk_rows:
            base = f"{parent.title.strip()}_{pr.name.strip()}"
            existing = next((r for r in child.rows if r.name == base), None)
            name = base
            if existing is not None and "FK" in dbgen.key_tokens(existing.key):
                cols.append(name)
                continue
            k = 2
            while any(r.name == name for r in child.rows):
                name = f"{base}_{k}"
                k += 1
            child.rows.append(Row(name, "", pr.type or "INTEGER", "FK", l.must_a))
            cols.append(name)
        l.fk_cols, l.ref_cols = cols, [pr.name for pr in pk_rows]
        self.grow()

    def _rename_column(self, nid: str, old: str, new: str) -> None:
        """Переименовывает столбец в привязках связей и в составных ограничениях UNIQUE узла.

        Args:
            nid (str): идентификатор узла, в котором переименован столбец.
            old (str): прежнее имя столбца.
            new (str): новое имя.
        """
        d = self.d
        for l in d.links:
            if l.a == nid:
                l.fk_cols = [new if c == old else c for c in l.fk_cols]
            if l.b == nid:
                l.ref_cols = [new if c == old else c for c in l.ref_cols]
        n = d.node(nid)
        if n is not None:
            n.uniques = [[new if c == old else c for c in g] for g in n.uniques]
            n.fds = normalform.rename_in_fds(n.fds, old, new)

    def toggle_key(self, nid: str, i: int, token: str) -> None:
        """Включает или снимает метку ключа (``PK``, ``FK``, ``U``) у строки таблицы.

        Args:
            nid (str): идентификатор узла.
            i (int): индекс строки.
            token (str): метка ключа.
        """
        n = self.d.node(nid)
        if not n or not 0 <= i < len(n.rows):
            return
        tokens = dbgen.key_tokens(n.rows[i].key)
        tokens = [t for t in tokens if t != token] if token in tokens else tokens + [token]
        order = {"PK": 0, "FK": 1, "U": 2}
        tokens.sort(key=lambda t: order.get(t, 9))
        self.r_set(nid, i, key=",".join(tokens))

    def set_uniques(self, nid: str, text: str) -> None:
        """Задаёт составные ограничения UNIQUE таблицы по тексту из поля ввода.

        Группы из одного столбца превращаются в метку ``U`` у этого столбца.

        Args:
            nid (str): идентификатор узла.
            text (str): группы столбцов через «;», столбцы внутри группы через запятую.
        """
        n = self.d.node(nid)
        if not n:
            return
        self.snapshot()
        groups = dbgen.parse_uniques(text)
        n.uniques = [g for g in groups if len(g) > 1]
        for g in groups:
            if len(g) == 1:
                for r in n.rows:
                    if r.name.strip().lower() == g[0].lower() and "U" not in dbgen.key_tokens(r.key):
                        r.key = ",".join(dbgen.key_tokens(r.key) + ["U"])

    # ───────────────────────── редактирование текста ─────────────────────────

    def begin_edit(self, fid: str, text: str, commit: CommitFn, inline: Optional[InlineRect] = None) -> None:
        """Начинает правку текста (предыдущая правка завершается с сохранением).

        Args:
            fid (str): идентификатор поля.
            text (str): исходный текст (выделяется целиком).
            commit (CommitFn): получает итоговый текст при Enter или клике в сторону.
            inline (Optional[InlineRect]): для правки на холсте — функция, возвращающая
                мировой прямоугольник текста.
        """
        self.end_edit(True)
        self.edit = Edit(fid, text, commit, inline)
        try:
            pygame.key.start_text_input()
        except Exception:
            pass

    def end_edit(self, ok: bool) -> None:
        """Завершает правку текста.

        Args:
            ok (bool): ``True`` — применить текст (вызвать ``commit``); ``False`` — отменить (Esc).
        """
        e, self.edit = self.edit, None
        if e and ok:
            e.commit(e.ted.text)
            self.rescene()

    def edit_node_title(self, nid: str) -> None:
        """Начинает правку заголовка узла прямо на холсте.

        Args:
            nid (str): идентификатор узла; если узла нет — ничего не делает.
        """
        n = self.d.node(nid)
        if not n:
            return
        head = STYLE[self.d.kind]["head"]

        def inline(sc: Scene) -> Optional[FRect]:
            """Определяет прямоугольник шапки узла, поверх которого рисуется поле правки.

            Args:
                sc (Scene): текущая геометрия диаграммы.

            Returns:
                Optional[FRect]: шапка узла или ``None``, если узел исчез.
            """
            geo = sc.nodes.get(nid)
            return FRect(geo.rect.x, geo.rect.y, geo.rect.w, head) if geo else None
        self.begin_edit(f"title:{nid}", n.title, lambda v: self.n_set(nid, title=v), inline)

    def edit_row(self, nid: str, i: int, field: str = "name") -> None:
        """Начинает правку поля строки узла на холсте.

        Args:
            nid (str): идентификатор узла.
            i (int): индекс строки; при выходе за границы ничего не делает.
            field (str): ``"name"``, ``"type"`` или ``"key"`` (последние два — в даталогической).
        """
        n = self.d.node(nid)
        if not n or not 0 <= i < len(n.rows):
            return
        row = n.rows[i]

        def inline(sc: Scene) -> Optional[FRect]:
            """Определяет прямоугольник редактируемой ячейки строки.

            Args:
                sc (Scene): текущая геометрия диаграммы.

            Returns:
                Optional[FRect]: ячейка (имя, тип или ключ) или ``None``, если строка исчезла.
            """
            geo = sc.nodes.get(nid)
            if not geo or i >= len(geo.rows):
                return None
            rr = geo.rows[i]
            if field == "type":
                return FRect(geo.type_x, rr.y, 120, rr.h)
            if field == "key":
                return FRect(rr.x, rr.y, 60, rr.h)
            return FRect(rr.x + 28, rr.y, rr.w - 40, rr.h)
        self.begin_edit(f"row.{field}:{nid}:{i}", getattr(row, field), lambda v: self.r_set(nid, i, **{field: v}), inline)

    def edit_note(self, nid: str) -> None:
        """Начинает правку текста надписи на холсте.

        Args:
            nid (str): идентификатор надписи; если надписи нет — ничего не делает.
        """
        n = self.d.note(nid)
        if n:
            self.begin_edit(f"note:{nid}", n.text, lambda v: self.note_set(nid, text=v), lambda sc: sc.notes.get(nid))

    def edit_legend_title(self) -> None:
        """Начинает правку заголовка легенды на холсте."""
        self.begin_edit("legend.title", self.d.legend.title, lambda v: self.legend_set(title=v),
                        lambda sc: sc.legend.title if sc.legend else None)

    def edit_legend_item(self, i: int) -> None:
        """Начинает правку текста пункта легенды на холсте.

        Args:
            i (int): индекс пункта; при выходе за границы ничего не делает.
        """
        items = self.d.legend.items
        if 0 <= i < len(items):
            self.begin_edit(f"item.text:{i}", items[i].text, lambda v: self.item_set(i, text=v),
                            lambda sc: (sc.legend.items[i] if sc.legend and i < len(sc.legend.items) else None))

    def edit_link_label(self, lid: str, which: str) -> None:
        """Начинает правку подписи связи на холсте.

        Args:
            lid (str): идентификатор связи; если связи нет — ничего не делает.
            which (str): ``"a"`` или ``"b"`` — имя связи у конца, ``"c"`` — центральная подпись.

        Raises:
            KeyError: если ``which`` не ``"a"``, ``"b"`` или ``"c"``.
        """
        l = self.d.link(lid)
        if not l:
            return
        field = {"a": "name_a", "b": "name_b", "c": "label"}[which]

        def inline(sc: Scene) -> Optional[FRect]:
            """Определяет прямоугольник редактируемой подписи связи.

            Args:
                sc (Scene): текущая геометрия диаграммы.

            Returns:
                Optional[FRect]: прямоугольник подписи или ``None``, если её нет (текст пуст).
            """
            lg = sc.links.get(lid)
            if not lg:
                return None
            return next((r for w, _, r in lg.labels if w == which), None)
        self.begin_edit(f"link.{field}:{lid}", getattr(l, field), lambda v: self.l_set(lid, **{field: v}), inline)

    def edit_selected(self) -> None:
        """Начинает правку выделенного объекта (F2 / Enter): строки, заголовка, надписи, пункта легенды или подписи связи."""
        s, d = self.sel, self.d
        if s.kind == "node" and s.id:
            if s.row is not None:
                self.edit_row(s.id, s.row)
            else:
                self.edit_node_title(s.id)
        elif s.kind == "note" and s.id:
            self.edit_note(s.id)
        elif s.kind == "legend":
            if s.row is not None:
                self.edit_legend_item(s.row)
            else:
                self.edit_legend_title()
        elif s.kind == "link" and s.id:
            self.edit_link_label(s.id, "c" if d.kind == KIND_DATA else "a")

    # ───────────────────────── нормальные формы ─────────────────────────

    def nf_report(self) -> normalform.Report:
        """Возвращает результат проверки нарисованной схемы на выбранную нормальную форму.

        Результат запоминается и пересчитывается только после изменения таблиц, столбцов, ключей,
        зависимостей, связей или выбранной формы (по отпечатку :func:`er_editor.normalform.fingerprint`).

        Returns:
            normalform.Report: отчёт проверки даталогической диаграммы.
        """
        data = self.project.diagrams[KIND_DATA]
        level = self.project.normal_form if self.project.normal_form in normalform.LEVELS else 3
        fp = normalform.fingerprint(data, level)
        if self._nf_cache is None or self._nf_cache[0] != fp:
            self._nf_cache = (fp, normalform.check(data, level))
        return self._nf_cache[1]

    def set_normal_form(self, level: int) -> None:
        """Выбирает нормальную форму, на соответствие которой проверяется схема.

        Args:
            level (int): 1 — 1НФ, 2 — 2НФ, 3 — 3НФ, 4 — НФБК; другие значения игнорируются.
        """
        if level in normalform.LEVELS and level != self.project.normal_form:
            self.snapshot()
            self.project.normal_form = level
            rep = self.nf_report()
            self.say(rep.verdict(), 5)

    def nf_flags(self) -> dict[str, Flag]:
        """Составляет подсветку проблемных таблиц и строк для холста даталогической диаграммы.

        Returns:
            dict[str, Flag]: подсветка по идентификатору узла; пустой словарь на инфологической диаграмме.
        """
        if self.kind != KIND_DATA:
            return {}
        rep, data = self.nf_report(), self.project.diagrams[KIND_DATA]
        out: dict[str, Flag] = {}
        for iss in rep.issues:
            n = data.node(iss.node_id) if iss.node_id else None
            if n is None or iss.level > rep.target:
                continue
            flag = out.setdefault(iss.node_id, Flag(iss.severity))
            if iss.severity == normalform.ERROR:
                flag.level = normalform.ERROR
            rows = {r.name.strip().lower(): i for i, r in enumerate(n.rows)}
            for c in iss.columns:
                i = rows.get(c.strip().lower())
                if i is not None and flag.rows.get(i) != normalform.ERROR:
                    flag.rows[i] = iss.severity
        return out

    def show_nf_report(self) -> None:
        """Открывает даталогическую диаграмму и снимает выделение, чтобы справа был полный отчёт о нормальной форме."""
        self.switch_kind(KIND_DATA)
        self.sel, self.tool, self.link_src, self.panel_scroll = Sel(), "select", None, 0

    def set_fds(self, nid: str, text: str) -> None:
        """Задаёт функциональные зависимости таблицы по тексту из поля ввода.

        Args:
            nid (str): идентификатор узла.
            text (str): записи вида ``a, b -> c`` через «;»; нераспознанные записи отбрасываются
                с сообщением.
        """
        n = self.d.node(nid)
        if not n:
            return
        good, bad = normalform.normalize_fds(text)
        self.snapshot()
        n.fds = good
        if bad:
            self.say("Не распознано: " + "; ".join(bad) + ". Формат записи: город -> страна", 7)

    def reveal_node(self, nid: str) -> None:
        """Сдвигает холст так, чтобы таблица оказалась в видимой части (масштаб не меняется).

        Args:
            nid (str): идентификатор узла текущей диаграммы.
        """
        geo = self.scene.nodes.get(nid)
        if geo is None:
            return
        cr, cam = self.canvas_rect, self.cam
        sx, sy = self.w2s(geo.rect.cx, geo.rect.cy)
        if not cr.inflate(-120, -120).collidepoint(sx, sy):
            cam.ox += cr.centerx - sx
            cam.oy += cr.centery - sy

    def focus_issue(self, nid: str, columns: list[str]) -> None:
        """Выделяет на схеме таблицу и первый затронутый столбец замечания.

        Args:
            nid (str): идентификатор таблицы.
            columns (list[str]): затронутые столбцы (имена).
        """
        self.switch_kind(KIND_DATA)
        n = self.d.node(nid)
        if n is None:
            return
        names = {r.name.strip().lower(): i for i, r in enumerate(n.rows)}
        row = next((names[c.strip().lower()] for c in columns if c.strip().lower() in names), None)
        self.sel = Sel("node", nid, row)
        self.reveal_node(nid)

    def nf_report_dialog(self) -> None:
        """Сохраняет отчёт о проверке нормальной формы в файл Markdown (диалог «Сохранить как»)."""
        default = f"{self.base_name()}_normal_form.md"
        src = self.project_path or self.project.db_path
        folder = os.path.dirname(src) if src else os.getcwd()

        def done(res: DialogResult) -> None:
            """Записывает отчёт по пути из диалога (или в папку проекта, если диалога нет).

            Args:
                res (DialogResult): ``(путь | None, диалог_недоступен)``.
            """
            p, unavailable = res
            if unavailable:
                p = os.path.join(folder, default)
            if p:
                self.write_nf_report(p)
        self.jobs.append(AsyncJob(lambda: choose_save("Отчёт о нормальной форме", default, folder), done))

    def write_nf_report(self, path: str) -> bool:
        """Записывает отчёт о проверке нормальной формы в файл Markdown.

        Ошибки записи показываются сообщением, исключения не пробрасываются.

        Args:
            path (str): путь к файлу (при отсутствии расширения добавляется ``.md``).

        Returns:
            bool: ``True``, если файл записан.
        """
        if not path.lower().endswith(".md"):
            path += ".md"
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(normalform.format_markdown(self.nf_report(), self.base_name()))
        except OSError as exc:
            self.say(f"Не удалось записать отчёт: {exc}", 6)
            return False
        self.say("Отчёт сохранён: " + os.path.basename(path), 5)
        return True

    def set_arrows(self, arrows: bool) -> None:
        """Переключает вид связей даталогической диаграммы: стрелки FK → PK или «воронья лапка».

        Если легенда осталась стандартной, она заменяется стандартной легендой нового вида.

        Args:
            arrows (bool): ``True`` — стрелки, ``False`` — «воронья лапка».
        """
        d = self.project.diagrams[KIND_DATA]
        if d.arrows == arrows:
            return
        self.snapshot()
        old = [(i.kind, i.text) for i in default_legend(KIND_DATA, d.arrows).items]
        d.arrows = arrows
        if [(i.kind, i.text) for i in d.legend.items] == old:
            d.legend.items = default_legend(KIND_DATA, arrows).items

    # ───────────────────────── окно «О программе» ─────────────────────────

    def open_about(self) -> None:
        """Открывает окно «О программе»: показывает README.md (или краткий текст, если файла нет)."""
        try:
            text = README_PATH.read_text(encoding="utf-8")
        except OSError:
            text = ABOUT_FALLBACK
        self.end_edit(True)
        self.about = AboutWindow(MarkdownView(text, self.fonts))

    def close_about(self) -> None:
        """Закрывает окно «О программе»."""
        self.about = None
        self.drag = None

    def about_geometry(self) -> dict[str, pygame.Rect]:
        """Вычисляет экранные прямоугольники окна «О программе».

        Returns:
            dict[str, pygame.Rect]: ``box`` (всё окно), ``head`` (шапка), ``body`` (область
            текста), ``close`` (кнопка «Закрыть»), ``track`` (полоса прокрутки).
        """
        w, h = self.size
        box = pygame.Rect(0, 0, min(1060, w - 60), h - 60)
        box.center = (w // 2, h // 2)
        head = pygame.Rect(box.x, box.y, box.w, 64)
        body = pygame.Rect(box.x, head.bottom, box.w, box.h - head.h)
        return dict(box=box, head=head, body=body,
                    close=pygame.Rect(box.right - 132, box.y + 16, 112, 32),
                    track=pygame.Rect(body.right - 18, body.y + 10, 8, body.h - 20))

    def _about_surface(self) -> pygame.Surface:
        """Отрисованный README для текущей ширины окна.

        Returns:
            pygame.Surface: длинная поверхность с документом.

        Raises:
            AssertionError: если окно «О программе» не открыто.
        """
        assert self.about is not None
        body = self.about_geometry()["body"]
        return self.about.view.render(body.w - 2 * 32 - 14)

    def _about_max_scroll(self) -> int:
        """Вычисляет максимальную прокрутку README.

        Returns:
            int: наибольшее допустимое смещение в пикселях (0, если документ помещается целиком).
        """
        body = self.about_geometry()["body"]
        return max(0, self._about_surface().get_height() - body.h + 24)

    def about_scroll_to(self, value: float) -> None:
        """Устанавливает прокрутку README с ограничением по краям документа.

        Args:
            value (float): желаемое смещение в пикселях от верха документа.
        """
        if self.about:
            self.about.scroll = int(max(0, min(self._about_max_scroll(), value)))

    def about_event(self, ev: pygame.event.Event) -> None:
        """Обрабатывает событие, пока открыто окно «О программе» (оно перехватывает весь ввод).

        Закрытие: Esc, F1, кнопка «Закрыть» или клик за пределами окна. Прокрутка: колесо,
        стрелки, PageUp / PageDown / пробел, Home / End, перетаскивание полосы прокрутки.

        Args:
            ev (pygame.event.Event): событие pygame.
        """
        ab = self.about
        if ab is None:
            return
        g = self.about_geometry()
        page = g["body"].h * 0.9
        if ev.type == pygame.KEYDOWN:
            k = ev.key
            if k in (pygame.K_ESCAPE, pygame.K_F1):
                self.close_about()
            elif k == pygame.K_PAGEDOWN or k == pygame.K_SPACE:
                self.about_scroll_to(ab.scroll + page)
            elif k == pygame.K_PAGEUP:
                self.about_scroll_to(ab.scroll - page)
            elif k == pygame.K_DOWN:
                self.about_scroll_to(ab.scroll + 40)
            elif k == pygame.K_UP:
                self.about_scroll_to(ab.scroll - 40)
            elif k == pygame.K_HOME:
                self.about_scroll_to(0)
            elif k == pygame.K_END:
                self.about_scroll_to(1e9)
        elif ev.type == pygame.MOUSEWHEEL:
            self.about_scroll_to(ab.scroll - getattr(ev, "precise_y", ev.y) * 48)
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            if g["close"].collidepoint(ev.pos):
                self.close_about()
            elif g["track"].inflate(14, 0).collidepoint(ev.pos):
                self.drag = dict(t="about_bar")
                self._about_bar_to(ev.pos[1])
            elif not g["box"].collidepoint(ev.pos):
                self.close_about()
        elif ev.type == pygame.MOUSEMOTION and self.drag and self.drag.get("t") == "about_bar":
            self._about_bar_to(ev.pos[1])
        elif ev.type == pygame.MOUSEBUTTONUP:
            self.drag = None

    def _about_bar_to(self, y: int) -> None:
        """Прокручивает README по положению указателя на полосе прокрутки.

        Args:
            y (int): экранная ордината указателя.
        """
        track = self.about_geometry()["track"]
        self.about_scroll_to((y - track.y) / max(1, track.h) * self._about_max_scroll())

    def draw_about(self) -> None:
        """Рисует окно «О программе»: шапку с версиями, README с прокруткой и полосу прокрутки."""
        ab = self.about
        if ab is None:
            return
        scr = self.screen
        w, h = self.size
        g = self.about_geometry()
        shade = pygame.Surface((w, h), pygame.SRCALPHA)
        shade.fill((20, 28, 40, 150))
        scr.blit(shade, (0, 0))
        box, head, body = g["box"], g["head"], g["body"]
        pygame.draw.rect(scr, (255, 255, 255), box, 0, border_radius=10)
        pygame.draw.rect(scr, BORDER, box, 1, border_radius=10)
        self.t("О программе", box.x + 32, head.y + 24, 19, INK, True)
        sdl = ".".join(str(v) for v in getattr(pygame.version, "SDL", ()))
        self.t(f"ER Editor {__version__}   ·   Python {platform.python_version()}   ·   pygame {pygame.version.ver}"
               + (f" (SDL {sdl})" if sdl else ""), box.x + 32, head.y + 46, 12, MUTED)
        close = g["close"]
        hot = close.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(scr, BTN_HOT if hot else BTN, close, 0, border_radius=6)
        pygame.draw.rect(scr, BORDER, close, 1, border_radius=6)
        self.t("Закрыть (Esc)", close.centerx, close.centery, 13, INK, anchor="m")
        pygame.draw.line(scr, BORDER, (box.x + 1, head.bottom), (box.right - 1, head.bottom))

        content = self._about_surface()
        ab.scroll = int(max(0, min(ab.scroll, self._about_max_scroll())))
        prev = scr.get_clip()
        scr.set_clip(body.inflate(-2, -2))
        scr.blit(content, (body.x + 32, body.y + 12), area=pygame.Rect(0, ab.scroll, content.get_width(), body.h - 12))
        scr.set_clip(prev)
        max_scroll = self._about_max_scroll()
        if max_scroll > 0:
            track = g["track"]
            pygame.draw.rect(scr, (236, 239, 243), track, 0, border_radius=4)
            thumb_h = max(36, int(track.h * body.h / (body.h + max_scroll)))
            ty = track.y + int((track.h - thumb_h) * ab.scroll / max_scroll)
            pygame.draw.rect(scr, (170, 180, 194), pygame.Rect(track.x, ty, track.w, thumb_h), 0, border_radius=4)

    # ───────────────────────── ввод ─────────────────────────

    def handle(self, ev: pygame.event.Event) -> None:
        """Обрабатывает одно событие pygame.

        Маршрутизация: выход → перетаскивание файла → окно «О программе» → модальное окно →
        клавиатура / мышь / колесо.

        Args:
            ev (pygame.event.Event): событие pygame.
        """
        t = ev.type
        if t == pygame.QUIT:
            self.request_quit()
        elif t == pygame.DROPFILE:
            self.open_path(ev.file)
        elif self.modal:
            self.modal_event(ev)
        elif self.about:
            self.about_event(ev)
        elif t == pygame.KEYDOWN:
            self.on_key(ev)
        elif t == pygame.TEXTINPUT:
            if self.edit:
                self.edit.ted.insert(ev.text)
        elif t == pygame.MOUSEBUTTONDOWN:
            self.on_mouse_down(ev)
        elif t == pygame.MOUSEBUTTONUP:
            self.on_mouse_up(ev)
        elif t == pygame.MOUSEMOTION:
            self.on_motion(ev)
        elif t == pygame.MOUSEWHEEL:
            self.on_wheel(ev)
        self.rescene()

    def on_mouse_up(self, ev: pygame.event.Event) -> None:
        """Завершает перетаскивание; протянутая стрелка связи создаёт связь, если отпущена над другой таблицей.

        Отпускание над пустым местом после протягивания отменяет начатую связь; отпускание над
        той же таблицей оставляет режим «клик по главной таблице».

        Args:
            ev (pygame.event.Event): событие ``MOUSEBUTTONUP``.
        """
        dg, self.drag = self.drag, None
        if not dg or dg["t"] != "linkdrag" or not dg["moved"] or self.tool != "link":
            return
        src = self.link_src
        nid = self.scene.node_at(*self.s2w(ev.pos))
        if src and nid and nid != src:
            self.link_src = None
            self.create_link(src, nid)
            self.tool = "select"
        elif not nid:
            self.link_src = None

    def modal_event(self, ev: pygame.event.Event) -> None:
        """Обрабатывает ввод в модальном окне: Enter — первая кнопка, Esc — последняя, клик — нажатая кнопка.

        Args:
            ev (pygame.event.Event): событие pygame.
        """
        modal = self.modal
        if modal is None:
            return
        btns = modal.buttons

        def pick(i: int) -> None:
            """Закрывает окно и выполняет действие выбранной кнопки.

            Args:
                i (int): индекс кнопки.
            """
            cb = btns[i][1]
            self.modal = None
            if cb:
                cb()
        if ev.type == pygame.KEYDOWN:
            if ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                pick(0)
            elif ev.key == pygame.K_ESCAPE:
                pick(len(btns) - 1)
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            for i, rect in enumerate(self.modal_regions):
                if rect.collidepoint(ev.pos):
                    pick(i)
                    return

    def on_key(self, ev: pygame.event.Event) -> None:
        """Обрабатывает клавиатуру: правку текста (если активна), иначе горячие клавиши (см. справку, F1).

        Args:
            ev (pygame.event.Event): событие ``KEYDOWN``.
        """
        k, mod = ev.key, ev.mod
        ch = letter(ev)
        cmd = bool(mod & (pygame.KMOD_CTRL | pygame.KMOD_META))
        shift = bool(mod & pygame.KMOD_SHIFT)
        alt = bool(mod & pygame.KMOD_ALT)
        if self.edit:
            res = self.edit.ted.key(ev)
            if res == "commit":
                self.end_edit(True)
            elif res == "cancel":
                self.end_edit(False)
            elif k == pygame.K_TAB:
                self.end_edit(True)
            return
        if self.help:
            self.help = False
            return
        if cmd:
            if ch == "z":
                self.redo() if shift else self.undo()
            elif ch == "y":
                self.redo()
            elif ch == "s":
                self.save_project()
            elif ch == "o":
                self.open_db_dialog()
            elif ch == "e":
                self.export_dialog(both=shift)
            elif ch == "d":
                self.duplicate_node()
            elif ch == "b":
                self.download_bundle_dialog() if shift else self.download_db_dialog()
            elif ch == "0":
                self.set_zoom_100()
            elif ch == "1":
                self.fit_view()
            elif k in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
                self.zoom_center(1.2)
            elif k in (pygame.K_MINUS, pygame.K_KP_MINUS):
                self.zoom_center(1 / 1.2)
            return
        if k == pygame.K_ESCAPE:
            if self.tool != "select":
                self.tool, self.link_src = "select", None
            else:
                self.sel = Sel()
        elif k in (pygame.K_DELETE, pygame.K_BACKSPACE):
            self.delete_selection()
        elif k == pygame.K_TAB:
            self.switch_kind(KIND_DATA if self.kind == KIND_INFO else KIND_INFO)
        elif k in (pygame.K_F2, pygame.K_RETURN, pygame.K_KP_ENTER):
            self.edit_selected()
        elif k == pygame.K_F1:
            self.help = True
        elif k in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
            self.zoom_center(1.2)
        elif k in (pygame.K_MINUS, pygame.K_KP_MINUS):
            self.zoom_center(1 / 1.2)
        elif k in (pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT):
            dx = {pygame.K_LEFT: -1, pygame.K_RIGHT: 1}.get(k, 0)
            dy = {pygame.K_UP: -1, pygame.K_DOWN: 1}.get(k, 0)
            if alt and dy:
                (self.move_row if self.sel.kind == "node" else self.move_item)(dy)
            else:
                self.nudge(dx, dy, 1 if shift else GRID_STEP)

    def nudge(self, dx: int, dy: int, step: int) -> None:
        """Сдвигает выделенный узел, надпись или легенду.

        Args:
            dx (int): направление по горизонтали (−1, 0, 1).
            dy (int): направление по вертикали (−1, 0, 1).
            step (int): величина сдвига в мировых единицах.
        """
        s, d = self.sel, self.d
        obj: Any = (d.node(s.id or "") if s.kind == "node" else d.note(s.id or "") if s.kind == "note"
                    else d.legend if s.kind == "legend" else None)
        if obj is None:
            return
        self.snapshot()
        obj.x += dx * step
        obj.y += dy * step

    def on_wheel(self, ev: pygame.event.Event) -> None:
        """Обрабатывает колесо / трекпад: над панелью — прокрутка панели; над холстом — прокрутка, с Cmd/Ctrl — масштаб.

        Args:
            ev (pygame.event.Event): событие ``MOUSEWHEEL``.
        """
        pos = pygame.mouse.get_pos()
        if self.panel_rect.collidepoint(pos):
            self.panel_scroll = max(0, min(max(0, self._panel_h - self.panel_rect.h + 20), self.panel_scroll - ev.y * 30))
        elif self.canvas_rect.collidepoint(pos):
            mods = pygame.key.get_mods()
            py, px = getattr(ev, "precise_y", ev.y) or ev.y, getattr(ev, "precise_x", ev.x) or ev.x
            if mods & (pygame.KMOD_CTRL | pygame.KMOD_META):
                self.zoom_at(1.1 ** py, *pos)
            else:
                self.cam.oy += py * 30
                self.cam.ox += px * 30

    def on_mouse_down(self, ev: pygame.event.Event) -> None:
        """Обрабатывает нажатие кнопки мыши: каретка в редактируемом поле → области кадра → холст.

        Args:
            ev (pygame.event.Event): событие ``MOUSEBUTTONDOWN``.
        """
        pos = ev.pos
        if self.edit:
            if self.edit.rect and self.edit.rect.collidepoint(pos):
                font = self.fonts.font(self.edit_px(), False)
                self.edit.ted.click(pos[0] - self.edit.rect.x - 7 + self.edit.scroll, font)
                return
            self.end_edit(True)
        if self.help:
            self.help = False
            return
        now = time.time()
        double = (now - self.last_click[0] < 0.4 and hypot(pos[0] - self.last_click[1][0], pos[1] - self.last_click[1][1]) < 6)
        self.last_click = (0.0 if double else now, pos)
        if ev.button == 1:
            for rect, cb, _ in reversed(self.regions):
                if rect.collidepoint(pos):
                    cb()
                    return
            if self.canvas_rect.collidepoint(pos):
                self.canvas_down(pos, double)
        elif ev.button in (2, 3) and self.canvas_rect.collidepoint(pos):
            c = self.cam
            self.drag = dict(t="pan", sx=pos[0], sy=pos[1], ox=c.ox, oy=c.oy)

    def edit_px(self) -> int:
        """Размер шрифта поля правки текста.

        Returns:
            int: размер в пикселях.
        """
        return 14

    def start_drag(self, t: str, pos: Pos, **kw: Any) -> None:
        """Запоминает начало перетаскивания (изменение модели и снимок undo — при первом заметном движении).

        Args:
            t (str): тип: ``"node"``, ``"resize"``, ``"lresize"``, ``"legend"``, ``"note"``,
                ``"end"``, ``"mid"``, ``"label"``.
            pos (Pos): экранная точка нажатия.
            **kw (Any): параметры перетаскивания (идентификаторы, смещения…).
        """
        self.drag = dict(t=t, spos=pos, moved=False, **kw)

    def canvas_down(self, pos: Pos, double: bool) -> None:
        """Обрабатывает нажатие на холсте.

        Порядок проверок задаёт приоритет объектов под курсором: создание связи → ручки
        выделенного объекта → подпись связи → надпись → легенда → узел → линия связи → пустое
        место (панорамирование). Двойной клик запускает правку текста.

        Args:
            pos (Pos): экранная точка нажатия.
            double (bool): ``True``, если это второй клик двойного.
        """
        wx, wy = self.s2w(pos)
        z = self.cam.z
        sc, d, s = self.scene, self.d, self.sel
        if self.tool == "link":
            nid = sc.node_at(wx, wy)
            if nid:
                if self.link_src is None:
                    self.link_src = nid
                    self.drag = dict(t="linkdrag", spos=pos, moved=False)
                else:
                    self.create_link(self.link_src, nid)
                    self.tool, self.link_src = "select", None
            return
        # ручки выделенных объектов
        if s.kind == "link" and s.id in sc.links:
            lg = sc.links[s.id or ""]
            for end, pt in (("a", lg.pa), ("b", lg.pb)):
                if hypot(wx - pt[0], wy - pt[1]) <= 9 / z:
                    self.start_drag("end", pos, id=s.id, end=end)
                    return
            if lg.mid_pt and hypot(wx - lg.mid_pt[0], wy - lg.mid_pt[1]) <= 9 / z:
                self.start_drag("mid", pos, id=s.id)
                return
        if s.kind == "node" and s.id in sc.nodes:
            r = sc.nodes[s.id or ""].rect
            n0 = d.node(s.id or "")
            if n0 and abs(wx - r.right) <= 6 / z and r.y - 4 / z <= wy <= r.bottom + 4 / z:
                self.start_drag("resize", pos, id=s.id, need=layout.needed_width(d, n0, self.fonts.measure))
                return
        if s.kind == "legend" and sc.legend:
            r = sc.legend.rect
            if abs(wx - r.right) <= 6 / z and r.y - 4 / z <= wy <= r.bottom + 4 / z:
                self.start_drag("lresize", pos)
                return
        # подпись связи
        hit = sc.link_label_at(wx, wy)
        if hit:
            lid, which = hit
            self.sel = Sel("link", lid)
            l = d.link(lid)
            if double:
                self.edit_link_label(lid, which)
            elif l:
                off = {"a": l.off_a, "b": l.off_b, "c": l.off_c}[which]
                self.start_drag("label", pos, id=lid, which=which, w0=(wx, wy), o0=list(off))
            return
        nid = sc.note_at(wx, wy)
        note = d.note(nid) if nid else None
        if nid and note:
            self.sel = Sel("note", nid)
            if double:
                self.edit_note(nid)
            else:
                self.start_drag("note", pos, id=nid, dx=wx - note.x, dy=wy - note.y)
            return
        if sc.legend and sc.legend.rect.contains(wx, wy):
            idx = next((i for i, r in enumerate(sc.legend.items) if r.y <= wy < r.bottom), None)
            self.sel = Sel("legend", None, idx)
            if double:
                if idx is not None:
                    self.edit_legend_item(idx)
                else:
                    self.edit_legend_title()
            else:
                self.start_drag("legend", pos, dx=wx - d.legend.x, dy=wy - d.legend.y)
            return
        nid = sc.node_at(wx, wy)
        n = d.node(nid) if nid else None
        if nid and n:
            row = sc.row_at(nid, wy)
            self.sel = Sel("node", nid, row)
            if double:
                if row is None:
                    self.edit_node_title(nid)
                else:
                    geo = sc.nodes[nid]
                    field = "name"
                    if d.kind == KIND_DATA:
                        field = "type" if wx >= geo.type_x else "key" if wx < geo.rect.x + 52 else "name"
                    self.edit_row(nid, row, field)
            else:
                self.start_drag("node", pos, id=nid, dx=wx - n.x, dy=wy - n.y)
            return
        lid2 = sc.link_at(wx, wy, 7 / z)
        if lid2:
            self.sel = Sel("link", lid2)
            return
        self.sel = Sel()
        c = self.cam
        self.drag = dict(t="pan", sx=pos[0], sy=pos[1], ox=c.ox, oy=c.oy)

    def on_motion(self, ev: pygame.event.Event) -> None:
        """Продолжает перетаскивание: узел, ширину, концы и излом связи, подписи, легенду, надпись или холст.

        Первое заметное движение (больше 3 пикселей) записывает снимок для undo.

        Args:
            ev (pygame.event.Event): событие ``MOUSEMOTION``.
        """
        dg = self.drag
        if not dg:
            return
        pos = ev.pos
        if dg["t"] == "pan":
            self.cam.ox = dg["ox"] + pos[0] - dg["sx"]
            self.cam.oy = dg["oy"] + pos[1] - dg["sy"]
            return
        if dg["t"] == "linkdrag":
            dg["moved"] = dg["moved"] or hypot(pos[0] - dg["spos"][0], pos[1] - dg["spos"][1]) >= 4
            return
        if not dg["moved"]:
            if hypot(pos[0] - dg["spos"][0], pos[1] - dg["spos"][1]) < 3:
                return
            self.snapshot()
            dg["moved"] = True
        wx, wy = self.s2w(pos)
        d, g = self.d, self.project.grid
        t = dg["t"]
        if t == "node":
            n = d.node(dg["id"])
            if n:
                n.x, n.y = snap(wx - dg["dx"], g), snap(wy - dg["dy"], g)
        elif t == "resize":
            n = d.node(dg["id"])
            if n:
                n.w = max(dg["need"], snap(wx - n.x, g))
        elif t == "lresize":
            d.legend.w = max(100.0, snap(wx - d.legend.x, g))
        elif t == "legend":
            d.legend.x, d.legend.y = snap(wx - dg["dx"], g), snap(wy - dg["dy"], g)
        elif t == "note":
            note = d.note(dg["id"])
            if note:
                note.x, note.y = snap(wx - dg["dx"], g), snap(wy - dg["dy"], g)
        elif t == "end":
            l = d.link(dg["id"])
            if l:
                nid = l.a if dg["end"] == "a" else l.b
                geo = self.scene.nodes.get(nid)
                if geo:
                    side, tt = nearest_anchor(geo.rect, wx, wy)
                    old = l.sa if dg["end"] == "a" else l.sb
                    if (old in ("top", "bottom")) != (side in ("top", "bottom")):
                        l.mid = None
                    if dg["end"] == "a":
                        l.sa, l.ta = side, tt
                    else:
                        l.sb, l.tb = side, tt
        elif t == "mid":
            l = d.link(dg["id"])
            lg = self.scene.links.get(dg["id"])
            if l and lg and lg.axis:
                l.mid = float(snap(wx if lg.axis == "x" else wy, g))
        elif t == "label":
            l = d.link(dg["id"])
            if l:
                off = {"a": l.off_a, "b": l.off_b, "c": l.off_c}[dg["which"]]
                off[0] = dg["o0"][0] + wx - dg["w0"][0]
                off[1] = dg["o0"][1] + wy - dg["w0"][1]

    # ───────────────────────── рисование: общее ─────────────────────────

    def t(self, s: str, x: float, y: float, px: int = 13, color: Color = INK, bold: bool = False,
          anchor: str = "l", italic: bool = False, surf: Optional[pygame.Surface] = None) -> pygame.Rect:
        """Рисует текст интерфейса.

        Args:
            s (str): текст (пустой не рисуется).
            x (float): экранная абсцисса точки привязки.
            y (float): экранная ордината середины строки.
            px (int): размер шрифта в пикселях.
            color (Color): цвет текста.
            bold (bool): жирное начертание.
            anchor (str): ``"l"`` — ``x`` слева от текста, ``"m"`` — по центру, ``"r"`` — справа.
            italic (bool): курсив.
            surf (Optional[pygame.Surface]): поверхность; ``None`` — экран окна.

        Returns:
            pygame.Rect: прямоугольник, занятый текстом (нулевой для пустого текста).
        """
        if not s:
            return pygame.Rect(round(x), round(y), 0, 0)
        img = self.fonts.surface(s, px, bold, italic, color)
        r = img.get_rect()
        r.centery = round(y)
        r.left = round(x) if anchor == "l" else round(x - r.w / 2) if anchor == "m" else round(x - r.w)
        (surf or self.screen).blit(img, r)
        return r

    def reg(self, rect: pygame.Rect, cb: Callback, tip: str = "") -> None:
        """Регистрирует кликабельную область кадра (обрезается по текущей области отсечения панели).

        Args:
            rect (pygame.Rect): экранный прямоугольник области.
            cb (Callback): действие при клике.
            tip (str): подсказка для строки состояния при наведении.
        """
        if self._clip is not None:
            rect = rect.clip(self._clip)
            if rect.w <= 0 or rect.h <= 0:
                return
        self.regions.append((rect, cb, tip))

    def button(self, rect: pygame.Rect, label: str, cb: Callback, active: bool = False, enabled: bool = True,
               tip: str = "", danger: bool = False, px: int = 13, tone: str = "") -> None:
        """Рисует кнопку и регистрирует её область.

        Args:
            rect (pygame.Rect): экранный прямоугольник кнопки.
            label (str): подпись.
            cb (Callback): действие при клике.
            active (bool): ``True`` — кнопка «нажата» (выбранный режим).
            enabled (bool): ``False`` — серая, не реагирует на клики.
            tip (str): подсказка в строке состояния при наведении.
            danger (bool): ``True`` — красная подпись (опасное действие).
            px (int): размер шрифта подписи.
            tone (str): оттенок кнопки (``"ok"``, ``"warn"``, ``"bad"``) или пустая строка.
        """
        mouse = pygame.mouse.get_pos()
        hot = enabled and rect.collidepoint(mouse) and (self._clip is None or self._clip.collidepoint(mouse))
        fill = BTN_ON if active else BTN_HOT if hot else BTN
        edge = ACCENT if active else BORDER
        if tone in TONES:
            fill, edge = TONES[tone]
        pygame.draw.rect(self.screen, fill, rect, 0, border_radius=6)
        pygame.draw.rect(self.screen, edge, rect, 2 if tone in TONES else 1, border_radius=6)
        color = FAINT if not enabled else DANGER if danger else INK
        self.t(label, rect.centerx, rect.centery, px, color, active, "m")
        if enabled:
            self.reg(rect, cb, tip)
        if hot and tip:
            self.hover_tip = tip

    def draw_edit_text(self, rect: pygame.Rect, e: Edit, px: int = 14) -> None:
        """Рисует содержимое редактируемого поля: текст, выделение и мигающую каретку (с прокруткой).

        Args:
            rect (pygame.Rect): экранный прямоугольник поля.
            e (Edit): активная правка.
            px (int): размер шрифта.
        """
        font = self.fonts.font(px)
        ted = e.ted
        inner = rect.inflate(-10, -4)
        prev = self.screen.get_clip()
        self.screen.set_clip(inner.clip(prev) if prev else inner)
        caret_x = font.size(ted.text[:ted.cur])[0]
        scroll = max(0, caret_x - (inner.w - 4))
        e.scroll = scroll
        s = ted.sel()
        if s:
            x0, x1 = font.size(ted.text[:s[0]])[0], font.size(ted.text[:s[1]])[0]
            pygame.draw.rect(self.screen, (178, 205, 250), (inner.x + x0 - scroll, inner.y, x1 - x0, inner.h))
        self.t(ted.text, inner.x - scroll, rect.centery, px, INK)
        if (pygame.time.get_ticks() // 500) % 2 == 0:
            cx = inner.x + caret_x - scroll
            pygame.draw.line(self.screen, INK, (cx, inner.y + 1), (cx, inner.bottom - 1), 1)
        self.screen.set_clip(prev)

    def field(self, rect: pygame.Rect, fid: str, value: str, commit: CommitFn) -> None:
        """Рисует текстовое поле панели свойств: значение, по клику — правка с кареткой.

        Args:
            rect (pygame.Rect): экранный прямоугольник поля.
            fid (str): уникальный идентификатор поля (по нему определяется, редактируется ли оно).
            value (str): текущее значение.
            commit (CommitFn): получает новое значение при завершении правки.
        """
        editing = self.edit is not None and self.edit.fid == fid
        pygame.draw.rect(self.screen, (255, 255, 255), rect, 0, border_radius=5)
        pygame.draw.rect(self.screen, ACCENT if editing else BORDER, rect, 2 if editing else 1, border_radius=5)
        if editing and self.edit is not None:
            self.edit.rect = rect.copy()
            self.draw_edit_text(rect, self.edit)
        else:
            prev = self.screen.get_clip()
            inner = rect.inflate(-10, 0)
            self.screen.set_clip(inner.clip(prev) if prev else inner)
            self.t(value, rect.x + 7, rect.centery, 13, INK)
            self.screen.set_clip(prev)
            self.reg(rect, lambda: self.begin_edit(fid, value, commit))

    # ───────────────────────── рисование: окно ─────────────────────────

    def draw(self) -> None:
        """Рисует весь кадр: холст, панель свойств, панель инструментов, строку состояния и окна поверх.

        Raises:
            pygame.error: если поверхность окна недоступна.
        """
        self.screen = scr = pygame.display.get_surface() or self.screen
        self.regions, self.modal_regions, self.hover_tip = [], [], ""
        caption = f"ER Editor — {self.base_name()}{' *' if self.modified else ''}"
        if caption != self._caption:
            self._caption = caption
            pygame.display.set_caption(caption)
        scr.fill(BG)
        self.draw_canvas()
        self.draw_panel()
        self.draw_toolbar()
        self.draw_status()
        if self.modal:
            self.draw_modal()
        if self.help:
            self.draw_help()
        if self.about:
            self.draw_about()
        if self.toast_msg and time.time() < self.toast_until and not self.about:
            w = self.fonts.font(13).size(self.toast_msg)[0] + 28
            r = pygame.Rect(0, 0, min(w, self.size[0] - 40), 34)
            r.midbottom = (self.canvas_rect.centerx, self.canvas_rect.bottom - 14)
            pygame.draw.rect(scr, (38, 50, 66), r, 0, border_radius=8)
            self.t(self.toast_msg, r.centerx, r.centery, 13, (255, 255, 255), anchor="m")

    def draw_canvas(self) -> None:
        """Рисует холст: сетку, диаграмму с выделением, подсветку при создании связи и правку на месте."""
        scr, cr = self.screen, self.canvas_rect
        scr.set_clip(cr)
        scr.fill((255, 255, 255), cr)
        g = Gfx(scr, self.fonts, self.cam.ox, self.cam.oy, self.cam.z)
        if self.project.grid:
            draw_grid(g, cr)
        self.rescene()
        sc = self.scene
        draw_diagram(g, sc, self.sel, self.nf_flags())
        if self.tool == "link":
            hover = sc.node_at(*self.s2w(pygame.mouse.get_pos()))
            for nid, col in ((self.link_src, ORANGE), (hover, ACCENT)):
                if nid in sc.nodes:
                    g.rect(sc.nodes[nid].rect.inflated(4), None, col, 3, 16)
            if self.link_src in sc.nodes:
                self.draw_rubber_arrow(g, sc.nodes[self.link_src].rect)
        if not self.d.nodes and not self.project.db_path:
            self.t("Перетащите файл базы данных (.db) в окно", cr.centerx, cr.centery - 14, 20, MUTED, anchor="m")
            self.t("или нажмите «Открыть БД» / Cmd+O.  F1 — справка", cr.centerx, cr.centery + 16, 14, FAINT, anchor="m")
            self.t("Или рисуйте с нуля: вкладка «Даталогическая» → «+ Таблица» → «Связь» → форма 1НФ…НФБК → «БД и модели»",
                   cr.centerx, cr.centery + 42, 14, FAINT, anchor="m")
        self.draw_inline_edit()
        scr.set_clip(None)

    def draw_rubber_arrow(self, g: Gfx, src: FRect) -> None:
        """Рисует «резиновую» стрелку от таблицы-источника к курсору при создании связи.

        Args:
            g (Gfx): графический контекст холста.
            src (FRect): прямоугольник таблицы, от которой тянется связь.
        """
        mx, my = self.s2w(pygame.mouse.get_pos())
        sx, sy = min(max(mx, src.x), src.right), min(max(my, src.y), src.bottom)
        length = hypot(mx - sx, my - sy)
        if length < 8:
            return
        g.line((sx, sy), (mx, my), ORANGE, 2.4)
        draw_arrow(g, (mx, my), ((sx - mx) / length, (sy - my) / length), ORANGE)

    def draw_inline_edit(self) -> None:
        """Рисует поле правки поверх редактируемого текста на холсте; если текст исчез — отменяет правку."""
        e = self.edit
        if not e or e.inline is None:
            return
        r = e.inline(self.scene)
        if r is None:
            self.edit = None
            return
        x, y = self.w2s(r.x, r.y)
        w, h = r.w * self.cam.z, r.h * self.cam.z
        box = pygame.Rect(round(x) - 4, round(y + h / 2) - 15, round(max(w + 8, 170)), 30)
        box.clamp_ip(self.canvas_rect)
        pygame.draw.rect(self.screen, (255, 255, 255), box, 0, border_radius=5)
        pygame.draw.rect(self.screen, ACCENT, box, 2, border_radius=5)
        e.rect = box
        self.draw_edit_text(box, e, self.edit_px())
        try:
            pygame.key.set_text_input_rect(box)
        except Exception:
            pass

    # ----- панель инструментов -----

    def toolbar_groups(self) -> list[list[ToolItem]]:
        """Составляет группы элементов панели инструментов для текущего состояния.

        Returns:
            list[list[ToolItem]]: группы кнопок и чекбокс «случайные записи».
        """
        d = self.d
        new_label = "+ Сущность" if d.kind == KIND_INFO else "+ Таблица"
        has_tables = bool(self.project.diagrams[KIND_DATA].nodes)
        rep = self.nf_report()
        tone = "" if not rep.tables else "bad" if not rep.ok else "warn" if rep.warnings else "ok"
        return [
            [ToolItem("Открыть БД", self.open_db_dialog, tip="Открыть базу данных SQLite или проект (Cmd+O)"),
             ToolItem("Сохранить", self.save_project, tip="Сохранить проект .erd.json (Cmd+S)"),
             ToolItem("Экспорт PNG", self.export_dialog, tip="Экспорт текущей диаграммы в PNG (Cmd+E); Cmd+Shift+E — обе")],
            [ToolItem("Инфологическая", lambda: self.switch_kind(KIND_INFO), self.kind == KIND_INFO, tip="Нотация Баркера (Tab)"),
             ToolItem("Даталогическая", lambda: self.switch_kind(KIND_DATA), self.kind == KIND_DATA, tip="Таблицы, ключи, типы (Tab)")],
            [ToolItem(new_label, self.add_node, tip="Добавить на холст"),
             ToolItem("Связь", self.start_link_tool, self.tool == "link", len(d.nodes) >= 1,
                      "Протяните стрелку от подчинённой таблицы (внешний ключ) к главной или кликните их по очереди"),
             ToolItem("Надпись", self.add_note, tip="Свободный текст"),
             ToolItem("Легенда", self.toggle_legend, d.legend.visible, tip="Показать / скрыть легенду"),
             ToolItem("Авто-раскладка", self.auto_layout, enabled=bool(d.nodes), tip="Расставить узлы и связи заново")],
            [ToolItem("Норм. форма:", self.show_nf_report, static=True)]
            + [ToolItem(normalform.LEVEL_NAMES[k], partial(self.set_normal_form, k), self.project.normal_form == k,
                        tip=normalform.LEVEL_NAMES[k] + ": " + normalform.LEVEL_RULES[k]) for k in normalform.LEVELS]
            + [ToolItem(rep.short(), self.show_nf_report, tone=tone, tip=rep.verdict() + " — клик: подробности справа")],
            [ToolItem("Скачать БД", self.download_db_dialog, enabled=has_tables,
                      tip="Создать файл SQLite по даталогической диаграмме (Cmd+B)"),
             ToolItem("БД и модели", self.download_bundle_dialog, enabled=has_tables,
                      tip="Файл SQLite + обе модели в PNG + отчёт о проверке нормальной формы (Cmd+Shift+B)"),
             ToolItem(f"{dbgen.SAMPLE_ROWS} случайных записей", self.toggle_fill_sample, self.project.fill_sample, True,
                      f"Заполнить каждую таблицу {dbgen.SAMPLE_ROWS} случайными записями при создании базы", check=True)],
            [ToolItem("Отмена", self.undo, enabled=bool(self.undo_stack), tip="Отменить действие (Cmd+Z)"),
             ToolItem("Повтор", self.redo, enabled=bool(self.redo_stack), tip="Вернуть отменённое (Cmd+Shift+Z)"),
             ToolItem("?", lambda: setattr(self, "help", True), tip="Краткая справка по клавишам (F1)"),
             ToolItem("О программе", self.open_about, self.about is not None, tip="Описание и руководство (README.md)")],
        ]

    def toolbar_layout(self) -> tuple[int, list[tuple[ToolItem, pygame.Rect]]]:
        """Раскладывает элементы панели инструментов; группы, не поместившиеся по ширине, переносятся вниз.

        Returns:
            tuple[int, list[tuple[ToolItem, pygame.Rect]]]: ``(число строк, [(элемент, прямоугольник)])``.
        """
        w = self.size[0]
        font = self.fonts.font(TOOLBAR_PX)
        pad, gap, ggap = 8, 5, 12
        placed: list[tuple[ToolItem, pygame.Rect]] = []
        row, x = 0, 10
        for grp in self.toolbar_groups():
            widths = [font.size(it.label)[0] + (30 if it.check else 2 * pad) for it in grp]
            gw = sum(widths) + gap * (len(widths) - 1)
            if x > 10 and x + gw > w - 10:
                row, x = row + 1, 10
            for it, iw in zip(grp, widths):
                if x > 10 and x + iw > w - 10:
                    row, x = row + 1, 10
                placed.append((it, pygame.Rect(x, row * TOOLBAR_H + 8, iw, TOOLBAR_H - 16)))
                x += iw + gap
            x += ggap - gap
        return row + 1, placed

    def checkbox(self, rect: pygame.Rect, label: str, checked: bool, cb: Callback, enabled: bool = True, tip: str = "",
                 px: int = 13) -> None:
        """Рисует чекбокс с подписью и регистрирует его область.

        Args:
            rect (pygame.Rect): область элемента (квадратик и подпись справа от него).
            label (str): подпись.
            checked (bool): отмечен ли чекбокс.
            cb (Callback): действие при клике (переключение).
            enabled (bool): ``False`` — серый, не реагирует на клики.
            tip (str): подсказка в строке состояния при наведении.
            px (int): размер шрифта подписи.
        """
        hot = enabled and rect.collidepoint(pygame.mouse.get_pos())
        box = pygame.Rect(rect.x, rect.centery - 9, 18, 18)
        pygame.draw.rect(self.screen, ACCENT if checked else (BTN_HOT if hot else BTN), box, 0, border_radius=4)
        pygame.draw.rect(self.screen, ACCENT if (checked or hot) else BORDER, box, 2, border_radius=4)
        if checked:
            pygame.draw.lines(self.screen, (255, 255, 255), False,
                              [(box.x + 4, box.y + 9), (box.x + 8, box.y + 13), (box.x + 14, box.y + 5)], 2)
        self.t(label, box.right + 8, rect.centery, px, INK if enabled else FAINT)
        if enabled:
            self.reg(rect, cb, tip)
        if hot and tip:
            self.hover_tip = tip

    def draw_toolbar(self) -> None:
        """Рисует верхнюю панель инструментов (одна или несколько строк) с кнопками и чекбоксом."""
        scr, w = self.screen, self.size[0]
        rows, placed = self.toolbar_layout()
        bar = pygame.Rect(0, 0, w, TOOLBAR_H * rows)
        pygame.draw.rect(scr, BAR_BG, bar)
        pygame.draw.line(scr, BORDER, (0, bar.bottom - 1), (w, bar.bottom - 1))
        for it, r in placed:
            if it.static:
                self.t(it.label, r.x + 2, r.centery, TOOLBAR_PX, MUTED)
            elif it.check:
                self.checkbox(r, it.label, it.active, it.cb, it.enabled, it.tip, TOOLBAR_PX)
            else:
                self.button(r, it.label, it.cb, it.active, it.enabled, it.tip, px=TOOLBAR_PX, tone=it.tone)

    def start_link_tool(self) -> None:
        """Включает / выключает режим создания связи (кликнуть подчинённую, затем главную сущность)."""
        self.tool = "select" if self.tool == "link" else "link"
        self.link_src = None
        self.sel = Sel()

    # ----- строка состояния -----

    def draw_status(self) -> None:
        """Рисует строку состояния: подсказка или сводка слева, управление масштабом справа."""
        scr = self.screen
        w, h = self.size
        r = pygame.Rect(0, h - STATUS_H, w, STATUS_H)
        pygame.draw.rect(scr, BAR_BG, r)
        pygame.draw.line(scr, BORDER, (0, r.y), (w, r.y))
        if self.tool == "link":
            noun = "сущность" if self.kind == KIND_INFO else "таблицу"
            msg = (f"Отпустите кнопку (или кликните) на главной {noun} — туда, куда указывает стрелка. Esc — отмена" if self.link_src else
                   f"Связь: нажмите на подчинённую {noun} (где внешний ключ, сторона «много») и протяните стрелку к главной. Esc — отмена")
        elif self.hover_tip:
            msg = self.hover_tip
        else:
            n, ln = len(self.d.nodes), len(self.d.links)
            what = "сущностей" if self.kind == KIND_INFO else "таблиц"
            msg = f"{KIND_TITLES[self.kind]}:  {what} — {n},  связей — {ln}"
            if self.kind == KIND_DATA and n:
                msg += "   •   " + self.nf_report().verdict()
            if self.modified:
                msg += "   •   есть несохранённые изменения"
        self.t(msg, 12, r.centery, 12, MUTED)
        cam = self.cam
        x = w - 12
        zoom_buttons: list[tuple[str, Callback, int, str]] = [
            ("Вписать", self.fit_view, 66, "Показать всю диаграмму (Cmd+1)"),
            ("+", lambda: self.zoom_center(1.2), 26, "Приблизить"),
            (f"{round(cam.z * 100)}%", self.set_zoom_100, 54, "Масштаб 100% (Cmd+0)"),
            ("−", lambda: self.zoom_center(1 / 1.2), 26, "Отдалить")]
        for label, cb, bw, tip in zoom_buttons:
            x -= bw
            self.button(pygame.Rect(x, r.y + 3, bw, STATUS_H - 6), label, cb, tip=tip, px=12)
            x -= 4

    # ----- модальное окно и справка -----

    def draw_modal(self) -> None:
        """Рисует модальное окно с вопросом и кнопками (области кнопок — в ``modal_regions``)."""
        modal = self.modal
        if modal is None:
            return
        scr = self.screen
        w, h = self.size
        shade = pygame.Surface((w, h), pygame.SRCALPHA)
        shade.fill((20, 28, 40, 110))
        scr.blit(shade, (0, 0))
        width = 560
        words, lines, cur = modal.text.split(), [], ""
        for wd in words:
            if self.fonts.font(13).size(cur + " " + wd)[0] > width - 48:
                lines.append(cur)
                cur = wd
            else:
                cur = (cur + " " + wd).strip()
        lines.append(cur)
        lines = lines[:12]
        box = pygame.Rect(0, 0, width, max(190, 128 + 20 * len(lines)))
        box.center = (w // 2, h // 2)
        pygame.draw.rect(scr, (255, 255, 255), box, 0, border_radius=10)
        pygame.draw.rect(scr, BORDER, box, 1, border_radius=10)
        self.t(modal.title, box.x + 24, box.y + 30, 16, INK, True)
        for i, ln in enumerate(lines):
            self.t(ln, box.x + 24, box.y + 66 + i * 20, 13, MUTED)
        n = len(modal.buttons)
        bw = (box.w - 48 - 10 * (n - 1)) // n
        for i, (label, _) in enumerate(modal.buttons):
            r = pygame.Rect(box.x + 24 + i * (bw + 10), box.bottom - 56, bw, 34)
            hot = r.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(scr, BTN_ON if i == 0 else BTN_HOT if hot else BTN, r, 0, border_radius=6)
            pygame.draw.rect(scr, ACCENT if i == 0 else BORDER, r, 1, border_radius=6)
            self.t(label, r.centerx, r.centery, 13, INK, i == 0, "m")
            self.modal_regions.append(r)

    def draw_help(self) -> None:
        """Рисует краткую справку по мыши и клавишам (закрывается любым кликом или клавишей)."""
        scr = self.screen
        w, h = self.size
        shade = pygame.Surface((w, h), pygame.SRCALPHA)
        shade.fill((20, 28, 40, 140))
        scr.blit(shade, (0, 0))
        box = pygame.Rect(0, 0, min(980, w - 60), 60 + len(HELP_LINES) * 26 + 40)
        box.center = (w // 2, h // 2)
        pygame.draw.rect(scr, (255, 255, 255), box, 0, border_radius=10)
        self.t("ER Editor — справка", box.x + 24, box.y + 30, 17, INK, True)
        y = box.y + 66
        for key, text in HELP_LINES:
            if not text:
                self.t(key, box.x + 24, y, 13, ACCENT, True)
            else:
                self.t(key, box.x + 40, y, 13, INK, True)
                self.t(text, box.x + 290, y, 13, MUTED)
            y += 26
        self.t("Любая клавиша или клик — закрыть.  Полное описание — кнопка «О программе»", box.x + 24, box.bottom - 22, 12, FAINT)

    # ───────────────────────── панель свойств ─────────────────────────
    # Строки панели выкладываются сверху вниз; текущая ордината — self._py.

    def p_header(self, text: str, color: Color = INK) -> None:
        """Рисует заголовок раздела панели.

        Args:
            text (str): текст заголовка.
            color (Color): цвет текста.
        """
        x0 = self.panel_rect.x
        self.t(text, x0 + 12, self._py + 12, 14, color, True)
        self._py += 28

    def p_note(self, text: str, color: Color = MUTED, px: int = 12) -> None:
        """Рисует абзац пояснения с переносом по словам.

        Args:
            text (str): текст.
            color (Color): цвет текста.
            px (int): размер шрифта.
        """
        x0, w = self.panel_rect.x, PANEL_W - 24
        font = self.fonts.font(px)
        cur = ""
        for wd in text.split():
            if font.size(cur + " " + wd)[0] > w:
                self.t(cur, x0 + 12, self._py + 9, px, color)
                self._py += 18
                cur = wd
            else:
                cur = (cur + " " + wd).strip()
        self.t(cur, x0 + 12, self._py + 9, px, color)
        self._py += 22

    def wrap_text(self, text: str, px: int, width: int, bold: bool = False) -> list[str]:
        """Переносит текст по словам так, чтобы строки помещались в ширину.

        Args:
            text (str): текст.
            px (int): размер шрифта.
            width (int): допустимая ширина строки в пикселях.
            bold (bool): жирное начертание (влияет на ширину).

        Returns:
            list[str]: строки (слишком длинное слово остаётся в одной строке).
        """
        font = self.fonts.font(px, bold)
        lines: list[str] = []
        cur = ""
        for wd in text.split():
            trial = (cur + " " + wd).strip()
            if cur and font.size(trial)[0] > width:
                lines.append(cur)
                cur = wd
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    def p_issue(self, i: normalform.Issue) -> None:
        """Рисует карточку замечания проверки нормальной формы; клик показывает таблицу на схеме.

        Args:
            i (normalform.Issue): замечание.
        """
        bad = i.severity == normalform.ERROR
        col, tint = (FLAG_ERROR, (253, 236, 236)) if bad else (FLAG_WARN, (253, 245, 230))
        x0, inner = self.panel_rect.x + 12, PANEL_W - 24 - 22
        head = self.wrap_text(f"{normalform.LEVEL_NAMES[i.level]} · {(i.table + ': ') if i.table else ''}{i.rule}", 12, inner, True)
        body = self.wrap_text(i.message, 12, inner)
        tail = self.wrap_text("Что делать: " + i.hint, 11, inner) if i.hint else []
        h = 10 + 16 * len(head) + 15 * len(body) + 14 * len(tail) + 8
        r = pygame.Rect(x0, self._py, PANEL_W - 24, h)
        pygame.draw.rect(self.screen, tint, r, 0, border_radius=6)
        pygame.draw.rect(self.screen, col, pygame.Rect(r.x, r.y, 4, r.h), 0, border_radius=2)
        y = r.y + 17
        for ln in head:
            self.t(ln, r.x + 14, y, 12, col, True)
            y += 16
        for ln in body:
            self.t(ln, r.x + 14, y - 1, 12, INK)
            y += 15
        for ln in tail:
            self.t(ln, r.x + 14, y - 1, 11, MUTED)
            y += 14
        if i.node_id:
            self.reg(r, partial(self.focus_issue, i.node_id, list(i.columns)), "Показать на схеме")
        self._py += h + 6

    def panel_nf(self, node_id: Optional[str] = None) -> None:
        """Рисует раздел «Нормальная форма»: выбор формы, вердикт и карточки замечаний.

        Args:
            node_id (Optional[str]): идентификатор таблицы — показать только её замечания;
                ``None`` — выбор формы и все замечания схемы.
        """
        rep = self.nf_report()
        name = normalform.LEVEL_NAMES[rep.target]
        if node_id is None:
            self.p_header("Нормальная форма")
            self.p_choice("Проверять", [(normalform.LEVEL_NAMES[k], rep.target == k, partial(self.set_normal_form, k))
                                        for k in normalform.LEVELS])
            self.p_note(f"{name}: {normalform.LEVEL_RULES[rep.target]}.", px=11)
            ok = rep.ok
            self.p_note(rep.verdict(), (GOOD if ok else DANGER) if rep.tables else MUTED, 13)
            if rep.tables and not ok:
                got = normalform.LEVEL_NAMES[rep.achieved] if rep.achieved else "ни одна форма"
                self.p_note(f"Достигнута: {got}.", px=12)
            items = rep.ordered()
        else:
            items = rep.for_node(node_id)
            self.p_gap(6)
            self.p_header(f"Проверка {name}: эта таблица", MUTED)
            if not items:
                self.p_note("Замечаний нет.", GOOD)
        for i in items[:NF_PANEL_LIMIT]:
            self.p_issue(i)
        if len(items) > NF_PANEL_LIMIT:
            self.p_note(f"… и ещё {len(items) - NF_PANEL_LIMIT}", px=12)
        if node_id is None and rep.tables:
            self.p_buttons([("Отчёт в .md", self.nf_report_dialog, True, False)])
            self.p_note("Нарушения — по заявленным зависимостям и признакам в структуре схемы; «подозрение» не меняет вердикт. "
                        "Зависимости таблицы записываются в её панели в поле «Зависимости».", px=11)

    def p_gap(self, n: int = 8) -> None:
        """Добавляет вертикальный отступ в панели.

        Args:
            n (int): высота отступа в пикселях.
        """
        self._py += n

    def p_field(self, label: str, fid: str, value: str, commit: CommitFn) -> None:
        """Рисует строку «подпись + текстовое поле».

        Args:
            label (str): подпись слева.
            fid (str): уникальный идентификатор поля.
            value (str): текущее значение.
            commit (CommitFn): получает новое значение при завершении правки.
        """
        x0 = self.panel_rect.x
        self.t(label, x0 + 12, self._py + 13, 12, MUTED)
        self.field(pygame.Rect(x0 + 118, self._py, PANEL_W - 130, 26), fid, value, commit)
        self._py += 33

    def p_choice(self, label: str, options: list[tuple[str, bool, Callback]]) -> None:
        """Рисует строку «подпись + ряд кнопок-переключателей».

        Args:
            label (str): подпись слева.
            options (list[tuple[str, bool, Callback]]): кнопки ``(текст, активна, действие)``.
        """
        x0 = self.panel_rect.x
        self.t(label, x0 + 12, self._py + 13, 12, MUTED)
        total, n = PANEL_W - 130, len(options)
        bw = (total - 4 * (n - 1)) // n
        for i, (text, active, cb) in enumerate(options):
            self.button(pygame.Rect(x0 + 118 + i * (bw + 4), self._py, bw, 26), text, cb, active, px=12)
        self._py += 33

    def p_buttons(self, items: list[tuple[str, Callback, bool, bool]]) -> None:
        """Рисует ряд кнопок на всю ширину панели.

        Args:
            items (list[tuple[str, Callback, bool, bool]]): кнопки
                ``(текст, действие, доступна, опасная)``.
        """
        x0 = self.panel_rect.x
        n = len(items)
        bw = (PANEL_W - 24 - 6 * (n - 1)) // n
        for i, (text, cb, enabled, danger) in enumerate(items):
            self.button(pygame.Rect(x0 + 12 + i * (bw + 6), self._py, bw, 28), text, cb, enabled=enabled, danger=danger, px=12)
        self._py += 36

    def draw_panel(self) -> None:
        """Рисует панель свойств, подходящую выделению (узел, связь, легенда, надпись или диаграмма в целом)."""
        scr, pr = self.screen, self.panel_rect
        key = (self.kind, self.sel.kind, self.sel.id)
        if key != self._sel_key:                      # новый объект — панель прокручиваем в начало
            self._sel_key, self.panel_scroll = key, 0
        self.panel_scroll = max(0, min(self.panel_scroll, self._panel_h - pr.h + 20))
        pygame.draw.rect(scr, PANEL_BG, pr)
        pygame.draw.line(scr, BORDER, (pr.x, pr.y), (pr.x, pr.bottom))
        scr.set_clip(pr)
        self._clip = pr
        self._py = pr.y + 10 - self.panel_scroll
        top = self._py
        s = self.sel
        d = self.d
        sid = s.id or ""
        if s.kind == "node" and d.node(sid):
            self.panel_node(d.node(sid), s)  # type: ignore[arg-type]
        elif s.kind == "link" and d.link(sid):
            self.panel_link(d.link(sid))  # type: ignore[arg-type]
        elif s.kind == "legend":
            self.panel_legend(s)
        elif s.kind == "note" and d.note(sid):
            self.panel_note(d.note(sid))  # type: ignore[arg-type]
        else:
            self.panel_diagram()
        self._panel_h = self._py - top
        self._clip = None
        scr.set_clip(None)

    def panel_diagram(self) -> None:
        """Рисует панель без выделения: сведения о диаграмме, настройки вида и экспорта, общие действия."""
        d, p = self.d, self.project
        if self.kind == KIND_DATA:
            self.panel_nf()
            self.p_gap(8)
        self.p_header(KIND_TITLES[self.kind])
        self.p_note(f"{'Сущностей' if self.kind == KIND_INFO else 'Таблиц'}: {len(d.nodes)}   связей: {len(d.links)}   надписей: {len(d.notes)}")
        if p.db_path:
            self.p_note("БД: " + p.db_path, px=11)
        self.p_gap(4)
        self.p_header("Вид и экспорт")
        self.p_choice("Сетка", [("Точки и привязка", p.grid, lambda: setattr(p, "grid", True)),
                                ("Нет", not p.grid, lambda: setattr(p, "grid", False))])
        self.p_choice("Масштаб PNG", [(f"×{k}", p.export_scale == k, partial(setattr, p, "export_scale", k)) for k in (1, 2, 3)])
        self.p_buttons([("PNG (эта диаграмма)", self.export_dialog, True, False),
                        ("PNG (обе)", lambda: self.export_dialog(True), True, False)])
        self.p_gap(6)
        self.p_header("Диаграмма")
        self.p_buttons([("Авто-раскладка", self.auto_layout, bool(d.nodes), False),
                        ("Заново из БД", self.reimport, bool(p.db_path), True)])
        if self.kind == KIND_DATA:
            self.p_choice("Концы связей", [("Стрелка", d.arrows, partial(self.set_arrows, True)),
                                           ("Лапка", not d.arrows, partial(self.set_arrows, False))])
        else:
            self.p_buttons([("Построить по таблицам", self.rebuild_info_dialog, bool(p.diagrams[KIND_DATA].nodes), False)])
        self.p_gap(6)
        self.p_header("База данных")
        self.p_choice("Тест. данные", [(f"{dbgen.SAMPLE_ROWS} записей", p.fill_sample, partial(setattr, p, "fill_sample", True)),
                                       ("Нет", not p.fill_sample, partial(setattr, p, "fill_sample", False))])
        has_tables = bool(self.project.diagrams[KIND_DATA].nodes)
        self.p_buttons([("Скачать БД (.db)", self.download_db_dialog, has_tables, False),
                        ("БД и модели", self.download_bundle_dialog, has_tables, False)])
        self.p_note("База строится по даталогической диаграмме: таблицы, типы, PK, FK, UNIQUE, ON DELETE. "
                    "«БД и модели» добавляет обе диаграммы в PNG и отчёт о нормальной форме.", px=11)
        self.p_gap(10)
        self.p_header("Как редактировать", MUTED)
        for line in ("Кликните сущность, связь, легенду или надпись — справа появятся их свойства.",
                     "Двойной клик по тексту — правка прямо на холсте.",
                     "Кнопка «Связь»: нажмите на подчинённую («много») и протяните стрелку к главной.",
                     "Выделенная связь: кружки на концах — переставить, квадрат — сдвинуть излом.",
                     "F1 — краткая справка по клавишам, «О программе» — полное руководство."):
            self.p_note("• " + line, px=12)

    def panel_node(self, n: Node, s: Sel) -> None:
        """Рисует панель узла: название, цвет, дублирование / удаление, свойства выделенной строки, связи узла.

        Args:
            n (Node): выделенный узел.
            s (Sel): выделение (из него берётся выбранная строка).
        """
        d = self.d
        nid = n.id
        self.p_header("Сущность" if d.kind == KIND_INFO else "Таблица")
        self.p_field("Название", f"p.title:{nid}", n.title, lambda v: self.n_set(nid, title=v))
        x0 = self.panel_rect.x
        self.t("Цвет", x0 + 12, self._py + 13, 12, MUTED)
        for i, (head, _body) in enumerate(PALETTE):
            r = pygame.Rect(x0 + 118 + i * 34, self._py + 1, 28, 24)
            pygame.draw.rect(self.screen, hexrgb(head), r, 0, border_radius=5)
            pygame.draw.rect(self.screen, ACCENT if n.color == i else BORDER, r, 3 if n.color == i else 1, border_radius=5)
            self.reg(r, partial(self.n_set, nid, color=i))
        self._py += 34
        if d.kind == KIND_DATA:
            self.p_field("UNIQUE", f"p.uniq:{nid}", dbgen.format_uniques(n.uniques), lambda v: self.set_uniques(nid, v))
            self.p_note("Составной UNIQUE: столбцы через запятую, группы через «;». Одиночный — кнопка UNIQUE у поля.", px=11)
            self.p_field("Зависимости", f"p.fds:{nid}", normalform.format_fds(n.fds), lambda v: self.set_fds(nid, v))
            self.p_note("Функциональные зависимости между столбцами для проверки 2НФ / 3НФ: «левая -> правая», группы через «;». "
                        "Например: город -> страна.", px=11)
        self.p_buttons([("Дублировать", self.duplicate_node, True, False),
                        ("Удалить сущность", self.delete_node_whole, True, True)])
        self.p_gap(4)
        self.p_header("Поле" if d.kind == KIND_DATA else "Атрибут")
        row = n.rows[s.row] if s.row is not None and 0 <= s.row < len(n.rows) else None
        if row is None or s.row is None:
            self.p_note("Кликните строку на холсте, чтобы править поле. Кнопка ниже добавляет новое.")
        else:
            i = s.row
            self.p_field("Имя", f"p.row.name:{nid}", row.name, lambda v: self.r_set(nid, i, name=v))
            if d.kind == KIND_INFO:
                self.p_choice("Маркер", [("# UID", row.mark == "#", lambda: self.r_set(nid, i, mark="#")),
                                         ("* обяз.", row.mark == "*", lambda: self.r_set(nid, i, mark="*")),
                                         ("o необяз.", row.mark == "o", lambda: self.r_set(nid, i, mark="o"))])
            else:
                toks = dbgen.key_tokens(row.key)
                self.p_choice("Ключ", [("PK", "PK" in toks, partial(self.toggle_key, nid, i, "PK")),
                                       ("FK", "FK" in toks, partial(self.toggle_key, nid, i, "FK")),
                                       ("UNIQUE", "U" in toks, partial(self.toggle_key, nid, i, "U"))])
                self.p_field("Тип", f"p.row.type:{nid}", row.type, lambda v: self.r_set(nid, i, type=v))
                self.p_field("DEFAULT", f"p.row.default:{nid}", row.default, lambda v: self.r_set(nid, i, default=v))
                self.p_choice("Быстро", [(short, row.type.upper() == tp, partial(self.r_set, nid, i, type=tp))
                                         for short, tp in (("INT", "INTEGER"), ("TEXT", "TEXT"), ("REAL", "REAL"), ("DATE", "DATE"))])
                self.p_choice("NOT NULL", [("Да", row.notnull, lambda: self.r_set(nid, i, notnull=True)),
                                           ("Нет", not row.notnull, lambda: self.r_set(nid, i, notnull=False))])
        self.p_buttons([("+ Поле", self.add_row, True, False),
                        ("↑", lambda: self.move_row(-1), row is not None, False),
                        ("↓", lambda: self.move_row(1), row is not None, False),
                        ("− Поле", self.delete_selection, row is not None, True)])
        links = d.links_of(nid)
        if links:
            self.p_gap(6)
            self.p_header("Связи узла", MUTED)
            for l in links:
                other = d.node(l.b if l.a == nid else l.a)
                arrow = "→" if l.a == nid else "←"
                self.p_link_row(f"{arrow} {other.title if other else '?'}", l.id)
        if d.kind == KIND_DATA:
            self.panel_nf(nid)

    def p_link_row(self, text: str, lid: str) -> None:
        """Рисует кнопку-переход к связи в списке «Связи узла».

        Args:
            text (str): подпись кнопки.
            lid (str): идентификатор связи, которая выделяется по клику.
        """
        x0 = self.panel_rect.x
        r = pygame.Rect(x0 + 12, self._py, PANEL_W - 24, 24)
        self.button(r, text, lambda: setattr(self, "sel", Sel("link", lid)), px=12)
        self._py += 29

    def panel_link(self, l: Link) -> None:
        """Рисует панель связи: подписи, кратность, сплошная / пунктирная половины, черты UID, смена концов, удаление.

        Args:
            l (Link): выделенная связь.
        """
        d, lid = self.d, l.id
        a, b = d.node(l.a), d.node(l.b)
        self.p_header("Связь")
        self.p_note(f"{a.title if a else '?'}  →  {b.title if b else '?'}", INK, 13)
        if d.kind == KIND_INFO:
            self.p_field("Имя у A", f"p.na:{lid}", l.name_a, lambda v: self.l_set(lid, name_a=v))
            self.p_field("Имя у B", f"p.nb:{lid}", l.name_b, lambda v: self.l_set(lid, name_b=v))
        else:
            rule = dbgen._on_delete(l.label)
            opts: list[tuple[str, bool, Callback]] = [(r, rule == r, partial(self.l_set, lid, label=r)) for r in dbgen.ON_DELETE_RULES]
            self.p_choice("ON DELETE", opts[:2])
            self.p_choice("", opts[2:])
            self.p_note("CASCADE — удалить и дочерние записи; RESTRICT / NO ACTION — запретить удаление родителя; "
                        "SET NULL — записать NULL в ключ; SET DEFAULT — записать DEFAULT столбца.", px=11)
            hint = dbgen.link_rule_hint(a, l) if a else ""
            if hint:
                self.p_note("⚠ " + hint, DANGER, 11)
            self.p_field("Подпись", f"p.label:{lid}", l.label, lambda v: self.l_set(lid, label=v))
        self.p_note("A — подчинённая сторона (где «лапка» / внешний ключ), B — главная.", px=11)
        self.p_choice("Кратность A", [("Один", l.card_a == "1", lambda: self.l_set(lid, card_a="1")),
                                      ("Много (лапка)", l.card_a == "M", lambda: self.l_set(lid, card_a="M"))])
        self.p_choice("Кратность B", [("Один", l.card_b == "1", lambda: self.l_set(lid, card_b="1")),
                                      ("Много (лапка)", l.card_b == "M", lambda: self.l_set(lid, card_b="M"))])
        self.p_choice("Половина у A", [("Сплошная", l.must_a, lambda: self.l_set(lid, must_a=True)),
                                       ("Пунктир", not l.must_a, lambda: self.l_set(lid, must_a=False))])
        self.p_choice("Половина у B", [("Сплошная", l.must_b, lambda: self.l_set(lid, must_b=True)),
                                       ("Пунктир", not l.must_b, lambda: self.l_set(lid, must_b=False))])
        if d.kind == KIND_INFO:
            self.p_choice("Черта UID у A", [("Есть", l.bar_a, lambda: self.l_set(lid, bar_a=True)),
                                            ("Нет", not l.bar_a, lambda: self.l_set(lid, bar_a=False))])
            self.p_choice("Черта UID у B", [("Есть", l.bar_b, lambda: self.l_set(lid, bar_b=True)),
                                            ("Нет", not l.bar_b, lambda: self.l_set(lid, bar_b=False))])
        self.p_gap(4)
        self.p_buttons([("Поменять A↔B", lambda: self.swap_link(lid), True, False),
                        ("Авто-маршрут", lambda: self.reroute_link(lid), True, False)])
        self.p_buttons([("Удалить связь", self.delete_selection, True, True)])
        self.p_note("На холсте: кружки — переставить конец, квадрат — сдвинуть излом, подписи тянутся мышью.", px=11)

    def panel_note(self, note: Note) -> None:
        """Рисует панель надписи: текст, размер шрифта, удаление.

        Args:
            note (Note): выделенная надпись.
        """
        nid = note.id
        self.p_header("Надпись")
        self.p_field("Текст", f"p.note:{nid}", note.text, lambda v: self.note_set(nid, text=v))
        self.p_choice("Размер", [("−", False, lambda: self.note_set(nid, size=max(8, note.size - 2))),
                                 (str(note.size), False, lambda: None),
                                 ("+", False, lambda: self.note_set(nid, size=min(60, note.size + 2)))])
        self.p_buttons([("Удалить надпись", self.delete_selection, True, True)])

    def panel_legend(self, s: Sel) -> None:
        """Рисует панель легенды: заголовок, видимость, свойства выделенного пункта, добавление / порядок / удаление, сброс.

        Args:
            s (Sel): выделение (из него берётся выбранный пункт).
        """
        d = self.d
        lg = d.legend
        self.p_header("Легенда")
        self.p_field("Заголовок", "p.lg.title", lg.title, lambda v: self.legend_set(title=v))
        self.p_choice("Показывать", [("Да", lg.visible, lambda: self.legend_set(visible=True)),
                                     ("Нет", not lg.visible, lambda: self.legend_set(visible=False))])
        self.p_gap(2)
        self.p_header("Пункт")
        i = s.row if s.row is not None and 0 <= s.row < len(lg.items) else None
        if i is None:
            self.p_note("Кликните пункт на холсте, чтобы править его текст и значок.")
        else:
            it = lg.items[i]
            self.p_field("Текст", f"p.it.text:{i}", it.text, lambda v: self.item_set(i, text=v))
            kinds = list(LEGEND_KINDS)
            nxt = kinds[(kinds.index(it.kind) + 1) % len(kinds)] if it.kind in kinds else kinds[0]
            x0 = self.panel_rect.x
            self.t("Значок", x0 + 12, self._py + 13, 12, MUTED)
            self.button(pygame.Rect(x0 + 118, self._py, PANEL_W - 130, 26), LEGEND_KINDS.get(it.kind, it.kind) + "  ›",
                        lambda: self.item_set(i, kind=nxt), tip="Сменить вид значка", px=12)
            self._py += 33
        self.p_buttons([("+ Пункт", self.add_legend_item, True, False),
                        ("↑", lambda: self.move_item(-1), i is not None, False),
                        ("↓", lambda: self.move_item(1), i is not None, False),
                        ("Удалить", self.delete_selection, i is not None, True)])
        self.p_buttons([("Сбросить легенду", self.reset_legend, True, False)])
        self.p_note("Ширина легенды — перетаскиванием правого края; положение — перетаскиванием.", px=11)

    # ───────────────────────── главный цикл ─────────────────────────

    def run(self) -> None:
        """Главный цикл: события → завершённые диалоги → перерисовка только при изменениях (не чаще 60 раз в секунду).

        Цикл работает, пока ``self.running`` истинно; по завершении закрывает pygame.
        """
        last_blink: Optional[int] = None
        while self.running:
            for ev in pygame.event.get():
                self.handle(ev)
                self.dirty = True
            for job in list(self.jobs):
                if job.done:
                    self.jobs.remove(job)
                    job.callback(job.result)
                    self.dirty = True
            blink = (pygame.time.get_ticks() // 500) % 2
            if self.edit and blink != last_blink:
                self.dirty = True
            last_blink = blink
            if self.toast_msg and time.time() < self.toast_until + 0.5:
                self.dirty = True
            if self.dirty:
                self.draw()
                pygame.display.flip()
                self.dirty = False
            self.clock.tick(60)
        pygame.quit()


def main(argv: Optional[list[str]] = None) -> None:
    """Точка входа программы: ``python -m er_editor [база.db | проект.erd.json]``.

    Args:
        argv (Optional[list[str]]): аргументы командной строки без имени программы;
            ``None`` — взять из ``sys.argv``. Первый аргумент — файл, открываемый при старте.

    Raises:
        pygame.error: если не удалось создать окно (нет графической подсистемы).
    """
    argv = sys.argv[1:] if argv is None else argv
    App(argv[0] if argv else None).run()

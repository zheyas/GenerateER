"""HTTP-сервер веб-версии редактора (только стандартная библиотека + код пакета).

Сервер **не хранит состояние**: проект живёт в браузере и присылается в запросах. Он нужен для
того, что уже реализовано на Python и не должно дублироваться в JavaScript.

=========================  =====================================================================
Запрос                     Что делает
=========================  =====================================================================
``GET  /api/meta``         версия, названия и требования нормальных форм, число случайных записей
``GET  /api/new``          пустой проект
``POST /api/import``       тело — файл SQLite; ответ — проект с раскладкой (``?name=``)
``POST /api/check``        ``{diagram, level}`` → отчёт о нормальной форме
``POST /api/layout``       ``{diagram}`` → диаграмма с автораскладкой
``POST /api/route``        ``{diagram, ids}`` → стороны и положения концов выбранных связей
``POST /api/info``         ``{diagram}`` (даталогическая) → инфологическая диаграмма
``POST /api/fds``          тело — файл SQLite → кандидаты в функциональные зависимости по данным
``POST /api/describe``     ``{text}`` (SQL или текст) → проект с обеими диаграммами и пояснения
``POST /api/database``     ``{project, sample}`` → файл ``.db``
``POST /api/bundle``       ``{project, sample}`` → ZIP: база, обе модели в PNG, отчёт ``.md``
``POST /api/png``          ``{diagram, scale}`` → PNG
``GET  /api/readme``       руководство (README.md) в виде HTML
=========================  =====================================================================

Ошибки возвращаются как JSON ``{"error": "...", ...}`` с кодом 400 (плохой запрос), 413
(слишком большое тело), 422 (схему нельзя превратить в базу: ``problems``) или 500.
"""
from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import traceback
import webbrowser
import zipfile
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, quote, urlsplit

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402

from .. import __version__, dbgen, fdmine, describe, layout, markdown, normalform, schema  # noqa: E402
from ..model import KIND_DATA, KIND_INFO, LEGEND_KINDS, Diagram, Project, default_legend, empty_project  # noqa: E402
from ..render import Fonts, export_png  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
README_PATH = Path(__file__).resolve().parents[2] / "README.md"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
MAX_BODY = 64 * 1024 * 1024                    # наибольшее тело запроса (файл базы данных), байт
LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}
_PYGAME_LOCK = threading.Lock()                # pygame не потокобезопасен: вся работа со шрифтами и PNG — под замком
_fonts: Optional[Fonts] = None


class ApiError(Exception):
    """Ошибка запроса, которую нужно вернуть клиенту как JSON.

    Attributes:
        status (int): HTTP-код ответа.
        message (str): понятное пользователю описание.
        extra (dict[str, Any]): дополнительные поля ответа (например, ``problems``).
    """

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        """Создаёт исключение.

        Args:
            status (int): HTTP-код ответа.
            message (str): описание ошибки.
            **extra (Any): дополнительные поля JSON-ответа.
        """
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


@dataclass
class Request:
    """Разобранный запрос.

    Attributes:
        path (str): путь без строки запроса.
        query (dict[str, str]): параметры строки запроса.
        body (bytes): тело запроса.
    """

    path: str
    query: dict[str, str]
    body: bytes = b""


@dataclass
class Response:
    """Ответ сервера.

    Attributes:
        body (bytes): тело ответа.
        content_type (str): значение заголовка ``Content-Type``.
        status (int): HTTP-код.
        filename (str): если не пусто — ответ скачивается как файл с этим именем.
        headers (dict[str, str]): дополнительные заголовки.
    """

    body: bytes
    content_type: str = "application/json; charset=utf-8"
    status: int = 200
    filename: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def as_json(obj: Any, status: int = 200) -> Response:
    """Упаковывает объект в JSON-ответ.

    Args:
        obj (Any): сериализуемый в JSON объект.
        status (int): HTTP-код.

    Returns:
        Response: ответ с телом в UTF-8 без экранирования кириллицы.
    """
    return Response(json.dumps(obj, ensure_ascii=False).encode("utf-8"), status=status)


def fonts() -> Fonts:
    """Возвращает общий кэш шрифтов (создаётся при первом обращении). Вызывать под ``_PYGAME_LOCK``.

    Returns:
        Fonts: кэш шрифтов pygame.

    Raises:
        pygame.error: если не удалось инициализировать шрифты.
    """
    global _fonts
    if _fonts is None:
        pygame.font.init()
        _fonts = Fonts()
    return _fonts


def body_json(req: Request) -> dict[str, Any]:
    """Разбирает тело запроса как JSON-объект.

    Args:
        req (Request): запрос.

    Returns:
        dict[str, Any]: словарь из тела запроса.

    Raises:
        ApiError: если тело — не корректный JSON или не объект.
    """
    try:
        data = json.loads(req.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, f"Тело запроса не является корректным JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "Ожидался JSON-объект")
    return data


def read_diagram(data: dict[str, Any], key: str = "diagram", kind: Optional[str] = None) -> Diagram:
    """Достаёт из тела запроса диаграмму и проверяет её вид.

    Args:
        data (dict[str, Any]): тело запроса.
        key (str): ключ диаграммы.
        kind (Optional[str]): ожидаемый вид (``KIND_DATA`` / ``KIND_INFO``); ``None`` — любой.

    Returns:
        Diagram: диаграмма.

    Raises:
        ApiError: если диаграммы нет, она повреждена или имеет другой вид.
    """
    raw = data.get(key)
    if not isinstance(raw, dict):
        raise ApiError(400, f"В запросе нет поля «{key}» с диаграммой")
    try:
        d = Diagram.from_dict(raw)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ApiError(400, f"Диаграмма повреждена: {exc!r}") from exc
    if kind and d.kind != kind:
        raise ApiError(400, f"Ожидалась диаграмма вида «{kind}», получена «{d.kind}»")
    return d


def read_project(data: dict[str, Any]) -> Project:
    """Достаёт из тела запроса проект.

    Args:
        data (dict[str, Any]): тело запроса (поле ``project``).

    Returns:
        Project: проект с обеими диаграммами.

    Raises:
        ApiError: если проекта нет, он повреждён или в нём нет одной из диаграмм.
    """
    raw = data.get("project")
    if not isinstance(raw, dict):
        raise ApiError(400, "В запросе нет поля «project»")
    try:
        project = Project.from_dict(raw)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ApiError(400, f"Проект повреждён: {exc!r}") from exc
    if KIND_DATA not in project.diagrams or KIND_INFO not in project.diagrams:
        raise ApiError(400, "В проекте должны быть обе диаграммы: «info» и «data»")
    return project


def safe_name(name: str, default: str = "schema") -> str:
    """Превращает имя из запроса в безопасное имя файла без каталогов и расширения.

    Args:
        name (str): имя, присланное клиентом (может быть пустым или с путём).
        default (str): имя на случай, если ничего подходящего не осталось.

    Returns:
        str: имя из букв, цифр, ``_``, ``-`` и ``.`` длиной до 60 символов, без расширения ``.db`` / ``.json``.
    """
    base = os.path.basename(name.replace("\\", "/"))
    base = re.sub(r"\.(db|sqlite3?|erd\.json|json)$", "", base, flags=re.I)
    base = re.sub(r"[^\w.-]+", "_", base, flags=re.UNICODE).strip("._")
    return base[:60] or default


def tables_of(d: Diagram) -> list[schema.Table]:
    """Превращает даталогическую диаграмму в таблицы, а проблемы схемы — в ответ 422.

    Args:
        d (Diagram): даталогическая диаграмма.

    Returns:
        list[schema.Table]: таблицы схемы.

    Raises:
        ApiError: если в диаграмме нет таблиц или схему нельзя превратить в базу данных.
    """
    if not d.nodes:
        raise ApiError(422, "Нет ни одной таблицы: сначала нарисуйте схему", problems=["Нет ни одной таблицы"])
    try:
        return dbgen.diagram_to_tables(d)
    except dbgen.SchemaError as exc:
        raise ApiError(422, "Схему нельзя превратить в базу данных", problems=exc.problems) from exc


# ───────────────────────── обработчики API ─────────────────────────

def api_meta(req: Request) -> Response:
    """Сведения для интерфейса: версия, нормальные формы, число случайных записей.

    Args:
        req (Request): запрос (не используется).

    Returns:
        Response: JSON ``{version, sample_rows, levels: {"1": {name, rule}, …}, legend_kinds, legends, examples}``;
        ``legends`` — стандартные легенды: ``info``, ``data_arrows``, ``data_crow``; ``examples`` — образцы
        описания (``text`` и ``sql``).
    """
    levels = {str(k): {"name": normalform.LEVEL_NAMES[k], "rule": normalform.LEVEL_RULES[k]} for k in normalform.LEVELS}
    legends = {"info": asdict(default_legend(KIND_INFO)), "data_arrows": asdict(default_legend(KIND_DATA, True)),
               "data_crow": asdict(default_legend(KIND_DATA, False))}
    return as_json({"version": __version__, "sample_rows": dbgen.SAMPLE_ROWS, "levels": levels,
                    "legend_kinds": LEGEND_KINDS, "legends": legends,
                    "examples": {"text": describe.EXAMPLE_TEXT, "sql": describe.EXAMPLE_SQL, "prose": describe.EXAMPLE_PROSE}})


def api_new(req: Request) -> Response:
    """Создаёт пустой проект.

    Args:
        req (Request): запрос (не используется).

    Returns:
        Response: JSON проекта в формате ``Project.to_json``.
    """
    return Response(empty_project().to_json().encode("utf-8"))


def api_import(req: Request) -> Response:
    """Читает присланный файл SQLite и строит проект: обе диаграммы с раскладкой.

    Args:
        req (Request): тело — байты файла базы данных, параметр ``name`` — исходное имя файла.

    Returns:
        Response: JSON проекта; в ``db_path`` записано имя файла.

    Raises:
        ApiError: если тело пустое или файл не читается как база данных SQLite.
    """
    if not req.body:
        raise ApiError(400, "Файл пустой")
    name = os.path.basename(req.query.get("name", "") or "database.db")
    with tempfile.TemporaryDirectory(prefix="er_import_") as tmp:
        path = os.path.join(tmp, "in.db")
        with open(path, "wb") as fh:
            fh.write(req.body)
        try:
            with _PYGAME_LOCK:
                project = schema.build_project(path, fonts().measure)
        except sqlite3.Error as exc:
            raise ApiError(400, f"Файл не читается как база данных SQLite: {exc}") from exc
    project.db_path = name
    return Response(project.to_json().encode("utf-8"))


def api_fds(req: Request) -> Response:
    """Ищет по данным присланной базы SQLite кандидатов в функциональные зависимости (для поля «Зависимости»).

    Args:
        req (Request): тело — байты файла базы данных.

    Returns:
        Response: JSON ``{tables: [{name, rows, note, fds: [{lhs, rhs, text}]}]}``; базу сервер читает во временной копии и удаляет.

    Raises:
        ApiError: если тело пустое или файл не читается как база данных SQLite.
    """
    if not req.body:
        raise ApiError(400, "Файл пустой")
    with tempfile.TemporaryDirectory(prefix="er_fds_") as tmp:
        path = os.path.join(tmp, "in.db")
        with open(path, "wb") as fh:
            fh.write(req.body)
        try:
            tables = fdmine.discover(path)
        except sqlite3.Error as exc:
            raise ApiError(400, f"Файл не читается как база данных SQLite: {exc}") from exc
    return as_json({"tables": fdmine.as_json(tables)})


def api_check(req: Request) -> Response:
    """Проверяет даталогическую диаграмму на нормальную форму.

    Args:
        req (Request): тело — ``{diagram, level}``.

    Returns:
        Response: JSON отчёта (:meth:`er_editor.normalform.Report.to_dict`).

    Raises:
        ApiError: если диаграмма повреждена или форма не 1–4.
    """
    data = body_json(req)
    d = read_diagram(data, kind=KIND_DATA)
    level = data.get("level", 3)
    if not isinstance(level, int) or level not in normalform.LEVELS:
        raise ApiError(400, "Нормальная форма должна быть числом 1–4")
    return as_json(normalform.check(d, level).to_dict())


def api_layout(req: Request) -> Response:
    """Подгоняет ширину узлов и расставляет узлы и маршруты связей (автораскладка).

    Args:
        req (Request): тело — ``{diagram}``.

    Returns:
        Response: JSON ``{diagram}`` с новыми координатами.

    Raises:
        ApiError: если диаграмма повреждена.
    """
    d = read_diagram(body_json(req))
    with _PYGAME_LOCK:
        layout.fit_widths(d, fonts().measure, grow_only=True)
        layout.auto_layout(d)
    return as_json({"diagram": d.to_dict()})


def api_route(req: Request) -> Response:
    """Подбирает маршруты выбранных связей.

    Args:
        req (Request): тело — ``{diagram, ids}``; ``ids`` — идентификаторы связей (пусто — все).

    Returns:
        Response: JSON ``{links: {id: {sa, ta, sb, tb, mid}}}``.

    Raises:
        ApiError: если диаграмма повреждена или ``ids`` — не список строк.
    """
    data = body_json(req)
    d = read_diagram(data)
    ids = data.get("ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ApiError(400, "Поле «ids» должно быть списком идентификаторов связей")
    only = [l for l in d.links if not ids or l.id in ids]
    with _PYGAME_LOCK:
        layout.auto_route(d, only)
    return as_json({"links": {l.id: {"sa": l.sa, "ta": l.ta, "sb": l.sb, "tb": l.tb, "mid": l.mid} for l in only}})


def build_info_diagram(data_diagram: Diagram) -> Diagram:
    """Строит инфологическую диаграмму по даталогической с подбором ширины и раскладкой.

    Args:
        data_diagram (Diagram): даталогическая диаграмма.

    Returns:
        Diagram: инфологическая диаграмма (Баркер).

    Raises:
        ApiError: если схему нельзя превратить в таблицы.
    """
    info = schema.build_info(tables_of(data_diagram))
    with _PYGAME_LOCK:
        layout.fit_widths(info, fonts().measure)
        layout.auto_layout(info)
    return info


def api_info(req: Request) -> Response:
    """Строит инфологическую диаграмму по нарисованным таблицам.

    Args:
        req (Request): тело — ``{diagram}`` (даталогическая).

    Returns:
        Response: JSON ``{diagram}`` — инфологическая диаграмма.

    Raises:
        ApiError: если диаграмма повреждена или схему нельзя превратить в таблицы.
    """
    return as_json({"diagram": build_info_diagram(read_diagram(body_json(req), kind=KIND_DATA)).to_dict()})


def api_describe(req: Request) -> Response:
    """Строит проект (обе диаграммы с раскладкой) по описанию: SQL или короткий текст.

    Args:
        req (Request): тело — ``{text}``.

    Returns:
        Response: JSON ``{project, notes, format, tables, links}``; ``notes`` — как поняты фразы и что пропущено.

    Raises:
        ApiError: 400, если нет поля ``text``; 422 (со списком ``problems``), если описание не удалось разобрать.
    """
    text = body_json(req).get("text")
    if not isinstance(text, str):
        raise ApiError(400, "В запросе нет поля «text» с описанием")
    try:
        res = describe.describe(text)
    except describe.DescribeError as exc:
        raise ApiError(422, "Описание не удалось превратить в схему", problems=exc.problems) from exc
    with _PYGAME_LOCK:
        project = schema.project_from_tables("", res.tables, fonts().measure)
    return as_json({"project": json.loads(project.to_json()), "notes": res.notes, "format": res.fmt,
                    "tables": len(res.tables), "links": len(project.diagrams[KIND_DATA].links)})


def make_database(project: Project, sample: bool) -> tuple[bytes, dbgen.DbReport]:
    """Создаёт базу данных SQLite по даталогической диаграмме проекта.

    Args:
        project (Project): проект.
        sample (bool): добавлять ли в каждую таблицу случайные записи.

    Returns:
        tuple[bytes, dbgen.DbReport]: содержимое файла ``.db`` и отчёт о создании.

    Raises:
        ApiError: если схему нельзя превратить в базу данных или не удалось создать файл.
    """
    tables = tables_of(project.diagrams[KIND_DATA])
    with tempfile.TemporaryDirectory(prefix="er_db_") as tmp:
        path = os.path.join(tmp, "out.db")
        try:
            report = dbgen.create_database(path, tables, dbgen.SAMPLE_ROWS if sample else 0)
        except (sqlite3.Error, OSError) as exc:
            raise ApiError(500, f"Не удалось создать базу данных: {exc}") from exc
        with open(path, "rb") as fh:
            return fh.read(), report


def report_header(report: dbgen.DbReport) -> str:
    """Записывает отчёт о создании базы в значение HTTP-заголовка (процентное кодирование JSON).

    Args:
        report (dbgen.DbReport): отчёт.

    Returns:
        str: ASCII-строка; браузер разбирает её через ``decodeURIComponent`` и ``JSON.parse``.
    """
    return quote(json.dumps({"tables": report.tables, "rows": report.total_rows, "warnings": report.warnings},
                            ensure_ascii=False))


def api_database(req: Request) -> Response:
    """Создаёт и отдаёт файл базы данных SQLite.

    Args:
        req (Request): тело — ``{project, sample}``; параметр ``name`` — имя файла.

    Returns:
        Response: байты ``.db`` с заголовком ``X-Db-Report``.

    Raises:
        ApiError: если проект повреждён или схему нельзя превратить в базу данных.
    """
    data = body_json(req)
    project = read_project(data)
    content, report = make_database(project, bool(data.get("sample")))
    name = safe_name(req.query.get("name", ""))
    return Response(content, "application/x-sqlite3", filename=f"{name}.db", headers={"X-Db-Report": report_header(report)})


def render_png(d: Diagram, scale: int) -> bytes:
    """Рисует диаграмму в PNG тем же кодом, что и настольная программа.

    Args:
        d (Diagram): диаграмма.
        scale (int): множитель разрешения (1–3).

    Returns:
        bytes: содержимое PNG-файла.

    Raises:
        ApiError: если картинку не удалось нарисовать или сохранить.
    """
    with tempfile.TemporaryDirectory(prefix="er_png_") as tmp:
        path = os.path.join(tmp, "out.png")
        try:
            with _PYGAME_LOCK:
                export_png(d, fonts(), path, float(max(1, min(3, scale))))
        except (pygame.error, OSError, KeyError) as exc:
            raise ApiError(500, f"Не удалось нарисовать диаграмму: {exc}") from exc
        with open(path, "rb") as fh:
            return fh.read()


def api_png(req: Request) -> Response:
    """Экспортирует диаграмму в PNG.

    Args:
        req (Request): тело — ``{diagram, scale}``; параметр ``name`` — имя файла.

    Returns:
        Response: байты PNG.

    Raises:
        ApiError: если диаграмма повреждена или не нарисовалась.
    """
    data = body_json(req)
    d = read_diagram(data)
    scale = data.get("scale", 2)
    png = render_png(d, scale if isinstance(scale, int) else 2)
    suffix = "infological" if d.kind == KIND_INFO else "datalogical"
    return Response(png, "image/png", filename=f"{safe_name(req.query.get('name', ''))}_{suffix}.png")


def api_bundle(req: Request) -> Response:
    """Собирает ZIP-комплект: база данных, обе модели в PNG и отчёт о нормальной форме.

    Если инфологическая диаграмма пуста, она строится по таблицам.

    Args:
        req (Request): тело — ``{project, sample}``; параметр ``name`` — основа имён файлов.

    Returns:
        Response: байты ZIP-архива.

    Raises:
        ApiError: если проект повреждён или схему нельзя превратить в базу данных.
    """
    data = body_json(req)
    project = read_project(data)
    name = safe_name(req.query.get("name", ""))
    content, report = make_database(project, bool(data.get("sample")))
    if not project.diagrams[KIND_INFO].nodes:
        project.diagrams[KIND_INFO] = build_info_diagram(project.diagrams[KIND_DATA])
    level = project.normal_form if project.normal_form in normalform.LEVELS else 3
    nf_text = normalform.format_markdown(normalform.check(project.diagrams[KIND_DATA], level), name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}.db", content)
        zf.writestr(f"{name}_infological.png", render_png(project.diagrams[KIND_INFO], project.export_scale))
        zf.writestr(f"{name}_datalogical.png", render_png(project.diagrams[KIND_DATA], project.export_scale))
        zf.writestr(f"{name}_normal_form.md", nf_text)
    return Response(buf.getvalue(), "application/zip", filename=f"{name}_bundle.zip",
                    headers={"X-Db-Report": report_header(report)})


def markdown_html(md: str) -> str:
    """Переводит Markdown (подмножество мини-рендерера программы) в HTML.

    Args:
        md (str): текст документа.

    Returns:
        str: фрагмент HTML: заголовки, абзацы, списки, цитаты, блоки кода, таблицы.
    """
    def inline(text: str) -> str:
        """Переводит встроенную разметку строки в HTML.

        Args:
            text (str): строка с ``**жирным**``, ``*курсивом*``, кодом и ссылками.

        Returns:
            str: HTML-фрагмент с экранированным текстом.
        """
        tags = {"b": "strong", "i": "em", "code": "code", "link": "span"}
        out = []
        for t, style in markdown.parse_inline(text):
            tag = tags.get(style)
            out.append(f"<{tag}>{html.escape(t)}</{tag}>" if tag else html.escape(t))
        return "".join(out)

    out: list[str] = []
    for b in markdown.parse(md):
        if b.kind == "h":
            out.append(f"<h{b.level}>{inline(b.text)}</h{b.level}>")
        elif b.kind == "p":
            out.append(f"<p>{inline(b.text)}</p>")
        elif b.kind == "li":
            out.append(f'<div class="li" style="margin-left:{b.level * 22}px"><span class="mk">{html.escape(b.marker)}</span>'
                       f"<span>{inline(b.text)}</span></div>")
        elif b.kind == "quote":
            out.append(f"<blockquote>{inline(b.text)}</blockquote>")
        elif b.kind == "code":
            out.append("<pre>" + html.escape("\n".join(b.lines)) + "</pre>")
        elif b.kind == "hr":
            out.append("<hr>")
        elif b.kind == "table" and b.rows:
            head = "".join(f"<th>{inline(c)}</th>" for c in b.rows[0])
            rows = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in b.rows[1:])
            out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
    return "\n".join(out)


def api_readme(req: Request) -> Response:
    """Отдаёт руководство (README.md) в виде HTML для окна «О программе».

    Args:
        req (Request): запрос (не используется).

    Returns:
        Response: JSON ``{html}``; если README не найден — краткая справка.
    """
    try:
        text = README_PATH.read_text(encoding="utf-8")
    except OSError:
        text = "# ER Editor\n\nФайл README.md не найден рядом с программой."
    return as_json({"html": markdown_html(text)})


ApiHandler = Callable[[Request], Response]
ROUTES: dict[tuple[str, str], ApiHandler] = {
    ("GET", "/api/meta"): api_meta,
    ("GET", "/api/new"): api_new,
    ("POST", "/api/import"): api_import,
    ("POST", "/api/check"): api_check,
    ("POST", "/api/layout"): api_layout,
    ("POST", "/api/route"): api_route,
    ("POST", "/api/info"): api_info,
    ("POST", "/api/describe"): api_describe,
    ("POST", "/api/fds"): api_fds,
    ("POST", "/api/database"): api_database,
    ("POST", "/api/bundle"): api_bundle,
    ("POST", "/api/png"): api_png,
    ("GET", "/api/readme"): api_readme,
}


def static_response(path: str) -> Response:
    """Отдаёт файл страницы из каталога ``static`` (только простые имена, без подкаталогов).

    Args:
        path (str): путь запроса (``/`` — главная страница).

    Returns:
        Response: содержимое файла.

    Raises:
        ApiError: 404, если такого файла нет.
    """
    name = "index.html" if path in ("", "/") else path.lstrip("/")
    f = STATIC_DIR / name
    if not re.fullmatch(r"[\w.-]+", name) or f.suffix not in STATIC_TYPES or not f.is_file():
        raise ApiError(404, "Не найдено")
    return Response(f.read_bytes(), STATIC_TYPES[f.suffix], headers={"Cache-Control": "no-store"})


class RequestHandler(BaseHTTPRequestHandler):
    """Обработчик HTTP-запросов: маршрутизация на API и статические файлы."""

    server_version = "ERWeb/1.0"
    quiet = True

    def log_message(self, format: str, *args: Any) -> None:
        """Печатает строку журнала запросов (по умолчанию журнал выключен).

        Args:
            format (str): шаблон строки.
            *args (Any): значения шаблона.
        """
        if not self.quiet:
            super().log_message(format, *args)

    def host_ok(self) -> bool:
        """Проверяет заголовок ``Host``, если сервер слушает только локальный адрес (защита от DNS rebinding).

        Returns:
            bool: ``True``, если запрос адресован ``localhost`` / ``127.0.0.1`` / ``::1`` или сервер открыт сети.
        """
        addr = self.server.server_address
        if (str(addr[0]) if isinstance(addr, tuple) else str(addr)) not in ("127.0.0.1", "::1", "localhost"):
            return True
        raw = self.headers.get("Host") or ""
        host = raw[1:raw.index("]")] if raw.startswith("[") and "]" in raw else raw.rsplit(":", 1)[0]
        return host in LOOPBACK

    def send(self, resp: Response) -> None:
        """Отправляет ответ клиенту.

        Args:
            resp (Response): подготовленный ответ.
        """
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        self.send_header("Cache-Control", "no-store")
        if resp.filename:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(resp.filename)}")
        for k, v in resp.headers.items():
            self.send_header(k, v)
        if resp.filename or resp.headers:
            self.send_header("Access-Control-Expose-Headers", "X-Db-Report, Content-Disposition")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp.body)

    def dispatch(self, method: str) -> None:
        """Находит обработчик запроса и отправляет ответ; ошибки превращает в JSON.

        Args:
            method (str): ``"GET"`` или ``"POST"``.

        Raises:
            ApiError: поднимается внутри (неизвестный адрес, большое тело, нет страницы), но наружу не выходит:
                превращается в JSON-ответ с кодом ошибки.
        """
        try:
            if not self.host_ok():
                raise ApiError(403, "Запрос с неизвестного адреса")
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            body = b""
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise ApiError(413, f"Слишком большой запрос (больше {MAX_BODY // 1024 // 1024} МБ)")
                body = self.rfile.read(length) if length > 0 else b""
            req = Request(url.path, query, body)
            handler = ROUTES.get((method, url.path))
            if handler is not None:
                self.send(handler(req))
            elif method == "GET" and not url.path.startswith("/api/"):
                self.send(static_response(url.path))
            else:
                raise ApiError(404, "Не найдено")
        except ApiError as exc:
            self.send(as_json({"error": exc.message, **exc.extra}, exc.status))
        except Exception as exc:                          # noqa: BLE001
            traceback.print_exc()
            self.send(as_json({"error": f"Внутренняя ошибка сервера: {exc}"}, 500))

    def do_GET(self) -> None:
        """Обрабатывает GET-запрос (страница и справочные данные)."""
        self.dispatch("GET")

    def do_POST(self) -> None:
        """Обрабатывает POST-запрос (операции над проектом)."""
        self.dispatch("POST")


def make_server(host: str = "127.0.0.1", port: int = 8000, tries: int = 20) -> ThreadingHTTPServer:
    """Создаёт сервер; если порт занят, пробует следующие.

    Args:
        host (str): адрес прослушивания (по умолчанию только локальный).
        port (int): желаемый порт.
        tries (int): сколько подряд идущих портов попробовать.

    Returns:
        ThreadingHTTPServer: готовый к работе сервер.

    Raises:
        OSError: если ни один из портов не удалось занять.
    """
    last: Optional[OSError] = None
    for p in range(port, port + max(1, tries)):
        try:
            return ThreadingHTTPServer((host, p), RequestHandler)
        except OSError as exc:
            last = exc
    raise OSError(f"Не удалось занять порты {port}–{port + tries - 1}: {last}")


def main(argv: Optional[list[str]] = None) -> None:
    """Точка входа: ``python run_web.py [--host H] [--port N] [--no-browser]``.

    Args:
        argv (Optional[list[str]]): аргументы командной строки без имени программы;
            ``None`` — взять из ``sys.argv``.

    Raises:
        SystemExit: при неверных аргументах или если не удалось занять порт.
    """
    ap = argparse.ArgumentParser(prog="er_editor.web", description="Веб-версия ER Editor")
    ap.add_argument("--host", default="127.0.0.1", help="адрес (по умолчанию 127.0.0.1 — только этот компьютер)")
    ap.add_argument("--port", type=int, default=8000, help="порт (по умолчанию 8000)")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    ap.add_argument("-v", "--verbose", action="store_true", help="печатать журнал запросов")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        server = make_server(args.host, args.port)
    except OSError as exc:
        raise SystemExit(str(exc)) from exc
    RequestHandler.quiet = not args.verbose
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '::1') else args.host}:{server.server_address[1]}/"
    print(f"ER Editor (веб-версия) работает: {url}\nОстановить: Ctrl+C")
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")
    finally:
        server.server_close()

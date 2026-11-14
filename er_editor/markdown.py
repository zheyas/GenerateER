"""Мини-рендерер Markdown на pygame — для окна «О программе», которое показывает README.md.

Поддерживаемое подмножество Markdown:

* заголовки ``#`` … ``####``, абзацы, горизонтальная линия ``---``;
* маркированные и нумерованные списки (вложенность по отступу), цитаты ``>``;
* блоки кода в ограждении из трёх обратных кавычек;
* простые таблицы ``| a | b |`` (первая строка — заголовок);
* в тексте: ``**жирный**``, ``*курсив*``, ``код`` в обратных кавычках, ``[ссылка](url)`` (показывается подписью).

Конвейер: :func:`parse` → список :class:`Block` → :class:`MarkdownView` раскладывает блоки по
ширине в список команд рисования и рисует их на одной «длинной» поверхности, которую окно
прокручивает.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import pygame

from .render import ACCENT, GRID_COL, INK, MUTED, WHITE, Color, Fonts

Span = tuple[str, str]            # (текст, стиль): "" обычный, "b" жирный, "i" курсив, "code" код, "link" ссылка
Cmd = tuple[Any, ...]             # команда рисования: ("text"|"rect"|"line", …)

BODY_PX, LINE_H = 15, 23          # основной текст: размер шрифта и высота строки
CODE_PX, CODE_LINE = 13, 17       # блоки кода
TABLE_PX, TABLE_LINE = 14, 21     # таблицы

H_SIZES = {1: 28, 2: 21, 3: 17, 4: 15}      # размеры шрифта заголовков
H_BEFORE = {1: 4, 2: 24, 3: 16, 4: 12}      # отступ перед заголовком
H_AFTER = {1: 12, 2: 8, 3: 6, 4: 4}         # отступ после заголовка

CODE_BG: Color = (237, 240, 244)            # фон встроенного кода
CODE_BLOCK_BG: Color = (246, 248, 250)      # фон блока кода
CODE_BLOCK_BORDER: Color = (225, 229, 235)
TABLE_HEAD_BG: Color = (240, 243, 247)
QUOTE_BAR: Color = (190, 200, 214)

_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_HEAD_RE = re.compile(r"^(#{1,4})\s+(.*?)\s*#*$")
_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")
_INLINE_RE = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|(?<![\w*])\*[^*\s][^*]*\*(?![\w*])|\[[^\]]+\]\([^)]+\))")


@dataclass
class Block:
    """Блок документа.

    Attributes:
        kind (str): ``"h"`` заголовок, ``"p"`` абзац, ``"li"`` элемент списка, ``"code"`` блок
            кода, ``"quote"`` цитата, ``"hr"`` линия, ``"table"`` таблица.
        level (int): уровень заголовка (1..4) или вложенность элемента списка (0..3).
        text (str): исходный текст с разметкой (для ``h``, ``p``, ``li``, ``quote``).
        marker (str): маркер списка — ``"•"`` или номер вида ``"1."``.
        lines (list[str]): строки блока кода.
        rows (list[list[str]]): строки таблицы; ``rows[0]`` — заголовок.
    """

    kind: str
    level: int = 0
    text: str = ""
    marker: str = ""
    lines: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


def parse_inline(text: str) -> list[Span]:
    """Разбирает встроенную разметку строки на фрагменты со стилем.

    Args:
        text (str): строка с ``**жирным**``, ``*курсивом*``, ``кодом`` и ``[ссылками](url)``.

    Returns:
        list[Span]: фрагменты ``(текст, стиль)`` в порядке следования; ссылка превращается
        в фрагмент со стилем ``"link"`` (показывается только подпись).
    """
    spans: list[Span] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            spans.append((text[pos:m.start()], ""))
        tok = m.group(0)
        if tok.startswith("`"):
            spans.append((tok[1:-1], "code"))
        elif tok.startswith("**"):
            spans.append((tok[2:-2], "b"))
        elif tok.startswith("*"):
            spans.append((tok[1:-1], "i"))
        else:
            label = re.match(r"\[([^\]]+)\]", tok)
            spans.append((label.group(1) if label else tok, "link"))
        pos = m.end()
    if pos < len(text):
        spans.append((text[pos:], ""))
    return spans


def plain(text: str) -> str:
    """Убирает встроенную разметку (для измерения ширины текста).

    Args:
        text (str): строка с разметкой.

    Returns:
        str: те же слова без ``**``, ``*``, обратных кавычек и адресов ссылок.
    """
    return "".join(t for t, _ in parse_inline(text))


def parse(md: str) -> list[Block]:
    """Разбирает Markdown на блоки.

    Args:
        md (str): исходный текст документа.

    Returns:
        list[Block]: блоки в порядке следования. Неподдерживаемая разметка превращается
        в обычные абзацы.
    """
    blocks: list[Block] = []
    lines = md.splitlines()
    para: list[str] = []
    i = 0

    def flush() -> None:
        """Завершает накопленный абзац, добавляя его в список блоков."""
        if para:
            blocks.append(Block("p", text=" ".join(para)))
            para.clear()

    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if s.startswith("```"):
            flush()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i].rstrip())
                i += 1
            blocks.append(Block("code", lines=code))
            i += 1
            continue
        if not s:
            flush()
            i += 1
            continue
        m = _HEAD_RE.match(s)
        if m:
            flush()
            blocks.append(Block("h", level=len(m.group(1)), text=m.group(2)))
            i += 1
            continue
        if _HR_RE.match(s):
            flush()
            blocks.append(Block("hr"))
            i += 1
            continue
        if s.startswith(">"):
            flush()
            quote: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(Block("quote", text=" ".join(quote)))
            continue
        if s.startswith("|"):
            flush()
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                i += 1
                if any(cells) and all(_SEP_CELL_RE.match(c) for c in cells if c):
                    continue                              # строка-разделитель |---|---|
                rows.append(cells)
            blocks.append(Block("table", rows=rows))
            continue
        lm = _LIST_RE.match(raw)
        if lm:
            flush()
            indent = len(lm.group(1).expandtabs(4))
            marker, text = lm.group(2), lm.group(3)
            i += 1
            while (i < len(lines) and lines[i].strip() and not _LIST_RE.match(lines[i])
                   and len(lines[i]) - len(lines[i].lstrip()) > indent):   # продолжение элемента на следующей строке
                text += " " + lines[i].strip()
                i += 1
            blocks.append(Block("li", level=min(3, indent // 2), text=text,
                                marker="•" if marker in ("-", "*", "+") else marker))
            continue
        para.append(s)
        i += 1
    flush()
    return blocks


def _column_widths(natural: list[int], width: int) -> list[int]:
    """Подбирает ширины колонок таблицы: короткие остаются по содержимому, длинные сжимаются.

    Args:
        natural (list[int]): естественная (по содержимому) ширина каждой колонки.
        width (int): доступная ширина таблицы.

    Returns:
        list[int]: ширины колонок, в сумме ровно ``width`` (последняя колонка поглощает остаток).
    """
    n = len(natural)
    if sum(natural) <= width:
        total = sum(natural) or 1
        out = [int(c * width / total) for c in natural]
    else:
        share = width / n
        fixed = [i for i in range(n) if natural[i] <= share]
        rest = width - sum(natural[i] for i in fixed)
        free_total = sum(natural[i] for i in range(n) if i not in fixed) or 1
        out = [natural[i] if i in fixed else max(70, int(natural[i] * rest / free_total)) for i in range(n)]
    out[-1] += width - sum(out)
    return out


class MarkdownView:
    """Раскладывает документ по ширине и рисует его на одной длинной поверхности.

    Attributes:
        blocks (list[Block]): разобранный документ.
        fonts (Fonts): кэш шрифтов (нужны обычный, жирный, курсив и моноширинный).
    """

    def __init__(self, text: str, fonts: Fonts) -> None:
        """Разбирает документ.

        Args:
            text (str): исходный Markdown.
            fonts (Fonts): кэш шрифтов.
        """
        self.blocks = parse(text)
        self.fonts = fonts
        self._cache: Optional[tuple[int, pygame.Surface]] = None

    def render(self, width: int) -> pygame.Surface:
        """Рисует документ на поверхности заданной ширины (кэшируется для последней ширины).

        Args:
            width (int): ширина области текста в пикселях.

        Returns:
            pygame.Surface: поверхность ``width × высота документа`` на белом фоне.

        Raises:
            pygame.error: если шрифты pygame не инициализированы или не хватает памяти
                на поверхность.
        """
        if self._cache and self._cache[0] == width:
            return self._cache[1]
        cmds, height = self._layout(width)
        surf = pygame.Surface((width, max(1, height)))
        surf.fill(WHITE)
        for c in cmds:
            kind = c[0]
            if kind == "text":
                _, x, y, text, px, bold, italic, mono, color = c
                img = self.fonts.surface(text, px, bold, italic, color, mono)
                r = img.get_rect()
                r.centery, r.left = round(y), round(x)
                surf.blit(img, r)
            elif kind == "rect":
                _, rect, fill, outline, radius = c
                if fill is not None:
                    pygame.draw.rect(surf, fill, rect, 0, border_radius=radius)
                if outline is not None:
                    pygame.draw.rect(surf, outline, rect, 1, border_radius=radius)
            elif kind == "line":
                _, a, b, color, w = c
                pygame.draw.line(surf, color, a, b, w)
        self._cache = (width, surf)
        return surf

    # ----- раскладка -----

    def _flow(self, spans: list[Span], x0: float, width: float, y: float, px: int, color: Color,
              bold: bool = False, line_h: int = LINE_H) -> tuple[list[Cmd], int]:
        """Раскладывает фрагменты текста с переносом по словам.

        Args:
            spans (list[Span]): фрагменты ``(текст, стиль)``.
            x0 (float): левая граница области.
            width (float): ширина области.
            y (float): верх первой строки.
            px (int): размер шрифта.
            color (Color): цвет текста.
            bold (bool): базовое жирное начертание (для заголовков).
            line_h (int): высота строки.

        Returns:
            tuple[list[Cmd], int]: ``(команды рисования, число строк)``.
        """
        fonts = self.fonts
        tokens: list[tuple[str, str, bool]] = []          # (слово, стиль, перед словом был пробел)
        space = False
        for text, style in spans:
            for part in re.split(r"(\s+)", text):
                if not part:
                    continue
                if part.isspace():
                    space = True
                else:
                    tokens.append((part, style, space))
                    space = False
        space_w = fonts.font(px).size(" ")[0]
        cmds: list[Cmd] = []
        x, line = 0, 0
        for text, style, sp in tokens:
            bld, ital, mono = bold or style == "b", style == "i", style == "code"
            fpx = px - 1 if mono else px
            w = fonts.font(fpx, bld, ital, mono).size(text)[0] + (6 if mono else 0)
            gap = space_w if sp and x > 0 else 0
            if x > 0 and x + gap + w > width:
                line += 1
                x, gap = 0, 0
            cx = x0 + x + gap
            yc = y + line * line_h + line_h / 2
            if mono:
                cmds.append(("rect", pygame.Rect(round(cx), round(yc - (line_h - 6) / 2), w, line_h - 6), CODE_BG, None, 3))
            cmds.append(("text", cx + (3 if mono else 0), yc, text, fpx, bld, ital, mono, ACCENT if style == "link" else color))
            x += gap + w
        return cmds, line + 1

    def _layout(self, width: int) -> tuple[list[Cmd], int]:
        """Раскладывает все блоки документа по ширине.

        Args:
            width (int): ширина области текста в пикселях.

        Returns:
            tuple[list[Cmd], int]: ``(команды рисования, полная высота документа)``.
        """
        cmds: list[Cmd] = []
        y = 0.0
        fonts = self.fonts
        for idx, blk in enumerate(self.blocks):
            k = blk.kind
            if k == "h":
                size = H_SIZES.get(blk.level, 15)
                if idx:
                    y += H_BEFORE.get(blk.level, 12)
                lh = size + 10
                c, n = self._flow(parse_inline(blk.text), 0, width, y, size, INK, bold=True, line_h=lh)
                cmds += c
                y += n * lh
                if blk.level <= 2:
                    cmds.append(("line", (0, y + 2), (width, y + 2), GRID_COL, 1))
                    y += 6
                y += H_AFTER.get(blk.level, 4)
            elif k == "p":
                c, n = self._flow(parse_inline(blk.text), 0, width, y, BODY_PX, INK)
                cmds += c
                y += n * LINE_H + 10
            elif k == "li":
                ind = 24 + blk.level * 24
                cmds.append(("text", ind - 18, y + LINE_H / 2, blk.marker, BODY_PX, False, False, False, MUTED))
                c, n = self._flow(parse_inline(blk.text), ind, width - ind, y, BODY_PX, INK)
                cmds += c
                y += n * LINE_H + 5
            elif k == "quote":
                c, n = self._flow(parse_inline(blk.text), 18, width - 18, y, BODY_PX, MUTED)
                cmds += c
                cmds.append(("rect", pygame.Rect(4, round(y + 2), 4, n * LINE_H - 4), QUOTE_BAR, None, 2))
                y += n * LINE_H + 10
            elif k == "hr":
                cmds.append(("line", (0, y + 8), (width, y + 8), GRID_COL, 1))
                y += 18
            elif k == "code":
                mono = fonts.font(CODE_PX, mono=True)
                maxc = max(10, int((width - 28) // max(1, mono.size("M")[0])))
                wrapped = [ln[j:j + maxc] for ln in (blk.lines or [""]) for j in range(0, max(1, len(ln)), maxc)]
                h = len(wrapped) * CODE_LINE + 18
                cmds.append(("rect", pygame.Rect(0, round(y), width, h), CODE_BLOCK_BG, CODE_BLOCK_BORDER, 6))
                for j, ln in enumerate(wrapped):
                    cmds.append(("text", 14, y + 9 + j * CODE_LINE + CODE_LINE / 2, ln, CODE_PX, False, False, True, INK))
                y += h + 14
            elif k == "table" and blk.rows:
                y = self._layout_table(blk, width, y, cmds) + 14
        return cmds, int(y + 24)

    def _layout_table(self, blk: Block, width: int, y: float, cmds: list[Cmd]) -> float:
        """Раскладывает таблицу и добавляет её команды рисования.

        Args:
            blk (Block): блок-таблица (``blk.rows`` не пуст).
            width (int): ширина таблицы.
            y (float): верх таблицы.
            cmds (list[Cmd]): список команд, в который дописываются команды таблицы.

        Returns:
            float: ордината нижней границы таблицы.
        """
        rows, fonts, pad = blk.rows, self.fonts, 8
        ncols = max(len(r) for r in rows)
        natural = [8 + 2 * pad] * ncols
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row):
                w = fonts.font(TABLE_PX, bold=(ri == 0)).size(plain(cell))[0] + 2 * pad + 4
                natural[ci] = max(natural[ci], w)
        colw = _column_widths(natural, width)
        top = y
        for ri, row in enumerate(rows):
            cell_cmds: list[Cmd] = []
            nlines = 1
            for ci in range(ncols):
                text = row[ci] if ci < len(row) else ""
                c, n = self._flow(parse_inline(text), sum(colw[:ci]) + pad, colw[ci] - 2 * pad, y + 5, TABLE_PX,
                                  INK, bold=(ri == 0), line_h=TABLE_LINE)
                cell_cmds += c
                nlines = max(nlines, n)
            rh = nlines * TABLE_LINE + 10
            if ri == 0:
                cmds.append(("rect", pygame.Rect(0, round(y), width, rh), TABLE_HEAD_BG, None, 0))
            cmds += cell_cmds
            y += rh
            cmds.append(("line", (0, y), (width, y), GRID_COL, 1))
        x = 0
        for cw in colw[:-1]:
            x += cw
            cmds.append(("line", (x, top), (x, y), GRID_COL, 1))
        cmds.append(("rect", pygame.Rect(0, round(top), width, round(y - top)), None, GRID_COL, 0))
        return y

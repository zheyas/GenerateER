"""Мелкие UI-средства: однострочный текстовый редактор, буфер обмена, системные диалоги выбора файла."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from typing import Any, Callable, Optional

import pygame

#: Результат диалога выбора файла: ``(путь | None, диалог_недоступен)``.
#: ``(None, False)`` — пользователь отменил; ``(None, True)`` — диалог в этой системе не работает.
DialogResult = tuple[Optional[str], bool]

_internal_clip = ""

# Физические клавиши → латинские буквы/цифры: горячие клавиши работают и на русской раскладке.
_SCAN_LETTERS: dict[int, str] = {getattr(pygame, "KSCAN_" + c.upper()): c for c in "abcdefghijklmnopqrstuvwxyz"}
_SCAN_LETTERS.update({getattr(pygame, f"KSCAN_{i}"): str(i) for i in range(10)})


def letter(ev: pygame.event.Event) -> str:
    """Определяет латинскую букву или цифру физической клавиши независимо от раскладки.

    Args:
        ev (pygame.event.Event): событие ``KEYDOWN`` (используются поля ``scancode`` и ``key``).

    Returns:
        str: строчная латинская буква или цифра; пустая строка, если клавиша не буквенно-цифровая.
    """
    sc = getattr(ev, "scancode", None)
    if sc in _SCAN_LETTERS:
        return _SCAN_LETTERS[sc]
    return chr(ev.key) if 32 < ev.key < 127 else ""


def clip_get() -> str:
    """Читает текст из буфера обмена.

    Порядок источников: ``pbpaste`` (macOS) → ``pygame.scrap`` → внутренний буфер программы.

    Returns:
        str: текст буфера обмена (пустая строка, если буфер пуст или недоступен).
    """
    if sys.platform == "darwin" and shutil.which("pbpaste"):
        try:
            return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=2).stdout
        except Exception:
            pass
    try:
        if not pygame.scrap.get_init():
            pygame.scrap.init()
        data = pygame.scrap.get(pygame.SCRAP_TEXT)
        if data:
            return data.decode("utf-8", "ignore").rstrip("\x00")
    except Exception:
        pass
    return _internal_clip


def clip_set(text: str) -> None:
    """Кладёт текст в буфер обмена (и во внутренний буфер программы как запасной вариант).

    Ошибки системного буфера подавляются: в худшем случае текст останется только во внутреннем буфере.

    Args:
        text (str): копируемый текст.
    """
    global _internal_clip
    _internal_clip = text
    if sys.platform == "darwin" and shutil.which("pbcopy"):
        try:
            subprocess.run(["pbcopy"], input=text, text=True, timeout=2)
            return
        except Exception:
            pass
    try:
        if not pygame.scrap.get_init():
            pygame.scrap.init()
        pygame.scrap.put(pygame.SCRAP_TEXT, text.encode("utf-8"))
    except Exception:
        pass


class TextEdit:
    """Однострочный редактор: каретка, выделение, Backspace/Delete, Home/End, копирование и вставка.

    Attributes:
        text (str): текущий текст.
        cur (int): позиция каретки (0..len(text)).
        anchor (Optional[int]): начало выделения (``None`` — выделения нет). При создании
            выделен весь текст, поэтому первый же ввод заменяет его.
    """

    def __init__(self, text: str = "") -> None:
        """Создаёт редактор с выделенным текстом.

        Args:
            text (str): исходный текст; каретка ставится в конец, весь текст выделен.
        """
        self.text = text
        self.cur = len(text)
        self.anchor: Optional[int] = 0

    def sel(self) -> Optional[tuple[int, int]]:
        """Возвращает границы выделения.

        Returns:
            Optional[tuple[int, int]]: ``(от, до)`` или ``None``, если выделения нет.
        """
        if self.anchor is None or self.anchor == self.cur:
            return None
        return min(self.anchor, self.cur), max(self.anchor, self.cur)

    def selected_text(self) -> str:
        """Возвращает выделенный фрагмент.

        Returns:
            str: выделенный текст; пустая строка, если выделения нет.
        """
        s = self.sel()
        return self.text[s[0]:s[1]] if s else ""

    def _delete_sel(self) -> bool:
        """Удаляет выделенный фрагмент и ставит каретку на его начало.

        Returns:
            bool: ``True``, если что-то было удалено; ``False``, если выделения не было.
        """
        s = self.sel()
        if not s:
            return False
        self.text = self.text[:s[0]] + self.text[s[1]:]
        self.cur = s[0]
        self.anchor = None
        return True

    def insert(self, s: str) -> None:
        """Вставляет текст в позицию каретки, заменяя выделение.

        Args:
            s (str): вставляемый текст; переводы строк превращаются в пробелы
                (редактор однострочный).
        """
        s = s.replace("\n", " ").replace("\r", "")
        self._delete_sel()
        self.text = self.text[:self.cur] + s + self.text[self.cur:]
        self.cur += len(s)
        self.anchor = None

    def move(self, pos: int, extend: bool) -> None:
        """Перемещает каретку.

        Args:
            pos (int): новая позиция; ограничивается диапазоном ``0..len(text)``.
            extend (bool): ``True`` — расширять выделение от прежней позиции (Shift),
                ``False`` — снять выделение.
        """
        pos = max(0, min(len(self.text), pos))
        if extend:
            if self.anchor is None:
                self.anchor = self.cur
        else:
            self.anchor = None
        self.cur = pos

    def click(self, x_rel: float, font: pygame.font.Font) -> None:
        """Ставит каретку по горизонтальной позиции клика.

        Args:
            x_rel (float): расстояние клика от левого края текста в пикселях.
            font (pygame.font.Font): шрифт, которым текст нарисован (нужен для измерения).
        """
        idx = len(self.text)
        for i in range(len(self.text) + 1):
            if font.size(self.text[:i])[0] >= x_rel:
                idx = i
                if i and abs(font.size(self.text[:i - 1])[0] - x_rel) < abs(font.size(self.text[:i])[0] - x_rel):
                    idx = i - 1
                break
        self.move(idx, False)

    def key(self, ev: pygame.event.Event) -> Optional[str]:
        """Обрабатывает нажатие клавиши (ввод символов приходит отдельно — событием ``TEXTINPUT``).

        Поддерживаются: Enter, Esc, Backspace, Delete, стрелки (с Shift — выделение, с Cmd/Ctrl —
        в начало / конец), Home, End, Cmd/Ctrl + A / C / X / V.

        Args:
            ev (pygame.event.Event): событие ``KEYDOWN``.

        Returns:
            Optional[str]: ``"commit"`` (Enter), ``"cancel"`` (Esc) или ``None``, если
            редактирование продолжается.
        """
        k, mod = ev.key, ev.mod
        ch = letter(ev)
        cmd = bool(mod & (pygame.KMOD_CTRL | pygame.KMOD_META))
        shift = bool(mod & pygame.KMOD_SHIFT)
        if k in (pygame.K_RETURN, pygame.K_KP_ENTER):
            return "commit"
        if k == pygame.K_ESCAPE:
            return "cancel"
        if cmd and ch == "a":
            self.anchor, self.cur = 0, len(self.text)
        elif cmd and ch == "c":
            if self.sel():
                clip_set(self.selected_text())
        elif cmd and ch == "x":
            if self.sel():
                clip_set(self.selected_text())
                self._delete_sel()
        elif cmd and ch == "v":
            self.insert(clip_get())
        elif k == pygame.K_BACKSPACE:
            if not self._delete_sel() and self.cur > 0:
                self.text = self.text[:self.cur - 1] + self.text[self.cur:]
                self.cur -= 1
        elif k == pygame.K_DELETE:
            if not self._delete_sel() and self.cur < len(self.text):
                self.text = self.text[:self.cur] + self.text[self.cur + 1:]
        elif k == pygame.K_LEFT:
            self.move(0 if cmd else self.cur - 1, shift)
        elif k == pygame.K_RIGHT:
            self.move(len(self.text) if cmd else self.cur + 1, shift)
        elif k == pygame.K_HOME:
            self.move(0, shift)
        elif k == pygame.K_END:
            self.move(len(self.text), shift)
        return None


# ───────────────────────── диалоги выбора файла ─────────────────────────

def _osascript(script: str) -> DialogResult:
    """Выполняет AppleScript с диалогом выбора файла и возвращает выбранный путь.

    Args:
        script (str): текст AppleScript, возвращающий ``POSIX path``.

    Returns:
        DialogResult: ``(путь, False)`` при выборе; ``(None, False)``, если пользователь
        отменил; ``(None, True)``, если ``osascript`` не удалось запустить.
    """
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    except Exception:
        return None, True
    if r.returncode != 0:
        return None, False                           # пользователь отменил
    return r.stdout.strip() or None, False


def _tk(code: str) -> DialogResult:
    """Запускает tkinter-диалог в отдельном процессе (чтобы не конфликтовать с SDL).

    Args:
        code (str): Python-код, печатающий выбранный путь в stdout.

    Returns:
        DialogResult: ``(путь | None, False)`` или ``(None, True)``, если процесс не удалось
        запустить или он превысил лимит времени (10 минут).
    """
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=600)
        return (r.stdout.strip() or None), False
    except Exception:
        return None, True


def choose_file(prompt: str, exts: list[str]) -> DialogResult:
    """Показывает системный диалог «Открыть файл» (macOS — ``osascript``, иначе tkinter).

    Args:
        prompt (str): подсказка в заголовке диалога.
        exts (list[str]): допустимые расширения без точки, например ``["db", "json"]``.

    Returns:
        DialogResult: ``(путь, False)``; ``(None, False)`` — отмена; ``(None, True)`` — диалог
        в этой системе недоступен.
    """
    if sys.platform == "darwin" and shutil.which("osascript"):
        types = ", ".join(f'"{e}"' for e in exts)
        return _osascript(f'POSIX path of (choose file with prompt "{prompt}" of type {{{types}}})')
    patt = " ".join(f"*.{e}" for e in exts)
    return _tk("import tkinter as tk\nfrom tkinter import filedialog\nr=tk.Tk(); r.withdraw()\n"
               f"print(filedialog.askopenfilename(title={prompt!r}, filetypes=[('files', {patt!r})]))")


def choose_save(prompt: str, default_name: str, directory: str = "") -> DialogResult:
    """Показывает системный диалог «Сохранить как».

    Args:
        prompt (str): подсказка в заголовке диалога.
        default_name (str): предлагаемое имя файла.
        directory (str): начальная папка; игнорируется, если не существует.

    Returns:
        DialogResult: ``(путь, False)``; ``(None, False)`` — отмена; ``(None, True)`` — диалог
        в этой системе недоступен.
    """
    if sys.platform == "darwin" and shutil.which("osascript"):
        loc = f' default location (POSIX file "{directory}")' if directory and os.path.isdir(directory) else ""
        return _osascript(f'POSIX path of (choose file name with prompt "{prompt}" default name "{default_name}"{loc})')
    return _tk("import tkinter as tk\nfrom tkinter import filedialog\nr=tk.Tk(); r.withdraw()\n"
               f"print(filedialog.asksaveasfilename(title={prompt!r}, initialfile={default_name!r}, initialdir={directory!r}))")


class AsyncJob:
    """Выполняет блокирующую функцию (диалог) в потоке, чтобы окно не «зависало».

    Главный цикл периодически проверяет ``done`` и, когда работа завершена, вызывает
    ``callback(result)`` уже в главном потоке.

    Attributes:
        callback (Callable[[Any], None]): функция, получающая результат.
        result (Any): результат ``fn`` (``(None, True)``, если ``fn`` упала с исключением).
        done (bool): ``True``, когда работа завершена.
    """

    def __init__(self, fn: Callable[[], Any], callback: Callable[[Any], None]) -> None:
        """Запускает ``fn`` в фоновом (демон-)потоке.

        Args:
            fn (Callable[[], Any]): выполняемая функция без аргументов.
            callback (Callable[[Any], None]): вызывается главным циклом с результатом ``fn``.
        """
        self.callback = callback
        self.result: Any = None
        self.done = False
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()

    def _run(self, fn: Callable[[], Any]) -> None:
        """Тело потока: вызывает ``fn``, сохраняет результат и отмечает работу завершённой.

        Исключения ``fn`` не пробрасываются: результатом становится ``(None, True)``.

        Args:
            fn (Callable[[], Any]): выполняемая функция.
        """
        try:
            self.result = fn()
        except Exception:                              # noqa: BLE001
            self.result = (None, True)
        self.done = True

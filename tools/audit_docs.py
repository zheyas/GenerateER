#!/usr/bin/env python3
"""Аудит аннотаций типов и докстрингов: каждая функция должна быть описана полностью.

Проверяется для каждой функции и метода (включая вложенные; lambda не проверяются):

* у всех параметров (кроме ``self`` / ``cls``) есть аннотация типа;
* указан тип возвращаемого значения (``-> None`` тоже нужно писать);
* есть докстring; в нём раздел ``Args:`` упоминает каждый параметр;
* если функция возвращает значение (аннотация не ``None``) — есть раздел ``Returns:``;
* если в теле есть ``raise`` — есть раздел ``Raises:``.

Запуск::

    python tools/audit_docs.py [файл_или_папка ...]

Код возврата — число найденных замечаний (0 — всё в порядке).
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    """Возвращает все параметры функции в порядке объявления (включая ``*args`` и ``**kwargs``).

    Args:
        fn: узел определения функции.

    Returns:
        Список узлов ``ast.arg``.
    """
    a = fn.args
    out = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
    if a.vararg:
        out.append(a.vararg)
    if a.kwarg:
        out.append(a.kwarg)
    return out


def _has_raise(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Проверяет, есть ли в теле функции (без вложенных функций) оператор ``raise``.

    Args:
        fn: узел определения функции.

    Returns:
        ``True``, если ``raise`` встречается в самой функции.
    """
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return False


def check_function(fn: ast.FunctionDef | ast.AsyncFunctionDef, where: str) -> list[str]:
    """Проверяет одну функцию по правилам модуля.

    Args:
        fn: узел определения функции.
        where: человекочитаемое место (``файл:строка имя``) для сообщений.

    Returns:
        Список текстов замечаний (пустой, если замечаний нет).
    """
    issues: list[str] = []
    names = [p for p in _params(fn) if p.arg not in ("self", "cls")]
    for p in names:
        if p.annotation is None:
            issues.append(f"{where}: параметр «{p.arg}» без аннотации типа")
    if fn.returns is None:
        issues.append(f"{where}: нет аннотации возвращаемого значения (-> ...)")
    doc = ast.get_docstring(fn)
    if not doc:
        issues.append(f"{where}: нет докстринга")
        return issues
    if names:
        if "Args:" not in doc:
            issues.append(f"{where}: в докстринге нет раздела Args:")
        else:
            args_part = doc.split("Args:", 1)[1]
            for stop in ("Returns:", "Raises:", "Yields:"):
                args_part = args_part.split(stop, 1)[0]
            for p in names:
                if p.arg not in args_part:
                    issues.append(f"{where}: параметр «{p.arg}» не описан в Args:")
    returns_none = isinstance(fn.returns, ast.Constant) and fn.returns.value is None
    if fn.returns is not None and not returns_none and "Returns:" not in doc:
        issues.append(f"{where}: в докстринге нет раздела Returns:")
    if _has_raise(fn) and "Raises:" not in doc:
        issues.append(f"{where}: есть raise, но в докстринге нет раздела Raises:")
    return issues


def audit_file(path: Path) -> tuple[int, list[str]]:
    """Проверяет все функции файла.

    Args:
        path: путь к файлу ``.py``.

    Returns:
        ``(число проверенных функций, список замечаний)``.

    Raises:
        SyntaxError: если файл не удаётся разобрать.
        OSError: если файл не читается.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    issues: list[str] = []
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            count += 1
            issues += check_function(node, f"{path.name}:{node.lineno} {node.name}")
    return count, issues


def main(argv: list[str]) -> int:
    """Точка входа: проверяет указанные файлы / папки (по умолчанию ``er_editor`` и ``generate_er.py``).

    Args:
        argv: аргументы командной строки без имени программы.

    Returns:
        Число замечаний (используется как код возврата).
    """
    root = Path(__file__).resolve().parent.parent
    targets = [Path(a) for a in argv] or [root / "er_editor", root / "generate_er.py", root / "run_editor.py", root / "tools"]
    files: list[Path] = []
    for t in targets:
        files += sorted(t.rglob("*.py")) if t.is_dir() else [t]
    total, all_issues = 0, []
    for f in files:
        n, iss = audit_file(f)
        total += n
        all_issues += iss
        print(f"{f.name:16s} функций: {n:4d}   замечаний: {len(iss)}")
    for line in all_issues[:int(os.environ.get('AUDIT_SHOW', '60'))]:
        print("  -", line)
    print(f"\nИтого функций: {total}, замечаний: {len(all_issues)}")
    return len(all_issues)


if __name__ == "__main__":
    sys.exit(min(main(sys.argv[1:]), 255))

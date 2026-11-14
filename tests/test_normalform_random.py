"""Случайные схемы: вердикт normalform.check сверяется с независимым полным перебором (учебные определения НФ).

Зависимости записываются в поле «Зависимости»; ключи-кандидаты объявляются либо все (PK и UNIQUE), либо только первый (остальные
программа должна вывести из зависимостей сама). Имена столбцов
нейтральные (``a``, ``b``…), чтобы признаки по именам не срабатывали: проверяется именно логика по зависимостям.
"""
import random

from common import Counter

from er_editor import normalform as nf
from tools.experiments import build_table, candidate_keys, random_fds, textbook_level

T = Counter()


def run(declare_all):
    """Сверяет программу с перебором на 1500 случайных таблицах; возвращает (расхождений, распределение эталона)."""
    rnd = random.Random(20260101)
    diff = 0
    by_level = {1: 0, 2: 0, 3: 0, 4: 0}
    for _ in range(1500):
        cols, fds = random_fds(rnd)
        keys = candidate_keys(cols, fds)
        expect = textbook_level(cols, fds)
        got = nf.check(build_table(cols, fds, keys if declare_all else keys[:1]), 4)
        by_level[expect] += 1
        if got.achieved != expect:
            diff += 1
            if diff <= 5:
                print("FAIL: столбцы", cols, "зависимости", [(sorted(lhs), sorted(rhs)) for lhs, rhs in fds], "ключи", [sorted(k) for k in keys],
                      "ожидали", expect, "получили", got.achieved, [i.rule for i in got.issues])
    return diff, by_level


for label, flag in (("объявлены все ключи-кандидаты", True), ("объявлен только первичный ключ", False)):
    diff, by_level = run(flag)
    T.check(f"вердикт совпал с перебором на 1500 схемах: {label} (расхождений: {diff})", diff == 0)
    T.check(f"в выборке есть схемы всех уровней: {label}", all(by_level.values()), by_level)
print("распределение по достигнутой форме:", by_level)
T.finish()

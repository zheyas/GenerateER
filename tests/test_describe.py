"""Проверка модуля describe: текстовое описание и SQL превращаются в схему."""
from common import Counter

from er_editor import describe as ds
from er_editor import normalform as nf
from er_editor.model import KIND_DATA
from er_editor.schema import build_data

T = Counter()


def fk_map(res):
    return {t.name: [(f.cols, f.ref_table, f.on_delete) for f in t.fks] for t in res.tables}


def cols(res, name):
    return {c.name: (c.type, c.notnull) for c in next(t for t in res.tables if t.name == name).cols}


# пример из окна
r = ds.describe(ds.EXAMPLE_TEXT)
T.check("пример: формат и таблицы", r.fmt == "text" and [t.name for t in r.tables] == ["клиент", "заказ", "товар", "категория", "позиция"], [t.name for t in r.tables])
T.check("пример: id автоматически", all(t.pk and t.cols[0].name == "id" for t in r.tables[:4]))
T.check("пример: типы и необязательность", cols(r, "клиент")["телефон"] == ("TEXT", False) and cols(r, "заказ")["дата"][0] == "DATE"
        and cols(r, "заказ")["сумма"][0] == "REAL" and cols(r, "товар")["остаток"][0] == "INTEGER")
T.check("пример: уникальность", [["email"]] == next(t for t in r.tables if t.name == "клиент").uniques)
T.check("пример: 1:N ключ в «много»", fk_map(r)["заказ"] == [(["клиент_id"], "клиент", "RESTRICT")] and fk_map(r)["товар"] == [(["категория_id"], "категория", "RESTRICT")])
T.check("пример: N:M — таблица-связка", fk_map(r)["позиция"] == [(["заказ_id"], "заказ", "CASCADE"), (["товар_id"], "товар", "CASCADE")]
        and next(t for t in r.tables if t.name == "позиция").pk == ["заказ_id", "товар_id"] and "количество" in cols(r, "позиция"))
T.check("пример: схема даёт диаграмму с 4 связями", len(build_data(r.tables).links) == 4)
T.check("результат проходит проверку 3НФ", nf.check(build_data(r.tables), 3).ok)

# кратности и записи
r = ds.describe("Клиент: имя\nЗаказ: номер\nПаспорт: серия\nКлиент 1:N? Заказ\nПаспорт N:1 Клиент\nПаспорт 1:1 Заказ")
fk = fk_map(r)
T.check("1:N? — необязательная ссылка", fk["заказ"][0][2] == "SET NULL" and cols(r, "заказ")["клиент_id"] == ("INTEGER", False), fk)
T.check("N:1 — ключ в левой", fk["паспорт"][0] == (["клиент_id"], "клиент", "RESTRICT"))
T.check("1:1 — UNIQUE на ключе", ["паспорт_id"] in next(t for t in r.tables if t.name == "заказ").uniques, fk)
r = ds.describe("Сотрудник: имя\nСотрудник -> Сотрудник")
T.check("самосвязь допускает NULL", cols(r, "сотрудник")["parent_id"] == ("INTEGER", False))
r = ds.describe("Автор: имя\nКнига: название\nАвтор Н:М Книга")
T.check("кириллическая Н:М", any(t.name == "автор_книга" for t in r.tables))
r = ds.describe("Клиент: имя\nЗаказ: номер\nЗаказ -> Клиент")
T.check("-> : левая ссылается на правую", fk_map(r)["заказ"] == [(["клиент_id"], "клиент", "RESTRICT")])
r = ds.describe("Клиент: имя\nЗаказ: номер\nКлиент один-ко-многим Заказ")
T.check("«один-ко-многим»", fk_map(r)["заказ"][0][1] == "клиент")

# обычные фразы
r = ds.describe("Клиент: имя\nЗаказ: дата\nТовар: название\nКатегория: название\n"
                "у одного клиента может быть много заказов\nкаждый товар принадлежит одной категории\nмногие заказы содержат много товаров")
fk = fk_map(r)
T.check("фраза: у одного … много …", fk["заказ"] == [(["клиент_id"], "клиент", "RESTRICT")])
T.check("фраза: «каждый X принадлежит одной Y»", fk["товар"] == [(["категория_id"], "категория", "RESTRICT")] and not fk["категория"])
T.check("фраза: много … много → связка", any(t.name == "заказ_товар" for t in r.tables))
T.check("фразы поясняются", sum("понято как" in n for n in r.notes) == 3, r.notes)

# ошибки
for text, needle in [("", "пусто"), ("просто слова", "не описано ни одной сущности"), ("Клиент: имя\nЗаказ: дата\nКлиент 1:N Тест", "не найдена сущность"),
                     ("Клиент: имя\nКлиент: город", "описана дважды"), ("Клиент: имя\nЗаказ: д\nКлиент X:Y Заказ", "")]:
    try:
        ds.describe(text)
        T.check(f"ошибка для {text[:20]!r}", needle == "", "не упало")
    except ds.DescribeError as e:
        T.check(f"ошибка для {text[:20]!r}", needle in " ".join(e.problems), e.problems)

# SQL
r = ds.describe(ds.EXAMPLE_SQL)
T.check("SQL: таблицы и FK", r.fmt == "sql" and [t.name for t in r.tables] == ["clients", "orders"] and r.tables[1].fks[0].on_delete == "CASCADE")
r = ds.describe("ATTACH DATABASE '/tmp/er_x.db' AS y; create table z(id integer primary key, a text unique); insert into z values (1,'x');")
T.check("SQL: опасные операторы пропущены", [t.name for t in r.tables] == ["z"] and len(r.notes) == 2 and r.tables[0].uniques == [["a"]], r.notes)
for bad in ("CREATE TABLE a(", "create table t as with recursive c(x) as (select 1 union all select x+1 from c) select * from c;", "drop table x"):
    try:
        ds.describe(bad)
        T.check(f"SQL-ошибка {bad[:18]!r}", False)
    except ds.DescribeError as e:
        T.check(f"SQL-ошибка {bad[:18]!r}", bool(e.problems), e.problems)
try:
    ds.describe("x" * (ds.MAX_TEXT + 1))
    T.check("слишком большое описание", False)
except ds.DescribeError:
    T.check("слишком большое описание", True)
T.check("ident и stem", ds.ident(" Тип  заказа-1 ") == "тип_заказа_1" and ds.stem("клиент") == "клие" and ds.stem("заказ") == "зака")
T.check("parse_attr", ds.parse_attr("email!") == ("email", "TEXT", True, True) and ds.parse_attr("цена:вещественное?") == ("цена", "REAL", False, False)
        and ds.parse_attr("?") is None)
r = ds.describe(ds.EXAMPLE_PROSE)
names = [t.name for t in r.tables]
T.check("проза: формат и сущности", r.fmt == "prose" and "продукт" in names and "заказ" in names and "учётные_данные" in names, names)
T.check("проза: «Код» — id, «Код X» — FK", fk_map(r)["заказ"] == [(["пользователь_id"], "пользователь", "RESTRICT")])
j = next(t for t in r.tables if t.name == "связь_заказ_продукт")
T.check("проза: связка с составным ключом", j.pk == ["заказ_id", "продукт_id"] and "количество" in [c.name for c in j.cols] and len(j.fks) == 2)
T.check("проза: типы по названиям", cols(r, "пользователь")["дата_рождения"][0] == "DATE" and cols(r, "продукт")["цена"][0] == "REAL")
T.check("проза: пропущенное — в пояснениях", any("например" in n for n in r.notes), r.notes)
T.finish()

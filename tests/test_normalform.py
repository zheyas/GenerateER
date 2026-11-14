"""Проверка модуля normalform: правила 1НФ–НФБК, зависимости, устойчивость к мусору."""
import random

from common import Counter

from er_editor import normalform as nf
from er_editor.model import KIND_DATA, Diagram, Link, Node, Row

T = Counter()


def table(d, title, rows, uniques=None, fds=None):
    n = Node(d.new_id("n"), title, rows=[Row(name=r[0], type=r[1], key=r[2] if len(r) > 2 else "", notnull=False) for r in rows],
             uniques=uniques or [], fds=fds or [])
    d.nodes.append(n)
    return n


def link(d, a, b, fk, ref):
    d.links.append(Link(d.new_id("l"), a.id, b.id, fk_cols=fk, ref_cols=ref))


def rules(d, level):
    return {(i.level, i.severity, i.rule) for i in nf.check(d, level).ordered()}


# хорошая схема
d = Diagram(KIND_DATA)
u = table(d, "users", [("id", "INTEGER", "PK"), ("name", "TEXT"), ("email", "TEXT", "U")])
o = table(d, "orders", [("id", "INTEGER", "PK"), ("user_id", "INTEGER", "FK"), ("created", "DATE")])
link(d, o, u, ["user_id"], ["id"])
r = nf.check(d, 3)
T.check("хорошая схема: 3НФ", r.ok and r.achieved == 4 and not r.issues, r.verdict())

# 1НФ
d = Diagram(KIND_DATA)
table(d, "clients", [("id", "INTEGER", "PK"), ("fio", "TEXT"), ("phone1", "TEXT"), ("phone2", "TEXT"), ("tags", "JSON"), ("note", "")])
table(d, "nopk", [("a", "INTEGER"), ("b", "TEXT")])
got = rules(d, 1)
T.check("1НФ: повторяющаяся группа", (1, "error", "Повторяющаяся группа") in got, got)
T.check("1НФ: неатомарный тип", (1, "error", "Неатомарный тип") in got, got)
T.check("1НФ: нет PK", (1, "error", "Нет первичного ключа") in got, got)
T.check("1НФ: тип не указан — только подозрение", (1, "warn", "Тип не указан") in got, got)
T.check("1НФ не соблюдена", not nf.check(d, 1).ok and nf.check(d, 1).achieved == 0)

# 2НФ: копия атрибута родителя в позиции заказа
d = Diagram(KIND_DATA)
p = table(d, "products", [("id", "INTEGER", "PK"), ("name", "TEXT"), ("price", "REAL")])
o = table(d, "orders", [("id", "INTEGER", "PK")])
oi = table(d, "order_items", [("order_id", "INTEGER", "PK,FK"), ("product_id", "INTEGER", "PK,FK"), ("qty", "INTEGER"),
                              ("product_name", "TEXT"), ("price", "REAL")])
link(d, oi, o, ["order_id"], ["id"])
link(d, oi, p, ["product_id"], ["id"])
got = rules(d, 2)
T.check("2НФ: копия атрибута родителя", (2, "error", "Копия атрибута родителя") in got, got)
T.check("2НФ: одноимённый столбец — подозрение", (2, "warn", "Возможная копия атрибута") in got, got)
T.check("для 1НФ эта схема в порядке", nf.check(d, 1).ok)

# 2НФ по заявленной зависимости
d = Diagram(KIND_DATA)
table(d, "enroll", [("student_id", "INTEGER", "PK"), ("course_id", "INTEGER", "PK"), ("grade", "INTEGER"), ("student_name", "TEXT")],
      fds=["student_id -> student_name"])
got = rules(d, 2)
T.check("2НФ: частичная зависимость", (2, "error", "Зависимость от части ключа") in got, got)
T.check("2НФ: слабый признак не дублирует", not any(k[2] == "Возможная частичная зависимость" for k in got), got)

# 3НФ
d = Diagram(KIND_DATA)
c = table(d, "customers", [("id", "INTEGER", "PK"), ("name", "TEXT")])
t = table(d, "orders", [("id", "INTEGER", "PK"), ("customer_id", "INTEGER", "FK"), ("customer_name", "TEXT"), ("city", "TEXT"), ("country", "TEXT")],
          fds=["city -> country"])
link(d, t, c, ["customer_id"], ["id"])
got = rules(d, 3)
T.check("3НФ: копия родителя при неключевом FK", (3, "error", "Копия атрибута родителя") in got, got)
T.check("3НФ: транзитивная зависимость", (3, "error", "Транзитивная зависимость") in got, got)
T.check("для 2НФ эта схема в порядке", nf.check(d, 2).ok and nf.check(d, 3).achieved == 2)

# НФБК
d = Diagram(KIND_DATA)
table(d, "tt", [("student", "TEXT", "PK"), ("subject", "TEXT", "PK"), ("teacher", "TEXT")], fds=["teacher -> subject"])
T.check("НФБК: 3НФ соблюдена", nf.check(d, 3).ok)
T.check("НФБК нарушена", not nf.check(d, 4).ok and (4, "error", "Левая часть не ключ") in rules(d, 4))

# структура схемы, пустая схема, самоссылка
d = Diagram(KIND_DATA)
a = table(d, "a", [("id", "INTEGER"), ("x", "INTEGER")])
b = table(d, "b", [("id", "INTEGER", "PK"), ("a_id", "TEXT", "FK")])
link(d, b, a, ["a_id"], [])
T.check("структура: нет ключа у родителя", (0, "error", "Нет ключа у родителя") in rules(d, 1))
T.check("пустая схема", nf.check(Diagram(KIND_DATA), 3).tables == 0 and not nf.check(Diagram(KIND_DATA), 3).ok)
d = Diagram(KIND_DATA)
e = table(d, "emp", [("id", "INTEGER", "PK"), ("boss_id", "INTEGER", "FK"), ("boss_name", "TEXT")])
link(d, e, e, ["boss_id"], ["id"])
T.check("самоссылка: пара id/name", (3, "error", "Транзитивная зависимость") in rules(d, 3))

# разбор зависимостей
T.check("parse_fd", nf.parse_fd("a,b => c") == nf.FD(("a", "b"), ("c",)) and nf.parse_fd("x") is None and nf.parse_fd("-> a") is None)
T.check("normalize_fds", nf.normalize_fds("a->b; x y; z,w → q") == (["a -> b", "z, w -> q"], ["x y"]))
T.check("rename_in_fds", nf.rename_in_fds(["a, b -> c", "bad"], "B", "z") == ["a, z -> c", "bad"])
T.check("id_stem", nf.id_stem("product_id") == "product" and nf.id_stem("id") == "")
try:
    nf.check(Diagram(KIND_DATA), 7)
    T.check("level=7 -> ValueError", False)
except ValueError:
    T.check("level=7 -> ValueError", True)

# отчёт: словарь и Markdown
rep = nf.check(d, 3).to_dict()
T.check("to_dict", rep["target"] == 3 and rep["issues"] and "verdict" in rep and rep["tables"] == 1)
T.check("format_markdown", "# Проверка нормальной формы: 3НФ" in nf.format_markdown(nf.check(d, 3), "x"))

# устойчивость к мусору
random.seed(7)
NAMES = ["id", "name", "", " ", "ID", "a", "b", "city", "country", "user_id", "user_name", "phone1", "phone2", "tags", "price", "x_id", "x_name"]
TYPES = ["", "INTEGER", "TEXT", "JSON", "int[]", "VARCHAR(20)"]
KEYS = ["", "PK", "FK", "U", "PK,FK", "pk"]
crashed = None
for it in range(1500):
    d = Diagram(KIND_DATA)
    for _ in range(random.randint(0, 5)):
        rows = [Row(name=random.choice(NAMES), type=random.choice(TYPES), key=random.choice(KEYS), notnull=random.random() < .5)
                for _ in range(random.randint(0, 6))]
        d.nodes.append(Node(d.new_id("n"), random.choice(["t", "T", "orders", "", "users"]), rows=rows,
                            uniques=[[random.choice(NAMES) for _ in range(random.randint(0, 3))]],
                            fds=[random.choice(["a -> b", "id->name", "x", "city -> country", "-> a", "a, b -> c", ""]) for _ in range(random.randint(0, 3))]))
    ids = [n.id for n in d.nodes] + ["ghost"]
    for _ in range(random.randint(0, 4)):
        d.links.append(Link(d.new_id("l"), random.choice(ids), random.choice(ids), fk_cols=[random.choice(NAMES)], ref_cols=[random.choice(NAMES)]))
    try:
        for lvl in nf.LEVELS:
            r = nf.check(d, lvl)
            r.verdict(), r.short(), r.achieved, nf.format_markdown(r, "f"), r.to_dict(), nf.fingerprint(d, lvl)
    except Exception as exc:  # noqa: BLE001
        crashed = repr(exc)
        break
T.check("1500 случайных схем без исключений", crashed is None, crashed)
# ключи-кандидаты, выведенные из зависимостей, но не объявленные
d = Diagram(KIND_DATA)
table(d, "t", [("a", "INTEGER", "PK"), ("b", "INTEGER"), ("c", "INTEGER"), ("d", "INTEGER")],
      fds=["a -> b, c", "b, c -> d", "c, d -> a", "b -> d"])
r = nf.check(d, 4)
T.check("выведенные ключи: форма 3НФ, а не 2НФ", r.achieved == 3, r.achieved)
T.check("выведенные ключи: предупреждение о необъявленных", [i.rule for i in r.issues].count("Необъявленный ключ") == 2
        and all(i.severity == "warn" for i in r.issues if i.rule == "Необъявленный ключ"), [i.rule for i in r.issues])
d = Diagram(KIND_DATA)
table(d, "t", [("a", "INTEGER"), ("b", "INTEGER", "PK"), ("c", "INTEGER", "PK"), ("d", "INTEGER")],
      fds=["d -> a", "c -> d", "b, d -> c", "c -> a"])
r = nf.check(d, 2)
T.check("выведенный ключ: частичная зависимость не пропущена", not r.ok and r.achieved == 1, (r.achieved, [i.rule for i in r.issues]))
d = Diagram(KIND_DATA)
table(d, "t", [("a", "INTEGER"), ("b", "INTEGER"), ("c", "INTEGER")], fds=["a -> b"])
r = nf.check(d, 1)
T.check("без объявленного ключа ключи не выводятся", "Нет первичного ключа" in [i.rule for i in r.issues] and "Необъявленный ключ" not in [i.rule for i in r.issues], [i.rule for i in r.issues])
d = Diagram(KIND_DATA)
table(d, "t", [("id", "INTEGER", "PK"), ("name", "TEXT"), ("city", "TEXT")], fds=["name -> city"])
T.check("без новых ключей предупреждений нет", "Необъявленный ключ" not in [i.rule for i in nf.check(d, 3).issues])

T.finish()

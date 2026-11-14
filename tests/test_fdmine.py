"""Тест поиска зависимостей по данным: находит настоящие, не выдаёт ключи, константы и случайные совпадения."""
import os
import random
import sqlite3
import tempfile

from common import Counter

from er_editor import fdmine
from er_editor.normalform import parse_fd

T = Counter()
check = T.check
path = os.path.join(tempfile.mkdtemp(prefix="er_fd_"), "t.db")
con = sqlite3.connect(path)
con.executescript("""
create table emp(id integer primary key, name text, dept text, boss text, city text, country text, salary real, flag text);
create table tiny(a, b);
create table lines(order_id integer, item_id integer, qty integer, item_name text, primary key(order_id, item_id));
""")
rnd = random.Random(7)
cities = {"Москва": "Россия", "Казань": "Россия", "Минск": "Беларусь", "Киев": "Украина"}
depts = {"A": "Иванов", "B": "Петров", "C": "Сидоров"}
for i in range(200):
    c, d = rnd.choice(list(cities)), rnd.choice(list(depts))
    con.execute("insert into emp values(?,?,?,?,?,?,?,?)", (i, f"n{i}", d, depts[d], c, cities[c], rnd.randint(1, 10_000), "x"))
con.execute("insert into tiny values(1, 1)")
names = {1: "ручка", 2: "тетрадь", 3: "линейка"}
for o in range(40):
    for it in rnd.sample([1, 2, 3], 2):
        con.execute("insert into lines values(?,?,?,?)", (o, it, rnd.randint(1, 5), names[it]))
con.commit()
con.close()

res = {t.name: t for t in fdmine.discover(path)}
emp = {f.text() for f in res["emp"].fds}
check("найдена city -> country", "city -> country" in emp, emp)
check("найдены dept <-> boss", {"dept -> boss", "boss -> dept"} <= emp, emp)
check("ключ id не левая часть", not any(f.lhs == ("id",) for f in res["emp"].fds))
check("уникальный и константный столбцы не правые части", not any({"id", "name", "flag"} & set(f.rhs) for f in res["emp"].fds), emp)
check("нет ложных зависимостей от salary", not any("salary" in f.lhs for f in res["emp"].fds), emp)
check("минимальность: нет city, X -> country", not any(len(f.lhs) > 1 and "city" in f.lhs and "country" in f.rhs for f in res["emp"].fds))
lines = {f.text() for f in res["lines"].fds}
check("часть составного ключа: item_id -> item_name", "item_id -> item_name" in lines, lines)
check("мало строк — пояснение, без кандидатов", res["tiny"].fds == [] and "мало строк" in res["tiny"].note)
check("записи разбирает parse_fd из поля «Зависимости»", all(parse_fd(t) for t in emp | lines))
check("as_json", fdmine.as_json(list(res.values()))[0]["fds"][0]["text"] and fdmine.fd_text(list(res.values()), "EMP") is not None
      and fdmine.fd_text(list(res.values()), "нет") is None)
try:
    fdmine.discover(__file__)
    ok = False
except Exception as exc:   # noqa: BLE001 — файл не база: sqlite3.Error
    ok = isinstance(exc, sqlite3.Error)
check("не база SQLite -> sqlite3.Error", ok)
before = open(path, "rb").read()
fdmine.discover(path)
check("база только читается", open(path, "rb").read() == before)
T.finish()

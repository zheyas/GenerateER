"""Тест правил ON DELETE и значений DEFAULT: создание базы, реальное поведение SQLite, предупреждения, безопасность, импорт."""
import os
import sqlite3
import tempfile

from common import Counter

from er_editor import dbgen, schema
from er_editor.model import KIND_DATA, Diagram, Link, Node, Row

T = Counter()
check = T.check


def make(rule, child_notnull=False, child_default=""):
    """Диаграмма: родитель p(id PK, name) и ребёнок c(id PK, p_id FK [NOT NULL] [DEFAULT]) со связью c → p и подписью rule."""
    d = Diagram(KIND_DATA)
    p = Node(d.new_id("n"), "p", rows=[Row("id", "", "INTEGER", "PK", True), Row("name", "", "TEXT", "", False)])
    c = Node(d.new_id("n"), "c", rows=[Row("id", "", "INTEGER", "PK", True),
                                       Row("p_id", "", "INTEGER", "FK", child_notnull, child_default)])
    d.nodes += [p, c]
    d.links.append(Link(d.new_id("l"), c.id, p.id, label=rule, fk_cols=["p_id"], ref_cols=["id"]))
    return d


def build(d):
    """Создаёт базу по диаграмме и возвращает (соединение, отчёт)."""
    path = os.path.join(tempfile.mkdtemp(prefix="er_od_"), "t.db")
    rep = dbgen.create_database(path, dbgen.diagram_to_tables(d), 0, 1)
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    return con, rep


def after_delete(rule, child_notnull=False, child_default=""):
    """Вставляет родителей 1 и 2 и ребёнка, ссылающегося на 1; удаляет родителя 1; возвращает (ошибка, строка ребёнка)."""
    con, rep = build(make(rule, child_notnull, child_default))
    con.executescript("insert into p values(1,'a'); insert into p values(2,'b'); insert into c values(10,1);")
    err = None
    try:
        con.execute("delete from p where id=1")
    except sqlite3.IntegrityError as exc:
        err = str(exc)
    return err, con.execute("select p_id from c where id=10").fetchall(), rep


# реальное поведение каждого правила в SQLite
err, rows, _ = after_delete("CASCADE")
check("CASCADE: дочерняя запись удалена", err is None and rows == [])
err, rows, _ = after_delete("RESTRICT")
check("RESTRICT: удаление запрещено", err is not None and rows == [(1,)])
err, rows, _ = after_delete("NO ACTION")
check("NO ACTION: удаление запрещено", err is not None and rows == [(1,)])
err, rows, _ = after_delete("SET NULL")
check("SET NULL: в ключ записан NULL", err is None and rows == [(None,)])
err, rows, _ = after_delete("SET DEFAULT", child_default="2")
check("SET DEFAULT: подставлено значение по умолчанию", err is None and rows == [(2,)], (err, rows))
err, rows, _ = after_delete("set  default", child_default="2")
check("регистр и пробелы в подписи не важны", err is None and rows == [(2,)])
err, rows, _ = after_delete("что-то странное")
check("непонятная подпись = NO ACTION", err is not None and rows == [(1,)])

# предупреждения
_, _, rep = after_delete("SET NULL", child_notnull=True)
check("SET NULL при NOT NULL: предупреждение", any("SET NULL" in w and "NOT NULL" in w for w in rep.warnings), rep.warnings)
_, _, rep = after_delete("SET DEFAULT")
check("SET DEFAULT без DEFAULT: предупреждение", any("SET DEFAULT" in w and "p_id" in w for w in rep.warnings), rep.warnings)
_, _, rep = after_delete("SET DEFAULT", child_default="1")
check("SET DEFAULT с DEFAULT: без предупреждений", rep.warnings == [], rep.warnings)
_, _, rep = after_delete("CASCADE", child_notnull=True)
check("CASCADE: без предупреждений", rep.warnings == [], rep.warnings)

# подсказка для панели связи
d = make("SET NULL", child_notnull=True)
check("подсказка: SET NULL и NOT NULL", "NOT NULL" in dbgen.link_rule_hint(d.nodes[1], d.links[0]))
d = make("SET DEFAULT")
check("подсказка: SET DEFAULT без DEFAULT", "DEFAULT" in dbgen.link_rule_hint(d.nodes[1], d.links[0]))
d = make("SET DEFAULT", child_default="1")
check("подсказка: проблем нет", dbgen.link_rule_hint(d.nodes[1], d.links[0]) == "")

# безопасность значения DEFAULT
check("default_sql: число, слова, строка", dbgen.default_sql(" 42 ") == "42" and dbgen.default_sql("-1.5") == "-1.5" and dbgen.default_sql("current_timestamp") == "CURRENT_TIMESTAMP"
      and dbgen.default_sql("null") == "NULL" and dbgen.default_sql("abc") == "'abc'" and dbgen.default_sql("") == "")
check("default_sql: апострофы экранируются", dbgen.default_sql("it's") == "'it''s'" and dbgen.default_sql("'x'") == "'x'")
check("default_sql: выражение в скобках из импорта", dbgen.default_sql("(datetime('now'))") == "(datetime('now'))")
evil = "x'); DROP TABLE p; --"
d = make("CASCADE", child_default=evil)
con, _ = build(d)
check("default_sql: инъекция не проходит, таблица на месте", con.execute("select count(*) from sqlite_master where name in ('p','c')").fetchone()[0] == 2
      and con.execute("select dflt_value from pragma_table_info('c') where name='p_id'").fetchone()[0] == "'x''); DROP TABLE p; --'")
check("default_sql: «;» в скобках не пропускается как выражение", dbgen.default_sql("(1); drop table p") == "'(1); drop table p'")

# импорт и круг: БД -> диаграмма -> БД сохраняет DEFAULT и правила
src = os.path.join(tempfile.mkdtemp(prefix="er_od_"), "s.db")
c = sqlite3.connect(src)
c.executescript("""create table p(id integer primary key);
create table c(id integer primary key, p_id integer not null default 1 references p(id) on delete set default, note text default 'n/a');""")
c.commit()
c.close()
d = schema.build_data(schema.read_schema(src))
rows = {r.name: r for n in d.nodes if n.title == "c" for r in n.rows}
check("импорт: DEFAULT прочитан", rows["p_id"].default == "1" and rows["note"].default == "'n/a'", (rows["p_id"].default, rows["note"].default))
check("импорт: правило прочитано", d.links[0].label == "SET DEFAULT")
con, rep = build(d)
dflt = dict(con.execute("select name, dflt_value from pragma_table_info('c')"))
check("круг: DEFAULT и правило сохранены", dflt["p_id"] == "1" and dflt["note"] == "'n/a'"
      and con.execute("select \"on_delete\" from pragma_foreign_key_list('c')").fetchone()[0] == "SET DEFAULT" and rep.warnings == [], (dflt, rep.warnings))

# проект из старого файла (без поля default) открывается
old = Diagram.from_dict({"kind": "data", "nodes": [{"id": "n1", "title": "t", "rows": [{"name": "a", "type": "INTEGER", "key": "PK", "notnull": True}]}]})
check("старый файл без default открывается", old.nodes[0].rows[0].default == "")
T.finish()

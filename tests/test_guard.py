"""Тест защитных триггеров: нагрузки блокируются при вставке и правке, обычный текст проходит, защита выключается."""
import os
import sqlite3
import tempfile

from common import Counter

from er_editor import dbgen
from er_editor.model import KIND_DATA, Diagram, Node, Project, Row, empty_project

T = Counter()
check = T.check


def make_tables(title="users"):
    """Таблица с текстовым, нетипизированным и числовым столбцами."""
    d = Diagram(KIND_DATA)
    d.nodes.append(Node(d.new_id("n"), title, rows=[Row("id", "", "INTEGER", "PK", True), Row("name", "", "VARCHAR(50)"),
                                                  Row("note", "", ""), Row("age", "", "INTEGER"), Row("born", "", "DATE")]))
    return dbgen.diagram_to_tables(d)


def build(guard, sample=0, title="users"):
    """Создаёт базу и возвращает (соединение, отчёт)."""
    path = os.path.join(tempfile.mkdtemp(prefix="er_guard_"), "g.db")
    rep = dbgen.create_database(path, make_tables(title), sample, 3, guard=guard)
    return sqlite3.connect(path), rep


def blocked(con, sql, params):
    """True, если оператор отклонён защитным триггером."""
    try:
        con.execute(sql, params)
    except sqlite3.IntegrityError as exc:
        return "Защита от инъекций" in str(exc)
    return False


ATTACKS = ["x' OR '1'='1", "'; DROP TABLE users; --", "1 UNION SELECT * FROM sqlite_master", "x' UNION ALL SELECT 1,2 --", "a'/**/or/**/1=1",
           "a\t'\tor\t1=1", "a'     or  1=1", "a'" + " " * 100 + "or 1=1", "X' Or 'a'='a", "admin'--", "admin' --", "admin' #", "1; DELETE FROM users",
           "<script>alert(1)</script>", "select load_extension('x')", "'; ATTACH DATABASE 'x' AS y; --", "1 or 1=1", "1 AND 1=1", "1 and sleep(5)",
           "' OR 1=1 --", '" OR ""="', "' OR '' = '", "')) OR 1=1--", "1' ORDER BY 3--", "'; EXEC xp_cmdshell('dir');--", "' AND 1=2 UNION SELECT 1,2,3--",
           "' OR 'a' LIKE 'a", "x'||(SELECT 1)||'", "1); DROP TABLE t--", "' AND (SELECT COUNT(*) FROM users) > 0 --", "%27 OR 1=1", "x'; UPDATE users SET name='h' --",
           "1; SELECT * FROM users", "1 UNION SELECT null, null", "' having 1=1 --", "a' or id>0 --", "1 or 2 = 2", "abc'; --", "' UNION SELECT name FROM t"]
# безобидный ввод: цитаты, точки с запятой, слова из SQL в обычной речи, пути, код, дефисы, пустые и короткие значения
SAFE = ["O'Brien", "Иван Петров", "select your plan", "a -- b", "Rock & Roll", "union of workers", "drop it", "100% organic",
 "тел.: +7 (900) 123-45-67", "it's fine; really", "a or b", "1+1=2",
 "he said 'no' or 'maybe'", 'she said "hi" or "bye"', "the 'cat' or the 'dog'", "he said 'hi'; she left", 'a "quoted"; then more',
 "credit union selection committee", "the union select committee meets", "Hello 'world' -- said John", "see 'a' -- 'b'",
 "wait; drop by later", "do it; update me on this", "ok; delete the old file", "first; insert coin", "plan; create a new account",
 "time.sleep(5) in the script", "/usr/local/*", "src/*.py and lib/*", "/* TODO: fix */", "javascript: void(0) is a pseudo-URL",
 "5 and 6", "1 or 2", "x = 5 or y = 6", "l'or et l'argent", "Côte-d'Or", "d'or", "'a' or 'b'", "Tom's and Jerry's",
 "select * is slow, avoid it", "where do you live?", "order by date, then name", "having fun; or not", "table of contents",
 "Smith & Sons; Ltd.", "price: 5 < 10 and 10 > 5", "a <b> tag", "e-mail: a@b.c", "C:\\Users\\Ann", "50% off -- today only",
 "ПРИВЕТ'; ПОКА", "SELECT is a SQL keyword", "my password is 'secret'", "'Quote' and \"double\"", "or", "and", "''", "'", '"',
 "He asked: 'Is it 2 or 3?'", "Use the 'AND' operator or the 'OR' operator", "-- comment-like line", "#hashtag 'x'"]

con, rep = build(True)
check("триггеры созданы (по два на таблицу)", rep.triggers == 2 and con.execute("select count(*) from sqlite_master where type='trigger'").fetchone()[0] == 2, rep.triggers)
for col in ("name", "note"):
    bad = [a for a in ATTACKS if not blocked(con, f"insert into users({col}) values(?)", (a,))]
    check(f"вставка в {col}: все нагрузки отклонены", not bad, bad)
    ok = [s for s in SAFE if blocked(con, f"insert into users({col}) values(?)", (s,))]
    check(f"вставка в {col}: обычный текст проходит", not ok, ok)
con.execute("delete from users")
con.execute("insert into users(id, name) values(1, 'Ann')")
bad = [a for a in ATTACKS if not blocked(con, "update users set name=? where id=1", (a,))]
check("правка: все нагрузки отклонены", not bad, bad)
bad = [x for x in SAFE if blocked(con, "update users set name=? where id=1", (x,))]
check("правка: безобидный ввод проходит", not bad, bad)
check("правка: обычное значение проходит", not blocked(con, "update users set name=? where id=1", ("Bob",)) and con.execute("select name from users").fetchone() == ("Bob",))
check("отклонённая запись не сохранилась", con.execute("select count(*) from users").fetchone()[0] == 1)
check("числовые столбцы не затронуты", not blocked(con, "insert into users(age, born) values(?, ?)", (5, "2020-01-01")))
check("NULL проходит", not blocked(con, "insert into users(name, note) values(NULL, NULL)", ()))
check("правка столбца вне защиты не запускает триггер", not blocked(con, "update users set age=7 where id=1", ()))
check("таблицы целы", con.execute("select count(*) from sqlite_master where name='users'").fetchone()[0] == 1)

# случайные данные не мешают и не отклоняются
con, rep = build(True, sample=5)
check("случайные данные вставлены полностью", rep.total_rows == 5 and rep.warnings == [] and rep.triggers == 2, (rep.rows, rep.warnings))

# выключено
con, rep = build(False)
check("защита выключена: триггеров нет, нагрузка проходит", rep.triggers == 0 and con.execute("select count(*) from sqlite_master where type='trigger'").fetchone()[0] == 0
      and not blocked(con, "insert into users(name) values(?)", ("x' OR '1'='1",)))

# необычные имена таблицы
con, rep = build(True, title='a"b\'c; d')
check("необычное имя таблицы", rep.triggers == 2 and blocked(con, 'insert into "a""b\'c; d"(name) values(?)', ("x' or 1=1",)))

# таблица без текстовых столбцов
d = Diagram(KIND_DATA)
d.nodes.append(Node(d.new_id("n"), "n", rows=[Row("id", "", "INTEGER", "PK", True), Row("v", "", "REAL")]))
check("без текстовых столбцов триггеров нет", dbgen.guard_sql(dbgen.diagram_to_tables(d)) == [])
check("is_text_type", dbgen.is_text_type("TEXT") and dbgen.is_text_type("varchar(9)") and dbgen.is_text_type("") and not dbgen.is_text_type("INTEGER")
      and not dbgen.is_text_type("DATE"))

# настройка проекта
p = empty_project()
check("по умолчанию защита включена", p.guard is True and Project.from_json(p.to_json()).guard is True)
p.guard = False
check("настройка сохраняется в файле проекта", Project.from_json(p.to_json()).guard is False)
check("старый файл без поля — включено", Project.from_dict({"diagrams": {}}).guard is True)
T.finish()

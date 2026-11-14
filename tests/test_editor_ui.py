"""Безголовый тест настольной программы: рисуем таблицы, связи-стрелки, проверяем НФ, скачиваем комплект."""
import os
import sqlite3
import tempfile

from common import Counter, make_shop_db

import pygame

from er_editor import app as appmod
from er_editor.model import KIND_DATA, KIND_INFO, Project, Row
from er_editor.render import Sel

T = Counter()
check = T.check
app = appmod.App()
app.screen = pygame.display.set_mode((1500, 920))
app.draw()


def frame():
    app.rescene()
    app.draw()


def click(pos):
    app.handle(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
    app.handle(pygame.event.Event(pygame.MOUSEBUTTONUP, pos=pos, button=1))
    app.last_click = (0.0, (0, 0))
    frame()


def drag(a, b):
    app.handle(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=a, button=1))
    app.last_click = (0.0, (0, 0))
    for i in range(1, 9):
        p = (int(a[0] + (b[0] - a[0]) * i / 8), int(a[1] + (b[1] - a[1]) * i / 8))
        app.handle(pygame.event.Event(pygame.MOUSEMOTION, pos=p, rel=(0, 0), buttons=(1, 0, 0)))
    app.handle(pygame.event.Event(pygame.MOUSEBUTTONUP, pos=b, button=1))
    frame()


def tb_click(label):
    for it, r in app.toolbar_layout()[1]:
        if it.label == label or it.label.startswith(label):
            click(r.center)
            return it
    raise AssertionError("нет кнопки " + label)


def tb_item(label):
    return next(it for it, r in app.toolbar_layout()[1] if it.label == label or it.label.startswith(label))


def center(nid):
    r = app.scene.nodes[nid].rect
    return tuple(int(v) for v in app.w2s(r.cx, r.y + 12))


def set_table(n, title, rows):
    n.title = title
    n.rows = [Row(name=r[0], type=r[1], key=r[2] if len(r) > 2 else "", notnull=bool(r[3]) if len(r) > 3 else False) for r in rows]


# ───── 1. рисуем таблицы ─────
check("старт: пустая схема", not app.project.diagrams[KIND_DATA].nodes and app.nf_report().tables == 0)
check("кнопка вердикта: нет таблиц", tb_item("НФ:").label == "НФ: нет таблиц")
tb_click("Даталогическая")
d = app.project.diagrams[KIND_DATA]
check("data.arrows и легенда со стрелкой", d.arrows and any(i.kind == "arrow" for i in d.legend.items))
for _ in range(3):
    tb_click("+ Таблица")
    app.end_edit(True)
    frame()
check("3 таблицы с уникальными именами", len(d.nodes) == 3 and len({n.title for n in d.nodes}) == 3, [n.title for n in d.nodes])
users, orders, items = d.nodes
set_table(users, "users", [("id", "INTEGER", "PK", True), ("name", "TEXT"), ("email", "TEXT", "U")])
set_table(orders, "orders", [("id", "INTEGER", "PK", True), ("created", "DATE")])
set_table(items, "order_items", [("order_id", "INTEGER", "PK,FK", True), ("product_id", "INTEGER", "PK", True), ("qty", "INTEGER"),
                                 ("product_name", "TEXT")])
for n, (x, y) in zip(d.nodes, [(60, 60), (460, 60), (260, 340)]):
    n.x, n.y = x, y
app.grow()
frame()

# ───── 2. связи ─────
tb_click("Связь")
drag(center(orders.id), center(users.id))
check("связь протягиванием", len(d.links) == 1 and d.links[0].a == orders.id and d.links[0].b == users.id)
check("режим связи сброшен", app.tool == "select" and app.link_src is None)
check("FK-столбец добавлен", any(r.name == "users_id" and "FK" in r.key for r in orders.rows))
check("fk_cols/ref_cols", d.links[0].fk_cols == ["users_id"] and d.links[0].ref_cols == ["id"])
tb_click("Связь")
click(center(items.id))
check("первый клик — источник", app.link_src == items.id)
click(center(orders.id))
check("связь двумя кликами", len(d.links) == 2 and d.links[1].a == items.id and d.links[1].b == orders.id)
tb_click("Связь")
drag(center(users.id), (1100, 800))
check("протягивание в пустоту отменяет", len(d.links) == 2 and app.link_src is None and app.tool == "link")
app.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE, mod=0, unicode="", scancode=41))
check("Esc выходит из режима связи", app.tool == "select")
items.rows = [r for r in items.rows if r.name != "order_id"]
for r in items.rows:
    if r.name == "orders_id":
        r.key, r.notnull = "PK,FK", True
d.links[1].fk_cols, d.links[1].ref_cols = ["orders_id"], ["id"]
frame()

# ───── 3. вердикты ─────
rep = app.nf_report()
check("3НФ по умолчанию и соблюдена", rep.target == 3 and rep.ok, [(i.rule, i.message) for i in rep.violations])
tb_click("2НФ")
app.set_fds(items.id, "product_id -> product_name; qty ->")
check("ФЗ сохранена, битая отброшена", items.fds == ["product_id -> product_name"], items.fds)
check("2НФ нарушена, кнопка красная", not app.nf_report().ok and tb_item("2НФ:").tone == "bad")
tb_click("1НФ")
check("1НФ соблюдена", app.nf_report().ok)
tb_click("2НФ")
flags = app.nf_flags()
check("подсветка order_items и строки", items.id in flags and flags[items.id].level == "error"
      and any(items.rows[i].name == "product_name" for i in flags[items.id].rows))
check("подсветка не затрагивает другие таблицы", users.id not in flags and orders.id not in flags)
app.sel = Sel("node", users.id)
tb_click("2НФ:")
check("клик по вердикту: отчёт без выделения", app.sel.kind is None)
frame()
cards = [r for r, cb, tip in app.regions if tip == "Показать на схеме"]
check("карточки замечаний", len(cards) >= 1)
click(cards[0].center)
check("клик по карточке выделяет таблицу", app.sel.kind == "node" and app.sel.id == items.id and app.sel.row is not None)
idx = [r.name for r in items.rows].index("product_name")
app.r_set(items.id, idx, name="pname")
check("переименование обновило ФЗ", items.fds == ["product_id -> pname"], items.fds)
app.undo()
d = app.project.diagrams[KIND_DATA]
users, orders, items = (d.node(x.id) for x in (users, orders, items))
check("undo вернул имя и ФЗ", items.fds == ["product_id -> product_name"])

# ───── 4. шлюз скачивания ─────
tb_click("2НФ")
app.download_db_dialog()
check("нарушение НФ: вопрос", app.modal is not None and "не соответствует" in app.modal.title)
check("кнопки окна", [b[0] for b in app.modal.buttons] == ["Показать нарушения", "Скачать всё равно", "Отмена"])
app.modal.buttons[0][1]()
app.modal = None
check("показать нарушения", app.kind == KIND_DATA and app.sel.kind is None)
app.jobs.clear()
app.download_db_dialog()
app.modal.buttons[1][1]()
app.modal = None
check("«Скачать всё равно» открывает диалог", len(app.jobs) == 1)
app.jobs.clear()
tb_click("1НФ")
app.download_db_dialog()
check("1НФ соблюдена: сразу диалог", app.modal is None and len(app.jobs) == 1)
app.jobs.clear()

# ───── 5. комплект ─────
tmp = tempfile.mkdtemp(prefix="nf_bundle_")
app.project.fill_sample = True
check("инфологическая пуста до комплекта", not app.project.diagrams[KIND_INFO].nodes)
res = app.create_bundle(os.path.join(tmp, "shop"))
files = sorted(os.listdir(tmp))
check("комплект: 4 файла", res is True and files == ["shop.db", "shop_datalogical.png", "shop_infological.png", "shop_normal_form.md"], files)
check("инфологическая построена", len(app.project.diagrams[KIND_INFO].nodes) == 3)
check("PNG непустые", all(os.path.getsize(os.path.join(tmp, f)) > 3000 for f in files if f.endswith(".png")))
con = sqlite3.connect(os.path.join(tmp, "shop.db"))
tbls = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
check("таблицы и по 5 записей", tbls == ["order_items", "orders", "users"] and all(con.execute(f"select count(*) from {t}").fetchone()[0] == 5 for t in tbls))
check("FK целы", con.execute("pragma foreign_key_check").fetchall() == [] and con.execute("pragma foreign_key_list(orders)").fetchone()[2] == "users")
con.close()
check("отчёт .md", "Схема соответствует 1НФ" in open(os.path.join(tmp, "shop_normal_form.md"), encoding="utf-8").read())
before = app.project.diagrams[KIND_INFO]
check("повторный комплект не трогает инфологическую",
      app.create_bundle(os.path.join(tempfile.mkdtemp(), "again.db")) is True and app.project.diagrams[KIND_INFO] is before)

# ───── 6. вид связей, сохранение, импорт ─────
app.set_arrows(False)
check("лапка: легенда обновилась", any(i.kind == "crow" for i in d.legend.items) and not d.arrows)
app.set_arrows(True)
check("стрелка: легенда обновилась", any(i.kind == "arrow" for i in d.legend.items))
proj = os.path.join(tmp, "p.erd.json")
app.project.save(proj)
p2 = Project.load(proj)
n2 = next(n for n in p2.diagrams[KIND_DATA].nodes if n.title == "order_items")
check("ФЗ, arrows и форма сохраняются", n2.fds == ["product_id -> product_name"] and p2.diagrams[KIND_DATA].arrows and p2.normal_form == 1)
app2 = appmod.App(make_shop_db())
app2.screen = pygame.display.set_mode((1500, 920))
app2.draw()
check("импорт БД: таблицы, связи, стрелки", len(app2.project.diagrams[KIND_DATA].nodes) == 4 and len(app2.project.diagrams[KIND_DATA].links) == 3
      and app2.project.diagrams[KIND_DATA].arrows)
check("тулбар 1–2 строки без наложений", app.toolbar_layout()[0] <= 2 and all(
    not a[1].colliderect(b[1]) for i, a in enumerate(app.toolbar_layout()[1]) for b in app.toolbar_layout()[1][i + 1:]))
T.finish()

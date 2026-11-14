"""Общие помощники тестов: путь к пакету, счётчик проверок, тестовая база SQLite."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class Counter:
    """Считает пройденные и упавшие проверки и печатает итог."""

    def __init__(self) -> None:
        self.ok = 0
        self.fail = 0

    def check(self, name, cond, extra=""):
        if cond:
            self.ok += 1
        else:
            self.fail += 1
            print("FAIL:", name, extra)

    def finish(self):
        print(f"OK {self.ok}, FAIL {self.fail}")
        sys.exit(1 if self.fail else 0)


def make_shop_db() -> str:
    """Создаёт во временной папке небольшую базу: customers, products, orders, order_items."""
    path = os.path.join(tempfile.mkdtemp(prefix="er_test_"), "shop.db")
    con = sqlite3.connect(path)
    con.executescript("""
    create table customers(id integer primary key, name text not null, city text, country text);
    create table products(id integer primary key, title text not null, price real);
    create table orders(id integer primary key, customer_id integer not null references customers(id) on delete cascade, created date);
    create table order_items(order_id integer not null references orders(id), product_id integer not null references products(id),
                             qty integer, primary key(order_id, product_id));
    """)
    con.commit()
    con.close()
    return path

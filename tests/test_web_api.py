"""Тест веб-сервера: запускает его на свободном порту и проверяет все запросы API и защиту."""
import io
import json
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from common import Counter, make_shop_db

from er_editor.web import server

T = Counter()
check = T.check
srv = server.make_server("127.0.0.1", 8765)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()


def call(method, path, body=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


for page, ctype in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css"), ("/core.js", "text/javascript")):
    st, b, h = call("GET", page)
    check(f"статика {page}", st == 200 and ctype in h["Content-Type"] and len(b) > 100, (st, h.get("Content-Type")))
st, b, h = call("GET", "/")
check("index подключает модуль app.js", b"/app.js" in b and b'type="module"' in b)

st, b, _ = call("GET", "/api/meta")
meta = json.loads(b)
check("meta", st == 200 and meta["sample_rows"] == 5 and meta["levels"]["3"]["name"] == "3НФ" and set(meta["legends"]) == {"info", "data_arrows", "data_crow"}, meta.keys())
st, b, _ = call("GET", "/api/new")
proj = json.loads(b)
check("new", st == 200 and set(proj["diagrams"]) == {"info", "data"} and proj["diagrams"]["data"]["arrows"] is True)
st, b, _ = call("GET", "/api/readme")
check("readme в HTML", st == 200 and "<h1>ER Editor</h1>" in json.loads(b)["html"] and "<table>" in json.loads(b)["html"])

db = open(make_shop_db(), "rb").read()
st, b, _ = call("POST", "/api/import?name=shop.db", raw=db, headers={"Content-Type": "application/octet-stream"})
imp = json.loads(b)
check("import", st == 200 and len(imp["diagrams"]["data"]["nodes"]) == 4 and len(imp["diagrams"]["data"]["links"]) == 3 and imp["db_path"] == "shop.db", st)
check("import: не база -> 400", call("POST", "/api/import", raw=b"not a database")[0] == 400)
check("import: пусто -> 400", call("POST", "/api/import", raw=b"")[0] == 400)

st, b, _ = call("POST", "/api/check", {"diagram": imp["diagrams"]["data"], "level": 3})
rep = json.loads(b)
check("check", st == 200 and rep["ok"] and rep["target"] == 3 and rep["tables"] == 4, rep.get("verdict"))
check("check: info -> 400", call("POST", "/api/check", {"diagram": imp["diagrams"]["info"], "level": 3})[0] == 400)
check("check: level 9 -> 400", call("POST", "/api/check", {"diagram": imp["diagrams"]["data"], "level": 9})[0] == 400)
check("check: плохой JSON -> 400", call("POST", "/api/check", raw=b"{oops")[0] == 400)

st, b, _ = call("POST", "/api/layout", {"diagram": imp["diagrams"]["data"]})
check("layout", st == 200 and len(json.loads(b)["diagram"]["nodes"]) == 4)
ids = [l["id"] for l in imp["diagrams"]["data"]["links"]][:2]
st, b, _ = call("POST", "/api/route", {"diagram": imp["diagrams"]["data"], "ids": ids})
check("route", st == 200 and set(json.loads(b)["links"]) == set(ids) and "sa" in json.loads(b)["links"][ids[0]])
st, b, _ = call("POST", "/api/info", {"diagram": imp["diagrams"]["data"]})
check("info", st == 200 and json.loads(b)["diagram"]["kind"] == "info" and len(json.loads(b)["diagram"]["nodes"]) == 4)

st, b, h = call("POST", "/api/database?name=my%20shop", {"project": imp, "sample": True})
check("database: файл и имя", st == 200 and b[:15] == b"SQLite format 3" and "my_shop.db" in h.get("Content-Disposition", ""), (st, h))
path = tempfile.mktemp(suffix=".db")
open(path, "wb").write(b)
con = sqlite3.connect(path)
check("database: по 5 записей", all(con.execute(f'select count(*) from "{t}"').fetchone()[0] == 5 for (t,) in con.execute("select name from sqlite_master where type='table'")))
report = json.loads(urllib.parse.unquote(h["X-Db-Report"]))
check("заголовок X-Db-Report", report["tables"] == 4 and report["rows"] == 20, report)
st, b, _ = call("POST", "/api/database", {"project": imp, "sample": False})
path2 = tempfile.mktemp(suffix=".db")
open(path2, "wb").write(b)
check("database: пустая", sqlite3.connect(path2).execute("select count(*) from customers").fetchone()[0] == 0)
bad = json.loads(json.dumps(imp))
bad["diagrams"]["data"]["nodes"][0]["rows"][0]["name"] = bad["diagrams"]["data"]["nodes"][0]["rows"][1]["name"]
st, b, _ = call("POST", "/api/database", {"project": bad})
check("database: проблемы схемы -> 422", st == 422 and json.loads(b)["problems"], (st, b[:100]))
check("database: пустая схема -> 422", call("POST", "/api/database", {"project": json.loads(call("GET", "/api/new")[1])})[0] == 422)

st, b, h = call("POST", "/api/png?name=x", {"diagram": imp["diagrams"]["data"], "scale": 1})
check("png", st == 200 and b[:8] == b"\x89PNG\r\n\x1a\n" and "x_datalogical.png" in h["Content-Disposition"], (st, b[:30]))
proj2 = json.loads(json.dumps(imp))
proj2["diagrams"]["info"] = json.loads(call("GET", "/api/new")[1])["diagrams"]["info"]
st, b, h = call("POST", "/api/bundle?name=shop", {"project": proj2, "sample": True})
z = zipfile.ZipFile(io.BytesIO(b)) if st == 200 else None
check("bundle: 4 файла (инфологическая строится)", st == 200 and sorted(z.namelist()) == ["shop.db", "shop_datalogical.png", "shop_infological.png", "shop_normal_form.md"], (st, b[:80]))
check("bundle: отчёт", "Схема соответствует 3НФ" in z.read("shop_normal_form.md").decode())

check("meta: образцы описания", set(meta["examples"]) == {"text", "sql", "prose"} and "Клиент" in meta["examples"]["text"])
st, b, _ = call("POST", "/api/describe", {"text": meta["examples"]["text"]})
res = json.loads(b)
check("describe: текст", st == 200 and res["format"] == "text" and res["tables"] == 5 and res["links"] == 4, (st, b[:120]))
check("describe: обе диаграммы с раскладкой", len(res["project"]["diagrams"]["info"]["nodes"]) == 5 and res["project"]["diagrams"]["data"]["arrows"]
      and any(n["x"] or n["y"] for n in res["project"]["diagrams"]["data"]["nodes"]))
st, b, _ = call("POST", "/api/describe", {"text": meta["examples"]["sql"]})
check("describe: SQL", st == 200 and json.loads(b)["format"] == "sql" and json.loads(b)["tables"] == 2, (st, b[:120]))
st, b, _ = call("POST", "/api/describe", {"text": "просто слова"})
check("describe: непонятное -> 422 с problems", st == 422 and json.loads(b)["problems"], (st, b[:120]))
check("describe: нет text -> 400", call("POST", "/api/describe", {"x": 1})[0] == 400)
check("describe: результат скачивается как БД", call("POST", "/api/database", {"project": res["project"], "sample": True})[2]["Content-Type"] == "application/x-sqlite3")
st, b, _ = call("POST", "/api/fds", raw=db, headers={"Content-Type": "application/octet-stream"})
check("fds: ответ по таблицам", st == 200 and {t["name"] for t in json.loads(b)["tables"]} == {"customers", "products", "orders", "order_items"}, (st, b[:100]))
check("fds: не база -> 400", call("POST", "/api/fds", raw=b"not a database")[0] == 400)
check("fds: пусто -> 400", call("POST", "/api/fds", raw=b"")[0] == 400)
check("path traversal %2f", call("GET", "/..%2fREADME.md")[0] == 404)
check("path traversal ..", call("GET", "/static/../x")[0] == 404)
check("неизвестный API", call("GET", "/api/nope")[0] == 404)
check("чужой Host -> 403", call("GET", "/", headers={"Host": "evil.example.com"})[0] == 403)
check("битая диаграмма -> 400", call("POST", "/api/png", {"diagram": {"kind": "data", "nodes": [{"bad": 1}]}})[0] == 400)
check("safe_name", server.safe_name("../../etc/pass wd.db") == "pass_wd" and server.safe_name("") == "schema" and server.safe_name("моя схема.erd.json") == "моя_схема")
T.finish()
